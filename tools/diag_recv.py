#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位接收端启动错误：把 window.onerror 抓下来。"""
import functools
import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _browser import kill_by_profile, profile_dir, work_dir

ROOT = Path(__file__).resolve().parents[1]
RECV = ROOT / "receiver" / "recv.html"

CAP = """<script>
window.__ERR__ = [];
window.addEventListener('error', function (e) {
  window.__ERR__.push(String(e.message) + ' @' + (e.lineno || 0));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__ERR__.push('reject: ' + String(e.reason));
});
</script>"""

POST = """<script>
(function () {
  function go() {
  var el = document.getElementById('libjsqr');
  var out = {
    err: window.__ERR__, liberr: window.__LIBERR__ || null,
    hasLib: !!el, libLen: el && el.textContent ? el.textContent.length : -1,
    wired: typeof document.getElementById('btnStart').onclick,
    jsqr: (function () { try { return typeof __JSQR__; } catch (e) { return 'throw:' + e.message; } })(),
    state: (function () { try { return window.Wink2DRecv.state(); } catch (e) { return 'throw:' + e.message; } })(),
    wtest: (function () {
      try {
        var w = new Worker(URL.createObjectURL(new Blob(['self.onmessage=function(e){self.postMessage(1)}'], {type:'text/javascript'})));
        w.postMessage(1);
        return 'created';
      } catch (e) { return 'throw:' + e.message; }
    })()
  };
  fetch('http://127.0.0.1:__PORT__/done', { method: 'POST', mode: 'no-cors', body: JSON.stringify(out) });
  }
  setTimeout(go, 1500);
})();
</script>"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
work = work_dir("wink-diag-")
prof = profile_dir("diag")
box, ev = {}, threading.Event()


class H(SimpleHTTPRequestHandler):
    def do_POST(self):
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
threading.Thread(target=srv.serve_forever, daemon=True).start()

html = RECV.read_text(encoding="utf-8").replace("<body>", "<body>\n" + CAP, 1)
html = html.replace("</body>", POST.replace("__PORT__", str(port)) + "\n</body>")
(work / "d.html").write_text(html, encoding="utf-8")

browser = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
proc = subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
                         "--no-sandbox", "--no-first-run", f"--user-data-dir={prof}",
                         f"http://127.0.0.1:{port}/d.html"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
got = ev.wait(40)
kill_by_profile(work)
srv.shutdown()
if not got:
    sys.exit("[x] 没有回传")
print(json.dumps(box, ensure_ascii=False, indent=2))