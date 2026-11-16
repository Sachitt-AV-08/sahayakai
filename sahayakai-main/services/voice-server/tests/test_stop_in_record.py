"""CLASS GATE — a confirmed stop is in the record the teacher sees.

Independent review #11: the guard swallowed the parent's stop before the
conversation context, and the acknowledgement was spoken without being
added to it. The persisted transcript and the call summary held no trace that
the parent opted out (often just the greeting, so no summary at all), and a
teacher could not see, check or dispute why the parent would never be called
again.

The class is "something said on the call is missing from its record". The
gate runs the real Vobiz handler through a confirmed stop and requires the
written transcript to hold the parent's words, the question, the yes and the
acknowledgement, the summary to say the parent opted out with their words,
and the outreach to carry the quote and its source. The suppression document
itself keeps exactly the web app's shape, and the model still never receives
the stop (nothing passes the guard).
"""

from __future__ import annotations

import asyncio

from bot_harness import (
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    ScriptedRunner,
    TranscriptionFrame,
    import_main,
    run_from_recorder,
    run_vobiz_path,
    stubbed_runtime,
)
from main_ast import RECORD
from src.call_policy import load_call_lines

OID = "OutreachFs0000000001"
STOP = "दोबारा फ़ोन मत करना"
YES = "हाँ जी"


def test_a_confirmed_stop_reaches_the_transcript_summary_and_outreach(tmp_path, monkeypatch):
    db = FakeAsyncFirestore({("parent_outreach", OID): RECORD})
    passed = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def live():
            run = run_from_recorder()
            guard = run.processor("StopRequestGuard")
            for text in (STOP, YES):
                before = len(guard.pushed)
                await guard.process_frame(TranscriptionFrame(text=text), FrameDirection.DOWNSTREAM)
                passed.extend(guard.pushed[before:])
            await asyncio.sleep(0)

        ScriptedRunner.script = live
        main.WorkerRunner = ScriptedRunner
        try:
            asyncio.run(run_vobiz_path(main, db, outreach_id=OID))
        finally:
            ScriptedRunner.script = None

    lines = load_call_lines()["hi-IN"]
    [result] = [w[3] for w in db.writes if w[0] == "set" and w[1] == "parent_outreach" and "transcript" in w[3]]
    said = [(t["role"], t["text"]) for t in result["transcript"]]
    for entry in [("parent", STOP), ("agent", lines["stop_confirmation_question"]),
                  ("parent", YES), ("agent", lines["stop_acknowledgement"])]:
        assert entry in said, (entry, said)
    summary = result["callSummary"]
    assert "asked not to be called again" in summary["actionItemsForTeacher"][0]
    assert STOP in summary["actionItemsForTeacher"][0]
    assert summary["followUpNeeded"] is True and summary["parentResponse"]

    outreach = db.docs[("parent_outreach", OID)]
    assert outreach["optedOut"] is True and outreach["optOutSource"] == "parent_speech"
    assert STOP in outreach["optOutQuote"] and YES in outreach["optOutQuote"]

    [suppression] = [w[3] for w in db.writes if w[1] == "call_suppressions"]
    assert set(suppression) == {"teacherUid", "classId", "studentId", "outreachId", "reason", "createdAt"}
    assert passed == [], "the stop and the yes still never reach the model"
