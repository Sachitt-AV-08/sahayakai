"""CLASS GATE — no model reply promising "no more calls" is ever spoken, stop detected or not.

Follow-up to final-0b #1: after a detected stop the model is silenced, but
when the stop detector does NOT fire, the model's reply was spoken unchecked,
and positive_feedback's prompt has no rule against promising. Phrasings the
detector missed ("Stop calling me, I am busy", "मुझे ये कॉल्स नहीं चाहिए",
"बंद करो ये कॉल") let "जी ठीक है, अब आपको फ़ोन नहीं करेंगे।" through, a
promise the school may break.

The class is "a promise of no more calls reaching the parent". The gate:
  * every reply is screened before speech: promise replies in all ten call
    languages (each language's old acknowledgement among them), fed with no
    stop detected, in both call modes, never reach TTS; the reply is handled
    as a stop: optOutRequested is recorded with the parent's last words
    (source live), the opt-out line and the fixed goodbye are said, the call
    hangs up once and the summary leads with the note;
  * on the unauthenticated route the promise is still never said (goodbye
    only, nothing recorded);
  * ordinary replies, rescheduling replies, replies about someone else and
    the engine's own lines are not promises, and are spoken as before;
  * the phrasings the detector missed are now detected.
"""

from __future__ import annotations

import asyncio

import pytest

from bot_harness import (
    RECORDER,
    FakeAsyncFirestore,
    FakeCallStore,
    FrameDirection,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ModelRunner,
    ScriptedRunner,
    TranscriptionFrame,
    TTSSpeakFrame,
    import_main,
    run_from_recorder,
    run_twilio_path,
    run_vobiz_path,
    stubbed_runtime,
)
from main_ast import RECORD, opt_out_requests, suppressions
from src.call_policy import LANGUAGE_CODES, load_call_lines
from src.opt_out import is_no_more_calls_promise, is_stop_request
from test_opt_out_never_suppresses import OLD_PROMISES

OID = "OutreachFs0000000001"

# One promise per call language (the engine's own old acknowledgement in
# each), plus the review's examples and a few more ways to say it.
PROMISES = {
    "English": ["Okay, we won't call you anymore, thank you.", "We will never call you again.",
                "Of course, we will not call you again."],
    "Hindi": ["जी ठीक है, अब आपको फ़ोन नहीं करेंगे।", "हम आपको दोबारा फ़ोन नहीं करेंगे।",
              "theek hai, ab hum phone nahi karenge."],
    "Bengali": ["অবশ্যই, আমরা আপনাকে আর ফোন করব না।"],
    "Marathi": ["नक्कीच, आम्ही तुम्हाला पुन्हा फोन करणार नाही."],
    "Gujarati": ["ચોક્કસ, અમે તમને ફરીથી ફોન નહીં કરીએ."],
    "Punjabi": ["ਜੀ ਬਿਲਕੁਲ, ਅਸੀਂ ਤੁਹਾਨੂੰ ਦੁਬਾਰਾ ਫ਼ੋਨ ਨਹੀਂ ਕਰਾਂਗੇ।"],
    "Kannada": ["ಖಂಡಿತ, ನಾವು ನಿಮಗೆ ಮತ್ತೆ ಕರೆ ಮಾಡುವುದಿಲ್ಲ."],
    "Tamil": ["கண்டிப்பாக, நாங்கள் இனி உங்களை அழைக்க மாட்டோம்."],
    "Telugu": ["తప్పకుండా, మేము మీకు మళ్ళీ ఫోన్ చేయము."],
    "Malayalam": ["തീർച്ചയായും, ഞങ്ങൾ ഇനി നിങ്ങളെ വിളിക്കില്ല."],
}
NOT_PROMISES = [
    "The class teacher will call you on Tuesday at five.", "Is there a good time this week?",
    "क्लास टीचर आपको शाम को फ़ोन करेंगे।", "अभी फ़ोन नहीं करेंगे, शाम को करेंगे।",
    "We will not call your husband, only you.", "Don't worry, it will not take long.",
    "Thank you, have a lovely evening, goodbye.", "आरव ने इस हफ़्ते बहुत अच्छा किया।",
]
MISSED_STOPS = ["Stop calling me, I am busy", "मुझे दोबारा फ़ोन मत करना, मैं व्यस्त रहता हूँ",
                "मुझे ये कॉल्स नहीं चाहिए", "बंद करो ये कॉल"]
MODES = {
    "request_to_talk": {"reason": "consecutive_absences"},
    "positive_feedback": {"reason": "positive_feedback", "studentName": "Vaibhav",
                          "generatedMessage": "Hello, this is Ms. Sharma. Vaibhav did very well this week."},
}
PARENT_SAYS = "ठीक है जी, समझ गए"  # not a stop: the detector stays silent


# ── the pure pieces ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [t for ts in PROMISES.values() for t in ts] + OLD_PROMISES)
def test_promises_are_recognised(text):
    assert is_no_more_calls_promise(text), text


@pytest.mark.parametrize("text", NOT_PROMISES)
def test_ordinary_replies_are_not_promises(text):
    assert not is_no_more_calls_promise(text), text


def test_none_of_the_engine_lines_is_a_promise():
    from src.prompts.agent_reply import CALL_GREETINGS

    own = [v for entry in load_call_lines().values() for k, v in entry.items() if k != "review"]
    own += [g["greeting"] for g in CALL_GREETINGS.values()]
    assert [t for t in own if is_no_more_calls_promise(t)] == []


@pytest.mark.parametrize("text", MISSED_STOPS)
def test_the_missed_phrasings_are_now_detected(text):
    assert is_stop_request(text)


def test_the_parent_line_used_below_is_not_a_stop():
    assert not is_stop_request(PARENT_SAYS)


# ── through the real handler ─────────────────────────────────────────────────


async def _model_reply(run, text):
    manager = run.processor("CallManager")
    await manager.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    for part in (text[: len(text) // 3], text[len(text) // 3: 2 * len(text) // 3], text[2 * len(text) // 3:]):
        await manager.process_frame(LLMTextFrame(text=part), FrameDirection.DOWNSTREAM)
    await manager.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)


def _vobiz_call(language, mode, reply, tmp_path, monkeypatch, parent_says=None):
    parent_says = PARENT_SAYS if parent_says is None else parent_says
    db = FakeAsyncFirestore({("parent_outreach", OID): {**RECORD, **MODES[mode], "parentLanguage": language}})
    hangups = []
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def fake_hangup(call_uuid, auth_id, auth_token):
            hangups.append(call_uuid)

        async def script():
            run = run_from_recorder()
            guard = run.processor("StopRequestGuard")
            await guard.process_frame(TranscriptionFrame(text=parent_says), FrameDirection.DOWNSTREAM)
            run.context.add_message({"role": "user", "content": parent_says})  # the aggregator's job
            await _model_reply(run, reply)

        main.hangup_vobiz_call = fake_hangup
        ModelRunner.script = script
        main.WorkerRunner = ModelRunner
        try:
            asyncio.run(asyncio.wait_for(run_vobiz_path(main, db, outreach_id=OID), 5))
        finally:
            ModelRunner.script = None
        runner = ModelRunner.last
        run = run_from_recorder()
        manager = run.processor("CallManager")
    model_text = "".join(f.text for f, _ in manager.pushed if isinstance(f, LLMTextFrame))
    tts = [f.text for f, _ in manager.pushed if isinstance(f, TTSSpeakFrame)]
    return runner, run, model_text, tts, hangups, db


CASES = [(lang, mode, reply) for lang in ("English", "Hindi", "Bengali", "Tamil", "Malayalam", "Kannada")
         for mode in sorted(MODES) for reply in PROMISES[lang][:2]]


@pytest.mark.parametrize("language,mode,reply", CASES, ids=lambda v: v if len(v) < 40 else v[:40])
def test_a_promise_with_no_stop_detected_never_reaches_tts(language, mode, reply, tmp_path, monkeypatch):
    runner, run, model_text, tts, hangups, db = _vobiz_call(language, mode, reply, tmp_path, monkeypatch)
    lines = load_call_lines()[LANGUAGE_CODES[language]]
    assert model_text == "", "no model text reached TTS"
    assert tts == [lines["opt_out_noted"], lines["goodbye"]]
    assert runner.ended and not runner.timed_out and len(hangups) == 1
    [request] = opt_out_requests(db)
    assert request["quote"] == PARENT_SAYS and request["source"] == "live"
    assert suppressions(db) == []
    [result] = [w[3] for w in db.writes if w[0] == "set" and "callSummary" in w[3]]
    assert result["callSummary"]["actionItemsForTeacher"][0].startswith("The parent said they would prefer not")
    said = [t["text"] for t in result["transcript"]]
    assert reply not in said and lines["goodbye"] in said


@pytest.mark.parametrize("mode", sorted(MODES))
def test_an_ordinary_reply_is_spoken_as_before(mode, tmp_path, monkeypatch):
    reply = "Thank you, the class teacher will call you this evening."
    _runner, _run, model_text, tts, _hangups, db = _vobiz_call("English", mode, reply, tmp_path, monkeypatch)
    assert model_text == reply and tts == []
    assert opt_out_requests(db) == []


def test_on_the_unauthenticated_route_the_promise_is_still_never_said(tmp_path, monkeypatch):
    web_ctx = {"studentName": "Aarav", "reason": "", "requestToTalk": True, "parentLanguage": "Hindi"}
    with stubbed_runtime():
        main = import_main(tmp_path, monkeypatch)
        main.call_store = FakeCallStore()

        async def script():
            await _model_reply(run_from_recorder(), PROMISES["Hindi"][0])

        ScriptedRunner.script = script
        main.WorkerRunner = ScriptedRunner
        try:
            asyncio.run(run_twilio_path(main, web_ctx, outreach_id="OutreachTw0000000001"))
        finally:
            ScriptedRunner.script = None
        manager = run_from_recorder().processor("CallManager")
    assert not [f for f, _ in manager.pushed if isinstance(f, LLMTextFrame)]
    assert [f.text for f, _ in manager.pushed if isinstance(f, TTSSpeakFrame)] == [load_call_lines()["hi-IN"]["goodbye"]]
    assert any(type(f).__name__ == "EndTaskFrame" for f, _ in manager.pushed)


# ── politeness is not a promise ──────────────────────────────────────────────
# Independent review of 5191d1e72 (#1): "won't disturb / bother" counted as a
# promise with no "again" and no check on who is speaking, so an ordinary
# polite reply ended the call and recorded a bogus optOutRequested. Only
# CALLING counts, and only a promise made by us (we/I) or to the parent (you).

POLITE = [
    "I won't disturb you for long.",
    "I won't bother you much, just wanted to share good news.",
    "जी, मैं आपको ज़्यादा परेशान नहीं करूँगा।",
    "Thank you, I won't disturb you any further, have a good day.",
    "আপনাকে বেশিক্ষণ বিরক্ত করব না।",
    "जास्त वेळ त्रास देणार नाही.",
    "Rahul won't disturb anyone in class anymore, he has improved a lot.",
    "क्लास टीचर फ़ोन नहीं करेंगे, आप स्कूल आकर मिल सकते हैं।",
]


@pytest.mark.parametrize("text", POLITE)
def test_politeness_and_someone_elses_plans_are_not_promises(text):
    assert not is_no_more_calls_promise(text), text


POLITE_CALLS = [("Hindi", POLITE[2]), ("English", POLITE[0]), ("English", POLITE[3]), ("Bengali", POLITE[4]),
                ("Marathi", POLITE[5]), ("English", POLITE[6]), ("Hindi", POLITE[7])]


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("language,reply", POLITE_CALLS, ids=lambda v: v if len(v) < 30 else v[:30])
def test_a_polite_reply_to_a_busy_parent_is_spoken_and_records_nothing(language, reply, mode, tmp_path, monkeypatch):
    # The reviewer's probe: the parent says they are a bit busy; the model is polite.
    monkeypatch.setattr(ModelRunner, "max_wait", 0.05)  # the call rightly goes on; do not wait for it
    runner, run, model_text, tts, hangups, db = _vobiz_call(
        language, mode, reply, tmp_path, monkeypatch, parent_says="मैं थोड़ा बिज़ी हूँ")
    first = reply.split("।")[0].split(". ")[0]
    assert model_text.startswith(first[:20]), (model_text, reply)
    assert tts == [], "no opt-out line, no goodbye"
    assert opt_out_requests(db) == []
    assert not [f for f, _ in run.processor("CallManager").pushed if type(f).__name__ == "EndTaskFrame"]
