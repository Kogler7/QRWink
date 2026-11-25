#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案二接收端端到端测试（不需要真屏幕、不需要摄像头）。

分两步：
  1. 用发送端页面把一份测试文件渲染成 N 张 PNG，回传给本进程；
  2. 用接收端页面，把 getDisplayMedia 换成「一个不断播放这些 PNG 的 canvas 流」，
     点「开始采集」，看它能不能收齐、拼回、CRC 通过。

这样测的是真实的收发两份代码，不是仿写的链路。

用法：python tools/recv2d_test.py [--size 6000] [--density 800]
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
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

# ---- 第一步：渲染帧并回传 ----
RENDER_PROBE = r"""
<script>
(function () {
  var PORT = __PORT__, SIZE = __SIZE__, DENSITY = __DENSITY__, MODE = __CH__;
  function post(o) {
    try { fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) }); } catch (e) {}
  }
  (function () {
    var b = new Uint8Array(SIZE), s = 20261125;
    for (var i = 0; i < SIZE; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; b[i] = s & 0xFF; }
    document.getElementById('density').value = String(DENSITY);
    document.getElementById('mode').value = String(MODE);
    var total = Wink2D.loadBytes(b, 'payload.bin');
    var meta = Wink2D.meta(), cv = document.getElementById('code'), screens = [], texts = [];
    var n = Wink2D.screens();
    for (var p2 = 0; p2 < n; p2++) {
      Wink2D.renderScreen(p2);
      screens.push({ g: p2, png: cv.toDataURL('image/png'), w: cv.width, h: cv.height });
      texts.push(Wink2D.screenFrames(p2).map(function (f) { return Wink2D.frameText(f); }));
    }
    // 第二个文件：长度不同，CRC 和总帧数都不一样
    var b2 = new Uint8Array(SIZE + 1), s2 = 987654321;
    for (var j = 0; j < b2.length; j++) { s2 = (s2 * 1103515245 + 12345) & 0x7fffffff; b2[j] = s2 & 0xFF; }
    var total2 = Wink2D.loadBytes(b2, 'second.bin');
    var meta2 = Wink2D.meta(), screens2 = [];
    var n2 = Wink2D.screens();
    for (var p3 = 0; p3 < n2; p3++) {
      Wink2D.renderScreen(p3);
      screens2.push({ g: p3, png: cv.toDataURL('image/png'), w: cv.width, h: cv.height });
    }
    post({ ok: true, meta: meta, meta2: meta2, screens: screens, screens2: screens2,
           texts: texts, groups: n, slots: n });
  })();
})();
</script>
"""

# ---- 第二步：把 canvas 流伪装成「屏幕采集」 ----
RECV_PROBE = r"""
<script src="qrcode.js"></script>
<script>
(function () {
  var SCREENS = __SCREENS__.concat(__SCREENS2__);   // A 的屏接 B 的屏，循环播
  var TEXTS = __TEXTS__, BAD = __BAD__;   // TEXTS[s] = 该屏各通道的文本
  var BAD_CS = __BADCS__;             // 篡改哪些通道（彩色只改 G；灰度三个都改）
  var HOLD = __HOLD__;                // 每屏停留毫秒数
  var WAIT = __WAIT__;                // 探针最多等多久

  // 按同样规则渲染一个码（每模块 1 像素，含 4 模块静区）
  function qrCanvas(text) {
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
      for (var col = 0; col < n; col++) { if (qr.isDark(r, col)) { x.fillRect(col + pad, r + pad, 1, 1); } }
    }
    return c;
  }

  // 把第 BAD 屏的第 BAD_CH 通道换成一个「格式合法、内容错」的码
  var badQr = null;
  var badIdx = BAD_CS.length === 3 ? 0 : BAD_CS[0];
  if (BAD >= 0 && TEXTS[BAD] && TEXTS[BAD][badIdx]) {
    var txt = TEXTS[BAD][badIdx];
    var last = txt.charAt(txt.length - 1);
    badQr = qrCanvas(txt.slice(0, txt.length - 1) + (last === '0' ? '1' : '0'));
  }

  var cv = document.createElement('canvas');
  var ctx = cv.getContext('2d', { willReadFrequently: true });
  var imgs = [], loaded = 0, idx = 0, badShown = 0;

  function ready() {
    if (loaded < imgs.length) { return; }
    var SW = __SW__ || imgs[0].width, SH = __SH__ || imgs[0].height;
    cv.width = SW; cv.height = SH;
    ctx.imageSmoothingEnabled = false;
    var OX = Math.round((SW - imgs[0].width) / 2), OY = Math.round((SH - imgs[0].height) / 2);
    var timer = setInterval(function () {
      var g = idx % imgs.length;
      ctx.fillStyle = '#e9ecef'; ctx.fillRect(0, 0, SW, SH);
      ctx.drawImage(imgs[g], OX, OY);
      if (badQr && g === BAD && badShown < 2) {
        badShown++;
        var w = cv.width, h = cv.height;
        var d = ctx.getImageData(0, 0, w, h);
        var tmp = document.createElement('canvas');
        tmp.width = w; tmp.height = h;
        var tx = tmp.getContext('2d');
        tx.imageSmoothingEnabled = false;
        tx.fillStyle = '#fff'; tx.fillRect(0, 0, w, h);
        tx.drawImage(badQr, 0, 0, badQr.width, badQr.height, OX, OY, imgs[0].width, imgs[0].height);
        var s = tx.getImageData(0, 0, w, h).data, dd = d.data;
        for (var bi = 0; bi < BAD_CS.length; bi++) {
          for (var i = 0; i < dd.length; i += 4) {
            dd[i + BAD_CS[bi]] = s[i] < 128 ? 0 : 255;
          }
        }
        ctx.putImageData(d, 0, 0);
      }
      idx++;
    }, HOLD);
    var stream = cv.captureStream(15);
    navigator.mediaDevices.getDisplayMedia = function () { return Promise.resolve(stream); };
    setTimeout(function () { document.getElementById('btnStart').click(); }, 120);
  }

  for (var k = 0; k < SCREENS.length; k++) {
    var im = new Image();
    im.onload = function () { loaded++; ready(); };
    im.onerror = function () { loaded++; ready(); window.__IMGERR__ = (window.__IMGERR__ || 0) + 1; };
    im.src = SCREENS[k].png;
    imgs.push(im);
  }

  setTimeout(function () { if (loaded < imgs.length) { window.__READYSLOW__ = loaded + '/' + imgs.length; } ready(); }, 3000);

  var t0 = Date.now();
  // 帧到达时间线：看是「匀速到」还是「一轮一轮到」
  window.__SERIES__ = [];
  (function () {
    var s0 = Date.now();
    var iv = setInterval(function () {
      var s = window.Wink2DRecv ? window.Wink2DRecv.state() : null;
      window.__SERIES__.push([Date.now() - s0, s ? s.got : -1]);
      if (Date.now() - s0 > 6000) { clearInterval(iv); }
    }, 250);
  })();
  var poll = setInterval(function () {
    var done = document.getElementById('done');
    var st = document.getElementById('stat');
    var links = done ? done.querySelectorAll('a').length : 0;
    var finished = links >= __EXPECT__;
    if (finished || Date.now() - t0 > WAIT) {
      clearInterval(poll);
      fetch('http://127.0.0.1:' + __PORT__ + '/done', {
        method: 'POST', mode: 'no-cors',
        body: JSON.stringify({
          ok: true, finished: !!finished,
          doneText: done ? done.textContent.trim() : '',
          hasLink: !!(done && done.querySelector('a')),
          saveName: done && done.querySelector('a') ? done.querySelector('a').getAttribute('download') : '',
          stat: st ? st.textContent : '',
          dbg: window.__DBG || null,
          log: (window.__LOG || []).slice(0, 12),
          state: window.Wink2DRecv ? window.Wink2DRecv.state() : null,
          doneList: (function () {
            var d = document.getElementById('done');
            if (!d) { return null; }
            var as = d.querySelectorAll('a');
            return { n: as.length, names: Array.prototype.map.call(as, function (x) {
              return x.getAttribute('download'); }), text: d.textContent.slice(0, 160) };
          })(),
          series: window.__SERIES__ || null,
          probe: window.__PROBE, errs: window.__ERR__,
          pv: (function () {
            var e = document.getElementById('preview');
            if (!e || !e.width) { return null; }
            var d = null;
            try { d = e.getContext('2d').getImageData(2, 2, 1, 1).data; } catch (err) {}
            var url = '';
            try { url = e.toDataURL('image/png'); } catch (err) {}
            return { w: e.width, h: e.height, px: d ? [d[0], d[1], d[2]] : null, url: url };
          })(),
          imgs: { n: imgs.length, loaded: loaded, err: window.__IMGERR__ || 0,
                  slow: window.__READYSLOW__ || null,
                  wired: typeof document.getElementById('btnStart').onclick },
          elapsedMs: Date.now() - t0
        })
      });
    }
  }, 500);
})();
</script>
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(directory: Path, port: int, on_post) -> ThreadingHTTPServer:
    class H(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                on_post(json.loads(body.decode("utf-8")))
            except Exception as exc:
                on_post({"ok": False, "why": f"bad json {exc}"})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    import functools
    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(directory)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_page(browser: str, url: str, work: Path, tag: str, timeout: int) -> None:
    prof = profile_dir("recv-" + tag)       # 固定目录，跨轮复用（不能每轮新建）
    subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
         "--no-sandbox", "--no-first-run", "--window-size=1280,900",
         f"--user-data-dir={prof}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=6000)
    ap.add_argument("--density", type=int, default=2799)
    ap.add_argument("--ch", type=int, default=3, help="通道数：3=彩色，1=灰度")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--noworker", action="store_true", help="强制同步解码，用来对比")
    ap.add_argument("--screen", default="0x0",
                    help="把码放进这么大的「屏幕」里，模拟整屏共享，例如 1280x800")
    ap.add_argument("--noroI", action="store_true", help="不锁区域，一直整屏扫描")
    ap.add_argument("--shot", default="", help="把接收端预览图存成 PNG，用来眼看")
    ap.add_argument("--expect", type=int, default=1,
                    help="期望收到几个文件（发送端会依次播 --expect 个）")
    ap.add_argument("--hold", type=int, default=260,
                    help="每屏停留多少毫秒（越小越接近「鼠标拉着进度条狂滚」）")
    ap.add_argument("--corrupt", type=int, default=-2,
                    help="把第几屏的 G 通道弄成「内容错但格式对」的坏帧；-1 关闭")
    args = ap.parse_args()
    _sw, _sh = (args.screen.lower().split("x") + ["0", "0"])[:2]
    args.screen_w, args.screen_h = int(_sw), int(_sh)
    if args.corrupt == -2:
        # 灰度模式第 0 屏就是文件名帧，改坏它没意义；彩色模式第 0 屏有数据帧，可以改
        args.corrupt = 0 if args.ch == 3 else 1

    for f in (SENDER, RECEIVER):
        if not f.is_file():
            sys.exit(f"缺少 {f}，先跑 python tools/build_2d.py")
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    work = work_dir("wink-2d-recv-")
    prof = profile_dir("recv-recv")
    port = free_port()
    box: dict = {}
    ev = threading.Event()

    def on_post(o):
        if "screens" in o:
            box.update(o)
            ev.set()
        else:
            box["recv"] = o
            ev.set()

    srv = serve(work, port, on_post)
    try:
        import shutil
        shutil.copy2(QRCODE, work / "qrcode.js")

        # ---------- 第一步：渲染 ----------
        html = SENDER.read_text(encoding="utf-8")
        probe = (RENDER_PROBE.replace("__PORT__", str(port))
                 .replace("__SIZE__", str(args.size))
                 .replace("__DENSITY__", str(args.density))
                 .replace("__HOLD__", str(args.hold))
                 .replace("__CH__", str(args.ch)))
        (work / "render.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")
        run_page(browser, f"http://127.0.0.1:{port}/render.html", work, "render", args.timeout)
        if not ev.wait(args.timeout) or "screens" not in box:
            sys.exit("[x] 渲染阶段没有回传结果")
        meta, screens = box["meta"], box["screens"]
        ev.clear()
        kill_by_profile(profile_dir("recv-render"))

        print(f"负载 {meta['size']:,} B → {meta['total']} 帧 / {box['groups']} 屏"
              f"（{meta['chunk']} 字符/帧 × {meta['channels']} 通道，"
              f"约 {meta['bytesPerScreen'] / 1024:.1f} KB/屏）CRC {meta['crc']:08X}"
              f"　文件名 {meta.get('name')}")

        # ---------- 第二步：接收 ----------
        recv_html = RECEIVER.read_text(encoding="utf-8")
        inject = ("window.__ERR__ = [];"
                  "window.addEventListener('error', function (e) {"
                  " window.__ERR__.push(String(e.message) + ' @' + (e.lineno || 0)); });"
                  "window.__PROBE = { md: typeof navigator.mediaDevices,"
                  " gdm: navigator.mediaDevices ? typeof navigator.mediaDevices.getDisplayMedia : 'no-md',"
                  " cs: typeof HTMLCanvasElement.prototype.captureStream };"
                  "window.__LOG = [];"
                  "window.__SCREENS__ = "
                  + json.dumps([{"g": f["g"], "png": f["png"]} for f in screens]) + ";"
                  + "window.__SCREENS2__ = "
                  + json.dumps([{"g": f["g"], "png": f["png"]} for f in box.get("screens2") or []]) + ";")
        rprobe = (RECV_PROBE.replace("__PORT__", str(port))
                  .replace("__SCREENS2__", "__SCREENS2__")
                  .replace("__TEXTS__", json.dumps(box.get("texts") or []))
                  .replace("__BAD__", str(args.corrupt))
                  .replace("__BADCS__", "[1]" if args.ch == 3 else "[0,1,2]")
                  .replace("__HOLD__", str(args.hold))
                  .replace("__SW__", str(args.screen_w))
                  .replace("__SH__", str(args.screen_h))
                  .replace("__EXPECT__", str(args.expect))
                  .replace("__WAIT__", str(max(3000, min(args.timeout * 1000 - 5000, 45000)))))
        if args.noroI:
            recv_html = recv_html.replace(
                "<body>", "<body>\n<script>window.__NOROI__ = 1;</script>", 1)
        if args.noworker:
            recv_html = recv_html.replace(
                "<body>", "<body>\n<script>window.__NOWORKER__ = 1;</script>", 1)
        body = recv_html.replace("<head>", "<head>\n<script>" + inject + "</script>", 1)
        body = body.replace("</body>", rprobe + "\n</body>")
        (work / "recv.html").write_text(body, encoding="utf-8")
        run_page(browser, f"http://127.0.0.1:{port}/recv.html", work, "recv", args.timeout)

        if not ev.wait(args.timeout):
            sys.exit("[x] 接收阶段没有回传结果")
        r = box.get("recv") or {}
    finally:
        srv.shutdown()
        kill_by_profile(profile_dir("recv-render"))
        kill_by_profile(profile_dir("recv-recv"))

    _s = r.get("state") or {}
    print(f"每帧解码 {_s.get('ms')} ms　·　锁定的区域 {_s.get('roi')}")
    _pv = r.get("pv") or {}
    if args.shot and (_pv.get("url") or "").startswith("data:image"):
        import base64
        Path(args.shot).write_bytes(base64.b64decode(_pv["url"].split(",", 1)[1]))
        print("预览图已存：", args.shot)
    _roi = (_s.get("roi") or None)
    if _pv and _roi:
        _ok = (_pv["w"] == 480
               and abs(_pv["h"] - round(480 * _roi[3] / _roi[2])) <= 2
               and _pv["px"] and _pv["px"][1] > 120 and _pv["px"][0] < 90)
        print("预览：", _pv["w"], "x", _pv["h"], "边缘色", _pv.get("px"),
              "锁定区域", _roi, "→", "只画锁定区域" if _ok else "（未按预期渲染）")
    print("状态栏：", json.dumps(r.get("stat"), ensure_ascii=False))
    _dl = r.get("doneList") or {}
    print("收到的文件：", _dl.get("n"), "个", _dl.get("names"))
    _ser = r.get("series")
    if _ser:
        print("帧到达时间线（每 250ms 的累计帧数）：")
        print("   " + "  ".join(f"{t}ms:{g}" for t, g in _ser))
    print("探针：", json.dumps(r.get("probe"), ensure_ascii=False),
          json.dumps(r.get("imgs"), ensure_ascii=False),
          "errs=", json.dumps(r.get("errs"), ensure_ascii=False)[:300])
    print("日志：", json.dumps(r.get("log"), ensure_ascii=False)[:600])
    print("诊断：", json.dumps(r.get("dbg"), ensure_ascii=False)[:400])
    print("状态：", json.dumps(r.get("state"), ensure_ascii=False)[:300])
    print(f"接收端：finished={r.get('finished')} 用时 {r.get('elapsedMs')}ms"
          f"　保存名={r.get('saveName') or '（无）'}")
    for line in (r.get("stat") or "").splitlines():
        print("   " + line)
    txt = r.get("doneText") or ""
    print(f"完成提示：{txt[:120]}")

    # 页面里的字节数没有千分位，比较前统一去掉逗号
    _dl = r.get("doneList") or {}
    _n = _dl.get("n") or 0
    if args.expect > 1:
        # 连收多个文件：链接数够、两个文件名都在、并且现在还在采（没被停掉）
        names = _dl.get("names") or []
        want = {meta.get("name"), "second.bin"}
        ok = (r.get("finished") and _n >= args.expect and want.issubset(set(names))
              and bool((r.get("state") or {}).get("running")))
        if not ok:
            print(f"[!] 期望收到 {args.expect} 个文件，实际 {_n} 个：{names}"
                  f"　running={(r.get('state') or {}).get('running')}")
        else:
            print(f"连收 {_n} 个文件、都给了保存链接，捕获仍在继续（running=True）")
    else:
        ok = (bool(r.get("finished")) and bool(r.get("hasLink"))
              and str(meta["size"]) in txt.replace(",", ""))
        if r.get("saveName") and r["saveName"] != meta.get("name"):
            print(f"[!] 回传的文件名不对：{r['saveName']} ≠ {meta.get('name')}")
            ok = False
    if args.corrupt >= 0:
        if "坏帧" in (r.get("stat") or ""):
            print(f"坏帧（第 {args.corrupt} 帧被篡改）已当场判废：是")
        else:
            print(f"坏帧（第 {args.corrupt} 帧被篡改）已当场判废：否 —— 说明帧级校验没生效")
            ok = False
    print()
    print("[结论] " + ("通过：接收端收齐全部帧、CRC 校验一致、给出保存链接"
                     if ok else "未通过，见上面输出"))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()