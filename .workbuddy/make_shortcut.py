"""重建桌面快捷方式，指向 start.bat（全英文批处理，避免编码问题）。"""

import base64
import subprocess
import sys
from pathlib import Path

PROJECT = Path(r"C:\Users\zly\WorkBuddy\2026-10-27-18-26-17")
TARGET = PROJECT / "start.bat"


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: not found: {TARGET}")
        return 1

    ps_getdesktop = "[Environment]::GetFolderPath('Desktop')"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_getdesktop],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    desktop = r.stdout.strip()
    if not desktop:
        desktop = str(Path.home() / "Desktop")

    link = str(Path(desktop) / "小满 Agent.lnk")

    ps = f"""
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut("{link}")
$s.TargetPath = "{TARGET}"
$s.WorkingDirectory = "{PROJECT}"
$s.Description = "Xiaoman Agent - local assistant"
$s.IconLocation = "$env:SystemRoot\\System32\\shell32.dll,14"
$s.Save()
Write-Output (Test-Path "{link}")
"""
    b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
    r2 = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", b64],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (r2.stdout or "").strip()
    print(f"结果: {out}")
    if Path(link).exists():
        print(f"快捷方式: {link}")
        return 0
    print(r2.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
