#!/usr/bin/env python3
"""apply_mining 交互页面的本地服务：静态托管 + 投递状态即时落盘。

用法：
    python3 track_server.py            # 前台运行，浏览器打开 http://localhost:8123
    nohup python3 track_server.py &    # 后台挂起，断了自己重启

- GET  /                    -> data/apply_mining.html（由 mine_apply.py 生成）
- GET  /api/tracking        -> data/tracking.json（不存在则返回 {}）
- POST /api/tracking        -> 请求体 {"公司名": "状态", ...}，合并进 tracking.json
                               （以服务器文件为准；页面只补传服务器缺失的键）

页面在 http:// 下打开时自动走本服务：改动状态即时写入 data/tracking.json，
无需任何手动导出；file:// 下打开则退化为 localStorage + 手动备份。
"""

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
PAGE = DATA_DIR / "apply_mining.html"
TRACKING_PATH = DATA_DIR / "tracking.json"
PORT = 8123

# 快速连点时多个 POST 可能并发到达，合并+写盘必须串行，否则读-改-写会互相覆盖
_write_lock = threading.Lock()


def load_tracking() -> dict:
    if not TRACKING_PATH.exists():
        return {}
    try:
        return json.loads(TRACKING_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


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

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/tracking":
            self._send_json(load_tracking())
        elif path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send_json({"error": f"{PAGE} 不存在，请先运行 mine_apply.py"}, 404)
                return
            self.path = "/apply_mining.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path != "/api/tracking":
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
        with _write_lock:
            data = load_tracking()
            data.update(patch)
            tmp = TRACKING_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(TRACKING_PATH)
            count = len(data)
        self._send_json({"ok": True, "count": count})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[tracking] {args[0] if args else fmt}", flush=True)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"服务已启动：http://localhost:{PORT}  （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
