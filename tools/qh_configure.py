#!/usr/bin/env python3
"""
Shared install/uninstall core for the QH Claude Code telemetry hook.

Everything risky about installation is here rather than in the shell scripts:
writing config.json, and merging our four hook entries into settings.json
without disturbing hooks the user already had. install.sh and install.ps1 are
thin front ends that parse flags, print the disclosure, and call this.

That split exists because the settings merge is the one operation that can
damage a file the employee cares about. Reimplementing it in PowerShell would
have meant two versions of that logic drifting apart, and the PowerShell one
would have been the untested version.

Runs on Linux, macOS, and Windows. Python 3.8+, stdlib only.

    python3 qh_configure.py write-config --config PATH   (values via environment)
    python3 qh_configure.py install-hooks --settings PATH --hook PATH --python PATH
    python3 qh_configure.py remove-hooks  --settings PATH
    python3 qh_configure.py verify        --hook PATH --python PATH
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

MARKER = "qh_telemetry_hook.py"

# Each event gets one matcher group. UserPromptSubmit / SessionStart / SessionEnd
# are not tool events, so they carry no matcher. Skill and subagent invocations
# surface as PreToolUse with tool_name Skill / Task / SlashCommand.
DESIRED_HOOKS = {
    "UserPromptSubmit": None,
    "SessionStart": None,
    "SessionEnd": None,
    "PreToolUse": "Skill|Task|SlashCommand",
}

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def die(msg):
    sys.stderr.write("ERROR: %s\n" % msg)
    raise SystemExit(1)


def load_json(path):
    """Returns (data, existed). Raises SystemExit on a file that exists but is not JSON."""
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        return (json.loads(text) if text else {}), True
    except Exception as exc:
        die("%s is not valid JSON (%s). Fix or move it, then re-run." % (path, exc))


def write_json_atomic(path, data):
    """
    Write via a temp file in the same directory, then rename over the target.

    Same directory matters: os.replace is only atomic within a filesystem, and
    on Windows a cross-volume replace fails outright rather than degrading.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".qh-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def command_string(python_path, hook_path):
    """
    The command Claude Code will execute for each hook.

    Both paths are quoted because on Windows they almost always contain a space
    ("C:\\Program Files\\Python312\\python.exe", "C:\\Users\\Firstname Lastname\\...").
    An unquoted command there fails silently at hook time, which is the worst
    possible place to find out.
    """
    return '"%s" "%s"' % (python_path, hook_path)


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

# Config values arrive through the environment, never as command-line
# arguments. On both Linux and Windows the full argument list of a running
# process is readable by other users, and one of these values is the GA4
# api_secret.
CONFIG_ENV = {
    "collector_url": "COLLECTOR_URL",
    "collector_token": "COLLECTOR_TOKEN",
    "ga4_measurement_id": "GA4_MEASUREMENT_ID",
    "ga4_api_secret": "GA4_API_SECRET",
    "user_email": "USER_EMAIL",
    "team": "TEAM",
    "prompt_capture": "PROMPT_CAPTURE",
    "path_capture": "PATH_CAPTURE",
}


def cmd_write_config(args):
    path = args.config
    cfg, _ = load_json(path)
    if not isinstance(cfg, dict):
        cfg = {}

    for key, env in CONFIG_ENV.items():
        value = (os.environ.get(env) or "").strip()
        if value:
            cfg[key] = value

    cfg.setdefault("prompt_capture", "preview")
    cfg.setdefault("path_capture", "full")

    write_json_atomic(path, cfg)

    # Delegate the lockdown to the hook so the chmod-vs-ACL decision lives in
    # exactly one place. It is imported rather than shelled out to, which keeps
    # this usable even if the hook is not on PATH yet.
    note = "permissions unchanged"
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(args.hook)) if args.hook else "")
        from qh_telemetry_hook import secure_file  # type: ignore

        note = secure_file(path)
    except Exception as exc:
        note = "could not restrict permissions (%s)" % exc
    print("wrote %s (%s)" % (path, note))


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------

def backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = "%s.bak.%s" % (path, stamp)
    shutil.copy2(path, dest)
    print("backed up existing settings -> %s" % dest)


def strip_our_entries(groups, settings_path, event):
    """
    Remove our hook from every group, preserving everything else byte for byte.

    Groups that do not contain our marker are passed through untouched,
    including ones with no "hooks" key, an empty list, or an unexpected shape.
    Returns (cleaned_groups, removed_count).
    """
    if not isinstance(groups, list):
        die("hooks.%s in %s is not a list; aborting." % (event, settings_path))

    cleaned, removed = [], 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            cleaned.append(group)
            continue
        original = group["hooks"]
        kept = [h for h in original
                if not (isinstance(h, dict) and MARKER in str(h.get("command", "")))]
        if len(kept) == len(original):
            cleaned.append(group)      # none of ours in here
            continue
        removed += len(original) - len(kept)
        if kept:
            group["hooks"] = kept      # ours removed, theirs remain
            cleaned.append(group)
        # else: the group held only our hook, so drop the now-empty group
    return cleaned, removed


def cmd_install_hooks(args):
    settings_path = args.settings
    settings, existed = load_json(settings_path)
    if not isinstance(settings, dict):
        die("%s does not contain a JSON object." % settings_path)
    if existed:
        backup(settings_path)

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        die("'hooks' in %s is not a JSON object. Fix it by hand, then re-run." % settings_path)

    command = command_string(args.python, args.hook)
    for event, matcher in DESIRED_HOOKS.items():
        groups, _ = strip_our_entries(hooks.get(event, []), settings_path, event)
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        if matcher:
            entry["matcher"] = matcher
        groups.append(entry)
        hooks[event] = groups

    write_json_atomic(settings_path, settings)
    print("wired %d hook events into %s" % (len(DESIRED_HOOKS), settings_path))


def cmd_remove_hooks(args):
    settings_path = args.settings
    if not os.path.exists(settings_path):
        print("no settings file at %s; nothing to unwire." % settings_path)
        return

    settings, _ = load_json(settings_path)
    if not isinstance(settings, dict):
        die("%s does not contain a JSON object." % settings_path)
    backup(settings_path)

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        die("'hooks' in %s is not an object; nothing changed." % settings_path)

    removed = 0
    for event in list(hooks.keys()):
        groups, removed_here = strip_our_entries(hooks[event], settings_path, event)
        removed += removed_here
        # Only prune an event key if we actually removed something from it. An
        # event the user left empty on purpose is left exactly as it was.
        if removed_here == 0:
            continue
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]

    if removed and not hooks:
        settings.pop("hooks", None)

    write_json_atomic(settings_path, settings)
    print("removed %d telemetry hook entr%s from %s"
          % (removed, "y" if removed == 1 else "ies", settings_path))


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args):
    """
    Feed the installed hook one synthetic event and confirm it spools.

    Runs against a throwaway QH_TELEMETRY_DIR so the verification event is never
    shipped to real analytics and never pollutes the employee's spool.
    """
    payload = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "install-check",
        "cwd": os.getcwd(),
        "prompt": "install verification",
    })

    tmpdir = tempfile.mkdtemp(prefix="qh-verify-")
    try:
        env = dict(os.environ)
        env["QH_TELEMETRY_DIR"] = tmpdir
        env.pop("QH_TELEMETRY", None)
        env.pop("QH_TELEMETRY_DISABLE", None)
        proc = subprocess.run(
            [args.python, args.hook],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=30,
        )
        spool = os.path.join(tmpdir, "spool.ndjson")
        ok = os.path.exists(spool) and os.path.getsize(spool) > 0
        if ok and proc.stdout.strip():
            # A hook that prints on UserPromptSubmit injects that text straight
            # into Claude's context, so this is a correctness failure even
            # though the event was captured.
            print("  WARNING: hook wrote to stdout: %r" % proc.stdout[:200])
        if ok:
            print("  hook captured the event correctly")
            return
        print("  WARNING: the hook did not record a test event.")
        if proc.stderr.strip():
            print("  stderr: %s" % proc.stderr.decode("utf-8", "replace").strip()[:500])
        raise SystemExit(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("write-config")
    p.add_argument("--config", required=True)
    p.add_argument("--hook", default="")
    p.set_defaults(func=cmd_write_config)

    p = sub.add_parser("install-hooks")
    p.add_argument("--settings", required=True)
    p.add_argument("--hook", required=True)
    p.add_argument("--python", required=True)
    p.set_defaults(func=cmd_install_hooks)

    p = sub.add_parser("remove-hooks")
    p.add_argument("--settings", required=True)
    p.set_defaults(func=cmd_remove_hooks)

    p = sub.add_parser("verify")
    p.add_argument("--hook", required=True)
    p.add_argument("--python", required=True)
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
