#Requires -Version 5.1
<#
.SYNOPSIS
    SkillSyncer uninstaller for native Windows (PowerShell).

.DESCRIPTION
    Default: removes the `skillsyncer` binary only. Your secrets,
    rendered skills, and git hooks are kept.

    With -Purge, also deletes $HOME\.skillsyncer\ entirely
    (secrets, config, state, cloned source repos). The hooks
    in your project repos are still left alone.

.PARAMETER Purge
    After removing the binary, delete $HOME\.skillsyncer\.
    Asks for confirmation unless -Yes is also passed.

.PARAMETER Yes
    Don't prompt before purging. Required when -Purge is used
    in a non-interactive shell.

.EXAMPLE
    iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/uninstall.ps1 | iex

.EXAMPLE
    iex "& { $(iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/uninstall.ps1) } -Purge"
#>

param(
    [switch]$Purge,
    [switch]$Yes,
    [switch]$Help
)

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

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

if ($Purge) {
    $ssDir = Join-Path $HOME '.skillsyncer'
    if (-not (Test-Path $ssDir)) {
        Write-Host ''
        Write-Host '[OK] No .skillsyncer\ to purge.'
        exit 0
    }

    Write-Host ''
    Write-Host '═══════════════════════════════════════════════════════════════'
    Write-Host ' -Purge will PERMANENTLY delete the following:'
    Write-Host '═══════════════════════════════════════════════════════════════'
    Write-Host ''
    Write-Host '   $HOME\.skillsyncer\identity.yaml      (your stored secrets)'
    Write-Host '   $HOME\.skillsyncer\config.yaml        (your sources / targets)'
    Write-Host '   $HOME\.skillsyncer\state.yaml         (sync state)'
    Write-Host '   $HOME\.skillsyncer\reports\           (run reports)'
    Write-Host '   $HOME\.skillsyncer\repos\             (cloned source repos)'
    Write-Host ''
    Write-Host ' Rendered skills in $HOME\.claude\skills\, etc. are NOT touched.'
    Write-Host ' Hooks in your project repos are NOT touched.'
    Write-Host ''

    if ($Yes) {
        $confirm = 'PURGE'
    } elseif ([Console]::IsInputRedirected) {
        Write-Error 'Refusing to purge in non-interactive shell without -Yes.'
        Write-Host '  Re-run with: -Purge -Yes'
        exit 1
    } else {
        $confirm = Read-Host " Type 'PURGE' to confirm (anything else cancels)"
    }

    if ($confirm -ceq 'PURGE') {
        Remove-Item -Recurse -Force $ssDir
        Write-Host ''
        Write-Host '[OK] $HOME\.skillsyncer\ purged.'
    } else {
        Write-Host ''
        Write-Host 'Cancelled. $HOME\.skillsyncer\ was not touched.'
        exit 1
    }
    exit 0
}

Write-Host ''
Write-Host 'Your data is intact:'
Write-Host '  $HOME\.skillsyncer\        secrets, config, sync state'
Write-Host '  $HOME\.claude\skills\, ... rendered skills'
Write-Host '  $HOME\.git\hooks\          unchanged (silent no-op without the binary)'
Write-Host ''
Write-Host 'To wipe SkillSyncer data too, re-run with -Purge:'
Write-Host '  iex "& { $(iwr -useb https://raw.githubusercontent.com/saiyan86/SkillSyncer/main/uninstall.ps1) } -Purge"'
Write-Host ''
