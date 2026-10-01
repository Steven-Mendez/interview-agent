"""The technical-notice UI hint is best effort and never blocks speech."""

import asyncio
import time

from interview_agent.interview.dialogue import DialogueController


async def test_notice_signal_is_bounded_and_failures_do_not_propagate():
    controller = DialogueController.__new__(DialogueController)
    seen = []

    async def record(active):
        seen.append(active)

    controller.notice_callback = record
    await controller.signal_notice(True)
    await controller.signal_notice(False)
    assert seen == [True, False]

    async def broken(active):
        raise RuntimeError("room closed")

    controller.notice_callback = broken
    await controller.signal_notice(True)

    async def stuck(active):
        await asyncio.sleep(30)

    controller.notice_callback = stuck
    started = time.monotonic()
    await controller.signal_notice(True)
    assert time.monotonic() - started < 2


async def test_notice_signal_without_room_is_a_no_op():
    controller = DialogueController.__new__(DialogueController)
    await controller.signal_notice(True)
