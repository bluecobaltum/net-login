<#
校园网自动登录: Windows 一键安装 / 卸载 (任务计划程序)

  .\install-windows.ps1              用户级: 登录 Windows 后自动运行(不需要管理员)
  .\install-windows.ps1 -AtStartup   系统级: 开机即运行(需要管理员权限)
  .\install-windows.ps1 -Uninstall   卸载

脚本本体与 Linux/macOS 完全相同, 只是自启动换成任务计划程序。
#>
param(
    [switch]$AtStartup,
    [switch]$Uninstall,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "net-login"
$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    if ($PythonPath) { return $PythonPath }
    $found = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try { $found += (& py -3 -c "import sys;print(sys.executable)" 2>$null) } catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $found += $python.Source }
    foreach ($p in $found) { if ($p -and (Test-Path $p)) { return $p } }
    return ""
}

if ($AtStartup) {
    $BinDir  = Join-Path $env:ProgramData "net-login"
    $ConfDir = $BinDir
} else {
    $BinDir  = Join-Path $env:LOCALAPPDATA "net-login"
    $ConfDir = Join-Path $env:APPDATA "net-login"
}
$Bin  = Join-Path $BinDir  "net-login.py"
$Conf = Join-Path $ConfDir "config.ini"
$Log  = Join-Path $ConfDir "netlogin.log"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -Force -ErrorAction SilentlyContinue $Bin
    Write-Host "已卸载。配置文件保留在: $Conf" -ForegroundColor Green
    exit 0
}

$python = Find-Python
if (-not $python) {
    Write-Host "没找到 Python 3。请先安装(勾选 Add python.exe to PATH):" -ForegroundColor Red
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "  或 https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Force -Path $BinDir, $ConfDir | Out-Null
Copy-Item -Force (Join-Path $SrcDir "auto-login.py") $Bin
if (Test-Path $Conf) {
    Write-Host "已存在配置文件, 保留原内容: $Conf"
} else {
    Copy-Item (Join-Path $SrcDir "config.example.ini") $Conf
    Write-Host "已生成配置文件: $Conf"
}

# pythonw.exe 跑起来不弹黑框(pythonw 下没有控制台, 所以日志走 --log-file)
$pythonExe = $python
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (Test-Path $pythonw) { $pythonExe = $pythonw }

$arguments = '"{0}" --config "{1}" --log-file "{2}" watch' -f $Bin, $Conf, $Log
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument $arguments

if ($AtStartup) {
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host @"

安装完成, 下一步:
  1. 填写账号密码:  notepad "$Conf"
  2. 试一次登录:    python "$Bin" login --verbose
  3. 查看日志:      Get-Content "$Log" -Wait

提示: 状态:  Get-ScheduledTask -TaskName $TaskName
      停止:  Stop-ScheduledTask -TaskName $TaskName
      重跑:  Start-ScheduledTask -TaskName $TaskName
      若同时装了哆点/Dr.COM 客户端, 建议退出客户端, 以免两边互相抢会话
"@ -ForegroundColor Cyan
