"""CLASS GATE — a call that is over stops using the instance promptly.

Production, 2026-11-16: after the parent hung up, the pipeline idled until
pipecat's idle timeout (07:04:37 to 07:09:35 UTC), holding a Cloud Run slot
whose CPU is always allocated. Nothing cancelled the pipeline when the far
end went away.

The class is "an ended call keeps its pipeline". The gate, with the real
create_bot and the real handlers:
  * on the client-disconnected event the pipeline is cancelled within a
    bounded time, the end-of-call bookkeeping still runs, and an opt-out heard
    earlier whose first write failed is still retried and saved;
  * after a stop request the call ends gracefully (acknowledgement, then
    EndFrame), and if that has not finished in time the pipeline is cancelled;
  * through the Vobiz handler, a disconnect mid-call cancels the pipeline, the
    leg is hung up once and the suppression is saved.
"""

from __future__ import annotations

import asyncio

import pytest

from bot_harness import (
    RECORDER,
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    ScriptedRunner,
    TranscriptionFrame,
    build_bot,
    import_main,
    run_from_recorder,
    run_vobiz_path,
    stubbed_runtime,
)
from main_ast import PHONE, RECORD, suppressions
from src.opt_out import phone_suppression_id

BOUND_SECONDS = 1.0
CTX = {"id": "OutreachFs0000000001", "studentName": "Aarav", "reason": "consecutive_absences",
       "parentLanguage": "Hindi", "teacherName": "Ms. Sharma", "schoolName": "Demo Public School",
       "parentPhone": PHONE}


class Writer:
    def __init__(self, results=(True,)):
        self.results, self.calls = list(results), 0

    async def __call__(self):
        self.calls += 1
        return self.results[min(self.calls, len(self.results)) - 1]


async def _hear(run, text):
    guard = run.processor("StopRequestGuard")
    await guard.process_frame(TranscriptionFrame(text=text), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)


async def _disconnect(run):
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(run.transport.handlers["on_client_disconnected"](run.transport, object()), BOUND_SECONDS)
    return started


def test_a_disconnect_cancels_the_pipeline_promptly_and_still_finishes_the_call():
    store = FakeCallStore()
    with stubbed_runtime():
        async def go():
            run = await build_bot(CTX, call_store=store, on_opt_out=Writer())
            started = await _disconnect(run)
            return run, started

        run, started = asyncio.run(go())
    assert run.task.cancelled_at is not None, "the pipeline was left to idle"
    assert run.task.cancelled_at - started < BOUND_SECONDS
    assert store.completed == ["OutreachFs0000000001"], "the end-of-call bookkeeping did not run"


def test_an_opt_out_heard_before_the_disconnect_is_still_retried_and_saved():
    writer = Writer(results=(False, True))  # the immediate write fails
    with stubbed_runtime():
        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=writer)
            await _hear(run, "please don't call me again")
            await _disconnect(run)
            return run

        run = asyncio.run(go())
    assert writer.calls == 2, "the end-of-call retry did not run"
    assert run.task.cancelled_at is not None


def test_a_stop_that_does_not_end_the_call_in_time_cancels_it(monkeypatch):
    with stubbed_runtime():
        from src import bot

        monkeypatch.setattr(bot, "STOP_CANCEL_AFTER_SECONDS", 0.05)

        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=Writer())
            await _hear(run, "stop calling me")
            queued = [type(f).__name__ for f in run.task.queued]
            await asyncio.sleep(0.05 + BOUND_SECONDS / 2)
            return run, queued

        run, queued = asyncio.run(go())
    assert queued == ["TTSSpeakFrame", "EndFrame"], "the graceful end comes first"
    assert run.task.cancelled_at is not None, "a stalled stop left the pipeline running"


def test_a_stop_that_ended_the_call_is_not_cancelled_again(monkeypatch):
    with stubbed_runtime():
        from src import bot

        monkeypatch.setattr(bot, "STOP_CANCEL_AFTER_SECONDS", 0.05)

        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=Writer())
            await _hear(run, "stop calling me")
            run.task.finished = True
            await asyncio.sleep(0.2)
            return run

        run = asyncio.run(go())
    assert run.task.cancelled_at is None


def test_a_vobiz_disconnect_mid_call_cancels_hangs_up_once_and_saves_the_stop(tmp_path, monkeypatch):
    db = FakeAsyncFirestore({("parent_outreach", "OutreachFs0000000001"): RECORD}, fail_suppression_sets=1)
    hangups = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def fake_hangup(call_uuid, auth_id, auth_token):
            hangups.append(call_uuid)

        main.hangup_vobiz_call = fake_hangup

        async def live():
            run = run_from_recorder()
            await _hear(run, "दोबारा फ़ोन मत करना")
            await _disconnect(run)

        ScriptedRunner.script = live
        main.WorkerRunner = ScriptedRunner
        try:
            asyncio.run(asyncio.wait_for(run_vobiz_path(main, db), 5))
        finally:
            ScriptedRunner.script = None
        [task] = RECORDER.instances_of("PipelineTask")
    assert task.cancelled_at is not None
    assert hangups == ["call-uuid-test"]
    writes = suppressions(db)
    assert len(writes) == 1 and writes[0][2] == phone_suppression_id(PHONE)
