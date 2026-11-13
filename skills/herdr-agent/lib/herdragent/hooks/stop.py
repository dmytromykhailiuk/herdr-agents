import json
import os
import sys
import time

from ..core import atomic_write


def last_text_from_transcript(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(t for t in texts if t.strip())
            if text.strip():
                return text
    return ""


def write_result(agent_dir, payload):
    message = payload.get("last_assistant_message")
    source = "hook"
    if not isinstance(message, str) or not message.strip():
        message = last_text_from_transcript(payload.get("transcript_path") or "")
        source = "transcript"
    record = {
        "written_at_ns": time.time_ns(),
        "source": source,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "background_tasks": payload.get("background_tasks") or [],
        "message": message or "",
    }
    # result.md before result.json: a reader that sees a fresh result.json also sees the matching result.md.
    atomic_write(os.path.join(agent_dir, "result.md"), message or "")
    atomic_write(os.path.join(agent_dir, "result.json"), json.dumps(record, ensure_ascii=False))
    turn = dict({k: v for k, v in record.items() if k != "message"}, chars=len(message or ""))
    with open(os.path.join(agent_dir, "turns.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(turn) + "\n")


def run():
    agent_dir = os.environ.get("HERDR_AGENT_DIR")
    if not agent_dir or not os.path.isdir(agent_dir):
        return
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    write_result(agent_dir, payload)


def main(argv=None):
    try:
        run()
    except Exception as exc:
        try:
            d = os.environ.get("HERDR_AGENT_DIR")
            if d:
                with open(os.path.join(d, "hook-errors.log"), "a", encoding="utf-8") as f:
                    f.write(f"{time.time()}: {exc!r}\n")
        except Exception:
            pass
    return 0
