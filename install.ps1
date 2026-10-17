$ErrorActionPreference = 'Stop'
$TaskName = 'ObsidianTaskReminders'
. (Join-Path $PSScriptRoot 'runtime.ps1')
$Pythonw = Get-ObsidianPythonw
$Worker = Join-Path $PSScriptRoot 'bot_service.py'
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) { throw 'Python не найден.' }
if (-not (Test-Path -LiteralPath $Worker -PathType Leaf)) { throw 'Программа напоминаний не найдена.' }
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-X utf8 `"$Worker`" --watch" -WorkingDirectory $PSScriptRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Напоминания о задачах из Obsidian в отдельном Telegram-боте.'
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output 'Напоминания установлены: фоновая работа сейчас и после входа в Windows.'
