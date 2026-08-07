#!/usr/bin/env python3
"""
Self-test for the QH Claude Code telemetry hook. Linux, macOS, and Windows.

Runs entirely in a temp directory, sends no network traffic, and touches
nothing in your real ~/.qh-claude-telemetry. Run this before rolling out to
anyone.

    python3 selftest.py          (macOS / Linux)
    py -3 selftest.py            (Windows)

Hook registration now belongs to the plugin (hooks/hooks.json plus the
bin/qh-hook launcher), so nothing here installs, merges, or removes anything
from settings.json. What is left is the behaviour of the hook itself.

Everything here is stdlib and platform-neutral; checks that only make sense on
one platform announce themselves as SKIP on the others rather than silently
disappearing, so a Windows run and a Linux run can be compared line by line.

Twelve areas are covered: syntax, event capture, noise filtering, redaction,
capture modes, opt-out, session safety, GA4 payload shape, crash recovery,
partial failure, platform behaviour, and paths containing spaces.

Exit code is 0 only if every check passed.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

IS_WINDOWS = os.name == "nt"
REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

SELFTEST_SRC = os.path.abspath(__file__)
HOOK_SRC = os.path.join(REPO, "hooks", "qh_telemetry_hook.py")
COLLECTOR_SRC = os.path.join(REPO, "collector", "main.py")
LAUNCHER_SRC = os.path.join(REPO, "bin", "qh-hook")

# bin/qh-hook adopts several of these bare names as fallbacks for its
# QH_TELEMETRY_* equivalents. Stripped from every child environment so a
# developer who happens to have GA4_API_SECRET exported does not accidentally
# point the self-test at real analytics.
CONFIG_ENV_NAMES = (
    "COLLECTOR_URL", "COLLECTOR_TOKEN", "GA4_MEASUREMENT_ID", "GA4_API_SECRET",
    "USER_EMAIL", "TEAM", "PROMPT_CAPTURE", "PATH_CAPTURE",
)

# Windows process creation is several times more expensive than fork+exec, and
# the hook pays for it twice (itself, plus the detached flusher). The budget is
# still tight enough to catch a hook that has started doing real work inline.
LATENCY_BUDGET_MS = 900 if IS_WINDOWS else 400


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _ansi_ok():
    if not sys.stdout.isatty():
        return False
    if not IS_WINDOWS:
        return True
    # Windows 10+ consoles support VT sequences but only once asked.
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


COLOR = _ansi_ok()


def _paint(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if COLOR else text


class Report:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []

    def section(self, title):
        print("\n" + _paint("1", title))

    def ok(self, label):
        self.passed += 1
        print("  %s  %s" % (_paint("32", "PASS"), label))

    def bad(self, label):
        self.failed += 1
        self.failures.append(label)
        print("  %s  %s" % (_paint("31", "FAIL"), label))

    def skip(self, label, why):
        self.skipped += 1
        print("  %s  %s (%s)" % (_paint("33", "SKIP"), label, why))

    def check(self, label, got, want):
        if str(got) == str(want):
            self.ok(label)
        else:
            self.bad("%s (expected %r, got %r)" % (label, str(want), str(got)))

    def truth(self, label, condition):
        self.ok(label) if condition else self.bad(label)

    def summary(self):
        print("\n" + _paint("1", "%d passed, %d failed, %d skipped"
                            % (self.passed, self.failed, self.skipped)))
        if self.failed:
            print("\nFailures:")
            for failure in self.failures:
                print("  - %s" % failure)
            return 1
        print("All good. Nothing in your real ~/.qh-claude-telemetry was "
              "touched.")
        return 0


R = Report()


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------

TMP = tempfile.mkdtemp(prefix="qh-selftest-")
TEL_DIR = os.path.join(TMP, "telemetry")
HOOK = os.path.join(TEL_DIR, "qh_telemetry_hook.py")
SPOOL = os.path.join(TEL_DIR, "spool.ndjson")

# The hook resolves its paths from the environment at import time, so this must
# be set before the module is loaded further down.
os.environ["QH_TELEMETRY_DIR"] = TEL_DIR
for _name in list(os.environ):
    if _name.startswith("QH_TELEMETRY_") and _name != "QH_TELEMETRY_DIR":
        del os.environ[_name]
os.environ.pop("QH_TELEMETRY", None)


def child_env(**extra):
    """A clean environment for a child process, pointed at the temp install."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("QH_TELEMETRY") and k not in CONFIG_ENV_NAMES}
    env["QH_TELEMETRY_DIR"] = TEL_DIR
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd, **kwargs):
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("env", child_env())
    kwargs.setdefault("timeout", 120)
    return subprocess.run(cmd, **kwargs)


def emit(payload, **envextra):
    """Feed one hook event to the installed hook, exactly as Claude Code would."""
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return subprocess.run(
        [PY, HOOK], input=body,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(**envextra), timeout=60,
    )


def spool_events(path=SPOOL):
    """
    Parse the spool, skipping anything unreadable.

    Tolerant on purpose: this is polled in a loop while a hook may be mid-write,
    and a half-written final line should make the caller wait rather than make
    the self-test explode.
    """
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def spool_count(path=SPOOL):
    return len(spool_events(path))


def clear_spool():
    for path in [SPOOL] + glob.glob(SPOOL + ".sending.*"):
        try:
            os.unlink(path)
        except OSError:
            pass


def do_install(email="tester@growisto.com", team="selftest",
               base=TEL_DIR, **cfg):
    """
    Lay out a temp install the way the plugin looks at runtime: a config.json
    in BASE_DIR, next to a copy of the hook for the tests to invoke directly.

    There is no settings.json step any more. The plugin declares its own hooks
    in hooks/hooks.json, so registration is not this suite's business; what is
    left is making sure the hook reads its configuration and behaves.
    """
    os.makedirs(base, exist_ok=True)
    shutil.copy2(HOOK_SRC, os.path.join(base, "qh_telemetry_hook.py"))

    config = {
        "user_email": email,
        "team": team,
        "prompt_capture": cfg.get("prompt_capture", "preview"),
        "path_capture": cfg.get("path_capture", "full"),
    }
    for key in ("collector_url", "collector_token",
                "ga4_measurement_id", "ga4_api_secret"):
        if cfg.get(key):
            config[key] = cfg[key]

    with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    return config


def wait_for(predicate, seconds=10.0, interval=0.1):
    """Poll instead of sleeping a fixed amount: fast machines should not wait."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ===========================================================================
# 1. Syntax
# ===========================================================================

def section_syntax():
    R.section("1. Syntax")

    for label, path in (("selftest", SELFTEST_SRC),
                        ("hook", HOOK_SRC),
                        ("collector", COLLECTOR_SRC)):
        if not os.path.exists(path):
            R.bad("%s missing at %s" % (label, path))
            continue
        # compile() rather than `python -m py_compile`, which would scatter
        # __pycache__ directories through a checkout every time anyone runs the
        # self-test.
        try:
            with open(path, "rb") as fh:
                compile(fh.read(), path, "exec")
            R.ok("%s compiles" % label)
        except SyntaxError as exc:
            R.bad("%s does not compile: %s line %s" % (label, exc.msg, exc.lineno))

    # The launcher is the only shell script left, and it is what Claude Code
    # actually runs on all three platforms.
    bash = shutil.which("bash")
    if not os.path.exists(LAUNCHER_SRC):
        R.bad("bin/qh-hook is missing")
    elif not bash:
        R.skip("bin/qh-hook syntax", "no bash on this machine")
    else:
        proc = run([bash, "-n", LAUNCHER_SRC])
        R.truth("bin/qh-hook syntax", proc.returncode == 0)
        if proc.returncode != 0:
            print("      %s" % proc.stderr.decode("utf-8", "replace").strip()[:300])


# ===========================================================================
# 2-3. Event capture
# ===========================================================================

def section_capture():
    R.section("2. Event capture")

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": TMP,
          "prompt": "refactor the claims parser", "permission_mode": "default"})
    emit({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": TMP,
          "source": "startup", "model": "claude-opus-5"})
    emit({"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": TMP,
          "tool_name": "Skill",
          "tool_input": {"skill": "qh-prototypes:create-backend"}})
    emit({"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": TMP,
          "reason": "exit"})

    wait_for(lambda: spool_count() >= 4, seconds=15)
    events = spool_events()

    R.check("4 events spooled", len(events), 4)
    if len(events) < 4:
        return

    by_name = {e.get("event_name"): e for e in events}
    R.check("prompt preview captured",
            by_name.get("cc_prompt", {}).get("prompt_preview"),
            "refactor the claims parser")
    R.check("no full-text prompt field anywhere",
            sum(1 for e in events if "prompt" in e), 0)
    R.check("skill name extracted",
            by_name.get("cc_skill", {}).get("skill"),
            "qh-prototypes:create-backend")
    R.check("email from config", events[0].get("user_email"),
            "tester@growisto.com")
    R.check("team recorded", events[0].get("team"), "selftest")
    R.check("session_source recorded",
            by_name.get("cc_session_start", {}).get("session_source"), "startup")
    R.check("no internal _fields leaked",
            sum(1 for e in events for k in e if k.startswith("_")), 0)

    R.section("3. Noise is filtered out")
    before = spool_count()
    emit({"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": TMP,
          "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}})
    emit({"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": TMP,
          "tool_name": "Edit", "tool_input": {"file_path": "/secret/file"}})
    time.sleep(0.5)
    R.check("Bash/Edit tool calls not recorded", spool_count(), before)


# ===========================================================================
# 4-5. Redaction and capture modes
# ===========================================================================

def section_redaction_and_modes():
    R.section("4. Secret redaction")

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "cwd": TMP,
          "prompt": "deploy sk-abcdefghij0123456789XYZ password: hunter2trustme"})
    wait_for(lambda: spool_count() >= 1)
    preview = (spool_events() or [{}])[0].get("prompt_preview", "")
    R.truth("api key redacted", "sk-abcdefghij0123456789XYZ" not in preview)
    R.truth("password redacted", "hunter2trustme" not in preview)

    R.section("5. Capture modes")

    # The core guarantee: a long prompt is truncated to 100 characters, and
    # prompt_chars still reports the true length so you know there was more.
    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s3a", "cwd": TMP,
          "prompt": "a" * 450})
    wait_for(lambda: spool_count() >= 1)
    event = (spool_events() or [{}])[0]
    R.check("preview truncated to 100 chars", len(event.get("prompt_preview", "")), 100)
    R.check("prompt_chars reports the true length", event.get("prompt_chars"), 450)
    R.truth("no full-text field on a long prompt", "prompt" not in event)

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s3", "cwd": TMP,
          "prompt": "sensitive patient record 12345"},
         QH_TELEMETRY_PROMPT_CAPTURE="hash")
    wait_for(lambda: spool_count() >= 1)
    event = (spool_events() or [{}])[0]
    R.truth("hash mode stores no prompt text at all",
            "prompt_preview" not in event and "prompt" not in event)
    R.check("hash mode still stores length", event.get("prompt_chars"), 30)
    R.check("hash mode still stores hash", len(event.get("prompt_sha256", "")), 64)

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s4",
          "cwd": "/Users/x/src/qh-platform", "prompt": "hi"},
         QH_TELEMETRY_PATH_CAPTURE="basename")
    wait_for(lambda: spool_count() >= 1)
    R.check("basename path mode drops full path (posix input)",
            (spool_events() or [{}])[0].get("folder_path"), "qh-platform")

    # A Windows agent reports a backslash cwd. basename must still reduce it,
    # which os.path.basename only does on Windows -- so on POSIX this asserts
    # the honest outcome rather than pretending the path was understood.
    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s4b",
          "cwd": r"C:\Users\x\src\qh-platform", "prompt": "hi"},
         QH_TELEMETRY_PATH_CAPTURE="basename")
    wait_for(lambda: spool_count() >= 1)
    folder = (spool_events() or [{}])[0].get("folder_path")
    if IS_WINDOWS:
        R.check("basename path mode drops full path (windows input)",
                folder, "qh-platform")
    else:
        R.skip("basename path mode on a windows path", "only meaningful on Windows")

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s4c",
          "cwd": TMP, "prompt": "hi"},
         QH_TELEMETRY_PATH_CAPTURE="none")
    wait_for(lambda: spool_count() >= 1)
    event = (spool_events() or [{}])[0]
    R.truth("none path mode records no path at all",
            not event.get("folder_path") and not event.get("folder_name"))


# ===========================================================================
# 6-7. Opt-out and robustness
# ===========================================================================

def section_optout_and_robustness():
    R.section("6. Opt-out")

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s5", "cwd": TMP,
          "prompt": "should not be recorded"}, QH_TELEMETRY="0")
    time.sleep(0.5)
    R.check("QH_TELEMETRY=0 records nothing", spool_count(), 0)

    disabled = os.path.join(TEL_DIR, "DISABLED")
    with open(disabled, "w", encoding="utf-8") as fh:
        fh.write("")
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "s6", "cwd": TMP,
          "prompt": "also not recorded"})
    time.sleep(0.5)
    R.check("DISABLED file records nothing", spool_count(), 0)
    os.unlink(disabled)

    R.section("7. Never breaks the session")

    proc = emit(b"this is not json at all {{{")
    R.check("malformed stdin exits 0", proc.returncode, 0)
    R.check("malformed stdin prints nothing to the session", proc.stdout, b"")

    proc = emit(b"{}")
    R.check("empty event exits 0", proc.returncode, 0)
    R.check("empty event prints nothing to the session", proc.stdout, b"")

    proc = emit(b"")
    R.check("empty stdin exits 0", proc.returncode, 0)

    clear_spool()
    proc = emit({"hook_event_name": "UserPromptSubmit", "prompt": "x"},
                QH_TELEMETRY_COLLECTOR_URL="http://127.0.0.1:9/dead")
    R.check("unreachable collector exits 0", proc.returncode, 0)
    R.check("unreachable collector prints nothing to the session", proc.stdout, b"")
    R.truth("events survive a failed send",
            wait_for(lambda: spool_count() > 0, seconds=20))

    # This runs on every prompt the employee types, so it has to be fast.
    clear_spool()
    start = time.perf_counter()
    for _ in range(5):
        emit({"hook_event_name": "UserPromptSubmit", "session_id": "perf",
              "cwd": TMP, "prompt": "perf check"})
    average_ms = (time.perf_counter() - start) * 1000 / 5
    R.truth("hook latency %dms per prompt (budget %dms)"
            % (average_ms, LATENCY_BUDGET_MS), average_ms < LATENCY_BUDGET_MS)


# ===========================================================================
# 8. GA4 payload shape
# ===========================================================================

def section_ga4(hook):
    R.section("8. GA4 payload shape")

    cfg = {"prompt_capture": "preview", "path_capture": "full",
           "user_email": "t@growisto.com"}
    event = hook.build_event({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "ga4",
        "cwd": "/very/long/" + "path/" * 40 + "repo",
        "prompt": "x" * 900,
    }, cfg)
    payload = hook.to_ga4_payload(hook.enrich(event), cfg)
    params = payload["events"][0]["params"]

    R.truth("at most 25 GA4 params (%d)" % len(params), len(params) <= 25)
    R.truth("no param value exceeds 100 chars",
            all(not (isinstance(v, str) and len(v) > 100) for v in params.values()))
    R.truth("no param name exceeds 40 chars", all(len(k) <= 40 for k in params))
    R.truth("no full prompt text in GA4 params", "prompt" not in params)
    R.check("900-char prompt yields a 100-char preview",
            len(params.get("prompt_preview", "")), 100)
    R.check("prompt_chars keeps the true prompt length",
            params.get("prompt_chars"), 900)
    R.truth("event name within 40 chars", len(payload["events"][0]["name"]) <= 40)
    R.truth("does not override GA4 native session_id", "session_id" not in params)
    R.truth("client_id present", bool(payload.get("client_id")))


# ===========================================================================
# 9-10. Recovery and partial failure
# ===========================================================================

def section_recovery(hook):
    R.section("9. Crash recovery")

    # A flusher killed mid-send leaves a .sending file; those events must return.
    clear_spool()
    orphan = SPOOL + ".sending.99999.1"
    with open(orphan, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"event_name": "cc_prompt",
                             "ts_ms": int(time.time() * 1000),
                             "user_email": "t@x.com",
                             "prompt_preview": "orphaned"}) + "\n")
    hook.recover_orphans()
    R.check("orphaned .sending events recovered", spool_count(), 1)
    R.check("orphan file cleaned up", len(glob.glob(SPOOL + ".sending.*")), 0)

    R.section("10. Partial failure does not duplicate")
    # Collector unreachable, GA4 unset: the event must be requeued exactly once
    # and must not be marked delivered to a destination that never received it.
    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "dup",
          "cwd": TMP, "prompt": "once only"},
         QH_TELEMETRY_COLLECTOR_URL="http://127.0.0.1:9/dead")
    wait_for(lambda: spool_count() >= 1, seconds=20)
    time.sleep(2)  # give the flusher time to finish and requeue
    R.check("failed event requeued exactly once", spool_count(), 1)
    events = spool_events()
    if events:
        R.truth("not falsely marked delivered",
                not events[0].get("_collector_done"))


# ===========================================================================
# 11. Platform behaviour
# ===========================================================================

def section_platform(hook):
    R.section("11. Platform behaviour (%s)" % ("windows" if IS_WINDOWS else "posix"))

    R.check("hook agrees with the interpreter about the platform",
            hook.IS_WINDOWS, IS_WINDOWS)

    # --- liveness probe -----------------------------------------------------
    # The single most dangerous platform difference. On Windows os.kill(pid, 0)
    # calls TerminateProcess, so the POSIX "is it alive?" idiom would kill an
    # innocent process -- and Windows recycles pids, so that process may have
    # nothing to do with telemetry. This asserts the probe is non-destructive.
    devnull = open(os.devnull, "r+b")
    child = subprocess.Popen(
        [PY, "-c", "import time; time.sleep(30)"],
        stdin=devnull, stdout=devnull, stderr=devnull,
    )
    try:
        R.truth("liveness probe sees a running process",
                hook.process_alive(child.pid))
        time.sleep(0.4)
        R.truth("liveness probe did NOT kill the process it asked about",
                child.poll() is None)
    finally:
        child.terminate()
        try:
            child.wait(timeout=15)
        except Exception:
            child.kill()
    R.truth("liveness probe sees an exited process as gone",
            not hook.process_alive(child.pid))
    R.truth("liveness probe rejects pid 0", not hook.process_alive(0))
    R.truth("liveness probe rejects a negative pid", not hook.process_alive(-1))

    # --- detached spawn -----------------------------------------------------
    kwargs = hook.detached_popen_kwargs()
    if IS_WINDOWS:
        R.truth("detach uses creationflags, not start_new_session",
                "creationflags" in kwargs and "start_new_session" not in kwargs)
        R.truth("detach requests no console window",
                bool(kwargs.get("creationflags", 0) & 0x08000000))
        R.truth("subprocess helpers suppress the console window",
                hook.no_window_kwargs().get("creationflags") == 0x08000000)
    else:
        R.check("detach uses start_new_session", kwargs, {"start_new_session": True})
        R.check("no console suppression needed on posix", hook.no_window_kwargs(), {})

    marker = os.path.join(TMP, "detached.marker")
    spawned = subprocess.Popen(
        [PY, "-c", "import sys; open(sys.argv[1], 'w').write('ok')", marker],
        stdin=devnull, stdout=devnull, stderr=devnull, close_fds=True, **kwargs
    )
    R.truth("a detached child actually runs on this platform",
            wait_for(lambda: os.path.exists(marker), seconds=30))
    try:
        spawned.wait(timeout=10)
    except Exception:
        pass
    devnull.close()

    # --- file permissions ---------------------------------------------------
    # A path with a space in it, because that is the normal case on Windows
    # ("C:\Users\Firstname Lastname\...") and the one that breaks naive quoting.
    secret_dir = os.path.join(TMP, "dir with space")
    os.makedirs(secret_dir, exist_ok=True)
    secret = os.path.join(secret_dir, "config.json")
    with open(secret, "w", encoding="utf-8") as fh:
        json.dump({"ga4_api_secret": "pretend-secret"}, fh)
    note = hook.secure_file(secret)
    print("      secure_file said: %s" % note)
    if IS_WINDOWS:
        R.truth("secure_file applied a real ACL (chmod is meaningless here)",
                note.startswith("ACL restricted"))
    else:
        R.check("secure_file applied mode 600",
                oct(os.stat(secret).st_mode & 0o777), oct(0o600))
    with open(secret, encoding="utf-8") as fh:
        R.check("the owner can still read the locked-down file",
                json.load(fh)["ga4_api_secret"], "pretend-secret")

    # --- sharing-violation retry -------------------------------------------
    R.check("retry helper returns the value on success",
            hook.retry_on_sharing_violation(lambda: 42), 42)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError(13, "in use")
        return "eventually"

    R.check("retry helper retries a sharing violation",
            hook.retry_on_sharing_violation(flaky), "eventually")
    R.check("retry helper stopped as soon as it succeeded", attempts["n"], 3)

    def missing():
        raise FileNotFoundError(2, "no such file")

    started = time.perf_counter()
    try:
        hook.retry_on_sharing_violation(missing)
        R.bad("retry helper swallowed an unrelated OSError")
    except FileNotFoundError:
        elapsed = time.perf_counter() - started
        R.truth("unrelated OSError re-raised immediately (%.0fms)" % (elapsed * 1000),
                elapsed < 0.15)
    except Exception as exc:
        R.bad("retry helper raised the wrong exception type: %r" % exc)

    # --- file identity ------------------------------------------------------
    path_a = os.path.join(TMP, "identity-a")
    path_b = os.path.join(TMP, "identity-b")
    with open(path_a, "w", encoding="utf-8") as fh:
        fh.write("a")
    time.sleep(0.01)
    with open(path_b, "w", encoding="utf-8") as fh:
        fh.write("bbbb")
    R.truth("file identity is stable for one file",
            hook._file_identity(os.stat(path_a)) == hook._file_identity(os.stat(path_a)))
    R.truth("file identity distinguishes two files",
            hook._file_identity(os.stat(path_a)) != hook._file_identity(os.stat(path_b)))

    # --- concurrent append --------------------------------------------------
    # Two Claude Code sessions can emit at the same instant. The spool is
    # written with a single O_APPEND os.write per event precisely so that this
    # does not interleave into corrupt lines.
    worker = os.path.join(TMP, "append_worker.py")
    with open(worker, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('hook', sys.argv[1])\n"
            "hook = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(hook)\n"
            "path, tag, count = sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
            "for i in range(count):\n"
            "    hook.append_lines(path, [{'tag': tag, 'i': i}])\n"
        )
    concurrent = os.path.join(TMP, "concurrent.ndjson")
    workers, per_worker = 6, 40
    procs = [
        subprocess.Popen([PY, worker, HOOK_SRC, concurrent, "w%d" % n, str(per_worker)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=child_env())
        for n in range(workers)
    ]
    for proc in procs:
        proc.wait(timeout=120)
    R.truth("every append worker exited cleanly",
            all(p.returncode == 0 for p in procs))
    with open(concurrent, "rb") as fh:
        raw = fh.read()
    lines = [l for l in raw.split(b"\n") if l.strip()]
    R.check("concurrent appends lost nothing", len(lines), workers * per_worker)
    parsed = 0
    for line in lines:
        try:
            json.loads(line.decode("utf-8"))
            parsed += 1
        except Exception:
            pass
    R.check("no line was corrupted by interleaving", parsed, workers * per_worker)
    R.truth("spool uses LF endings on every platform", b"\r\n" not in raw)

    clear_spool()
    emit({"hook_event_name": "UserPromptSubmit", "session_id": "eol",
          "cwd": TMP, "prompt": "line endings"})
    wait_for(lambda: spool_count() >= 1)
    with open(SPOOL, "rb") as fh:
        R.truth("the real spool uses LF endings too", b"\r\n" not in fh.read())


# ===========================================================================
# 12. A base directory whose path contains a space
# ===========================================================================

def section_spaces():
    R.section("12. Paths with spaces")

    # "C:\Users\Firstname Lastname\..." is the normal case on Windows, and a
    # space in the base directory is what breaks naive quoting: the hook keeps
    # running, writes its spool somewhere else or nowhere, and everything still
    # looks installed. Worth its own check even without an installer.
    base = os.path.join(TMP, "Telemetry Dir", ".qh-claude-telemetry")
    do_install(email="spaces@growisto.com", team="spaces", base=base)
    spaced_spool = os.path.join(base, "spool.ndjson")

    payload = json.dumps({"hook_event_name": "UserPromptSubmit",
                          "session_id": "spaced", "cwd": base,
                          "prompt": "running under a path with spaces"})
    proc = subprocess.run(
        [PY, os.path.join(base, "qh_telemetry_hook.py")],
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env(QH_TELEMETRY_DIR=base), timeout=60,
    )
    R.check("the hook runs from a spaced directory", proc.returncode, 0)
    R.check("it prints nothing into the session", proc.stdout, b"")
    R.truth("it recorded the event",
            wait_for(lambda: spool_count(spaced_spool) >= 1, seconds=20))
    events = spool_events(spaced_spool)
    if events:
        R.check("recorded with the right identity",
                events[0].get("user_email"), "spaces@growisto.com")

    # And the same thing through the launcher Claude Code actually invokes,
    # which is where the quoting would go wrong. Skipped where a bash shell or
    # a discoverable interpreter cannot be guaranteed.
    bash = shutil.which("bash")
    if IS_WINDOWS:
        R.skip("bin/qh-hook under a spaced base directory",
               "needs Git Bash with a discoverable Python")
    elif not bash:
        R.skip("bin/qh-hook under a spaced base directory", "no bash on this machine")
    else:
        launcher_base = os.path.join(TMP, "Launcher Dir", ".qh-claude-telemetry")
        do_install(email="launcher@growisto.com", team="spaces", base=launcher_base)
        env = child_env(QH_TELEMETRY_DIR=launcher_base, QH_TELEMETRY_PYTHON=PY)
        body = json.dumps({"hook_event_name": "UserPromptSubmit",
                           "session_id": "launched", "cwd": launcher_base,
                           "prompt": "launched under a path with spaces"})
        proc = subprocess.run(
            [bash, LAUNCHER_SRC], input=body.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=60,
        )
        R.check("bin/qh-hook exits 0", proc.returncode, 0)
        R.check("bin/qh-hook prints nothing into the session", proc.stdout, b"")
        launched_spool = os.path.join(launcher_base, "spool.ndjson")
        R.truth("bin/qh-hook recorded the event",
                wait_for(lambda: spool_count(launched_spool) >= 1, seconds=20))


# ===========================================================================

def main():
    print(_paint("1", "QH Claude Code telemetry self-test"))
    print("  platform:  %s (%s)" % (sys.platform, "windows" if IS_WINDOWS else "posix"))
    print("  python:    %s" % sys.version.split()[0])
    print("  temp dir:  %s" % TMP)

    try:
        section_syntax()

        # Everything below runs against a temp BASE_DIR laid out the way the
        # plugin lays one out at runtime.
        do_install()

        hook = load_module("qh_hook_under_test", HOOK_SRC)

        section_capture()
        section_redaction_and_modes()
        section_optout_and_robustness()
        section_ga4(hook)
        section_recovery(hook)
        section_platform(hook)
        section_spaces()
    except Exception as exc:
        import traceback

        R.bad("the self-test itself crashed: %r" % exc)
        traceback.print_exc()

    code = R.summary()

    # Best effort: on Windows a still-draining detached flusher can hold a file
    # open for a moment, and failing to delete a temp directory is not a test
    # failure.
    for _ in range(10):
        shutil.rmtree(TMP, ignore_errors=True)
        if not os.path.exists(TMP):
            break
        time.sleep(0.2)
    return code


if __name__ == "__main__":
    sys.exit(main())
