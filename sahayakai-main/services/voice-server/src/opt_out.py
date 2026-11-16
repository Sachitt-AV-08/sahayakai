"""
Do-not-call: noticing that a parent would prefer not to be called, and telling
the teacher. The engine never blocks a number.

A parent's "please don't call me again" is RECORDED, never enforced, here:

  StopListener          runs on every final transcription of the PARENT's
                        speech, before the model sees it. A possible stop
                        (is_stop_request, tuned for recall) is noticed once per
                        call: bot.py records it on the outreach, says one fixed,
                        honest line ("I will let the class teacher know you
                        would prefer not to receive these calls") and closes
                        the call politely.
  backstop_quote        after the call, the post-call analysis may find a stop
                        the phrase check missed. It is recorded the same way,
                        only on the parent's own quoted words.
  record_opt_out_request
                        the only write: `optOutRequested: {quote, at, source}`
                        on parent_outreach/{id}, source "live" or "analysis".
  opt_out_note          the note that leads the teacher's call summary.

WHY THE ENGINE DOES NOT WRITE call_suppressions
A suppression silently stops every future call to that number and nothing on
the web side lifts it. Independent review (rereview-0b #1, #2, #3, #6) showed a
spoken yes/no confirmation cannot be trusted with that: "yes, call me", "yes,
tomorrow evening", a backchannel or a bystander's "haan" all confirmed, while
"yes, please don't call" did not. So the decision stays with the teacher, who
sees the parent's own words. `persist_opt_out` and the digest
(`phone_suppression_id`, `suppression_record`) are kept, unused, for a later
designed feature; tests/test_opt_out_never_suppresses.py fails if any engine
code path reaches them.

WHY A DETERMINISTIC DETECTOR, NOT A MODEL TOOL
Missed stops are the failure that matters: a parent who said "do not call me"
and is called again without the teacher knowing. The model here is capped at
40 tokens and one sentence, and its output is cut and filtered by CallManager,
so a tool call or marker from it would be the least reliable signal in the
pipeline. A phrase check on the parent's own words runs on every utterance
whether or not the model behaves, and the post-call backstop catches
paraphrases the list does not know. A false hit costs one honest sentence and a
note the teacher can read and dismiss; it never silences a parent.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

SUPPRESSION_COLLECTION = "call_suppressions"
OUTREACH_COLLECTION = "parent_outreach"
OPT_OUT_REASON = "opt_out"


# ── The shared key ───────────────────────────────────────────────────────────


def phone_suppression_id(e164: str) -> str:
    """sha256 hex of the trimmed E.164 number — identical to the web app's phoneSuppressionId."""
    return hashlib.sha256(e164.strip().encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Same timestamp shape as the agents bridge (ISO 8601, Z suffix)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def suppression_record(outreach: Mapping[str, Any], outreach_id: str, now: str) -> dict[str, Any]:
    """The call_suppressions document. Never contains the phone number itself."""
    return {
        "teacherUid": outreach.get("teacherUid"),
        "classId": outreach.get("classId"),
        "studentId": outreach.get("studentId"),
        "outreachId": outreach_id,
        "reason": OPT_OUT_REASON,
        "createdAt": now,
    }


@dataclass(frozen=True)
class OptOutWrite:
    ok: bool
    detail: str


async def persist_opt_out(
    db: Any, outreach_id: str, *, now: str | None = None, quote: str | None = None,
) -> OptOutWrite:
    """Write call_suppressions/{sha256(phone)} for this outreach's parent.

    UNUSED ON PURPOSE: no engine code path may call this (see the module
    docstring and tests/test_opt_out_never_suppresses.py). It is kept, with
    its tests and the shared digest fixture, for a later designed feature.

    `db` is a google.cloud.firestore.AsyncClient (or a fake with the same
    shape). The phone comes from the outreach record itself, the source of
    truth, not from whatever context the call was started with.

    The suppression is written FIRST: it is the record that stops the next
    dial. The `optedOut` flag on the outreach doc is best effort after it.
    Never raises; `ok` is True only when the suppression record was written.
    The detail never contains the phone number.
    """
    now = now or utc_now_iso()
    try:
        ref = db.collection(OUTREACH_COLLECTION).document(outreach_id)
        snap = await ref.get()
        data = (snap.to_dict() if snap.exists else None) or {}
        phone = data.get("parentPhone")
        if not isinstance(phone, str) or not phone.strip():
            return OptOutWrite(False, f"{OUTREACH_COLLECTION}/{outreach_id} has no parentPhone")
        await db.collection(SUPPRESSION_COLLECTION).document(phone_suppression_id(phone)).set(
            suppression_record(data, outreach_id, now)
        )
    except Exception as exc:  # noqa: BLE001 — a lost write is reported, never raised into the call
        return OptOutWrite(False, f"suppression write failed for {outreach_id}: {type(exc).__name__}: {exc}")

    try:
        flag = {"optedOut": True, "optedOutAt": now, "updatedAt": now, "optOutSource": "parent_speech"}
        if quote:
            flag["optOutQuote"] = quote[:500]  # the parent's own words, for the teacher
        await ref.update(flag)
    except Exception as exc:  # noqa: BLE001
        return OptOutWrite(True, f"suppression saved; outreach flag not set: {type(exc).__name__}")
    return OptOutWrite(True, "suppression saved")


# ── Noticing a possible stop request in the parent's speech ──────────────────
#
# The phrase check favours recall: a false hit costs one honest sentence and a
# note the teacher can dismiss; a miss costs a parent who said "do not call me"
# being called again without the teacher knowing.

# Every letter or combining mark of the Indic scripts this engine speaks.
_INDIC = "ऀ-෿"


def _word(word: str) -> str:
    r"""Standalone word in an Indic script (`\b` breaks on combining marks)."""
    return rf"(?<![{_INDIC}])(?:{word})(?![{_INDIC}])"


def _starts(word: str) -> str:
    """Word start in an agglutinative script (Dravidian suffixes attach to it)."""
    return rf"(?<![{_INDIC}])(?:{word})"


def _compile(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


_END = rf"(?![{_INDIC}])"  # the verb ends here ("करें" must not match "करेंगे")

_HI_CALL = r"(?:फ़ोन|फोन|कॉल|काल)"
_HI_VERB = rf"(?:करना|करो|कीजिए|कीजिये|करें|करिए|करिएगा|किया\s*करो){_END}"
_HI_DONT = rf"(?:मत|ना|न|नहीं)\s*{_HI_VERB}"
_HI_AGAIN = r"(?:दोबारा|दुबारा|फिर\s*से|फिर\s*कभी|कभी\s*भी|कभी|आगे\s*से|अब\s*से)"
_BN_CALL = r"(?:ফোন|কল)"
_BN_DONT = r"(?:করবেন|কোরো|করো|করিস|দিবেন|দেবেন|দিও|দিয়ো|দিস)\s*না"
_MR_CALL = r"(?:फोन|फ़ोन|कॉल)"
_MR_DONT = r"(?:(?:करू|करु)\s*(?:नका|नकोस)|(?:नका|नकोस)\s*(?:करू|करु))"
_GU_CALL = r"(?:ફોન|કૉલ|કોલ)"
_GU_VERB = r"(?:કરો|કરશો|કરતા|કરજો)"
_GU_DONT = rf"(?:(?:ન|ના|નહીં|નહિ)\s*{_GU_VERB}|{_GU_VERB}\s*(?:નહીં|નહિ|ના))"
_PA_CALL = "(?:ਫ਼ੋਨ|ਫ਼ੋਨ|ਫੋਨ|ਕਾਲ)"  # ਫ਼ੋਨ (both forms), ਫੋਨ, ਕਾਲ
_PA_VERB = r"(?:ਕਰੋ|ਕਰਿਓ|ਕਰਨਾ)"
_PA_DONT = rf"(?:(?:ਨਾ|ਨ|ਨਹੀਂ)\s*{_PA_VERB}|{_PA_VERB}\s*(?:ਨਹੀਂ|ਨਾ))"
_KN_CALL = r"(?:ಫೋನ್|ಕರೆ|ಕಾಲ್)"
_KN_DONT = r"(?:ಮಾಡಬೇಡಿ|ಮಾಡ್ಬೇಡಿ|ಮಾಡಬೇಡ)"
_TA_CALL = r"(?:போன்|ஃபோன்|கால்)"
_TA_DONT = r"(?:பண்ணாதீங்க|பண்ணாதீர்கள்|பண்ணாதே|பண்ண\s*வேண்டாம்|செய்ய\s*வேண்டாம்)"
_TA_CALLVERB = r"(?:கூப்பிடாதீங்க|கூப்பிடாதீர்கள்|கூப்பிட\s*வேண்டாம்|அழைக்க\s*வேண்டாம்|அழைக்காதீர்கள்)"
_TE_CALL = r"(?:ఫోన్|కాల్)"
_TE_DONT = r"(?:చేయవద్దు|చేయకండి|చెయ్యకండి|చేయొద్దు|చెయ్యొద్దు)"
_ML_DONT = (
    r"(?:വിളിക്കരുത്|വിളിക്കേണ്ട|വിളിക്കണ്ട|വിളിക്കല്ലേ|"
    r"ഫോൺ\s*ചെയ്യരുത്|ഫോൺ\s*ചെയ്യേണ്ട|ഫോൺ\s*ചെയ്യണ്ട|ഫോൺ\s*ചെയ്യല്ലേ)"
)

# Phrases that mean "stop calling this number" whatever else is said.
_UNAMBIGUOUS = _compile(
    r"\bunsubscribe\b",
    r"\b(?:remove|delete|take off|take out|block) (?:my|this|our) (?:number|phone number|mobile number|mobile)\b",
    r"\btake (?:me|us|my number|this number) off\b",
    r"\bnumber\b.{0,6}\b(?:hata|hatao|hataiye|hata do|delete)\b",
    r"(?:नंबर|नम्बर)\s*.{0,8}(?:हटा|डिलीट|काट\s*द)",
    r"(?:নাম্বার|নম্বর)\s*.{0,6}(?:বাদ|মুছ|কেটে)",
    r"નંબર\s*.{0,6}(?:કાઢી\s*નાખો|ડિલીટ|હટાવો)",
    r"ਨੰਬਰ\s*.{0,6}(?:ਹਟਾ|ਕੱਟ|ਡਿਲੀਟ)",
    r"ನಂಬರ್\s*.{0,6}(?:ತೆಗೆದು|ಡಿಲೀಟ್)",
    r"(?:நம்பர்|நம்பரை|எண்ணை)\s*.{0,6}(?:நீக்க|எடுத்து|டிலீட்)",
    r"నంబర్\s*.{0,6}(?:తీసేయండి|తొలగించండి|డిలీట్)",
    r"നമ്പർ\s*.{0,6}(?:മാറ്റ|നീക്ക|ഡിലീറ്റ്)",
)

# Ways to say "don't call (me) (again)". Only counted when the utterance has no
# rescheduling, third-party or redirection cue (_NOT_A_STOP).
_STOP_PHRASES = _compile(
    # English
    r"\bstop (?:calling|phoning|ringing|these calls|the calls|your calls)\b",
    r"\bno more (?:phone )?calls\b",
    r"^(?:please )?never (?:ever )?(?:call|phone|ring) (?:me|us|this number)\b",
    r"\b(?:do not|don't|dont) (?:ever )?(?:call|phone|ring)\b",
    r"\b(?:i|we) (?:do not|don't|dont) want (?:any )?(?:more )?(?:these |your |such |any )?(?:phone )?calls\b",
    r"\b(?:i|we) (?:do not|don't|dont) want (?:you|the school|anyone) to (?:call|phone|ring)\b",
    r"\b(?:i|we) (?:do not|don't|dont) want to (?:receive|get|have) (?:any )?(?:more )?(?:phone )?calls\b",
    r"\b(?:do not|don't|dont) want to be (?:called|contacted)\b",
    r"\b(?:do not|don't|dont) (?:disturb|bother) (?:me|us)\b",
    # Hinglish (romanised Hindi). "mat", or "na/nahi" directly before the
    # verb; a trailing Hinglish "na" tag ("call karna na") is an invitation.
    r"\b(?:phone|fone|call)\s*(?:mat\s*(?:karo|karna|kijiye|kijie|kariye|karein|kiya karo)|"
    r"(?:na|nahi|nahin)\s*(?:karo|karna|kijiye|kariye|karein))\b",
    r"\b(?:phone|fone|call)\s*karna\s*band\s*(?:karo|kijiye|kariye|kar do|kar dijiye)\b",
    # Hindi
    rf"{_HI_CALL}\s*{_HI_DONT}",
    rf"{_HI_CALL}\s*करना\s*बंद\s*(?:करो|कीजिए|करें|कर\s*दो|कर\s*दीजिए|करिए)",
    r"(?:स्टॉप|स्टाप)\s*(?:कॉलिंग|कालिंग)",
    rf"(?:हमें|मुझे)\s*.{{0,10}}{_HI_CALL}\s*(?:नहीं\s*चाहिए|नहीं\s*चाहिये)",
    r"डोंट\s*(?:कॉल|काल)",
    r"परेशान\s*(?:मत|ना)\s*(?:करो|कीजिए|करें|करिए)",
    # Bengali
    rf"{_BN_CALL}\s*{_BN_DONT}",
    rf"{_BN_CALL}\s*করা\s*বন্ধ\s*(?:করুন|করো|করবেন)",
    rf"বিরক্ত\s*{_BN_DONT}",
    # Marathi
    rf"{_MR_CALL}\s*{_MR_DONT}",
    rf"{_MR_CALL}\s*(?:करणं|करणे)\s*बंद\s*(?:करा|कर)",
    r"त्रास\s*(?:देऊ|देउ)\s*(?:नका|नकोस)",
    # Gujarati, Punjabi, Kannada, Tamil, Telugu, Malayalam, Odia
    rf"{_GU_CALL}\s*{_GU_DONT}",
    rf"{_PA_CALL}\s*{_PA_DONT}",
    rf"{_KN_CALL}\s*{_KN_DONT}",
    rf"{_TA_CALL}\s*{_TA_DONT}",
    _TA_CALLVERB,
    rf"{_TE_CALL}\s*{_TE_DONT}",
    _ML_DONT,
    r"ଫୋନ୍\s*କରନ୍ତୁ\s*ନାହିଁ",
)

# In the same utterance, these make "don't call" mean something else: a time
# limit ("not now", "before 10", "today"), someone else ("my husband",
# "उनको", "স্বামীকে"), or a redirection ("I'll come myself", "I'll call you").
_NOT_A_STOP = _compile(
    # Time and scope
    r"\b(?:now|right now|at the moment|today|tonight|tomorrow|later|morning|afternoon|evening|night|"
    r"after|before|until|till|during|weekend|weekdays?|office|work|working|busy|driving|meeting|"
    r"o'?clock|\d{1,2}\s*(?:am|pm)|minutes?|hours?|next week|this week|exams?)\b",
    r"\b(?:abhi|baad|kal|shaam|subah|raat|dopahar|aaj|ghante|minute|baje)\b",
    _word(r"अभी|बाद|कल|शाम|सुबह|रात|दोपहर|आज|बिज़ी|बिजी|व्यस्त|घंटे|मिनट|बजे|ऑफिस|मीटिंग|ड्राइव"),
    r"गाड़ी\s*चला",
    _word(r"आता|नंतर|उद्या|संध्याकाळी|सकाळी|रात्री|दुपारी|कामात"),
    _word(r"এখন|পরে|কাল|সন্ধ্যা|সন্ধ্যায়|সকাল|সকালে|রাতে|দুপুর|দুপুরে|আজ|ব্যস্ত|অফিস|মিটিং"),
    r"গাড়ি\s*চালা",
    _word(r"અત્યારે|હમણાં|પછી|કાલે|સાંજે|સવારે|રાત્રે|બપોરે|આજે|વ્યસ્ત|ઓફિસ"),
    _word(r"ਹੁਣ|ਬਾਅਦ|ਕੱਲ੍ਹ|ਸ਼ਾਮ|ਸ਼ਾਮ|ਸਵੇਰੇ|ਰਾਤ|ਦੁਪਹਿਰ|ਅੱਜ|ਬਿਜ਼ੀ|ਰੁੱਝਿਆ"),
    _starts(r"ಈಗ|ಆಮೇಲೆ|ನಂತರ|ನಾಳೆ|ಸಂಜೆ|ಬೆಳಿಗ್ಗೆ|ರಾತ್ರಿ|ಮಧ್ಯಾಹ್ನ|ಇವತ್ತು|ಬ್ಯುಸಿ"),
    _starts(r"இப்போ|இப்போது|அப்புறம்|பிறகு|நாளை|சாயங்காலம்|மாலை|காலை|ராத்திரி|இரவு|மதியம்|இன்னைக்கு|இன்று|பிஸி"),
    _starts(r"ఇప్పుడు|తర్వాత|తరువాత|రేపు|సాయంత్రం|పొద్దున|ఉదయం|రాత్రి|మధ్యాహ్నం|ఈరోజు|బిజీ"),
    _starts(r"ഇപ്പോൾ|പിന്നെ|പിന്നീട്|നാളെ|വൈകുന്നേരം|രാവിലെ|രാത്രി|ഉച്ചയ്ക്ക്|ഇന്ന്|തിരക്ക്"),
    _word(r"ଏବେ|ପରେ|କାଲି"),
    # Someone else
    r"\b(?:him|her|them|his|their)\b",
    r"\bmy (?:son|daughter|child|kid|husband|wife|father|mother|dad|mom|mum|brother|sister)\b",
    r"\bthe (?:child|kid|boy|girl)\b",
    r"\b(?:uske|unke|iske|inke|mere)?\s*(?:papa|mummy|pati|husband|wife|bete|beti|bachche|bachcha|bhai|behen)\s*ko\b",
    r"\b(?:usko|unko|isko|inko|use|unhe|unhein)\b",
    _word(r"उसको|उसे|उनको|उन्हें|इनको|इन्हें|इसको|इसे"),
    rf"(?:^|\s)(?!मुझ|हम)\S+\s+को\s*{_HI_CALL}",  # "<anyone but me/us> को फ़ोन …"
    _starts(r"(?:पति|पत्नी|पापा|पिताजी|पिता|मम्मी|माँ|मां|बेटे|बेटी|बच्चे|बच्चा|भाई|बहन|दादा|दादी|नाना|नानी)\s*को"),
    _word(r"ওকে|ওঁকে|তাকে|তাঁকে|ওনাকে|ওদের"),
    _starts(r"(?:স্বামী|বাবা|মা|ছেলে|মেয়ে|বাচ্চা|ভাই|বোন)কে"),  # word start: "আমাকে" (me) is not "মাকে"
    # Redirection: "I'll come myself", "I'll call you"
    r"\bi(?:'ll| will) (?:call|phone|ring|come|visit|message|text)\b",
    r"\b(?:main|mai)\s*(?:khud|khudh)\b",
    r"मैं\s*(?:ख़ुद|खुद)",
    r"\bkhud\b",
    _word(r"ख़ुद|खुद"),
    r"আমি\s*নিজে",
)

# Punctuation that only separates words (kept: apostrophes, Indic marks).
_PUNCT = re.compile(r"[.,!?;:।॥\"“”()\[\]{}…\-–—/]+")


def normalize(text: object) -> str:
    if not isinstance(text, str):
        return ""
    t = text.replace("’", "'").replace("‘", "'").lower()
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_stop_request(text: object) -> bool:
    """Should the engine ASK whether this parent wants the calls to stop?

    True for an unambiguous phrase ("remove my number"), or for a "don't call"
    phrase in an utterance with no time, third-party or redirection cue. A
    True only ever leads to a flag for the teacher (StopListener).
    """
    t = normalize(text)
    if not t:
        return False
    if _UNAMBIGUOUS.search(t):
        return True
    return bool(_STOP_PHRASES.search(t)) and not _NOT_A_STOP.search(t)


def has_reschedule_cue(text: object) -> bool:
    """Any time, third-party or redirection cue (used by the post-call flag too)."""
    return bool(_NOT_A_STOP.search(normalize(text)))


# ── Noticing it during the call ──────────────────────────────────────────────


class StopListener:
    """Notice, once per call, that the parent may be asking not to be called.

    Speech-to-text splits an utterance wherever the parent pauses, so each
    transcription is checked alone, then joined to the one before. After the
    first notice everything else is ordinary speech for the model: the engine
    says its line once and never argues.
    """

    def __init__(self) -> None:
        self._previous = ""
        self._previous_raw = ""
        self.noticed = False
        self.quote: str | None = None

    def on_parent_speech(self, text: object) -> bool:
        """True exactly once: when this utterance (alone or joined) is a possible stop."""
        raw = text.strip() if isinstance(text, str) else ""
        utterance = normalize(text)
        if self.noticed or not utterance:
            return False
        previous, previous_raw = self._previous, self._previous_raw
        self._previous, self._previous_raw = utterance, raw
        if is_stop_request(utterance):
            self.noticed, self.quote = True, raw
        elif previous and is_stop_request(f"{previous} {utterance}"):
            self.noticed, self.quote = True, f"{previous_raw} {raw}".strip()
        return self.noticed


# ── After the call: the analysis backstop ────────────────────────────────────


def backstop_quote(insights: Mapping[str, Any] | None, transcript: Sequence[Mapping[str, Any]]) -> str | None:
    """The parent's own words asking not to be called, if the analysis found them.

    Counts only when the post-call analysis says `optOut.requested` is exactly
    True AND its quote appears, verbatim (after normalising), in one of the
    PARENT's turns, AND the quote is not a rescheduling request. A model that
    invents or paraphrases a quote, or reads "call me later" as a stop, is
    ignored.
    """
    if not isinstance(insights, Mapping):
        return None
    opt_out = insights.get("optOut")
    if not isinstance(opt_out, Mapping) or opt_out.get("requested") is not True:
        return None
    quote = opt_out.get("quote")
    needle = normalize(quote)
    if len(needle) < 4 or has_reschedule_cue(needle):
        return None
    for turn in transcript:
        if isinstance(turn, Mapping) and turn.get("role") == "parent" and needle in normalize(turn.get("text")):
            return quote if isinstance(quote, str) else None
    return None


LIVE = "live"          # heard by StopListener during the call
ANALYSIS = "analysis"  # found by the post-call analysis (backstop_quote)
OPT_OUT_REQUEST_FIELD = "optOutRequested"
MAX_QUOTE_CHARS = 500


def opt_out_request(quote: str, at: str, source: str) -> dict[str, Any]:
    """The `optOutRequested` map on parent_outreach/{id}."""
    if source not in (LIVE, ANALYSIS):
        raise ValueError(f"unknown opt-out source {source!r}")
    return {"quote": quote[:MAX_QUOTE_CHARS], "at": at, "source": source}


async def record_opt_out_request(
    db: Any, outreach_id: str, quote: str, source: str, *, now: str | None = None,
) -> OptOutWrite:
    """Record (never enforce) that the parent may not want these calls. Never raises.

    The one write the engine makes for a stop request: it updates only the
    outreach the call was authenticated for, and never touches
    call_suppressions.
    """
    now = now or utc_now_iso()
    try:
        fields = {OPT_OUT_REQUEST_FIELD: opt_out_request(quote, now, source), "updatedAt": now}
        await db.collection(OUTREACH_COLLECTION).document(outreach_id).update(fields)
    except Exception as exc:  # noqa: BLE001
        return OptOutWrite(False, f"opt-out request not recorded for {outreach_id}: {type(exc).__name__}")
    return OptOutWrite(True, f"opt-out request recorded ({source})")


def opt_out_note(request: Mapping[str, Any], in_english: str | None = None) -> str:
    """The first line of the teacher's call summary, quoting the parent.

    English like the rest of the summary, with the parent's own words and,
    when the post-call analysis gave one, their English translation.
    """
    quote = str(request.get("quote") or "").strip()
    if request.get("source") == ANALYSIS:
        lead = "The call review found the parent may prefer not to receive these calls"
    else:
        lead = ("The parent said they would prefer not to receive these calls, and was told "
                "the class teacher would be informed")
    note = lead + (f'. Their words: "{quote}"' if quote else "")
    english = (in_english or "").strip()
    if quote and english and normalize(english) != normalize(quote):
        note += f' (in English: "{english}")'
    return note + ". Nothing has been blocked: they can still be called. Please decide how to follow up."
