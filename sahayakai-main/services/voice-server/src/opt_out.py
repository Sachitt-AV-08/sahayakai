"""
Do-not-call: noticing that a parent asked not to be called again, and saving it.

The web app refuses to dial a number when `call_suppressions/{id}` exists,
where id = lower-case hex sha256 of the E.164 number
(sahayakai-main/src/lib/call-suppression.ts, `refuseIfParentOptedOut`). This
engine writes that same document, with the same fields the sahayakai-agents
bridge writes (`_persist_opt_out`), so either engine's "no" stops both.

Pure on purpose: stdlib only, Firestore is passed in. Three parts:

  StopDetector    — runs on every final transcription of the PARENT's speech,
                    deterministically, before the model sees it. A match makes
                    bot.py speak the fixed acknowledgement, end the call and
                    write the suppression immediately.
  backstop_quote  — after the call, the post-call analysis may report an
                    opt-out the phrase list missed. It only counts if the
                    analysis quotes words that really are in a parent turn.
  persist_opt_out — writes the suppression record (and flags the outreach).

WHY A DETERMINISTIC DETECTOR, NOT A MODEL TOOL
Missed stops are the failure that matters: a parent who said "do not call me"
and is called again. The model here is capped at 40 tokens and one sentence,
and its output is cut and filtered by CallManager, so a tool call or marker
from it would be the least reliable signal in the pipeline; it also depends on
Gemini answering at all. A phrase check on the parent's own words runs on every
utterance whether or not the model behaves, and the post-call backstop catches
paraphrases the list does not know.

False stops — hanging up on "don't call me NOW, call in the evening" — are kept
down two ways. Unambiguous phrases ("stop calling", "don't call again",
"दोबारा फ़ोन मत करना", "remove my number") always count. A bare "don't call
(me)" only counts when the same utterance carries no rescheduling or
third-person cue (now, later, evening, busy, tomorrow, "him/her"…), in any of
the call languages. The detector never fires on the engine's own lines (echo).
"""

from __future__ import annotations

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


async def persist_opt_out(db: Any, outreach_id: str, *, now: str | None = None) -> OptOutWrite:
    """Write call_suppressions/{sha256(phone)} for this outreach's parent.

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
        await ref.update({"optedOut": True, "optedOutAt": now, "updatedAt": now})
    except Exception as exc:  # noqa: BLE001
        return OptOutWrite(True, f"suppression saved; outreach flag not set: {type(exc).__name__}")
    return OptOutWrite(True, "suppression saved")


# ── Detecting a stop request in the parent's speech ──────────────────────────

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


_HI_CALL = r"(?:फ़ोन|फोन|कॉल|काल)"
_HI_DONT = r"(?:मत|ना|न)\s*(?:करना|करो|कीजिए|कीजिये|करें|करिए|करिएगा|किया\s*करो)"
_HI_AGAIN = r"(?:दोबारा|दुबारा|फिर\s*से|फिर\s*कभी|कभी\s*भी|कभी|आगे\s*से|अब\s*से)"
_BN_CALL = r"(?:ফোন|কল)"
_BN_DONT = r"(?:করবেন|কোরো|করো|করিস)\s*না"
_MR_CALL = r"(?:फोन|फ़ोन|कॉल)"
_MR_DONT = r"(?:करू|करु)\s*(?:नका|नकोस)"
_GU_CALL = r"(?:ફોન|કૉલ|કોલ)"
_GU_DONT = r"(?:ન|ના|નહીં)\s*(?:કરો|કરશો|કરતા|કરજો)"
_PA_CALL = "(?:ਫ਼ੋਨ|ਫ਼ੋਨ|ਫੋਨ|ਕਾਲ)"  # ਫ਼ੋਨ (both forms), ਫੋਨ, ਕਾਲ
_PA_DONT = r"(?:ਨਾ|ਨ)\s*(?:ਕਰੋ|ਕਰਿਓ|ਕਰਨਾ)"
_KN_CALL = r"(?:ಫೋನ್|ಕರೆ|ಕಾಲ್)"
_KN_DONT = r"(?:ಮಾಡಬೇಡಿ|ಮಾಡ್ಬೇಡಿ|ಮಾಡಬೇಡ)"
_TA_CALL = r"(?:போன்|ஃபோன்|கால்)"
_TA_DONT = r"(?:பண்ணாதீங்க|பண்ணாதீர்கள்|பண்ணாதே|பண்ண\s*வேண்டாம்|செய்ய\s*வேண்டாம்)"
_TA_CALLVERB = r"(?:கூப்பிடாதீங்க|கூப்பிடாதீர்கள்|கூப்பிட\s*வேண்டாம்|அழைக்க\s*வேண்டாம்|அழைக்காதீர்கள்)"
_TE_CALL = r"(?:ఫోన్|కాల్)"
_TE_DONT = r"(?:చేయవద్దు|చేయకండి|చెయ్యకండి|చేయొద్దు|చెయ్యొద్దు)"
_ML_DONT = r"(?:വിളിക്കരുത്|വിളിക്കേണ്ട|ഫോൺ\s*ചെയ്യരുത്|ഫോൺ\s*ചെയ്യേണ്ട)"

# Always a stop, whatever else the parent says in the same breath.
_STRONG = _compile(
    # English
    r"\bstop (?:calling|phoning|ringing|these calls|the calls|your calls)\b",
    r"\bno more (?:phone )?calls\b",
    r"\bnever (?:ever )?(?:call|phone|ring)\b",
    r"\b(?:do not|don't|dont) (?:ever )?(?:call|phone|ring)(?: (?:me|us|this number|my number|here))?"
    r" (?:again|anymore|any more|ever again|at all)\b",
    r"\b(?:remove|delete|take off|take out|block) (?:my|this|our) (?:number|phone number|mobile number|mobile)\b",
    r"\btake (?:me|us|my number|this number) off\b",
    r"\bunsubscribe\b",
    r"\b(?:i|we) (?:do not|don't|dont) want (?:any )?(?:more )?(?:these |your |such |any )?(?:phone )?calls\b",
    r"\b(?:i|we) (?:do not|don't|dont) want (?:you|the school|anyone) to (?:call|phone|ring)\b",
    r"\b(?:do not|don't|dont) want to be (?:called|contacted)\b",
    # Hinglish (romanised Hindi)
    r"\b(?:dobara|dubara|dobaara|phir se|fir se|kabhi bhi|kabhi|aage se|ab se)\b.{0,15}\b(?:phone|fone|call)\b.{0,10}\b(?:mat|na)\b",
    r"\b(?:phone|fone|call)\b.{0,6}\b(?:mat|na)\b.{0,12}\b(?:dobara|dubara|phir se|fir se|kabhi)\b",
    r"\bnumber\b.{0,6}\b(?:hata|hatao|hataiye|hata do|delete)\b",
    r"\b(?:phone|call)\b.{0,8}\bband\b.{0,4}\b(?:karo|kijiye|kariye|kar do|kar dijiye)\b",
    # Hindi
    rf"{_HI_AGAIN}\s*.{{0,15}}{_HI_CALL}\s*.{{0,8}}{_HI_DONT}",
    rf"{_HI_CALL}\s*{_HI_DONT}\s*.{{0,8}}(?:दोबारा|दुबारा|फिर\s*से|कभी)",
    r"(?:नंबर|नम्बर)\s*.{0,8}(?:हटा|डिलीट|काट\s*द)",
    rf"{_HI_CALL}\s*(?:करना\s*)?बंद\s*(?:करो|कीजिए|करें|कर\s*दो|कर\s*दीजिए|करिए)",
    r"(?:स्टॉप|स्टाप)\s*(?:कॉलिंग|कालिंग)",
    rf"(?:हमें|मुझे)\s*.{{0,10}}{_HI_CALL}\s*(?:नहीं\s*चाहिए|नहीं\s*चाहिये)",
    r"डोंट\s*(?:कॉल|काल)\s*.{0,10}(?:अगेन|एनीमोर)",
    # Bengali
    rf"(?:আর|আবার|কখনো|কখনও|কোনোদিন)\s*.{{0,10}}{_BN_CALL}\s*{_BN_DONT}",
    r"(?:নাম্বার|নম্বর)\s*.{0,6}(?:বাদ|মুছ|কেটে)",
    rf"{_BN_CALL}\s*করা\s*বন্ধ\s*(?:করুন|করো|করবেন)",
    # Marathi
    rf"(?:पुन्हा|परत|यापुढे|कधीही|कधी)\s*.{{0,10}}{_MR_CALL}\s*{_MR_DONT}",
    rf"{_MR_CALL}\s*(?:करणं|करणे)\s*बंद\s*(?:करा|कर)",
    # Gujarati
    rf"(?:ફરીથી|ફરી|ક્યારેય|હવે\s*પછી)\s*.{{0,10}}{_GU_CALL}\s*{_GU_DONT}",
    r"નંબર\s*.{0,6}(?:કાઢી\s*નાખો|ડિલીટ|હટાવો)",
    # Punjabi
    rf"(?:ਦੁਬਾਰਾ|ਮੁੜ\s*ਕੇ|ਫਿਰ|ਕਦੇ\s*ਵੀ|ਅੱਗੇ\s*ਤੋਂ)\s*.{{0,10}}{_PA_CALL}\s*{_PA_DONT}",
    r"ਨੰਬਰ\s*.{0,6}(?:ਹਟਾ|ਕੱਟ|ਡਿਲੀਟ)",
    # Kannada
    rf"(?:ಮತ್ತೆ|ಇನ್ನು\s*ಮುಂದೆ|ಇನ್ಮೇಲೆ|ಯಾವತ್ತೂ|ಇನ್ನೊಮ್ಮೆ)\s*.{{0,10}}{_KN_CALL}\s*{_KN_DONT}",
    r"ನಂಬರ್\s*.{0,6}(?:ತೆಗೆದು|ಡಿಲೀಟ್)",
    # Tamil
    rf"(?:இனிமேல்|இனிமே|இனி|மறுபடியும்|மறுபடி|மீண்டும்)\s*.{{0,10}}(?:{_TA_CALL}\s*{_TA_DONT}|{_TA_CALLVERB})",
    r"(?:நம்பர்|நம்பரை|எண்ணை)\s*.{0,6}(?:நீக்க|எடுத்து|டிலீட்)",
    # Telugu
    rf"(?:మళ్ళీ|మళ్లీ|ఇంకెప్పుడూ|ఇకపై|ఇక)\s*.{{0,10}}{_TE_CALL}\s*{_TE_DONT}",
    r"నంబర్\s*.{0,6}(?:తీసేయండి|తొలగించండి|డిలీట్)",
    # Malayalam
    rf"(?:ഇനി|വീണ്ടും|ഇനിമേൽ|ഒരിക്കലും)\s*.{{0,10}}{_ML_DONT}",
    r"നമ്പർ\s*.{0,6}(?:മാറ്റ|നീക്ക|ഡിലീറ്റ്)",
    # Odia (spoken calls run in en-IN, but a parent may still say it)
    r"(?:ଆଉ|ପୁଣି)\s*.{0,10}ଫୋନ୍\s*କରନ୍ତୁ\s*ନାହିଁ",
)

# A plain "don't call (me)". A stop unless the same utterance reschedules or
# points at someone else.
_WEAK = _compile(
    r"\b(?:do not|don't|dont) (?:ever )?(?:call|phone|ring)\b"
    r"(?! (?:him|her|them|it|that|his|their|my (?:son|daughter|child|kid|husband|wife)|the (?:child|kid|boy|girl)))",
    r"\b(?:phone|fone|call)\s*(?:mat|na)\s*(?:karo|karna|kijiye|kijie|kariye|karein|kiya karo)\b",
    r"\bdon'?t (?:disturb|bother) (?:me|us)\b",
    rf"{_HI_CALL}\s*{_HI_DONT}",
    r"डोंट\s*(?:कॉल|काल)",
    r"परेशान\s*(?:मत|ना)\s*(?:करो|कीजिए|करें|करिए)",
    rf"{_BN_CALL}\s*{_BN_DONT}",
    rf"বিরক্ত\s*{_BN_DONT}",
    rf"{_MR_CALL}\s*{_MR_DONT}",
    r"त्रास\s*(?:देऊ|देउ)\s*(?:नका|नकोस)",
    rf"{_GU_CALL}\s*{_GU_DONT}",
    rf"{_PA_CALL}\s*{_PA_DONT}",
    rf"{_KN_CALL}\s*{_KN_DONT}",
    rf"{_TA_CALL}\s*{_TA_DONT}",
    _TA_CALLVERB,
    rf"{_TE_CALL}\s*{_TE_DONT}",
    _ML_DONT,
    r"ଫୋନ୍\s*କରନ୍ତୁ\s*ନାହିଁ",
)

# "Not now / not like this", or "not HIM". Any of these in the same utterance
# turns a plain "don't call" into a request to reschedule, not a stop.
_RESCHEDULE = _compile(
    r"\b(?:now|right now|at the moment|today|tonight|tomorrow|later|morning|afternoon|evening|night|"
    r"after|before|until|till|during|weekend|weekdays?|office|work|working|busy|driving|meeting|"
    r"o'?clock|\d{1,2}\s*(?:am|pm)|minutes?|hours?|next week|this week)\b",
    r"\b(?:abhi|baad|kal|shaam|subah|raat|dopahar|aaj|ghante|minute|baje)\b",
    _word(r"अभी|बाद|कल|शाम|सुबह|रात|दोपहर|आज|बिज़ी|बिजी|व्यस्त|घंटे|मिनट|बजे|ऑफिस|मीटिंग|"
          r"उसको|उसे|उनको|उन्हें|इनको|इन्हें|इसको|इसे"),
    _word(r"आता|नंतर|उद्या|संध्याकाळी|सकाळी|रात्री|दुपारी|कामात"),
    _word(r"এখন|পরে|কাল|সন্ধ্যা|সন্ধ্যায়|সকাল|সকালে|রাতে|দুপুর|দুপুরে|আজ|ব্যস্ত|অফিস|মিটিং"),
    _word(r"અત્યારે|હમણાં|પછી|કાલે|સાંજે|સવારે|રાત્રે|બપોરે|આજે|વ્યસ્ત|ઓફિસ"),
    _word(r"ਹੁਣ|ਬਾਅਦ|ਕੱਲ੍ਹ|ਸ਼ਾਮ|ਸ਼ਾਮ|ਸਵੇਰੇ|ਰਾਤ|ਦੁਪਹਿਰ|ਅੱਜ|ਬਿਜ਼ੀ|ਰੁੱਝਿਆ"),
    _starts(r"ಈಗ|ಆಮೇಲೆ|ನಂತರ|ನಾಳೆ|ಸಂಜೆ|ಬೆಳಿಗ್ಗೆ|ರಾತ್ರಿ|ಮಧ್ಯಾಹ್ನ|ಇವತ್ತು|ಬ್ಯುಸಿ"),
    _starts(r"இப்போ|இப்போது|அப்புறம்|பிறகு|நாளை|சாயங்காலம்|மாலை|காலை|ராத்திரி|இரவு|மதியம்|இன்னைக்கு|இன்று|பிஸி"),
    _starts(r"ఇప్పుడు|తర్వాత|తరువాత|రేపు|సాయంత్రం|పొద్దున|ఉదయం|రాత్రి|మధ్యాహ్నం|ఈరోజు|బిజీ"),
    _starts(r"ഇപ്പോൾ|പിന്നെ|പിന്നീട്|നാളെ|വൈകുന്നേരം|രാവിലെ|രാത്രി|ഉച്ചയ്ക്ക്|ഇന്ന്|തിരക്ക്"),
    _word(r"ଏବେ|ପରେ|କାଲି"),
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
    """Does this (single) utterance ask not to be called again?"""
    t = normalize(text)
    if not t:
        return False
    if _STRONG.search(t):
        return True
    return bool(_WEAK.search(t)) and not _RESCHEDULE.search(t)


def has_reschedule_cue(text: object) -> bool:
    return bool(_RESCHEDULE.search(normalize(text)))


class StopDetector:
    """Watches the parent's final transcriptions; fires once.

    Speech-to-text splits an utterance wherever the parent pauses, so a phrase
    can straddle two transcriptions ("please don't" … "call me again"). Each
    new one is checked alone first, then joined to the one before it. The
    rescheduling veto applies only to the smallest window that contains the
    match, so an earlier "call me in the evening" cannot veto a later, plain
    "don't call me".
    """

    def __init__(self) -> None:
        self._previous = ""
        self.fired = False
        self.matched: str | None = None

    def observe(self, text: object) -> bool:
        if self.fired:
            return False
        utterance = normalize(text)
        if not utterance:
            return False
        previous, self._previous = self._previous, utterance
        for window in (utterance, f"{previous} {utterance}" if previous else None):
            if window and is_stop_request(window):
                self.fired = True
                self.matched = window
                return True
        return False


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
