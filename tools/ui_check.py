#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发送端交互自检：测速 → 退出测速，必须能干净地回到文件视图。

检查项：
  1. 载入文件后能渲染第一帧；
  2. 点「测速」进入测速，状态栏变成测速信息，按钮变成「退出测速」；
  3. 再点一次退出，状态栏恢复成文件信息、画面回到第一帧、「开始」可用；
  4. 测速中途点「开始」，也能退出测速并直接开始发文件。

用法：python tools/ui_check.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _browser import kill_by_profile, profile_dir, work_dir

ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "sender" / "send.html"
BROWSERS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]

PROBE = r"""
<script>
(function () {
  var PORT = __PORT__, log = [], out = {};
  function post(o) {
    try { fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) }); } catch (e) {}
  }
  function snap(tag) {
    log.push({ tag: tag,
               status: document.getElementById('status').textContent,
               test: document.getElementById('btnTest').textContent,
               play: document.getElementById('btnPlay').textContent,
               playDisabled: document.getElementById('btnPlay').disabled,
               width: document.getElementById('code').width });
  }
  function step(fns, i) {
    if (i >= fns.length) { out.log = log; out.ok = true; post(out); return; }
    fns[i]();
    setTimeout(function () { step(fns, i + 1); }, 1200);
  }
  // 先走真实的上传路径（input 的 change 事件），这是用户实际的入口
  function uploadThenRun() {
    var b = new Uint8Array(24000), s = 29;
    for (var k = 0; k < b.length; k++) { s = (s * 1103515245 + 12345) & 0x7fffffff; b[k] = s & 0xFF; }
    try {
      var dt = new DataTransfer();
      dt.items.add(new File([b], 'payload.bin', { type: 'application/octet-stream' }));
      var inp = document.getElementById('file');
      inp.files = dt.files;
      inp.dispatchEvent(new Event('change'));
    } catch (e) {
      out.ok = false; out.err = 'input 上传失败：' + String(e); post(out); return;
    }
    setTimeout(function () {
      snap('uploaded');
      out.uploadedWarn = document.getElementById('warn').textContent;
      out.qrcodeOk = (typeof window.__QRCODE__ === 'function');
      out.uploadedFrames = Wink2D.meta().total;
      runTests();
    }, 900);
  }

  function runTests() {
    try {
      var b = new Uint8Array(24000), s = 13;
      for (var k = 0; k < b.length; k++) { s = (s * 1103515245 + 12345) & 0x7fffffff; b[k] = s & 0xFF; }
      Wink2D.loadBytes(b, 'x.bin');
      snap('loaded');
      var codeW = document.getElementById('code').width;

      step([
        function () { document.getElementById('btnTest').click(); snap('test-on'); },
        function () { snap('test-running'); },
        function () {
          out.testFrameW = document.getElementById('code').width;
          document.getElementById('btnTest').click();
          snap('test-off');
        },
        function () {
          out.restoredW = document.getElementById('code').width;
          out.fileFrameW = codeW;
          // 测速中途点「开始」也要能退出
          document.getElementById('btnTest').click();
        },
        function () {
          snap('test-on-again');
          document.getElementById('btnPlay').click();
        },
        function () { snap('after-start'); }
      ], 0);
    } catch (e) {
      out.ok = false; out.err = String(e && (e.stack || e.message || e));
      out.log = log; post(out);
    }
  }

  setTimeout(uploadThenRun, 400);
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _has_frames(status: str) -> bool:
    """状态栏里有没有「多少屏 / 多少帧」这类信息（用词随着 UI 改过）。"""
    return ("帧" in status) or ("屏" in status)


def main() -> None:
    amd = "--amd" in sys.argv[1:]
    for f in (SENDER,):
        if not f.is_file():
            sys.exit(f"缺少 {f}")
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    work = work_dir("wink-ui-")
    prof = profile_dir("ui")
    port = free_port()
    box: dict = {}
    ev = threading.Event()

    class H(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                box.update(json.loads(body.decode("utf-8")))
            except Exception as exc:
                box["fatal"] = str(exc)
            self.send_response(204)
            self.end_headers()
            ev.set()

        def log_message(self, *a):
            pass

    import functools
    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        probe = PROBE.replace("__PORT__", str(port))
        html = SENDER.read_text(encoding="utf-8")
        if amd:
            # 模拟「浏览器里有 AMD loader」：UMD 库会优先走 define([], factory)，
            # 导出被丢掉，__QRCODE__ 变成空对象 —— 这就是用户看到的 not a function。
            stub = ("<script>window.__amdStub = 1;"
                    "window.define = function (a, b) {};window.define.amd = true;</script>\n")
            html = html.replace("<head>", "<head>\n" + stub, 1)
        (work / "u.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
                          "--no-sandbox", "--no-first-run", "--window-size=1280,900",
                          f"--user-data-dir={prof}", f"http://127.0.0.1:{port}/u.html"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not ev.wait(90):
            sys.exit("[x] 90s 内没有回报")
    finally:
        srv.shutdown()
        kill_by_profile(profile_dir("ui"))

    if not box.get("ok"):
        print("[x] 页面报错：" + str(box.get("err") or box.get("fatal")))
        sys.exit(2)

    fails = []
    for row in box.get("log", []):
        print(f"[{row['tag']:>14}] 测速键={row['test']:<6} 开始键={row['play']:<4}"
              f" disabled={str(row['playDisabled']):<5} 画布={row['width']}px  状态={row['status'][:46]}")

    states = {r["tag"]: r for r in box.get("log", [])}

    def need(cond, msg):
        if not cond:
            fails.append(msg)

    need(_has_frames(states.get("uploaded", {}).get("status", "")), "走 input 上传后状态栏没有帧信息")
    need(not (box.get("uploadedWarn") or "").strip(), "走 input 上传后出现了告警：" + str(box.get("uploadedWarn")))
    need(box.get("qrcodeOk") is True, "编码库没有导出成函数（__QRCODE__ 不是函数）")
    need(box.get("uploadedFrames", 0) > 0, "走 input 上传后没有切出帧")
    need(_has_frames(states.get("loaded", {}).get("status", "")), "载入后状态栏没有帧信息")
    need(states.get("test-on", {}).get("test") == "退出测速", "进入测速后按钮没变成「退出测速」")
    need(states.get("test-running", {}).get("status", "").startswith("测速"), "测速中状态栏没有测速信息")
    need(states.get("test-off", {}).get("test") == "测速", "退出后按钮没变回「测速」")
    need(_has_frames(states.get("test-off", {}).get("status", "")), "退出后状态栏没有恢复文件信息")
    need(not states.get("test-off", {}).get("playDisabled"), "退出测速后「开始」仍被禁用")
    need(box.get("restoredW") == box.get("fileFrameW"), "退出测速后画面没有回到文件第一帧")
    need(states.get("after-start", {}).get("test") == "测速", "测速中点「开始」没退出测速")
    need(states.get("after-start", {}).get("play") == "暂停", "测速中点「开始」没有开始发文件")

    fails = [f for f in fails if f]
    print()
    if amd:
        print("[场景] 预置了 AMD loader（window.define.amd === true）")
    print("[结论] " + ("通过：测速可进可退，两种情况都能回到文件模式" if not fails
                     else "未通过：" + "；".join(fails)))
    sys.exit(0 if not fails else 2)


if __name__ == "__main__":
    main()