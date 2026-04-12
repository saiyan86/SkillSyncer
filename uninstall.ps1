#Requires -Version 5.1
<#
.SYNOPSIS
    SkillSyncer uninstaller for native Windows (PowerShell).

.DESCRIPTION
    Removes the `skillsyncer` binary only. Does NOT touch your
    secrets, your rendered skills, or any git hooks in your repos.
    Run this from any shell:

        iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/uninstall.ps1 | iex

    Hooks installed in repos remain on disk but become silent
    no-ops without the binary on PATH. To wipe SkillSyncer data
    too, after this script runs:

        Remove-Item -Recurse -Force "$HOME\.skillsyncer"
#>

$ErrorActionPreference = 'Stop'

Write-Host 'Uninstalling SkillSyncer...'

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

$uninstalled = $false

if (Test-Cmd 'uv') {
    try { & uv tool uninstall skillsyncer 2>$null; $uninstalled = $true } catch {}
}
if (-not $uninstalled -and (Test-Cmd 'pipx')) {
    try { & pipx uninstall skillsyncer 2>$null; $uninstalled = $true } catch {}
}
if (-not $uninstalled -and (Test-Cmd 'pip')) {
    try { & pip uninstall -y skillsyncer 2>$null; $uninstalled = $true } catch {}
}
if (-not $uninstalled -and (Test-Cmd 'pip3')) {
    try { & pip3 uninstall -y skillsyncer 2>$null; $uninstalled = $true } catch {}
}

if (-not $uninstalled) {
    Write-Error 'Could not find skillsyncer in uv / pipx / pip. Remove it manually.'
    exit 1
}

Write-Host ''
Write-Host '[OK] SkillSyncer binary removed.'
Write-Host ''
Write-Host 'Your data is intact:'
Write-Host '  $HOME\.skillsyncer\        secrets, config, sync state'
Write-Host '  $HOME\.claude\skills\, ... rendered skills'
Write-Host '  .git\hooks\                unchanged (silent no-op without the binary)'
Write-Host ''
Write-Host 'To wipe SkillSyncer data too:'
Write-Host '  Remove-Item -Recurse -Force "$HOME\.skillsyncer"'
Write-Host ''
