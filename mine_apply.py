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
    python3 mine_apply.py            # 全量挖掘，生成 data/apply_mining.md + apply_mining.html
                                     # （.html 为交互版：浏览器打开可逐家标记投递状态）
"""

import json
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
HTML_PATH = DATA_DIR / "apply_mining.html"
TRACKING_PATH = DATA_DIR / "tracking.json"

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
SOE_SECTION = "⚠️ 匹配但疑似国企/央企属性"
FAIR_SECTION = "🏢 组团招聘会内匹配的参会单位"

# 投递状态分类（单选）。key 存进 tracking.json / localStorage，
# 以公司名为键，重生成报告不丢；新公司默认未处理。
STATUSES = [
    ("", "⬜ 未处理"),
    ("interested", "⭐ 有意向"),
    ("watching", "👀 待观察"),
    ("applied", "📮 已投递"),
    ("in_process", "🔄 流程中"),
    ("offer", "✅ 已 Offer"),
    ("rejected", "🚫 不合适"),
    ("failed", "❌ 已挂"),
]
EXCLUDED_STATUSES = {"rejected", "failed"}


def esc(s: str) -> str:
    """HTML 转义（数据来自抓取文本，防注入）。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def render_html(sections: list[str], rows: list[dict], tracking: dict) -> str:
    """生成交互式单文件 HTML：状态标记存 localStorage，可导出/导入 JSON。"""
    dumps = lambda obj: json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    page = HTML_TEMPLATE
    for placeholder, value in [
        ("__DATE__", str(date.today())),
        ("__SECTIONS__", dumps(sections)),
        ("__ROWS__", dumps(rows)),
        ("__STATUSES__", dumps(STATUSES)),
        ("__EMBEDDED__", dumps(tracking)),
        ("__EXCLUDED__", dumps(sorted(EXCLUDED_STATUSES))),
    ]:
        page = page.replace(placeholder, value)
    return page


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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USTC 可回捞投递名单（交互版）</title>
<style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:22px}
h2{font-size:16px;margin:26px 0 10px;border-left:4px solid #4a7dff;padding-left:8px}
.toolbar{position:sticky;top:0;background:#fff;padding:10px 4px;border-bottom:1px solid #eee;display:flex;gap:14px;align-items:center;flex-wrap:wrap;z-index:9;font-size:13px}
button{padding:4px 12px;border:1px solid #ccc;background:#fff;border-radius:6px;cursor:pointer;font-size:13px}
.chip{border:1px solid #ccc;border-radius:12px;padding:2px 10px;font-size:12px;background:#fff;cursor:pointer}
.chip.on{background:#4a7dff;color:#fff;border-color:#4a7dff}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:10px 12px;margin:8px 0;display:flex;gap:12px;align-items:flex-start}
.card.excluded{opacity:.4}
.main{flex:1;min-width:0}
.name{font-weight:600}
.meta{color:#888;font-size:12px;margin:3px 0}
.snip{color:#555;font-size:13px;margin-top:3px}
.tags span{background:#eef3ff;border-radius:4px;padding:1px 6px;margin-right:6px;font-size:12px}
a{color:#4a7dff;word-break:break-all;font-size:13px}
select{flex-shrink:0;font-size:13px;padding:3px}
</style>
</head>
<body>
<h1>USTC 可回捞投递名单 <small style="color:#888;font-weight:normal">生成于 __DATE__</small></h1>
<div class="toolbar">
  <strong id="stats"></strong>
  <span id="filters" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"></span>
  <button onclick="selectAllStatuses()">全选</button>
  <button onclick="resetFilter()">重置</button>
  <span style="display:flex;gap:10px;align-items:center">
    <button onclick="exportJson()">备份导出</button>
    <label style="color:#666">恢复 <input type="file" accept=".json" onchange="importJson(event)"></label>
  </span>
  <span id="sync" style="color:#999;font-size:12px"></span>
</div>
<div id="app"></div>
<script>
const SECTIONS = __SECTIONS__;
const ROWS = __ROWS__;
const STATUSES = __STATUSES__;
const EXCLUDED = new Set(__EXCLUDED__);
const LS_KEY = "ustc-jobs-tracking-v1";
const SERVER = location.protocol.startsWith("http");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 初始状态：生成时嵌入的 data/tracking.json + 浏览器 localStorage 覆盖
const lsCache = (() => { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch (e) { return {}; } })();
let tracking = Object.assign({}, __EMBEDDED__, lsCache);
// 默认筛选：除"不合适/已挂"外全选
let selected = new Set(STATUSES.map(s => s[0]).filter(v => !EXCLUDED.has(v)));

function statusOf(key) { return tracking[key] || ""; }
function save() { localStorage.setItem(LS_KEY, JSON.stringify(tracking)); updateStats(); }

function setSync(t) { const el = document.getElementById("sync"); if (el) el.textContent = t; }
function postTracking(patch) {
  if (!SERVER) return Promise.resolve();
  return fetch("/api/tracking", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) })
    .then(r => r.json())
    .then(() => setSync("已自动保存 " + new Date().toLocaleTimeString()))
    .catch(() => setSync("⚠️ 自动保存失败：服务未响应，状态暂存浏览器"));
}

function updateStats() {
  const cnt = {};
  ROWS.forEach(r => { const s = statusOf(r.key); cnt[s] = (cnt[s] || 0) + 1; });
  const parts = STATUSES.filter(s => cnt[s[0]]).map(s => s[1] + " " + cnt[s[0]]);
  document.getElementById("stats").textContent = "共 " + ROWS.length + " 家 ｜ " + parts.join(" ｜ ");
}

function rowHtml(r) {
  const st = statusOf(r.key);
  const opts = STATUSES.map(([v, l]) =>
    `<option value="${v}"${v === st ? " selected" : ""}>${l}</option>`).join("");
  const emails = r.emails.length ? esc(r.emails.join("、")) : "—";
  const links = r.urls.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>");
  return `<div class="card${EXCLUDED.has(st) ? " excluded" : ""}" data-key="${esc(r.key)}">
    <select onchange="setStatus(this)">${opts}</select>
    <div class="main">
      <div class="name">${esc(r.name)}</div>
      <div class="meta">${esc(r.meta)}</div>
      <div class="tags"><span>${esc(r.section)}</span>${r.note ? `<span>${esc(r.note)}</span>` : ""}</div>
      <div class="meta">邮箱：${emails}</div>
      ${links ? `<div>${links}</div>` : ""}
      ${r.snippet ? `<div class="snip">${esc(r.snippet)}</div>` : ""}
    </div></div>`;
}

function render() {
  const app = document.getElementById("app");
  app.innerHTML = SECTIONS.map(sec => {
    const rows = ROWS.filter(r => r.section === sec && selected.has(statusOf(r.key)));
    if (!rows.length) return "";
    return `<h2>${sec}（${rows.length}）</h2>` + rows.map(rowHtml).join("");
  }).join("");
}

function renderFilters() {
  document.getElementById("filters").innerHTML = "筛选：" + STATUSES.map(([v, l]) =>
    `<button class="chip${selected.has(v) ? " on" : ""}" onclick="toggleStatus('${v}')">${l}</button>`).join("");
}
function toggleStatus(v) { selected.has(v) ? selected.delete(v) : selected.add(v); renderFilters(); render(); }
function selectAllStatuses() { selected = new Set(STATUSES.map(s => s[0])); renderFilters(); render(); }
function resetFilter() {
  selected = new Set(STATUSES.map(s => s[0]).filter(v => !EXCLUDED.has(v)));
  renderFilters(); render();
}

function setStatus(sel) {
  const key = sel.closest(".card").dataset.key;
  tracking[key] = sel.value;
  save();
  postTracking({ [key]: sel.value });
  render();
}

function exportJson() {
  const blob = new Blob([JSON.stringify(tracking, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "tracking.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function importJson(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      Object.assign(tracking, JSON.parse(reader.result));
      save(); postTracking(JSON.parse(reader.result)); render();
      alert("导入成功");
    } catch (e) { alert("导入失败：不是合法 JSON"); }
  };
  reader.readAsText(f);
  ev.target.value = "";
}

save(); renderFilters(); render();
if (SERVER) {
  setSync("连接服务中…");
  fetch("/api/tracking").then(r => r.json()).then(serverT => {
    // 服务器为准；localStorage 里有而服务器没有的键视为旧数据，自动补传（迁移）
    const missing = {};
    for (const [k, v] of Object.entries(lsCache)) if (!(k in serverT)) missing[k] = v;
    if (Object.keys(missing).length) postTracking(missing);
    tracking = Object.assign({}, lsCache, serverT);
    save(); render();
    setSync(Object.keys(missing).length
      ? "已迁移 " + Object.keys(missing).length + " 条旧状态，自动保存已开启"
      : "自动保存已开启（改动即时写入 data/tracking.json）");
  }).catch(() => setSync("⚠️ 服务不可用，状态只存浏览器"));
} else {
  setSync("file:// 模式：状态只存浏览器，看页面请先运行 track_server.py");
}
</script>
</body></html>
"""


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
          "",
          "交互版（浏览器打开可逐家标记投递状态，自动隐藏不合适/已挂）："
          "`data/apply_mining.html`",
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

    # ---- 交互版 HTML：与 md 相同的分组排序，每行可标记投递状态 ----
    tracking = {}
    if TRACKING_PATH.exists():
        tracking = json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
    sections: list[str] = []
    html_rows: list[dict] = []

    def add_rows(items: list[dict], section: str, meta_of, snippet_of):
        if not items:
            return
        if section not in sections:
            sections.append(section)
        for it in items:
            html_rows.append({
                "key": it["key"], "section": section, "name": it["name"],
                "meta": meta_of(it), "note": it.get("note", ""),
                "emails": sorted(it.get("emails", [])),
                "urls": sorted(it.get("urls", []))[:3],
                "snippet": snippet_of(it),
            })

    for tier, title in TIER_TITLES:
        items = [{"key": k, "name": sorted(c["themes"])[0], "latest": c["latest"],
                  "hold": c["hold"], "emails": c["emails"], "urls": c["urls"],
                  "note": "", "snippets": c["snippets"]}
                 for k, c in companies.items() if c["tier"] == tier and not c["is_soe"]]
        items.sort(key=lambda it: it["latest"] or date.min, reverse=True)
        add_rows(items, title,
                 lambda it: f"上次来校 {it['hold'] or '—'}",
                 lambda it: it["snippets"][0] if it["snippets"] else "")

    soe = [{"key": k, "name": sorted(c["themes"])[0], "latest": c["latest"],
            "hold": c["hold"], "emails": c["emails"], "urls": c["urls"],
            "note": "", "snippets": c["snippets"]}
           for k, c in companies.items() if c["is_soe"]]
    soe.sort(key=lambda it: it["latest"] or date.min, reverse=True)
    add_rows(soe, SOE_SECTION,
             lambda it: f"上次来校 {it['hold'] or '—'}",
             lambda it: it["snippets"][0] if it["snippets"] else "")

    units = [{"key": u["name"], "name": u["name"], "latest": u["latest"],
              "hold": u["hold"], "note": u["note"], "themes": u["themes"],
              "ctx": u["ctx"], "emails": [], "urls": []}
             for u in fair_units.values()]
    units.sort(key=lambda it: it["latest"] or date.min, reverse=True)
    add_rows(units, FAIR_SECTION,
             lambda it: f"{max(it['themes'], key=len)} ｜ {it['hold'] or '—'}",
             lambda it: clean_html(it["ctx"])[:100])

    HTML_PATH.write_text(render_html(sections, html_rows, tracking),
                         encoding="utf-8")
    print(f"已保存：{HTML_PATH}（{len(html_rows)} 家，状态 {len(tracking)} 条）")


if __name__ == "__main__":
    main()
