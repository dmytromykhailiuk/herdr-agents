import shutil
import time

from .core import EXIT_ERR, log
from .herdr import HerdrError, agent_info, herdr, pane_exists, screen, tab_info
from .results import (API_ERROR_PREFIX, CONTINUE_PROMPT, STATUS_EXIT, agent_dir, api_error_check,
                      api_error_hint, cli_ref, emit, read_result, save_meta, transcript_fallback, transcript_last)
from .spawn import launches_call, send_prompt


def do_wait(meta, timeout, poll=1.0):
    """Block until the current turn finishes. Returns (status, result_text, extra)."""
    deadline = time.monotonic() + timeout if timeout else None
    since = meta.get("last_prompt_at_ns", 0)
    seen_working = False
    idle_since = None
    bg_noted = False
    blocked_since = None
    while True:
        r = read_result(meta)
        if r and r.get("written_at_ns", 0) > since:
            if r.get("background_tasks"):
                if not bg_noted:
                    log(f"{meta['id']}: turn ended with background tasks still running; waiting for them")
                    bg_noted = True
            else:
                text = r.get("message") or ""
                if not text.strip():
                    text = transcript_fallback(meta, since)
                if api_error_check(meta, since, text):
                    return api_error_result(meta, text, r.get("source"))
                meta["status"] = "done"
                save_meta(meta)
                return "done", text, {"source": r.get("source")}

        info = agent_info(meta["id"])
        if info is None:
            r = read_result(meta)
            if r and r.get("written_at_ns", 0) > since:
                continue
            meta["status"] = "dead"
            save_meta(meta)
            alive = pane_exists(meta.get("pane_id"))
            return "dead", None, {"screen": screen(meta) if alive else "",
                                  "hint": "agent exited" if alive else "tab/pane was closed"}
        if info.get("pane_id") and info["pane_id"] != meta.get("pane_id"):
            meta.update(pane_id=info["pane_id"], tab_id=info.get("tab_id"))
            save_meta(meta)

        state = info.get("agent_status")
        now = time.monotonic()
        if state != "blocked":
            blocked_since = None
        if state == "working":
            seen_working, idle_since = True, None
        elif state == "blocked" and now - (blocked_since := blocked_since or now) < 2:
            pass
        elif state == "blocked":
            meta["status"] = "blocked"
            save_meta(meta)
            return "blocked", None, {"screen": screen(meta), "hint":
                    f"answer with `{cli_ref()} keys {meta['id']} <keys>` then `{cli_ref()} wait {meta['id']} --close`"}
        elif state in ("idle", "done") and not bg_noted:
            idle_since = idle_since or now
            grace = 8 if seen_working else 45
            if now - idle_since > grace:
                text, marked = transcript_last(meta, since)
                if text and (marked or text.lstrip().startswith(API_ERROR_PREFIX)):
                    return api_error_result(meta, text, "transcript-fallback")
                if text:
                    meta["status"] = "done"
                    save_meta(meta)
                    return "done", text, {"source": "transcript-fallback"}
                meta["status"] = "no_result"
                save_meta(meta)
                return "no_result", None, {"screen": screen(meta), "hint":
                        "agent is idle but produced no final message (interrupted, API error, or prompt never started)"}
        else:
            idle_since = None

        if deadline and now > deadline:
            meta["status"] = "timeout"
            save_meta(meta)
            return "timeout", None, {"hint": f"still running; resume with `{cli_ref()} wait {meta['id']}`"}
        time.sleep(poll)


def api_error_result(meta, text, source):
    meta["status"] = "api_error"
    meta["api_error"] = (text or "").strip()[:500]
    save_meta(meta)
    return "api_error", None, {"source": source, "api_error": meta["api_error"]}


def wait_turn(meta, timeout):
    """do_wait, plus one automatic `continue` when the turn ended on an API error
    (sleep, lost connection) and the session is still alive."""
    started = time.monotonic()
    status, text, extra = do_wait(meta, timeout)
    if status != "api_error":
        return status, text, extra
    first = extra.get("api_error")
    info = agent_info(meta["id"])
    if info is not None and pane_exists(info.get("pane_id") or meta.get("pane_id")):
        log(f"{meta['id']}: turn ended on an API error ({first[:120]!r}); sending one automatic continue")
        meta["auto_continues"] = meta.get("auto_continues", 0) + 1
        settle = time.monotonic() + 15
        while time.monotonic() < settle and (info or {}).get("agent_status") == "working":
            time.sleep(0.5)
            info = agent_info(meta["id"])
        meta, sent, sent_extra = send_prompt(meta, CONTINUE_PROMPT)
        note = "sent after API error: " + first[:200]
        if sent == "working":
            left = max(60, timeout - (time.monotonic() - started)) if timeout else timeout
            status, text, extra = do_wait(meta, left)
            if status != "api_error":
                return status, text, dict(extra, auto_continue=note)
            why = extra.get("api_error") or status
        elif sent in ("blocked", "dead"):
            return sent, None, dict(sent_extra, auto_continue="continue not delivered: " + sent)
        else:
            why = f"continue not delivered ({sent}: {sent_extra.get('hint') or sent_extra.get('error') or ''})"
    else:
        why = "session is gone (tab/pane closed), no automatic continue"
    meta["status"] = "api_error"
    save_meta(meta)
    return "api_error", None, {"api_error": first, "auto_continue": why, "hint": api_error_hint(meta)}


def close_agent(meta, keep_files=False):
    """Close only what we created. Idempotent."""
    closed = None
    info = agent_info(meta["id"])
    pane_id = (info or {}).get("pane_id") or meta.get("pane_id")
    tab_id = (info or {}).get("tab_id") or meta.get("tab_id")
    if meta.get("created_tab"):
        tab = tab_info(tab_id)
        if tab and tab.get("pane_count", 1) <= 1 and pane_exists(pane_id):
            herdr("tab", "close", tab_id, timeout=30)
            closed = f"tab {tab_id}"
        elif pane_exists(pane_id):
            herdr("pane", "close", pane_id, timeout=30)
            closed = f"pane {pane_id}"
    if meta.get("session_id"):
        launches_call("forget", meta["session_id"])
    if keep_files:
        meta["status"] = meta.get("status") if meta.get("status") in ("done", "error") else "closed"
        meta["closed"] = True
        save_meta(meta)
    else:
        shutil.rmtree(agent_dir(meta["id"]), ignore_errors=True)
    return closed


def finish(meta, status, text, extra, args, close_on_done):
    if status == "done" and close_on_done:
        try:
            closed = close_agent(meta, keep_files=args.keep_files)
            extra = dict(extra, closed=closed)
        except HerdrError as e:
            extra = dict(extra, close_error=str(e))
        if not args.keep_files:
            extra = dict(extra, files="removed")
    emit(meta, status, text, args.json, extra)
    return STATUS_EXIT.get(status, EXIT_ERR)
