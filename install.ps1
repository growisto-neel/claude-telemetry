<#
.SYNOPSIS
    Qualified Health - Claude Code telemetry installer (native Windows).

.DESCRIPTION
    Installs a hook that records Claude Code usage (who, what prompt, which
    skill, which folder) and ships it to QH analytics.

    Safe to re-run: it replaces its own hook entries and leaves any other
    hooks you have configured untouched.

    This is the Windows counterpart to install.sh. Both are thin front ends
    over tools\qh_configure.py, which owns the two operations that can damage
    a file you care about: writing config.json and merging hook entries into
    settings.json. There is deliberately only one implementation of that
    logic, because a second one written in PowerShell would be the untested
    one.

.EXAMPLE
    .\install.ps1
    Interactive install.

.EXAMPLE
    .\install.ps1 -NonInteractive -Ga4MeasurementId G-XXXX -Ga4ApiSecret SECRET
    Scripted / MDM rollout.

.EXAMPLE
    .\install.ps1 -PromptCapture hash -PathCapture basename
    Install with the strictest privacy dials.

.NOTES
    If Windows blocks the script, run it as:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

[CmdletBinding()]
param(
    [string]$CollectorUrl = "",
    [string]$CollectorToken = "",
    [string]$Ga4MeasurementId = "",
    [string]$Ga4ApiSecret = "",
    [string]$Email = "",
    [string]$Team = "",
    [ValidateSet("preview", "hash")]
    [string]$PromptCapture = "preview",
    [ValidateSet("full", "basename", "none")]
    [string]$PathCapture = "full",
    [switch]$NonInteractive
)

# Stop on PowerShell errors. Native executables do not raise, so every external
# call below is checked against $LASTEXITCODE explicitly.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Seeded because Set-StrictMode treats a never-yet-set automatic variable as an
# error, and $LASTEXITCODE does not exist until the first native command runs.
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

$HookSrc    = Join-Path $ScriptDir "hooks\qh_telemetry_hook.py"
$Configure  = Join-Path $ScriptDir "tools\qh_configure.py"
$HookDest   = Join-Path $BaseDir "qh_telemetry_hook.py"
$ConfigPath = Join-Path $BaseDir "config.json"

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

function Resolve-Python {
    <#
        Find a Python 3.8+ interpreter and return its real executable path.

        Candidates are (command, leading-args) pairs because the `py` launcher
        is the most reliable way to find Python on Windows but is not itself an
        interpreter path. Whatever answers, we ask it for sys.executable and use
        that from then on: the path is embedded in settings.json as part of the
        hook command, and "py -3 ..." there would break the moment the launcher
        is not on PATH for whichever process runs the hook.

        `python` on a stock Windows install is often the Microsoft Store stub,
        which prints an advert and exits non-zero. The version probe rejects it
        without needing a special case.
    #>
    # Native tools legitimately write to stderr while we probe them; under the
    # script-level Stop preference that would abort the search on the first
    # candidate that is not installed.
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
    Write-Host @"
ERROR: Python 3.8+ is required but was not found.

  Install it from https://www.python.org/downloads/windows/ and tick
  "Add python.exe to PATH", or run:  winget install Python.Python.3.12

  If you just installed Python, open a new terminal so PATH picks it up.
"@
    exit 1
}

foreach ($required in @($HookSrc, $Configure)) {
    if (-not (Test-Path -LiteralPath $required)) {
        Write-Host "ERROR: cannot find $required"
        Write-Host "Run this script from inside a complete checkout of qh-claude-telemetry."
        exit 1
    }
}

function Invoke-PyRaw {
    <#
        Run the interpreter, echo whatever it says, and hand back its exit code.

        The local Continue preference matters more than it looks. In Windows
        PowerShell 5.1, a native command writing to stderr while the script-level
        preference is Stop can surface as a terminating NativeCommandError, which
        would abort with a red trace *before* the caller's exit-code check runs.
        qh_configure reports its failures on stderr, so without this the careful
        error messages below would never be reached.
    #>
    $ErrorActionPreference = "Continue"
    & $Py @args 2>&1 | ForEach-Object { Write-Host $_ }
    return $LASTEXITCODE
}

function Invoke-Py {
    <# Run the interpreter and fail loudly if it does. #>
    $code = Invoke-PyRaw @args
    if ($code -ne 0) {
        throw ("python exited with code {0}: {1}" -f $code, ($args -join " "))
    }
}

# ---------------------------------------------------------------------------
# disclosure - employees see exactly what is collected before anything runs
# ---------------------------------------------------------------------------

# Kept in step with install.sh word for word. If the two ever disagree, an
# employee's understanding of what is collected would depend on which operating
# system they happen to use, which is not acceptable.
Write-Host @"
------------------------------------------------------------------
 Qualified Health - Claude Code usage telemetry
------------------------------------------------------------------
This records, for each Claude Code session on this machine:

   * your work email address
   * the FIRST 100 CHARACTERS of each prompt you send to Claude,
     with common secret shapes scrubbed, plus how long the prompt
     was in characters and words
   * which skill or subagent was invoked
   * the folder path / repo you were working in
   * session start & end, model, and timestamps

The full text of your prompts is never recorded or transmitted.
Nothing longer than 100 characters of prompt text is stored anywhere.

It does NOT record Claude's responses, your file contents, your
keystrokes, your terminal output, or anything outside Claude Code.

You can turn it off at any time:   setx QH_TELEMETRY 0
Local log of everything sent:      $BaseDir
------------------------------------------------------------------
"@

# A redirected or non-console host would read EOF from Read-Host and fail in a
# confusing way, so fall back to non-interactive exactly as install.sh's
# `[[ ! -t 0 ]]` does. UserInteractive alone is not enough: it only reports
# whether we are a Windows service, and stays true when stdin is a pipe, which
# is exactly the MDM / `Get-Content script | powershell` case.
$Interactive = (-not $NonInteractive) -and
               [Environment]::UserInteractive -and
               (-not [Console]::IsInputRedirected)

if ($Interactive) {
    $reply = Read-Host "Install? [y/N]"
    if ("$reply" -notmatch '^[yY]') {
        Write-Host "Aborted."
        exit 0
    }
}

# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

if ([string]::IsNullOrWhiteSpace($Email)) {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $detected = (& git config --get user.email 2>&1 | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($detected)) {
                $Email = "$detected".Trim()
            }
        } catch {
            # No git identity configured is normal; fall through to the prompt.
        } finally {
            $ErrorActionPreference = $previous
        }
    }
}
if ($Interactive) {
    $shown = if ([string]::IsNullOrWhiteSpace($Email)) { "none detected" } else { $Email }
    $entered = Read-Host "Work email [$shown]"
    if (-not [string]::IsNullOrWhiteSpace($entered)) { $Email = "$entered".Trim() }
}
if ([string]::IsNullOrWhiteSpace($Email)) {
    Write-Warning "No email resolved; events will fall back to OS username@hostname."
}

# ---------------------------------------------------------------------------
# install files
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsPath) | Out-Null

Copy-Item -LiteralPath $HookSrc -Destination $HookDest -Force
# Keep a copy beside the hook so uninstall.ps1 still works after the checkout
# this was installed from has been deleted or moved.
Copy-Item -LiteralPath $Configure -Destination (Join-Path $BaseDir "qh_configure.py") -Force

# Config values go through the environment, never as command-line arguments: on
# Windows as on Linux the command line of a running process is readable by other
# accounts, and one of these values is the GA4 api_secret. Cleared immediately
# afterwards so nothing is left in this shell's environment.
$env:COLLECTOR_URL      = $CollectorUrl
$env:COLLECTOR_TOKEN    = $CollectorToken
$env:GA4_MEASUREMENT_ID = $Ga4MeasurementId
$env:GA4_API_SECRET     = $Ga4ApiSecret
$env:USER_EMAIL         = $Email
$env:TEAM               = $Team
$env:PROMPT_CAPTURE     = $PromptCapture
$env:PATH_CAPTURE       = $PathCapture
try {
    Invoke-Py $Configure write-config --config $ConfigPath --hook $HookDest
} finally {
    foreach ($name in @("COLLECTOR_URL", "COLLECTOR_TOKEN", "GA4_MEASUREMENT_ID",
                        "GA4_API_SECRET", "USER_EMAIL", "TEAM",
                        "PROMPT_CAPTURE", "PATH_CAPTURE")) {
        Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# merge hooks into settings.json (idempotent, backed up, non-destructive)
# ---------------------------------------------------------------------------

Invoke-Py $Configure install-hooks --settings $SettingsPath --hook $HookDest --python $Py

# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Verifying with a synthetic event..."
if ((Invoke-PyRaw $Configure verify --hook $HookDest --python $Py) -ne 0) {
    Write-Warning "Verification did not pass. Check $BaseDir\telemetry.log."
}

Write-Host ""
Invoke-PyRaw $HookDest --status | Out-Null

Write-Host @"

Installed. Hooks take effect in newly started Claude Code sessions.

  Check status:   & "$Py" "$HookDest" --status
  Dry-run event:  & "$Py" "$HookDest" --test
  Opt out:        setx QH_TELEMETRY 0     (then open a new terminal)
  Remove:         .\uninstall.ps1
"@
