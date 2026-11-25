# -*- coding: utf-8 -*-
"""量发送端「改了帧率之后实际切屏有多快」。

直接用发送端自己的 API 观察 currentScreen 的变化，不掺接收端，
所以能区分「发送端没变速」和「接收端跟不上」。
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
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // 在 win 毫秒里数 currentScreen 变了多少次
  function measure(win) {
    return new Promise(function (resolve) {
      var last = Wink2D.currentScreen(), n = 0, t0 = performance.now();
      var iv = setInterval(function () {
        var c = Wink2D.currentScreen();
        if (c !== last) { n++; last = c; }
        if (performance.now() - t0 >= win) { clearInterval(iv); resolve(n / (win / 1000)); }
      }, 15);
    });
  }

  function setFps(v) {
    var e = document.getElementById('fps');
    e.value = String(v);
    e.dispatchEvent(new Event('change'));
  }

  async function run() {
    var out = { ok: true, sizes: [], rates: [] };
    try {
      if (__NW__) { window.__NOWORKER__ = true; }
      var b = new Uint8Array(__SIZE__);
      for (var i = 0; i < b.length; i++) { b[i] = (i * 37) & 0xFF; }
      Wink2D.loadBytes(b, 'r.bin');
      out.screens = Wink2D.screens();
      out.px = document.getElementById('code').width;      // 画布边长
      out.pool = Wink2D.pool ? Wink2D.pool() : null;
      // 等后台把整份文件编完，量「并行编码」的实际耗时
      var te0 = performance.now();
      while (Wink2D.pool && Wink2D.pool().total > 0
             && Wink2D.pool().done < Wink2D.pool().total
             && performance.now() - te0 < 120000) {
        await sleep(100);
      }
      out.encodeMs = Math.round(performance.now() - te0);
      out.poolAfter = Wink2D.pool ? Wink2D.pool() : null;
      // 单独量「画一屏」要多久（含 3 个通道的编码 + 缩放绘制）
      var tA = performance.now();
      for (var q = 0; q < 10; q++) { Wink2D.renderScreen(q % Wink2D.screens()); }
      out.drawMs = +((performance.now() - tA) / 10).toFixed(1);
      // 再量一次「同样的屏画第二遍」（编码有缓存，应该更快）
      var tB = performance.now();
      for (var q2 = 0; q2 < 10; q2++) { Wink2D.renderScreen(q2 % Wink2D.screens()); }
      out.drawMs2 = +((performance.now() - tB) / 10).toFixed(1);
      out.meta = Wink2D.meta();
      document.getElementById('fps').value = '3';
      Wink2D.play();
      await sleep(600);                       // 先跑起来
      var rates = out.rates;
      for (var k = 0; k < 3; k++) {
        var fps = [3, 6, 10][k];
        setFps(fps);
        await sleep(400);                     // 等变速生效
        rates.push({ set: fps, got: +(await measure(2500)).toFixed(2) });
      }
      Wink2D.pause ? Wink2D.pause() : null;
    } catch (e) { out = { ok: false, why: String(e && e.stack || e).slice(0, 400) }; }
    post(out);
  }
  window.addEventListener('load', function () { setTimeout(run, 500); });
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--size", type=int, default=60000)
    _ap.add_argument("--window", default="1280,800")
    _ap.add_argument("--noworker", action="store_true")
    _args = _ap.parse_args()
    work = work_dir("wink-rate-")
    prof = profile_dir("rate")
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
                 .replace("__SIZE__", str(_args.size))
                 .replace("__NW__", "true" if _args.noworker else "false"))
        (work / "r.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        subprocess.Popen(
            [BROWSER, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", f"--window-size={_args.window}",
             f"--user-data-dir={prof}", f"http://127.0.0.1:{port}/r.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        got = ev.wait(90)
        kill_by_profile(work)
    finally:
        srv.shutdown()

    if not got:
        sys.exit("[x] 超时")
    if not box.get("ok"):
        sys.exit("[x] " + str(box.get("why")))

    meta = box["meta"]
    print(f"{meta['screens']} 屏　·　每屏 {meta['bytesPerScreen']:,} B　·　画布 {box.get('px')} px")
    print(f"画一屏 {box.get('drawMs')} ms（首轮）　·　{box.get('drawMs2')} ms（缓存后）")
    _p = box.get("poolAfter") or box.get("pool") or {}
    print(f"编码线程池：{_p.get('n')} 个（ok={_p.get('ok')}）"
          f"　·　编完 {_p.get('done')}/{_p.get('total')} 帧，用时 {box.get('encodeMs')} ms")
    print(f"{'设定 FPS':>8} | {'实测 屏/秒':>10} | {'KB/s':>8} | 说明")
    bad = 0
    for r in box["rates"]:
        kbs = r["got"] * meta["bytesPerScreen"] / 1024
        note = ""
        if r["got"] < r["set"] * 0.75:
            note = "跟不上设定"
            bad += 1
        print(f"{r['set']:>8} | {r['got']:>10} | {kbs:>8.1f} | {note}")
    print()
    if bad:
        print("[x] 发送端达不到设定帧率 —— 瓶颈在发送端")
    else:
        print("[结论] 发送端能跑到设定帧率；接收端跟不上就是接收端的事")


if __name__ == "__main__":
    main()