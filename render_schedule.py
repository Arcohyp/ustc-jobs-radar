#!/usr/bin/env python3
"""渲染 USTC 2027 届秋招宣讲会双月日历图为 data/schedule.png。

数据来自 data/matches.md（collect.py 生成），分级来自下方 TIER_MAP 策展：
  A=必去（红） B=建议（蓝） C=可选（灰） 未列出的场次不展示
数据更新后重跑本脚本即可刷新图片：
    python3 render_schedule.py
"""

import calendar
import re
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.patches import FancyBboxPatch

BASE = Path(__file__).resolve().parent
MATCHES_MD = BASE / "data" / "matches.md"
OUT_PNG = BASE / "data" / "schedule.png"

for _f in (BASE / "fonts").glob("NotoSansCJKsc-*.otf"):
    fm.fontManager.addfont(str(_f))


def load_schedule_config() -> dict:
    import yaml
    for name in ("profile.yaml", "profile.example.yaml"):
        path = BASE / name
        if path.exists():
            return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("schedule", {})
    return {}


_SCHEDULE = load_schedule_config()
TIER_MAP = _SCHEDULE.get("tiers", {})
SHORT_NAME = _SCHEDULE.get("short_names", {})
CONFLICT_NOTES = list(_SCHEDULE.get("notes", []))

YEAR, MONTHS = 2026, (9, 10)
TODAY = date.today()

TIER_COLOR = {"A": "#d4380d", "B": "#1677ff", "C": "#8c8c8c"}
TIER_LABEL = {"A": "必去", "B": "建议", "C": "可选"}


def tier_of(theme: str) -> tuple[str | None, str | None]:
    for tier, keys in TIER_MAP.items():
        for k in keys:
            if k in theme:
                return tier, k
    return None, None


def load_events() -> dict[date, list[dict]]:
    events: dict[date, list[dict]] = {}
    for line in MATCHES_MD.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 20"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 6:
            continue
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s*(.*)", cols[0])
        if not m:
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        tier, key = tier_of(cols[1])
        if tier is None:
            continue
        events.setdefault(d, []).append({
            "time": m.group(4).split("-")[0],
            "name": SHORT_NAME.get(key, key),
            "tier": tier,
        })
    for day_events in events.values():
        day_events.sort(key=lambda e: e["time"])
    return events


def draw_month(ax, year: int, month: int, events: dict[date, list[dict]]):
    ax.set_xlim(0, 7)
    ax.set_ylim(0, 7.6)
    ax.axis("off")
    ax.set_title(f"{year} 年 {month} 月", fontsize=20, fontweight="bold", pad=10)

    for i, wd in enumerate(["一", "二", "三", "四", "五", "六", "日"]):
        color = "#d4380d" if i >= 5 else "#333333"
        ax.text(i + 0.5, 7.15, wd, ha="center", va="center",
                fontsize=13, fontweight="bold", color=color)

    weeks = calendar.monthcalendar(year, month)
    for row, week in enumerate(weeks):
        y_top = 6.6 - row * 1.1
        for col, day in enumerate(week):
            if day == 0:
                continue
            d = date(year, month, day)
            is_today = d == TODAY
            fc = "#fff7e6" if is_today else ("#fafafa" if col >= 5 else "#ffffff")
            ec = "#fa8c16" if is_today else "#d9d9d9"
            ax.add_patch(FancyBboxPatch(
                (col + 0.03, y_top - 1.04), 0.94, 1.0,
                boxstyle="round,pad=0.008,rounding_size=0.05",
                facecolor=fc, edgecolor=ec,
                linewidth=2.0 if is_today else 0.8))
            ax.text(col + 0.12, y_top - 0.16, str(day),
                    ha="left", va="top", fontsize=11,
                    fontweight="bold" if is_today else "normal",
                    color="#fa8c16" if is_today else "#595959")
            for j, ev in enumerate(events.get(d, [])[:3]):
                y = y_top - 0.46 - j * 0.21
                color = TIER_COLOR[ev["tier"]]
                ax.add_patch(FancyBboxPatch(
                    (col + 0.07, y - 0.085), 0.86, 0.175,
                    boxstyle="round,pad=0.004,rounding_size=0.04",
                    facecolor=color, edgecolor="none", alpha=0.92))
                ax.text(col + 0.5, y + 0.005, f"{ev['time']} {ev['name']}",
                        ha="center", va="center", fontsize=7.2,
                        color="white", fontweight="bold")


def main():
    events = load_events()
    plt.rcParams["font.family"] = ["Noto Sans CJK SC", "sans-serif"]
    fig, axes = plt.subplots(2, 1, figsize=(16, 15))

    for ax, month in zip(axes, MONTHS):
        draw_month(ax, YEAR, month, events)

    handles = [plt.Rectangle((0, 0), 1, 1, color=TIER_COLOR[t]) for t in "ABC"]
    fig.legend(handles, [f"{t} 级 · {TIER_LABEL[t]}" for t in "ABC"],
               loc="upper right", fontsize=13, frameon=False, ncol=3,
               bbox_to_anchor=(0.98, 1.0))
    fig.suptitle("USTC 2027 届秋招宣讲会日程表（9–10 月 · 已按简历匹配分级）",
                 fontsize=24, fontweight="bold", x=0.02, ha="left")

    note_text = "备注：\n" + "\n".join(f"· {n}" for n in CONFLICT_NOTES + ["橙框日期 = 今天"])
    fig.text(0.02, 0.015, note_text, fontsize=11, color="#595959", va="bottom")

    fig.tight_layout(rect=[0, 0.09, 1, 0.97])
    fig.savefig(OUT_PNG, dpi=150, bbox_inches="tight", facecolor="white")
    print(f"已保存：{OUT_PNG}（{len(events)} 天有推荐场次）")


if __name__ == "__main__":
    main()
