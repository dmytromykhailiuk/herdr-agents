import os
import time

from .. import core
from . import common

ENDED_REASONS = ("prompt_input_exit", "logout")


def mark_ended(payload, env):
    if payload.get("reason") not in ENDED_REASONS:
        return False
    sid = payload.get("session_id")
    if not sid or env.get("HERDR_AGENT_ID") or common.is_sdk(env):
        return False
    from .. import launches
    rec = launches.get(sid, environ=env)
    if not rec or rec.get("kind") != "session":
        return False
    return launches.update(sid, environ=env, ended_at=time.time(), ended_reason=payload["reason"]) is not None


def main(argv=None):
    env = os.environ
    try:
        if common.hooks_off(env):
            return 0
        payload = common.parse_payload(common.read_stdin())
        if payload is not None:
            mark_ended(payload, env)
    except Exception as e:
        core.log(f"session-end: {e!r}")
    return 0
