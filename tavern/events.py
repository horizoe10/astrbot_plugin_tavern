from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any


class EventBroker:
    """Small in-process fan-out used by the WebUI activity stream."""

    def __init__(self, hooks: Any = None) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._hooks = hooks

    async def publish(self, event: Mapping[str, Any]) -> None:
        if self._closed:
            return
        payload = dict(event)
        hook_name = str(payload.get("hook") or "")
        if hook_name and self._hooks is not None:
            await self._hooks.dispatch(hook_name, payload)
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield {"type": "ready"}
            while not self._closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    yield {"type": "keepalive"}
                    continue
                yield item
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            self._subscribers.clear()
