"""CLASS GATE — every way a call ends hangs up the leg exactly once.

Independent review #6: the normal endings (the CLOSE phase and max turns)
pushed an EndFrame DOWNSTREAM from the middle of the pipeline. pipecat's task
never sees that frame, so runner.run() never returned, main.py's hang-up after
the pipeline never ran, and with keepCallAlive the parent sat on a silent,
billed line until they hung up or the idle timeout fired.

The class is "an ending that does not end the task". The gate runs the real
Vobiz handler with a runner that ends exactly the way pipecat does
(bot_harness.ModelRunner): only an EndFrame queued at the source, an
EndTaskFrame sent upstream, a cancel, or a CancelFrame injected into the
pipeline ends it. For every ending — CLOSE phase, max turns, a stop request
followed by silence, the parent hanging up, a pipeline error — the call must
end within a bound, hang up exactly once and write its record.
"""

from __future__ import annotations

import asyncio

import pytest

from bot_harness import (
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ModelRunner,
    TranscriptionFrame,
    import_main,
    run_from_recorder,
    run_vobiz_path,
    stubbed_runtime,
)
from main_ast import RECORD

OID = "OutreachFs0000000001"


async def _model_turn(run, text="ji, dhanyavaad."):
    manager = run.processor("CallManager")
    await manager.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await manager.process_frame(LLMTextFrame(text=text), FrameDirection.DOWNSTREAM)
    await manager.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)


async def _close_phase():
    run = run_from_recorder()
    run.processor("PhaseInstructor").skip_to_close()
    await _model_turn(run, "ji, dhanyavaad, namaste.")


async def _max_turns():
    run = run_from_recorder()
    run.processor("CallManager")._max_turns = 2
    await _model_turn(run)
    await _model_turn(run)


async def _stop_request():
    run = run_from_recorder()
    guard = run.processor("StopRequestGuard")
    await guard.process_frame(TranscriptionFrame(text="दोबारा फ़ोन मत करना"), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)


async def _parent_hangs_up():
    run = run_from_recorder()
    await run.transport.handlers["on_client_disconnected"](run.transport, object())


async def _pipeline_error():
    raise RuntimeError("the pipeline blew up mid-call")


ENDINGS = {
    "close phase": _close_phase,
    "max turns": _max_turns,
    "stop request": _stop_request,
    "parent hangs up": _parent_hangs_up,
    "pipeline error": _pipeline_error,
}


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_every_ending_hangs_up_exactly_once_and_writes_the_record(ending, tmp_path, monkeypatch):
    db = FakeAsyncFirestore({("parent_outreach", OID): RECORD})
    hangups = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()
        from src import bot

        # After the opt-out line the parent says nothing; close well inside the runner's bound.
        monkeypatch.setattr(bot, "NOTED_CLOSE_AFTER_SILENCE_SECONDS", 0.05)

        async def fake_hangup(call_uuid, auth_id, auth_token):
            hangups.append(call_uuid)

        main.hangup_vobiz_call = fake_hangup
        ModelRunner.script = ENDINGS[ending]
        main.WorkerRunner = ModelRunner
        try:
            asyncio.run(asyncio.wait_for(run_vobiz_path(main, db, outreach_id=OID), 5))
        finally:
            ModelRunner.script = None
        runner = ModelRunner.last
    if ending != "pipeline error":
        assert runner.ended and not runner.timed_out, f"{ending}: the task never ended (pipecat would idle for minutes)"
    assert len(hangups) == 1, f"{ending}: hung up {len(hangups)} times"
    assert any(w[0] == "set" and w[1] == "parent_outreach" and "transcript" in w[3] for w in db.writes), \
        f"{ending}: no transcript written"


def test_a_mid_pipeline_endframe_alone_would_not_end_the_task():
    """The model is faithful: the old downstream EndFrame really leaves the task running."""
    with stubbed_runtime():
        from bot_harness import EndFrame, FakeCallStore as Store, build_bot

        async def go():
            run = await build_bot({"id": OID, "reason": "consecutive_absences", "parentLanguage": "Hindi"},
                                  call_store=Store())
            await run.processor("CallManager").push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
            runner = ModelRunner()
            ModelRunner.max_wait = 0.05
            try:
                await runner.add_workers(run.task)
                await runner.run()
            finally:
                ModelRunner.max_wait = 1.0
            return runner

        runner = asyncio.run(go())
    assert runner.timed_out
