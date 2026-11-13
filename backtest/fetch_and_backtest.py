#!/usr/bin/env python3
"""
fetch_and_backtest.py — 自包含回测：从 Binance 拉数据 → 跑策略 → 出统计

设计要点（为什么这样写）：
  1. **零外部依赖**：只用 requests/pandas/numpy，不 import 本机任何模块。
     Actions runner 上不会有 /opt/data/scripts，所以不能复用本地代码。
  2. **runner 在境外 ⇒ 直连 fapi.binance.com，无需代理**。这是 Actions 最大的价值。
  3. 分页拉取：Binance 单次最多 1500 根，按 startTime 循环。
  4. 输出 result.json（统计）+ data/*.csv（原始数据），便于 artifact 下载对照。
  5. 成本假设写明：手续费 0.04%/边（taker），滑点 0.02%，用于让收益数字不虚高。

用法：
    python3 fetch_and_backtest.py --symbols BTCUSDT,ETHUSDT --interval 5m --days 30 --strategy ma_cross
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

FAPI = "https://fapi.binance.com"
INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
FEE_PER_SIDE = 0.0004   # taker 0.04%
SLIP_PER_SIDE = 0.0002  # 0.02%


def fetch_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """分页拉取 K 线。返回 DataFrame[ts, open, high, low, close, volume]。"""
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    rows = []
    cur = start
    while cur < now:
        params = {
            "symbol": symbol, "interval": interval,
            "startTime": cur, "limit": 1500,
        }
        for attempt in range(4):
            try:
                r = requests.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=20)
                if r.status_code == 200:
                    break
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= cur:
            break
        cur = last_ts + step
        if len(batch) < 1500:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "ts","open","high","low","close","volume",
        "close_time","qav","trades","tbb","tbq","ignore"])
    for c in ("open","high","low","close","volume"):
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df[["ts","open","high","low","close","volume"]].drop_duplicates("ts")
    return df.sort_values("ts").reset_index(drop=True)


# ══════════════ 策略：每个返回 +1(多) / -1(空) / 0(空仓) 信号序列 ══════════════

def sig_ma_cross(df: pd.DataFrame, fast: int = 12, slow: int = 48) -> pd.Series:
    f = df["close"].rolling(fast).mean()
    s = df["close"].rolling(slow).mean()
    sig = pd.Series(0, index=df.index)
    sig[f > s] = 1
    sig[f < s] = -1
    return sig

def sig_rsi_revert(df: pd.DataFrame, period: int = 14, lo: int = 30, hi: int = 70) -> pd.Series:
    delta = df["close"].diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    sig = pd.Series(0, index=df.index)
    sig[rsi < lo] = 1
    sig[rsi > hi] = -1
    return sig

def sig_breakout(df: pd.DataFrame, n: int = 20) -> pd.Series:
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    sig = pd.Series(0, index=df.index)
    sig[df["close"] > hi] = 1
    sig[df["close"] < lo] = -1
    return sig

def sig_donchian(df: pd.DataFrame, n: int = 55, exit_n: int = 20) -> pd.Series:
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    exit_hi = df["high"].rolling(exit_n).max().shift(1)
    exit_lo = df["low"].rolling(exit_n).min().shift(1)
    sig = pd.Series(0, index=df.index)
    pos = 0
    for i in range(len(df)):
        c = df["close"].iloc[i]
        if pos == 0:
            if pd.notna(hi.iloc[i]) and c > hi.iloc[i]:
                pos = 1
            elif pd.notna(lo.iloc[i]) and c < lo.iloc[i]:
                pos = -1
        elif pos == 1:
            if pd.notna(exit_lo.iloc[i]) and c < exit_lo.iloc[i]:
                pos = 0
        elif pos == -1:
            if pd.notna(exit_hi.iloc[i]) and c > exit_hi.iloc[i]:
                pos = 0
        sig.iloc[i] = pos
    return sig

STRATEGIES = {
    "ma_cross": sig_ma_cross,
    "rsi_revert": sig_rsi_revert,
    "breakout": sig_breakout,
    "donchian": sig_donchian,
}


def simulate(df: pd.DataFrame, sig: pd.Series) -> dict:
    """按信号逐根模拟，并按「持仓段」结算每笔交易。

    ★[2026-11-13 修两个 bug]
      bug1 成本多扣一倍：原 `turn * (FEE+SLIP) * 2`。turn=|pos.diff()| 已经是
           「手数变化」：0→1 是 1（一次进场费），1→-1 是 2（平多+开空各一次），
           再乘 2 等于把成本翻倍，导致胜率被压到 0~6%（实测 BTC -13.7%/56 笔）。
      bug2 胜率口径错：原代码拿「换仓当根的 net」判输赢，而换仓当根刚扣完成本，
           几乎必然亏 ⇒ 胜率恒为 0。正确口径是「一笔完整持仓段的累计收益」。

    成交假设：用上一根信号在本根持仓，等价于「信号在下一根开盘成交」，
    避免使用当根收盘价的未来函数。
    """
    ret = df["close"].pct_change().fillna(0).to_numpy()
    pos = sig.shift(1).fillna(0).to_numpy()          # 本根持仓
    n = len(ret)
    if n == 0:
        return {"bars": 0, "trades": 0, "win_rate": None, "total_return": 0.0,
                "max_drawdown": 0.0, "sharpe_like": 0.0, "avg_bar_return": 0.0,
                "profit_factor": None, "avg_win": None, "avg_loss": None}
    # 换仓成本：|持仓变化| × 单边成本（不再 ×2）
    turn = np.abs(np.diff(np.concatenate([[0.0], pos])))
    cost = turn * (FEE_PER_SIDE + SLIP_PER_SIDE)
    gross = pos * ret
    net = gross - cost

    eq = np.cumprod(1 + net)
    peak = np.maximum.accumulate(eq)
    dd = float((eq / peak - 1).min()) if n else 0.0

    # ── 按持仓段结算每笔交易 ──
    seg_rets = []            # 每笔交易的净收益
    seg_dir = []             # 方向
    cur = 0.0
    cur_dir = 0.0
    for i in range(n):
        d = pos[i]
        if cur_dir != 0 and d != cur_dir:
            seg_rets.append(cur); seg_dir.append(cur_dir)
            cur = 0.0
        if d != 0:
            cur = (1 + cur) * (1 + net[i]) - 1
        cur_dir = d
    if cur_dir != 0:
        seg_rets.append(cur); seg_dir.append(cur_dir)

    trades = len(seg_rets)
    wins = [r for r in seg_rets if r > 0]
    losses = [r for r in seg_rets if r <= 0]
    gp = sum(wins); gl = -sum(losses)
    return {
        "bars": n,
        "trades": trades,
        "win_rate": round(len(wins) / trades, 4) if trades else None,
        "total_return": round(float(eq[-1] - 1), 6),
        "max_drawdown": round(dd, 6),
        "sharpe_like": round(float(net.mean() / net.std() * np.sqrt(n)) if net.std() > 0 else 0.0, 4),
        "avg_bar_return": round(float(net.mean()), 8),
        "profit_factor": round(gp / gl, 4) if gl > 0 else None,
        "avg_win": round(float(np.mean(wins)), 6) if wins else None,
        "avg_loss": round(float(np.mean(losses)), 6) if losses else None,
        "long_trades": int(sum(1 for d in seg_dir if d > 0)),
        "short_trades": int(sum(1 for d in seg_dir if d < 0)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strategy", default="ma_cross")
    ap.add_argument("--out", default="result.json")
    a = ap.parse_args()

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    fn = STRATEGIES.get(a.strategy)
    if fn is None:
        print(f"未知策略 {a.strategy}，可选: {list(STRATEGIES)}", file=sys.stderr)
        return 2

    os.makedirs("data", exist_ok=True)
    out = {
        "meta": {
            "interval": a.interval, "days": a.days, "strategy": a.strategy,
            "fee_per_side": FEE_PER_SIDE, "slip_per_side": SLIP_PER_SIDE,
            "runner": "github-actions", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "symbols": {},
    }

    for sym in syms:
        try:
            df = fetch_klines(sym, a.interval, a.days)
            if df.empty:
                out["symbols"][sym] = {"error": "no_data"}
                print(f"[{sym}] 无数据")
                continue
            df.to_csv(f"data/{sym}_{a.interval}.csv", index=False)
            sig = fn(df)
            st = simulate(df, sig)
            st["first_bar"] = str(df["ts"].iloc[0])
            st["last_bar"] = str(df["ts"].iloc[-1])
            out["symbols"][sym] = st
            print(f"[{sym}] {st['bars']} 根  交易 {st['trades']} 次  "
                  f"收益 {st['total_return']*100:.2f}%  回撤 {st['max_drawdown']*100:.2f}%  "
                  f"胜率 {st['win_rate']}")
        except Exception as e:
            out["symbols"][sym] = {"error": f"{type(e).__name__}: {e}"[:200]}
            print(f"[{sym}] 失败: {e}", file=sys.stderr)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
