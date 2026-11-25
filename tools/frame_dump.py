#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把发送端生成的每一帧文本导出，在本机按接收端同样的规则拼回去，校验 CRC。

这样能直接判定问题在「编码端」还是「采集/解码端」：
发送端自己的帧拼不回原文件，就是编码端的问题，跟屏幕、压缩、采样都无关。

用法：python tools/frame_dump.py --size 185000
"""

from __future__ import annotations

import functools
import json
import socket
import subprocess
import sys
import tempfile
import threading
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _browser import kill_by_profile, profile_dir, work_dir

ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "sender" / "send.html"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

ALU = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$%*+-./:"

PROBE = r"""
<script>
(function () {
  var PORT = __PORT__, SIZE = __SIZE__, DENSITY = __DENSITY__;
  function post(o) {
    fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) });
  }
  try {
    document.getElementById('density').value = String(DENSITY);
    var b = new Uint8Array(SIZE), s = 1234567;
    for (var i = 0; i < SIZE; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; b[i] = s & 0xFF; }
    var total = Wink2D.loadBytes(b, 'dump.bin');
    var meta = Wink2D.meta();
    var frames = [];
    for (var f = 1; f <= total; f++) { frames.push(Wink2D.frameText(f)); }
    post({ ok: true, meta: meta, frames: frames, metaText: Wink2D.frameText(0) });
  } catch (e) { post({ ok: false, why: String(e && e.stack || e).slice(0, 500) }); }
})();
</script>
"""


def b44dec(s: str) -> bytes:
    out = bytearray()
    i, n = 0, len(s)
    while i < n:
        c0 = ALU.index(s[i])
        c1 = ALU.index(s[i + 1]) if i + 1 < n else None
        c2 = ALU.index(s[i + 2]) if i + 2 < n else None
        if c2 is None:
            v = c0 * 44 + (c1 or 0)
            if v < 256:
                out.append(v)
            else:
                out.extend((v // 256, v % 256))
            break
        v = c0 * 1936 + c1 * 44 + c2      # 大端：高位在前
        out.append(v // 256)
        out.append(v % 256)
        i += 3
    return bytes(out)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=185000)
    ap.add_argument("--density", type=int, default=2799)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    work = work_dir("wink-dump-")
    prof = profile_dir("dump")
    port = free_port()
    box: dict = {}
    ev = threading.Event()

    class H(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            box.update(json.loads(body.decode("utf-8")))
            self.send_response(204)
            self.end_headers()
            ev.set()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        html = SENDER.read_text(encoding="utf-8")
        probe = (PROBE.replace("__PORT__", str(port))
                 .replace("__SIZE__", str(args.size))
                 .replace("__DENSITY__", str(args.density)))
        (work / "d.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", f"--user-data-dir={prof}",
             f"http://127.0.0.1:{port}/d.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        got = ev.wait(args.timeout)
        kill_by_profile(work)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_by_profile(work)
    finally:
        srv.shutdown()

    if not got:
        sys.exit("[x] 导出超时")
    if not box.get("ok"):
        sys.exit("[x] " + str(box.get("why")))

    meta = box["meta"]
    frames = box["frames"]
    print(f"文件 {meta['size']:,} B　·　{meta['total']} 帧　·　每帧 {meta['chunk']} 字符"
          f"　·　期望 CRC {meta['crc']:08X}")

    payloads = [t[33:] for t in frames]
    joined = "".join(payloads)
    bytes_ = b44dec(joined)[: meta["size"]]
    got_crc = zlib.crc32(bytes_) & 0xFFFFFFFF
    print(f"拼回 {len(bytes_):,} B　·　实际 CRC {got_crc:08X}")
    print(f"每帧载荷长度一致：{len(set(len(p) for p in payloads)) == 1}（{len(payloads[0])} 字符）")

    if len(bytes_) != meta["size"]:
        print(f"[x] 长度不对：{len(bytes_)} ≠ {meta['size']}")
    if got_crc != meta["crc"]:
        # 找出第一个不一致的字节，看是不是某个帧错位
        print("[x] CRC 不一致")
        i = next((k for k in range(min(len(bytes_), meta["size"])) if bytes_[k] != 0xAA), None)
        for f, p in enumerate(payloads):
            if len(p) != meta["chunk"]:
                print(f"  第 {f} 帧长度 {len(p)} ≠ {meta['chunk']}")
        sys.exit(2)
    print("[结论] 发送端自己的帧能完整拼回，编码端没问题")


if __name__ == "__main__":
    main()