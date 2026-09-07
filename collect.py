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
            -> 剔除举办日期早于今天的场次
            -> 输出 data/upcoming.csv（未开始的全部场次）
            -> 按 resume/ 下的求职画像做关键词匹配，输出 data/matches.md

用法：
  python3 collect.py                # 全量更新（默认即增量：合并去重+剔过期+匹配）
  python3 collect.py --details      # 额外抓取匹配场次的详情正文（数量少，几分钟）
  python3 collect.py --keyword 华为  # 只按关键词抓列表（调试用）
"""

import argparse
import csv
import html
import json
import random
import re
import time
from datetime import date
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
STORE_PATH = DATA_DIR / "list_store.jsonl"
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


def match_row(row):
    """返回 (档位, 命中名, 理由, 是否疑似国企)。
    档位: 'target' 目标企业 / 'worth' 值得一看 / 'direction' 方向相关 / None"""
    title = row.get("Theme", "")
    is_soe = (any(h in title for h in SOE_HINTS)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--details", action="store_true",
                    help="抓取匹配场次的详情正文（几分钟）")
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

    # ---- 剔除过期：只保留举办日期 >= 今天的 ----
    expired = [r for r in store.values()
               if (d := parse_hold_date(r)) is None or d < today]
    upcoming = [r for r in store.values()
                if (d := parse_hold_date(r)) is not None and d >= today]
    upcoming.sort(key=lambda r: (parse_hold_date(r), r.get("nPos") or 0))
    for r in expired:
        del store[r["ID"]]
    print(f"已剔除过期/无日期 {len(expired)} 场，保留未开始 {len(upcoming)} 场")

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

    # ---- 匹配 ----
    matched = []
    for r in upcoming:
        tier, name, note, is_soe = match_row(r)
        if tier:
            matched.append((tier, name, note, is_soe, r))

    md = [f"# 匹配报告（{now}，今日 {today}）\n",
          f"未开始场次 {len(upcoming)}，其中匹配 {len(matched)} 场。\n"]
    groups = [("## 🎯 目标企业（投递清单内）", "target", False),
              ("## ⚠️ 匹配但疑似国企/央企属性", None, True),
              ("## 🔎 值得一看（方向高度相关）", "worth", False),
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
            md.append(f"| {d} {clean_html(r.get('TimeSlotText',''))} | "
                      f"{clean_html(r.get('Theme',''))} | {note} | "
                      f"{clean_html(r.get('VenuesName',''))} | {url} |{mark} |")
    report = DATA_DIR / "matches.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"已保存：{report}（匹配 {len(matched)} 场）")

    # ---- 可选：抓匹配场次的详情 ----
    if args.details and matched:
        detail_path = DATA_DIR / "details.jsonl"
        done = set()
        if detail_path.exists():
            done = {json.loads(l)["ID"] for l in
                    detail_path.read_text(encoding="utf-8").splitlines() if l.strip()}
        with detail_path.open("a", encoding="utf-8") as f:
            for _, _, _, _, r in matched:
                iid = r["ID"]
                if iid in done:
                    continue
                if args.offline:
                    print("  离线模式跳过详情抓取")
                    break
                try:
                    d = polite_get(INFO_API, post=True,
                                   data={"action": "bookinginfo", "rid": iid}).json()
                    d["ID"] = iid
                    d["DescriptionText"] = clean_html(d.get("Description", ""))
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"  详情：{clean_html(d.get('Theme',''))[:30]}")
                except Exception as e:
                    print(f"  详情 {iid} 失败：{e}")
        print(f"详情已保存：{detail_path}")


if __name__ == "__main__":
    main()
