"""Keep Forge's native queue stream alive independently of browser tabs."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import time
from urllib.parse import urlencode

import httpx
from fastapi import Request


MAX_REPLAY_MESSAGES = 12
MAX_REPLAY_BYTES = 256_000
NON_REPLAYABLE_MESSAGES = {"process_completed", "close_stream"}


@dataclass(eq=False)
class QueueChannel:
    worker_id: str
    account_id: int
    session_hash: str
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[asyncio.Queue[str | None]] = field(default_factory=set)
    replay: deque[str] = field(default_factory=deque)
    replay_bytes: int = 0
    task: asyncio.Task | None = None
    error: str | None = None
    closed: bool = False
    closed_at: float | None = None
    ever_subscribed: bool = False


class QueueRelay:
    """Own the upstream Gradio SSE connection; downstream clients may disconnect."""

    def __init__(self, client: httpx.AsyncClient, worker_urls: dict[str, str]):
        self.client = client
        self.worker_urls = worker_urls
        self.channels: dict[tuple[str, int, str], QueueChannel] = {}

    @staticmethod
    def _key(worker_id: str, account_id: int, session_hash: str) -> tuple[str, int, str]:
        return worker_id, account_id, session_hash

    def get(self, worker_id: str, account_id: int, session_hash: str) -> QueueChannel | None:
        key = self._key(worker_id, account_id, session_hash)
        channel = self.channels.get(key)
        if channel and channel.closed_at and time.monotonic() - channel.closed_at > 120:
            self.channels.pop(key, None)
            return None
        return channel

    async def start(self, worker_id: str, account_id: int, session_hash: str) -> QueueChannel:
        key = self._key(worker_id, account_id, session_hash)
        channel = self.get(worker_id, account_id, session_hash)
        if channel and channel.task and not channel.task.done() and not channel.closed:
            return channel

        channel = QueueChannel(worker_id, account_id, session_hash)
        self.channels[key] = channel
        channel.task = asyncio.create_task(self._pump(channel), name=f"forge-queue-{worker_id}-{session_hash[:8]}")
        return channel

    async def ensure(self, worker_id: str, account_id: int, session_hash: str) -> QueueChannel:
        channel = await self.start(worker_id, account_id, session_hash)
        try:
            await asyncio.wait_for(channel.connected.wait(), timeout=10)
        except TimeoutError as exc:
            await self._discard(channel)
            raise RuntimeError("Forge queue stream did not connect") from exc
        if channel.error:
            await self._discard(channel)
            raise RuntimeError(channel.error)
        return channel

    async def _pump(self, channel: QueueChannel) -> None:
        query = urlencode({"session_hash": channel.session_hash})
        url = f"{self.worker_urls[channel.worker_id]}/queue/data?{query}"
        try:
            async with self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    body = (await response.aread())[:500].decode("utf-8", "replace")
                    channel.error = f"Forge queue stream returned HTTP {response.status_code}: {body}"
                    return
                channel.connected.set()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].lstrip()
                    if not payload:
                        continue
                    await self._publish(channel, payload)
                    try:
                        message = json.loads(payload)
                    except ValueError:
                        continue
                    if message.get("msg") == "close_stream":
                        channel.closed = True
                        break
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            channel.error = f"Forge queue stream disconnected: {type(exc).__name__}"
        finally:
            channel.connected.set()
            channel.closed = True
            channel.closed_at = time.monotonic()
            await self._publish(channel, None)

    async def _publish(self, channel: QueueChannel, payload: str | None) -> None:
        async with channel.lock:
            if channel.subscribers:
                for subscriber in tuple(channel.subscribers):
                    if subscriber.full():
                        try:
                            subscriber.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        subscriber.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass
                return
            if payload is None or len(payload.encode("utf-8")) > MAX_REPLAY_BYTES:
                return
            try:
                message_type = json.loads(payload).get("msg")
            except (TypeError, ValueError, AttributeError):
                message_type = None
            # A reconnecting Gradio page must not receive a terminal output
            # from an older event and overwrite the current Gallery. Live
            # subscribers still receive these messages above; only the replay
            # buffer excludes them.
            if message_type in NON_REPLAYABLE_MESSAGES and channel.ever_subscribed:
                return
            encoded_size = len(payload.encode("utf-8"))
            while channel.replay and (
                len(channel.replay) >= MAX_REPLAY_MESSAGES
                or channel.replay_bytes + encoded_size > MAX_REPLAY_BYTES
            ):
                removed = channel.replay.popleft()
                channel.replay_bytes -= len(removed.encode("utf-8"))
            channel.replay.append(payload)
            channel.replay_bytes += encoded_size

    async def subscribe(self, channel: QueueChannel) -> asyncio.Queue[str | None]:
        subscriber: asyncio.Queue[str | None] = asyncio.Queue(maxsize=32)
        async with channel.lock:
            channel.ever_subscribed = True
            for message in channel.replay:
                try:
                    subscriber.put_nowait(message)
                except asyncio.QueueFull:
                    break
            channel.replay.clear()
            channel.replay_bytes = 0
            if channel.closed:
                subscriber.put_nowait(None)
            else:
                channel.subscribers.add(subscriber)
        return subscriber

    async def unsubscribe(self, channel: QueueChannel, subscriber: asyncio.Queue[str | None]) -> None:
        async with channel.lock:
            channel.subscribers.discard(subscriber)

    async def stream(self, channel: QueueChannel, request: Request):
        subscriber = await self.subscribe(channel)
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(subscriber.get(), timeout=15)
                except TimeoutError:
                    yield 'data: {"msg":"heartbeat"}\n\n'
                    continue
                if payload is None:
                    return
                yield f"data: {payload}\n\n"
        finally:
            await self.unsubscribe(channel, subscriber)

    async def _discard(self, channel: QueueChannel) -> None:
        key = self._key(channel.worker_id, channel.account_id, channel.session_hash)
        if self.channels.get(key) is channel:
            self.channels.pop(key, None)
        if channel.task and not channel.task.done():
            channel.task.cancel()
            await asyncio.gather(channel.task, return_exceptions=True)

    async def close(self) -> None:
        channels = list(self.channels.values())
        self.channels.clear()
        tasks = [channel.task for channel in channels if channel.task and not channel.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
