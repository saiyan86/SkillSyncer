#Requires -Version 5.1
<#
.SYNOPSIS
    SkillSyncer installer for native Windows (PowerShell).

.DESCRIPTION
    Installs SkillSyncer using uv tool, pipx, or pip --user (in that order),
    sourcing from the public GitHub repo until a PyPI release ships.

    Override the install source via $env:SKILLSYNCER_SOURCE.

.EXAMPLE
    iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/install.ps1 | iex
#>

$ErrorActionPreference = 'Stop'

$Version = '0.1.0'
Write-Host "Installing SkillSyncer v$Version..."

# Default source: GitHub repo. Set $env:SKILLSYNCER_SOURCE=skillsyncer
# once the package is published to PyPI.
if ($env:SKILLSYNCER_SOURCE) {
    $source = $env:SKILLSYNCER_SOURCE
} else {
    $source = 'git+https://github.com/saiyan86/SkillSyncer.git'
}

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (Test-Cmd 'uv') {
    & uv tool install --force $source
} elseif (Test-Cmd 'pipx') {
    & pipx install --force $source
} elseif (Test-Cmd 'pip') {
    & pip install --user $source
} elseif (Test-Cmd 'pip3') {
    & pip3 install --user $source
} else {
    Write-Error 'No uv / pipx / pip found. Install Python first: https://www.python.org/downloads/'
    exit 1
}

# Make sure the user-scripts dir is on PATH for the current session.
# pipx and uv tool already manage this; pip --user does not.
$pyUserScripts = & python -c "import sysconfig; print(sysconfig.get_path('scripts', f'{sysconfig.get_default_scheme()}_user'))" 2>$null
if ($pyUserScripts -and (Test-Path $pyUserScripts)) {
    if ($env:PATH -notlike "*$pyUserScripts*") {
        $env:PATH = "$pyUserScripts;$env:PATH"
        Write-Host "Added $pyUserScripts to PATH for this session."
        Write-Host "To make it permanent, add it to your user PATH via System Properties."
    }
}

Write-Host ''
Write-Host '[OK] SkillSyncer installed'
Write-Host ''
Write-Host 'Run:  skillsyncer init'
Write-Host ''
