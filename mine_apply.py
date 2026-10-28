#!/usr/bin/env python3
"""从归档场次挖掘"仍可投递"的企业名单（纯离线，不访问网络）。

思路：宣讲会过期≠投递渠道关闭。归档里的 JD 正文（archive_details.jsonl）
保留了投递邮箱、网申链接等文本信息，本脚本：

    1. 对全部归档场次跑画像匹配（标题四档 + JD 关键词兜底，规则与 collect.py 一致），
       并展开组团招聘会的参会单位（extract_units / match_unit）
    2. 企业为主键归并：同企业无论几场宣讲、单独办会还是团招参展，合并为一张企业卡；
       按归一化名称（去公司后缀/标点/大小写）判同，自动别名合并
    3. 从 JD 提取邮箱、外链、投递提示，并打自动标签（命中的方向/强关键词、国企）
    4. 输出：
       - data/apply_mining.md（文本版，供 grep）
       - data/apply_mining.html（交互版：状态标记、标签增删、搜索、JD 全文检索——
         后者需 track_server.py 提供 /api/search）

注意：JD 是历史文本，提取的邮箱/链接可能已过期；二维码和图片海报
在抓取时未保留。建议拿企业名去搜当年校招入口确认。

用法：
    python3 mine_apply.py
"""

import json
import re
from datetime import date
from pathlib import Path

from collect import (ARCHIVE_DETAIL_PATH, DIRECTION_KEYWORDS, JD_KEYWORDS,
                     clean_html, extract_units, is_fair_theme, match_detail,
                     match_row, match_unit, parse_hold_date)

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
ARCHIVE_PATH = DATA_DIR / "archive.jsonl"
OUT_PATH = DATA_DIR / "apply_mining.md"
HTML_PATH = DATA_DIR / "apply_mining.html"
TRACKING_PATH = DATA_DIR / "tracking.json"
TAGS_PATH = DATA_DIR / "tags.json"

# 常见 TLD 白名单，避免把紧跟的中文/大写字母误吞进邮箱域名（如 xxx.comQ）
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
    r"\.(?:com\.cn|net\.cn|org\.cn|edu\.cn|com|cn|net|org|edu|io|co|cc|tech|ai)\b",
    re.I)
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+")
SNIP_RE = re.compile(r"网申|投递|简历|报名|申请|邮箱|career|campus", re.I)
DROP_URL_HINTS = ("ustc.edu.cn", "ahbys.com")  # 校内的信息页/源站链接，非投递渠道

# 分档与优先级（数字越小越靠前）
TIER_TITLES = [
    ("target", "🎯 目标企业（投递清单内）"),
    ("worth", "🔎 值得一看（方向高度相关）"),
    ("detail", "📄 JD 正文命中（标题未命中）"),
    ("direction_jd", "🔍 JD 方向相关（宽松命中）"),
    ("direction", "🔍 方向相关（兜底关键词）"),
]
TIER_RANK = {tier: i for i, (tier, _) in enumerate(TIER_TITLES)}
SOE_SECTION = "⚠️ 匹配但疑似国企/央企属性"

# 投递状态分类（单选）。key 存进 tracking.json / localStorage，
# 以企业名为键，重生成报告不丢；新企业默认未处理。
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

_CORP_SUFFIX_RE = re.compile(
    r"(股份有限公司|有限责任公司|集团有限公司|集团股份有限公司|集成电路|"
    r"机器人|半导体|微电子|集成|电子科技|信息|有限公司|集团|股份|公司|汽车|科技|技术)$")

_DATE_PREFIX_RE = re.compile(r"^(\d{1,2}\s*月\s*\d{1,2}\s*日|(19|20)\d{2}\s*年)")

# 前导/尾随地域词：只有剩余部分仍像企业名（长度够、不带机构简称）才剥，
# 避免 "上海银行"->"银行"、"西安交通大学"->"交通大学" 这类误并
# 地域词（前后中间都可能出现："深圳埃芯半导体"、"国仪量子（合肥）技术"）：
# 只有整体够长、剩余仍像企业名且不带机构简称时才剥，防止
# "上海银行"->"银行"、"西安交通大学"->"交通大学" 这类误并
_CITIES = ("北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州",
           "无锡", "合肥", "成都", "武汉", "西安", "长沙", "郑州", "宁波", "厦门",
           "青岛", "济南", "烟台", "常州", "温州", "东莞", "佛山", "珠海", "三亚",
           "大连", "沈阳", "长春", "哈尔滨", "石家庄", "太原", "兰州", "南昌",
           "昆明", "贵阳", "南宁", "福州", "海口", "江苏", "浙江", "安徽", "广东",
           "山东", "四川", "湖南", "湖北", "福建", "陕西", "河南", "河北", "江西",
           "辽宁", "吉林", "黑龙江", "内蒙古", "广西", "云南", "贵州", "海南",
           "甘肃", "山西")
_ORG_GUARD = ("大学", "学院", "中学", "学校", "医院", "银行", "研究院", "研究所")

# ---- base 地识别：JD 里"工作地点/base"锚点附近的城市权重高，全文散提权重低 ----
_CITY_RE = re.compile("|".join(map(re.escape, sorted(_CITIES, key=len, reverse=True))))
_LOC_ANCHOR_RE = re.compile(
    r"(?:工作地[点址城]|工作城市|base地?|入职地|期望城市|所在城市|办公地[点址]|"
    r"公司地址|单位地址|地\s*址)[：: ]*([^\n。；;]{2,60})")

_REGION_MAP: dict[str, str] = {}
for _cs, _rg in (
    (("合肥", "安徽"), "合肥"),
    (("上海", "江苏", "浙江", "南京", "苏州", "无锡", "常州", "温州", "杭州", "宁波"), "江浙沪"),
    (("北京", "天津", "河北", "石家庄"), "京津冀"),
    (("广东", "广州", "深圳", "东莞", "佛山", "珠海"), "珠三角"),
    (("四川", "成都", "重庆"), "成渝"),
):
    for _c in _cs:
        _REGION_MAP[_c] = _rg
# 省级地名（用于展示去重：top3 里同区域已有具体城市时不再显示省份）
_PROVINCES = frozenset(("江苏", "浙江", "安徽", "广东", "山东", "四川", "湖南", "湖北",
                        "福建", "陕西", "河南", "河北", "江西", "辽宁", "吉林", "黑龙江",
                        "内蒙古", "广西", "云南", "贵州", "海南", "甘肃", "山西"))
REGIONS = ["合肥", "江浙沪", "京津冀", "珠三角", "成渝", "其他", "未识别"]


def extract_cities(text: str) -> dict[str, int]:
    """从 JD 正文提取城市提及权重：工作地点类锚点附近 ×3，全文提及 ×1。"""
    if not text:
        return {}
    w: dict[str, int] = {}
    for m in _LOC_ANCHOR_RE.finditer(text):
        for c in _CITY_RE.findall(m.group(1)):
            w[c] = w.get(c, 0) + 3
    for c in set(_CITY_RE.findall(text)):
        w[c] = w.get(c, 0) + 1
    return w


def card_locations(city_weights: dict[str, int], name: str) -> tuple[list[str], list[str]]:
    """聚合各场权重 -> (展示城市 top3[合肥优先], 区域列表)。

    无 JD 城市时退回公司名里的城市（"苏州镁伽…"->苏州）。展示城市与区域
    严格对应：卡片上看到的城市都可按它的区域筛出来。"""
    w = dict(city_weights)
    if not w:
        for c in set(_CITY_RE.findall(name)):
            w[c] = 2
    if not w:
        return [], ["未识别"]
    top = sorted(w, key=lambda c: (-w[c], c))[:3]
    if "合肥" in w and "合肥" not in top:
        top = ["合肥"] + top[:2]
    # 同区域已有具体城市时不重复展示省份（"合肥 · 安徽" -> "合肥"）
    top = [c for c in top
           if c not in _PROVINCES
           or not any(o != c and _REGION_MAP.get(o, "其他") == _REGION_MAP.get(c, "其他")
                      for o in top)]
    regions: list[str] = []
    for c in top:
        rg = _REGION_MAP.get(c, "其他")
        if rg not in regions:
            regions.append(rg)
    return top, regions


def _strip_regions(n: str) -> str:
    if len(n) < 6 or any(g in n for g in _ORG_GUARD):
        return n
    n2 = n
    for city in _CITIES:
        n2 = n2.replace(city + "市", "").replace(city, "")
    return n2 if len(n2) >= 4 else n


def norm_key(name: str) -> str:
    """归一化企业名用于判同：去日期前缀、括号注释、公司后缀、地域词、
    品牌字母前缀、标点，忽略大小写。

    "长鑫科技集团股份有限公司"、"长鑫科技"、"长鑫科技集团" -> 同一归并键。
    注意可能误并名字相近的不同企业（少见），宁可少拆不可多拆的原则下可接受。"""
    n = _DATE_PREFIX_RE.sub("", name.strip())
    n = re.sub(r"[（(][^）)]*[）)]", "", n)  # 括号注释：东阳光集团（电子材料板块）
    n = n.lower()
    # 品牌字母前缀：AAC瑞声科技 -> 瑞声科技；剩余太短则保留（TCL实业 -> tcl实业）
    m = re.match(r"^[a-z0-9]+", n)
    if m and len(n) - m.end() >= 4:
        n = n[m.end():]
    # 地域与后缀交替剥到稳定（每轮地域先剥，再剥后缀）：
    # 安徽江淮汽车集团 -> 江淮；深圳优艾智合机器人科技有限公司需 3 轮。
    # 地域必须先于本轮后缀剥："江苏卓胜微电子" 若先剥微电子成 "江苏卓胜"(4 字)，
    # 会触发 _strip_regions 长度守卫而漏剥省份，与全称归一化结果不一致。
    for _ in range(5):
        n2 = _CORP_SUFFIX_RE.sub("", _strip_regions(n))
        if n2 == n:
            break
        n = n2
    n = re.sub(r"[\s\"'“”‘’（）()·、,，.。]", "", n)
    return n


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


def _infer_name(theme: str) -> str:
    """从宣讲主题推断企业展示名：剥标签/引号口号/日期前缀/届数/招聘尾巴。

    "3月27日杰瑞集团2026届春招空中宣讲会" -> "杰瑞集团"
    ""汽梦同行，研创未来" 中国汽研" -> "中国汽研"
    "AAC瑞声科技26届校园招聘线下" -> "AAC瑞声科技"
    """
    t = theme.strip()
    for _ in range(2):
        t = re.sub(r"^【[^】]*】[：:]?\s*", "", t)               # 【上市国企】类标签
    t = re.sub(r"^[“\"'‘][^”\"'’]*[”\"'’]\s*", "", t)         # 引号口号前缀
    t = re.sub(r"^\d{1,2}\s*月\s*\d{1,2}\s*日[，,]?\s*", "", t)  # 3月27日
    t = re.sub(r"^(19|20)\d{2}\s*年\s*", "", t)                # 2026年
    t = re.sub(r"^((19|20)\d{2}|\d{2})\s*届[，,]?\s*", "", t)   # 2026届 / 26届
    # 尾切："…2026全球春季校园招聘"、"兆尹科技26春招"；\d{2} 前瞻带
    # (?<![0-9A-Za-z])，避免 "360…校园招聘" 从数字中间切开
    t = re.sub(r"((?<![0-9A-Za-z])((19|20)\d{2}|\d{2})"
               r"(?=[\u4e00-\u9fff\s]{0,10}?(招聘|校招|秋招|春招|宣讲|找聘))"
               r".*|(?<![0-9A-Za-z])((19|20)\d{2}|\d{2})\s*届.*|"
               r"(秋季|春季|冬季|暑期|全球)?\s*(校园|空中|毕业生|应届)?\s*"
               r"(招聘|校招|秋招|春招|找聘|宣讲会).*|专场.*|宣讲会.*)$", "", t)
    # 口号与企业名连写（"汽梦同行，研创未来  中国汽研"）：取最后一段，
    # 多轮处理 "生而不凡  敢为向上——TCL实业" 这类多重分隔
    sep = r"[，,、·｜|丨—–\-]|\s{2,}|\u3000+"
    for _ in range(3):
        if re.search(sep, t):
            t = re.split(sep, t)[-1]
    # 口号与名单空格连写（"新央企 向新而行 长安汽车"）：单空格再取末词
    if re.search(r"\s", t):
        last = re.split(r"\s+", t)[-1]
        if len(last) >= 4 and re.search(r"[一-龥]", last):
            t = last
    t = t.strip("“”\"'‘’【】 ，,、·|｜丨—–-　")
    if not t:
        # 剥完只剩招聘套话时（"【捷迅光电】2027届校园招聘宣讲会"），用标签内容兜底
        m = re.search(r"【([^】]{2,20})】", theme)
        t = (m.group(1).strip() if m else "") or theme
    return t or theme


def company_display_name(name: str | None, theme: str) -> str:
    """展示名：画像匹配名优先，否则从宣讲主题推断。"""
    return name if name else _infer_name(theme)


def add_company(companies: dict, nkey: str, display: str, tier: str,
                is_soe: bool, session: dict) -> dict:
    """按归并键 upsert 企业卡：场次进列表，档位取最高优先级。"""
    c = companies.get(nkey)
    if c is None:
        c = companies[nkey] = {
            "nkey": nkey, "names": set(), "tier": tier, "is_soe": is_soe,
            "sessions": [], "emails": set(), "urls": set(), "tags": set(),
            "latest": None,
        }
    c["names"].add(display)
    if TIER_RANK.get(tier, 99) < TIER_RANK.get(c["tier"], 99):
        c["tier"] = tier
    c["is_soe"] = c["is_soe"] or is_soe
    c["sessions"].append(session)
    d = session.get("hold_date")
    if d and (c["latest"] is None or d > c["latest"]):
        c["latest"] = d
    c["emails"] |= set(session.get("emails", []))
    c["urls"] |= set(session.get("urls", []))
    c["tags"] |= set(session.get("tags", []))
    return c


def render_html(sections: list[str], rows: list[dict], tracking: dict,
                user_tags: dict, row_keys: list[str]) -> str:
    """生成交互式单文件 HTML。"""
    dumps = lambda obj: json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    page = HTML_TEMPLATE
    for placeholder, value in [
        ("__DATE__", str(date.today())),
        ("__SECTIONS__", dumps(sections)),
        ("__ROWS__", dumps(rows)),
        ("__STATUSES__", dumps(STATUSES)),
        ("__REGIONS__", dumps(REGIONS)),
        ("__EXCLUDED__", dumps(sorted(EXCLUDED_STATUSES))),
        ("__EMBEDDED_TRACKING__", dumps(tracking)),
        ("__EMBEDDED_TAGS__", dumps(user_tags)),
        ("__ROWKEYS__", dumps(row_keys)),
    ]:
        page = page.replace(placeholder, value)
    return page


def main():
    from collect import load_jsonl_by_id  # 延迟导入，复用按 ID 读取
    archived = load_jsonl_by_id(ARCHIVE_PATH)
    details = load_jsonl_by_id(ARCHIVE_DETAIL_PATH)

    companies: dict[str, dict] = {}

    # ---- 专场宣讲：画像匹配 + 联系方式 + 自动标签 ----
    # 组团招聘会的 JD 是参会单位表，专场匹配只会命中"招聘会"本身产生噪音卡；
    # 能展开出单位的团招直接跳过（单位卡已覆盖），展开不出单位的才保留专场卡
    fair_units: dict[int, list] = {}
    for r in archived.values():
        if is_fair_theme(r.get("Theme", "")):
            text = details.get(r["ID"], {}).get("DescriptionText", "")
            fair_units[r["ID"]] = extract_units(text)

    n_matched = 0
    for r in archived.values():
        tier, name, note, is_soe = match_row(r)
        if not tier:
            tier, note = match_detail(r, details)
        if not tier:
            continue
        title = r.get("Theme", "")
        if fair_units.get(r["ID"]):
            continue
        n_matched += 1
        d = details.get(r["ID"], {})
        text = d.get("DescriptionText", "")
        emails, urls, snippet = extract_contacts(text)
        # 自动标签：标题/JD 命中的关键词
        tags = {k for k in DIRECTION_KEYWORDS if k in title}
        tags |= {k for k in JD_KEYWORDS if k in text}
        tags |= {k for k in DIRECTION_KEYWORDS if k in text}
        if is_soe:
            tags.add("国企")
        hd = parse_hold_date(r)
        display = company_display_name(name, title)
        add_company(companies, norm_key(display), display, tier, is_soe, {
            "iid": r["ID"], "theme": title,
            "hold": r.get("HoldDateTxt", "").replace("&nbsp;", " ").strip(),
            "hold_date": str(hd) if hd else "",
            "url": f"https://job.ustc.edu.cn/Specialrecruitment/info.aspx?itemid={r['ID']}",
            "src": "专场宣讲", "note": note, "snippet": snippet,
            "cities": extract_cities(text),
            "emails": emails, "urls": urls, "tags": sorted(tags),
        })
    print(f"专场命中 {n_matched} 场，归并后 {len(companies)} 家企业")

    # ---- 组团招聘会：展开参会单位，并入同一张企业卡 ----
    n_units = 0
    for r in archived.values():
        for uname, ctx in fair_units.get(r["ID"], []):
            tier, note, is_soe = match_unit(uname, ctx)
            if not tier:
                continue
            n_units += 1
            emails, urls, snippet = extract_contacts(ctx)
            tags = {k for k in JD_KEYWORDS if k in ctx}
            tags |= {k for k in DIRECTION_KEYWORDS if k in ctx}
            if is_soe:
                tags.add("国企")
            hd = parse_hold_date(r)
            add_company(companies, norm_key(uname), uname, tier, is_soe, {
                "iid": r["ID"], "theme": r.get("Theme", ""),
                "hold": r.get("HoldDateTxt", "").replace("&nbsp;", " ").strip(),
                "hold_date": str(hd) if hd else "",
                "url": f"https://job.ustc.edu.cn/Specialrecruitment/info.aspx?itemid={r['ID']}",
                "src": f"团招·{uname}", "note": note, "snippet": snippet,
                "cities": extract_cities(ctx),
                "emails": emails, "urls": urls, "tags": sorted(tags),
            })
    print(f"团招单位命中 {n_units} 家，归并后共 {len(companies)} 家企业")

    # ---- 展示名：取最短别名（不带日期/口号前缀的最干净形态）----
    for c in companies.values():
        c["name"] = min(c["names"], key=lambda n: (len(n), n))

    # ---- 分组排序 ----
    def in_tier(tier: str) -> list[dict]:
        items = [c for c in companies.values()
                 if c["tier"] == tier and not c["is_soe"]]
        items.sort(key=lambda c: c["latest"] or date.min, reverse=True)
        return items

    groups = [(title, in_tier(tier)) for tier, title in TIER_TITLES]
    soe = sorted((c for c in companies.values() if c["is_soe"]),
                 key=lambda c: c["latest"] or date.min, reverse=True)

    # ---- 文本版 md ----
    md = [f"# 归档挖掘：可回捞投递的企业名单（生成于 {date.today()}）",
          "",
          f"数据来源：{len(archived)} 场过期宣讲会归档，专场命中 {n_matched} 场、"
          f"团招单位 {n_units} 家，归并后 {len(companies)} 家企业（企业为主键）。",
          "",
          "> ⚠️ 时效提醒：JD 为历史文本，下列邮箱/链接可能已失效；"
          "二维码与图片海报未保留。建议以企业名为准搜索当年校招入口确认投递状态。",
          "",
          "交互版（状态标记/标签/搜索/JD 全文检索）：`data/apply_mining.html`",
          ""]

    def row_line(c: dict) -> str:
        cell = lambda s: str(s).replace("|", "｜")
        sess = sorted(c["sessions"],
                      key=lambda s: s["hold_date"], reverse=True)
        s0 = sess[0]
        emails = "、".join(sorted(c["emails"])) or "—"
        urls = "<br>".join(sorted(c["urls"])[:3]) or "—"
        snip = cell(s0["snippet"]) if s0["snippet"] else "—"
        more = f"（共 {len(sess)} 场）" if len(sess) > 1 else ""
        return (f"| {cell(s0['theme'])} | {cell(s0['hold'] or '—')}{more} "
                f"| {cell(emails)} | {cell(urls)} | {snip} |")

    for title, items in groups:
        if not items:
            continue
        md.append(f"\n## {title}（{len(items)} 家）\n")
        md.append("| 宣讲会主题 | 上次来校 | 投递邮箱 | 网申/链接 | JD 投递提示 |")
        md.append("|---|---|---|---|---|")
        md.extend(row_line(c) for c in items)
    if soe:
        md.append(f"\n## {SOE_SECTION}（{len(soe)} 家）\n")
        md.append("| 宣讲会主题 | 上次来校 | 投递邮箱 | 网申/链接 | JD 投递提示 |")
        md.append("|---|---|---|---|---|")
        md.extend(row_line(c) for c in soe)

    OUT_PATH.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"已保存：{OUT_PATH}")

    # ---- 交互版 HTML ----
    tracking = (json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
                if TRACKING_PATH.exists() else {})
    user_tags = (json.loads(TAGS_PATH.read_text(encoding="utf-8"))
                 if TAGS_PATH.exists() else {})

    sections: list[str] = []
    rows: list[dict] = []

    def add_rows(items: list[dict], section: str) -> None:
        if not items:
            return
        if section not in sections:
            sections.append(section)
        for c in items:
            sess = sorted(c["sessions"],
                          key=lambda s: s["hold_date"], reverse=True)
            cw: dict[str, int] = {}
            for s in sess:
                for city, n in s.get("cities", {}).items():
                    cw[city] = cw.get(city, 0) + n
            cities, regions = card_locations(cw, c["name"])
            rows.append({
                "key": c["nkey"],
                "name": c["name"],
                "section": section,
                "note": sess[0]["note"],
                "tags": sorted(c["tags"]),
                "cities": cities,
                "regions": regions,
                "emails": sorted(c["emails"]),
                "urls": sorted(c["urls"])[:4],
                "latest": str(c["latest"]) if c["latest"] else "",
                "n_sessions": len(sess),
                "sessions": [{
                    "iid": s["iid"], "theme": s["theme"], "hold": s["hold"],
                    "url": s["url"], "src": s["src"],
                    "snippet": s["snippet"][:120],
                } for s in sess],
            })

    for (tier, title), (_, items) in zip(TIER_TITLES, groups):
        add_rows(items, title)
    add_rows(soe, SOE_SECTION)

    # ---- 旧键迁移：老版本页面用展示名做键，企业卡归并后键是归一化名 ----
    row_keys = {r["key"] for r in rows}

    def _migrate_keys(d: dict) -> tuple[dict, bool]:
        out, changed = {}, False
        for k, v in d.items():
            nk = k if k in row_keys else norm_key(k)
            if nk not in row_keys:
                nk = k  # 已下架企业：保留原键
            changed |= nk != k
            out[nk] = v
        return out, changed

    tracking, ch_t = _migrate_keys(tracking)
    user_tags, ch_u = _migrate_keys(user_tags)
    for path, data, changed in ((TRACKING_PATH, tracking, ch_t),
                                (TAGS_PATH, user_tags, ch_u)):
        if changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)
            print(f"已迁移旧键：{path.name}")

    HTML_PATH.write_text(
        render_html(sections, rows, tracking, user_tags, sorted(row_keys)),
        encoding="utf-8")
    print(f"已保存：{HTML_PATH}（{len(rows)} 家企业）")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USTC 可回捞投递名单（交互版）</title>
<style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222;background:#fafafa}
h1{font-size:22px}
h2{font-size:16px;margin:26px 0 10px;border-left:4px solid #4a7dff;padding-left:8px}
button{padding:4px 12px;border:1px solid #ccc;background:#fff;border-radius:6px;cursor:pointer;font-size:13px}
.chip{border:1px solid #ccc;border-radius:12px;padding:2px 10px;font-size:12px;background:#fff;cursor:pointer}
.chip.on{background:#4a7dff;color:#fff;border-color:#4a7dff}
.chip.tag.on{background:#2f9e44;border-color:#2f9e44}
.toolbar{position:sticky;top:0;background:#fff;padding:10px 4px;border-bottom:1px solid #eee;z-index:9;font-size:13px}
.toolbar .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0}
#search{flex:1;min-width:200px;padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:14px}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:10px 12px;margin:8px 0;display:flex;gap:12px;align-items:flex-start}
.card.excluded{opacity:.4}
.main{flex:1;min-width:0}
.name{font-weight:600}
.meta{color:#888;font-size:12px;margin:3px 0}
.snip{color:#555;font-size:13px;margin-top:3px}
.tag{display:inline-block;background:#eef3ff;border-radius:4px;padding:1px 6px;margin:2px 4px 2px 0;font-size:12px}
.tag.utag{background:#e7f5ec;color:#2b8a3e;cursor:pointer}
a{color:#4a7dff;word-break:break-all;font-size:13px}
select{flex-shrink:0;font-size:13px;padding:3px}
details{margin-top:6px;font-size:13px}
summary{cursor:pointer;color:#666}
.sess{padding:4px 0;border-top:1px dashed #eee}
.tagadd{font-size:12px;padding:2px 6px;width:110px;border:1px solid #ddd;border-radius:4px}
</style>
</head>
<body>
<h1>USTC 可回捞投递名单 <small style="color:#888;font-weight:normal">生成于 __DATE__</small></h1>
<div class="toolbar">
  <div class="row">
    <strong id="stats"></strong>
    <span style="flex:1"></span>
    <button onclick="exportJson()">备份导出</button>
    <label style="color:#666">恢复 <input type="file" accept=".json" onchange="importJson(event)"></label>
    <span id="sync" style="color:#999;font-size:12px"></span>
  </div>
  <div class="row">
    <input id="search" placeholder="搜索企业名 / 宣讲主题 / 备注 / 标签；输入≥2字自动检索 JD 全文" oninput="onSearch(this.value)">
    <span id="searchHint" style="color:#999;font-size:12px"></span>
  </div>
  <div class="row" id="statusFilters"></div>
  <div class="row" id="regionFilters"></div>
  <div class="row" id="tagFilters"></div>
</div>
<div id="app"></div>
<script>
const SECTIONS = __SECTIONS__;
const ROWS = __ROWS__;
const STATUSES = __STATUSES__;
const REGIONS = __REGIONS__;
const EXCLUDED = new Set(__EXCLUDED__);
const LS_KEY = "ustc-jobs-tracking-v1";
const LS_TAGS = "ustc-jobs-utags-v1";
const SERVER = location.protocol.startsWith("http");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- 状态（statuses）与用户标签（utags）：localStorage 起步，服务模式下以服务器为准 ----
// 旧版本用展示名做键（如"京东集团"），企业卡归并后键为归一化名（如"京东"）：
// 两边数据都在加载时按归一化规则重映射，旧键不会丢
const ROWKEYS = new Set(__ROWKEYS__);
function normKey(name) {
  return String(name).trim()
    .replace(/(股份有限公司|有限责任公司|集团有限公司|集团股份有限公司|有限公司|集团)$/, "")
    .replace(/[\s"'“”‘’（）()·、,，.。]/g, "").toLowerCase();
}
function remapKeys(d) {
  const out = {};
  for (const [k, v] of Object.entries(d || {})) {
    const nk = ROWKEYS.has(k) ? k : normKey(k);
    out[ROWKEYS.has(nk) ? nk : k] = v;
  }
  return out;
}
const lsT = remapKeys((() => { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch (e) { return {}; } })());
const lsU = remapKeys((() => { try { return JSON.parse(localStorage.getItem(LS_TAGS) || "{}"); } catch (e) { return {}; } })());
let tracking = Object.assign({}, remapKeys(__EMBEDDED_TRACKING__), lsT);
let utags = Object.assign({}, remapKeys(__EMBEDDED_TAGS__), lsU);

let selectedStatuses = new Set(STATUSES.map(s => s[0]).filter(v => !EXCLUDED.has(v)));
let selectedTags = new Set();
let selectedRegions = new Set();  // 空 = 不按 base 区域筛选
let query = "";
let jdHitKeys = new Set();   // JD 全文检索命中的企业键

function statusOf(key) { return tracking[key] || ""; }
function allTags(r) { return [...(r.tags || []), ...(utags[r.key] || [])]; }
function save() {
  localStorage.setItem(LS_KEY, JSON.stringify(tracking));
  localStorage.setItem(LS_TAGS, JSON.stringify(utags));
  updateStats();
}
function setSync(t) { const el = document.getElementById("sync"); if (el) el.textContent = t; }
function postJSON(url, patch) {
  if (!SERVER) return Promise.resolve();
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) })
    .then(r => r.json())
    .then(() => setSync("已自动保存 " + new Date().toLocaleTimeString()))
    .catch(() => setSync("⚠️ 自动保存失败：服务未响应，改动暂存浏览器"));
}

function updateStats() {
  const cnt = {};
  ROWS.forEach(r => { const s = statusOf(r.key); cnt[s] = (cnt[s] || 0) + 1; });
  const parts = STATUSES.filter(s => cnt[s[0]]).map(s => s[1] + " " + cnt[s[0]]);
  document.getElementById("stats").textContent = "共 " + ROWS.length + " 家 ｜ " + parts.join(" ｜ ");
  // 标签 chips（带计数）
  const tagCnt = {};
  ROWS.forEach(r => allTags(r).forEach(t => tagCnt[t] = (tagCnt[t] || 0) + 1));
  document.getElementById("tagFilters").innerHTML =
    "<span style='color:#888'>标签：</span>" + Object.entries(tagCnt).sort((a, b) => b[1] - a[1]).map(([t, n]) =>
    `<button class="chip tag${selectedTags.has(t) ? " on" : ""}" onclick="toggleTag('${esc(t)}')">${esc(t)} ${n}</button>`).join("");
  document.getElementById("statusFilters").innerHTML =
    "<span style='color:#888'>状态：</span>" + STATUSES.map(([v, l]) =>
    `<button class="chip${selectedStatuses.has(v) ? " on" : ""}" onclick="toggleStatus('${v}')">${l}</button>`).join("");
  // base 区域 chips（带计数，固定顺序）
  const rgCnt = {};
  ROWS.forEach(r => (r.regions || []).forEach(g => rgCnt[g] = (rgCnt[g] || 0) + 1));
  document.getElementById("regionFilters").innerHTML =
    "<span style='color:#888'>base：</span>" + REGIONS.filter(g => rgCnt[g]).map(g =>
    `<button class="chip${selectedRegions.has(g) ? " on" : ""}" onclick="toggleRegion('${g}')">${g} ${rgCnt[g]}</button>`).join("");
}

function rowVisible(r) {
  const st = statusOf(r.key);
  if (!selectedStatuses.has(st)) return false;
  if (selectedTags.size && !allTags(r).some(t => selectedTags.has(t))) return false;
  if (selectedRegions.size && !(r.regions || []).some(g => selectedRegions.has(g))) return false;
  if (query) {
    const q = query.toLowerCase();
    const inRow = (r.name + " " + r.note + " " + r.sessions.map(s => s.theme).join(" ") + " " + allTags(r).join(" ")).toLowerCase().includes(q);
    if (!inRow && !jdHitKeys.has(r.key)) return false;
  }
  return true;
}

function rowHtml(r) {
  const st = statusOf(r.key);
  const opts = STATUSES.map(([v, l]) =>
    `<option value="${v}"${v === st ? " selected" : ""}>${l}</option>`).join("");
  const emails = r.emails.length ? esc(r.emails.join("、")) : "—";
  const links = r.urls.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>");
  const tagsHtml = [
    ...(r.tags || []).map(t => `<span class="tag">${esc(t)}</span>`),
    ...(utags[r.key] || []).map(t => `<span class="tag utag" title="点击删除" onclick="removeUserTag('${esc(r.key)}','${esc(t)}')">${esc(t)} ✕</span>`),
  ].join("");
  const sessHtml = r.sessions.map(s =>
    `<div class="sess">📅 ${esc(s.hold || "—")} ｜ <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.theme)}</a>`
    + ` <span style="color:#999">[${esc(s.src)}]</span>`
    + (s.snippet ? `<div class="snip">${esc(s.snippet)}</div>` : "") + `</div>`).join("");
  return `<div class="card${EXCLUDED.has(st) ? " excluded" : ""}" data-key="${esc(r.key)}">
    <select onchange="setStatus(this)">${opts}</select>
    <div class="main">
      <div class="name">${esc(r.name)}${r.n_sessions > 1 ? ` <span style="color:#999;font-size:12px">（共 ${r.n_sessions} 场）</span>` : ""}</div>
      <div class="meta">${r.cities && r.cities.length ? "📍" + esc(r.cities.join(" · ")) + " ｜ " : ""}${esc(r.latest)} ｜ ${esc(r.note)}</div>
      <div>${tagsHtml}<input class="tagadd" placeholder="+回车加标签" onkeydown="addUserTag(event,'${esc(r.key)}')"></div>
      <div class="meta">邮箱：${emails}</div>
      ${links ? `<div>${links}</div>` : ""}
      ${r.sessions[0].snippet ? `<div class="snip">${esc(r.sessions[0].snippet)}</div>` : ""}
      ${r.n_sessions > 1 ? `<details><summary>展开全部 ${r.n_sessions} 场宣讲</summary>${sessHtml}</details>`
                         : sessHtml ? `<details><summary>本场详情</summary>${sessHtml}</details>` : ""}
    </div></div>`;
}

function render() {
  const app = document.getElementById("app");
  app.innerHTML = SECTIONS.map(sec => {
    const rows = ROWS.filter(r => r.section === sec && rowVisible(r));
    if (!rows.length) return "";
    return `<h2>${sec}（${rows.length}）</h2>` + rows.map(rowHtml).join("");
  }).join("");
}

function toggleStatus(v) { selectedStatuses.has(v) ? selectedStatuses.delete(v) : selectedStatuses.add(v); updateStats(); render(); }
function toggleTag(t) { selectedTags.has(t) ? selectedTags.delete(t) : selectedTags.add(t); updateStats(); render(); }
function toggleRegion(g) { selectedRegions.has(g) ? selectedRegions.delete(g) : selectedRegions.add(g); updateStats(); render(); }

function setStatus(sel) {
  const key = sel.closest(".card").dataset.key;
  tracking[key] = sel.value;
  save(); postJSON("/api/tracking", { [key]: sel.value }); render();
}

function addUserTag(ev, key) {
  if (ev.key !== "Enter") return;
  const t = ev.target.value.trim();
  if (!t) return;
  const cur = new Set(utags[key] || []);
  cur.add(t);
  utags[key] = [...cur];
  ev.target.value = "";
  save(); postJSON("/api/tags", { [key]: utags[key] }); render();
}
function removeUserTag(key, t) {
  utags[key] = (utags[key] || []).filter(x => x !== t);
  if (!utags[key].length) delete utags[key];
  save(); postJSON("/api/tags", { [key]: utags[key] || null }); render();
}

// ---- 搜索：本地即时过滤 + 服务端 JD 全文检索（防抖）----
let searchTimer = null;
function onSearch(v) {
  query = v.trim();
  render();
  clearTimeout(searchTimer);
  const hint = document.getElementById("searchHint");
  if (!SERVER || query.length < 2) { jdHitKeys = new Set(); hint.textContent = ""; render(); return; }
  hint.textContent = "检索 JD 全文…";
  searchTimer = setTimeout(() => {
    fetch("/api/search?q=" + encodeURIComponent(query))
      .then(r => r.json())
      .then(d => {
        // 服务端按场次 iid 返回命中；映射回企业键
        const hitIds = new Set(d.results.map(x => x.iid));
        jdHitKeys = new Set(ROWS.filter(r =>
          r.sessions.some(s => hitIds.has(s.iid))
        ).map(r => r.key));
        hint.textContent = "JD 全文命中 " + jdHitKeys.size + " 家";
        render();
      })
      .catch(() => { hint.textContent = "JD 检索不可用"; });
  }, 250);
}

function exportJson() {
  const blob = new Blob([JSON.stringify({ statuses: tracking, tags: utags }, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "tracking_backup.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function importJson(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const d = JSON.parse(reader.result);
      Object.assign(tracking, remapKeys(d.statuses || d));   // 兼容旧的纯状态格式
      Object.assign(utags, remapKeys(d.tags || {}));
      save(); postJSON("/api/tracking", tracking); postJSON("/api/tags", utags);
      render(); alert("导入成功");
    } catch (e) { alert("导入失败：不是合法 JSON"); }
  };
  reader.readAsText(f);
  ev.target.value = "";
}

save(); updateStats(); render();
if (SERVER) {
  setSync("连接服务中…");
  Promise.all([fetch("/api/tracking").then(r => r.json()),
               fetch("/api/tags").then(r => r.json())]).then(([serverT, serverU]) => {
    // 服务器为准；浏览器独有的键补传（迁移旧数据）
    const missT = {}, missU = {};
    for (const [k, v] of Object.entries(lsT)) if (!(k in serverT)) missT[k] = v;
    for (const [k, v] of Object.entries(lsU)) if (!(k in serverU)) missU[k] = v;
    const jobs = [];
    if (Object.keys(missT).length) jobs.push(postJSON("/api/tracking", missT));
    if (Object.keys(missU).length) jobs.push(postJSON("/api/tags", missU));
    tracking = Object.assign({}, lsT, serverT);
    utags = Object.assign({}, lsU, serverU);
    save(); render();
    const n = Object.keys(missT).length + Object.keys(missU).length;
    setSync(n ? "已迁移 " + n + " 条旧数据，自动保存已开启" : "自动保存已开启（改动即时落盘）");
  }).catch(() => setSync("⚠️ 服务不可用，改动只存浏览器"));
} else {
  setSync("file:// 模式：改动只存浏览器，看页面请先运行 track_server.py");
}
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
