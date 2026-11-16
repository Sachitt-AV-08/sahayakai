"""
Pipeline stage that hears a possible "please don't call me again" before the
model does, and confirms it before anything is written.

Pipeline position: stt → StopRequestGuard → stt_filter → context_aggregator.user()

Every final transcription of the parent's speech goes through
opt_out.StopConversation:

  ASK      a possible stop. The guard swallows it and bot.py asks one fixed,
           localized question ("should the school stop calling this
           number?"). A timer starts; if the parent says nothing before it
           runs out, the question lapses and the call carries on.
  CONFIRM  an explicit yes to that question. Swallowed; bot.py writes the
           do-not-call record, speaks the acknowledgement and ends the call.
  DECLINE  a no or an unclear answer. Handed to the model like any other
           speech, so the call carries on. Nothing is written.
  IGNORE   our own question heard back, or speech after a confirmed stop.
  PASS     ordinary speech.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from src.opt_out import StopAction, StopConversation

# How long the parent has to answer the confirmation question (it takes about
# four seconds to speak) before it lapses and the call carries on.
CONFIRM_ANSWER_SECONDS = 12.0


class StopRequestGuard(FrameProcessor):
    def __init__(
        self,
        *,
        conversation: StopConversation,
        on_ask: Callable[[str], Awaitable[None]],
        on_confirm: Callable[[str], Awaitable[None]],
        answer_seconds: float | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._conversation = conversation
        self._on_ask = on_ask
        self._on_confirm = on_confirm
        self._answer_seconds = CONFIRM_ANSWER_SECONDS if answer_seconds is None else answer_seconds
        self._timer: asyncio.Task | None = None

    @property
    def stopped(self) -> bool:
        return self._conversation.confirmed

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _lapse_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._conversation.awaiting:
            self._conversation.on_no_answer()  # silence: carry on, write nothing

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text or ""
            action = self._conversation.on_parent_speech(text)
            if action is StopAction.ASK:
                self._cancel_timer()
                await self._on_ask(text)
                self._timer = asyncio.ensure_future(self._lapse_after(self._answer_seconds))
                return
            if action is StopAction.CONFIRM:
                self._cancel_timer()
                await self._on_confirm(text)
                return
            if action is StopAction.IGNORE:
                return
            if action is StopAction.DECLINE:
                self._cancel_timer()

        await self.push_frame(frame, direction)
