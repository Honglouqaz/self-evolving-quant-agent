#!/usr/bin/env python3
"""
market_supervisor.py — 三大量化系统总监督层 (Meta-Supervisor)
==============================================================
站在三个总体维度监督市场，输出对 K1/S1/T1 的操作指导：
  ① 市场阶段 (phase): launch/main_up/bottom_building/top_building/retreat + strength
  ② 资金炒作节奏 (capital): 主流资金流入/流出 + 板块轮动 + 外部资金分流 + 链上信号
  ③ 情绪周期 (sentiment): safe/caution/danger + 恐慌/贪婪映射

输出: DATA_DIR/quant_supervisor/directive.json
    每维度状态 + 三系统各自指导 (方向/时间维度/空间维度/仓位系数/频率系数)

设计原则:
  - 纯规则引擎, 无 LLM 依赖, 确定性可审计
  - 只读取现有数据源, 不改动三大系统核心逻辑
  - 输出系数供三大系统接入 (类似 day_week_filter 模式)
"""
import os, sys, json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "PROJECT_ROOT")

CST = timezone(timedelta(hours=8))
PHASE_FILE = "DATA_DIR/phase_current.json"
ONCHAIN_DB = "DATA_DIR/one_data.db"
OUT_DIR = "DATA_DIR/quant_supervisor"
OUT_FILE = os.path.join(OUT_DIR, "directive.json")

# ── 阶段语义 (镜像设计, 与 day_week_filter 一致) ──
STRONG_PHASES = ("launch", "main_up", "bottom_building")   # 做多顺势
WEAK_PHASES = ("top_building", "retreat")                   # 做空顺势

PHASE_CN = {
    "launch": "龙头启动", "main_up": "主升", "bottom_building": "筑底",
    "top_building": "筑顶", "retreat": "退潮", "unknown": "未知",
}

# 阶段 → K线时间维度偏好 (持仓周期/入场节奏)
# 主升/启动: 快节奏 5m/15m 短线; 筑底: 15m/1h 布局等确认; 筑顶: 15m 谨慎; 退潮: 降频观望
PHASE_TIMEFRAME = {
    "launch":        {"cycle": "5m/15m", "hold": "快进快出", "entry_rhythm": "追龙头突破"},
    "main_up":       {"cycle": "5m/15m", "hold": "短线趋势", "entry_rhythm": "顺势加仓"},
    "bottom_building": {"cycle": "15m/1h", "hold": "中线布局", "entry_rhythm": "低位承接等待确认"},
    "top_building":  {"cycle": "15m",     "hold": "短线谨慎", "entry_rhythm": "轻仓快跑/冲顶做空"},
    "retreat":       {"cycle": "1h",      "hold": "观望为主", "entry_rhythm": "超跌反弹或做空"},
    "unknown":       {"cycle": "15m",     "hold": "正常",     "entry_rhythm": "标准入场"},
}

# 阶段 → 空间维度偏好
PHASE_SPACE = {
    "launch":        {"entry": "突破前高放量", "exit": "乖离过大减仓"},
    "main_up":       {"entry": "回踩EMA不破", "exit": "三档止盈梯度"},
    "bottom_building": {"entry": "箱体下沿承接", "exit": "箱体上沿/前高"},
    "top_building":  {"entry": "冲顶背离做空", "exit": "反弹到位离场"},
    "retreat":       {"entry": "超跌企稳", "exit": "反弹无力即走"},
    "unknown":       {"entry": "标准信号", "exit": "标准止损止盈"},
}


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_phase():
    try:
        with open(PHASE_FILE) as f:
            data = json.load(f)
        ph = data.get("phase", {})
        return {
            "stage": ph.get("market_phase", "unknown"),
            "strength": float(ph.get("strength", 0) or 0),
            "leader": ph.get("leader", ""),
            "sector": ph.get("sector", ""),
            "diversion": ph.get("diversion", False),
            "loose_money": ph.get("loose_money", False),
            "detail": ph.get("detail", {}),
        }
    except Exception:
        return {"stage": "unknown", "strength": 0, "leader": "", "sector": "", "detail": {}}


def load_sentiment():
    try:
        from market_sentiment import get_market_sentiment
        s = get_market_sentiment()
        return {
            "action": s.get("action", "safe"),
            "phase": s.get("phase", "unknown"),
            "strength": s.get("strength", 0.5),
            "volume_signal": s.get("volume_signal", "decreasing"),
        }
    except Exception:
        return {"action": "safe", "phase": "unknown", "strength": 0.5, "volume_signal": "unknown"}


def load_capital_flow():
    """资金炒作节奏: 主流资金流 + 板块热度 + 外部资金 + 链上"""
    result = {"flow": "neutral", "confidence": 0.5, "sector_heat": {}, "onchain": {}}
    try:
        from strategies.phase_scoring import calc_mainstream_health
        mh = calc_mainstream_health({}) if hasattr(calc_mainstream_health, "__call__") else None
        # calc_mainstream_health 需要 market_cache, 传空可能失败 — 容错
        if mh:
            result["flow"] = mh.get("flow", "neutral")
            result["confidence"] = float(mh.get("confidence", 0.5) or 0.5)
    except Exception:
        pass
    # 板块热度 + 资金分散 (来自 phase_current detail)
    phase = load_phase()
    detail = phase.get("detail", {})
    sh = detail.get("sector_heat", {})
    if sh:
        result["sector_heat"] = dict(sorted(sh.items(), key=lambda x: -x[1])[:3])
    result["diversion"] = phase.get("diversion", False)
    result["loose_money"] = phase.get("loose_money", False)
    result["leader"] = phase.get("leader", "")
    result["sector"] = phase.get("sector", "")
    # 链上信号: 主流币买卖比均值
    try:
        import sqlite3
        conn = sqlite3.connect(ONCHAIN_DB)
        cur = conn.cursor()
        cur.execute("SELECT AVG(buy_sell_ratio), AVG(factor) FROM cronlog_onchain WHERE status='success' AND rowid > (SELECT MAX(rowid)-40 FROM cronlog_onchain)")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            result["onchain"] = {"avg_buy_sell": round(float(row[0]), 2),
                                 "avg_factor": round(float(row[1] or 0), 2)}
    except Exception:
        pass
    return result


def compute_system_directive(system, phase, capital, sentiment):
    """为单个系统生成指导。返回 dict。
    [FIX 2026-08-03] 加入自适应调优: 基于 decision_reflector 各系统近期胜率微调系数
    """
    stage = phase["stage"]
    strength = phase["strength"]
    sent_action = sentiment["action"]
    capital_flow = capital["flow"]

    # ── 主攻方向 ──
    if stage in STRONG_PHASES:
        direction = "long_priority"
    elif stage in WEAK_PHASES:
        direction = "short_priority" if strength > 0.55 else "both_cautious"
    else:
        direction = "both"

    # ── 仓位系数 (阶段 + 情绪 + 资金三重叠加) ──
    pos_scale = 1.0
    # 阶段基础
    if stage == "top_building":
        pos_scale *= 0.7
    elif stage == "retreat":
        pos_scale *= 0.5
    elif stage == "launch":
        pos_scale *= 1.1
    # 情绪 (4档: safe/cautious_safe/caution/danger)
    if sent_action == "danger":
        pos_scale *= 0.6
    elif sent_action == "caution":
        pos_scale *= 0.8
    elif sent_action == "cautious_safe":
        pos_scale *= 0.95
    # 资金流出
    if capital_flow == "outflow":
        pos_scale *= 0.7
    elif capital_flow == "inflow" and stage in STRONG_PHASES:
        pos_scale *= 1.1

    # [FIX 2026-08-03] 自适应调优: 系统近期胜率影响系数
    # 胜率<40% → ×0.85 (表现差收紧); 胜率≥70%且样本≥5 → ×1.1 (表现好放宽)
    try:
        from decision_reflector import _read_memory
        recs = [r for r in _read_memory(limit=50) if r.get("system") == system]
        if len(recs) >= 5:
            wins = sum(1 for r in recs if r.get("pnl_pct", 0) >= 0)
            wr = wins / len(recs)
            if wr < 0.4:
                pos_scale *= 0.85
            elif wr >= 0.7:
                pos_scale *= 1.1
    except Exception:
        pass

    pos_scale = round(max(0.3, min(pos_scale, 1.3)), 2)

    # ── 频率系数 ──
    freq_scale = 1.0
    if stage == "retreat":
        freq_scale = 0.4
    elif stage == "top_building":
        freq_scale = 0.6
    elif sent_action == "danger":
        freq_scale = 0.5
    elif sent_action == "caution":
        freq_scale = 0.7

    # [FIX 2026-08-03] 自适应调优: 连亏≥3笔 → 频率再收紧
    try:
        from decision_reflector import _read_memory
        recs = [r for r in _read_memory(limit=10) if r.get("system") == system]
        consec = 0
        for r in recs:
            if r.get("pnl_pct", 0) < 0:
                consec += 1
            else:
                break
        if consec >= 3:
            freq_scale *= 0.7
    except Exception:
        pass
    freq_scale = round(max(0.2, min(freq_scale, 1.2)), 2)

    # ── 系统特化 ──
    note = ""
    if system == "K1":
        if stage in ("launch", "main_up"):
            note = "15m 快节奏顺势, 专注热点龙头"
        elif stage == "bottom_building":
            note = "等待 EMA 确认后入场, 不追涨"
        elif stage == "top_building":
            note = "冲顶做空优先, 多单轻仓"
        else:
            note = "退潮期降频, 只做超跌反弹"
    elif system == "S1":
        if stage == "main_up":
            note = "5m 高频捕捉, 追涨+承接双模式"
        elif stage == "bottom_building":
            note = "承接模式为主, 等待放量突破确认"
        elif stage == "top_building":
            note = "冲顶做空活跃, 做多降频"
        else:
            note = "做空为主, 做多仅超跌"
    elif system == "T1":
        if stage in ("main_up", "launch"):
            note = "TradFi 顺势跟随, 可加仓"
        elif stage == "retreat":
            note = "TradFi 避险, 观望为主"
        else:
            note = "标准跟随"

    tf = PHASE_TIMEFRAME.get(stage, PHASE_TIMEFRAME["unknown"])
    sp = PHASE_SPACE.get(stage, PHASE_SPACE["unknown"])

    return {
        "direction": direction,
        "pos_scale": pos_scale,
        "freq_scale": freq_scale,
        "timeframe": tf,
        "space": sp,
        "note": note,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    phase = load_phase()
    sentiment = load_sentiment()
    capital = load_capital_flow()

    stage = phase["stage"]
    directive = {
        "time": now_str(),
        "dimensions": {
            "phase": {
                "stage": stage,
                "stage_cn": PHASE_CN.get(stage, stage),
                "strength": phase["strength"],
                "leader": phase["leader"],
                "sector": phase["sector"],
            },
            "capital": {
                "flow": capital["flow"],
                "confidence": capital["confidence"],
                "hot_sectors": list(capital.get("sector_heat", {}).keys()),
                "diversion": capital.get("diversion", False),
                "loose_money": capital.get("loose_money", False),
                "onchain": capital.get("onchain", {}),
            },
            "sentiment": {
                "action": sentiment["action"],
                "volume_signal": sentiment["volume_signal"],
            },
        },
        "systems": {
            "K1": compute_system_directive("K1", phase, capital, sentiment),
            "S1": compute_system_directive("S1", phase, capital, sentiment),
            "T1": compute_system_directive("T1", phase, capital, sentiment),
        },
    }

    with open(OUT_FILE, "w") as f:
        json.dump(directive, f, ensure_ascii=False, indent=2)

    print(f"✅ supervisor 更新: {now_str()}")
    print(f"   阶段: {directive['dimensions']['phase']['stage_cn']} "
          f"(强度{directive['dimensions']['phase']['strength']:.2f}) | "
          f"资金: {directive['dimensions']['capital']['flow']} | "
          f"情绪: {directive['dimensions']['sentiment']['action']}")
    for sys_name, d in directive["systems"].items():
        print(f"   {sys_name}: 方向={d['direction']} 仓位×{d['pos_scale']} "
              f"频率×{d['freq_scale']} | {d['note']}")


if __name__ == "__main__":
    main()
