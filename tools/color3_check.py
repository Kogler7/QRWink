#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三通道（RGB 各装一个码）能不能走通？—— 直接量，不猜。

做法：把三个二维码分别画进 R / G / B 三个通道，合成一张彩色图；
      用 canvas 的 JPEG 编码模拟远程桌面的 H.264（4:2:0 色度子采样），
      再把每个通道单独拆出来转灰度，交给 jsQR 解码。

关键判据是**色度子采样**：JPEG/H.264 的亮度是全分辨率，色度只有一半。
一个模块如果只有 3~4 像素，色度上就只剩 1~2 个采样点，三种颜色会糊在一起。

用法：python tools/color3_check.py
"""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _browser import kill_by_profile, profile_dir, work_dir

ROOT = Path(__file__).resolve().parents[1]
QRCODE = ROOT / "vendor2d" / "qrcode.js"
JSQR = ROOT / "vendor2d" / "jsQR.js"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

PROBE = r"""
<script src="qrcode.js"></script>
<script src="jsQR.js"></script>
<script>
(function () {
  var PORT = __PORT__;
  function post(o) {
    try { fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
      body: JSON.stringify(o) }); } catch (e) {}
  }

  function text(len, seed) {
    var A = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', s = seed, out = '';
    for (var i = 0; i < len; i++) {
      s = (s * 1103515245 + 12345) & 0x7fffffff;
      out += A.charAt(s % 36);
    }
    return out;
  }

  // 每模块 1 像素的位图（含 4 模块静区）
  function bits(txt) {
    var qr = null;
    for (var v = 6; v <= 40; v++) {
      try { var q = qrcode(v, 'L'); q.addData(txt, 'Alphanumeric'); q.make(); qr = q; break; } catch (e) {}
    }
    if (!qr) { return null; }
    var n = qr.getModuleCount(), pad = 4, size = n + pad * 2;
    var m = new Uint8Array(size * size);
    for (var y = 0; y < n; y++) {
      for (var x = 0; x < n; x++) {
        m[(y + pad) * size + (x + pad)] = qr.isDark(y, x) ? 1 : 0;
      }
    }
    return { m: m, size: size, n: n };
  }

  // 三个位图合成一张彩色图（R/G/B 各自一个通道），按整数倍放大
  function compose(bitsArr, scale) {
    var size = bitsArr[0].size, px = size * scale;
    var cv = document.createElement('canvas');
    cv.width = px; cv.height = px;
    var ctx = cv.getContext('2d');
    var img = ctx.createImageData(px, px), d = img.data;
    for (var y = 0; y < px; y++) {
      var sy = Math.floor(y / scale);
      for (var x = 0; x < px; x++) {
        var sx = Math.floor(x / scale), si = sy * size + sx, o = (y * px + x) * 4;
        for (var c = 0; c < 3; c++) {
          d[o + c] = bitsArr[c].m[si] ? 0 : 255;
        }
        d[o + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return cv;
  }

  // 取出一个通道当灰度图
  function channelGray(src, c) {
    var w = src.width, h = src.height;
    var cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    var img = ctx.createImageData(w, h), d = img.data;
    for (var i = 0; i < src.data.length; i += 4) {
      var v = src.data[i + c];
      d[i] = d[i + 1] = d[i + 2] = v; d[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return cv;
  }

  function decodeCanvas(cv) {
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    var d = ctx.getImageData(0, 0, cv.width, cv.height);
    var r = jsQR(d.data, cv.width, cv.height, { inversionAttempts: 'dontInvert' });
    return r ? r.data : null;
  }

  function jpegRoundTrip(cv, q) {
    return new Promise(function (resolve) {
      var im = new Image();
      im.onload = function () {
        var c2 = document.createElement('canvas');
        c2.width = cv.width; c2.height = cv.height;
        c2.getContext('2d').drawImage(im, 0, 0);
        var d = c2.getContext('2d', { willReadFrequently: true })
                  .getImageData(0, 0, cv.width, cv.height);
        resolve(d);
      };
      im.onerror = function () { resolve(null); };
      im.src = cv.toDataURL('image/jpeg', q);
    });
  }

  (async function () {
    var out = { ok: true, rows: [] };
    var LEN = 1400;
    var texts = [text(LEN, 11), text(LEN, 22), text(LEN, 33)];
    var b = [bits(texts[0]), bits(texts[1]), bits(texts[2])];
    if (!b[0] || !b[1] || !b[2]) { return post({ ok: false, why: '编码失败' }); }
    out.n = b[0].n;
    out.size = b[0].size;

    var scales = [8, 6, 4, 3];
    var qs = ['lossless', 0.9, 0.65, 0.45, 0.25, 0.15];

    for (var si = 0; si < scales.length; si++) {
      var cv = compose(b, scales[si]);
      var row = { scale: scales[si], px: cv.width, q: {} };
      for (var qi = 0; qi < qs.length; qi++) {
        var label = String(qs[qi]);
        var px;
        if (qs[qi] === 'lossless') {
          var dctx = cv.getContext('2d', { willReadFrequently: true });
          px = dctx.getImageData(0, 0, cv.width, cv.height);
        } else {
          px = await jpegRoundTrip(cv, qs[qi]);
        }
        if (!px) { row.q[label] = 'noimg'; continue; }
        var res = [];
        for (var c = 0; c < 3; c++) {
          var g = channelGray(px, c);
          var got = decodeCanvas(g);
          res.push(got === texts[c] ? 'ok' : (got === null ? 'miss' : 'wrong'));
        }
        row.q[label] = res.join('/');
      }
      out.rows.push(row);
    }
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    work = work_dir("wink-color-")
    prof = profile_dir("color")
    port = free_port()
    box: dict = {}
    ev = threading.Event()

    class H(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                box.update(json.loads(body.decode("utf-8")))
            except Exception as exc:
                box["fatal"] = f"bad json: {exc}"
            self.send_response(204)
            self.end_headers()
            ev.set()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        shutil.copy2(QRCODE, work / "qrcode.js")
        shutil.copy2(JSQR, work / "jsQR.js")
        probe = PROBE.replace("__PORT__", str(port))
        (work / "test.html").write_text(
            "<!DOCTYPE html><meta charset='utf-8'><body style='margin:0'>"
            + probe + "</body>", encoding="utf-8")
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", "--window-size=1400,1000",
             f"--user-data-dir={prof}",
             f"http://127.0.0.1:{port}/test.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        got = ev.wait(args.timeout)
        kill_by_profile(work)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_by_profile(work)
    finally:
        srv.shutdown()

    if not got:
        sys.exit("[x] 超时，页面没有回报结果")
    if not box.get("ok"):
        sys.exit("[x] 探针失败：" + str(box.get("why")))

    print(f"三个码各 {1400} 字符（v{box['n']}，{box['size']}×{box['size']} 模块）"
          f"分别画在 R/G/B 上，合成一张彩色图")
    print()
    head = f"  {'模块像素':>8} {'画布':>6}  " + "".join(
        f"{q:>12}" for q in ["无损", "0.9", "0.65", "0.45", "0.25", "0.15"])
    print(head)
    for row in box["rows"]:
        cells = []
        for k in ["lossless", "0.9", "0.65", "0.45", "0.25", "0.15"]:
            v = row["q"].get(k, "-")
            cells.append(f"{v:>12}")
        print(f"  {row['scale']:>8} {row['px']:>6}  " + "".join(cells))
    print()
    print("  每格是 R/G/B 三个码各自的解码结果（ok/miss/wrong）")
    print()
    ok45 = all(row["q"].get("0.45", "") == "ok/ok/ok" for row in box["rows"])
    best = [row["scale"] for row in box["rows"] if row["q"].get("0.45", "") == "ok/ok/ok"]
    if ok45:
        print(f"[结论] 0.45 质量下三通道全部可解（模块 {min(best)}~{max(best)} 像素）。"
              f"这套色度子采样下可用。")
    else:
        print("[结论] 至少有一档在 0.45 下解不出来 —— 见上表，看色度子采样吃掉了多少。")


if __name__ == "__main__":
    main()