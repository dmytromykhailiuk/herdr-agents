import datetime as dt
import glob
import json
import os
import shlex
import sys
import time

from . import core
from .core import EXIT_API_ERROR, EXIT_BLOCKED, EXIT_DEAD, EXIT_ERR, EXIT_NORESULT, EXIT_OK, EXIT_TIMEOUT, die, log
from .trust import claude_invocation

STATUS_EXIT = {
    "done": EXIT_OK,
    "blocked": EXIT_BLOCKED,
    "timeout": EXIT_TIMEOUT,
    "dead": EXIT_DEAD,
    "no_result": EXIT_NORESULT,
    "api_error": EXIT_API_ERROR,
    "error": EXIT_ERR,
    "busy": EXIT_ERR,
}

API_ERROR_PREFIX = "API Error:"
CONTINUE_PROMPT = ("continue (your previous turn was cut off by an API error: pick up where you "
                   "stopped and finish with your final report)")


def cli_ref():
    return shlex.quote(core.CLI_PATH) if core.CLI_PATH else "herdr-agent"


def agent_dir(agent_id):
    return os.path.join(str(core.agent_root()), agent_id)


def load_meta(agent_id):
    path = os.path.join(agent_dir(agent_id), "meta.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        die(f"unknown agent '{agent_id}' (no {path})")


def save_meta(meta):
    meta["updated_at"] = time.time()
    core.atomic_write(os.path.join(agent_dir(meta["id"]), "meta.json"),
                      json.dumps(meta, indent=2, ensure_ascii=False))


def iter_metas():
    root = str(core.agent_root())
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, "meta.json")
        try:
            with open(p, encoding="utf-8") as f:
                yield name, json.load(f)
        except (OSError, ValueError):
            yield name, None


def read_result(meta):
    try:
        with open(os.path.join(agent_dir(meta["id"]), "result.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def is_api_error_entry(e):
    """A transcript entry Claude Code synthesized for a failed API call (not model output).
    `<synthetic>` alone is not enough: Claude Code also writes non-error synthetic entries."""
    if e.get("isApiErrorMessage"):
        return True
    msg = e.get("message") or {}
    if msg.get("model") != "<synthetic>":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        text = content if isinstance(content, str) else ""
    return text.lstrip().startswith(API_ERROR_PREFIX)


def transcript_path_for(sid, config_dir=None):
    if not sid:
        return None
    base = os.path.expanduser(config_dir) if config_dir else os.path.join(str(core.home()), ".claude")
    paths = sorted(glob.glob(os.path.join(glob.escape(base), "projects", "*", f"{sid}.jsonl")))
    return paths[0] if paths else None


def meta_transcript_path(meta):
    r = read_result(meta) if meta.get("id") else None
    path = (r or {}).get("transcript_path")
    if path and (r or {}).get("session_id") in (None, meta.get("session_id")) and os.path.isfile(path):
        return path
    return transcript_path_for(meta.get("session_id"), meta.get("claude_config_dir"))


def transcript_last(meta, since_ns):
    """(text, api_error) of the last assistant text entry in the session transcript
    written after since_ns; api_error is None when there is no such entry."""
    if not meta.get("session_id"):
        return "", None
    path = meta_transcript_path(meta)
    if not path:
        return "", None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return "", None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") != "assistant":
            continue
        ts = e.get("timestamp")
        try:
            if ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9 < since_ns:
                return "", None
        except ValueError:
            pass
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                return text, is_api_error_entry(e)
        elif isinstance(content, str) and content.strip():
            return content, is_api_error_entry(e)
    return "", None


def transcript_fallback(meta, since_ns):
    return transcript_last(meta, since_ns)[0]


def api_error_check(meta, since_ns, text):
    """True when the turn's final message is a Claude Code API error, not a result.

    The transcript's markers (isApiErrorMessage / model "<synthetic>") decide when the
    entry is found; without a transcript the "API Error:" prefix decides."""
    _, marked = transcript_last(meta, since_ns)
    if marked is not None:
        return marked
    return (text or "").lstrip().startswith(API_ERROR_PREFIX)


def api_error_hint(meta):
    sid = meta.get("session_id") or "?"
    cwd = meta.get("cwd")
    return (f"agent {meta['id']} (session {sid}) ended its turn on a Claude Code API error, so its work is "
            f"unfinished. The tab and files were kept. Resume with "
            f"`{cli_ref()} send {meta['id']} --prompt continue --wait --close`, or if the tab is gone, "
            f"`cd {shlex.quote(cwd) if cwd else '<cwd>'} && "
            f"{claude_invocation(meta.get('claude_config_dir'))} --resume {sid}`")


def emit(meta, status, result_text=None, as_json=False, extra=None):
    """Report an outcome: result text on stdout, status on stderr (or one JSON doc)."""
    d = agent_dir(meta["id"])
    if as_json:
        doc = {
            "id": meta["id"], "status": status, "tab_id": meta.get("tab_id"),
            "pane_id": meta.get("pane_id"), "dir": d,
            "result_path": os.path.join(d, "result.md"), "result": result_text,
            "session_id": meta.get("session_id"),
        }
        doc.update(extra or {})
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return
    if result_text is not None:
        sys.stdout.write(result_text if result_text.endswith("\n") else result_text + "\n")
        sys.stdout.flush()
    for k, v in (extra or {}).items():
        if k == "screen" and v:
            log("terminal snapshot:\n" + v)
        elif v not in (None, ""):
            log(f"{k}: {v}")
    log(f"id={meta['id']} status={status} tab={meta.get('tab_id')} session={meta.get('session_id')}")
