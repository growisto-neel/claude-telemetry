#!/usr/bin/env python3
"""
Growisto - Claude Code usage telemetry hook.

Reads a Claude Code hook event from stdin, records it, and ships it to
a collector and/or GA4. Designed so that it can NEVER break, slow down,
or block an employee's Claude Code session:

  - all work is wrapped in try/except and the process always exits 0
  - the hook itself only appends one line to a local spool file (sub-ms)
  - network I/O happens in a detached background process
  - if the network is down, events stay spooled and are sent next time

Usage (wired up by the plugin's hooks/hooks.json, via bin/growisto-hook):
    growisto_telemetry_hook.py            # hook mode, event JSON on stdin
    growisto_telemetry_hook.py --flush    # background sender
    growisto_telemetry_hook.py --status   # human-readable diagnostics
    growisto_telemetry_hook.py --test     # send a synthetic event, print result
    growisto_telemetry_hook.py --secure P # lock file P down to the current user

Runs on Linux, macOS, and Windows against Python 3.8+ with no third-party
dependencies. The platform section below covers the three places where Windows
genuinely differs; everything else is portable stdlib.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

HOME = os.path.expanduser("~")
BASE_DIR = os.environ.get("GROWISTO_TELEMETRY_DIR", os.path.join(HOME, ".growisto-claude-telemetry"))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SPOOL_PATH = os.path.join(BASE_DIR, "spool.ndjson")
LOCK_PATH = os.path.join(BASE_DIR, "flush.lock")
LOG_PATH = os.path.join(BASE_DIR, "telemetry.log")
CLIENT_ID_PATH = os.path.join(BASE_DIR, "client_id")

GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"
GA4_DEBUG_ENDPOINT = "https://www.google-analytics.com/debug/mp/collect"

# Hard caps so a spool can never grow without bound or wedge a laptop.
MAX_SPOOL_BYTES = 5 * 1024 * 1024      # 5 MB
MAX_EVENT_AGE_SECONDS = 48 * 3600      # 48h, safely inside GA4's 72h cutoff
MAX_BATCH = 25                         # GA4 allows max 25 events per request
HTTP_TIMEOUT = 5
MAX_FLUSH_SECONDS = 300                # total budget for one background flush
# Only treated as stale if the recorded pid is also gone. Must comfortably
# exceed MAX_FLUSH_SECONDS so a slow-but-healthy flush is never interrupted.
LOCK_STALE_SECONDS = 900

# GA4 truncates any event parameter value at 100 characters.
GA4_PARAM_MAX = 100

IS_WINDOWS = os.name == "nt"

# Windows refuses to rename or unlink a file another process has open, and
# returns a sharing violation rather than blocking. Every such operation here is
# retried briefly instead of being treated as a hard failure.
SHARE_RETRIES = 8
SHARE_RETRY_SLEEP = 0.02

# os.open on Windows defaults to text mode, which would translate every "\n" we
# write into "\r\n" and give the spool different bytes on different platforms.
# O_BINARY does not exist on POSIX, where the behaviour is already what we want.
APPEND_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)


# --------------------------------------------------------------------------
# platform
#
# Three things behave differently enough on Windows to need their own path:
# testing whether a process is alive, detaching a background child, and
# renaming a file that something else may have open. Everything else in this
# file is portable stdlib.
# --------------------------------------------------------------------------

def process_alive(pid):
    """
    True if `pid` is a live process.

    Deliberately does NOT use os.kill(pid, 0) on Windows. There, os.kill with
    any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT calls TerminateProcess,
    so the POSIX idiom for "is this alive?" would actually kill the process --
    and since Windows recycles pids aggressively, a stale lock file could point
    at an unrelated process belonging to the user. Telemetry must never do that.
    """
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user
        except Exception:
            return True  # unknown: assume alive, which is the safe direction

    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, ctypes.c_uint(pid)
        )
        if not handle:
            # ERROR_ACCESS_DENIED (5) means the process exists but is not ours.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # ctypes unavailable or the call failed: assume alive so we never steal
        # a lock we cannot reason about.
        return True


def detached_popen_kwargs():
    """
    Keyword arguments that fully detach a child process on this platform.

    start_new_session is POSIX-only; passing it on Windows is silently ignored,
    which would leave the sender tied to the Claude Code console and flash a
    window on every prompt.
    """
    if not IS_WINDOWS:
        return {"start_new_session": True}
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    return {
        "creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    }


def no_window_kwargs():
    """
    Stop a child process from flashing a console window on Windows.

    The flusher runs detached with no console of its own, so any git or icacls
    subprocess it starts would otherwise allocate and briefly show one. On a
    machine where someone prompts Claude fifty times a day that is fifty visible
    flickers, which is exactly the kind of thing that gets telemetry uninstalled.
    """
    if not IS_WINDOWS:
        return {}
    return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW


def secure_file(path):
    """
    Restrict a file to its owner. Returns a human-readable description of what
    was actually applied, because the guarantee differs by platform.

    chmod 600 is meaningless on Windows -- the mode bits only toggle the
    read-only attribute and do nothing about who can read the file. The config
    holds the GA4 api_secret in direct mode, so on Windows we drop inherited
    ACEs and grant the current user alone, which is the real equivalent.
    """
    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o600)
            return "chmod 600"
        except OSError as exc:
            return "chmod failed: %s" % exc

    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    principal = ("%s\\%s" % (domain, user)) if domain and user else user
    if not principal:
        return "no ACL applied: could not determine current user"
    try:
        proc = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % principal],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
            **no_window_kwargs()
        )
        if proc.returncode == 0:
            return "ACL restricted to %s" % principal
        return "icacls failed (%d): %s" % (
            proc.returncode, proc.stdout.decode("utf-8", "replace").strip()[:200]
        )
    except Exception as exc:
        return "icacls unavailable: %s" % exc


def retry_on_sharing_violation(fn):
    """
    Run `fn`, retrying briefly on the Windows "file in use" errors.

    On POSIX this is a single call: renaming and unlinking an open file are both
    legal. On Windows a concurrent hook appending to the spool makes either fail
    with PermissionError, and the correct response is to wait rather than to
    treat the spool as unreadable.
    """
    last = None
    for attempt in range(SHARE_RETRIES):
        try:
            return fn()
        except PermissionError as exc:
            last = exc
        except OSError as exc:
            # ERROR_SHARING_VIOLATION (32) / ERROR_LOCK_VIOLATION (33)
            if getattr(exc, "winerror", None) not in (32, 33):
                raise
            last = exc
        time.sleep(SHARE_RETRY_SLEEP * (attempt + 1))
    raise last


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    cfg = {}
    try:
        # Every reader and writer in this file pins UTF-8. Left to the default,
        # Python would use the locale encoding, which is cp1252 on a stock
        # Windows install -- so a non-ASCII team name, folder path or prompt
        # preview would round-trip cleanly on macOS and be mangled on Windows.
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass

    # Environment always wins, so ops can override without touching files.
    env_map = {
        "collector_url": "GROWISTO_TELEMETRY_COLLECTOR_URL",
        "collector_token": "GROWISTO_TELEMETRY_COLLECTOR_TOKEN",
        "ga4_measurement_id": "GROWISTO_TELEMETRY_GA4_MEASUREMENT_ID",
        "ga4_api_secret": "GROWISTO_TELEMETRY_GA4_API_SECRET",
        "user_email": "GROWISTO_TELEMETRY_USER_EMAIL",
        "team": "GROWISTO_TELEMETRY_TEAM",
        "prompt_capture": "GROWISTO_TELEMETRY_PROMPT_CAPTURE",
        "path_capture": "GROWISTO_TELEMETRY_PATH_CAPTURE",
        "debug": "GROWISTO_TELEMETRY_DEBUG",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val

    # Both of these are privacy dials, and neither source that sets them can
    # validate them: plugin.json's userConfig has no enum support, and an
    # environment variable is free text by definition. So normalise here, and
    # fail *closed* -- an absent value keeps the documented default, but a value
    # that was clearly meant to be something and came out wrong ("Hash",
    # "basename ", "nonw") resolves to the most private option rather than
    # quietly widening capture beyond what the person asked for.
    cfg["prompt_capture"] = _pick(cfg.get("prompt_capture"),
                                  allowed=("preview", "hash"),
                                  absent="preview", invalid="hash")
    cfg["path_capture"] = _pick(cfg.get("path_capture"),
                                allowed=("full", "basename", "none"),
                                absent="full", invalid="none")
    return cfg


def _pick(value, allowed, absent, invalid):
    """Resolve a free-text capture-mode setting to one of `allowed`."""
    if value is None:
        return absent
    normalised = str(value).strip().lower()
    if not normalised:
        return absent
    if normalised in allowed:
        return normalised
    return invalid


def telemetry_disabled():
    """Employee opt-out. Any of these switches turns the hook into a no-op."""
    for var in ("GROWISTO_TELEMETRY", "GROWISTO_TELEMETRY_ENABLED"):
        if os.environ.get(var, "").strip().lower() in ("0", "false", "off", "no"):
            return True
    if os.environ.get("GROWISTO_TELEMETRY_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.path.exists(os.path.join(BASE_DIR, "DISABLED")):
        return True
    return False


def log(msg):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

IDENTITY_CACHE_PATH = os.path.join(BASE_DIR, "identity_cache.json")


def _os_identity():
    try:
        import getpass
        import socket
        return "%s@%s" % (getpass.getuser(), socket.gethostname()), "os"
    except Exception:
        return "unknown", "none"


def resolve_user_email(cfg, allow_subprocess=True):
    """
    Resolve the employee's identity, most-trusted source first.

    install.sh writes the email into config.json, so the common path is a plain
    dict lookup with no subprocess. The git fallback is cached on disk because
    this runs on the synchronous hook path and must not spawn a process per
    prompt.
    """
    if cfg.get("user_email"):
        return cfg["user_email"].strip().lower(), "config"

    try:
        with open(IDENTITY_CACHE_PATH, encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("email"):
            return cached["email"], cached.get("source", "cache")
    except Exception:
        pass

    if not allow_subprocess:
        return _os_identity()

    email, source = None, None
    try:
        out = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True, text=True, timeout=2,
            **no_window_kwargs()
        )
        candidate = (out.stdout or "").strip().lower()
        if "@" in candidate:
            email, source = candidate, "git"
    except Exception:
        pass

    if email:
        # Only a real git email is cached. Caching the user@hostname fallback
        # would poison the cache: the short-circuit above would then prefer it
        # forever and the actual git email would never be discovered.
        try:
            os.makedirs(BASE_DIR, exist_ok=True)
            with open(IDENTITY_CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump({"email": email, "source": source}, fh)
        except Exception:
            pass
        return email, source

    return _os_identity()


def stable_client_id():
    """One random ID per install. Used as GA4 client_id, not tied to identity."""
    try:
        with open(CLIENT_ID_PATH, encoding="utf-8") as fh:
            cid = fh.read().strip()
            if cid:
                return cid
    except Exception:
        pass
    cid = str(uuid.uuid4())
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CLIENT_ID_PATH, "w", encoding="utf-8") as fh:
            fh.write(cid)
    except Exception:
        pass
    return cid


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

# Deliberately conservative: these patterns catch the credential shapes that
# most often end up pasted into a prompt. This is damage limitation, not a
# guarantee. See PRIVACY_NOTICE.md.
_REDACTIONS = [
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CARD_NUMBER]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
]


def redact(text):
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        try:
            text = pattern.sub(replacement, text)
        except Exception:
            pass
    return text


def shape_prompt(prompt, mode):
    """
    Return the prompt preview, or None.

    There is deliberately no mode that returns full prompt text. The most that
    is ever retained anywhere — GA4 or warehouse — is GA4_PARAM_MAX characters,
    scrubbed of the common secret shapes. `prompt_chars` on the event records
    how long the real prompt was, so you can still see that there was more.

        preview  first 100 chars + length, word count, and hash (default)
        hash     length, word count, and hash only; no text at all
    """
    prompt = prompt or ""
    if mode == "hash" or not prompt:
        return None
    return redact(prompt)[:GA4_PARAM_MAX]


# --------------------------------------------------------------------------
# event construction
# --------------------------------------------------------------------------

# The PreToolUse tools worth an event. Keep in step with the `matcher` in
# hooks/hooks.json -- the matcher decides whether this process runs at all, and
# this set decides whether the event survives. A tool missing from either one
# produces silence, not an error.
SKILL_TOOLS = ("Skill", "Task", "Agent", "SlashCommand")

EVENT_NAME_MAP = {
    "UserPromptSubmit": "cc_prompt",
    "SessionStart": "cc_session_start",
    "SessionEnd": "cc_session_end",
    "PreToolUse": "cc_skill",
    "UserPromptExpansion": "cc_slash_command",
}


REPO_CACHE_PATH = os.path.join(BASE_DIR, "repo_cache.json")


def git_repo_name(cwd):
    """
    Resolve the repo name for a directory. Called only from the background
    flusher, never from the hook path, and memoised on disk so a given
    directory costs one `git` invocation ever.
    """
    if not cwd:
        return None
    cache = {}
    try:
        with open(REPO_CACHE_PATH, encoding="utf-8") as fh:
            cache = json.load(fh)
        if cwd in cache:
            return cache[cwd] or None
    except Exception:
        cache = {}

    repo = None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
            **no_window_kwargs()
        )
        top = (out.stdout or "").strip()
        if top:
            repo = os.path.basename(top)
    except Exception:
        pass

    try:
        if len(cache) > 500:
            cache = {}
        cache[cwd] = repo or ""
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(REPO_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass
    return repo


def extract_skill_name(payload):
    """
    Claude Code has no dedicated skill hook event. A skill invocation shows up
    as PreToolUse with tool_name == "Skill" (and Task for subagents), with the
    skill identifier inside tool_input.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("skill", "skill_name", "name", "command", "subagent_type", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:120]
    return None


def local_utc_offset_minutes():
    """Minutes east of UTC, DST-aware."""
    try:
        if time.daylight and time.localtime().tm_isdst:
            return -int(time.altzone // 60)
        return -int(time.timezone // 60)
    except Exception:
        return 0


def build_event(payload, cfg, allow_subprocess=True):
    hook_event = payload.get("hook_event_name") or "Unknown"
    tool_name = payload.get("tool_name")

    # Only forward the tool events that tell us something about skill adoption.
    # `Agent` and `Task` are the same thing under two names: Claude Code renamed
    # the subagent tool, and which name a given install sends depends on its
    # version, so both are listed rather than guessing which one you have.
    if hook_event == "PreToolUse" and tool_name not in SKILL_TOOLS:
        # Every other tool is dropped on purpose -- see README, "Known
        # limitations". Record the name locally anyway: if a future Claude Code
        # renames these tools again, telemetry goes quiet with no error, and
        # this log line is the only thing that would say why.
        log("dropped PreToolUse for tool_name=%r" % (tool_name,))
        return None

    name = EVENT_NAME_MAP.get(hook_event)
    if not name:
        return None

    email, id_source = resolve_user_email(cfg, allow_subprocess=allow_subprocess)
    cwd = payload.get("cwd") or os.getcwd()
    prompt = payload.get("prompt") or ""
    prompt_preview = shape_prompt(prompt, cfg.get("prompt_capture", "preview"))

    # `none` has to suppress folder_name as well as folder_path. The directory
    # name on its own is still a path fragment, it is still mapped into GA4, and
    # an operator who chose `none` asked for no location at all.
    path_mode = cfg.get("path_capture", "full")
    folder_name = os.path.basename(cwd) if cwd else None
    if path_mode == "none":
        folder_path = None
        folder_name = None
    elif path_mode == "basename":
        folder_path = folder_name
    else:
        folder_path = cwd

    event = {
        "schema_version": 1,
        "event_name": name,
        "hook_event_name": hook_event,
        "ts_ms": int(time.time() * 1000),
        "tz_offset_min": local_utc_offset_minutes(),

        # identity
        "user_email": email,
        "user_email_sha256": sha256_hex(email),
        "user_id_source": id_source,
        "team": cfg.get("team"),
        "client_id": stable_client_id(),

        # location. `repo` is filled in later by the background flusher so the
        # hook path never shells out to git; it resolves the repo from
        # folder_path, which means repo detection only works in `full` path
        # mode. That is deliberate — carrying the real cwd in a side field so
        # the flusher could use it would write the full path into the spool
        # even when the operator configured path_capture to suppress it.
        "folder_path": folder_path,
        "folder_name": folder_name,

        # session
        "session_id": payload.get("session_id"),
        "session_source": payload.get("source"),
        "session_end_reason": payload.get("reason"),
        "model": payload.get("model"),
        "permission_mode": payload.get("permission_mode"),
        "agent_id": payload.get("agent_id"),

        # skill / tool
        "skill": extract_skill_name(payload) if hook_event == "PreToolUse" else None,
        "tool_name": tool_name,

        # prompt. There is no full-text field by design; prompt_chars is what
        # tells you the real prompt was longer than the preview.
        "prompt_preview": prompt_preview,
        "prompt_chars": len(prompt),
        "prompt_words": len(prompt.split()) if prompt else 0,
        "prompt_sha256": sha256_hex(prompt) if prompt else None,

        # environment
        "os": sys.platform,
        "hook_version": "1.0.0",
    }
    return {k: v for k, v in event.items() if v is not None and v != ""}


# --------------------------------------------------------------------------
# GA4 mapping
# --------------------------------------------------------------------------

# GA4 allows 25 params per event and truncates every value at 100 chars, so we
# send a deliberately small, dashboard-friendly projection of the event here.
def to_ga4_payload(event, cfg):
    def clip(val):
        if val is None:
            return None
        return str(val)[:GA4_PARAM_MAX]

    params = {
        "engagement_time_msec": 1,
        # Deliberately NOT named `session_id`: that param drives GA4's own
        # session stitching, and a Claude session UUID would distort GA4's
        # session counts. Keep Claude's session as its own dimension.
        "cc_session_id": clip(event.get("session_id")),
        "user_email": clip(event.get("user_email")),
        "team": clip(event.get("team")),
        "folder_name": clip(event.get("folder_name")),
        # tail of the path is the informative half once it exceeds 100 chars
        "folder_path": (event.get("folder_path") or "")[-GA4_PARAM_MAX:] or None,
        "repo": clip(event.get("repo")),
        "skill": clip(event.get("skill")),
        "tool_name": clip(event.get("tool_name")),
        "model": clip(event.get("model")),
        "session_source": clip(event.get("session_source")),
        "permission_mode": clip(event.get("permission_mode")),
        "prompt_preview": clip(event.get("prompt_preview")),
        "prompt_chars": event.get("prompt_chars"),
        "prompt_words": event.get("prompt_words"),
        "prompt_hash": clip((event.get("prompt_sha256") or "")[:16]) or None,
        "os": clip(event.get("os")),
        "hook_version": clip(event.get("hook_version")),
    }
    params = {k: v for k, v in params.items() if v is not None and v != ""}

    return {
        "client_id": event.get("client_id") or stable_client_id(),
        "user_id": event.get("user_email"),
        "timestamp_micros": int(event.get("ts_ms", time.time() * 1000)) * 1000,
        "non_personalized_ads": True,
        "events": [{"name": event["event_name"], "params": params}],
    }


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def http_post_json(url, body, headers=None, timeout=HTTP_TIMEOUT):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)


def has_destination(cfg):
    return bool(cfg.get("collector_url")) or bool(
        cfg.get("ga4_measurement_id") and cfg.get("ga4_api_secret")
    )


def send_to_collector(events, cfg):
    url = cfg.get("collector_url")
    if not url:
        return True  # nothing configured, nothing to fail
    headers = {}
    if cfg.get("collector_token"):
        headers["Authorization"] = "Bearer %s" % cfg["collector_token"]
    status, body = http_post_json(url, {"events": events}, headers)
    ok = 200 <= status < 300
    if not ok:
        log("collector failed status=%s body=%s" % (status, body[:200]))
    return ok


def send_to_ga4(events, cfg, debug=False):
    mid = cfg.get("ga4_measurement_id")
    secret = cfg.get("ga4_api_secret")
    if not mid or not secret:
        return True  # direct GA4 not configured (collector-forward mode)
    endpoint = GA4_DEBUG_ENDPOINT if debug else GA4_ENDPOINT
    url = "%s?measurement_id=%s&api_secret=%s" % (endpoint, mid, secret)
    all_ok = True
    for event in events:
        status, body = http_post_json(url, to_ga4_payload(event, cfg))
        if debug:
            print("GA4 %s -> %s %s" % (event["event_name"], status, body))
        if not (200 <= status < 300):
            all_ok = False
            log("ga4 failed status=%s body=%s" % (status, body[:200]))
    return all_ok


# --------------------------------------------------------------------------
# spool
# --------------------------------------------------------------------------

def append_lines(path, events):
    """
    Append events to an ndjson file as one write per event.

    Opened as a raw binary append-mode descriptor and written as bytes, for two
    reasons. O_BINARY stops Windows translating every "\\n" into "\\r\\n", which
    would give the same spool file different bytes on different platforms for no
    benefit. And a single os.write of a complete line is the closest thing to an
    atomic append available without taking a lock, which matters when two Claude
    Code sessions emit at the same moment.

    The residual risk is that two interleaved writes corrupt one line. The spool
    reader skips lines it cannot parse, so the failure mode is losing a single
    event rather than losing the file.
    """
    payload = b"".join(
        json.dumps(e, separators=(",", ":")).encode("utf-8") + b"\n" for e in events
    )
    if not payload:
        return
    fd = retry_on_sharing_violation(lambda: os.open(path, APPEND_FLAGS, 0o600))
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def spool_append(event):
    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        if os.path.getsize(SPOOL_PATH) > MAX_SPOOL_BYTES:
            retry_on_sharing_violation(
                lambda: os.replace(SPOOL_PATH, SPOOL_PATH + ".dropped")
            )
            log("spool exceeded %d bytes; rotated to spool.ndjson.dropped "
                "(previous .dropped overwritten)" % MAX_SPOOL_BYTES)
    except OSError:
        pass
    append_lines(SPOOL_PATH, [event])


def spawn_flush():
    """Detach a background sender so the hook returns immediately."""
    devnull = None
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--flush"],
            stdin=devnull, stdout=devnull, stderr=devnull,
            close_fds=True,
            **detached_popen_kwargs()
        )
    except Exception as exc:
        log("spawn_flush failed: %s" % exc)
    finally:
        # The child holds its own duplicate of these handles. Leaving the
        # parent's copy open leaks a descriptor per prompt, which matters on
        # Windows where the hook process is comparatively expensive already.
        if devnull is not None:
            try:
                os.close(devnull)
            except OSError:
                pass


def _lock_holder_alive():
    """True if the pid recorded in the lock file is still running."""
    try:
        with open(LOCK_PATH) as fh:
            pid = int(fh.read().strip())
    except Exception:
        return False  # unreadable/corrupt lock is treated as abandoned
    if pid <= 0 or pid == os.getpid():
        return False
    return process_alive(pid)


def _file_identity(st):
    """
    A value that changes when a path stops referring to the same file.

    st_ino is the right answer on POSIX and on NTFS, where Python exposes the
    file index. Some Windows filesystems report 0, so fall back to creation time
    and size rather than comparing 0 == 0 and concluding two different files are
    the same one.
    """
    if getattr(st, "st_ino", 0):
        return ("ino", st.st_ino, getattr(st, "st_dev", 0))
    return ("ctime", getattr(st, "st_ctime_ns", 0), st.st_size)


def acquire_lock():
    # A lock is only stale if it is BOTH old and its owner is gone. Age alone is
    # not enough: a flush against a hung network can legitimately run for a long
    # time, and stealing its lock would let a second flusher clobber the
    # in-flight .sending file.
    try:
        before = os.stat(LOCK_PATH)
        age = time.time() - before.st_mtime
        if age > LOCK_STALE_SECONDS and not _lock_holder_alive():
            # Re-stat and compare identity immediately before unlinking, so we
            # cannot delete a *fresh* lock that another flusher created in the
            # gap since the staleness decision.
            if _file_identity(os.stat(LOCK_PATH)) == _file_identity(before):
                retry_on_sharing_violation(lambda: os.unlink(LOCK_PATH))
                log("removed stale lock (age %ds, owner gone)" % age)
    except OSError:
        pass

    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except OSError:
        return False

    # Read back before proceeding: if two flushers both raced through a stale
    # steal, only the one whose pid is actually in the file continues.
    try:
        with open(LOCK_PATH) as fh:
            return int(fh.read().strip()) == os.getpid()
    except Exception:
        return False


def touch_lock():
    """Keep the lock fresh so a long but healthy flush is not judged stale."""
    try:
        os.utime(LOCK_PATH, None)
    except OSError:
        pass


def release_lock():
    try:
        retry_on_sharing_violation(lambda: os.unlink(LOCK_PATH))
    except OSError:
        pass


def enrich_inplace(event, cfg=None):
    """
    Background-only enrichment. Anything that costs a subprocess or real time
    belongs here rather than on the hook path.
    """
    cfg = cfg or {}
    try:
        # Only an absolute folder_path can be handed to git, which confines repo
        # detection to `full` path mode. In basename/none mode folder_path is a
        # bare name or absent, so repo stays null rather than the hook keeping a
        # shadow copy of the path the operator asked us not to keep.
        cwd = event.get("folder_path")
        if cwd and os.path.isabs(cwd) and not event.get("repo"):
            repo = git_repo_name(cwd)
            if repo:
                event["repo"] = repo
    except Exception:
        pass

    # The hook path cannot run `git`, so identity may have fallen back to
    # user@hostname. Upgrade it here, where a subprocess is free.
    try:
        if event.get("user_id_source") in ("os", "none"):
            email, source = resolve_user_email(cfg, allow_subprocess=True)
            if source in ("config", "git", "cache") and email:
                event["user_email"] = email
                event["user_email_sha256"] = sha256_hex(email)
                event["user_id_source"] = source
    except Exception:
        pass
    return event


def strip_internal(event):
    """Internal bookkeeping keys (`_`-prefixed) must never reach a destination."""
    return {k: v for k, v in event.items() if not k.startswith("_")}


def enrich(event, cfg=None):
    return strip_internal(enrich_inplace(event, cfg))


def recover_orphans():
    """
    Re-queue any `.sending` file left behind by a flusher that died mid-send
    (laptop sleep, SIGKILL, crash). Without this those events are stranded, and
    because the working filename is unique per flush they are never clobbered.
    """
    import glob

    recovered = 0
    for path in sorted(glob.glob(SPOOL_PATH + ".sending.*")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = [l.strip() for l in fh if l.strip()]
            if lines:
                blob = "\n".join(lines).encode("utf-8") + b"\n"
                fd = retry_on_sharing_violation(
                    lambda: os.open(SPOOL_PATH, APPEND_FLAGS, 0o600)
                )
                try:
                    os.write(fd, blob)
                finally:
                    os.close(fd)
                recovered += len(lines)
            retry_on_sharing_violation(lambda: os.unlink(path))
        except Exception as exc:
            log("orphan recovery failed for %s: %s" % (path, exc))
    if recovered:
        log("recovered %d orphaned event(s)" % recovered)


def _send_batch(batch, cfg):
    """
    Deliver a batch to each configured destination independently and record
    per-destination success on the event. A destination that already succeeded is
    never re-sent, so a failure on one leg cannot duplicate rows on the other.

    An unconfigured destination reports success, which is the intended
    semantics: the event is considered fully delivered once every destination
    that actually exists has accepted it.
    """
    to_collector = [e for e in batch if not e.get("_collector_done")]
    if to_collector and send_to_collector([strip_internal(e) for e in to_collector], cfg):
        for event in to_collector:
            event["_collector_done"] = True

    # GA4 is one HTTP request per event (the Measurement Protocol carries
    # timestamp_micros at request level, so events cannot share a request
    # without losing their real timestamps). Marked per event so that one
    # rejection does not force a resend of its neighbours.
    for event in batch:
        if event.get("_ga4_done"):
            continue
        if send_to_ga4([strip_internal(event)], cfg):
            event["_ga4_done"] = True

    return [e for e in batch
            if not (e.get("_collector_done") and e.get("_ga4_done"))]


def _requeue(events):
    """Put undelivered events back on the spool. False means nothing was written."""
    if not events:
        return True
    try:
        append_lines(SPOOL_PATH, events)
        log("requeued %d event(s)" % len(events))
        return True
    except Exception as exc:
        log("requeue failed: %s" % exc)
        return False


def _flush_once(cfg, deadline):
    """Send one spool generation. Returns True if there may be more to send."""
    try:
        if not os.path.exists(SPOOL_PATH) or os.path.getsize(SPOOL_PATH) == 0:
            return False
    except OSError:
        return False

    # Unique working name: a crashed flush leaves a recoverable file rather than
    # one that the next flush silently overwrites.
    working = "%s.sending.%d.%d" % (SPOOL_PATH, os.getpid(), int(time.time()))
    try:
        # On Windows this fails outright if a hook is mid-append, so it is
        # retried rather than treated as "nothing to send".
        retry_on_sharing_violation(lambda: os.replace(SPOOL_PATH, working))
    except OSError:
        return False

    # From here on `working` holds the only copy of these events. It is deleted
    # only after every undelivered event is safely back on the spool. On any
    # other outcome it is left in place for recover_orphans() to pick up, so a
    # failure can duplicate at worst — it can never lose data.
    try:
        events, now_ms = [], int(time.time() * 1000)
        with open(working, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if now_ms - event.get("ts_ms", now_ms) > MAX_EVENT_AGE_SECONDS * 1000:
                    continue  # too old for GA4 to accept
                events.append(enrich_inplace(event, cfg))
    except Exception as exc:
        log("spool read error: %s; left %s for recovery" % (exc, working))
        return False

    unsent = []
    try:
        for i in range(0, len(events), MAX_BATCH):
            if time.time() > deadline:
                # Out of budget: everything not yet attempted goes back on the
                # spool for the next run rather than being dropped.
                unsent.extend(events[i:])
                log("flush budget exhausted; requeuing %d event(s)" % len(events[i:]))
                break
            unsent.extend(_send_batch(events[i:i + MAX_BATCH], cfg))
            touch_lock()
    except BaseException as exc:
        # Covers SIGTERM/SIGINT mid-send as well as ordinary errors. Recompute
        # from the delivery flags rather than trusting the partially built list,
        # so nothing already delivered is counted as unsent.
        log("send interrupted: %s" % exc)
        unsent = [e for e in events
                  if not (e.get("_collector_done") and e.get("_ga4_done"))]
        if _requeue(unsent):
            _discard(working)
        return False

    if not _requeue(unsent):
        return False  # spool unwritable: keep `working` so nothing is lost
    _discard(working)
    return not unsent


def _discard(path):
    try:
        retry_on_sharing_violation(lambda: os.unlink(path))
    except OSError:
        pass


def flush():
    cfg = load_config()

    # With no destination configured, events stay spooled rather than being
    # thrown away, so a phased rollout can backfill once the endpoint is live.
    # Size and age caps still stop the spool from growing without bound.
    if not has_destination(cfg):
        log("no destination configured; leaving events spooled")
        return

    if not acquire_lock():
        return
    try:
        recover_orphans()
        deadline = time.time() + MAX_FLUSH_SECONDS
        # A second pass picks up anything appended while we were sending, so the
        # tail of a session is not left waiting for the next session's event.
        for _ in range(3):
            if time.time() > deadline or not _flush_once(cfg, deadline):
                break
    except Exception as exc:
        log("flush error: %s" % exc)
    finally:
        release_lock()


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def hook_mode():
    raw = sys.stdin.read()
    payload = json.loads(raw)
    cfg = load_config()
    # allow_subprocess=False: nothing on the synchronous hook path may shell
    # out. If identity is not in config or cache, the flusher resolves it.
    event = build_event(payload, cfg, allow_subprocess=False)
    if event is None:
        return
    spool_append(event)
    spawn_flush()


def status_mode():
    cfg = load_config()
    email, source = resolve_user_email(cfg)
    spool_lines = 0
    try:
        with open(SPOOL_PATH, encoding="utf-8", errors="replace") as fh:
            spool_lines = sum(1 for _ in fh)
    except Exception:
        pass
    print("Growisto Claude telemetry status")
    print("  platform:        %s (%s)" % (sys.platform, "windows" if IS_WINDOWS else "posix"))
    print("  python:          %s" % sys.executable)
    print("  enabled:         %s" % (not telemetry_disabled()))
    print("  config:          %s (%s)" % (CONFIG_PATH, "found" if os.path.exists(CONFIG_PATH) else "MISSING"))
    print("  identity:        %s (via %s)" % (email, source))
    print("  prompt capture:  %s" % cfg.get("prompt_capture"))
    print("  path capture:    %s" % cfg.get("path_capture"))
    print("  collector:       %s" % (cfg.get("collector_url") or "(not configured)"))
    print("  ga4 direct:      %s" % (cfg.get("ga4_measurement_id") or "(not configured)"))
    print("  pending events:  %d" % spool_lines)
    print("  log:             %s" % LOG_PATH)
    print("\nTo opt out at any time:  export GROWISTO_TELEMETRY=0")


def test_mode():
    cfg = load_config()
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "test-session-%s" % uuid.uuid4().hex[:8],
        "cwd": os.getcwd(),
        "prompt": "This is a synthetic test prompt from --test.",
        "permission_mode": "default",
    }
    event = enrich(build_event(payload, cfg), cfg)
    print("Event that would be recorded:\n")
    print(json.dumps(event, indent=2))
    print("\nGA4 payload:\n")
    print(json.dumps(to_ga4_payload(event, cfg), indent=2))
    if cfg.get("ga4_measurement_id"):
        print("\nSending to GA4 debug endpoint...")
        send_to_ga4([event], cfg, debug=True)
    if cfg.get("collector_url"):
        print("\nSending to collector...")
        print("ok" if send_to_collector([event], cfg) else "FAILED (see %s)" % LOG_PATH)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--secure":
        # Used by the installers so the ACL/chmod logic lives in one place
        # rather than being reimplemented in bash and in PowerShell.
        if len(sys.argv) < 3:
            print("usage: growisto_telemetry_hook.py --secure <path>")
            raise SystemExit(2)
        print(secure_file(sys.argv[2]))
        return
    if arg == "--status":
        status_mode()
        return
    if telemetry_disabled():
        return
    if arg == "--flush":
        flush()
    elif arg == "--test":
        test_mode()
    else:
        hook_mode()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never surface an error into the user's session
        log("fatal: %s" % exc)
    sys.exit(0)
