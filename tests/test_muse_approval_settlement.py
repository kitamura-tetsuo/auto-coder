"""Executable regressions for Muse host-resolved approval settlement under denyUnmatched.

The host below is a scriptable subprocess MSP peer. It verifies that the client
returns the exact empty presentation receipt before it progresses, so a canned
success that never exercises the handler cannot satisfy these tests. The frame
shapes follow the official stable 1.4.2 types; orderings are synthetic and no
live Muse host or model was invoked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.auto_coder import muse_client
from src.auto_coder.execution_trace import EventKind, Outcome, get_trace_collector
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from tests.test_muse_msp import _manager, _repository

_HOST = r"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("Muse Code 1.4.2")
    raise SystemExit
log = Path(os.environ["MSP_LOG"])
mark = Path(os.environ["MSP_MARK"])
def emit(value):
    print(json.dumps(value), flush=True)
def sub(value, ctx):
    if isinstance(value, str):
        return ctx.get(value, value.replace("$S", ctx["$S"]).replace("$T", ctx["$T"]))
    if isinstance(value, list):
        return [sub(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: sub(v, ctx) for k, v in value.items()}
    return value
def read():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(4)
    with log.open("a") as out:
        out.write(line)
    return json.loads(line)
while True:
    try:
        frame = read()
    except SystemExit:
        break
    method = frame.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"serverInfo":{"name":"fixture","version":"1.4.2"},"schema":{"version":1,"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"},"capabilities":{}}})
    elif method == "session/start":
        session = {"sessionId":"opaque/provider/session","workspaceRoot":os.getcwd(),"modelId":"muse-spark-1.3"}
        mode = os.environ.get("MSP_MODE") or ("denyUnmatched" if frame["params"].get("approvalMode") == "denyUnmatched" else None)
        if mode:
            session["approvalMode"] = {"mode": mode}
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":[]}})
    elif method == "turn/start":
        s = frame["params"]["sessionId"]
        t = "turn-" + frame["params"]["commandId"]
        ctx = {"$S": s, "$T": t, "$RPCID": frame["id"], "$RPCID_STR": str(frame["id"])}
        ack = {"jsonrpc":"2.0","id":frame["id"],"result":{"commandId":frame["params"]["commandId"],"status":"accepted","turnId":t,"startedNewTurn":True,"disposition":"started"}}
        for step in json.loads(os.environ["MSP_SCRIPT"]):
            if "emit" in step:
                emit(sub(step["emit"], ctx))
            elif "split" in step:
                data = (json.dumps(sub(step["split"], ctx)) + "\n").encode()
                for chunk in (data[:7], data[7:]):
                    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); time.sleep(0.05)
            elif "coalesce" in step:
                sys.stdout.write("".join(json.dumps(sub(f, ctx)) + "\n" for f in step["coalesce"])); sys.stdout.flush()
            elif "ack" in step:
                emit(ack)
            elif "receipt" in step:
                expected_id = sub(step["receipt"], ctx)
                got = read()
                if got != {"jsonrpc":"2.0","id":expected_id,"result":{}} or type(got["id"]) is not type(expected_id):
                    mark.write_text("bad-receipt:" + json.dumps(got))
                    raise SystemExit(3)
            elif "read" in step:
                read()
            elif "answer" in step:
                emit({"jsonrpc":"2.0","method":"item/completed","params":{"sessionId":s,"item":{"itemId":"a","kind":"agentMessage","revision":1,"status":"completed","turnId":t,"text":step["answer"]}}})
            elif "complete" in step:
                emit({"jsonrpc":"2.0","method":"turn/completed","params":{"sessionId":s,"turnId":t,"terminal":"completed"}})
            elif "sleep" in step:
                time.sleep(step["sleep"])
            elif "repeat" in step:
                end = time.time() + step["repeat"]
                while time.time() < end:
                    emit(sub(step["frame"], ctx)); time.sleep(0.02)
"""

_REQ = {"jsonrpc": "2.0", "id": "$RPCID", "method": "approval/request", "params": {"sessionId": "$S", "turnId": "$T", "approvalId": "ap-1", "subject": {"command": "do-not-log-this-command"}}}
_REQUESTED = {"jsonrpc": "2.0", "method": "approval/requested", "params": {"sessionId": "$S", "turnId": "$T", "approvalId": "ap-1", "subject": {"command": "do-not-log-this-command"}}}
_UPDATED = {"jsonrpc": "2.0", "method": "approval/updated", "params": {"sessionId": "$S", "approvalId": "ap-1"}}


def _resolved(approval: str = "ap-1", result: object = "deny", **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {"sessionId": "$S", "turnId": "$T", "approvalId": approval, "policyResult": result, "decision": "whatever", "resolvedBy": "policy", "extra": 1}
    params.update(overrides)
    return {"jsonrpc": "2.0", "method": "approval/resolved", "params": params}


_FINISH = [{"answer": "denied-answer"}, {"complete": True}]


@pytest.fixture
def setup(tmp_path, monkeypatch, _use_real_commands):
    repo = _repository(tmp_path)
    host = tmp_path / "muse"
    host.write_text(_HOST)
    host.chmod(0o700)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(host))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_MARK", str(tmp_path / "mark"))
    monkeypatch.setattr(muse_client, "_MUSE_APPROVAL_SETTLEMENT_SECONDS", 0.8)

    class Env:
        log = tmp_path / "msp.jsonl"
        mark = tmp_path / "mark"

        @staticmethod
        def run(script, *, options=("--disable-approval",), mode=None):
            monkeypatch.setenv("MSP_SCRIPT", json.dumps(script))
            if mode:
                monkeypatch.setenv("MSP_MODE", mode)
            config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=list(options))})
            manager = _manager(config)
            manager.client = manager._clients["muse"]
            return manager

        @staticmethod
        def frames():
            return [json.loads(line) for line in Path(tmp_path / "msp.jsonl").read_text().splitlines()]

    return Env


def _blocked(case: int):
    return [e for e in get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=case).events if e.kind == EventKind.STAGE_RESULT.value and e.stage_id == "llm.muse-interactive-request"]


def _assert_no_decision(env):
    methods = [f.get("method") for f in env.frames()]
    assert "approval/decide" not in methods
    assert not any(m and m.startswith("permission") for m in methods)


def _run_ok(env, script, case, **kw):
    manager = env.run(script, **kw)
    before = len(_blocked(case))
    with get_trace_collector().start_execution("owner/repo", "issue", case, origin="worker") as execution:
        assert manager._run_llm_cli("first") == "denied-answer"
        execution.finish(Outcome.COMPLETED)
    assert not env.mark.exists()
    assert manager.get_last_session_id() == "opaque/provider/session"
    assert len(_blocked(case)) == before
    _assert_no_decision(env)
    return manager


def _run_fail(env, script, match="", **kw):
    manager = env.run(script, **kw)
    started = time.monotonic()
    with pytest.raises((RuntimeError,)) as info:
        manager._run_llm_cli("first")
    assert match in str(info.value)
    assert time.monotonic() - started < 5
    assert manager.get_last_session_id() is None
    assert manager.client.get_last_session_id() is None
    _assert_no_decision(env)
    return manager


@pytest.mark.parametrize("policy", ["deny", "allow"])
def test_auto_denial_exchange_reaches_manager_result(setup, policy):
    _run_ok(setup, [{"ack": True}, {"emit": _REQ}, {"receipt": "$RPCID"}, {"emit": _resolved(result=policy)}, *_FINISH], 24001)


@pytest.mark.parametrize("id_form", ["$RPCID", "$RPCID_STR", "server-id"])
def test_server_request_id_collision_keeps_turn_acknowledgement(setup, id_form):
    request = dict(_REQ, id=id_form)
    # Request before the acknowledgement; the receipt must echo the typed server id.
    _run_ok(setup, [{"emit": request}, {"receipt": id_form}, {"ack": True}, {"emit": _resolved()}, *_FINISH], 24002)


@pytest.mark.parametrize("bad_id", [True, False, None])
def test_invalid_server_request_ids_are_rejected(setup, bad_id):
    _run_fail(setup, [{"ack": True}, {"emit": dict(_REQ, id=bad_id)}, {"sleep": 3}], "invalid id")


@pytest.mark.parametrize(
    "script",
    [
        [{"ack": True}, {"emit": _REQUESTED}, {"emit": _UPDATED}, {"emit": _resolved()}, *_FINISH],
        [{"ack": True}, {"emit": _UPDATED}, {"emit": dict(_REQ, id=9)}, {"receipt": 9}, {"emit": _resolved()}, *_FINISH],
        [{"emit": _UPDATED}, {"emit": _REQUESTED}, {"ack": True}, {"emit": _resolved()}, *_FINISH],
        [{"split": _REQUESTED}, {"ack": True}, {"coalesce": [_UPDATED, _resolved()]}, *_FINISH],
    ],
)
def test_orderings_and_split_frames_settle(setup, script):
    _run_ok(setup, script, 24003)


def test_terminal_before_opening_and_delayed_duplicates_stay_terminal(setup):
    script = [{"ack": True}, {"emit": _resolved()}, {"emit": _REQUESTED}, {"emit": _UPDATED}, {"emit": dict(_REQ, id=5)}, {"receipt": 5}, *_FINISH]
    _run_ok(setup, script, 24004)


def test_one_of_two_approvals_unresolved_fails_at_its_deadline(setup):
    other = json.loads(json.dumps(_REQUESTED).replace("ap-1", "ap-2"))
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": other}, {"emit": _resolved("ap-1")}, {"sleep": 10}], "approval/settlement-expired")


def test_one_of_two_approvals_unresolved_blocks_completion(setup):
    other = json.loads(json.dumps(_REQUESTED).replace("ap-1", "ap-2"))
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": other}, {"emit": _resolved("ap-1")}, *_FINISH, {"sleep": 10}], "approval/unresolved-at-completion")


@pytest.mark.parametrize("override", [{"sessionId": "other"}, {"turnId": "old-turn"}, {"approvalId": "ap-9"}])
def test_resolution_for_another_identity_does_not_settle(setup, override):
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": _resolved(**override)}, *_FINISH, {"sleep": 10}], "interactive request")


def test_conflicting_policy_results_fail(setup):
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": _resolved(result="deny")}, {"emit": _resolved(result="allow")}, {"sleep": 5}], "conflicting policy")


@pytest.mark.parametrize("result", [None, "approve", "", 1])
def test_missing_or_unknown_policy_result_cannot_settle(setup, result):
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": _resolved(result=result)}, *_FINISH, {"sleep": 10}], "interactive request")


def test_noise_cannot_renew_the_window(setup):
    started = time.monotonic()
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, {"repeat": 4, "frame": _UPDATED}], "approval/settlement-expired")
    assert time.monotonic() - started < 3


def test_resolution_before_expiry_continues(setup):
    _run_ok(setup, [{"ack": True}, {"emit": _REQUESTED}, {"sleep": 0.4}, {"emit": _resolved()}, *_FINISH], 24005)


def test_completed_turn_with_unresolved_approval_is_not_rescued_by_late_resolution(setup):
    _run_fail(setup, [{"ack": True}, {"emit": _REQUESTED}, *_FINISH, {"emit": _resolved()}], "unresolved")


def test_completion_without_final_text_after_denial_fails(setup):
    _run_fail(setup, [{"ack": True}, {"emit": _REQ}, {"receipt": "$RPCID"}, {"emit": _resolved()}, {"complete": True}], "final assistant text")


def test_prior_invocation_terminal_does_not_settle_new_work(setup, monkeypatch):
    manager = _run_ok(setup, [{"ack": True}, {"emit": _REQUESTED}, {"emit": _resolved()}, *_FINISH], 24006)
    with pytest.raises(RuntimeError):
        # Replay only the previous turn's resolution; the current approval stays pending.
        setup_script = [{"ack": True}, {"emit": _REQUESTED}, {"emit": _resolved(turnId="turn-previous")}, *_FINISH, {"sleep": 10}]
        monkeypatch.setenv("MSP_SCRIPT", json.dumps(setup_script))
        manager._run_llm_cli("second")
    assert manager.get_last_session_id() is None


@pytest.mark.parametrize("mode", [None, "allowAll", "ask"])
def test_unconfirmed_mode_gets_receipt_then_refuses_immediately(setup, mode):
    # Fresh ordinary session without explicit denial: no automatic-settlement path.
    started = time.monotonic()
    manager = setup.run([{"ack": True}, {"emit": _REQ}, {"receipt": "$RPCID"}, {"sleep": 10}], options=(), mode=mode)
    with get_trace_collector().start_execution("owner/repo", "issue", 24007 + {None: 0, "allowAll": 1, "ask": 2}[mode], origin="worker") as execution:
        with pytest.raises(RuntimeError, match="cannot wait for interactive request approval/request"):
            manager._run_llm_cli("first")
        execution.finish(Outcome.FAILED)
    assert time.monotonic() - started < 5
    assert not setup.mark.exists()
    events = _blocked(24007 + {None: 0, "allowAll": 1, "ask": 2}[mode])
    assert len(events) == 1 and events[0].outcome == Outcome.BLOCKED.value
    assert events[0].facts["effective_approval_policy"] == (mode or "unknown")
    assert events[0].facts["approvalId"] == "ap-1"
    assert "do-not-log-this-command" not in str(events)
    _assert_no_decision(setup)


def test_unsupported_server_request_gets_method_error(setup):
    _run_fail(setup, [{"ack": True}, {"emit": {"jsonrpc": "2.0", "id": 77, "method": "userInput/request", "params": {"sessionId": "$S"}}}, {"read": 1}, {"sleep": 5}], "interactive request userInput/request")
    errors = [f for f in setup.frames() if f.get("id") == 77]
    assert errors and errors[0]["error"]["code"] == -32601


def test_denial_without_any_approval_frames_keeps_result_path(setup):
    _run_ok(setup, [{"ack": True}, *_FINISH], 24008, options=())
