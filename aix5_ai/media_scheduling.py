"""Keep large WebRTC video frames from monopolizing the media event loop."""

from __future__ import annotations

import asyncio
import functools

RTP_YIELD_PACKETS = 32


def install_rtp_fairness(transport_class=None):
    """Yield after bounded packet batches, preserving aiortc's send behavior.

    aiortc's UDP send coroutines can complete without suspending. A large H.264
    IDR therefore sends hundreds of fragments before PCM readers and native
    encoder pipes get a turn. Cooperative scheduling avoids that starvation.
    """
    if transport_class is None:
        from aiortc.rtcdtlstransport import RTCDtlsTransport

        transport_class = RTCDtlsTransport
    original = transport_class._send_rtp
    if getattr(original, "_aix5_fair_sender", False):
        return

    @functools.wraps(original)
    async def send(self, data):
        result = await original(self, data)
        count = getattr(self, "_aix5_rtp_batch_count", 0) + 1
        if count >= RTP_YIELD_PACKETS:
            self._aix5_rtp_batch_count = 0
            await asyncio.sleep(0)
        else:
            self._aix5_rtp_batch_count = count
        return result

    send._aix5_fair_sender = True
    transport_class._send_rtp = send


async def terminate_process(process, *, drain_stdout=True, timeout=2):
    """Stop a media child and bound pipe cleanup, including paused transports.

    The read owner enables draining only after its normal read has exited. A
    watchdog leaves draining to that owner, so it never races readexactly().
    """

    async def discard():
        if process.stdout is not None:
            while await process.stdout.read(65536):
                pass

    draining = asyncio.create_task(discard()) if drain_stdout else None
    waiting = asyncio.create_task(process.wait())
    try:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        done, _ = await asyncio.wait({waiting}, timeout=timeout)
        if not done:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            done, _ = await asyncio.wait({waiting}, timeout=timeout)
        if draining is not None and not draining.done():
            # returncode can be known before the paused stdout transport has
            # delivered EOF. Finish its bounded drain before dropping the pipe.
            await asyncio.wait({draining}, timeout=timeout)
        return bool(done)
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        raise
    finally:
        remaining = [waiting, *([draining] if draining else [])]
        for task in remaining:
            if not task.done():
                task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


class ProcessReadWatchdog:
    """Bound idle reads without creating a new asyncio Task for every packet.

    The reader awaits its pipe directly and can drain already-buffered data in
    the current loop turn. On idle expiry this single watchdog closes the child
    process, which wakes a blocked read with EOF. It also bounds termination.
    """

    def __init__(self, process, timeout, *, interval=0.25, kill_timeout=2):
        self.process = process
        self.loop = asyncio.get_running_loop()
        self.deadline = self.loop.time() + timeout
        self.interval = interval
        self.kill_timeout = kill_timeout
        self.expired = False
        self.task = asyncio.create_task(self._run())

    def reset(self, timeout):
        self.deadline = self.loop.time() + timeout

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self):
        while self.process.returncode is None:
            remaining = self.deadline - self.loop.time()
            if remaining > 0:
                await asyncio.sleep(min(self.interval, remaining))
                continue
            self.expired = True
            await terminate_process(
                self.process, drain_stdout=False, timeout=self.kill_timeout
            )
            return
