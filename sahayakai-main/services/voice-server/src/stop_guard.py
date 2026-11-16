"""
Pipeline stage that hears a possible "please don't call me again" before the
model does.

Pipeline position: stt → StopRequestGuard → stt_filter → context_aggregator.user()

Every final transcription of the parent's speech goes through
opt_out.StopListener. The first possible stop of the call is swallowed (the
model does not answer it) and handed to bot.py, which records it for the
teacher, says the one fixed line and closes the call politely. Nothing here
writes anything, and nothing anywhere in the engine blocks a number. All
other speech, including everything after the notice, passes through to the
model unchanged.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.opt_out import StopListener, normalize


class StopRequestGuard(FrameProcessor):
    def __init__(
        self,
        *,
        listener: StopListener,
        on_stop: Callable[[str, str], Awaitable[None]],
        **kwargs,
    ):
        """`on_stop(utterance, quote)`: the swallowed transcription, and the
        parent's words that made it a stop (the utterance, or the previous one
        joined to it when speech-to-text split the sentence)."""
        super().__init__(**kwargs)
        self._listener = listener
        self._on_stop = on_stop
        self.spoken_since_notice = False

    @property
    def noticed(self) -> bool:
        return self._listener.noticed

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text or ""
            if self._listener.noticed:
                if normalize(text):
                    self.spoken_since_notice = True
            elif self._listener.on_parent_speech(text):
                await self._on_stop(text, self._listener.quote or text)
                return

        await self.push_frame(frame, direction)
