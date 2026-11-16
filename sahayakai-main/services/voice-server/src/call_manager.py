"""
Call lifecycle manager for the Parent Call pipeline.

Sits between LLM and TTS in the pipeline to:
  - Track agent responses for transcript persistence
  - Count conversation turns
  - Emit EndFrame after max turns to terminate the call
  - Fire transcript-sync callbacks (non-blocking)
"""

from __future__ import annotations

import asyncio
import re
from typing import Callable, Awaitable, TYPE_CHECKING

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    EndTaskFrame,
    LLMTextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.call_policy import AI_REVEAL_ORIGINAL
from src.conversation_state import PHASE_INSTRUCTION_PREFIX

if TYPE_CHECKING:
    from src.conversation_state import PhaseInstructor

# Strip markdown, bullets, numbering, colons at end — TTS only handles natural language
_SANITIZE_RE = re.compile(r'[*#\-•\[\]\(\)\{\}:]|^\d+\.\s*', re.MULTILINE)

# Sentence-ending punctuation (Hindi purna viram + standard)
_SENTENCE_END_RE = re.compile(r'[।.?!]')

# AI identity reveal keywords — replace entire response if detected.
# Defined in call_policy (pure) so the plan can pick it per call; this is the
# original pattern, still the default and still what positive_feedback uses.
_AI_REVEAL_WORDS = AI_REVEAL_ORIGINAL
_AI_REVEAL_REPLACEMENT = "जी, मैं {teacher_name} बोल रहा हूँ। {student_name} के बारे में बात करते हैं।"


MAX_RESPONSE_WORDS = 20  # Hard cap — phone calls need brevity


def _first_sentence(text: str) -> str:
    """Extract only the first sentence, capped at MAX_RESPONSE_WORDS words."""
    m = _SENTENCE_END_RE.search(text)
    result = text[: m.end()].strip() if m else text.strip()
    # Word budget guard — truncate long sentences
    words = result.split()
    if len(words) > MAX_RESPONSE_WORDS:
        result = " ".join(words[:MAX_RESPONSE_WORDS])
    return result


class CallManager(FrameProcessor):
    """Manages call lifecycle: turn counting, transcript persistence, call ending.

    Placed AFTER the LLM in the pipeline. Observes LLM output frames
    and passes them through transparently to TTS.

    Frame flow:
      LLM → [CallManager observes LLMTextFrame/EndFrame] → TTS
    """

    # Callback: (transcript, turn_number) → awaitable
    OnTurnComplete = Callable[[list[dict], int], Awaitable[None]]

    def __init__(
        self,
        *,
        context,
        call_context: dict,
        max_turns: int = 6,
        on_turn_complete: "CallManager.OnTurnComplete | None" = None,
        phase_instructor: "PhaseInstructor | None" = None,
        ai_reveal_pattern: "re.Pattern[str] | None" = None,
        ai_reveal_line: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context  # Universal LLMContext — holds full conversation
        self._call_context = call_context
        # Both come from the CallPlan. None keeps the original behaviour
        # (original pattern, Hindi "I am the teacher" replacement).
        self._ai_reveal_re = ai_reveal_pattern or _AI_REVEAL_WORDS
        self._ai_reveal_line = ai_reveal_line
        self._max_turns = max_turns
        self._on_turn_complete = on_turn_complete
        self._phase_instructor = phase_instructor
        self._turn_number = 0
        self._current_response = ""
        self._collecting = False
        self._sentence_complete = False  # True after first sentence end detected
        self._ai_reveal_detected = False  # True if LLM tries to reveal AI identity

    @property
    def turn_number(self) -> int:
        return self._turn_number

    async def _end_call(self) -> None:
        """Ask the pipeline TASK to end, from its source.

        An EndFrame pushed downstream from here (the old way) only reached TTS
        and the output: the task never saw it, runner.run() never returned, so
        main.py never hung up and the parent sat on a silent, billed line
        until they hung up or pipecat's idle timeout fired. EndTaskFrame goes
        UPSTREAM to the task, which queues an EndFrame at the source; it
        travels behind the goodbye already on its way to TTS, so the goodbye
        is still spoken first.
        """
        await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    @property
    def transcript(self) -> list[dict]:
        """Extract transcript from universal LLMContext messages.

        Universal context uses OpenAI-style dicts:
          {"role": "user"/"assistant", "content": "..."}
        """
        transcript = []
        for msg in self._context.get_messages():
            try:
                if not isinstance(msg, dict):
                    continue
                role_raw = msg.get("role", "")
                if role_raw == "user":
                    role = "parent"
                elif role_raw in ("assistant", "model"):
                    role = "agent"
                else:
                    continue

                content = msg.get("content", "")
                text = content if isinstance(content, str) else str(content)

                # Skip phase instruction messages (injected by PhaseInstructor)
                if text.startswith(PHASE_INSTRUCTION_PREFIX):
                    continue

                if text and text.strip():
                    transcript.append({"role": role, "text": text.strip()})
            except Exception as e:
                logger.warning(f"Error extracting message: {e}")
                continue

        return transcript

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._collecting = True
            self._current_response = ""
            self._sentence_complete = False
            self._ai_reveal_detected = False

        elif isinstance(frame, LLMTextFrame):
            if self._collecting:
                # If AI reveal detected or first sentence complete, suppress
                if self._ai_reveal_detected or self._sentence_complete:
                    self._current_response += frame.text  # Still collect for context
                    return
                self._current_response += frame.text
                # Check if first sentence is now complete
                m = _SENTENCE_END_RE.search(self._current_response)
                if m:
                    self._sentence_complete = True
                    # Truncate frame text: only keep up to the sentence end
                    # prev text length = accumulated - this frame's text
                    prev_len = len(self._current_response) - len(frame.text)
                    cut_pos = m.end() - prev_len  # position within this frame
                    if cut_pos > 0:
                        frame = LLMTextFrame(text=frame.text[:cut_pos])
                    else:
                        # Sentence ended in a previous frame — suppress this one
                        return
            # Guard: if any AI-revealing words in accumulated response, suppress ALL text
            if self._ai_reveal_re.search(self._current_response):
                self._ai_reveal_detected = True
                return  # Suppress this and all future text frames

            # Sanitize text for TTS — strip markdown/symbols that break Sarvam
            clean = _SANITIZE_RE.sub('', frame.text)
            # Replace newlines with spaces (preserve word spacing!)
            clean = clean.replace('\n', ' ')
            # Only strip if result is entirely whitespace
            if not clean.strip():
                return
            if clean != frame.text:
                frame = LLMTextFrame(text=clean)

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._collecting = False

            # Already past max turns — suppress everything
            if self._turn_number >= self._max_turns:
                return

            self._turn_number += 1

            # Advance conversation phase (PhaseInstructor is upstream of LLM,
            # so it can't see LLMFullResponseEndFrame — we drive it from here)
            if self._phase_instructor:
                self._phase_instructor.advance_phase()

            if self._current_response:
                # Truncate to first sentence only
                truncated = _first_sentence(self._current_response)
                truncated = _SANITIZE_RE.sub('', truncated).replace('\n', ' ').strip()

                # Guard: If LLM reveals it's AI, replace with safe response
                if self._ai_reveal_re.search(truncated):
                    if self._ai_reveal_line:
                        truncated = self._ai_reveal_line
                    else:
                        teacher = self._call_context.get("teacherName", "Teacher")
                        student = self._call_context.get("studentName", "student")
                        truncated = f"जी, मैं {teacher} बोल रहा हूँ। {student} के बारे में बात करते हैं।"
                    logger.warning(f"Turn {self._turn_number}: AI reveal blocked, replaced")
                    # Also need to push this replacement to TTS
                    from pipecat.frames.frames import TTSSpeakFrame
                    await self.push_frame(TTSSpeakFrame(text=truncated), direction)
                    # No early return: a replaced reply is still a turn, and
                    # on the last turn the call must still end (review
                    # rereview-0b #7: a goodbye that tripped the guard left the
                    # line up until the parent hung up).
                else:
                    # Length only: what the model said is part of the conversation.
                    logger.info(f"Turn {self._turn_number}: reply of {len(truncated)} chars")
                # Add the TRUNCATED (or replacement) response to LLM context
                self._context.add_message(
                    {"role": "assistant", "content": truncated}
                )

            # Fire transcript sync (non-blocking)
            if self._on_turn_complete:
                try:
                    asyncio.create_task(
                        self._on_turn_complete(self.transcript, self._turn_number)
                    )
                except Exception as e:
                    logger.warning(f"Turn sync callback failed: {e}")

            # End call after max turns
            if self._turn_number >= self._max_turns:
                logger.info(f"Max turns ({self._max_turns}) reached — ending call")
                await self.push_frame(frame, direction)
                await self._end_call()
                return

            # End call after CLOSE phase goodbye is spoken
            # (PhaseInstructor transitions CLOSE → ENDED after its 1 turn)
            if self._phase_instructor is not None:
                from src.conversation_state import ConversationPhase
                if self._phase_instructor.phase == ConversationPhase.ENDED:
                    logger.info(f"CLOSE phase complete — ending call after turn {self._turn_number}")
                    await self.push_frame(frame, direction)
                    await self._end_call()
                    return

        # Pass ALL frames through — CallManager is transparent
        await self.push_frame(frame, direction)
