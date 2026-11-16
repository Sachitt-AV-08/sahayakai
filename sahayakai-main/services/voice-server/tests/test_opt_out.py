"""CLASS GATE — a parent's "do not call me again" outlives the call.

The live engine had no stop handling at all: a parent who asked not to be
called was answered by the model (which might even say "noted") and the next
click called them again. The web app refuses a dial when
call_suppressions/{sha256(E.164)} exists, so the class of bug is "a stop
request that does not end up as that exact document". The gate:

  * stop writes, non-stop never does: the real create_bot pipeline (pipecat
    stubbed) is fed the parent's words; a stop request writes the suppression
    under the right id with the web app's document shape, speaks the fixed
    acknowledgement, ends the call and keeps the words from the model; any
    ordinary or rescheduling sentence writes nothing;
  * the authenticated entry point (Vobiz) wires the writer to Firestore
    (/ws/call deliberately does not; see test_opt_out_auth.py);
  * the post-call backstop counts only a verbatim parent quote;
  * digest parity: the shared fixture digest equals what Python computes,
    what Node computes with the web module's exact expression, and the
    literals pinned in the web and agents tests when they are present;
  * the detector never fires on the engine's own lines (echo).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bot_harness import (
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    TranscriptionFrame,
    build_bot,
    import_main,
    monorepo_root,
    run_from_recorder,
    run_vobiz_path,
    stubbed_runtime,
)
from src.call_policy import load_call_lines
from src.opt_out import (
    SUPPRESSION_COLLECTION,
    StopAction,
    StopConversation,
    backstop_quote,
    is_stop_request,
    persist_opt_out,
    phone_suppression_id,
    suppression_record,
    utc_now_iso,
)

TESTS = Path(__file__).resolve().parent
REPO = monorepo_root()  # None when the service is built on its own
FIXTURE = json.loads((TESTS / "fixtures" / "suppression_digest.json").read_text(encoding="utf-8"))
PHONE = FIXTURE["cases"][0]["phone"]
DIGEST = FIXTURE["cases"][0]["digest"]
RECORD_KEYS = {"teacherUid", "classId", "studentId", "outreachId", "reason", "createdAt"}


def outreach(phone: str = PHONE, **extra) -> dict:
    return {
        "parentPhone": phone,
        "teacherUid": "teacher-test-1",
        "classId": "class-test-1",
        "studentId": "student-test-1",
        "studentName": "Aarav",
        "reason": "consecutive_absences",
        "parentLanguage": "Hindi",
        "teacherName": "Ms. Sharma",
        "schoolName": "Demo Public School",
        **extra,
    }


def suppression_writes(db: FakeAsyncFirestore) -> list[tuple]:
    return [w for w in db.writes if w[1] == SUPPRESSION_COLLECTION]


# ── digest parity ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["phone"])
def test_python_digest_matches_the_shared_fixture(case):
    assert phone_suppression_id(case["phone"]) == case["digest"]
    assert phone_suppression_id(f"  {case['phone']}\n") == case["digest"], "trimmed like JS .trim()"
    assert case["phone"].lstrip("+") not in phone_suppression_id(case["phone"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
def test_node_computes_the_same_digest_with_the_web_modules_expression():
    # The body of phoneSuppressionId in sahayakai-main/src/lib/call-suppression.ts.
    expr = "require('crypto').createHash('sha256').update(process.argv[1].trim(), 'utf8').digest('hex')"
    for case in FIXTURE["cases"]:
        out = subprocess.run(["node", "-e", f"process.stdout.write({expr})", case["phone"]],
                             capture_output=True, text=True, timeout=30, check=True)
        assert out.stdout == case["digest"]


def _pinned(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    phone = re.search(r"FIXTURE_PHONE\s*=\s*['\"]([^'\"]+)['\"]", text)
    digest = re.search(r"FIXTURE_DIGEST\s*=\s*['\"]([0-9a-f]{64})['\"]", text)
    assert phone and digest, f"{path} no longer pins FIXTURE_PHONE/FIXTURE_DIGEST"
    return phone.group(1), digest.group(1)


@pytest.mark.parametrize("relative", [
    "sahayakai-main/src/__tests__/lib/call-suppression.test.ts",
    "sahayakai-agents/tests/unit/test_telephony_opt_out_persistence.py",
])
def test_the_web_and_agents_tests_pin_the_same_pair(relative):
    if REPO is None:
        pytest.skip("this service was copied out of the monorepo (e.g. the Cloud Build upload)")
    pinned = _pinned(REPO / relative)
    if pinned is None:
        pytest.skip(f"{relative} is not on this branch yet; the check runs once it is")
    assert pinned == (PHONE, DIGEST)


# ── the write ────────────────────────────────────────────────────────────────


def test_persist_writes_the_web_apps_document_under_the_digest():
    db = FakeAsyncFirestore({("parent_outreach", "out-1"): outreach()})
    result = asyncio.run(persist_opt_out(db, "out-1", now="2026-11-15T10:00:00.000000Z"))

    assert result.ok, result.detail
    writes = suppression_writes(db)
    assert len(writes) == 1
    kind, _coll, doc_id, record, _merge = writes[0]
    assert kind == "set" and doc_id == DIGEST
    assert set(record) == RECORD_KEYS
    assert record == {
        "teacherUid": "teacher-test-1",
        "classId": "class-test-1",
        "studentId": "student-test-1",
        "outreachId": "out-1",
        "reason": "opt_out",
        "createdAt": "2026-11-15T10:00:00.000000Z",
    }
    flag = db.docs[("parent_outreach", "out-1")]
    assert flag["optedOut"] is True and flag["optedOutAt"] == "2026-11-15T10:00:00.000000Z"
    for _k, _c, doc, data, _m in db.writes:
        assert "9800000101" not in doc and PHONE not in repr(data)
    assert "9800000101" not in result.detail


def test_default_timestamp_matches_the_agents_bridge_shape():
    record = suppression_record(outreach(), "out-1", utc_now_iso())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", record["createdAt"])


def test_no_phone_means_no_suppression_and_a_reported_failure():
    db = FakeAsyncFirestore({("parent_outreach", "out-1"): outreach(phone="")})
    result = asyncio.run(persist_opt_out(db, "out-1"))
    assert not result.ok and suppression_writes(db) == []


def test_firestore_failure_is_reported_not_raised():
    db = FakeAsyncFirestore(fail=RuntimeError("firestore down"))
    result = asyncio.run(persist_opt_out(db, "out-1"))
    assert not result.ok and "firestore down" in result.detail


def test_the_suppression_is_written_even_if_the_outreach_flag_fails():
    db = FakeAsyncFirestore({("parent_outreach", "out-1"): outreach()})

    async def broken_update(self, data):
        raise RuntimeError("update failed")

    from bot_harness import _AsyncDoc

    original = _AsyncDoc.update
    _AsyncDoc.update = broken_update
    try:
        result = asyncio.run(persist_opt_out(db, "out-1"))
    finally:
        _AsyncDoc.update = original
    assert result.ok and len(suppression_writes(db)) == 1


# ── detection ────────────────────────────────────────────────────────────────

STOPS = [
    "Please don't call me again.", "Do not call this number again", "Stop calling me!",
    "No more calls please", "Remove my number", "Don't call me.", "Please don't call.",
    "I don't want these calls", "We don't want you to call",
    "दोबारा फ़ोन मत करना", "आगे से फोन मत कीजिए", "फोन मत करो", "मेरा नंबर हटा दीजिए",
    "phir se call mat karna", "phone mat karo", "dobara phone mat karna",
    "আর ফোন করবেন না", "ফোন করবেন না", "पुन्हा फोन करू नका", "फोन करू नका",
    "ફરીથી ફોન ન કરશો", "ફોન ન કરો", "ਦੁਬਾਰਾ ਫ਼ੋਨ ਨਾ ਕਰੋ", "ਫੋਨ ਨਾ ਕਰੋ",
    "ಮತ್ತೆ ಫೋನ್ ಮಾಡಬೇಡಿ", "ಫೋನ್ ಮಾಡಬೇಡಿ", "இனிமேல் போன் பண்ணாதீங்க", "கூப்பிடாதீங்க",
    "మళ్ళీ ఫోన్ చేయకండి", "ఫోన్ చేయవద్దు", "ഇനി വിളിക്കരുത്", "വിളിക്കരുത്",
]
NOT_STOPS = [
    "Don't call me now, I'm in a meeting", "Please don't call in the morning", "Call me in the evening",
    "Yes, tomorrow at 5 is fine", "Why is the teacher calling?", "Is it about his marks?",
    "Don't call him naughty", "Okay thank you, bye", "haan ji boliye", "",
    "अभी फोन मत करो, बाद में करना", "शाम को फोन करना", "उसको फोन मत करो", "abhi phone mat karo",
    "এখন ফোন করবেন না", "आता फोन करू नका", "હમણાં ફોન ન કરો", "ਹੁਣ ਫੋਨ ਨਾ ਕਰੋ",
    "ಈಗ ಫೋನ್ ಮಾಡಬೇಡಿ", "இப்போ போன் பண்ணாதீங்க", "ఇప్పుడు ఫోన్ చేయకండి", "ഇപ്പോൾ വിളിക്കേണ്ട",
]


@pytest.mark.parametrize("text", STOPS)
def test_stop_requests_are_recognised(text):
    assert is_stop_request(text)


@pytest.mark.parametrize("text", NOT_STOPS)
def test_ordinary_and_rescheduling_replies_are_not_stops(text):
    assert not is_stop_request(text)


def test_the_engine_never_hears_its_own_lines_as_a_stop():
    from src.prompts.agent_reply import CALL_GREETINGS

    own = [v for entry in load_call_lines().values() for k, v in entry.items() if k != "review"]
    own += [g["greeting"] for g in CALL_GREETINGS.values()]
    own += ["ज़रा दोबारा बोलिए, आवाज़ ठीक से नहीं आ रही।"]  # STTConfidenceFilter's repeat prompt
    assert [t for t in own if is_stop_request(t)] == []


def test_a_stop_split_across_two_transcriptions_still_asks():
    c = StopConversation()
    assert c.on_parent_speech("please don't") is StopAction.PASS
    assert c.on_parent_speech("call me again") is StopAction.ASK


def test_an_earlier_reschedule_cannot_veto_a_later_plain_stop():
    c = StopConversation()
    assert c.on_parent_speech("I am free in the evening") is StopAction.PASS
    assert c.on_parent_speech("actually, don't call me") is StopAction.ASK


# ── backstop ─────────────────────────────────────────────────────────────────

TRANSCRIPT = [
    {"role": "agent", "text": "Is there a good time for a short call?"},
    {"role": "parent", "text": "Honestly we would prefer the school stops ringing this house."},
]


def test_backstop_counts_a_verbatim_parent_quote():
    insights = {"optOut": {"requested": True, "quote": "prefer the school stops ringing this house"}}
    assert backstop_quote(insights, TRANSCRIPT) == "prefer the school stops ringing this house"


@pytest.mark.parametrize("insights", [
    {"optOut": {"requested": True, "quote": "please never call us"}},               # not said
    {"optOut": {"requested": True, "quote": "Is there a good time"}},               # agent's words
    {"optOut": {"requested": "true", "quote": "stops ringing this house"}},         # not exactly True
    {"optOut": {"requested": True, "quote": ""}},
    {"optOut": {"requested": False, "quote": "stops ringing this house"}},
    {"optOut": {"requested": True, "quote": "call me in the evening"}},             # reschedule
    {"optOut": None}, {}, None,
])
def test_backstop_ignores_anything_but_a_verbatim_stop(insights):
    transcript = TRANSCRIPT + [{"role": "parent", "text": "call me in the evening"}]
    assert backstop_quote(insights, transcript) is None


# ── the real pipeline (create_bot, pipecat stubbed) ──────────────────────────


class Writer:
    def __init__(self, results=(True,)):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, *args):
        self.calls += 1
        return self.results[min(self.calls, len(self.results)) - 1]


async def _hear(run, text: str):
    guard = run.processor("StopRequestGuard")
    before = len(guard.pushed)
    await guard.process_frame(TranscriptionFrame(text=text), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)
    return guard.pushed[before:]


YES = {"Hindi": "हाँ", "Tamil": "ஆமாம்", "English": "yes"}


async def _stop_and_confirm(run, text: str, yes: str = "yes"):
    """The parent asks not to be called, hears the question, and says yes."""
    return await _hear(run, text) + await _hear(run, yes)


@pytest.mark.parametrize("reason", ["consecutive_absences", "positive_feedback"])
@pytest.mark.parametrize("language", ["Hindi", "Tamil", "English"])
def test_a_stop_writes_acknowledges_ends_and_hides_the_words_from_the_model(reason, language):
    writer = Writer()
    ctx = {**outreach(), "id": "out-1", "reason": reason, "parentLanguage": language}
    with stubbed_runtime():
        async def go():
            run = await build_bot(ctx, call_store=FakeCallStore(), on_opt_out=writer)
            passed = await _stop_and_confirm(run, "Please don't call me again", YES[language])
            after = await _hear(run, "hello? are you there?")
            return run, passed, after

        run, passed, after = asyncio.run(go())

    lines = load_call_lines()[{"Hindi": "hi-IN", "Tamil": "ta-IN", "English": "en-IN"}[language]]
    assert writer.calls == 1
    assert passed == [] and after == [], "the model must not see the stop, the yes, or anything after"
    assert [type(f).__name__ for f in run.task.queued] == ["TTSSpeakFrame", "TTSSpeakFrame", "EndFrame"]
    assert run.task.queued[0].text == lines["stop_confirmation_question"]
    assert run.task.queued[1].text == lines["stop_acknowledgement"]


@pytest.mark.parametrize("text", NOT_STOPS[:12])
def test_a_non_stop_writes_nothing_and_reaches_the_model(text):
    writer = Writer()
    with stubbed_runtime():
        async def go():
            run = await build_bot({**outreach(), "id": "out-1"}, call_store=FakeCallStore(), on_opt_out=writer)
            return run, await _hear(run, text)

        run, passed = asyncio.run(go())
    assert writer.calls == 0
    assert run.task.queued == []
    if text:
        assert [f.text for f, _ in passed] == [text]


def test_a_failed_write_is_retried_once_at_the_end_of_the_call():
    writer = Writer(results=(False, True))
    with stubbed_runtime():
        async def go():
            run = await build_bot({**outreach(), "id": "out-1"}, call_store=FakeCallStore(), on_opt_out=writer)
            await _stop_and_confirm(run, "stop calling me")
            await run.finalize()
            await run.finalize()  # idempotent
        asyncio.run(go())
    assert writer.calls == 2


@pytest.mark.parametrize("found", [True, False])
def test_the_post_call_backstop_only_flags_and_never_suppresses(found, monkeypatch):
    writer = Writer()
    flagged = []

    async def flag(quote):
        flagged.append(quote)
        return True

    quote = "we would rather the school did not ring this house"
    with stubbed_runtime():
        from src import bot

        async def fake_analysis(call_context, transcript, api_key=None, **kwargs):
            return {"optOut": {"requested": True, "quote": quote if found else "something never said"}}

        monkeypatch.setattr(bot, "analyze_call", fake_analysis)

        async def go():
            run = await build_bot({**outreach(), "id": "out-1"}, call_store=FakeCallStore(),
                                  on_opt_out=writer, on_suspected_opt_out=flag)
            run.context.add_message({"role": "user", "content": f"Honestly, {quote}."})
            await run.finalize()
        asyncio.run(go())
    assert writer.calls == 0, "the analysis must never write a suppression by itself"
    assert flagged == ([quote] if found else [])


# ── both entry points write to Firestore ─────────────────────────────────────


def test_vobiz_path_writes_the_suppression_and_hangs_up_once(tmp_path, monkeypatch):
    db = FakeAsyncFirestore({("parent_outreach", "OutreachFs0000000001"): outreach()})
    hangups = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def fake_hangup(call_uuid, auth_id, auth_token):
            hangups.append(call_uuid)

        main.hangup_vobiz_call = fake_hangup

        async def go():
            await run_vobiz_path(main, db)  # runs to the end: pipeline, hangup, finalize
            run = run_from_recorder()
            await _stop_and_confirm(run, "दोबारा फ़ोन मत करना", "हाँ")
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(go())

    assert hangups == ["123e4567-e89b-42d3-a456-426614174000"], "the leg is ended exactly once after the pipeline stops"
    writes = suppression_writes(db)
    assert len(writes) == 1 and writes[0][2] == DIGEST and writes[0][3]["outreachId"] == "OutreachFs0000000001"
    assert any(w[0] == "set" and w[1] == "parent_outreach" and "transcript" in w[3] for w in db.writes)


# /ws/call is unauthenticated, so it must NOT write: see
# test_opt_out_auth.test_ws_call_cannot_write_a_suppression_for_a_named_outreach.


def test_a_built_in_test_call_persists_nothing(tmp_path, monkeypatch):
    db = FakeAsyncFirestore()
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()
        from src import vobiz

        vobiz._db = lambda project: db

        async def go():
            main.config = replace(main.config, allow_test_scenarios=True)
            await run_vobiz_path(main, db, outreach_id="__test__003")
            run = run_from_recorder()
            await _stop_and_confirm(run, "please stop calling")
            for _ in range(5):
                await asyncio.sleep(0)
            return run
        run = asyncio.run(go())
    # Only the stream-token consumption touches Firestore on a built-in test call.
    assert [w[1] for w in db.writes] == ["vobiz_stream_tokens"] and db.reads == []
    assert [type(f).__name__ for f in run.task.queued][-2:] == ["TTSSpeakFrame", "EndFrame"]
