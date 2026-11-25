#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wink-2D 闭环验证（不需要摄像头、不需要两台机器）。

链路：发送端页面渲染 → 截图（可选 JPEG 压缩，模拟远程桌面 H.264 的 4:2:0）
      → 拆通道解（灰度码就直接解；彩色三通道就把 R/G/B 各拆一次）
      → 按帧号拼回 → base44 还原 → CRC32 校验。

判据：整轮所有帧都能解出、拼回后 CRC 与发送端一致。

用法：
  python tools/qr2d_roundtrip.py                 # 默认 24 KB 负载
  python tools/qr2d_roundtrip.py --size 200000   # 大一点
"""

from __future__ import annotations

import argparse
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
SENDER = ROOT / "sender" / "send.html"
JSQR = ROOT / "vendor2d" / "jsQR.js"

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

PROBE = r"""
<script src="jsQR.js"></script>
<script>
(function () {
  var PORT = __PORT__, SIZE = __SIZE__;
  var t0 = performance.now();
  function post(o) {
    o.ms = Math.round(performance.now() - t0);
    try {
      fetch('http://127.0.0.1:' + PORT + '/done', { method: 'POST', mode: 'no-cors',
        body: JSON.stringify(o) });
    } catch (e) {}
  }

  var B44 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$%*+-./:';
  var B44_IDX = {}; for (var bi = 0; bi < B44.length; bi++) { B44_IDX[B44.charAt(bi)] = bi; }
  function b44dec(str) {
    var out = new Uint8Array(Math.ceil(str.length * 2 / 3) + 2), o = 0, i = 0, v, c0, c1, c2;
    while (i < str.length) {
      c0 = B44_IDX[str.charAt(i)]; c1 = B44_IDX[str.charAt(i + 1)];
      if (i + 2 < str.length) {
        c2 = B44_IDX[str.charAt(i + 2)];
        v = c0 * 1936 + c1 * 44 + c2; out[o++] = (v >> 8) & 0xFF; out[o++] = v & 0xFF; i += 3;
      } else { v = c0 * 44 + c1; out[o++] = v; i += 2; }
    }
    return out.subarray(0, o);
  }

  var CRC_TABLE = (function () {
    var t = new Uint32Array(256);
    for (var n = 0; n < 256; n++) { var c = n;
      for (var k = 0; k < 8; k++) { c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1); }
      t[n] = c >>> 0; }
    return t;
  })();
  function crc32(b) {
    var c = 0xFFFFFFFF;
    for (var i = 0; i < b.length; i++) { c = CRC_TABLE[(c ^ b[i]) & 0xFF] ^ (c >>> 8); }
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function parse(t) {
    if (!t || t.length <= 33) { return null; }
    var kind = t.slice(0, 3);
    if (kind !== 'WNK' && kind !== 'WNM') { return null; }
    return { kind: kind, idx: parseInt(t.substr(3, 4), 16), total: parseInt(t.substr(7, 4), 16),
             ver: parseInt(t.substr(11, 2), 16), size: parseInt(t.substr(13, 8), 16),
             crc: parseInt(t.substr(21, 8), 16), payload: t.slice(33) };
  }

  // 取某通道当灰度图
  function chan(px, c, w, h) {
    var cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    var img = ctx.createImageData(w, h), d = img.data, src = px.data;
    for (var i = 0; i < src.length; i += 4) {
      var v = src[i + c];
      d[i] = d[i + 1] = d[i + 2] = v; d[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return cv;
  }

  function dec(cv) {
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    var d = ctx.getImageData(0, 0, cv.width, cv.height);
    var r = jsQR(d.data, cv.width, cv.height, { inversionAttempts: 'dontInvert' });
    return r ? r.data : null;
  }

  function jpegRT(cv, q) {
    return new Promise(function (resolve) {
      var im = new Image();
      im.onload = function () {
        var c2 = document.createElement('canvas');
        c2.width = cv.width; c2.height = cv.height;
        var x2 = c2.getContext('2d', { willReadFrequently: true });
        x2.drawImage(im, 0, 0);
        resolve(x2.getImageData(0, 0, cv.width, cv.height));
      };
      im.onerror = function () { resolve(null); };
      im.src = cv.toDataURL('image/jpeg', q);
    });
  }

  function lossless(cv) {
    return cv.getContext('2d', { willReadFrequently: true })
             .getImageData(0, 0, cv.width, cv.height);
  }

  // 先按灰度整图解；不行再把三个通道各解一次
  function decodeScreen(px, w, h) {
    var out = [], seen = {}, c, t;
    // 灰度整图（灰度模式下一解即是；彩色模式下是叠影，会被 RS 校验挡掉）
    var whole = dec(chan(px, 0, w, h));
    if (whole) { out.push(whole); seen[whole] = 1; }
    for (c = 0; c < 3; c++) {
      t = dec(chan(px, c, w, h));
      if (t && !seen[t]) { out.push(t); seen[t] = 1; }
    }
    return out;
  }

  (async function () {
    if (!window.Wink2D || !window.jsQR) {
      return post({ ok: false, why: 'Wink2D=' + !!window.Wink2D + ' jsQR=' + !!window.jsQR });
    }
    var out = { ok: true, steps: [] };

    var bytes = new Uint8Array(SIZE), s = 987654321;
    for (var i = 0; i < SIZE; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; bytes[i] = s & 0xFF; }
    var total = Wink2D.loadBytes(bytes, 'probe.bin');
    out.meta = Wink2D.meta();
    var screens = Wink2D.screens();

    var cv = document.getElementById('code');

    // ---- A. 压缩余量（按屏，逐通道） ----
    var qs = ['lossless', 0.9, 0.65, 0.45, 0.25, 0.15];
    var picks = [0, Math.floor(screens / 2), screens - 1];
    for (var n = 0; n < picks.length; n++) {
      var p = picks[n];
      Wink2D.renderScreen(p);
      var w = cv.width, h = cv.height;
      var want = Wink2D.screenFrames(p).map(function (f) { return Wink2D.frameText(f); });
      var row = { screen: p, px: w, frames: want.length, q: {} };
      for (var qi = 0; qi < qs.length; qi++) {
        var px = qs[qi] === 'lossless' ? lossless(cv) : await jpegRT(cv, qs[qi]);
        if (!px) { row.q[String(qs[qi])] = 'noimg'; continue; }
        var got = decodeScreen(px, w, h);
        row.q[String(qs[qi])] = want.map(function (t) {
          return got.indexOf(t) >= 0 ? 'ok' : 'miss';
        }).join('/');
      }
      out.steps.push(row);
    }

    // ---- B. 单屏解码耗时（决定接收端帧率上限） ----
    Wink2D.renderScreen(0);
    var px0 = lossless(cv), w0 = cv.width, h0 = cv.height;
    var N = 9, tA = performance.now();
    for (var k = 0; k < N; k++) { decodeScreen(px0, w0, h0); }
    out.decodeMs = Math.round((performance.now() - tA) / N);

    // ---- C. 整轮：逐屏解，按帧号拼回 ----
    var Q = 0.65, chunks = {}, name = '', got = 0, tB = performance.now();
    for (var p2 = 0; p2 < screens; p2++) {
      Wink2D.renderScreen(p2);
      var w2 = cv.width, h2 = cv.height;
      var pxs = await jpegRT(cv, Q);
      if (!pxs) { continue; }
      var texts = decodeScreen(pxs, w2, h2);
      for (var x = 0; x < texts.length; x++) {
        var f = parse(texts[x]);
        if (!f) { continue; }
        if (f.kind === 'WNM') {
          try {
            var nb = b44dec(f.payload).subarray(0, f.size), nm = '';
            for (var z = 0; z < nb.length; z++) { nm += String.fromCharCode(nb[z]); }
            name = nm;
          } catch (e) { name = '（解不出来）'; }
          continue;
        }
        if (chunks[f.idx] === undefined) { chunks[f.idx] = f.payload; got++; }
      }
    }
    var parts = [], missing = [];
    for (var q3 = 0; q3 < total; q3++) {
      if (chunks[q3] === undefined) { missing.push(q3); } else { parts.push(chunks[q3]); }
    }
    var cyc = { total: total, got: got, missing: missing.length, quality: Q, screens: screens,
                secs: Math.round((performance.now() - tB) / 100) / 10, name: name };
    if (!missing.length) {
      try {
        var arr = b44dec(parts.join('')).subarray(0, out.meta.size);
        cyc.bytes = arr.length;
        cyc.crcOk = crc32(arr) === out.meta.crc;
      } catch (e) { cyc.err = String(e && e.message || e); }
    }
    out.cycle = cyc;
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
    ap.add_argument("--size", type=int, default=24000, help="测试负载字节数")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    if not SENDER.is_file():
        sys.exit(f"缺少 {SENDER}，先跑 python tools/build_2d.py")
    browser = next((p for p in BROWSERS if Path(p).is_file()), None)
    if not browser:
        sys.exit("找不到 Chrome/Edge")

    work = work_dir("wink-2d-")
    prof = profile_dir("roundtrip")
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

    import functools
    srv = ThreadingHTTPServer(("127.0.0.1", port),
                              functools.partial(H, directory=str(work)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        shutil.copy2(JSQR, work / "jsQR.js")
        html = SENDER.read_text(encoding="utf-8")
        probe = PROBE.replace("__PORT__", str(port)).replace("__SIZE__", str(args.size))
        (work / "test.html").write_text(html.replace("</body>", probe + "\n</body>"), encoding="utf-8")

        url = f"http://127.0.0.1:{port}/test.html"
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--no-first-run", "--window-size=1280,900",
             f"--user-data-dir={prof}", url],
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
        print("[x] 超时，页面没有回报结果")
        sys.exit(1)
    if box.get("fatal"):
        print("[x]", box["fatal"])
        sys.exit(1)
    if not box.get("ok"):
        print("[x] 探针失败：", box.get("why"))
        sys.exit(1)

    meta = box["meta"]
    print(f"负载 {meta['size']:,} B → {meta['total']} 帧，{meta['channels']} 通道/屏，"
          f"共 {meta['screens']} 屏（{meta['chunk']} 字符/帧，约 {int(meta['chunk'] * 2 / 3)} B/帧）")
    print(f"每屏 {meta['bytesPerScreen'] / 1024:.1f} KB　·　单屏解码 {box['decodeMs']} ms"
          f"（本机上限约 {1000 / max(1, box['decodeMs']):.0f} 屏/秒）")
    print()
    print("压缩余量　（每格是这一屏各通道的解码结果，ok=解出且内容正确）")
    print(f"  {'屏':>4} {'画布':>6} {'通道':>4}   " + " ".join(f"{q:>10}" for q in
          ["无损", "0.9", "0.65", "0.45", "0.25", "0.15"]))
    for row in box["steps"]:
        cells = " ".join(f"{row['q'].get(k, '-'):>10}" for k in
                         ["lossless", "0.9", "0.65", "0.45", "0.25", "0.15"])
        print(f"  {row['screen']:>4} {row['px']:>6} {row['frames']:>4}   {cells}")

    cyc = box.get("cycle") or {}
    print()
    print(f"整轮（质量 {cyc['quality']}，{cyc['screens']} 屏）：收到 {cyc['got']}/{cyc['total']} 帧"
          f"，缺 {cyc['missing']}")
    if cyc.get("crcOk") is True:
        print(f"  ✔ 拼回 {cyc['bytes']:,} B，CRC 一致；文件名回传：{cyc.get('name') or '（无）'}")
    elif cyc.get("err"):
        print("  ✘ 还原失败：", cyc["err"])
    else:
        print("  ✘ CRC 不一致或未收齐")

    okc = cyc.get("crcOk") is True
    print()
    print("[结论] " + ("通过：整轮全部帧可解、CRC 一致" if okc else "未通过，见上表"))
    sys.exit(0 if okc else 2)


if __name__ == "__main__":
    main()