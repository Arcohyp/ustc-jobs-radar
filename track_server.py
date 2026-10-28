#!/usr/bin/env python3
"""apply_mining 交互页面的本地服务：静态托管 + 投递状态即时落盘。

用法：
    python3 track_server.py            # 前台运行，浏览器打开 http://localhost:8123
    nohup python3 track_server.py &    # 后台挂起，断了自己重启

- GET  /                    -> data/apply_mining.html（由 mine_apply.py 生成）
- GET  /api/tracking        -> data/tracking.json（不存在则返回 {}）
- POST /api/tracking        -> 请求体 {"公司名": "状态", ...}，合并进 tracking.json
                               （以服务器文件为准；页面只补传服务器缺失的键）
- GET  /api/tags            -> data/tags.json：{"企业键": ["用户标签", ...]}
- POST /api/tags            -> {"企业键": ["标签", ...] 或 null}；null 删除该键
- GET  /api/search?q=关键词  -> 在全部 JD 正文（details + archive_details）里做
                               子串检索，返回 [{"iid", "theme", "hold"}, ...]
                               （索引按文件 mtime+size 缓存，数据更新自动重建）

页面在 http:// 下打开时自动走本服务：改动状态/标签即时写入 data/ 下的 JSON，
无需任何手动导出；file:// 下打开则退化为 localStorage + 手动备份。
"""

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
PAGE = DATA_DIR / "apply_mining.html"
TRACKING_PATH = DATA_DIR / "tracking.json"
TAGS_PATH = DATA_DIR / "tags.json"
PORT = 8123

# IP 白名单：仅本机回环 + 指定局域网主机可访问，其余连接 403。
# 本机当前为 .110（eno2np1，用户原话 190 经核实是另一台设备），另一台主机多网卡 61/100；
# 路由器 DHCP 若换 IP 需同步改这里。
ALLOWED_CLIENTS = frozenset({
    "127.0.0.1", "::1", "::ffff:127.0.0.1",
    "192.168.31.110",
    "192.168.31.61",
    "192.168.31.100",
})

# 快速连点时多个 POST 可能并发到达，合并+写盘必须串行，否则读-改-写会互相覆盖
_write_lock = threading.Lock()


def load_tracking() -> dict:
    return _load_json(TRACKING_PATH)


def load_tags() -> dict:
    return _load_json(TAGS_PATH)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _merge_json(path: Path, patch: dict, allow_null: bool = False) -> int:
    """合并补丁进 JSON 文件（绝对值语义：以最后写入为准），原子写盘，返回键总数。"""
    with _write_lock:
        data = _load_json(path)
        for k, v in patch.items():
            if v is None and allow_null:
                data.pop(k, None)
            else:
                data[k] = v
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
        return len(data)


# ---- JD 全文检索索引：按文件签名（mtime+size）缓存，数据更新自动重建 ----
_search_cache: dict = {}


def _read_jsonl_ids(path: Path) -> dict:
    """jsonl -> {ID: row}（容忍坏行）。"""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ID" in r:
            out[r["ID"]] = r
    return out


def _search_index() -> list:
    detail_files = [DATA_DIR / "details.jsonl", DATA_DIR / "archive_details.jsonl"]
    session_files = [DATA_DIR / "list_store.jsonl", DATA_DIR / "archive.jsonl"]
    sig = tuple((p.name, p.stat().st_mtime, p.stat().st_size)
                for p in detail_files + session_files if p.exists())
    if _search_cache.get("sig") == sig:
        return _search_cache["rows"]

    sessions = {}
    for p in session_files:
        sessions.update(_read_jsonl_ids(p))
    rows = []
    for p in detail_files:
        for iid, d in _read_jsonl_ids(p).items():
            text = d.get("DescriptionText", "")
            if not text:
                continue
            s = sessions.get(iid, {})
            rows.append({
                "iid": iid,
                "theme": s.get("Theme", ""),
                "hold": s.get("HoldDateTxt", "").replace("&nbsp;", " ").strip(),
                "text": text.lower(),
            })
    _search_cache["sig"] = sig
    _search_cache["rows"] = rows
    return rows


def search_jd(query: str, limit: int = 200) -> list:
    q = query.strip().lower()
    if not q:
        return []
    hits, seen = [], set()
    for r in _search_index():
        if r["iid"] in seen or q not in r["text"]:
            continue
        seen.add(r["iid"])
        hits.append({"iid": r["iid"], "theme": r["theme"], "hold": r["hold"]})
        if len(hits) >= limit:
            break
    return hits


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DATA_DIR), **kwargs)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_allowed(self) -> bool:
        return self.client_address[0] in ALLOWED_CLIENTS

    def do_GET(self) -> None:
        if not self._client_allowed():
            self._send_json({"error": "forbidden"}, 403)
            return
        path = self.path.split("?")[0]
        if path == "/api/tracking":
            self._send_json(load_tracking())
        elif path == "/api/tags":
            self._send_json(load_tags())
        elif path == "/api/search":
            q = unquote(parse_qs(urlparse(self.path).query).get("q", [""])[0])
            self._send_json({"results": search_jd(q)})
        elif path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send_json({"error": f"{PAGE} 不存在，请先运行 mine_apply.py"}, 404)
                return
            self.path = "/apply_mining.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        if not self._client_allowed():
            self._send_json({"error": "forbidden"}, 403)
            return
        path = self.path.split("?")[0]
        if path not in ("/api/tracking", "/api/tags"):
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            patch = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(patch, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "请求体须为 JSON 对象"}, 400)
            return
        if path == "/api/tracking":
            count = _merge_json(TRACKING_PATH, patch)
        else:
            # 标签的值是数组；null 表示删除该企业的全部用户标签
            clean = {k: v for k, v in patch.items()
                     if v is None or isinstance(v, list)}
            count = _merge_json(TAGS_PATH, clean, allow_null=True)
        self._send_json({"ok": True, "count": count})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[tracking] {args[0] if args else fmt}", flush=True)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"服务已启动：http://localhost:{PORT}  （仅白名单 IP 可访问，Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
