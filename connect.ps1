$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime.ps1')
$Pythonw = Get-ObsidianPythonw
$Setup = Join-Path $PSScriptRoot 'setup_bot.py'
Start-Process -FilePath $Pythonw -ArgumentList @('-X','utf8',"`"$Setup`"") -WorkingDirectory $PSScriptRoot
