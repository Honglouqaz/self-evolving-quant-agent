#!/usr/bin/env python3
"""
data_source_health.py — 数据源健康监控与自动切换 [FIX 2026-08-03]
==================================================================
#6 自进化: 监控 Binance 数据源健康度, 异常时自动切换备用源:
  主源: https://BINANCE_PROXY.example.com (CF Worker 代理)
  备用: https://fapi.binance.com (直连, 走 sing-box 代理)
  
监控指标:
  - 延迟 (p95)
  - 失败率 (连续 N 次失败)
  - 健康状态写入 DATA_DIR/quant_supervisor/data_source_status.json
  
用法: python3 data_source_health.py [--check] [--json]
"""
import os, sys, json, time, subprocess
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
MAIN = "https://BINANCE_PROXY.example.com"
FALLBACK = "https://fapi.binance.com"
STATUS_FILE = "DATA_DIR/quant_supervisor/data_source_status.json"
NOTIF = "DATA_DIR/notif_outbox"

CONSEC_FAIL_THRESHOLD = 3   # 连续3次失败 → 切换/告警
LATENCY_THRESHOLD = 5.0     # 延迟>5s → 警告


def _probe(url, path="/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=8):
    """探测数据源, 返回 (ok, latency)"""
    start = time.time()
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{url}{path}", "--max-time", str(timeout)],
            capture_output=True, text=True, timeout=timeout + 3)
        latency = time.time() - start
        return r.stdout.strip() in ("200",), round(latency, 2)
    except Exception:
        return False, None


def _push(title, body):
    os.makedirs(NOTIF, exist_ok=True)
    ts = int(time.time())
    with open(f"{NOTIF}/datasrc_{ts}.md", "w") as f:
        f.write(f"## ⚠️ {title}\n\n{body}\n\n{datetime.now(CST)}")


def main():
    check_only = "--check" in sys.argv
    as_json = "--json" in sys.argv

    # 加载历史状态
    state = {}
    if os.path.exists(STATUS_FILE):
        try:
            state = json.load(open(STATUS_FILE))
        except Exception:
            state = {}

    # 探测主源
    main_ok, main_lat = _probe(MAIN)
    consec_fail = state.get("main_consec_fail", 0)
    if main_ok:
        consec_fail = 0
    else:
        consec_fail += 1
    state["main_consec_fail"] = consec_fail

    # 探测备用源 (仅当主源异常时)
    fb_ok, fb_lat = None, None
    if not main_ok:
        fb_ok, fb_lat = _probe(FALLBACK)

    # 状态决策
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    status = {
        "time": now,
        "main": {"url": MAIN, "ok": main_ok, "latency": main_lat, "consec_fail": consec_fail},
        "fallback": {"url": FALLBACK, "ok": fb_ok, "latency": fb_lat},
        "active_source": "main" if main_ok else ("fallback" if fb_ok else "none"),
        "action": "none",
    }

    # 告警/切换逻辑
    if not main_ok and fb_ok:
        status["action"] = "switch_to_fallback"
        if state.get("last_action") != "switch_to_fallback":
            _push("🔀 数据源切换", f"主源 {MAIN} 不可用 → 已切备用 {FALLBACK}")
    elif not main_ok and not fb_ok:
        status["action"] = "both_down"
        if consec_fail >= CONSEC_FAIL_THRESHOLD and state.get("last_action") != "both_down":
            _push("🔴 数据源全挂", f"主备均不可用, 连续{consec_fail}次失败, 需人工介入")
    elif main_ok and main_lat and main_lat > LATENCY_THRESHOLD:
        status["action"] = "slow"
        if state.get("last_action") != "slow":
            _push("🐢 数据源延迟", f"主源延迟 {main_lat}s > {LATENCY_THRESHOLD}s")
    elif main_ok and state.get("last_action") in ("switch_to_fallback", "both_down", "slow"):
        status["action"] = "recovered"
        _push("✅ 数据源恢复", f"主源恢复正常 ({main_lat}s)")

    state["last_action"] = status["action"]
    state["main_consec_fail"] = consec_fail
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    if as_json:
        print(json.dumps(status, ensure_ascii=False))
    else:
        print(f"主源: {'✅' if main_ok else '❌'} {main_lat}s (连败{consec_fail}) | "
              f"备用: {'✅' if fb_ok else '❌'} {fb_lat}s | 活跃: {status['active_source']} | 动作: {status['action']}")


if __name__ == "__main__":
    main()
