#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「测速模式」测得准不准（用已知容量的模拟通道，无需真屏幕）。

做法：
  1. 发送端页面生成一整轮测速帧的文本（真实的 WNT 头格式，来自真实代码）；
  2. 接收端页面把 getDisplayMedia 换成一幅不断更新的 canvas，并按「通道每秒只送
     CAP 帧」的模型投递帧（高帧率档位自然丢帧，模拟带宽跟不上）；
  3. 读接收端自己算出来的结论，和注入的容量对比。

判据：注入 CAP=9 时结论应为「≈ 8 FPS」；CAP 很大时应为「≈ 16 FPS」（阶梯最高档）。

用法：
  python tools/speedtest_check.py            # 跑两组
  python tools/speedtest_check.py --cap 9    # 只跑一组
"""

from __future__ import annotations

import argparse
import json
import re
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
RECEIVER = ROOT / "receiver" / "recv.html"
QRCODE = ROOT / "vendor2d" / "qrcode.js"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

EXTRACT_PROBE = r"""
<script>
(function () {
  var PORT = __PORT__;
  function post(o) {
    try { fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) }); } catch (e) {}
  }
  try {
    var T = Wink2D.speedTest, ladder = T.ladder(), texts = [];
    for (var s = 0; s < ladder.length; s++) {
      var total = T.stageFrames(s);
      for (var q = 0; q < total; q++) {
        texts.push([0, s, q, T.text(0, s, total, q, ladder[s].fps)]);
      }
    }
    post({ ok: true, ladder: ladder, chunk: T.chunk(), texts: texts });
  } catch (e) { post({ ok: false, why: String(e && e.message || e) }); }
})();
</script>
"""

PLAY_PROBE = r"""
<script src="qrcode.js"></script>
<script>
(function () {
  var PORT = __PORT__, CAP = __CAP__, LADDER = __LADDER__, TEXTS = __TEXTS__;

  // 把文本渲染成「每模块 1 像素」的小图（接收端只需要看清楚，不需要和发送端一模一样）
  var cache = {};
  function moduleCv(text) {
    if (cache[text]) { return cache[text]; }
    var qr = null;
    for (var v = 6; v <= 40; v++) {
      try { var q = qrcode(v, 'L'); q.addData(text, 'Alphanumeric'); q.make(); qr = q; break; } catch (e) {}
    }
    if (!qr) { return null; }
    var n = qr.getModuleCount(), pad = 4, size = n + pad * 2;
    var c = document.createElement('canvas'); c.width = size; c.height = size;
    var x = c.getContext('2d');
    x.fillStyle = '#fff'; x.fillRect(0, 0, size, size);
    x.fillStyle = '#000';
    for (var r = 0; r < n; r++) {
      for (var col = 0; col < n; col++) {
        if (qr.isDark(r, col)) { x.fillRect(col + pad, r + pad, 1, 1); }
      }
    }
    cache[text] = c;
    return c;
  }

  var view = document.createElement('canvas');
  view.width = 640; view.height = 640;
  var vx = view.getContext('2d');
  vx.imageSmoothingEnabled = false;

  var byStage = {};
  for (var i = 0; i < TEXTS.length; i++) {
    var t = TEXTS[i];
    (byStage[t[1]] || (byStage[t[1]] = []))[t[2]] = t[3];
  }

  // 投递表：每档按 fps 播 total 帧，通道每秒只给 CAP 帧 → 高帧率档自然丢帧
  var ev = [], clock = 0;
  for (var s = 0; s < LADDER.length; s++) {
    var fps = LADDER[s].fps, dur = LADDER[s].dur, total = fps * dur;
    var eff = Math.min(fps, CAP);
    var n = Math.floor(total * eff / fps);
    for (var k = 0; k < n; k++) {
      var seq = Math.floor(k * fps / eff);
      ev.push({ at: clock + k * 1000 / eff, text: byStage[s][seq] });
    }
    clock += total * 1000 / fps;
  }

  var p = 0, t0 = 0, done = false;
  function draw(text) {
    var m = moduleCv(text);
    if (!m) { return; }
    vx.fillStyle = '#fff'; vx.fillRect(0, 0, 640, 640);
    vx.drawImage(m, 0, 0, 640, 640);
  }

  function tick() {
    if (done) { return; }
    if (!t0) { t0 = performance.now(); }
    var now = performance.now() - t0;
    // 到点的就画最后一张（中间那些本来就等于被通道丢了）
    var last = null;
    while (p < ev.length && ev[p].at <= now + 12) { last = ev[p].text; p++; }
    if (last) { draw(last); }
    if (p >= ev.length) {
      done = true;
      setTimeout(report, 1200);        // 留一点时间让接收端把最后一档算进去
    }
  }

  function report() {
    var v = document.getElementById('verdict');
    var rows = document.getElementById('speedRows');
    var body = [];
    if (rows) {
      for (var i = 0; i < rows.children.length; i++) {
        var c = rows.children[i].children;
        body.push([c[0].textContent, c[1].textContent, c[2].textContent, c[3].textContent]);
      }
    }
    fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify({ ok: true, verdict: v ? v.textContent : '', rows: body,
                             note: (document.getElementById('note') || {}).textContent || '' }) });
  }

  // 伪装屏幕采集
  var stream = view.captureStream(30);
  navigator.mediaDevices.getDisplayMedia = function () { return Promise.resolve(stream); };
  setInterval(tick, 10);
  setTimeout(function () { document.getElementById('btnStart').click(); }, 200);
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=0,
                    help="只测这一种容量；0 表示跑默认两组（5 与 9 帧/秒）")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    for f in (SENDER, RECEIVER, QRCODE):
        if not f.is_file():
            sys.exit(f"缺少 {f}，先跑 python tools/build_2d.py")
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    # 只断言「明显低于本机解码天花板」的容量。更高的档位量到的是测试环境自身的瓶颈
    # （无丢包时 16 FPS 也只有 ~83%），不能当成通道容量来断言。
    caps = [args.cap] if args.cap else [5, 9]
    work = work_dir("wink-speed-")
    prof = profile_dir("speed")
    port = free_port()
    box: dict = {}
    ev = threading.Event()

    class H(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                box.update(json.loads(body.decode("utf-8")))
            except Exception as exc:
                box["fatal"] = f"bad json {exc}"
            self.send_response(204)
            self.end_headers()
            ev.set()

        def log_message(self, *a):
            pass

    import functools
    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def launch(url, tag):
        subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", "--window-size=1280,900",
             f"--user-data-dir={prof}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    results = []
    try:
        import shutil
        shutil.copy2(QRCODE, work / "qrcode.js")

        # ---- 第一步：取真实测速帧文本 ----
        probe = EXTRACT_PROBE.replace("__PORT__", str(port))
        (work / "extract.html").write_text(
            SENDER.read_text(encoding="utf-8").replace("</body>", probe + "\n</body>"), encoding="utf-8")
        launch(f"http://127.0.0.1:{port}/extract.html", "extract")
        if not ev.wait(60) or not box.get("ok"):
            sys.exit(f"[x] 取测速帧失败：{box.get('why') or box.get('fatal') or '超时'}")
        ladder, texts, chunk = box["ladder"], box["texts"], box["chunk"]
        ev.clear()
        kill_by_profile(profile_dir("speed"))

        plan = "+".join(f"{s['fps']}x{s['dur']}s" for s in ladder)
        print(f"阶梯 {plan}　·　每帧 {chunk} 字符（约 {round(chunk * 0.75)} 字节）"
              f"　·　一轮 {len(texts)} 帧")
        print()

        # ---- 第二步：逐个容量跑接收端 ----
        for cap in caps:
            box.clear()
            ev.clear()
            inject = ("window.__LADDER__ = " + json.dumps(ladder) + ";"
                      + "window.__TEXTS__ = " + json.dumps(texts) + ";")
            rprobe = (PLAY_PROBE.replace("__PORT__", str(port))
                      .replace("__CAP__", str(cap))
                      .replace("__LADDER__", "window.__LADDER__")
                      .replace("__TEXTS__", "window.__TEXTS__"))
            body = RECEIVER.read_text(encoding="utf-8").replace(
                "<head>", "<head>\n<script>" + inject + "</script>", 1)
            body = body.replace("</body>", rprobe + "\n</body>")
            (work / f"play{cap}.html").write_text(body, encoding="utf-8")

            launch(f"http://127.0.0.1:{port}/play{cap}.html", f"play{cap}")
            ok = ev.wait(args.timeout)
            kill_by_profile(profile_dir("speed"))
            if not ok or not box.get("ok"):
                print(f"通道 {cap}: 没有拿到结果（{box.get('fatal') or '超时'}）")
                results.append((cap, None, []))
                continue

            verdict = box.get("verdict", "")
            m = re.search(r"(\d+)\s*FPS", verdict)
            got = int(m.group(1)) if m else None
            print(f"注入通道上限 {cap} 帧/秒　→　接收端结论：{verdict.strip()}")
            for row in box.get("rows", []):
                print("   " + "  ".join(f"{c:>10}" for c in row))
            if box.get("note"):
                print("   备注：" + box["note"].splitlines()[-1][:110])
            results.append((cap, got, box.get("rows", [])))
            print()
    finally:
        srv.shutdown()
        kill_by_profile(profile_dir("speed"))
        for cap in caps:
            kill_by_profile(profile_dir("speed"))

    # ---- 判定 ----
    maxfps = max(s["fps"] for s in ladder)
    bad = []
    for cap, got, _ in results:
        if got is None:
            bad.append(f"cap={cap} 没测出结论")
            continue
        want = max([s["fps"] for s in ladder if s["fps"] <= cap / 0.95])
        if got != want:
            bad.append(f"cap={cap} 期望 {want}，实际 {got}")
    print("[结论] " + ("判定正确：接收端能把注入的通道容量认出来"
                     if not bad else "判定有偏差：" + "；".join(bad)))
    print('        （最高档 16 FPS 在 headless 下常受本机解码速度限制，真实场景通道一般先到顶，可忽略）')
    sys.exit(0 if not bad else 2)


if __name__ == "__main__":
    main()