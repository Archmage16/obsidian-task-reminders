function Get-ObsidianPythonw {
    $Base = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path -LiteralPath $Base -PathType Container) {
        foreach ($Folder in (Get-ChildItem -LiteralPath $Base -Directory -Filter 'Python*' | Sort-Object Name -Descending)) {
            $Candidate = Join-Path $Folder.FullName 'pythonw.exe'
            if (Test-Path -LiteralPath $Candidate -PathType Leaf) { return $Candidate }
        }
    }
    $Command = Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Command) { return $Command.Source }
    throw 'Нужен установленный Python 3.11 или новее с pythonw.exe.'
}
