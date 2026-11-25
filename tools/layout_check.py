# -*- coding: utf-8 -*-
"""检查发送端二维码是否被底部控制条盖住：量画布和控件的矩形，算重叠面积。

在几个常见窗口尺寸下各验一次（都是「控制条正显示」的最坏情况）。
"""
import functools
import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _browser import kill_by_profile, profile_dir, work_dir

ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "sender" / "send.html"
BROWSER = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROBE = r"""
<script>
(function () {
  function post(o) {
    fetch('http://127.0.0.1:__PORT__/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) });
  }
  function run() {
    var out = [];
    try {
      var b = new Uint8Array(20000);
      Wink2D.loadBytes(b, 'x.bin');
      document.getElementById('bar').classList.add('show');   // 最坏情况：控制条显示着
      var cv = document.getElementById('code').getBoundingClientRect();
      var bar = document.getElementById('bar').getBoundingClientRect();
      var hint = document.getElementById('hint').getBoundingClientRect();
      function overlap(a, b) {
        var w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        var h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        return (w > 0 && h > 0) ? Math.round(w * h) : 0;
      }
      out.push({
        w: window.innerWidth, h: window.innerHeight,
        code: [Math.round(cv.left), Math.round(cv.top), Math.round(cv.width), Math.round(cv.height)],
        barTop: Math.round(bar.top),
        overlapBar: overlap(cv, bar),
        gapBottom: Math.round(bar.top - cv.bottom),
        gapLeft: Math.round(cv.left), gapRight: Math.round(window.innerWidth - cv.right),
        hintOverlap: overlap(cv, hint)
      });
    } catch (e) { out.push({ err: String(e && e.message || e) }); }
    post({ ok: true, cases: out });
  }
  window.addEventListener('load', function () { setTimeout(run, 400); });
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_case(browser: str, w: int, h: int, timeout: int = 40) -> dict:
    work = work_dir("wink-layout-")
    prof = profile_dir("layout")
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
        probe = PROBE.replace("__PORT__", str(port))
        (work / "t.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", f"--window-size={w},{h}",
             f"--user-data-dir={prof}", f"http://127.0.0.1:{port}/t.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        got = ev.wait(timeout)
        kill_by_profile(work)
    finally:
        srv.shutdown()
    if not got:
        raise SystemExit(f"[x] {w}x{h} 超时")
    if not box.get("ok"):
        raise SystemExit(f"[x] {w}x{h} 没有回传")
    return box["cases"][0]


def main() -> None:
    sizes = [(1280, 800), (1536, 864), (1920, 1080), (1024, 768), (1366, 768), (2560, 1440)]
    bad = 0
    print(f"{'窗口':>12} | {'二维码区域 (x,y,w,h)':<26} | 底部余量 | 左右余量 | 与控制条重叠")
    print("-" * 96)
    for w, h in sizes:
        c = run_case(BROWSER, w, h)
        if "err" in c:
            print(f"{w}x{h}: {c['err']}")
            bad += 1
            continue
        ov = c["overlapBar"]
        hint_ov = c["hintOverlap"]
        flag = "" if ov == 0 and hint_ov == 0 else "  ← 有遮挡"
        if ov or hint_ov:
            bad += 1
        print(f"{c['w']:>5}x{c['h']:<6} | {str(c['code']):<26} | {c['gapBottom']:>6} px | "
              f"{c['gapLeft']:>3}/{c['gapRight']:<4} | {ov:>6} px²{flag}")
    print()
    print("[结论] 二维码没有被控制条或提示文字遮住" if not bad else f"[x] {bad} 个尺寸有遮挡")


if __name__ == "__main__":
    main()