#!/usr/bin/env python3
"""补抓归档场次的 JD 详情：慢速、可断点续传。

archive.jsonl 里的过期场次只含列表字段（主题/时间/地点），本脚本按需补抓
JD 正文到 archive_details.jsonl。设计目标是对上游友好，避免触发风控：

- 每场间隔 5~9 秒随机（约 0.15 次/秒，单日几百次请求，负载远低于正常浏览）
- 每场抓到立即追加落盘，中断/重跑自动跳过已抓的，只补缺失的
- 单点失败重试 3 次（退避），仍失败则跳过并记下，下次运行自动重试
- 全部缺失补完后输出 "done"

用法：
    python3 backfill_archive_details.py            # 慢速补抓全部缺失
    python3 backfill_archive_details.py --limit 20 # 只补前 20 场（试跑）
"""

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path

from collect import (ARCHIVE_DETAIL_PATH, INFO_API, clean_html, load_jsonl_by_id,
                     polite_get)

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
ARCHIVE_PATH = DATA_DIR / "archive.jsonl"

MIN_INTERVAL = 5.0   # 每场最短间隔（秒）
JITTER = 4.0         # 额外随机 0~4 秒


def fetch_one(iid: int) -> dict | None:
    """抓单场详情，失败返回 None（已内部重试）。"""
    try:
        d = polite_get(INFO_API, post=True,
                       data={"action": "bookinginfo", "rid": iid}).json()
        d["ID"] = iid
        d["DescriptionText"] = clean_html(d.get("Description", ""))
        return d
    except Exception as e:
        print(f"  详情 {iid} 抓取失败：{e}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="只补前 N 场（0=全部缺失）")
    args = ap.parse_args()

    archived = load_jsonl_by_id(ARCHIVE_PATH)
    have = load_jsonl_by_id(ARCHIVE_DETAIL_PATH)
    missing = [iid for iid in archived if iid not in have]
    if args.limit:
        missing = missing[:args.limit]
    total = len(missing)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 待补抓 {total} 场"
          f"（已归档 {len(archived)}，已有详情 {len(have)}）", flush=True)
    if not total:
        print("done", flush=True)
        return

    ARCHIVE_DETAIL_PATH.touch(exist_ok=True)
    done = failed = 0
    with ARCHIVE_DETAIL_PATH.open("a", encoding="utf-8") as f:
        for n, iid in enumerate(missing, 1):
            d = fetch_one(iid)
            if d:
                d["archived_at"] = f"{datetime.now():%Y-%m-%d %H:%M}"
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
                f.flush()
                done += 1
            else:
                failed += 1
            if n % 10 == 0 or n == total:
                print(f"  进度 {n}/{total}（成功 {done}，失败 {failed}）",
                      flush=True)
            if n < total:
                time.sleep(MIN_INTERVAL + random.random() * JITTER)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 本轮完成：成功 {done}，"
          f"失败 {failed}（下次运行自动续抓失败的）", flush=True)


if __name__ == "__main__":
    main()
