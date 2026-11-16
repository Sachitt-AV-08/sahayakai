"""CLASS GATE — every way out of a call handler runs the end-of-call bookkeeping.

create_bot's finalize saves an opt-out heard during the call (retrying a
failed write), the transcript and the summary. The instance (security review
of 67839bbb6): /ws/call never awaited finalize after the pipeline, and both
handlers' except branches skipped it, so a stop heard during a call that then
ended badly could be lost.

Checked structurally over main.py (the normal end after runner.run() and every
except branch of that try must call _finish), with a self-test proving the
checker rejects a skipped branch; then behaviourally on both handlers for a
normal end, a disconnect and an exception, with the first suppression write
failing so only the end-of-call retry can save it.
"""

from __future__ import annotations

import ast

import pytest
import asyncio

from bot_harness import (
    FakeAsyncFirestore,
    FakeCallStore,
    ScriptedRunner,
    import_main,
    run_twilio_path,
    run_vobiz_path,
    stubbed_runtime,
)
from main_ast import PHONE, RECORD, SOURCE, call_name, handlers, say, suppressions
from src.opt_out import phone_suppression_id


def unfinished_exits(source: str) -> list[str]:
    """Handler exits after create_bot that skip the end-of-call bookkeeping."""
    tree = ast.parse(source)
    problems: list[str] = []

    def finishes(stmts) -> bool:
        return any(call_name(n) == "_finish" for s in stmts for n in ast.walk(s))

    for fn in handlers(tree):
        if not any(call_name(n) == "create_bot" for n in ast.walk(fn)):
            continue
        tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                 and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "run" for s in t.body for n in ast.walk(s))]
        if not tries:
            problems.append(f"{fn.name}: no try around runner.run()")
            continue
        for t in tries:
            run_at = next(i for i, s in enumerate(t.body) if any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
                for n in ast.walk(s)))
            if not finishes(t.body[run_at + 1:]):
                problems.append(f"{fn.name}: normal end after runner.run() does not call _finish")
            for h in t.handlers:
                label = ast.unparse(h.type) if h.type is not None else "bare except"
                if not finishes(h.body):
                    problems.append(f"{fn.name}: except {label} does not call _finish")
    return problems



def test_every_handler_exit_runs_the_end_of_call_bookkeeping():
    assert unfinished_exits(SOURCE) == []


def test_the_exit_checker_rejects_a_skipped_branch():
    lines = SOURCE.splitlines(keepends=True)
    # Drop the first `await _finish(finalize)` that sits in an except branch.
    for i, line in enumerate(lines):
        if line.strip() == "await _finish(finalize)" and "except" in "".join(lines[max(0, i - 3):i]):
            bad = "".join(lines[:i] + lines[i + 1:])
            break
    else:  # pragma: no cover
        pytest.fail("no except-branch _finish found to remove")
    assert unfinished_exits(bad)


# ── behaviour ────────────────────────────────────────────────────────────────


def _ending(kind: str, main):
    async def script():
        await say("दोबारा फ़ोन मत करना")
        if kind == "disconnect":
            raise main.WebSocketDisconnect()
        if kind == "error":
            raise RuntimeError("pipeline blew up mid-call")
    return script


@pytest.mark.parametrize("kind", ["normal", "disconnect", "error"])
def test_vobiz_persists_an_opt_out_on_every_exit_even_after_a_failed_write(kind, tmp_path, monkeypatch):
    # The immediate write fails; only the end-of-call retry can save it.
    db = FakeAsyncFirestore({("parent_outreach", "out-fs-1"): RECORD}, fail_suppression_sets=1)
    hangups = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def fake_hangup(call_uuid, auth_id, auth_token):
            hangups.append(call_uuid)

        main.hangup_vobiz_call = fake_hangup
        ScriptedRunner.script = _ending(kind, main)
        main.WorkerRunner = ScriptedRunner
        try:
            asyncio.run(run_vobiz_path(main, db))
        finally:
            ScriptedRunner.script = None
    writes = suppressions(db)
    assert len(writes) == 1, f"{kind}: opt-out lost"
    assert writes[0][2] == phone_suppression_id(PHONE)
    assert hangups == ["call-uuid-test"], f"{kind}: the leg must be ended exactly once"


@pytest.mark.parametrize("kind", ["normal", "disconnect", "error"])
def test_ws_call_runs_the_bookkeeping_on_every_exit(kind, tmp_path, monkeypatch):
    web_ctx = {"studentName": "Aarav", "reason": "", "requestToTalk": True, "parentLanguage": "Hindi"}
    store = FakeCallStore()
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = store
        ScriptedRunner.script = _ending(kind, main)
        main.WorkerRunner = ScriptedRunner
        try:
            asyncio.run(run_twilio_path(main, web_ctx, outreach_id="out-ctx-1"))
        finally:
            ScriptedRunner.script = None
    assert store.completed == ["out-ctx-1"], kind
