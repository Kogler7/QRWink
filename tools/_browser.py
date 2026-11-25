"""测试脚本共用的进程/目录管理。

**必须遵守的一条**：任何 `--user-data-dir` 都只能指向本文件给的 `profile_dir()`。

原因（2026-11-25 事故）：Chromium 系浏览器在**首次**创建某个 user-data-dir
（生成 `Local State`）时，会拿当前 Windows 账户发起一次交互式 LogonUser
（Negotiate，用户名为当前账户、密码为空）——必然失败，并往安全日志写一条 4625。
每轮测试都 mkdtemp 一个新目录，就等于每轮往账户的失败登录计数器上加次数。
当天累积 228 条 4625、账户被锁 10 次，用户自己都登不进去。

所以：
  * profile 放在固定目录 `%TEMP%/wink-prof/<tag>`，跨轮复用，**永不删除**；
  * 新建 tag 目录时先塞一份现成的 `Local State`（实测这样不会触发那次校验）；
  * 只有测试产物目录（截图、临时页面）才用 `work_dir()`，退出时自动删。

两个坑（都踩过）：
1. Chrome/Edge 会派生一堆子进程，`proc.terminate()` 只杀主进程，headless 的
   gpu/utility/renderer/crashpad 会留下来；攒到几百个之后新实例启动就会变得很怪。
2. Windows 上 Chrome 启动后会用**新 PID** 继续跑，launcher 立刻退出，
   所以拿 launcher 的 pid 去 taskkill 是无效的 —— 必须按 --user-data-dir 找进程。

另外 wmic 在 Windows 11 已移除，进程查询统一走 PowerShell。
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

PROFILE_ROOT = Path(tempfile.gettempdir()) / "wink-prof"
_WORK: list = []


def _seed_candidates() -> list:
    la = os.environ.get("LOCALAPPDATA", "")
    cands = []
    if la:
        cands.append(Path(la) / "Google" / "Chrome" / "User Data" / "Local State")
        cands.append(Path(la) / "Microsoft" / "Edge" / "User Data" / "Local State")
    cands.append(PROFILE_ROOT / "seed" / "Local State")
    return cands


def profile_dir(tag: str):
    """固定的浏览器 profile 目录，跨轮复用。

    首次创建该 tag 时先塞一份现成的 `Local State`：浏览器因此不走「首次初始化
    profile」那条路径，也就不会对当前 Windows 账户做一次注定失败的凭据校验。
    """
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    d = PROFILE_ROOT / tag
    d.mkdir(parents=True, exist_ok=True)
    local_state = d / "Local State"
    if not local_state.is_file():
        for cand in _seed_candidates():
            try:
                if cand.is_file():
                    shutil.copyfile(cand, local_state)
                    break
            except OSError:
                continue
    kill_by_profile(d)          # 上一轮没清干净的话 profile 会被锁住起不来
    return d


def work_dir(prefix: str = "wink-"):
    """本轮测试的产物目录（截图、临时页面）。退出时自动删除，与 profile 无关。"""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    _WORK.append(d)
    return d


@atexit.register
def _cleanup_work() -> None:
    for d in _WORK:
        shutil.rmtree(d, ignore_errors=True)


def clean_old_dirs(prefixes=("wink-",)) -> int:
    """一次性清掉历史遗留的临时目录（不动 wink-prof 这个 profile 根）。"""
    base = Path(tempfile.gettempdir())
    removed = 0
    if not base.is_dir():
        return 0
    for entry in base.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefixes):
            continue
        if entry.name == PROFILE_ROOT.name:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


def kill_tree(proc) -> None:
    """杀进程整棵树（launcher 还活着时可用；Chrome 通常会自己换 PID，见 kill_by_profile）。"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _list_browser_procs() -> list[tuple[int, str]]:
    """返回 [(pid, commandline), ...]，包含 chrome/msedge。"""
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    for shell in ("powershell", "pwsh"):
        try:
            out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=60).stdout
        except Exception:
            continue
        rows = []
        for line in out.splitlines():
            if "\t" not in line:
                continue
            pid, cmd = line.split("\t", 1)
            pid = pid.strip()
            if pid.isdigit():
                rows.append((int(pid), cmd))
        if rows:
            return rows
    return []


def kill_by_profile(profile_dir) -> int:
    """按 --user-data-dir 路径杀进程，处理「launcher 已退出、浏览器换 PID 继续跑」的情况。"""
    if os.name != "nt":
        return 0
    key = str(profile_dir)
    killed = 0
    for pid, cmd in _list_browser_procs():
        if key not in cmd:
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        killed += 1
    return killed


def sweep_stale(prefixes: tuple[str, ...] = ("wink-",)) -> int:
    """清掉以前跑测试留下的临时 profile 进程（只匹配带我们标记的命令行）。"""
    if os.name != "nt":
        return 0
    killed = 0
    for pid, cmd in _list_browser_procs():
        if str(PROFILE_ROOT) in cmd:
            continue        # profile 复用目录里的进程不是垃圾
        if not any(p in cmd for p in prefixes):
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        killed += 1
    return killed


def cdp_alive(port: int, timeout: float = 1.0) -> bool:
    """调试图端口是否已经能应答。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_cdp(port: int, seconds: float = 20.0) -> bool:
    import time
    end = time.time() + seconds
    while time.time() < end:
        if cdp_alive(port):
            return True
        time.sleep(0.25)
    return False


if __name__ == "__main__":
    if "--clean-dirs" in sys.argv:
        print(f"清掉历史临时目录: {clean_old_dirs()}")
    n = sweep_stale()
    print(f"清掉残留测试进程: {n}")
    print(f"当前 chrome/msedge 进程: {len(_list_browser_procs())}")
    sys.exit(0)