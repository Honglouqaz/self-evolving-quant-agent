#!/usr/bin/env python3
"""
supervisor_client.py — 三系统接入总监督层的轻量客户端
======================================================
读取 market_supervisor 生成的 directive.json, 为 K1/S1/T1 提供:
  get_directive(system) -> dict  (方向/仓位系数/频率系数/时间维度/空间维度)
  get_pos_scale(system) -> float  (仓位系数, 供仓位计算乘入)
  get_freq_scale(system) -> float (频率系数, 供开仓数量调整)
  should_trade(system, direction) -> (bool, reason)  (方向一致性检查)

设计原则:
  - 读取失败/文件缺失 → 全部返回 1.0/True (降级为无监督, 不影响实盘)
  - 只读, 无副作用
"""
import os, json

DIRECTIVE_FILE = "DATA_DIR/quant_supervisor/directive.json"

_cache = {"ts": 0, "data": None}


def _load():
    """带缓存读取 directive (缓存30秒, 避免高频调用反复读盘)"""
    import time
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < 30:
        return _cache["data"]
    try:
        with open(DIRECTIVE_FILE) as f:
            data = json.load(f)
        _cache["data"] = data
        _cache["ts"] = now
        return data
    except Exception:
        return None


def get_directive(system):
    """返回指定系统的完整指导 dict, 失败返回 None"""
    data = _load()
    if not data:
        return None
    return data.get("systems", {}).get(system)


def get_pos_scale(system):
    """仓位系数 (0.3~1.3), 失败默认 1.0"""
    d = get_directive(system)
    if not d:
        return 1.0
    return float(d.get("pos_scale", 1.0) or 1.0)


def get_freq_scale(system):
    """频率系数 (0.2~1.2), 失败默认 1.0"""
    d = get_directive(system)
    if not d:
        return 1.0
    return float(d.get("freq_scale", 1.0) or 1.0)


def get_direction(system):
    """主攻方向: long_priority/short_priority/both/both_cautious"""
    d = get_directive(system)
    if not d:
        return "both"
    return d.get("direction", "both")


def should_trade(system, direction):
    """方向一致性检查: 返回 (allowed, reason)
    direction: 'LONG'/'SHORT' (或 'long'/'short')
    """
    d = get_directive(system)
    if not d:
        return True, "[监督] 无指令, 放行"
    direction = direction.upper()
    main_dir = d.get("direction", "both")

    if main_dir == "long_priority":
        if direction == "LONG":
            return True, f"[监督] {main_dir} 做多优先 ✓"
        return True, f"[监督] {main_dir} 做空允许但非主攻(仓位×{d.get('pos_scale')})"
    if main_dir == "short_priority":
        if direction == "SHORT":
            return True, f"[监督] {main_dir} 做空优先 ✓"
        return True, f"[监督] {main_dir} 做多允许但逆势(仓位×{d.get('pos_scale')})"
    if main_dir == "both_cautious":
        return True, f"[监督] 双方向谨慎(仓位×{d.get('pos_scale')})"
    return True, "[监督] 双方向正常"


def get_timeframe_note(system):
    """时间维度指导文本"""
    d = get_directive(system)
    if not d:
        return ""
    tf = d.get("timeframe", {})
    return f"{tf.get('cycle','')}·{tf.get('hold','')}·{tf.get('entry_rhythm','')}"


def get_space_note(system):
    """空间维度指导文本"""
    d = get_directive(system)
    if not d:
        return ""
    sp = d.get("space", {})
    return f"入场:{sp.get('entry','')} | 离场:{sp.get('exit','')}"


def summary():
    """三系统指导摘要 (日志用)"""
    data = _load()
    if not data:
        return "监督: 无指令文件"
    parts = [f"阶段:{data.get('dimensions',{}).get('phase',{}).get('stage_cn','?')}"]
    for sys_name in ("K1", "S1", "T1"):
        d = data.get("systems", {}).get(sys_name, {})
        parts.append(f"{sys_name}({d.get('direction','?')}×{d.get('pos_scale',1)})")
    return "监督: " + " ".join(parts)


if __name__ == "__main__":
    print(summary())
    for s in ("K1", "S1", "T1"):
        print(f"\n=== {s} ===")
        print(f"  方向: {get_direction(s)} | 仓位×{get_pos_scale(s)} | 频率×{get_freq_scale(s)}")
        print(f"  时间: {get_timeframe_note(s)}")
        print(f"  空间: {get_space_note(s)}")
        ok, reason = should_trade(s, "LONG")
        print(f"  做多检查: {'✅' if ok else '❌'} {reason}")
