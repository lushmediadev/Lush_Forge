"""Queue stream ownership checks independent of a live Forge worker."""

import asyncio
import unittest

import httpx

from forge_hub.queue_relay import QueueRelay


class GateStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.closed = False
        self.closed_early = False

    async def __aiter__(self):
        yield b'data: {"msg":"process_starts","event_id":"evt-1"}\n\n'
        await self.release.wait()
        yield b'data: {"msg":"process_completed","event_id":"evt-1","success":true}\n\n'
        yield b'data: {"msg":"close_stream"}\n\n'

    async def aclose(self):
        self.closed_early = not self.release.is_set()
        self.closed = True


class QueueRelayTest(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_queue_stream_survives_downstream_disconnect(self):
        stream = GateStream()
        requests = []

        async def worker(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        relay = QueueRelay(client, {"forge1": "http://forge1", "forge2": "http://forge2"})
        try:
            channel = await relay.ensure("forge1", 7, "session-abc")
            same_channel = await relay.ensure("forge1", 7, "session-abc")
            self.assertIs(channel, same_channel)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/queue/data")

            subscriber = await relay.subscribe(channel)
            first = await asyncio.wait_for(subscriber.get(), timeout=1)
            self.assertIn('"process_starts"', first)
            await relay.unsubscribe(channel, subscriber)

            await asyncio.sleep(0.05)
            self.assertFalse(stream.closed)
            self.assertFalse(stream.closed_early)

            stream.release.set()
            await asyncio.wait_for(channel.task, timeout=1)
            self.assertTrue(stream.closed)
            self.assertFalse(stream.closed_early)
        finally:
            await relay.close()
            await client.aclose()

    async def test_reconnect_does_not_replay_terminal_gallery_output(self):
        stream = GateStream()

        async def worker(request: httpx.Request):
            return httpx.Response(200, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        relay = QueueRelay(client, {"forge1": "http://forge1", "forge2": "http://forge2"})
        try:
            channel = await relay.ensure("forge1", 7, "session-reconnect")
            first = await relay.subscribe(channel)
            self.assertIn('"process_starts"', await asyncio.wait_for(first.get(), timeout=1))
            await relay.unsubscribe(channel, first)

            stream.release.set()
            await asyncio.wait_for(channel.task, timeout=1)

            late = await relay.subscribe(channel)
            self.assertIsNone(await asyncio.wait_for(late.get(), timeout=1))
        finally:
            await relay.close()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
