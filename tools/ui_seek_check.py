#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发送端交互检查：默认隐藏、点击暂停、滚轮换屏、进度条拖拽跳转。

跑的是一个真实的无头浏览器，事件是真的发进去的。
用法：python tools/ui_seek_check.py
"""

from __future__ import annotations

import functools
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
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

PROBE = r"""
<script>
(function () {
  var PORT = __PORT__, SIZE = __SIZE__;
  function post(o) {
    try { fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) }); } catch (e) {}
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  (async function () {
    if (!window.Wink2D) { return post({ ok: false, why: '没有 Wink2D' }); }
    var out = { ok: true, steps: [] };
    function step(o) { out.steps.push(o); }

    var b = new Uint8Array(SIZE), s = 424242;
    for (var i = 0; i < SIZE; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; b[i] = s & 0xFF; }
    var total = Wink2D.loadBytes(b, 'seek-test.bin');
    var n = Wink2D.screens();
    out.screens = n;

    var cv = document.getElementById('code');
    var bar = document.getElementById('bar');
    var track = document.getElementById('track');

    // 1) 控制条默认是隐藏的（载入后短暂浮出，等它自己消失）
    await sleep(2400);
    step({ name: '默认隐藏', ok: !bar.classList.contains('show'), got: bar.className });

    // 2) 点一下画面 → 开始；再点 → 暂停
    cv.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await sleep(120);
    var afterFirst = Wink2D.playing();
    cv.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await sleep(120);
    step({ name: '点击 开始/暂停', ok: afterFirst === true && Wink2D.playing() === false,
           got: '第一次=' + afterFirst + ' 第二次=' + Wink2D.playing() });

    // 3) 滚轮向下 → 前进一屏；向上 → 退回
    Wink2D.seekScreen(3);
    await sleep(60);
    var base = Wink2D.currentScreen();
    window.dispatchEvent(new WheelEvent('wheel', { deltaY: 120, cancelable: true }));
    await sleep(60);
    var down = Wink2D.currentScreen();
    window.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, cancelable: true }));
    await sleep(60);
    var up = Wink2D.currentScreen();
    step({ name: '滚轮前后换屏', ok: down === (base + 1) % n && up === base,
           got: base + '→' + down + '→' + up });

    // 4) 进度条点 75% → 跳到约 75% 那屏
    var r = track.getBoundingClientRect();
    var x = r.left + r.width * 0.75, y = r.top + r.height / 2;
    track.dispatchEvent(new PointerEvent('pointerdown',
      { bubbles: true, clientX: x, clientY: y, pointerId: 1 }));
    track.dispatchEvent(new PointerEvent('pointerup',
      { bubbles: true, clientX: x, clientY: y, pointerId: 1 }));
    await sleep(60);
    var want = Math.floor(0.75 * n), got = Wink2D.currentScreen();
    step({ name: '进度条跳转', ok: got === want, got: got + '（期望 ' + want + '）' });

    // 5) 跳过去画的就是那一屏（和直接渲染比对）
    Wink2D.seekScreen(want);
    var a = cv.toDataURL('image/png');
    Wink2D.renderScreen(want);
    var b2 = cv.toDataURL('image/png');
    step({ name: '跳转后画面正确', ok: a === b2, got: a === b2 ? '一致' : '不一致' });

    // 6) 拖拽到 90% 连续跳，最终停在 90% 附近
    var x2 = r.left + r.width * 0.9;
    track.dispatchEvent(new PointerEvent('pointerdown',
      { bubbles: true, clientX: r.left + r.width * 0.2, clientY: y, pointerId: 2 }));
    track.dispatchEvent(new PointerEvent('pointermove',
      { bubbles: true, clientX: x2, clientY: y, pointerId: 2 }));
    track.dispatchEvent(new PointerEvent('pointerup',
      { bubbles: true, clientX: x2, clientY: y, pointerId: 2 }));
    await sleep(60);
    var want2 = Math.floor(0.9 * n), got2 = Wink2D.currentScreen();
    step({ name: '进度条拖动', ok: got2 === want2, got: got2 + '（期望 ' + want2 + '）' });

    // 7) 右下角显示的是「实测」速率：每 200ms 滚一屏，应报约 5 屏/秒
    await sleep(3200);                       // 清掉前面几次滚轮，别混进统计窗口
    window.dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
    for (var w = 0; w < 5; w++) {
      window.dispatchEvent(new WheelEvent('wheel', { deltaY: 120, cancelable: true }));
      await sleep(200);
    }
    await sleep(80);
    var stxt = document.getElementById('status').textContent;
    var mm = stxt.match(/([0-9.]+) 屏\/秒/);
    var rate = mm ? parseFloat(mm[1]) : -1;
    step({ name: '实测速率', ok: rate >= 3.5 && rate <= 6.5, got: stxt.slice(0, 70) });

    // 8) 鼠标一动控制条浮出
    window.dispatchEvent(new MouseEvent('mousemove', { bubbles: true }));
    await sleep(40);
    step({ name: '鼠标移动后浮出', ok: bar.classList.contains('show'), got: bar.className });

    post(out);
  })().catch(function (e) { post({ ok: false, why: String(e && e.stack || e).slice(0, 400) }); });
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")
    work = work_dir("wink-ui-")
    prof = profile_dir("ui-seek")
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

    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        html = SENDER.read_text(encoding="utf-8")
        probe = PROBE.replace("__PORT__", str(port)).replace("__SIZE__", "30000")
        (work / "ui.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", "--window-size=1280,900",
             f"--user-data-dir={prof}", f"http://127.0.0.1:{port}/ui.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        got = ev.wait(90)
        kill_by_profile(work)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_by_profile(work)
    finally:
        srv.shutdown()

    if not got:
        sys.exit("[x] 超时")
    if not box.get("ok"):
        sys.exit("[x] " + str(box.get("why")))

    print(f"{box['screens']} 屏，逐项检查：")
    bad = 0
    for s in box["steps"]:
        mark = "✔" if s["ok"] else "✘"
        print(f"  {mark} {s['name']:<14} {s['got']}")
        if not s["ok"]:
            bad += 1
    print()
    print("[结论] " + ("全部通过" if not bad else f"{bad} 项不通过"))
    sys.exit(0 if not bad else 2)


if __name__ == "__main__":
    main()