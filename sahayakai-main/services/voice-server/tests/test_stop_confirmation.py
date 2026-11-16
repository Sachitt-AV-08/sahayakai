"""CLASS GATE — a do-not-call record is written only on the parent's explicit yes.

Independent review (#5, #8 high, #9): the phrase lists fired on invitations
("call karna na"), hang-up phrases ("phone band karo"), time limits ("never
call me before 10"), third-party requests ("मेरे पति को फ़ोन मत करना, मुझे
करना") and redirections ("I'll come myself"), and each wrote a PERMANENT
suppression on the spot. They also missed common ways to say it ("ફોન કરશો
નહીં", "ഇനി വിളിക്കണ്ട", "do not disturb me").

The class is "a phrase match alone silences a parent". The gate: a possible
stop only ASKS one fixed, localized question; only an explicit yes writes; a
no, an unclear answer or silence carries on and writes nothing; every
false-positive phrase from the review, in every language, never writes; every
missed phrasing now triggers the question; and the post-call analysis can
only flag, never suppress.
"""

from __future__ import annotations

import asyncio

import pytest

from bot_harness import (
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    TranscriptionFrame,
    build_bot,
    import_main,
    run_from_recorder,
    run_vobiz_path,
    stubbed_runtime,
)
from src.call_policy import load_call_lines
from src.opt_out import (
    SUPPRESSION_COLLECTION,
    Confirmation,
    StopAction,
    StopConversation,
    classify_confirmation,
    is_stop_request,
)

PHONE = "+919800000101"
CTX = {"id": "OutreachFs0000000001", "studentName": "Aarav", "reason": "consecutive_absences",
       "parentLanguage": "Hindi", "teacherName": "Ms. Sharma", "schoolName": "Demo Public School",
       "parentPhone": PHONE}

# Review #8: invitations, hang-ups, time limits, complaints, third parties, redirections.
FALSE_STOPS_8 = [
    "haan dobara call karna na", "kabhi bhi call kar lena na", "phir se call karna na please",
    "अभी फ़ोन बंद कर दो, मैं बाद में बात करूँगा", "ok phone band karo", "Never call me before 10",
    "Please stop calling during work hours", "No more calls today please", "Don't call again today, call tomorrow",
    "आज दोबारा फ़ोन मत करना, कल शाम को करना", "Do not call me again until the exams", "The teacher never call me",
    "उनको दोबारा फ़ोन मत करना, मुझे करना", "मेरे पति को फ़ोन मत करना, मुझे करना",
    "I don't want you to call my husband", "Don't call me, I'll call you", "आप फ़ोन मत करो, मैं ख़ुद फ़ोन कर लूँगा",
    "हाँ, फ़ोन बंद कर दीजिए", "अच्छा ठीक है फ़ोन बंद करो", "phone band karo abhi", "haan kal phir se call karna na",
]
# Review #5: third parties and redirections in Hindi, Bengali and Hinglish.
FALSE_STOPS_5 = [
    "मेरे पति को फोन मत करना, मुझे करना", "আমার স্বামীকে ফোন করবেন না, আমাকে করুন", "unke papa ko phone mat karna",
    "फोन मत कीजिए, मैं खुद स्कूल आ जाऊँगा", "phone mat karo, main khud school aa jaunga",
    "फोन मत करना, मैं ड्राइव कर रहा हूँ", "ওর বাবাকে ফোন করবেন না", "ওকে ফোন করবেন না",
    "বাচ্চে को फोन मत करना", "उसके पापा को फोन मत करना", "তার বাবাকে ফোন করবেন না",
]
FALSE_STOPS = FALSE_STOPS_8 + FALSE_STOPS_5

# Review #9: ways to say it that were missed. Each must now ask.
MISSED_STOPS_9 = [
    "ફરીથી ફોન કરશો નહીં", "ફોન કરશો નહીં", "दोबारा फ़ोन नहीं करना", "मुझे फ़ोन नहीं करना", "पुन्हा फोन नका करू",
    "ਫੋਨ ਨਹੀਂ ਕਰਨਾ", "ഇനി വിളിക്കണ്ട", "ഇനി ഫോൺ ചെയ്യണ്ട", "আর কল দিবেন না", "আমাকে আর ফোন দেবেন না",
    "do not disturb me", "I don't want to receive any calls",
]

YES_ANSWERS = ["yes", "yes please", "haan", "हाँ", "जी हाँ", "हाँ जी", "হ্যাঁ", "होय", "હા", "ਹਾਂ ਜੀ",
               "ಹೌದು", "ஆமாம்", "అవును", "അതെ", "bilkul"]
NOT_YES_ANSWERS = ["no", "नहीं", "nahi nahi, call karte rahiye", "না", "ಇಲ್ಲ", "வேண்டாம்", "హ్మ్",
                   "haan... nahi, matlab", "what?", "who is this", "theek hai", "okay",
                   "yes yes I mean the teacher can call me any evening after six", "ha ha"]


# ── the pure decision ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", FALSE_STOPS)
def test_a_false_stop_phrase_never_confirms_on_its_own(text):
    c = StopConversation()
    first = c.on_parent_speech(text)
    assert first in (StopAction.PASS, StopAction.ASK)
    c.on_no_answer()
    assert c.on_parent_speech("acha theek hai") in (StopAction.PASS, StopAction.ASK, StopAction.DECLINE)
    assert c.confirmed is False


def test_the_split_dont_call_me_right_now_case_never_confirms():
    c = StopConversation()
    c.on_parent_speech("Don't call me")
    assert c.on_parent_speech("right now, I am driving") is StopAction.DECLINE
    assert c.confirmed is False


@pytest.mark.parametrize("text", MISSED_STOPS_9)
def test_every_phrasing_the_review_found_missing_now_asks(text):
    assert is_stop_request(text), text
    assert StopConversation().on_parent_speech(text) is StopAction.ASK


@pytest.mark.parametrize("answer", YES_ANSWERS)
def test_an_explicit_yes_confirms(answer):
    c = StopConversation()
    assert c.on_parent_speech("please don't call me again") is StopAction.ASK
    assert c.on_parent_speech(answer) is StopAction.CONFIRM


@pytest.mark.parametrize("answer", NOT_YES_ANSWERS)
def test_no_or_unclear_never_confirms(answer):
    c = StopConversation()
    assert c.on_parent_speech("please don't call me again") is StopAction.ASK
    assert c.on_parent_speech(answer) is StopAction.DECLINE
    assert c.confirmed is False


def test_silence_lapses_the_question():
    c = StopConversation()
    c.on_parent_speech("please don't call me again")
    c.on_no_answer()
    assert c.on_parent_speech("haan") is StopAction.PASS, "a later 'haan' is not an answer to a lapsed question"


def test_our_own_question_heard_back_is_ignored_while_waiting():
    question = load_call_lines()["en-IN"]["stop_confirmation_question"]
    c = StopConversation(question=question)
    c.on_parent_speech("please don't call me again")
    assert c.on_parent_speech(question) is StopAction.IGNORE
    assert c.on_parent_speech("yes") is StopAction.CONFIRM


def test_the_question_is_asked_at_most_twice_per_call():
    c = StopConversation()
    for _ in range(2):
        assert c.on_parent_speech("don't call me") is StopAction.ASK
        assert c.on_parent_speech("no") is StopAction.DECLINE
    assert c.on_parent_speech("don't call me") is StopAction.PASS


def test_none_of_the_engine_lines_could_be_heard_as_a_yes():
    for code, entry in load_call_lines().items():
        for key in ("request_to_talk", "explain_in_person", "stop_acknowledgement", "stop_confirmation_question"):
            assert classify_confirmation(entry[key], code) is not Confirmation.YES, (code, key)
            assert not is_stop_request(entry[key]), (code, key)


def test_every_language_has_a_confirmation_question():
    for code, entry in load_call_lines().items():
        assert entry["stop_confirmation_question"].strip(), code


# ── the real pipeline ────────────────────────────────────────────────────────


class Writer:
    def __init__(self):
        self.calls = 0

    async def __call__(self, *args):
        self.calls += 1
        return True


async def _hear(run, text):
    guard = run.processor("StopRequestGuard")
    before = len(guard.pushed)
    await guard.process_frame(TranscriptionFrame(text=text), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)
    return [f.text for f, _ in guard.pushed[before:]]


def _question():
    return load_call_lines()["hi-IN"]["stop_confirmation_question"]


@pytest.mark.parametrize("text", FALSE_STOPS + ["Don't call me"])
@pytest.mark.parametrize("then", ["silence", "carries on", "says no"])
def test_a_false_stop_phrase_never_writes_through_the_pipeline(text, then, monkeypatch):
    writer = Writer()
    with stubbed_runtime():
        from src import stop_guard

        monkeypatch.setattr(stop_guard, "CONFIRM_ANSWER_SECONDS", 0.01)

        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=writer)
            await _hear(run, text)
            if then == "silence":
                await asyncio.sleep(0.05)
            elif then == "carries on":
                await _hear(run, "right now, I am driving, call me in the evening")
            else:
                await _hear(run, "नहीं")
            await run.finalize()
            return run

        run = asyncio.run(go())
    assert writer.calls == 0, (text, then)
    assert "EndFrame" not in [type(f).__name__ for f in run.task.queued]


@pytest.mark.parametrize("text", MISSED_STOPS_9)
def test_a_missed_phrasing_asks_then_writes_only_on_yes(text):
    writer = Writer()
    with stubbed_runtime():
        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=writer)
            passed = await _hear(run, text)
            asked = [f.text for f in run.task.queued]
            before_yes = writer.calls
            await _hear(run, "हाँ")
            return asked, before_yes, passed

        asked, before_yes, passed = asyncio.run(go())
    assert asked == [_question()] and before_yes == 0 and passed == []
    assert writer.calls == 1


@pytest.mark.parametrize("answer", ["नहीं", "theek hai, shaam ko karna", "what?"])
def test_no_or_unclear_carries_on_and_the_model_hears_the_answer(answer):
    writer = Writer()
    with stubbed_runtime():
        async def go():
            run = await build_bot(CTX, call_store=FakeCallStore(), on_opt_out=writer)
            await _hear(run, "please don't call me again")
            passed = await _hear(run, answer)
            await run.finalize()
            return run, passed

        run, passed = asyncio.run(go())
    assert writer.calls == 0
    assert passed == [answer], "the answer goes to the model so the call carries on"
    assert [type(f).__name__ for f in run.task.queued] == ["TTSSpeakFrame"], "only the question; no end"
    # The model is told what was asked, so it can carry on sensibly.
    contents = [m["content"] for m in run.context.messages]
    assert "please don't call me again" in contents and _question() in contents


def test_the_analysis_flags_the_outreach_and_never_suppresses(tmp_path, monkeypatch):
    db = FakeAsyncFirestore({("parent_outreach", "OutreachFs0000000001"): {
        "parentPhone": PHONE, "teacherUid": "t", "classId": "c", "studentId": "s", "studentName": "Aarav",
        "reason": "consecutive_absences", "parentLanguage": "Hindi"}})
    quote = "we would rather the school did not ring this house"
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()
        from src import bot

        async def analysis(call_context, transcript, api_key=None, **kwargs):
            return {"optOut": {"requested": True, "quote": quote}}

        monkeypatch.setattr(bot, "analyze_call", analysis)

        async def go():
            # The handler finalizes when the (stubbed) pipeline ends; give the
            # analysis a parent turn to quote first.
            from bot_harness import ScriptedRunner

            async def live():
                run_from_recorder().context.add_message({"role": "user", "content": f"Honestly, {quote}."})

            ScriptedRunner.script = live
            main.WorkerRunner = ScriptedRunner
            try:
                await run_vobiz_path(main, db)
            finally:
                ScriptedRunner.script = None

        asyncio.run(go())
    assert [w for w in db.writes if w[1] == SUPPRESSION_COLLECTION] == []
    record = db.docs[("parent_outreach", "OutreachFs0000000001")]
    assert record["optOutSuspected"] is True and record["optOutSuspectedQuote"] == quote
    assert "optedOut" not in record
