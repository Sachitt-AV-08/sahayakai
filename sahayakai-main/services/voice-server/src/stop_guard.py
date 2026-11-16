"""
Pipeline stage that hears a possible "please don't call me again" before the
model does.

Pipeline position: stt → StopRequestGuard → stt_filter → context_aggregator.user()

Every final transcription of the parent's speech goes through
opt_out.StopListener:
  NOTICE  a stop. Swallowed (the model does not answer it) and handed to
          bot.py, which records it for the teacher, says the fixed line and
          closes the call politely.
  WAIT    a plain "don't call me". Held, unseen by the model, until the next
          transcription or QUIET_SECONDS of quiet: "Don't call me" + "after
          9 pm" is a limit, not a stop. If it was not a stop after all, the
          held transcription is released to the model, in order, before the
          next one.
  PASS    ordinary speech, passed on unchanged.
Nothing here writes anything, and nothing anywhere in the engine blocks a
number.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.opt_out import NOTICE, WAIT, StopListener, normalize

# How long a plain stop waits for the rest of the sentence.
QUIET_SECONDS = 1.2


class StopRequestGuard(FrameProcessor):
    def __init__(
        self,
        *,
        listener: StopListener,
        on_stop: Callable[[str, str], Awaitable[None]],
        **kwargs,
    ):
        """`on_stop(utterance, quote)`: the swallowed transcription(s), and the
        parent's words that made it a stop."""
        super().__init__(**kwargs)
        self._listener = listener
        self._on_stop = on_stop
        self._held: list[tuple[Frame, FrameDirection]] = []
        self._quiet: asyncio.Task | None = None
        self.spoken_since_notice = False

    @property
    def noticed(self) -> bool:
        return self._listener.noticed

    @property
    def waiting(self) -> bool:
        return self._listener.pending is not None

    def _cancel_quiet(self) -> None:
        if self._quiet is not None and not self._quiet.done() and self._quiet is not asyncio.current_task():
            self._quiet.cancel()
        self._quiet = None

    def _held_text(self, extra: str = "") -> str:
        texts = [f.text or "" for f, _ in self._held] + ([extra] if extra else [])
        return " ".join(t.strip() for t in texts if t and t.strip())

    async def _fire(self, extra: str = "") -> None:
        utterance = self._held_text(extra)
        self._held = []
        await self._on_stop(utterance, self._listener.quote or utterance)

    async def _after_quiet(self) -> None:
        await asyncio.sleep(QUIET_SECONDS)
        if self._listener.on_quiet():
            await self._fire()

    def take_pending(self) -> tuple[str, str] | None:
        """The call is ending with a plain stop still held: it stands.

        Returns (utterance, quote) and forgets it; None when nothing is held.
        """
        self._cancel_quiet()
        if not self._listener.on_quiet():
            return None
        utterance = self._held_text()
        self._held = []
        return utterance, self._listener.quote or utterance

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text or ""
            if self._listener.noticed:
                if normalize(text):
                    self.spoken_since_notice = True
            else:
                try:
                    now = asyncio.get_running_loop().time()
                except RuntimeError:
                    now = None
                decision = self._listener.hear(text, now)
                if decision == NOTICE:
                    self._cancel_quiet()
                    await self._fire(text)
                    return
                if decision == WAIT:
                    self._cancel_quiet()
                    self._held.append((frame, direction))
                    self._quiet = asyncio.ensure_future(self._after_quiet())
                    return
                # Not a stop: anything held goes to the model first, in order.
                self._cancel_quiet()
                held, self._held = self._held, []
                for held_frame, held_direction in held:
                    await self.push_frame(held_frame, held_direction)

        await self.push_frame(frame, direction)
