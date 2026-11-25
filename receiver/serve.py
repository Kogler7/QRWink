#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接收端本地静态服务。

为什么需要它：接收端要调用 `getUserMedia`（摄像头）/ `getDisplayMedia`（录屏），
这两个 API 硬性要求 secure context。浏览器把 `http://127.0.0.1` 和 `http://localhost`
视作 potentially trustworthy origin —— 也就是说**本机访问走裸 HTTP 就是 secure
context，摄像头可用，完全不需要证书、不需要 HTTPS**。

接收端是单文件 `recv.html`（不含 wasm），本目录里只需要它。

用法：
    python serve.py                # 只绑 127.0.0.1，本机接收（推荐）
    python serve.py --port 9000
    python serve.py --no-browser
    python serve.py --lan          # 绑 0.0.0.0，供同局域网另一台机器访问（见警告）
"""

from __future__ import annotations

import argparse
import http.server
import socket
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # 换版本后别让浏览器拿旧缓存
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stdout.write("  %s\n" % (fmt % args))


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def free_port(preferred: int) -> int:
    for port in [preferred, 0]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise SystemExit("找不到可用端口")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--lan", action="store_true", help="绑 0.0.0.0，允许局域网其它机器访问")
    args = ap.parse_args()

    if not (ROOT / "recv.html").is_file():
        sys.exit("缺少 recv.html：先跑 python tools/build_2d.py 生成")

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    port = args.port if args.lan else free_port(args.port)
    url = f"http://127.0.0.1:{port}/recv.html"

    httpd = Server((host, port), Handler)
    print("=" * 66)
    print(f"接收端已启动：{url}")
    if args.lan:
        ip = "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
        except OSError:
            pass
        print(f"局域网地址：  http://{ip}:{port}/recv.html")
        print("！警告：局域网 IP 是明文 HTTP，不是 secure context，")
        print("        手机/另一台机器的浏览器会拒绝摄像头。只当发送端时可用。")
    else:
        print("本机访问 = secure context，摄像头/录屏可直接用，无需证书。")
    print("Ctrl+C 停止")
    print("=" * 66)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()