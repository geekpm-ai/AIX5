import asyncio
from itertools import pairwise

import pytest

from aix5_ai.media_scheduling import RTP_YIELD_PACKETS, install_rtp_fairness


async def test_large_video_frame_gives_audio_reader_a_turn_between_packet_batches():
    class Transport:
        def __init__(self):
            self.sent = []

        async def _send_rtp(self, data):
            # UDP sendto does not suspend even though the wrapper is async.
            self.sent.append(data)

    install_rtp_fairness(Transport)
    wrapped = Transport._send_rtp
    install_rtp_fairness(Transport)
    assert Transport._send_rtp is wrapped
    transport = Transport()
    audio_observations = []

    async def send_idr():
        for number in range(100):
            await transport._send_rtp(number)

    async def audio_reader():
        while len(transport.sent) < 100:
            audio_observations.append(len(transport.sent))
            await asyncio.sleep(0)

    await asyncio.gather(send_idr(), audio_reader())
    assert transport.sent == list(range(100))
    assert audio_observations[0] <= RTP_YIELD_PACKETS
    assert len(audio_observations) >= 3
    assert max(b - a for a, b in pairwise(audio_observations)) <= RTP_YIELD_PACKETS


async def test_transport_send_failure_is_preserved():
    class Transport:
        async def _send_rtp(self, data):
            raise ConnectionError("transport closed")

    install_rtp_fairness(Transport)
    with pytest.raises(ConnectionError, match="transport closed"):
        await Transport()._send_rtp(b"packet")


async def test_process_watchdog_terminates_idle_pipe_reader():
    import sys

    from aix5_ai.media_scheduling import ProcessReadWatchdog

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
    )
    watchdog = ProcessReadWatchdog(proc, 0.05, interval=0.01)
    try:
        with pytest.raises(asyncio.IncompleteReadError):
            await asyncio.wait_for(proc.stdout.readexactly(1), 2)
        assert watchdog.expired
        await proc.wait()
    finally:
        await watchdog.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_closing_read_watchdog_preserves_healthy_process():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from aix5_ai.media_scheduling import ProcessReadWatchdog

    proc = SimpleNamespace(
        returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock()
    )
    watchdog = ProcessReadWatchdog(proc, 0.01, interval=0.001)
    await watchdog.close()
    await asyncio.sleep(0.02)
    assert watchdog.task.done()
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


async def test_already_exited_child_drains_paused_pipe_to_eof():
    import sys

    from aix5_ai.media_scheduling import terminate_process

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os,time; os.write(1, b'x' * 4194304); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        limit=1024,
    )
    try:
        # Under board load Python startup can exceed 50 ms. Await actual
        # output and high water so this always exercises a paused transport.
        await asyncio.wait_for(proc.stdout.readexactly(1), 5)
        deadline = asyncio.get_running_loop().time() + 2
        while not proc.stdout._paused and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert proc.stdout._paused
        proc.kill()
        for _ in range(100):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.01)
        assert proc.returncode is not None
        assert not proc.stdout.at_eof()
        assert await terminate_process(proc, timeout=0.5)
        assert proc.stdout.at_eof()
    finally:
        await terminate_process(proc, timeout=0.5)
