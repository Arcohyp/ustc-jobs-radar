#!/usr/bin/env python3
"""从归档场次挖掘"仍可投递"的公司名单（纯离线，不访问网络）。

思路：宣讲会过期≠投递渠道关闭。归档里的 JD 正文（archive_details.jsonl）
保留了投递邮箱、网申链接等文本信息，本脚本：

    1. 对全部归档场次跑画像匹配（标题四档 + JD 关键词兜底，规则与 collect.py 一致）
    2. 对命中的场次从 JD 正文提取邮箱、外链、投递提示原句
    3. 按公司去重（同公司多场宣讲合并为一条，以来校最晚的一场为准）
    4. 输出 data/apply_mining.md，按档位分组，附时效提醒

注意：JD 是历史文本，提取的邮箱/链接可能已过期；二维码和图片海报
在抓取时未保留。建议拿公司名去搜当年校招入口确认。

用法：
    python3 mine_apply.py            # 全量挖掘，生成 data/apply_mining.md
"""

import re
from datetime import date
from pathlib import Path

from collect import (ARCHIVE_DETAIL_PATH, clean_html, extract_units,
                     is_fair_theme, match_detail, match_row, match_unit,
                     parse_hold_date)

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
ARCHIVE_PATH = DATA_DIR / "archive.jsonl"
OUT_PATH = DATA_DIR / "apply_mining.md"

# 常见 TLD 白名单，避免把紧跟的中文/大写字母误吞进邮箱域名（如 xxx.comQ）
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    r"\.(?:com\.cn|net\.cn|org\.cn|edu\.cn|com|cn|net|org|edu|io|co|cc|tech|ai)\b",
    re.I)
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+")
SNIP_RE = re.compile(r"网申|投递|简历|报名|申请|邮箱|career|campus", re.I)
DROP_URL_HINTS = ("ustc.edu.cn", "ahbys.com")  # 校内的信息页/源站链接，非投递渠道

# 与 matches.md 一致的分档与顺序
TIER_TITLES = [
    ("target", "🎯 目标企业（投递清单内）"),
    ("worth", "🔎 值得一看（方向高度相关）"),
    ("detail", "📄 JD 正文命中（标题未命中）"),
    ("direction_jd", "🔍 JD 方向相关（宽松命中）"),
    ("direction", "🔍 方向相关（兜底关键词）"),
]


def extract_contacts(text: str) -> tuple[list[str], list[str], str]:
    """从 JD 正文提取 (邮箱列表, 外链列表, 投递提示片段)。"""
    emails = sorted({e.strip(".,;") for e in EMAIL_RE.findall(text)})
    urls = set()
    for u in URL_RE.findall(text):
        u = u.rstrip("(")
        if u and not any(h in u for h in DROP_URL_HINTS):
            urls.add(u)
    urls = sorted(urls)
    snippet = ""
    m = SNIP_RE.search(text)
    if m:
        start = max(0, m.start() - 30)
        end = min(len(text), m.start() + 90)
        snippet = text[start:end].replace("\n", " ").strip()
    return emails, urls, snippet


def company_key(tier: str, name: str | None, theme: str) -> str:
    """去重键：目标/值得一看档用匹配到的公司名，其余档用主题（近似公司）。"""
    if name:
        return name
    return re.sub(r"(20\d\d.*?(招聘|校招|秋招|春招|宣讲会).*|宣讲会.*)$", "", theme).strip() or theme


def main():
    from collect import load_jsonl_by_id  # 延迟导入，复用按 ID 读取
    archived = load_jsonl_by_id(ARCHIVE_PATH)
    details = load_jsonl_by_id(ARCHIVE_DETAIL_PATH)

    # ---- 画像匹配（与 collect.py 同一套规则）----
    hits = []
    for r in archived.values():
        tier, name, note, is_soe = match_row(r)
        if not tier:
            tier, note = match_detail(r, details)
        if tier:
            hits.append({"row": r, "tier": tier, "name": name,
                         "note": note, "is_soe": is_soe})
    print(f"归档 {len(archived)} 场，画像命中 {len(hits)} 场")

    # ---- 提取联系方式，按公司合并 ----
    companies: dict[str, dict] = {}
    for h in hits:
        r = h["row"]
        key = company_key(h["tier"], h["name"], r.get("Theme", ""))
        d = details.get(r["ID"], {})
        text = d.get("DescriptionText", "")
        emails, urls, snippet = extract_contacts(text)
        c = companies.setdefault(key, {
            "tier": h["tier"], "is_soe": h["is_soe"], "themes": set(),
            "latest": None, "emails": set(), "urls": set(), "snippets": [],
            "hold": "",
        })
        c["themes"].add(r.get("Theme", ""))
        hd = parse_hold_date(r)
        if hd and (c["latest"] is None or hd > c["latest"]):
            c["latest"] = hd
            c["hold"] = r.get("HoldDateTxt", "")
            if snippet:
                c["snippets"] = [snippet]
        c["emails"] |= set(emails)
        c["urls"] |= set(urls)
        if snippet and len(c["snippets"]) < 2:
            c["snippets"].append(snippet)

    print(f"去重后 {len(companies)} 家公司")

    # ---- 组团招聘会：展开归档场次的参会单位，按岗位上下文匹配 ----
    fair_units: dict[str, dict] = {}
    for r in archived.values():
        if not is_fair_theme(r.get("Theme", "")):
            continue
        d = details.get(r["ID"], {})
        for name, ctx in extract_units(d.get("DescriptionText", "")):
            tier, note, is_soe = match_unit(name, ctx)
            if not tier:
                continue
            hd = parse_hold_date(r)
            u = fair_units.setdefault(name, {
                "name": name, "themes": set(), "latest": None, "hold": "",
                "note": note, "is_soe": is_soe, "ctx": ctx,
            })
            u["themes"].add(r.get("Theme", ""))
            if hd and (u["latest"] is None or hd > u["latest"]):
                u.update(latest=hd, hold=r.get("HoldDateTxt", ""),
                         note=note, is_soe=is_soe, ctx=ctx)
    print(f"团招参会单位命中 {len(fair_units)} 家")

    # ---- 输出报告 ----
    md = [f"# 归档挖掘：可回捞投递的公司名单（生成于 {date.today()}）",
          "",
          f"数据来源：{len(archived)} 场过期宣讲会归档，画像命中 {len(hits)} 场，"
          f"去重后 {len(companies)} 家公司。",
          "",
          "> ⚠️ 时效提醒：JD 为历史文本，下列邮箱/链接可能已失效；"
          "二维码与图片海报未保留。建议以公司名为准搜索当年校招入口确认投递状态。",
          ""]

    def row_line(c: dict) -> str:
        cell = lambda s: s.replace("|", "｜")
        emails = "、".join(sorted(c["emails"])) or "—"
        urls = "<br>".join(sorted(c["urls"])[:3]) or "—"
        snip = cell(c["snippets"][0]) if c["snippets"] else "—"
        theme = cell(sorted(c["themes"])[0])
        return (f"| {theme} | {c['hold'] or '—'} | {cell(emails)} "
                f"| {cell(urls)} | {snip} |")

    for tier, title in TIER_TITLES:
        items = [(k, c) for k, c in companies.items()
                 if c["tier"] == tier and not c["is_soe"]]
        if not items:
            continue
        items.sort(key=lambda kv: kv[1]["latest"] or date.min, reverse=True)
        md.append(f"\n## {title}（{len(items)} 家）\n")
        md.append("| 宣讲会主题 | 上次来校 | 投递邮箱 | 网申/链接 | JD 投递提示 |")
        md.append("|---|---|---|---|---|")
        md.extend(row_line(c) for _, c in items)

    soe_items = [c for c in companies.values() if c["is_soe"]]
    if soe_items:
        soe_items.sort(key=lambda c: c["latest"] or date.min, reverse=True)
        md.append(f"\n## ⚠️ 匹配但疑似国企/央企属性（{len(soe_items)} 家）\n")
        md.append("| 宣讲会主题 | 上次来校 | 投递邮箱 | 网申/链接 | JD 投递提示 |")
        md.append("|---|---|---|---|---|")
        md.extend(row_line(c) for c in soe_items)

    if fair_units:
        items = sorted(fair_units.values(),
                       key=lambda u: u["latest"] or date.min, reverse=True)
        md.append(f"\n## 🏢 组团招聘会内匹配的参会单位（{len(items)} 家）\n")
        md.append("| 参会单位 | 最近所属场次 | 时间 | 命中 | 岗位/专业片段 |")
        md.append("|---|---|---|---|---|")
        for u in items:
            cell = lambda s: s.replace("|", "｜")
            theme = cell(max(u["themes"], key=len))
            snip = cell(clean_html(u["ctx"])[:70]) if u["ctx"] else "—"
            note = ("⚠️国企 " if u["is_soe"] else "") + u["note"]
            md.append(f"| {cell(u['name'])} | {theme} | {cell(u['hold'] or '—')} "
                      f"| {cell(note)} | {snip} |")

    OUT_PATH.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"已保存：{OUT_PATH}")


if __name__ == "__main__":
    main()
