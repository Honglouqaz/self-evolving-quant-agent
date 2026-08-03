#!/usr/bin/env python3
"""
decision_reflector.py — 决策记忆闭环 (Decision Memory Loop)
============================================================
借鉴 TradingAgents 的 decision-log → reflection → injection 设计，
为 K1/S1/T1 三系统提供"平仓后反思 + 下次开仓前注入"的轻量闭环。

三层结构:
─────────
1. 记录 (record_close):  平仓时调用，把 (系统/标的/方向/盈亏/退出原因/归因) 追加到 memory 文件
2. 反思 (reflect):       规则驱动生成一句话归因(盈利模式 vs 失败模式)，不依赖 LLM，轻量可审计
3. 注入 (get_symbol_adjustment): 开仓评估时调用，查询同标的近期胜率/连亏/教训
                         → 返回 (仓位乘数, 建议文本)，供策略降仓或加仓

数据文件: PROJECT_ROOT/decision_memory.jsonl (每行一条平仓记录)
用法:
  from decision_reflector import record_close, get_symbol_adjustment
  record_close("S1", "BTC", "LONG", entry=50000, exit=51000, pnl_pct=2.0, exit_reason="三档止盈T1", meta={...})
  mult, advice = get_symbol_adjustment("S1", "BTC", "LONG")
  if mult < 1.0:  # 同标的近期胜率低 → 降仓
      qty = qty * mult
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))  # 北京时间
MEMORY_FILE = "PROJECT_ROOT/decision_memory.jsonl"

# ── 配置 ────────────────────────────────────────────
LOOKBACK = 10          # 默认查询最近 N 笔
WIN_RATE_THRESHOLD = 0.4   # 胜率低于 40% → 降仓
CONSEC_LOSS_LIMIT = 2      # 同标的连亏 ≥2 → 降仓
POS_MULT_LOW = 0.7         # 降仓乘数
POS_MULT_HIGH = 1.15       # 连胜加仓乘数（胜率≥70% 且 ≥3 笔）
STRONG_WIN_RATE = 0.7
MIN_SAMPLES_FOR_BOOST = 3

# 退出原因 → 归因分类
REASON_TAG = {
    "止损": "入场失败", "硬止损": "入场失败", "结构止损": "入场失败",
    "stop_loss": "入场失败", "stop_loss_trigger": "入场失败",
    "三档止盈": "趋势正确", "移动止盈": "趋势正确", "趋势反转": "趋势正确",
    "partial_tp_trigger": "趋势正确", "trailing_stop_trigger": "趋势正确",
    "乖离止损": "追高失败", "超时平仓": "震荡粘滞", "timeout_exit": "震荡粘滞",
    "宏观风控": "外部风险", "ETF超时": "规则退出", "信号驱动": "信号退出",
}


def _now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def _read_memory(limit=None):
    """读取 memory 文件，返回记录列表(最新在前)。"""
    records = []
    if not os.path.exists(MEMORY_FILE):
        return records
    try:
        with open(MEMORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return records
    records.reverse()
    if limit:
        return records[:limit]
    return records


def _append(rec: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record_close(system, symbol, direction, pnl_pct, exit_reason="",
                 entry=None, exit=None, qty=None, meta=None):
    """平仓时调用，记录一笔已完成交易并生成反思。
    
    参数:
        system: "K1"/"S1"/"T1"
        symbol: 标的 (如 "BTC" 或 "BTCUSDT", 自动归一化)
        direction: "LONG"/"SHORT" (或 "long"/"short")
        pnl_pct: 平仓盈亏百分比 (如 -3.5)
        exit_reason: 退出原因 (止损/三档止盈/超时...)
        entry/exit/qty: 可选，记录价格和数量
        meta: dict, 额外上下文 (score/mode/regime 等)
    返回: (record, reflection) — 记录和反思文本
    
    [FIX 2026-08-03] 数据校验: 过滤荒谬盈亏 (entry_price=0兜底导致的 -416万% 污染数据)
    """
    sym = symbol.replace("USDT", "")
    direction = direction.upper()

    # ── 数据校验: pnl_pct 必须在合理范围 (-100% ~ +1000%) ──
    try:
        pnl_f = float(pnl_pct)
    except (TypeError, ValueError):
        return None, "数据校验: pnl_pct非法, 丢弃"
    # 加密市场无涨跌幅但单笔盈亏超 ±200% 必为数据错误 (entry_price=0兜底/价格错位)
    if abs(pnl_f) > 200.0:
        return None, f"数据校验: pnl_pct={pnl_f:.0f}% 超出±200%合理范围, 丢弃(疑似entry_price兜底污染)"
    # entry=0 或 entry 远小于实际价 → 可疑
    if entry is not None:
        try:
            entry_f = float(entry)
            if entry_f <= 0:
                return None, f"数据校验: entry={entry_f}≤0, 丢弃"
        except (TypeError, ValueError):
            return None, "数据校验: entry非法, 丢弃"

    tag = "未知"
    for key, val in REASON_TAG.items():
        if key in str(exit_reason):
            tag = val
            break
    if not exit_reason and pnl_f < 0:
        tag = "亏损平仓"

    reflection = _reflect(sym, direction, pnl_f, tag, exit_reason)

    rec = {
        "ts": int(time.time()),
        "time": _now_str(),
        "system": system, "symbol": sym, "direction": direction,
        "pnl_pct": round(pnl_f, 3),
        "exit_reason": str(exit_reason), "tag": tag,
        "reflection": reflection,
    }
    if entry is not None:
        rec["entry"] = float(entry)
    if exit is not None:
        rec["exit"] = float(exit)
    if qty is not None:
        rec["qty"] = float(qty)
    if meta:
        rec["meta"] = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}

    _append(rec)
    return rec, reflection


def _reflect(symbol, direction, pnl_pct, tag, exit_reason):
    """规则驱动反思：根据盈亏和归因生成一句话。"""
    d = "做多" if direction == "LONG" else "做空"
    if pnl_pct >= 0:
        if tag in ("趋势正确", "信号退出"):
            return f"{symbol} {d}盈利{pnl_pct:+.1f}%({exit_reason}) — 入场逻辑正确, 可维持同模式"
        return f"{symbol} {d}盈利{pnl_pct:+.1f}%({exit_reason}) — 方向正确"
    if tag == "入场失败":
        return f"{symbol} {d}亏损{pnl_pct:+.1f}%({exit_reason}) — 入场失败, 同条件需降仓或跳过"
    if tag == "追高失败":
        return f"{symbol} {d}亏损{pnl_pct:+.1f}%({exit_reason}) — 追高被套, 避免乖离过大时入场"
    if tag == "震荡粘滞":
        return f"{symbol} {d}亏损{pnl_pct:+.1f}%({exit_reason}) — 震荡行情粘滞, 降低持有时间预期"
    if tag == "外部风险":
        return f"{symbol} {d}亏损{pnl_pct:+.1f}%({exit_reason}) — 外部事件触发, 与策略无关"
    return f"{symbol} {d}亏损{pnl_pct:+.1f}%({exit_reason}) — 记录复盘"


def get_symbol_history(system=None, symbol=None, direction=None, n=LOOKBACK):
    """查询指定标的(或系统)的近期交易记录。"""
    records = _read_memory(limit=None)
    out = []
    for r in records:
        if system and r.get("system") != system:
            continue
        if symbol and r.get("symbol") != symbol.replace("USDT", ""):
            continue
        if direction and r.get("direction") != direction.upper():
            continue
        out.append(r)
        if len(out) >= n:
            break
    return out


def get_symbol_adjustment(system, symbol, direction):
    """开仓评估时调用：查询同标的近期表现 → 返回 (仓位乘数, 建议文本)。
    
    规则:
      - 同标的最近 LOOKBACK 笔: 胜率 < 40% → ×0.7 降仓
      - 同标的连亏 ≥2 → ×0.7 降仓
      - 同标的胜率 ≥70% 且 ≥3 笔 → ×1.15 加仓
      - 无记录 → ×1.0 正常
    """
    sym = symbol.replace("USDT", "")
    direction = direction.upper()
    hist = get_symbol_history(system, sym, direction, n=LOOKBACK)

    if not hist:
        return 1.0, f"[决策记忆] {sym} {direction} 无历史记录, 正常仓位"

    wins = sum(1 for r in hist if r.get("pnl_pct", 0) >= 0)
    losses = len(hist) - wins
    win_rate = wins / len(hist)

    # 连亏检测
    consec = 0
    for r in hist:  # hist 最新在前
        if r.get("pnl_pct", 0) < 0:
            consec += 1
        else:
            break

    # 最近失败教训
    lessons = [r.get("reflection", "") for r in hist if r.get("pnl_pct", 0) < 0][:2]

    if win_rate < WIN_RATE_THRESHOLD and len(hist) >= 3:
        return POS_MULT_LOW, (f"[决策记忆] {sym} {direction} 近{len(hist)}笔胜率{win_rate:.0%} "
                              f"< {WIN_RATE_THRESHOLD:.0%} → 降仓×{POS_MULT_LOW} | {'; '.join(lessons)}")
    if consec >= CONSEC_LOSS_LIMIT:
        return POS_MULT_LOW, (f"[决策记忆] {sym} {direction} 连亏{consec}笔 → 降仓×{POS_MULT_LOW} "
                              f"| {'; '.join(lessons)}")
    if win_rate >= STRONG_WIN_RATE and len(hist) >= MIN_SAMPLES_FOR_BOOST:
        return POS_MULT_HIGH, (f"[决策记忆] {sym} {direction} 近{len(hist)}笔胜率{win_rate:.0%} "
                               f"≥ {STRONG_WIN_RATE:.0%} → 加仓×{POS_MULT_HIGH}")
    return 1.0, (f"[决策记忆] {sym} {direction} 近{len(hist)}笔胜率{win_rate:.0%} "
                 f"({wins}胜{losses}负) → 正常仓位")


def get_adaptive_params(system, symbol=None, direction=None, base_stop=4.0, base_tp=6.0):
    """[FIX 2026-08-03] 参数自适应: 基于历史盈亏分布推荐止损/止盈/仓位调整。
    
    规则 (样本≥5笔时生效):
      - 平均亏损 > 止损 → 止损偏小(常被扫损), 建议放大止损
      - 平均亏损 < 止损×0.5 → 止损偏大(扛单), 建议缩小止损
      - 胜率低+亏损大 → 整体降仓
    返回: (建议dict, 说明文本)
    """
    hist = get_symbol_history(system=system, symbol=symbol, direction=direction, n=10)
    if len(hist) < 5:
        return {"stop_pct": base_stop, "tp_pct": base_tp, "pos_mult": 1.0}, \
               f"[参数自适应] {system} {symbol or '全标的'} 样本{len(hist)}<5, 用默认止损{base_stop}%止盈{base_tp}%"

    losses = [r.get("pnl_pct", 0) for r in hist if r.get("pnl_pct", 0) < 0]
    wins = [r.get("pnl_pct", 0) for r in hist if r.get("pnl_pct", 0) >= 0]
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    win_rate = len(wins) / len(hist)

    adj = {"stop_pct": base_stop, "tp_pct": base_tp, "pos_mult": 1.0}
    notes = []

    # 止损自适应
    if avg_loss > base_stop * 1.5:  # 平均亏损远超止损 → 常被扫损/滑点大
        adj["stop_pct"] = round(min(avg_loss * 1.3, base_stop * 2.5), 1)
        notes.append(f"平均亏损{avg_loss:.1f}%>止损{base_stop}%×1.5 → 止损放宽至{adj['stop_pct']}%")
    elif avg_loss > 0 and avg_loss < base_stop * 0.5:  # 亏损远小于止损 → 止损偏大扛单
        adj["stop_pct"] = round(max(base_stop * 0.7, avg_loss * 1.5), 1)
        notes.append(f"平均亏损{avg_loss:.1f}%<止损{base_stop}%×0.5 → 止损收窄至{adj['stop_pct']}%")

    # 胜率+盈亏比 → 仓位
    if win_rate < 0.35 and avg_win < avg_loss:
        adj["pos_mult"] = 0.7
        notes.append(f"胜率{win_rate:.0%}+盈亏比{avg_win:.1f}/{avg_loss:.1f}差 → 降仓×0.7")
    elif win_rate >= 0.65 and avg_win > avg_loss:
        adj["pos_mult"] = 1.15
        notes.append(f"胜率{win_rate:.0%}+盈亏比佳 → 加仓×1.15")

    note = f"[参数自适应] {system} {symbol or '全标的'}: " + (" | ".join(notes) if notes else "参数无需调整")
    return adj, note


def stats_summary(system=None, n=50):
    """生成最近 n 笔的统计摘要（供日志/看板）。"""
    records = _read_memory(limit=n)
    if system:
        records = [r for r in records if r.get("system") == system]
    if not records:
        return "决策记忆: 暂无记录"
    total = len(records)
    wins = sum(1 for r in records if r.get("pnl_pct", 0) >= 0)
    avg_pnl = sum(r.get("pnl_pct", 0) for r in records) / total
    tags = {}
    for r in records:
        t = r.get("tag", "未知")
        tags[t] = tags.get(t, 0) + 1
    tag_str = ", ".join(f"{k}{v}" for k, v in sorted(tags.items(), key=lambda x: -x[1])[:4])
    return (f"决策记忆: 近{total}笔 胜率{wins/total:.0%} 平均盈亏{avg_pnl:+.2f}% "
            f"| 归因: {tag_str}")


def show_recent(n=15):
    """打印最近 n 条记录（调试用）。"""
    records = _read_memory(limit=n)
    for r in records:
        print(f"[{r.get('time')}] {r.get('system')} {r.get('symbol')} "
              f"{r.get('direction')} {r.get('pnl_pct'):+.1f}% "
              f"({r.get('exit_reason')}) → {r.get('reflection')}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        show_recent(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
    elif len(sys.argv) > 1 and sys.argv[1] == "--stats":
        print(stats_summary())
    else:
        # 自测
        print("== decision_reflector 自测 ==")
        rec, ref = record_close("S1", "BTCUSDT", "LONG", -3.2, "止损", entry=50000, exit=48400, qty=0.001)
        print(f"记录: {rec['symbol']} {rec['direction']} {rec['pnl_pct']}% ({rec['tag']})")
        print(f"反思: {ref}")
        mult, advice = get_symbol_adjustment("S1", "BTC", "LONG")
        print(f"注入: mult={mult} | {advice}")
        print(stats_summary())
