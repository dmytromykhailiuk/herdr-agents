import json
import os
import shutil
import sys
import time

from . import INTEGRATION, __version__, core
from .core import EXIT_BLOCKED, EXIT_ERR, EXIT_NORESULT, EXIT_OK, die, log
from .herdr import HerdrError, agent_info, herdr, pane_exists, screen
from .results import STATUS_EXIT, agent_dir, cli_ref, emit, iter_metas, load_meta, read_result, save_meta
from .spawn import do_spawn, launches_call, read_prompt, require_herdr, send_prompt
from .waiting import close_agent, finish, wait_turn


def cmd_spawn(args):
    meta, status, extra = do_spawn(args, read_prompt(args))
    if status == "working" and not args.json:
        print(meta["id"], flush=True)
    emit(meta, status, None, args.json, extra)
    return EXIT_OK if status == "working" else STATUS_EXIT.get(status, EXIT_ERR)


def cmd_run(args):
    meta, status, extra = do_spawn(args, read_prompt(args))
    if status != "working":
        emit(meta, status, None, args.json, extra)
        return STATUS_EXIT.get(status, EXIT_ERR)
    log(f"spawned {meta['id']} in tab {meta['tab_id']}; waiting")
    status, text, extra = wait_turn(meta, args.timeout)
    return finish(meta, status, text, extra, args, close_on_done=not args.keep)


def cmd_wait(args):
    require_herdr()
    meta = load_meta(args.id)
    status, text, extra = wait_turn(meta, args.timeout)
    return finish(meta, status, text, extra, args, close_on_done=args.close)


def cmd_send(args):
    require_herdr()
    meta = load_meta(args.id)
    meta, status, extra = send_prompt(meta, read_prompt(args))
    if status != "working" or not args.wait:
        emit(meta, status, None, args.json, extra)
        return EXIT_OK if status == "working" else STATUS_EXIT.get(status, EXIT_ERR)
    status, text, extra = wait_turn(meta, args.timeout)
    return finish(meta, status, text, extra, args, close_on_done=args.close)


def cmd_keys(args):
    """Answer a dialog, then wait until herdr no longer reports it as blocked."""
    require_herdr()
    meta = load_meta(args.id)
    herdr("agent", "send-keys", meta["id"], *args.keys, timeout=15)
    deadline = time.monotonic() + 10
    state = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        info = agent_info(meta["id"])
        state = (info or {}).get("agent_status")
        if state != "blocked":
            break
    meta["status"] = "working" if state == "working" else (state or "dead")
    save_meta(meta)
    log(f"{meta['id']}: sent {' '.join(args.keys)}; now {state or 'gone'}")
    if state == "blocked":
        log(f"still blocked (another dialog?); inspect with `{cli_ref()} screen {meta['id']}`")
        return EXIT_BLOCKED
    return EXIT_OK


def cmd_result(args):
    meta = load_meta(args.id)
    path = os.path.join(agent_dir(meta["id"]), "result.md")
    if not os.path.exists(path):
        die(f"no result yet for {args.id}", EXIT_NORESULT)
    with open(path, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return EXIT_OK


def cmd_screen(args):
    require_herdr()
    meta = load_meta(args.id)
    print(screen(meta, args.lines))
    return EXIT_OK


def live_status(meta):
    try:
        info = agent_info(meta["id"])
    except HerdrError:
        return "unknown"
    if info:
        return info.get("agent_status", "unknown")
    return "pane-only" if pane_exists(meta.get("pane_id")) else "gone"


def cmd_status(args):
    require_herdr()
    meta = load_meta(args.id)
    meta["live"] = live_status(meta)
    r = read_result(meta)
    meta["has_result"] = bool(r and r.get("written_at_ns", 0) > meta.get("last_prompt_at_ns", 0))
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_list(args):
    require_herdr()
    rows = []
    for name, meta in iter_metas():
        if meta is None:
            rows.append({"id": name, "status": "corrupt", "live": "?"})
            continue
        rows.append({"id": name, "status": meta.get("status"), "live": live_status(meta),
                     "tab": meta.get("tab_id"),
                     "age_min": round((time.time() - meta.get("created_at", time.time())) / 60, 1)})
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no herdr agents")
    else:
        for r in rows:
            print(f"{r['id']:<34} {str(r['status']):<10} live={r['live']:<9} tab={r.get('tab')} age={r.get('age_min')}m")
    return EXIT_OK


def cmd_close(args):
    require_herdr()
    meta = load_meta(args.id)
    if not args.force and live_status(meta) == "working":
        die(f"{args.id} is still working; pass --force to close anyway")
    closed = close_agent(meta, keep_files=args.keep_files)
    log(f"{args.id}: closed {closed or 'nothing (already gone)'}; files {'kept' if args.keep_files else 'removed'}")
    return EXIT_OK


def cmd_gc(args):
    require_herdr()
    cutoff = time.time() - args.older_than * 3600
    for name, meta in iter_metas():
        d = os.path.join(str(core.agent_root()), name)
        mtime = os.path.getmtime(d)
        if meta is None:
            if mtime < cutoff:
                print(f"remove {name} (no meta)")
                if not args.dry_run:
                    shutil.rmtree(d, ignore_errors=True)
            continue
        live = live_status(meta)
        if live in ("gone",) and (meta.get("updated_at") or mtime) < cutoff:
            print(f"remove {name} ({meta.get('status')}, tab gone)")
            if not args.dry_run:
                shutil.rmtree(d, ignore_errors=True)
                if meta.get("session_id"):
                    launches_call("forget", meta["session_id"])
        elif live != "gone" and args.close_idle and live in ("idle", "done") \
                and (meta.get("updated_at") or mtime) < cutoff:
            print(f"close {name} (idle since before cutoff)")
            if not args.dry_run:
                close_agent(meta)
    return EXIT_OK


def cmd_path(args):
    print(core.CLI_PATH)
    return EXIT_OK


def cmd_version(args):
    if args.json:
        print(json.dumps({"version": __version__, "integration": INTEGRATION}))
    else:
        print(f"herdr-agents {__version__} (integration {INTEGRATION})")
    return EXIT_OK
