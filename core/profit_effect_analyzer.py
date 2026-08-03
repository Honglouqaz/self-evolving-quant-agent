#!/usr/bin/env python3
"""
profit_effect_analyzer.py — 赚钱效应复盘与归因分析
==================================================
自进化系统的核心复盘能力: 站在市场阶段/资金节奏/情绪周期角度,
回答三个问题:
  1. 最近哪些标的有赚钱效应? (涨幅榜 + 成交量过滤)
  2. 三大系统是否做到了这些标的? (实际成交 vs 涨幅榜)
  3. 做到了是否吃到大部分利润? 没做到该怎么优化?

数据源:
  - Binance 24h 涨幅榜 (实时, binance-proxy)
  - signals 表 (K1/S1 历史信号, quant_data.db)
  - 系统成交日志 (radar_quant.log / k1_executor.log)
  - phase_current.json (市场阶段)

输出: DATA_DIR/quant_supervisor/profit_effect_report.md + JSON
用法: python3 profit_effect_analyzer.py [--days 7] [--top 15]
"""
import os, sys, json, re, subprocess
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "PROJECT_ROOT")

CST = timezone(timedelta(hours=8))
PROXY = "https://BINANCE_PROXY.example.com"
UA = "HermesRadar/2.0"
OUT_DIR = "DATA_DIR/quant_supervisor"
SIGNAL_DB = "DATA_DIR/quant_data.db"
LOG_DIR = "DATA_DIR/cron/output"

# S1 池: 动态山寨池 (24h 成交量 ≥1500万U); K1 池: 19 固定主流; T1 池: TradFi
K1_POOL = {"BTC","ETH","SOL","ADA","XRP","DOGE","AVAX","BNB","LINK","DOT",
           "SUI","PEPE","HYPE","ZEC","BCH","DASH","TRX","UNI","LTC"}

def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M")


def fetch_top_gainers(days=1, top=20, min_vol_usd=3_000_000):
    """拉取 4h 周期涨幅榜 (加密市场特性: 日周期太慢, 4h是最小合理复盘周期)
    [FIX 2026-08-03] 从24h涨幅改为4h涨幅 (market_4h.py)
    """
    try:
        from market_4h import fetch_4h_gainers
        return fetch_4h_gainers(top=top, min_vol_usd=min_vol_usd)
    except Exception as e:
        return [], f"4h涨幅榜拉取失败: {e}"


def load_signals(symbol=None, days=7):
    """从 signals 表加载近期信号 (K1/S1 覆盖度检查)."""
    import sqlite3
    try:
        conn = sqlite3.connect(SIGNAL_DB)
        cur = conn.cursor()
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
        if symbol:
            cur.execute("SELECT symbol, COUNT(*), MAX(timestamp) FROM signals "
                        "WHERE symbol=? AND timestamp>? GROUP BY symbol", (symbol, since))
        else:
            cur.execute("SELECT symbol, COUNT(*), MAX(timestamp) FROM signals "
                        "WHERE timestamp>? GROUP BY symbol", (since,))
        rows = cur.fetchall()
        conn.close()
        return {r[0]: {"count": r[1], "last": r[2]} for r in rows}
    except Exception:
        return {}


def load_trade_logs(system, days=7):
    """从系统日志提取实际成交记录。
    [FIX 2026-08-03] 过滤价格=0的异常记录（S1 save_state bug导致的空记录）
    [FIX 2026-08-03] 过滤HTTP 400失败记录
    """
    trades = []
    if system == "S1":
        path = f"{LOG_DIR}/radar_quant.log"
        if os.path.exists(path):
            with open(path, errors="ignore") as f:
                lines = f.readlines()
            # S1 日志格式: 上一行是 "✅ SYM: 三步确认通过..." 下一行 "✅ 开多 x @ $y ($z)"
            for i, line in enumerate(lines):
                m = re.search(r"✅ 开多 ([\d.]+) @ \$([\d.]+) \(\$([\d.]+)\)", line)
                if not m:
                    m = re.search(r"✅ 开空 ([\d.]+) @ \$([\d.]+) \(\$([\d.]+)\)", line)
                if m:
                    price = float(m.group(2))
                    # 过滤价格=0的异常记录 (S1 的 cumQuote=0 bug)
                    if price <= 0:
                        continue
                    # 回溯找最近的 "✅ SYM:" 行
                    sym = None
                    for j in range(i - 1, max(-1, i - 6), -1):
                        ms = re.search(r"([A-Z0-9]+):\s*(做空确认通过|三步确认通过|开多|开空)", lines[j])
                        if ms:
                            sym = ms.group(1)
                            break
                    if sym is None:
                        for j in range(max(0, i - 6), i):
                            ms = re.search(r"✅ ([A-Z0-9]+):", lines[j])
                            if ms:
                                sym = ms.group(1)
                                break
                    trades.append({"sym": sym or "UNKNOWN",
                                   "dir": "LONG" if "开多" in line else "SHORT",
                                   "qty": float(m.group(1)), "price": price, "usdt": float(m.group(3))})
    elif system == "K1":
        path = f"{LOG_DIR}/k1_executor.log"
        if os.path.exists(path):
            with open(path, errors="ignore") as f:
                lines = f.readlines()
            # 跟踪最近的时间戳行: "🤖 **K1 实盘合约 | 2026-07-15 13:32 CST**"
            cur_ts = ""
            for line in lines:
                m_ts = re.search(r"\|\s*(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}) CST", line)
                if m_ts:
                    cur_ts = m_ts.group(1)
                m = re.search(r"✅ ([A-Z0-9]+): 开多 ([\d.]+) @ \$([\d.]+)", line)
                if m:
                    trades.append({"sym": m.group(1), "dir": "LONG", "ts": cur_ts,
                                   "qty": float(m.group(2)), "price": float(m.group(3))})
                m = re.search(r"✅ ([A-Z0-9]+): 开空 ([\d.]+) @ \$([\d.]+)", line)
                if m:
                    trades.append({"sym": m.group(1), "dir": "SHORT", "ts": cur_ts,
                                   "qty": float(m.group(2)), "price": float(m.group(3))})
    elif system == "T1":
        path = "DATA_DIR/t1_data/trade_log.jsonl"
        if os.path.exists(path):
            with open(path, errors="ignore") as f:
                for line in f:
                    try:
                        t = json.loads(line)
                        if t.get("action") == "open":
                            trades.append({"sym": t.get("symbol", ""),
                                           "dir": "LONG" if t.get("direction") == "long" else "SHORT",
                                           "qty": t.get("qty", 0), "price": t.get("entry_price", 0)})
                    except Exception:
                        continue
    return trades


def load_phase():
    try:
        with open("DATA_DIR/phase_current.json") as f:
            ph = json.load(f).get("phase", {})
        return {"stage": ph.get("market_phase", "unknown"),
                "strength": float(ph.get("strength", 0) or 0),
                "leader": ph.get("leader", ""), "sector": ph.get("sector", "")}
    except Exception:
        return {"stage": "unknown", "strength": 0, "leader": "", "sector": ""}


def analyze_system_coverage(system, gainers, phase):
    """分析某系统对赚钱效应标的的覆盖情况."""
    trades = load_trade_logs(system)
    traded_syms = set()
    for t in trades:
        if t.get("sym"):
            traded_syms.add(t["sym"].replace("USDT", ""))

    # 系统池定义
    pool = set()
    if system == "K1":
        pool = K1_POOL
    elif system == "T1":
        # T1 是 TradFi 池, 不在加密涨幅榜 → 覆盖度天然低, 特殊说明
        pass

    result = {
        "system": system,
        "trades_total": len(trades),
        "traded_syms": sorted(traded_syms),
        "hit": [],      # 抓到且吃到的
        "missed": [],   # 涨幅榜但系统没做
        "partial": [],  # 做了但涨幅榜里
    }
    for g in gainers:
        sym = g["symbol"]
        if sym in traded_syms:
            result["hit"].append({**g, "status": "已交易"})
        else:
            reason = "不在K1固定池" if system == "K1" and sym not in pool else \
                     "T1为TradFi池,不覆盖加密" if system == "T1" else \
                     "信号未触发/未选中"
            result["missed"].append({**g, "reason": reason})

    # 命中率
    result["hit_count"] = len(result["hit"])
    result["missed_count"] = len(result["missed"])
    result["coverage_rate"] = round(result["hit_count"] / len(gainers) * 100, 1) if gainers else 0
    return result


def analyze_profit_capture(system, gainers):
    """分析利润捕获: 系统做过的标的, 对比当前涨幅 vs 实际入场
    [FIX 2026-08-03] 只统计近24h内的开仓, 避免历史持仓(如7月开的ZEC)与今日涨幅
    时间错配产生虚假亏损报告。
    """
    import time as _time
    from datetime import datetime
    trades = load_trade_logs(system)
    sym_map = {g["symbol"]: g for g in gainers}
    now = datetime.now(CST)
    capture = []
    for t in trades:
        sym = t.get("sym")
        if not sym or sym not in sym_map:
            continue
        g = sym_map[sym]
        entry = t.get("price", 0)
        cur = g["last_price"]
        # 时间过滤: K1 日志带 ts, 超过24h的跳过 (S1/T1 无 ts 时放行)
        ts = t.get("ts", "")
        if ts:
            try:
                t_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
                if (now - t_dt).total_seconds() > 24 * 3600:
                    continue
            except Exception:
                pass
        # 做多捕获率 = (现价-入场)/(入场) 对比 24h涨幅
        if t.get("dir") == "LONG" and entry > 0:
            got = (cur - entry) / entry * 100
            ratio = got / g.get("change_4h", g.get("change_24h", 0)) * 100 if g.get("change_4h", g.get("change_24h", 0)) > 0 else 0
            capture.append({"symbol": sym, "system": system, "entry": entry, "current": cur,
                            "got_pct": round(got, 2), "mkt_pct": g.get("change_4h", g.get("change_24h", 0)),
                            "capture_ratio": round(ratio, 0)})
    return capture


def main():
    days = 7
    top = 15
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
        if a == "--top" and i + 1 < len(args):
            top = int(args[i + 1])

    os.makedirs(OUT_DIR, exist_ok=True)
    phase = load_phase()
    gainers, err = fetch_top_gainers(days=1, top=top)

    lines = []
    lines.append(f"# 📈 赚钱效应复盘 — {now_str()}\n")
    lines.append(f"> 市场阶段: **{phase.get('stage')}** (强度{phase.get('strength', 0):.2f}) "
                 f"| 龙头: {phase.get('leader') or '无'} | 板块: {phase.get('sector') or '无'}\n")

    if err or not gainers:
        lines.append(f"⚠️ 无法拉取涨幅榜: {err}")
        report = "\n".join(lines)
        with open(f"{OUT_DIR}/profit_effect_report.md", "w") as f:
            f.write(report)
        print(report)
        return

    lines.append(f"## 一、赚钱效应榜 (24h涨幅≥5%, 成交量≥300万U, Top{top})\n")
    lines.append("| 排名 | 标的 | 24h涨幅 | 成交量(M) | 现价 |")
    lines.append("|:----|:----|:-------|:---------|:----|")
    for i, g in enumerate(gainers, 1):
        lines.append(f"| {i} | **{g['symbol']}** | +{g.get('change_4h', g.get('change_24h', 0))}% | {g['vol_24h']} | ${g['last_price']:.6g} |")

    # 三系统覆盖分析
    lines.append(f"\n## 二、三大系统覆盖分析\n")
    for sys_name in ("S1", "K1", "T1"):
        res = analyze_system_coverage(sys_name, gainers, phase)
        lines.append(f"### {sys_name} (共{res['trades_total']}笔交易, 覆盖率{res['coverage_rate']}%)\n")
        if res["hit"]:
            hit_str = ", ".join(h["symbol"] for h in res["hit"])
            lines.append(f"- ✅ **做到**: {hit_str}")
        else:
            lines.append(f"- ❌ **没做到任何赚钱效应标的**")
        if res["missed"]:
            lines.append(f"- ⚠️ **漏掉 {res['missed_count']} 个**:")
            for m in res["missed"][:8]:
                lines.append(f"  - {m['symbol']} (+{m.get('change_4h', m.get('change_24h', 0))}%) — {m['reason']}")
            if res["missed_count"] > 8:
                lines.append(f"  - ...等 {res['missed_count']} 个")

    # 利润捕获分析
    lines.append(f"\n## 三、利润捕获分析 (做多持仓 vs 市场涨幅)\n")
    capture = analyze_profit_capture("S1", gainers) + analyze_profit_capture("K1", gainers)
    if capture:
        lines.append("| 标的 | 系统 | 入场价 | 现价 | 已获% | 市场% | 捕获率 |")
        lines.append("|:----|:----|:-----|:----|:----|:----|:------|")
        for c in capture[:12]:
            lines.append(f"| {c['symbol']} | {c['system']} | ${c['entry']:.4g} | ${c['current']:.4g} "
                         f"| {c['got_pct']:+.1f}% | +{c['mkt_pct']}% | {c['capture_ratio']:.0f}% |")
    else:
        lines.append("当前无做多持仓覆盖在涨幅榜内 (或均为做空/已平仓)")

    # 优化建议 (规则驱动)
    lines.append(f"\n## 四、归因与优化建议\n")
    for sys_name in ("S1", "K1"):
        res = analyze_system_coverage(sys_name, gainers, phase)
        if res["coverage_rate"] < 30:
            if sys_name == "K1":
                lines.append(f"- **{sys_name}**: 覆盖率仅{res['coverage_rate']}% — 涨幅榜多为山寨新币, K1固定19主流池天然受限。建议: 评估是否扩大K1池或与S1动态池联动")
            else:
                lines.append(f"- **{sys_name}**: 覆盖率仅{res['coverage_rate']}% — 需检查信号触发条件(评分门槛/成交量过滤)是否过严, 或标的池未覆盖当日龙头")
        elif res["coverage_rate"] >= 30 and res["coverage_rate"] < 70:
            lines.append(f"- **{sys_name}**: 覆盖率{res['coverage_rate']}% 中等 — 已捕捉部分龙头, 可检查漏掉的是否集中在特定板块(补板块轮动识别)")
        else:
            lines.append(f"- **{sys_name}**: 覆盖率{res['coverage_rate']}% 良好 — 维持现状, 关注利润捕获率而非仅覆盖")

    report = "\n".join(lines)
    with open(f"{OUT_DIR}/profit_effect_report.md", "w") as f:
        f.write(report)
    with open(f"{OUT_DIR}/profit_effect_data.json", "w") as f:
        json.dump({"time": now_str(), "phase": phase, "gainers": gainers,
                   "coverage": {s: analyze_system_coverage(s, gainers, phase)
                                for s in ("S1", "K1", "T1")}},
                  f, ensure_ascii=False, indent=2)
    print(report)


if __name__ == "__main__":
    main()
