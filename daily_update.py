#!/usr/bin/env python3
"""每日定时任务：更新 USTC 专场招聘会数据 + 推送钉钉摘要。

由 cron 调用，每天一次（建议早晨，如 08:30），需通过 DSM_ROOT 环境变量
指向 dingtalk-server-monitor 项目目录（用于复用其钉钉 notifier）：
    30 8 * * * DSM_ROOT=/path/to/dingtalk-server-monitor /usr/bin/python3 /path/to/daily_update.py >> /path/to/logs/cron.log 2>&1

流程：
    1. 运行 collect.py（在线全量更新，自带增量合并 + 过期剔除 + 匹配）
    2. 解析 data/matches.md，提取统计与分档表格
    3. 通过 dingtalk-server-monitor 的 notifier 推送 Markdown 摘要
    4. 任一步失败都会推送错误通知（含输出尾部），避免静默失败

调试：
    python3 daily_update.py --dry-run   # 只打印摘要，不发钉钉、不联网
    python3 daily_update.py --no-fetch  # 用本地已有 matches.md 生成摘要并发送（不重新抓取）
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
MATCHES_MD = DATA / "matches.md"
LOG_DIR = BASE / "logs"

_dsm_root = os.environ.get("DSM_ROOT", "")
if not _dsm_root or not (Path(_dsm_root) / "core" / "notifier.py").exists():
    sys.exit("请设置 DSM_ROOT 环境变量指向 dingtalk-server-monitor 项目目录")
sys.path.insert(0, _dsm_root)

from core.notifier import send_notification  # noqa: E402

MAX_ROWS_PER_SECTION = 15
SECTION_TITLES = [
    "🎯 目标企业（投递清单内）",
    "⚠️ 匹配但疑似国企/央企属性",
    "🔎 值得一看（方向高度相关）",
    "📄 JD 正文命中（标题未命中）",
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
    return {"stats": stats, "sections": sections, "new_rows": new_rows}


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
        if not ok:
            send_notification(
                title=f"USTC宣讲会日报 {today}（抓取失败）",
                content=f"collect.py 执行失败，输出尾部：\n```\n{tail}\n```",
                status="error",
            )
            sys.exit(1)

    if not MATCHES_MD.exists():
        if args.dry_run:
            print("matches.md 不存在")
            sys.exit(1)
        send_notification(
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

    ok = send_notification(
        title=f"USTC宣讲会日报 {today}",
        content=summary,
        status="success" if not parsed["new_rows"] else "warning",
    )
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] notify={'ok' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
