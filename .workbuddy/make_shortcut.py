"""在桌面创建/更新「小满 Agent」快捷方式（桌面版）

为什么不用 WScript.Shell：COM 实例化会被安全策略拦下（试过，被 block）。
这里用 pylnk3 直接写 .lnk 二进制，纯 Python，不碰 COM。

快捷方式指向 pythonw.exe + desktop.py，而不是 desktop.bat：
指向 bat 的话双击瞬间会闪一个黑框；pythonw 全程无窗口，
图标直接用 assets/icon.ico，不是 Python 那条蛇。

重复执行安全：同名文件直接覆盖（早期版本指向 start.bat，会被更新掉）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"
ICON = ROOT / "assets" / "icon.ico"
ENTRY = ROOT / "desktop.py"

for p, what in ((PY, "pythonw.exe"), (ICON, "assets/icon.ico"), (ENTRY, "desktop.py")):
    if not p.exists():
        print(f"[ERROR] 缺少 {what}: {p}")
        print("        先在项目根建虚拟环境并安装依赖")
        sys.exit(1)

try:
    import pylnk3
except ImportError:
    print("[ERROR] 缺 pylnk3：.venv\\Scripts\\python -m pip install pylnk3 -i https://pypi.org/simple")
    sys.exit(2)


def desktop_dir() -> Path:
    """桌面真实位置：OneDrive 会把「桌面」重定向，写死 ~/Desktop 会看不见。"""
    for d in Path.home().glob("OneDrive*/Desktop"):
        if d.is_dir():
            return d
    for name in ("Desktop", "桌面"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home() / "Desktop"


DESK = desktop_dir()
LNK = DESK / "小满 Agent.lnk"

pylnk3.for_file(
    str(PY),
    lnk_name=str(LNK),
    arguments=f'"{ENTRY}"',
    description="小满 Agent - 本地文档分析助手",
    icon_file=str(ICON),
    work_dir=str(ROOT),
    window_mode=pylnk3.WINDOW_NORMAL,
)

if not LNK.exists():
    print(f"[FAIL] 没写出来：{LNK}")
    sys.exit(3)

# 读回来核对，避免"文件在但内容错"这种静默失败
lnk = pylnk3.parse(str(LNK))
print(f"快捷方式  : {LNK}")
print(f"  目标    : {lnk.link_info.local_base_path}")
print(f"  参数    : {lnk.arguments}")
print(f"  工作目录: {lnk.work_dir}")
print(f"  大小    : {LNK.stat().st_size} bytes")
print("OK")
