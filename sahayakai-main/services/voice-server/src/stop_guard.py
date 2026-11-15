"""
Pipeline stage that hears "please don't call me again" before the model does.

Pipeline position: stt → StopRequestGuard → stt_filter → context_aggregator.user()

Every final transcription of the parent's speech goes through
opt_out.StopDetector. On a match the guard swallows that transcription (the
model never answers it, so it cannot argue or promise anything) and calls
`on_stop`, which bot.py uses to write the suppression immediately, speak the
fixed acknowledgement and end the call. After that, nothing else the parent
says reaches the model.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.opt_out import StopDetector


class StopRequestGuard(FrameProcessor):
    def __init__(
        self,
        *,
        on_stop: Callable[[], Awaitable[None]],
        detector: StopDetector | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._detector = detector or StopDetector()
        self._on_stop = on_stop
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            if self._stopped:
                return  # the call is ending; nothing more reaches the model
            if self._detector.observe(frame.text):
                self._stopped = True
                await self._on_stop()
                return

        await self.push_frame(frame, direction)
