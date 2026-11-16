"""
Do-not-call: noticing that a parent asked not to be called again, and saving it.

The web app refuses to dial a number when `call_suppressions/{id}` exists,
where id = lower-case hex sha256 of the E.164 number
(sahayakai-main/src/lib/call-suppression.ts, `refuseIfParentOptedOut`). This
engine writes that same document, with the same fields the sahayakai-agents
bridge writes (`_persist_opt_out`), so either engine's "no" stops both.

Pure on purpose: stdlib only, Firestore is passed in. A suppression is
permanent (nothing on the web side lifts it), so nothing is written on a phrase
match alone:

  StopConversation — runs on every final transcription of the PARENT's speech,
                     before the model sees it. A possible stop (is_stop_request,
                     tuned for recall) makes bot.py ask ONE fixed, localized
                     question: should the school stop calling this number?
                     Only an explicit yes (classify_confirmation) writes the
                     suppression, speaks the acknowledgement and ends the call.
                     A no, an unclear answer or silence carries on and writes
                     nothing.
  backstop_quote   — after the call, the post-call analysis may report a stop
                     nobody confirmed. It can only FLAG the outreach for the
                     teacher (suspected_opt_out_fields); it never writes a
                     suppression.
  persist_opt_out  — writes the suppression record (and flags the outreach).

WHY A DETERMINISTIC DETECTOR, NOT A MODEL TOOL
Missed stops are the failure that matters: a parent who said "do not call me"
and is called again. The model here is capped at 40 tokens and one sentence,
and its output is cut and filtered by CallManager, so a tool call or marker
from it would be the least reliable signal in the pipeline; it also depends on
Gemini answering at all. A phrase check on the parent's own words runs on every
utterance whether or not the model behaves, and the post-call backstop catches
paraphrases the list does not know.

False stops are what the confirmation question is for: "don't call me NOW",
"don't call my husband, call me", "call karna na" (an invitation) or "phone
band karo" (hang up) must never become a permanent do-not-call. The detector
also skips the question when the utterance carries a time, third-party or
redirection cue, so a parent is not asked needlessly. The engine's own lines,
the question included, never trigger it (echo).
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
# The phrase check only decides whether to ASK. It favours recall: a false hit
# costs one polite question, a miss costs a parent who said "do not call me"
# being called again. Nothing is written until the parent answers the
# confirmation question with an explicit yes (classify_confirmation).

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
    True never writes anything by itself (see StopConversation).
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


# ── The confirmation answer ──────────────────────────────────────────────────


class Confirmation(enum.Enum):
    YES = "yes"
    NO = "no"
    UNCLEAR = "unclear"


# Words that, ON THEIR OWN in a short answer, mean yes. Deliberately narrow:
# "okay", "sari", "theek hai" are left out because they also mean "go on".
_YES_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "yea", "sure", "correct", "definitely", "exactly",
    "haan", "han", "haanji", "hanji", "bilkul", "zaroor", "jaroor",
    "हाँ", "हां", "हाँजी", "हांजी", "बिल्कुल", "बिलकुल", "ज़रूर", "जरूर",
    "होय",                             # Marathi ("हो" only on a Marathi call, below)
    "হ্যাঁ", "হ্যা", "হাঁ", "অবশ্যই",      # Bengali
    "હા", "હાં", "ચોક્કસ",               # Gujarati
    "ਹਾਂ", "ਬਿਲਕੁਲ",                   # Punjabi
    "ಹೌದು", "ಹೂಂ",                     # Kannada
    "ஆமா", "ஆமாம்", "ஆம்",              # Tamil
    "అవును", "అవునండి",                # Telugu
    "അതെ", "ഉവ്വ്",                     # Malayalam
})
_NO_WORDS = frozenset({
    "no", "nope", "nah", "not", "nahi", "nahin", "nai", "na",
    "नहीं", "नही", "नहि", "ना", "न", "नाही", "नको",
    "না", "নাহ", "ના", "નહીં", "નહિ", "ਨਹੀਂ", "ਨਾ",
    "ಇಲ್ಲ", "ಬೇಡ", "இல்லை", "இல்ல", "வேண்டாம்", "లేదు", "వద్దు", "ഇല്ല", "വേണ്ട",
})
_YES_PHRASES = re.compile(r"\b(?:please do|go ahead|that's right|that is right|ji haan|haan ji)\b")
MAX_YES_WORDS = 6
# Yes words that are ordinary words in another call language ("हो" is
# Marathi's yes and Hindi's "be"), honoured only on a call in that language.
_LANGUAGE_YES_WORDS = {"mr-IN": frozenset({"हो"})}


def classify_confirmation(text: object, lang_code: str | None = None) -> Confirmation:
    """Classify the answer to "should the school stop calling this number?".

    YES only for a short answer that contains a yes word and no no word.
    Anything mixed, long or unknown is UNCLEAR, and UNCLEAR never writes.
    """
    t = normalize(text)
    tokens = t.split()
    if not tokens:
        return Confirmation.UNCLEAR
    yes_words = _YES_WORDS | _LANGUAGE_YES_WORDS.get(lang_code or "", frozenset())
    yes = any(tok in yes_words for tok in tokens) or bool(_YES_PHRASES.search(t))
    no = any(tok in _NO_WORDS for tok in tokens)
    if yes and not no and len(tokens) <= MAX_YES_WORDS:
        return Confirmation.YES
    if no and not yes:
        return Confirmation.NO
    return Confirmation.UNCLEAR


def _looks_like_echo(text: str, line: str) -> bool:
    """Speech-to-text hearing the engine's own question back."""
    heard, said = set(normalize(text).split()), set(normalize(line).split())
    return bool(heard) and len(heard & said) >= max(2, int(0.6 * len(heard)))


# ── The conversation around a possible stop ──────────────────────────────────


class StopAction(enum.Enum):
    PASS = "pass"        # ordinary speech: give it to the model
    ASK = "ask"          # possible stop: ask the confirmation question
    CONFIRM = "confirm"  # explicit yes: write the suppression, acknowledge, end
    DECLINE = "decline"  # no or unclear: carry on, hand the answer to the model
    IGNORE = "ignore"    # our own question heard back; keep waiting


class StopConversation:
    """Detect, confirm, then (and only then) write.

    Speech-to-text splits an utterance wherever the parent pauses, so the
    detector checks each transcription alone, then joined to the one before.
    A possible stop asks the question at most `max_questions` times per call;
    after that, "don't call" is just speech for the model.
    """

    def __init__(self, question: str = "", lang_code: str | None = None, max_questions: int = 2) -> None:
        self._previous = ""
        self._question = question
        self._lang_code = lang_code
        self.max_questions = max_questions
        self.questions = 0
        self.awaiting = False
        self.confirmed = False
        self.asked_about: str | None = None
        self.answer: str | None = None

    def on_parent_speech(self, text: object) -> StopAction:
        utterance = normalize(text)
        if self.confirmed:
            return StopAction.IGNORE
        if not utterance:
            return StopAction.PASS
        if self.awaiting:
            if self._question and _looks_like_echo(utterance, self._question):
                return StopAction.IGNORE
            self.awaiting = False
            self._previous = ""
            if classify_confirmation(utterance, self._lang_code) is Confirmation.YES:
                self.confirmed = True
                self.answer = text if isinstance(text, str) else utterance
                return StopAction.CONFIRM
            return StopAction.DECLINE
        previous, self._previous = self._previous, utterance
        if self.questions >= self.max_questions:
            return StopAction.PASS
        for window in (utterance, f"{previous} {utterance}" if previous else None):
            if window and is_stop_request(window):
                self.awaiting = True
                self.questions += 1
                self.asked_about = window
                self._previous = ""
                return StopAction.ASK
        return StopAction.PASS

    def on_no_answer(self) -> None:
        """The parent said nothing after the question: carry on, write nothing."""
        self.awaiting = False


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


def suspected_opt_out_fields(quote: str, now: str) -> dict[str, Any]:
    """Outreach fields that let the teacher see a stop the parent never confirmed."""
    return {
        "optOutSuspected": True,
        "optOutSuspectedQuote": quote[:500],
        "optOutSuspectedSource": "post_call_analysis",
        "optOutSuspectedAt": now,
        "updatedAt": now,
    }


async def flag_suspected_opt_out(db: Any, outreach_id: str, quote: str, *, now: str | None = None) -> OptOutWrite:
    """Flag (never suppress) the outreach. Never raises."""
    now = now or utc_now_iso()
    try:
        await db.collection(OUTREACH_COLLECTION).document(outreach_id).update(suspected_opt_out_fields(quote, now))
    except Exception as exc:  # noqa: BLE001
        return OptOutWrite(False, f"suspected opt-out flag failed for {outreach_id}: {type(exc).__name__}")
    return OptOutWrite(True, "suspected opt-out flagged")
