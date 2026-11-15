"""
Pipecat Bot Pipeline for Parent Call — V2 Streaming.

Full streaming pipeline with native Pipecat services:
  Twilio audio → RNNoise → Sarvam STT (WebSocket streaming)
               → Gemini LLM (streaming tokens)
               → Sarvam TTS (WebSocket streaming)
               → Twilio audio

Key features:
  - RNNoise audio filter: removes phone echo/background noise
  - allow_interruptions=True: bot stops when parent speaks
  - First-sentence truncation: CallManager caps LLM output to 1 sentence
  - VAD tuned for phone: min_volume=0.6, start_secs=0.3 (filters echo)
"""

import asyncio
from typing import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

# Universal LLM context (replaces deprecated GoogleLLMContext)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

# Native Pipecat services — streaming WebSocket/API
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.google.llm import GoogleLLMService


from src.config import Config
from src.call_manager import CallManager
from src.call_policy import LANGUAGE_CODES, is_positive_feedback, lang_code_for, plan_call
from src.conversation_state import PhaseInstructor
from src.stt_filter import STTConfidenceFilter
from src.stop_guard import StopRequestGuard
from src.opt_out import backstop_quote
from src.persistence import sync_transcript
from src.call_store import CallStore
from src.call_analyzer import analyze_call

# Parent language → speech code. Lives in call_policy (pure) so the
# language-coverage test enumerates the same map; kept under its old name.
TWILIO_LANG_MAP = LANGUAGE_CODES

# Sarvam TTS voice mapping — male and female options
# See: https://docs.sarvam.ai/api-reference-docs/text-to-speech
SARVAM_VOICE_FEMALE = "priya"   # Female voice — for female teacher names
SARVAM_VOICE_MALE = "rahul"     # Male voice — for male teacher names
                                # bulbul:v3 male voices (verified 2026-04):
                                # aditya, ashutosh, rahul, rohan, amit, dev, ratan, varun,
                                # manan, sumit, kabir, aayan, shubh, advait, anand, tarun,
                                # sunny, mani, gokul, vijay, mohit
                                # (arvind, abhilash NOT supported in bulbul:v3)

# Female name indicators (Indian names) — used to pick voice gender
_FEMALE_INDICATORS = {
    "ms.", "mrs.", "miss", "ma'am", "madam", "maam",
    "devi", "kumari", "bai", "ben", "amma",
    # Common female name endings
}


def _pick_voice(teacher_name: str) -> str:
    """Pick male or female TTS voice based on teacher name."""
    name_lower = teacher_name.lower().strip()

    # Check explicit female indicators
    for indicator in _FEMALE_INDICATORS:
        if indicator in name_lower:
            return SARVAM_VOICE_FEMALE

    # Default to male (majority of teacher names in test scenarios)
    # In production, this would come from teacher profile data
    return SARVAM_VOICE_MALE


async def create_bot(
    config: Config,
    call_context: dict,
    websocket,
    stream_sid: str,
    call_sid: str = "",
    call_store: CallStore | None = None,
    serializer=None,
    on_call_end=None,
    on_opt_out: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[PipelineTask, Callable[[], Awaitable[None]]]:
    """Create the streaming Pipecat pipeline for a parent call.

    Returns (task, finalize). `finalize` runs the end-of-call bookkeeping
    exactly once, whichever of the client-disconnect event or the caller gets
    there first; main.py awaits it after the pipeline ends, because when the
    bot ends the call itself the transport may not report a disconnect.

    `on_opt_out` writes the do-not-call record for this call's parent and
    returns True when it was saved. It runs the moment the parent asks not to
    be called again, and is retried once at the end of the call if it failed.

    Architecture:
      transport.input()
        → stt (Sarvam WebSocket streaming)
        → context_aggregator.user() (manages LLM conversation context)
        → llm (Gemini streaming tokens)
        → call_manager (transcript tracking, turn management, call ending)
        → tts (Sarvam WebSocket streaming, sentence-level aggregation)
        → transport.output()
        → context_aggregator.assistant() (captures agent responses)
    """
    parent_language = call_context.get("parentLanguage", "Hindi")
    lang_code = lang_code_for(parent_language)
    outreach_id = call_context.get("id", "")
    positive = is_positive_feedback(call_context)

    logger.info(
        f"Creating streaming bot for outreach {outreach_id}, "
        f"language: {parent_language} ({lang_code}), "
        f"mode: {'positive_feedback' if positive else 'request_to_talk'}"
    )

    # ── What this call may say ──
    # Decided by call_policy.plan_call (below, once the parent history is
    # known). Only positive_feedback speaks the teacher's generatedMessage;
    # every other reason, including a missing or unknown one, opens with the
    # fixed request-to-talk line and the model never sees the concern.

    # ── Serializer ──
    # Provider-specific mu-law codec + call-control. The caller may pass one in
    # (the Vobiz/Plivo path does); otherwise default to Twilio for back-compat.
    # auto_hang_up=True requires call_sid + Twilio creds so the serializer can
    # POST to the Twilio REST API to end the call when EndFrame fires.
    if serializer is None:
        serializer = TwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=config.twilio_account_sid,
            auth_token=config.twilio_auth_token,
            params=TwilioFrameSerializer.InputParams(
                twilio_sample_rate=8000,
                sample_rate=16000,  # Pipeline internal rate
                auto_hang_up=bool(call_sid and config.twilio_account_sid and config.twilio_auth_token),
            ),
        )

    # RNNoise needs an optional native dep (pyrnnoise). If it is not installed,
    # run without denoise rather than crashing the call mid-stream.
    try:
        audio_in_filter = RNNoiseFilter()
    except Exception as _rnnoise_err:  # noqa: BLE001
        logger.warning(f"RNNoise unavailable — running without denoise: {_rnnoise_err}")
        audio_in_filter = None

    # ── Transport: Media Streams via WebSocket ──
    # SmartTurn REMOVED — adds 1-3s of deliberation after silence.
    # VAD alone with stop_secs=0.6 is fast enough for phone calls.
    # (0.6s silence = user paused → send to LLM immediately)
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            serializer=serializer,
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_in_filter=audio_in_filter,  # Denoise phone audio (None if unavailable)
            audio_out_enabled=True,
            audio_out_sample_rate=16000,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    min_volume=0.6,  # Filter phone echo (echo is lower amplitude)
                    start_secs=0.3,  # 300ms sustained speech to trigger (filters echo bursts)
                    stop_secs=0.5,  # 0.5s silence → end of turn
                ),
            ),
        ),
    )

    # ── STT: Sarvam Saaras v3 (WebSocket streaming) ──
    stt = SarvamSTTService(
        api_key=config.sarvam_api_key,
        settings=SarvamSTTService.Settings(
            model="saaras:v3",
            language=lang_code,
        ),
    )

    # ── Save call to local store ──
    if call_store:
        call_store.create_call(outreach_id, call_context)

    # ── Build system instruction with parent history ──
    # Parent history is built from earlier calls' reasons and concerns, so a
    # request-to-talk call does not even read it; positive_feedback uses it as
    # before.
    parent_history = None
    if call_store and positive:
        parent_phone = call_context.get("parentPhone", "")
        parent_history = call_store.build_parent_context(parent_phone)
        if parent_history:
            logger.info(f"Parent history injected for {parent_phone}")

    plan = plan_call(call_context, parent_history=parent_history)
    greeting_text = plan.greeting
    system_instruction = plan.system_instruction

    llm = GoogleLLMService(
        api_key=config.google_api_key,
        settings=GoogleLLMService.Settings(
            model="gemini-2.0-flash",
            temperature=0.2,  # Very low = focused, deterministic, stays in character
            max_tokens=40,  # CallManager truncates to 1st sentence anyway
            system_instruction=system_instruction,
        ),
    )

    # ── LLM Context + Universal Aggregator ──
    # Universal LLMContext uses OpenAI-style dicts — GeminiLLMAdapter
    # converts to Google Content objects at inference time.
    context = LLMContext()
    # Seed greeting into context so LLM knows what was said to the parent
    context.add_message({"role": "assistant", "content": greeting_text})

    # MinWordsUserTurnStartStrategy: require 3+ words to interrupt bot.
    # Filters backchannel ("haan", "ji") from triggering interruption.
    # When bot is NOT speaking, any 1 word triggers (normal turn-taking).
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=3)],
        ),
    )
    context_aggregator = LLMContextAggregatorPair(
        context, user_params=user_params,
    )

    # ── Transcript sync callback (non-blocking) ──
    async def _sync_turn(transcript: list[dict], turn_number: int):
        # Save to local store (always works)
        if call_store:
            call_store.update_transcript(outreach_id, transcript, turn_number)
        # Also try backend sync (may fail if backend is down)
        await sync_transcript(
            api_url=config.sahayakai_api_url,
            internal_key=config.sahayakai_internal_key,
            outreach_id=outreach_id,
            transcript=transcript,
            turn_count=turn_number,
        )

    # ── Phase Instructor: injects turn-specific instructions before LLM ──
    phase_instructor = PhaseInstructor(guidance=plan.phase_guidance)

    # ── Call Manager: turn counting, transcript, call lifecycle ──
    call_manager = CallManager(
        context=context,
        call_context=plan.manager_context,
        max_turns=6,
        on_turn_complete=_sync_turn,
        phase_instructor=phase_instructor,
        ai_reveal_pattern=plan.ai_reveal_pattern,
        ai_reveal_line=plan.ai_reveal_line,
    )

    # ── TTS: Sarvam Bulbul v3 (WebSocket streaming) ──
    # Pick voice gender based on teacher name
    teacher_name = call_context.get("teacherName", "")
    tts_voice = _pick_voice(teacher_name)
    logger.info(f"TTS voice: {tts_voice} (teacher: {teacher_name})")

    tts = SarvamTTSService(
        api_key=config.sarvam_api_key,
        sample_rate=16000,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice=tts_voice,
            language=lang_code,
            pace=0.92,
        ),
    )

    # ── Pipeline ──
    # NOTE: No assistant aggregator in pipeline — it consumes LLM frames
    # (LLMTextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame)
    # and does NOT pass them downstream, starving TTS of input.
    # Instead, CallManager adds model responses to context directly.
    # ── STT Quality Filter: drop garbled/empty transcriptions ──
    stt_filter = STTConfidenceFilter()

    # ── Do-not-call ──
    # The parent's own words are checked before the model sees them. A stop
    # request is written to call_suppressions at once (not at hangup, so a
    # dropped line cannot lose it), acknowledged with the fixed line in the
    # call language, and the call ends. See src/opt_out.py for why this is a
    # deterministic check and not a model tool.
    opt_out = {"requested": False, "source": None, "write": None}

    async def _write_opt_out() -> bool:
        if on_opt_out is None:
            logger.error(f"Opt-out for {outreach_id} NOT saved: no writer configured")
            return False
        try:
            saved = bool(await on_opt_out())
        except Exception as e:  # noqa: BLE001
            logger.error(f"Opt-out write raised for {outreach_id}: {e}")
            saved = False
        if not saved:
            logger.error(f"Opt-out for {outreach_id} NOT saved — this parent may be called again")
        return saved

    async def _on_stop_request():
        if opt_out["requested"]:
            return
        opt_out["requested"] = True
        opt_out["source"] = "parent_speech"
        logger.info(f"Parent asked not to be called again (outreach {outreach_id}); acknowledging and ending")
        opt_out["write"] = asyncio.ensure_future(_write_opt_out())
        await task.queue_frames([TTSSpeakFrame(text=plan.stop_acknowledgement), EndFrame()])

    stop_guard = StopRequestGuard(on_stop=_on_stop_request)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            stop_guard,                # "Don't call me again" never reaches the model
            stt_filter,               # Filter noise/gibberish before aggregator
            context_aggregator.user(),
            phase_instructor,          # Inject phase-specific instructions before LLM
            llm,
            call_manager,
            tts,
            transport.output(),
        ]
    )

    params = PipelineParams(
        allow_interruptions=True,  # Let parent interrupt bot — essential for natural conversation
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
    )
    task = PipelineTask(pipeline, params=params)

    # ── Event handlers ──

    @transport.event_handler("on_client_connected")
    async def on_connected(transport_instance, websocket):
        """Play greeting when call connects.

        TextFrame bypasses STT/LLM and goes directly to TTS for synthesis.
        The greeting text is already included in the LLM system instruction
        so the LLM knows what was said when the parent responds.

        Delay: Twilio trial accounts play a ~15s disclaimer before audio
        connects. We wait 2s for the pipeline to be fully ready, then
        send the greeting.
        """
        logger.info(f"Waiting for pipeline warmup before greeting...")
        await asyncio.sleep(1)  # Minimal warmup — speed matters
        logger.info(f"Playing greeting ({len(greeting_text)} chars): {greeting_text[:60]}...")
        # TTSSpeakFrame is standalone — flushes immediately to TTS
        # (TextFrame waits for LLMFullResponseEndFrame to flush, causing silence)
        await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    async def _finalize_once():
        """Persist final transcript and run post-call analysis."""
        transcript = call_manager.transcript
        turns = call_manager.turn_number
        logger.info(f"Call ended. Turns: {turns}, Transcript: {len(transcript)} entries")

        # Save to local store
        if call_store:
            call_store.complete_call(
                outreach_id, transcript, turns,
                call_status="completed",
            )

        # Try backend sync
        await sync_transcript(
            api_url=config.sahayakai_api_url,
            internal_key=config.sahayakai_internal_key,
            outreach_id=outreach_id,
            transcript=transcript,
            turn_count=turns,
            call_status="completed",
        )

        # Run post-call analysis (async, non-blocking)
        insights = None
        if transcript:
            try:
                insights = await analyze_call(
                    call_context, transcript, api_key=config.google_api_key,
                )
                if insights and call_store:
                    call_store.save_insights(outreach_id, insights)
                    parent_phone = call_context.get("parentPhone", "")
                    # Save structured concerns
                    if parent_phone and insights.get("concerns"):
                        call_store.record_concerns(
                            outreach_id, parent_phone, insights["concerns"],
                        )
                    # Save follow-up tasks
                    if insights.get("followUps"):
                        call_store.create_follow_ups(
                            outreach_id, parent_phone, insights["followUps"],
                        )
                    logger.info(
                        f"Post-call analysis saved for {outreach_id}: "
                        f"sentiment={insights.get('parentSentiment')}, "
                        f"concerns={len(insights.get('concerns', []))}, "
                        f"followUps={len(insights.get('followUps', []))}"
                    )
            except Exception as e:
                logger.error(f"Post-call analysis failed: {e}")

        # Do-not-call: make sure a spoken stop was saved (one retry), and let
        # the post-call analysis catch a stop the phrase check missed — but
        # only on the parent's own quoted words.
        if opt_out["requested"]:
            saved = await opt_out["write"] if opt_out["write"] is not None else False
            if not saved:
                logger.warning(f"Retrying opt-out write for {outreach_id}")
                await _write_opt_out()
        else:
            quote = backstop_quote(insights, transcript)
            if quote is not None:
                opt_out["requested"] = True
                opt_out["source"] = "post_call_analysis"
                logger.warning(f"Post-call analysis found an opt-out the phrase check missed ({outreach_id})")
                await _write_opt_out()

        # Provider-specific finalisation. The Vobiz path uses this to write the
        # transcript + summary to Firestore (for the modal) and to end the leg
        # over Vobiz REST — the Twilio serializer hangs up on its own instead.
        if on_call_end:
            try:
                await on_call_end(transcript, turns, insights)
            except Exception as e:
                logger.error(f"on_call_end hook failed: {e}")

    finalizing: dict = {"task": None}

    async def finalize():
        # Shared future: a second caller waits for the first run instead of
        # repeating it, and cannot cancel it by going away.
        if finalizing["task"] is None:
            finalizing["task"] = asyncio.ensure_future(_finalize_once())
        await asyncio.shield(finalizing["task"])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport_instance, websocket):
        await finalize()

    return task, finalize
