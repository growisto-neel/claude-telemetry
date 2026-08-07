<#
.SYNOPSIS
    Removes the QH Claude Code telemetry hook (native Windows).

.DESCRIPTION
    Unwires the telemetry hook from settings.json and, with -Purge, deletes the
    local data directory. Any other hooks you have configured are left exactly
    as they were.

.EXAMPLE
    .\uninstall.ps1
    Unwire the hooks, keep the local spool and log.

.EXAMPLE
    .\uninstall.ps1 -Purge
    Unwire the hooks and delete %USERPROFILE%\.qh-claude-telemetry.

.NOTES
    If Windows blocks the script, run it as:
        powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#>

[CmdletBinding()]
param(
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$global:LASTEXITCODE = 0

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

$UserHome     = Get-EnvOrDefault "USERPROFILE" $HOME
$BaseDir      = Get-EnvOrDefault "QH_TELEMETRY_DIR"     (Join-Path $UserHome ".qh-claude-telemetry")
$SettingsPath = Get-EnvOrDefault "CLAUDE_SETTINGS_PATH" (Join-Path $UserHome ".claude\settings.json")

# Prefer the checkout's copy, fall back to the one install.ps1 left beside the
# hook, so removal still works if the checkout is gone.
$Configure = Join-Path $ScriptDir "tools\qh_configure.py"
if (-not (Test-Path -LiteralPath $Configure)) {
    $Configure = Join-Path $BaseDir "qh_configure.py"
}

function Resolve-Python {
    $ErrorActionPreference = "Continue"
    $probe = 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'
    $candidates = @(
        [pscustomobject]@{ Exe = "py";      Lead = @("-3") }
        [pscustomobject]@{ Exe = "python3"; Lead = @() }
        [pscustomobject]@{ Exe = "python";  Lead = @() }
    )
    foreach ($candidate in $candidates) {
        $exe  = $candidate.Exe
        $lead = $candidate.Lead
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $exe @lead -c $probe 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { continue }
            $resolved = (& $exe @lead -c "import sys; print(sys.executable)" 2>&1 |
                         Select-Object -First 1)
            if ($LASTEXITCODE -ne 0) { continue }
            if ([string]::IsNullOrWhiteSpace($resolved)) { continue }
            $resolved = "$resolved".Trim()
            if (Test-Path -LiteralPath $resolved) { return $resolved }
        } catch {
            continue
        }
    }
    return $null
}

$Py = Resolve-Python

if ((-not $Py) -or (-not (Test-Path -LiteralPath $Configure))) {
    # Nobody should ever be stuck with telemetry they cannot switch off, so if
    # the automatic path is unavailable, say exactly what to delete by hand.
    Write-Warning "Automatic removal is unavailable (python or qh_configure.py not found)."
    Write-Host "1. Open $SettingsPath and delete any hook entry whose command mentions qh_telemetry_hook.py."
    Write-Host "2. Delete $BaseDir if you also want the local data gone."
    Write-Host "Until then you can disable it immediately with:  setx QH_TELEMETRY 0"
    exit 1
}

function Invoke-PyRaw {
    <#
        Echo whatever python says and hand back its exit code.

        The local Continue preference is what keeps the friendly failure path
        below reachable: under the script-level Stop preference, a native
        command writing to stderr can raise a terminating NativeCommandError in
        Windows PowerShell 5.1, and qh_configure reports its failures on stderr.
    #>
    $ErrorActionPreference = "Continue"
    & $Py @args 2>&1 | ForEach-Object { Write-Host $_ }
    return $LASTEXITCODE
}

if ((Invoke-PyRaw $Configure remove-hooks --settings $SettingsPath) -ne 0) {
    Write-Warning "Removal reported an error. settings.json was backed up before any change."
    Write-Host "You can disable telemetry immediately with:  setx QH_TELEMETRY 0"
    exit 1
}

if ($Purge) {
    if (Test-Path -LiteralPath $BaseDir) {
        Remove-Item -LiteralPath $BaseDir -Recurse -Force
        Write-Host "Deleted $BaseDir"
    } else {
        Write-Host "No local data at $BaseDir."
    }
} else {
    Write-Host "Left local data in $BaseDir (re-run with -Purge to delete it)."
}

Write-Host "Done. Restart any open Claude Code sessions."
