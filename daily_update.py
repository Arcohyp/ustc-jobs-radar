#!/usr/bin/env python3
"""每日定时任务：更新 USTC 专场招聘会数据 + 推送钉钉摘要。

由 cron 调用，每天一次（建议早晨，如 08:30）：
    30 8 * * * /usr/bin/python3 /path/to/daily_update.py >> /path/to/logs/cron.log 2>&1

钉钉凭证：环境变量 DINGTALK_WEBHOOK / DINGTALK_SECRET，
或项目根目录 secrets.yaml（见 notifier.py 文档字符串，不入库）。

流程：
    1. 运行 collect.py（在线全量更新，自带增量合并 + 过期归档 + 匹配）
    2. 解析 data/matches.md，提取统计与分档表格
    3. 通过本地 notifier.py 推送 Markdown 摘要
    4. 任一步失败都会推送错误通知（含输出尾部），避免静默失败

调试：
    python3 daily_update.py --dry-run   # 只打印摘要，不发钉钉、不联网
    python3 daily_update.py --no-fetch  # 用本地已有 matches.md 生成摘要并发送（不重新抓取）
"""

import argparse
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MATCHES_MD = DATA / "matches.md"
LOG_DIR = BASE / "logs"
REPORTS = BASE / "reports"

# 上游接口（ustc.ahbys.com 第三方厂商）偶发故障（如整站 502），
# 失败时间隔重试；仍失败则降级用本地缓存出日报，保证推送不中断。
COLLECT_RETRIES = 3          # 共尝试 3 次
COLLECT_RETRY_WAIT = 300     # 每次失败等 5 分钟再试

# 钉钉推送重试（网络/DNS 偶发抖动，避免静默失败）
NOTIFY_RETRIES = 3           # 共尝试 3 次
NOTIFY_RETRY_WAIT = 30       # 每次失败等 30 秒再试

sys.path.insert(0, str(BASE))

from notifier import send_notification  # noqa: E402


def send_notification_with_retry(title: str, content: str, status: str = "success") -> bool:
    """带重试的钉钉推送，避免网络/DNS 抖动导致静默失败。"""
    for attempt in range(1, NOTIFY_RETRIES + 1):
        ok = send_notification(title=title, content=content, status=status)
        if ok:
            return True
        if attempt < NOTIFY_RETRIES:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] notify failed, retry {attempt+1}/{NOTIFY_RETRIES} in {NOTIFY_RETRY_WAIT}s")
            time.sleep(NOTIFY_RETRY_WAIT)
    return False

MAX_ROWS_PER_SECTION = 15
SECTION_TITLES = [
    "🎯 目标企业（投递清单内）",
    "⚠️ 匹配但疑似国企/央企属性",
    "🔎 值得一看（方向高度相关）",
    "📄 JD 正文命中（标题未命中）",
    "🔍 JD 方向相关（宽松命中）",
    "🔍 方向相关（兜底关键词）",
    "🏢 组团招聘会内匹配的参会单位",
]


def run_collect() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(BASE / "collect.py")],
        capture_output=True, text=True, timeout=1800,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-15:])
    return proc.returncode == 0, tail


def parse_matches(md_text: str) -> dict:
    stats = {"upcoming": "?", "matched": "?"}
    m = re.search(r"未开始场次 (\d+)，其中匹配 (\d+) 场", md_text)
    if m:
        stats = {"upcoming": m.group(1), "matched": m.group(2)}
    cache_time = ""
    m = re.search(r"# 匹配报告（([^，]+)", md_text)
    if m:
        cache_time = m.group(1).strip()

    sections: dict[str, list[str]] = {}
    new_rows: list[str] = []
    current = None
    for line in md_text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif line.startswith("| 20") and current:
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) < 6:
                continue
            date_slot, theme, note, venue, url, new_mark = cols[:6]
            row = {
                "date": date_slot, "theme": theme, "note": note,
                "venue": venue, "url": url, "is_new": "✅" in new_mark,
                "section": current,
            }
            sections[current].append(row)
            if row["is_new"]:
                new_rows.append(row)
    return {"stats": stats, "cache_time": cache_time, "sections": sections, "new_rows": new_rows}


def fmt_row(r: dict) -> str:
    theme = r["theme"]
    if len(theme) > 38:
        theme = theme[:37] + "…"
    return f"- {r['date']} | {theme} | {r['venue']}"


def build_summary(parsed: dict) -> str:
    stats = parsed["stats"]
    sections = parsed["sections"]
    new_rows = parsed["new_rows"]

    lines = [
        f"**日期：** {date.today()}　**未开始：** {stats['upcoming']} 场　**匹配：** {stats['matched']} 场　**今日新增匹配：** {len(new_rows)} 场",
        "",
    ]

    if new_rows:
        lines.append("### 🆕 今日新增匹配场次")
        lines.extend(fmt_row(r) for r in new_rows[:MAX_ROWS_PER_SECTION])
        lines.append("")

    for title in SECTION_TITLES:
        rows = sections.get(title, [])
        if not rows:
            continue
        lines.append(f"### {title}（{len(rows)} 场）")
        lines.extend(fmt_row(r) for r in rows[:MAX_ROWS_PER_SECTION])
        if len(rows) > MAX_ROWS_PER_SECTION:
            lines.append(f"- ……其余 {len(rows) - MAX_ROWS_PER_SECTION} 场见完整报告")
        lines.append("")

    lines.append(f"完整报告：`{MATCHES_MD}`")
    return "\n".join(lines)


def save_local_report(summary: str, today: date, note: str = "") -> Path:
    """把日报摘要写到 reports/latest.md（只保留最新一期，不留历史存档）。

    注意：日报内容基于个人画像匹配，仅保存本地，不入库、不上传远端。"""
    REPORTS.mkdir(exist_ok=True)
    content = f"# USTC宣讲会日报 {today}\n\n{note}{summary}\n"
    latest = REPORTS / "latest.md"
    latest.write_text(content, encoding="utf-8")
    return latest


def drop_past_rows(parsed: dict, today: date) -> dict:
    """缓存降级时调用：collect.py 未运行，已过去的场次不会被自动剔除，
    这里按行内日期（YYYY-MM-DD 前缀）再过滤一遍。"""
    cutoff = f"{today:%Y-%m-%d}"
    for title in list(parsed["sections"]):
        parsed["sections"][title] = [
            r for r in parsed["sections"][title] if r["date"][:10] >= cutoff
        ]
    parsed["new_rows"] = [r for r in parsed["new_rows"]
                          if r["date"][:10] >= cutoff]
    parsed["stats"]["upcoming"] = str(
        sum(len(v) for v in parsed["sections"].values()))
    return parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印摘要，不联网、不发钉钉")
    ap.add_argument("--no-fetch", action="store_true",
                    help="跳过 collect.py，直接用本地 matches.md 生成并发送")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    today = date.today()

    if not args.dry_run and not args.no_fetch:
        ok, tail = run_collect()
        for attempt in range(2, COLLECT_RETRIES + 1):
            if ok:
                break
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] collect failed, "
                  f"retry {attempt}/{COLLECT_RETRIES} in {COLLECT_RETRY_WAIT}s")
            time.sleep(COLLECT_RETRY_WAIT)
            ok, tail = run_collect()

        if not ok:
            # 降级：上游接口持续不可用（如厂商站点 502），
            # 用本地缓存的 matches.md 照常出日报，仅标注数据截止时间。
            if MATCHES_MD.exists():
                parsed = parse_matches(MATCHES_MD.read_text(encoding="utf-8"))
                parsed = drop_past_rows(parsed, today)
                summary = build_summary(parsed)
                cache_note = (f"> ⚠️ 就业网数据接口暂时不可用（抓取失败），"
                              f"以下为 {parsed['cache_time'] or '上次成功抓取'} 的缓存数据\n\n")
                report_file = save_local_report(summary, today, note=cache_note)
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] local report: {report_file}")
                send_notification_with_retry(
                    title=f"USTC宣讲会日报 {today}（缓存数据）",
                    content=cache_note + summary,
                    status="warning",
                )
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] notify=cache-fallback")
                sys.exit(0)
            send_notification_with_retry(
                title=f"USTC宣讲会日报 {today}（抓取失败）",
                content=f"collect.py 执行失败，输出尾部：\n```\n{tail}\n```",
                status="error",
            )
            sys.exit(1)

    if not MATCHES_MD.exists():
        if args.dry_run:
            print("matches.md 不存在")
            sys.exit(1)
        send_notification_with_retry(
            title=f"USTC宣讲会日报 {today}（无数据）",
            content="data/matches.md 不存在，collect.py 可能未产出报告。",
            status="error",
        )
        sys.exit(1)

    parsed = parse_matches(MATCHES_MD.read_text(encoding="utf-8"))
    summary = build_summary(parsed)

    if args.dry_run:
        print(summary)
        return

    report_file = save_local_report(summary, today)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] local report: {report_file}")

    ok = send_notification_with_retry(
        title=f"USTC宣讲会日报 {today}",
        content=summary,
        status="success" if not parsed["new_rows"] else "warning",
    )
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] notify={'ok' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
