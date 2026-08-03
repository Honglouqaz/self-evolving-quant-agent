#!/usr/bin/env python3
"""
self_evolve_collect.py — 自进化系统·采集层
==========================================
每天定时采集 AI Agent 生态动态:
  1. GitHub Releases: Hermes 官方 + 同类工具 (Claude Code/Codex/OpenClaw/OpenHands)
  2. GitHub Commits: Hermes 官方最近提交 (近7天)
  3. HackerNews: AI agent 相关热门讨论
输出 JSON 到 DATA_DIR/self_evolve/collect_YYYYMMDD.json, 供 LLM agent 分析汇总。

用法: python3 self_evolve_collect.py [--days 7]
"""
import os, sys, json, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta

OUT_DIR = "DATA_DIR/self_evolve"
UA = "Mozilla/5.0 (HermesSelfEvolve/1.0)"
TIMEOUT = 20

# 关注的 GitHub 仓库 (名称, 中文标签, 类型, per_page)
# codex 的 release body 极大(单条>2MB), per_page=2 防止 IncompleteRead
REPOS = [
    ("NousResearch/hermes-agent", "Hermes Agent", "同类工具-本体", 10),
    ("anthropics/claude-code", "Claude Code", "同类工具", 10),
    ("openai/codex", "Codex", "同类工具", 2),
    ("openclaw/openclaw", "OpenClaw", "同类工具", 10),
    ("All-Hands-AI/OpenHands", "OpenHands", "同类工具", 10),
]

def http_get(url, headers=None, retries=3):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise last_err

def fetch_json(url):
    return json.loads(http_get(url))

def collect_github_releases(repo, days, per_page=10):
    """拉取指定仓库近 N 天 releases"""
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        data = fetch_json(f"https://api.github.com/repos/{repo}/releases?per_page={per_page}")
        out = []
        for rel in data:
            published = rel.get("published_at", "")
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt < since:
                    continue
            except Exception:
                pass
            out.append({
                "tag": rel.get("tag_name", ""),
                "name": rel.get("name") or rel.get("tag_name", ""),
                "published": published,
                "body": (rel.get("body") or "")[:1500],
                "url": rel.get("html_url", ""),
            })
        return out
    except Exception as e:
        return [{"error": str(e)}]

def collect_github_commits(repo, days):
    """拉取 Hermes 官方近 N 天提交 (只对 Hermes 本体做)"""
    try:
        since_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        url = f"https://api.github.com/repos/{repo}/commits?per_page=30&since={since_ts}"
        data = fetch_json(url)
        out = []
        for c in data:
            commit = c.get("commit", {})
            out.append({
                "date": commit.get("author", {}).get("date", ""),
                "message": (commit.get("message") or "").split("\n")[0][:200],
                "url": c.get("html_url", ""),
            })
        return out
    except Exception as e:
        return [{"error": str(e)}]

def collect_hn(days):
    """HackerNews: AI agent 相关热门讨论 (Algolia API)"""
    try:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        queries = ["AI agent", "AI coding agent", "Claude Code", "agent framework"]
        out = []
        for q in queries:
            try:
                url = (f"https://hn.algolia.com/api/v1/search_by_date?"
                       f"query={urllib.parse.quote(q)}&tags=story&numericFilters=created_at_i>{since}&hitsPerPage=8")
                data = fetch_json(url)
                for hit in data.get("hits", []):
                    out.append({
                        "query": q,
                        "title": hit.get("title", ""),
                        "points": hit.get("points", 0),
                        "comments": hit.get("num_comments", 0),
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                        "date": hit.get("created_at", ""),
                    })
            except Exception:
                continue
        # 按热度排序去重
        seen, dedup = set(), []
        for item in sorted(out, key=lambda x: -x.get("points", 0)):
            key = item["title"]
            if key and key not in seen:
                seen.add(key)
                dedup.append(item)
        return dedup[:25]
    except Exception as e:
        return [{"error": str(e)}]

def main():
    days = 7
    if len(sys.argv) > 1 and sys.argv[1] == "--days" and len(sys.argv) > 2:
        days = int(sys.argv[2])
    os.makedirs(OUT_DIR, exist_ok=True)

    result = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "github_releases": {},
        "github_commits_hermes": [],
        "hackernews": [],
    }
    for repo, label, rtype, pp in REPOS:
        print(f"⏳ 拉取 {repo} releases...", flush=True)
        result["github_releases"][repo] = {
            "label": label, "type": rtype,
            "releases": collect_github_releases(repo, days, pp),
        }
        time.sleep(1)  # GitHub API 限流保护
    print("⏳ 拉取 Hermes commits...", flush=True)
    result["github_commits_hermes"] = collect_github_commits("NousResearch/hermes-agent", days)
    time.sleep(1)
    print("⏳ 拉取 HackerNews...", flush=True)
    result["hackernews"] = collect_hn(days)

    ts = datetime.now().strftime("%Y%m%d")
    path = os.path.join(OUT_DIR, f"collect_{ts}.json")
    with open(path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 摘要
    rel_count = sum(len(v["releases"]) if isinstance(v.get("releases"), list) else 0
                    for v in result["github_releases"].values())
    print(f"✅ 采集完成: {path}")
    print(f"   GitHub releases: {rel_count} 条 | Hermes commits: {len(result['github_commits_hermes'])} 条 | HN: {len(result['hackernews'])} 条")

if __name__ == "__main__":
    main()
