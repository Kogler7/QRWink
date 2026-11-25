#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 src/{send2d,recv2d}.html 和 vendor2d/*.js 打包成两个独立单文件。

为什么不用打包器：两个库都是 UMD，只要给它们一个假的 CommonJS 环境
（var module = {exports:{}}, exports = module.exports）就能把返回值接住，
不需要任何构建链。产物用 file:// 直接打开即可，发送端不需要服务器。

用法：python tools/build_2d.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VENDOR = ROOT / "vendor2d"

TASKS = [
    {
        "src": "send.tpl.html",
        "out": ROOT / "sender" / "send.html",
        "lib_placeholder": "var __QRCODE__;",
        "lib_file": "qrcode.js",
        "lib_var": "__QRCODE__",
        # 发送端也要一份纯文本：编码放进 Worker 并行做，Worker 从这段文本拼出编码器
        "lib_as_text": True,
        "lib_id": "libqrcode",
        "note": "qrcode-generator 1.4.4 (MIT, Kazuhiko Arase) —— 编码，纯 JS 2500 行无依赖",
    },
    {
        "src": "recv.tpl.html",
        "out": ROOT / "receiver" / "recv.html",
        "lib_placeholder": "var __JSQR__;",
        "lib_file": "jsQR.js",
        "lib_var": "__JSQR__",
        "lib_as_text": True,
        "note": "jsQR 1.4.0 (MIT, Cosmo Wolfe) —— 从 ImageData 解码，纯 JS，无 wasm",
    },
]


def wrap_lib(lib_src: str, var_name: str) -> str:
    """把 UMD 库包进 IIFE，返回它的导出函数。

    两个坑都会让导出悄悄丢掉，表现为「__QRCODE__ is not a function」：
      * 某些浏览器扩展/企业策略会定义 window.define（带 .amd），UMD 于是走 AMD 分支，
        module.exports 永远是空对象；
      * 少数 UMD 写法优先挂 window.*。
    所以这里：先 var define 把 AMD 分支挡掉，再从多个通道兜底找一个函数出来。
    找不到返回 null（不抛错），由页面在启动时自己报出来，避免整页白掉。
    """
    return (
        f"var {var_name} = (function () {{\n"
        "  var module = { exports: {} };\n"
        "  var exports = module.exports;\n"
        "  var define;\n"
        "  try {\n"
        f"{_indent(lib_src)}\n"
        "  } catch (e) {\n"
        "    window.__LIBERR__ = String((e && (e.message || e)) || e);\n"
        f"    console.error('内联库初始化抛错', e);\n"
        f"    return null;\n"
        "  }\n"
        "  window.__LIBOUT__ = (module.exports && typeof module.exports === 'object')\n"
        "    ? ('{keys:' + Object.keys(module.exports).join(',') + '}') : (typeof module.exports);\n"
        "  var out = module.exports;\n"
        "  if (typeof out === 'function' && out.qrcode && typeof out.qrcode === 'function') {\n"
        "    out = out.qrcode;\n"
        "  }\n"
        "  if (typeof out !== 'function') {\n"
        "    var cand = [window.qrcode, window.jsQR, window.QRCode, window.qrCode];\n"
        "    for (var i = 0; i < cand.length; i++) {\n"
        "      if (typeof cand[i] === 'function') { out = cand[i]; break; }\n"
        "    }\n"
        "  }\n"
        "  return (typeof out === 'function') ? out : null;\n"
        "})();\n"
    )


def text_bootstrap(var_name: str, lib_id: str = "libjsqr") -> str:
    """库以纯文本放在 <script id="libjsqr"> 里：Worker 从这段文本拼出解码器，
    页面自己需要同步兜底时用 new Function 现取一份，整个文件里只存一份代码。"""
    return (
        f"var {var_name} = (function () {{\n"
        f"  var el = document.getElementById('{lib_id}');\n"
        "  if (!el) { return null; }\n"
        "  try {\n"
        "    var f = new Function('var module = { exports: {} }, exports = module.exports, define;\\n'\n"
        "      + el.textContent\n"
        "      + '\\nreturn (module.exports && (module.exports.default || module.exports)) || null;');\n"
        "    return f();\n"
        "  } catch (e) {\n"
        "    window.__LIBERR__ = String((e && (e.message || e)) || e);\n"
        "    return null;\n"
        "  }\n"
        "})();"
    )


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def main() -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = f"build 2d-1 · 单文件 {stamp}"
    ok = True

    for t in TASKS:
        src_path = SRC / t["src"]
        lib_path = VENDOR / t["lib_file"]
        if not src_path.is_file():
            print(f"[x] 缺少 {src_path}")
            ok = False
            continue
        if not lib_path.is_file():
            print(f"[x] 缺少 {lib_path}（先下载到 vendor2d/）")
            ok = False
            continue

        html = src_path.read_text(encoding="utf-8")
        if t["lib_placeholder"] not in html:
            print(f"[x] {t['src']} 里找不到占位符 {t['lib_placeholder']!r}")
            ok = False
            continue

        lib_src = lib_path.read_text(encoding="utf-8")
        if "</script" in lib_src.lower():
            print(f"[x] {t['lib_file']} 含 </script，不能直接内联")
            ok = False
            continue

        html = html.replace("__BUILD__", tag)
        if "var __TEMPLATE__ = true;" not in html:
            print(f"[x] {t['src']} 里找不到模板标记")
            ok = False
            continue
        html = html.replace("var __TEMPLATE__ = true;", "var __TEMPLATE__ = false;")
        if t.get("lib_as_text"):
            lib_id = t.get("lib_id", "libjsqr")
            block = ('<script id="' + lib_id + '" type="text/plain">\n' + lib_src + '\n</script>\n')
            if "<body>" not in html:
                print(f"[x] {t['src']} 里找不到 <body>")
                ok = False
                continue
            html = html.replace("<body>", "<body>\n" + block, 1)
            html = html.replace(t["lib_placeholder"], text_bootstrap(t["lib_var"], lib_id))
        else:
            html = html.replace(t["lib_placeholder"], wrap_lib(lib_src, t["lib_var"]).strip())

        out = t["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        size = out.stat().st_size
        # 回填「本文件应有的字节数」：定宽 15 字符，保证回填后尺寸不变
        text = out.read_text(encoding="utf-8")
        if "__EXPECT_SIZE__" in text:
            text = text.replace("__EXPECT_SIZE__", f"{size:06d}".ljust(15))
            out.write_text(text, encoding="utf-8")
            size2 = out.stat().st_size
            assert size2 == size, f"回填尺寸变了：{size} -> {size2}"
        digest = hashlib.sha256(out.read_bytes()).hexdigest()[:12]
        print(f"[ok] {out.relative_to(ROOT)}  {size:,} B  sha256:{digest}")
        print(f"     └ {t['note']}")

    if not ok:
        sys.exit(1)
    print(f"\n完成 · {stamp}")
    print("发送端直接用浏览器打开 sender/send.html（file:// 即可，不需要 WebGL）")
    print("接收端要通过 http://127.0.0.1 打开（getDisplayMedia 要安全上下文）：")
    print("  python receiver/serve.py  然后访问 /recv.html")


if __name__ == "__main__":
    main()