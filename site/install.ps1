# QAJev installer for Windows (https://qajev.com/install.ps1). In PowerShell:
#
#   irm https://qajev.com/install.ps1 | iex
#
# QAJev runs on Linux. On Windows it runs inside WSL (Windows Subsystem for Linux), so this installs it in your default
# WSL distribution with the same installer as Linux: https://qajev.com/install.sh. It needs no admin rights, and it
# never turns WSL on by itself: when WSL or a Linux distribution is missing, it says the one command to run.

function Install-QAJev {
    $ErrorActionPreference = 'Stop'
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Host 'QAJev runs on Windows through WSL, and WSL is not installed.'
        Write-Host 'In PowerShell as administrator, run:  wsl --install'
        Write-Host 'Restart Windows, open "Ubuntu" from the Start menu once, then run this line again.'
        return
    }
    # wsl.exe prints its list in UTF-16, so drop the NUL characters before reading it.
    $distros = @((& wsl.exe --list --quiet 2>$null) | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
    if ($distros.Count -eq 0) {
        Write-Host 'WSL is on, but it has no Linux distribution yet.'
        Write-Host 'In PowerShell as administrator, run:  wsl --install -d Ubuntu'
        Write-Host 'Open "Ubuntu" from the Start menu once to finish it, then run this line again.'
        return
    }
    Write-Host "Installing QAJev in WSL ($($distros[0]) is the default)..."
    & wsl.exe -e sh -c 'curl -LsSf https://qajev.com/install.sh | sh'
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'The installer in WSL stopped with an error (see above).'
        return
    }
    Write-Host ''
    Write-Host 'QAJev is installed in WSL. Open your WSL terminal ("Ubuntu" in the Start menu) and run:  qajev doctor'
}

Install-QAJev
