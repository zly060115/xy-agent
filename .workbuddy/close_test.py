"""关窗退出的自动化测试

模拟用户点窗口右上角 X：启动桌面版（7862 端口），12 秒后程序内部
destroy 窗口 —— 之后 desktop.py 应该自己把进程退干净。

判定标准：
1. 脚本进程退出（而不是一直挂着）
2. 退出码 0
3. 7862 端口不再有人监听（uvicorn 被一起收掉了，没有僵尸线程）
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 7862


def port_free(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", p)) != 0


assert port_free(PORT), f"{PORT} 被占用，测试前先释放"

code = f"""
import threading, sys
sys.argv = ["desktop.py"]
import webview, desktop

_orig = webview.create_window
def patched(*a, **kw):
    w = _orig(*a, **kw)
    threading.Timer(12.0, w.destroy).start()   # 模拟 12 秒后用户关窗
    return w
webview.create_window = patched
raise SystemExit(desktop.main())
"""

proc = subprocess.run(
    [str(PY), "-u", "-c", code],
    cwd=str(ROOT),
    env={**os.environ, "WEB_PORT": str(PORT), "PYTHONIOENCODING": "utf-8"},
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=120,
)

time.sleep(1.5)
ok_exit = proc.returncode == 0
ok_port = port_free(PORT)

print(f"returncode = {proc.returncode}")
print(f"port {PORT} free after close = {ok_port}")
if proc.stdout.strip():
    print("stdout:", proc.stdout.strip()[-500:])
if proc.stderr.strip():
    print("stderr:", proc.stderr.strip()[-500:])

if ok_exit and ok_port:
    print("PASS: 窗口关闭后进程退出、端口释放")
else:
    print("FAIL")
    sys.exit(1)
