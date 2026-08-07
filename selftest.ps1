<#
.SYNOPSIS
    Self-test for the QH Claude Code telemetry hook (native Windows).

.DESCRIPTION
    The suite itself lives in selftest.py so that Windows, macOS, and Linux all
    run exactly the same checks. This wrapper just finds a Python 3.8+ and hands
    off to it.

    Runs entirely in a temp directory, sends no network traffic, and touches
    nothing in your real %USERPROFILE%\.claude or .qh-claude-telemetry.

.NOTES
    If Windows blocks the script, run it as:
        powershell -ExecutionPolicy Bypass -File .\selftest.ps1
#>

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$global:LASTEXITCODE = 0

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
if (-not $Py) {
    Write-Host "ERROR: Python 3.8+ is required but was not found."
    Write-Host "  Install it with:  winget install Python.Python.3.12"
    exit 1
}

# Continue, not Stop: the suite deliberately provokes failures in child
# processes and prints to stderr, and under Stop that can raise a terminating
# NativeCommandError in Windows PowerShell 5.1 before the run finishes.
$ErrorActionPreference = "Continue"
& $Py (Join-Path $ScriptDir "selftest.py") @args
exit $LASTEXITCODE
