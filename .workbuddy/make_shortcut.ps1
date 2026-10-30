$ErrorActionPreference = "Stop"

$target = "C:\Users\zly\WorkBuddy\2026-10-27-18-26-17\启动.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "小满 Agent.lnk"

if (-not (Test-Path $target)) {
    Write-Output "ERROR: target not found: $target"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = "C:\Users\zly\WorkBuddy\2026-10-27-18-26-17"
$shortcut.Description = "小满 Agent - 本地智能助手"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,14"
$shortcut.Save()

if (Test-Path $linkPath) {
    Write-Output "OK: $linkPath"
} else {
    Write-Output "FAILED"
    exit 1
}
