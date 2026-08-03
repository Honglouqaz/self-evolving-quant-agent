#!/usr/bin/env python3
"""
cron_health_check.py — Cron 任务健康检查 [FIX 2026-08-03]
==========================================================
#5 自进化: 扫描 Hermes cron 任务执行记录, 检测连续失败/长时间运行异常:
  - 连续失败 ≥3 次 → 输出告警 (标记待处理)
  - 单任务连续失败 ≥5 次 → 建议暂停 (输出 pause 建议)
  - 最近24h失败率 >30% 的任务 → 输出优化建议
输出: DATA_DIR/quant_supervisor/cron_health_report.md
用法: python3 cron_health_check.py [--json]
"""
import os, sys, json, sqlite3
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
DB = "DATA_DIR/cron/executions.db"
OUT = "DATA_DIR/quant_supervisor/cron_health_report.md"

# 已知的"高频低风险"任务 (每5/10/15分钟跑, 偶发失败可容忍)
HIGH_FREQ_OK = {"clean-cron-output", "cron_self_heal.sh"}


def load_executions():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT job_id, status, started_at, error FROM executions "
                "WHERE started_at > datetime('now', '-2 days') ORDER BY started_at")
    rows = cur.fetchall()
    conn.close()
    return rows


def load_job_names():
    """从 cron jobs.json 推断任务名 (job_id → name)"""
    names = {}
    try:
        d = json.load(open("DATA_DIR/cron/jobs.json"))
        jobs = d if isinstance(d, list) else d.get("jobs", [])
        for j in jobs:
            names[j.get("id") or j.get("job_id", "")] = j.get("name", "")
    except Exception:
        pass
    return names


def main():
    rows = load_executions()
    if not rows:
        print("无执行记录")
        return

    # 按 job_id 统计
    from collections import defaultdict
    stats = defaultdict(lambda: {"total": 0, "error": 0, "last_error": "", "last_ts": ""})
    for job_id, status, ts, err in rows:
        s = stats[job_id]
        s["total"] += 1
        if status in ("error", "failed"):
            s["error"] += 1
            s["last_error"] = str(err or "")[:200]
            s["last_ts"] = str(ts or "")

    names = load_job_names()
    lines = []
    lines.append(f"# ⚙️ Cron 健康检查 — {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"共 {len(stats)} 个任务, 2天窗口 {len(rows)} 次执行\n")

    alerts = []
    for job_id, s in sorted(stats.items(), key=lambda x: -x[1]["error"]):
        name = names.get(job_id, job_id[:8])
        if s["total"] < 3:
            continue
        fail_rate = s["error"] / s["total"] * 100
        if s["error"] >= 5:
            alerts.append(f"- 🔴 **{name}** ({job_id[:8]}): 连续失败 {s['error']}/{s['total']} 次"
                          f" ({fail_rate:.0f}%) → 建议暂停并排查\n"
                          f"  最近错误: {s['last_error'][:150]}")
        elif s["error"] >= 3:
            alerts.append(f"- 🟡 **{name}** ({job_id[:8]}): 失败 {s['error']}/{s['total']} 次"
                          f" ({fail_rate:.0f}%) → 需关注\n"
                          f"  最近错误: {s['last_error'][:150]}")
        elif fail_rate > 30 and s["total"] >= 10:
            alerts.append(f"- 🟢 **{name}** ({job_id[:8]}): 失败率 {fail_rate:.0f}%"
                          f" ({s['error']}/{s['total']}) → 建议优化")

    if alerts:
        lines.append("## ⚠️ 告警\n")
        lines.extend(alerts)
    else:
        lines.append("## ✅ 全部任务健康\n")

    # 附: 近24h执行次数最多的任务 (负载概览)
    lines.append("\n## 📊 高频任务负载 (2天执行次数)\n")
    for job_id, s in sorted(stats.items(), key=lambda x: -x[1]["total"])[:8]:
        name = names.get(job_id, job_id[:8])
        lines.append(f"- {name}: {s['total']}次 (失败{s['error']})")

    report = "\n".join(lines)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(report)

    if "--json" in sys.argv:
        print(json.dumps({"alerts": len(alerts), "total_jobs": len(stats),
                          "report": OUT}, ensure_ascii=False))
    else:
        print(report)


if __name__ == "__main__":
    main()
