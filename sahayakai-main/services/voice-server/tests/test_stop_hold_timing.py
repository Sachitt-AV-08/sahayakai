"""CLASS GATE (timed) — a held plain stop follows the parent's speech, not a stopwatch.

Verifier, with real timings through create_bot: the hold's timer counted from
when the held words ARRIVED and ignored the parent starting to speak again.
With the 0.5 s VAD stop, "Don't call me" ... "after nine" with any natural
pause over about 0.6 s fired a false stop, and "after nine" landed after the
notice (D1).

The class is "a live decision made while the parent is still talking". The
gate runs the real create_bot with the REAL hold timings (QUIET_SECONDS 1.2,
STT_MARGIN_SECONDS 0.6, HOLD_CAP_SECONDS 5.0) and real gaps between frames:
  * the parent starting to speak at 0.9 s and "after nine" at 1.6 s is not a
    stop, and both reach the model in order;
  * true silence still makes the stop stand, at about 1.2 s plus the margin,
    and not before;
  * the quiet counts from the parent stopping (VAD stop at 1.0 s: about 2.8 s);
  * a parent who keeps talking past the cap gets a decision at the cap: the
    held words go to the model, nothing is recorded, nothing is held.
"""

from __future__ import annotations

import asyncio

import pytest

from bot_harness import FakeCallStore, Frame, FrameDirection, TranscriptionFrame, build_bot, stubbed_runtime
from main_ast import RECORD


class VADUserStartedSpeakingFrame(Frame):
    pass


class VADUserStoppedSpeakingFrame(Frame):
    pass


def _timeline(steps, *, until: float):
    """Feed (at_seconds, text-or-frame) to the real guard; return what happened, with times."""
    records: list = []
    with stubbed_runtime():
        async def go():
            loop = asyncio.get_running_loop()

            async def recorded(quote, source):
                records.append((loop.time() - t0, quote))
                return True

            run = await build_bot({**RECORD, "parentLanguage": "English", "id": "OutreachFs0000000001"},
                                  call_store=FakeCallStore(), on_opt_out_request=recorded)
            guard = run.processor("StopRequestGuard")
            to_model: list = []
            original = guard.push_frame

            async def timed_push(frame, direction=FrameDirection.DOWNSTREAM):
                if isinstance(frame, TranscriptionFrame):
                    to_model.append((loop.time() - t0, frame.text))
                await original(frame, direction)

            guard.push_frame = timed_push
            t0 = loop.time()
            for at, what in steps:
                await asyncio.sleep(max(0.0, t0 + at - loop.time()))
                frame = TranscriptionFrame(text=what) if isinstance(what, str) else what
                await guard.process_frame(frame, FrameDirection.DOWNSTREAM)
            await asyncio.sleep(max(0.0, t0 + until - loop.time()))
            return records, to_model, guard

        records, to_model, guard = asyncio.run(go())
    return records, to_model, guard


def test_the_parent_speaking_again_inside_the_hold_is_not_a_stop():
    records, to_model, guard = _timeline(
        [(0.0, "Don't call me"), (0.9, VADUserStartedSpeakingFrame()), (1.6, "after nine")], until=2.6)
    assert records == [], "a false stop"
    assert [t for _, t in to_model] == ["Don't call me", "after nine"]
    assert not guard.noticed and not guard.waiting


def test_true_silence_still_makes_the_stop_stand_after_the_quiet_and_the_margin():
    records, to_model, _guard = _timeline([(0.0, "Don't call me")], until=2.4)
    [(at, quote)] = records
    assert quote == "Don't call me" and to_model == []
    assert 1.7 <= at <= 2.2, f"stood at {at:.2f} s, expected about 1.8 s (1.2 + 0.6)"


def test_the_quiet_counts_from_when_the_parent_stops_speaking():
    records, _to_model, _guard = _timeline(
        [(0.0, "Don't call me"), (0.4, VADUserStartedSpeakingFrame()), (1.0, VADUserStoppedSpeakingFrame())],
        until=3.4)
    [(at, _quote)] = records
    assert 2.7 <= at <= 3.2, f"stood at {at:.2f} s, expected about 2.8 s (1.0 + 1.2 + 0.6)"


def test_a_parent_who_keeps_talking_gets_a_decision_at_the_cap():
    records, to_model, guard = _timeline(
        [(0.0, "Don't call me"), (0.5, VADUserStartedSpeakingFrame())], until=5.6)
    assert records == [], "no live stop while they were still talking"
    [(at, text)] = to_model
    assert text == "Don't call me" and 4.9 <= at <= 5.4, f"released at {at:.2f} s, expected the 5 s cap"
    assert not guard.waiting
