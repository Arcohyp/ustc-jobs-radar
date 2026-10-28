#!/usr/bin/env python3
"""USTC 就业专场招聘会收集 + 增量更新 + 简历匹配。

数据源是就业网前端自己调用的公开 JSON 接口（非逆向、非绕过），
即浏览器打开列表页时会请求的同一个接口。

合规要点：
- 仅访问公开的列表/详情接口，不登录、不绕过验证码或权限控制
- 串行请求，每次请求间隔 1.5 秒，单次全量更新约 25 次请求，负载极小
- 建议每天运行一次（如早晨），不要高频轮询

数据流：
  抓取列表 -> 与 data/list_store.jsonl 合并（按 ID 去重，记录 first_seen）
            -> 过期场次（举办日期早于今天）归档到 data/archive.jsonl（含 archived_at），
               对应 JD 详情归档到 data/archive_details.jsonl，主存储只留未开始的
            -> 输出 data/upcoming.csv（未开始的全部场次）
            -> 详情正文缓存 data/details.jsonl（覆盖全部未开始场次，增量补抓）
            -> 按 profile.yaml 求职画像匹配：标题四档 + JD 正文兜底，输出 data/matches.md

用法：
  python3 collect.py                # 全量更新（合并去重+过期归档+补详情+匹配）
  python3 collect.py --no-details   # 跳过详情抓取，只按标题匹配（快）
  python3 collect.py --offline      # 不访问网络，用本地存储重新生成报告
  python3 collect.py --keyword 华为  # 只按关键词抓列表（调试用）
"""

import argparse
import csv
import html
import json
import random
import re
import requests
import time
from datetime import date
from pathlib import Path

import yaml


# 省市地名表：norm_key 地域剥离、base 地识别、单位名清洗共用一份
_CITIES = ("北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州",
           "无锡", "合肥", "成都", "武汉", "西安", "长沙", "郑州", "宁波", "厦门",
           "青岛", "济南", "烟台", "常州", "温州", "东莞", "佛山", "珠海", "三亚",
           "大连", "沈阳", "长春", "哈尔滨", "石家庄", "太原", "兰州", "南昌",
           "昆明", "贵阳", "南宁", "福州", "海口", "江苏", "浙江", "安徽", "广东",
           "山东", "四川", "湖南", "湖北", "福建", "陕西", "河南", "河北", "江西",
           "辽宁", "吉林", "黑龙江", "内蒙古", "广西", "云南", "贵州", "海南",
           "甘肃", "山西")

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
STORE_PATH = DATA_DIR / "list_store.jsonl"
DETAIL_PATH = DATA_DIR / "details.jsonl"
ARCHIVE_PATH = DATA_DIR / "archive.jsonl"
ARCHIVE_DETAIL_PATH = DATA_DIR / "archive_details.jsonl"
LIST_API = "https://ustc.ahbys.com/API/Web/index10358.ashx"
INFO_API = "https://job.ustc.edu.cn/Ajax/jywapi.ashx"
PAGE_SIZE = 200  # 接口实测支持

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://job.ustc.edu.cn/Specialrecruitment/list.aspx",
}

LIST_FIELDS = ["ID", "Theme", "HoldDateTxt", "VenuesName", "StatusName", "nPos"]


def load_profile():
    """加载求职画像：优先 profile.yaml（个人私有，不入库），否则 profile.example.yaml"""
    for name in ("profile.yaml", "profile.example.yaml"):
        path = BASE / name
        if path.exists():
            if name.endswith("example.yaml"):
                print("提示：未找到 profile.yaml，正在使用示例画像。"
                      "请 cp profile.example.yaml profile.yaml 后按个人意向修改。")
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("缺少求职画像配置：请基于 profile.example.yaml 创建 profile.yaml")


_PROFILE = load_profile()
TARGET_COMPANIES = _PROFILE.get("target_companies", {})
WORTH_A_LOOK = _PROFILE.get("worth_a_look", {})
DIRECTION_KEYWORDS = _PROFILE.get("direction_keywords", [])
SOE_HINTS = _PROFILE.get("soe_hints", [])
SOE_ALLOWED = _PROFILE.get("soe_allowed", [])
JD_KEYWORDS = _PROFILE.get("jd_keywords", [])
JD_MIN_HITS = 2

_SOE_CORP_SUFFIXES = ("集团有限责任公司", "股份有限公司", "有限责任公司",
                      "有限公司", "总公司")


def load_soe_names():
    """加载国企名录（soe_names.txt，本地私有文件），并自动补短名（去公司后缀）"""
    path = BASE / "soe_names.txt"
    names = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if not name:
                continue
            names.add(name)
            short = name
            for suf in _SOE_CORP_SUFFIXES:
                if short.endswith(suf):
                    short = short[: -len(suf)]
                    break
            if len(short) >= 4:
                names.add(short)
    return names


SOE_NAMES = load_soe_names()


def polite_get(url, params=None, post=False, data=None):
    for attempt in range(3):
        try:
            if post:
                r = requests.post(url, data=data, headers=HEADERS, timeout=30)
            else:
                r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            time.sleep(1.5 + random.random())  # 礼貌延迟：1.5~2.5 秒
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            wait = 5 * (attempt + 1)
            print(f"  请求失败({e})，{wait}s 后重试...")
            time.sleep(wait)


def clean_html(s):
    text = re.sub(r"<br\s*/?>", "\n", s or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()


def parse_hold_date(row):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", row.get("HoldDateTxt", "")
                  or row.get("HoldDate", ""))
    return date(*map(int, m.groups())) if m else None


def fetch_list(keyword=""):
    all_rows, page = [], 1
    while True:
        r = polite_get(LIST_API, params={
            "pagesize": PAGE_SIZE, "pageindex": page,
            "action": "bookinglist", "kind": 13, "keyword": keyword,
        })
        d = r.json()
        rows = d.get("data") or []
        if page == 1:
            print(f"服务端共 {d['RecorderCount']} 条记录，{d['PageCount']} 页")
        all_rows.extend(rows)
        print(f"  已抓取第 {page}/{d['PageCount']} 页（累计 {len(all_rows)} 条）")
        if page >= d["PageCount"] or not rows:
            break
        page += 1
    return all_rows


def load_store():
    if not STORE_PATH.exists():
        return {}
    store = {}
    for line in STORE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            store[row["ID"]] = row
    return store


def load_jsonl_by_id(path):
    """按 ID 读取 jsonl（归档文件用）。"""
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["ID"]] = row
    return rows


def append_archive(path, rows):
    """把行追加到归档文件，按 ID 去重（上游列表若仍返回过期场次，避免重复归档）。"""
    existing = load_jsonl_by_id(path)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            if row["ID"] in existing:
                continue
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def match_row(row):
    """返回 (档位, 命中名, 理由, 是否疑似国企)。
    档位: 'target' 目标企业 / 'worth' 值得一看 / 'direction' 方向相关 / None"""
    title = row.get("Theme", "")
    is_soe = ((any(h in title for h in SOE_HINTS)
               or any(n in title for n in SOE_NAMES))
              and not any(a in title for a in SOE_ALLOWED))
    for name, aliases in TARGET_COMPANIES.items():
        if name in title or any(a in title for a in aliases):
            return "target", name, "目标企业清单", is_soe
    for name, note in WORTH_A_LOOK.items():
        if name in title:
            return "worth", name, note, is_soe
    score = sum(1 for k in DIRECTION_KEYWORDS if k in title)
    if score >= 2:
        return "direction", None, f"标题命中 {score} 个方向词", is_soe
    return None, None, "", is_soe


def match_detail(row, details):
    """标题未命中时两级兜底：
    1) JD 正文命中 >=JD_MIN_HITS 个强关键词 -> 'detail' 档（精确）
    2) 否则 JD 命中 >=JD_MIN_HITS 个方向词 -> 'direction_jd' 宽松档
       （大公司 JD 常是通用简章、无具体技术词，宽松档防漏，代价是噪音偏多）"""
    d = details.get(row["ID"])
    if not d:
        return None, ""
    text = d.get("DescriptionText", "")
    if not text:
        return None, ""
    if JD_KEYWORDS:
        hits = [k for k in JD_KEYWORDS if k in text]
        if len(hits) >= JD_MIN_HITS:
            return "detail", "JD 命中: " + "/".join(hits[:6])
    dir_hits = [k for k in DIRECTION_KEYWORDS if k in text]
    if len(dir_hits) >= JD_MIN_HITS:
        return "direction_jd", f"JD 方向词 {len(dir_hits)} 个: " + "/".join(dir_hits[:6])
    return None, ""


# ---- 组团招聘会内参会单位展开 ----
# 主题里出现这些词才展开（JD 是参会单位表而非单一公司简章）。
# 注意：名单挖掘侧只有在真能展开出单位时才跳过专场卡，
# 所以宁可宽匹配（单个公司主题的 JD 展开不出单位，自然退回专场处理）
FAIR_THEME_RE = re.compile(
    r"组团|双选|洽谈|招聘会|专场活动|人才周|校招平台|引才|招才|"
    r"人才夜市|人才汇|英才汇|宣讲（")

UNIT_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,26}?"
    r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司|"
    r"研究院|研究所|设计院|重点实验室|实验室|创新中心|研发中心|技术中心|工程研究中心|"
    r"大学|学院|中学|小学|医院|学校|银行|分行|集团|中心|分院|工作站|基地|园区|管委会|所|院|站|局))")

# 参会单位表被压成 "单位名 性质 城市 N 下一家单位名" 连写，行号 N 是唯一的
# 行边界：中文字后紧跟 1-2 位数字、且数字后能在短距离内看到下一家单位的
# 后缀锚点时，在数字处切开（数字本身丢弃）
_ROW_IDX_RE = re.compile(
    r"(?<=[\u4e00-\u9fff])\d{1,2}(?=(?:[\u4e00-\u9fff]{2,4})?"
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,22}?"
    r"(?:股份有限公司|有限责任公司|集团有限公司|有限公司|"
    r"研究院|研究所|设计院|大学|学院|中学|小学|医院|学校|银行|分行|"
    r"集团|中心|分院|工作站|基地|园区|管委会|所|院|站|局))")

# 单位名里要剥掉的表头/岗位噪音词：连写文本里单位名前会粘着表头和岗位名，
# 取最后一个噪音词之后的部分作为单位名
_UNIT_NOISE_RE = re.compile(
    r"单位名称|岗位名称|专业要求|招聘岗位|招聘单位|序号|届|"
    r"公司简介|企业简介|企业介绍|公司介绍|"
    r"科技人才|青年人才|研发工程师|算法工程师|工程师|专员|主管|经理|"
    r"教师|医师|博士后|博士|硕士|科研人员|毕业生|岗位|名称|要求|详情|专业")

# 正文散文句的特征词：单位名里出现这些说明锚点匹配到了 JD 介绍文字而非单位
_UNIT_PROSE_MARKERS = ("交易所", "欢迎", "简介", "介绍", "投递", "招聘")

# 单位表"性质"列（国企/民企…）连写时会粘到下一家单位名前
_UNIT_NATURE_RE = re.compile(
    r"^(国有企业|民营企业|外资企业|合资企业|事业单位|科研院所|上市公司|其他)")


_GEO_GUARD = ("大学", "学院", "中学", "学校", "医院", "银行", "研究院", "研究所")


def _strip_geo_column(name: str) -> str:
    """剥单位表"城市列"（省市名连写）。只在像独立列时才剥，且剩余 ≥4 字、
    不落入机构简称；与 norm_key 的剥法保持归并一致，同时不把注册名里的
    城市剥掉（"合肥晶合集成电路"->保留合肥）。"""
    for _ in range(3):
        nxt = name
        for city in _CITIES:
            if not name.startswith(city):
                continue
            rest = name[len(city):]
            if rest.startswith("市") and len(rest) >= 5:
                rest2 = rest[1:]  # "深圳市腾讯"：norm_key 同样剥"深圳市"
            else:
                rest2 = rest
                if not any(rest2.startswith(c) or rest2.startswith(c + "市")
                           for c in _CITIES):
                    continue  # 后面不是地名 -> 大概率是注册名的一部分，不剥
            if len(rest2) >= 4 and not any(g in rest2 for g in _GEO_GUARD):
                nxt = rest2
                break
        if nxt == name:
            return name
        name = nxt
    return name


def _clean_unit_name(raw: str) -> str:
    """剥掉连写文本里粘在单位名前的表头/岗位词、性质列、城市列、展位号等前缀。"""
    name = _UNIT_NOISE_RE.split(raw)[-1]
    # 引号/空格连写的口号尾巴取最后一段（"'拓'路同行 '荆'彩未来 拓荆科技"）
    name = re.split(r"[“”\"'‘’\s]+", name)[-1].strip("（）()、，,· ")
    name = re.sub(r"^[^\u4e00-\u9fff]+", "", name)  # 去掉前导数字/字母/符号（展位号等）
    # 城市列（"国有企业安徽合肥合肥乐凯…"）：剥掉性质词后按"是否像独立列"剥地域——
    # 后面仍紧跟地名（安徽合肥|合肥乐凯）或是"市"字尾模式（深圳市腾讯，norm_key
    # 同样会剥）才剥；"合肥晶合"这类城市属于注册名的不剥，保住展示名完整。
    name = _strip_geo_column(_UNIT_NATURE_RE.sub("", name))
    return name


def is_fair_theme(theme: str) -> bool:
    return bool(FAIR_THEME_RE.search(theme or ""))


def extract_units(text: str) -> list[tuple[str, str]]:
    """从组团招聘会 JD 提取 (单位名, 岗位上下文) 列表。

    JD 里的参会单位表格被抓取时压成了纯文本（单位名+岗位+专业连写无分隔），
    用公司/机构后缀做锚点切分单位，单位名下到下一个单位名之间的文本作为
    岗位上下文，供关键词匹配。去重保序。"""
    if not text:
        return []
    # 先在行号边界切开连写的单位表，再按后缀锚点提取单位
    text = _ROW_IDX_RE.sub("\n", text)
    matches = list(UNIT_SUFFIX_RE.finditer(text))
    units = []
    for i, m in enumerate(matches):
        name = _clean_unit_name(m.group(1))
        if len(name) < 4 or len(name) > 30:
            continue
        # 锚点可能切到 JD 介绍散文（"…在上海证券交易所"、"国仪量子欢迎所…"），拒收
        if any(b in name for b in _UNIT_PROSE_MARKERS):
            continue
        end = (matches[i + 1].start() if i + 1 < len(matches)
               else min(len(text), m.end() + 200))
        ctx = text[m.end():end].split("\n")[0]  # 上下文只取本行，不带下一家单位
        units.append((name, ctx))
    seen, out = set(), []
    for name, ctx in units:
        if name not in seen:
            seen.add(name)
            out.append((name, ctx))
    return out


def match_unit(name: str, ctx: str) -> tuple[str | None, str, bool]:
    """对参会单位打分：公司名单优先，其次岗位上下文里的关键词。"""
    is_soe = ((any(h in name for h in SOE_HINTS)
               or any(n in name for n in SOE_NAMES))
              and not any(a in name for a in SOE_ALLOWED))
    for cname, aliases in TARGET_COMPANIES.items():
        if cname in name or any(a in name for a in aliases):
            return "target", "目标企业清单", is_soe
    for cname, note in WORTH_A_LOOK.items():
        if cname in name:
            return "worth", note, is_soe
    strong = [k for k in JD_KEYWORDS if k in ctx]
    if strong:
        return "detail", "岗位命中: " + "/".join(strong[:4]), is_soe
    dirs = [k for k in DIRECTION_KEYWORDS if k in ctx]
    if len(dirs) >= 2:
        return "direction_jd", f"岗位方向词 {len(dirs)} 个: " + "/".join(dirs[:4]), is_soe
    return None, "", is_soe


def load_details():
    cache = {}
    if DETAIL_PATH.exists():
        for line in DETAIL_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                cache[d["ID"]] = d
    return cache


def save_details(cache):
    with DETAIL_PATH.open("w", encoding="utf-8") as f:
        for d in cache.values():
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def fetch_missing_details(ids, cache, offline):
    missing = [i for i in ids if i not in cache]
    if offline or not missing:
        return
    print(f"需补抓 {len(missing)} 场详情（约 {len(missing) * 2 // 60 + 1} 分钟）")
    for n, iid in enumerate(missing, 1):
        try:
            d = polite_get(INFO_API, post=True,
                           data={"action": "bookinginfo", "rid": iid}).json()
            d["ID"] = iid
            d["DescriptionText"] = clean_html(d.get("Description", ""))
            cache[iid] = d
        except Exception as e:
            print(f"  详情 {iid} 失败：{e}")
        if n % 10 == 0:
            print(f"  详情进度 {n}/{len(missing)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-details", action="store_true",
                    help="不抓详情正文（只用标题匹配，速度快）")
    ap.add_argument("--keyword", default="", help="关键词过滤（调试用）")
    ap.add_argument("--offline", action="store_true",
                    help="不访问网络，只用本地存储重新生成报告（调匹配规则用）")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    today = date.today()
    if args.offline:
        rows = list(load_store().values())
        print(f"离线模式：从本地存储读取 {len(rows)} 条")
        new_count = 0
    else:
        rows = fetch_list(args.keyword)
    now = time.strftime("%Y-%m-%d %H:%M")

    # ---- 增量合并 ----
    store = load_store()
    if not args.offline:
        is_new = {}
        for row in rows:
            iid = row["ID"]
            is_new[iid] = iid not in store
            if iid not in store:
                row["first_seen"] = now
            else:
                row["first_seen"] = store[iid].get("first_seen", now)
            store[iid] = row
        new_count = sum(is_new.values())
        print(f"本次新收录 {new_count} 场（历史累计 {len(store)} 场）")
    else:
        is_new = {r["ID"]: False for r in rows}

    # ---- 过期归档：举办日期早于今天的移入 archive，主存储只留未开始的 ----
    expired = [r for r in store.values()
               if (d := parse_hold_date(r)) is None or d < today]
    upcoming = [r for r in store.values()
                if (d := parse_hold_date(r)) is not None and d >= today]
    upcoming.sort(key=lambda r: (parse_hold_date(r), r.get("nPos") or 0))
    for r in expired:
        del store[r["ID"]]
    for r in expired:
        r["archived_at"] = now
    append_archive(ARCHIVE_PATH, expired)
    print(f"已归档过期/无日期 {len(expired)} 场（累计归档 {len(load_jsonl_by_id(ARCHIVE_PATH))} 场），"
          f"保留未开始 {len(upcoming)} 场")

    # ---- 写主存储与 upcoming 表 ----
    with STORE_PATH.open("w", encoding="utf-8") as f:
        for r in store.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    csv_path = DATA_DIR / "upcoming.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LIST_FIELDS + ["first_seen"],
                           extrasaction="ignore")
        w.writeheader()
        for r in upcoming:
            w.writerow({k: clean_html(str(r.get(k, ""))) for k in LIST_FIELDS + ["first_seen"]})
    print(f"已保存：{csv_path}")

    # ---- 详情缓存：覆盖全部未开始场次，过期归档，补抓缺失 ----
    details = load_details()
    detail_rows = []
    for iid in [i for i in details if i not in store]:
        d = details.pop(iid)
        d["archived_at"] = now
        detail_rows.append(d)
    append_archive(ARCHIVE_DETAIL_PATH, detail_rows)
    if detail_rows:
        print(f"详情归档 {len(detail_rows)} 条，详情缓存剩 {len(details)} 条")
    if not args.no_details:
        fetch_missing_details([r["ID"] for r in upcoming], details, args.offline)
    save_details(details)

    # ---- 匹配：标题优先，标题未命中再查 JD 正文 ----
    matched = []
    for r in upcoming:
        tier, name, note, is_soe = match_row(r)
        if not tier:
            tier, note = match_detail(r, details)
        if tier:
            matched.append((tier, name, note, is_soe, r))

    # ---- 组团招聘会：展开参会单位，按岗位上下文匹配 ----
    fair_hits = []
    for r in upcoming:
        if not is_fair_theme(r.get("Theme", "")):
            continue
        d = details.get(r["ID"], {})
        for name, ctx in extract_units(d.get("DescriptionText", "")):
            tier, note, is_soe = match_unit(name, ctx)
            if tier:
                fair_hits.append((r, name, note, is_soe, ctx))

    md = [f"# 匹配报告（{now}，今日 {today}）\n",
          f"未开始场次 {len(upcoming)}，其中匹配 {len(matched)} 场。"
          f"（另有 {len(fair_hits)} 家团招参会单位命中，见文末专节）\n"]
    groups = [("## 🎯 目标企业（投递清单内）", "target", False),
              ("## ⚠️ 匹配但疑似国企/央企属性", None, True),
              ("## 🔎 值得一看（方向高度相关）", "worth", False),
              ("## 📄 JD 正文命中（标题未命中）", "detail", False),
              ("## 🔍 JD 方向相关（宽松命中）", "direction_jd", False),
              ("## 🔍 方向相关（兜底关键词）", "direction", False)]
    for title, tier, soe_filter in groups:
        if soe_filter:
            items = [m for m in matched if m[3]]
        else:
            items = [m for m in matched if m[0] == tier and not m[3]]
        if not items:
            continue
        md.append(f"\n{title}\n")
        md.append("| 日期 | 宣讲会 | 为什么相关 | 地点 | 链接 | 新 |")
        md.append("|---|---|---|---|---|---|")
        for _, name, note, _, r in items:
            d = parse_hold_date(r)
            url = f"https://job.ustc.edu.cn/Specialrecruitment/info.aspx?itemid={r['ID']}"
            mark = " ✅" if is_new.get(r["ID"]) else ""
            cell = lambda s: clean_html(str(s or "")).replace("|", "｜")
            md.append(f"| {d} {cell(r.get('TimeSlotText',''))} | "
                      f"{cell(r.get('Theme',''))} | {cell(note)} | "
                      f"{cell(r.get('VenuesName',''))} | {url} |{mark} |")
    if fair_hits:
        fair_hits.sort(key=lambda x: parse_hold_date(x[0]) or date.min)
        md.append("\n## 🏢 组团招聘会内匹配的参会单位\n")
        md.append("| 日期 | 参会单位 | 为什么相关 | 地点 | 链接 | 新 |")
        md.append("|---|---|---|---|---|---|")
        for r, name, note, is_soe, ctx in fair_hits:
            d = parse_hold_date(r)
            url = f"https://job.ustc.edu.cn/Specialrecruitment/info.aspx?itemid={r['ID']}"
            mark = " ✅" if is_new.get(r["ID"]) else ""
            snip = clean_html(ctx)[:60].replace("|", "｜")
            note_cell = f"{'⚠️国企 ' if is_soe else ''}{note}｜{snip}" if snip else note
            md.append(f"| {d} {cell(r.get('TimeSlotText',''))} | "
                      f"{cell(name)} | {cell(note_cell)} | "
                      f"{cell(r.get('VenuesName',''))} | {url} |{mark} |")

    report = DATA_DIR / "matches.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"已保存：{report}（匹配 {len(matched)} 场）")


if __name__ == "__main__":
    main()
