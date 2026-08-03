#!/usr/bin/env python3
"""
market_4h.py — 加密货币 4h 周期市场数据模块
=============================================
[FIX 2026-08-03] 加密货币市场特性: 节奏快+无涨跌幅限制+24h不停市
→ 日周期太慢, 4小时(4h)是最小合理复盘周期。

提供:
  fetch_4h_gainers(top, min_vol_usd, lookback_bars)
      → 4h周期涨幅榜 (从4h K线计算, 对比lookback_bars前的收盘价)
  get_4h_change(symbol)
      → 单标的 4h 涨幅 (最新收盘 vs N根前收盘)
  fetch_4h_all_changes()
      → 全市场 4h 涨幅 map {symbol: pct}

用法:
  from market_4h import fetch_4h_gainers
  gainers = fetch_4h_gainers(top=15, min_vol_usd=3_000_000)
"""
import os, json, time
import urllib.request

PROXY = "https://BINANCE_PROXY.example.com"
UA = "HermesRadar/2.0"
CACHE_FILE = "/tmp/market_4h_cache.json"
CACHE_TTL = 180  # 3分钟缓存

# 4h涨幅计算: 对比 N 根 4h K线前的收盘 (N=6 → 24h跨度但用4h粒度; 默认6根=1天)
DEFAULT_LOOKBACK = 6


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _fetch_4h_klines(symbol, limit=10):
    """拉取单标的4h K线"""
    url = f"{PROXY}/fapi/v1/klines?symbol={symbol}USDT&interval=4h&limit={limit}"
    data = _http_get(url)
    return [{"close": float(k[4]), "volume": float(k[5])} for k in data]


def get_4h_change(symbol, lookback=DEFAULT_LOOKBACK):
    """单标的 4h 涨幅: (最新收盘 - lookback根前收盘) / lookback根前收盘 * 100"""
    try:
        bars = _fetch_4h_klines(symbol, limit=lookback + 1)
        if len(bars) < 2:
            return None
        old = bars[0]["close"]
        new = bars[-1]["close"]
        if old <= 0:
            return None
        return (new - old) / old * 100
    except Exception:
        return None


def fetch_4h_all_changes(lookback=DEFAULT_LOOKBACK, min_vol_usd=0):
    """全市场 4h 涨幅 (批量: 先用24hr ticker拿全量列表+成交量, 再按需算4h涨幅)"""
    # 第一步: 拉全量合约列表 (拿 symbol + 成交量)
    tickers = _http_get(f"{PROXY}/fapi/v1/ticker/24hr")
    result = {}
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        vol = float(t.get("quoteVolume", 0) or 0)
        if vol < min_vol_usd:
            continue
        result[sym] = {"vol_24h": vol, "price": float(t.get("lastPrice", 0) or 0)}
    return result


def fetch_4h_gainers(top=20, min_vol_usd=3_000_000, lookback=DEFAULT_LOOKBACK, max_symbols=60):
    """4h周期涨幅榜。
    流程: 1) 24hr ticker 拿全量列表+成交量过滤  2) 对候选逐个算4h涨幅  3) 按4h涨幅排序
    候选集: 取24h成交量前 max_symbols 个 (高流动性优先), 再算4h涨幅避免拉全市场K线过慢
    """
    try:
        all_syms = fetch_4h_all_changes(min_vol_usd=min_vol_usd)
        if not all_syms:
            return [], "无法拉取合约列表"
        # 按24h成交量排序取前N个作为候选
        candidates = sorted(all_syms.items(), key=lambda x: -x[1]["vol_24h"])[:max_symbols]
        gainers = []
        for sym, info in candidates:
            chg = get_4h_change(sym[:-4], lookback=lookback)
            if chg is None or chg < 3:  # 4h涨幅<3%不算赚钱效应
                continue
            gainers.append({
                "symbol": sym[:-4],
                "change_4h": round(chg, 2),
                "change_24h": None,  # 如需24h可另算
                "vol_24h": round(info["vol_24h"] / 1e6, 1),
                "last_price": info["price"],
            })
        gainers.sort(key=lambda x: -x["change_4h"])
        return gainers[:top], None
    except Exception as e:
        return [], f"4h涨幅榜拉取失败: {e}"


if __name__ == "__main__":
    import sys
    g, err = fetch_4h_gainers(top=10, min_vol_usd=3_000_000)
    if err:
        print(f"❌ {err}")
    else:
        print(f"=== 4h 涨幅榜 Top{len(g)} ===")
        for i, x in enumerate(g, 1):
            print(f"  {i}. {x['symbol']:12s} +{x['change_4h']}% vol={x['vol_24h']}M ${x['last_price']:.6g}")
