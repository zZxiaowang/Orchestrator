"""运行事件总线（SSE 数据源）。

同一进程内既保存历史（后连接的界面能补全上下文），又向在线订阅者实时推送。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

HISTORY_LIMIT = 1500
HEARTBEAT_SECONDS = 15.0


class EventBus:
    def __init__(self) -> None:
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._seq: dict[str, int] = {}

    def publish(self, run_id: str, event_type: str, **data: Any) -> dict[str, Any]:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        event = {
            "type": event_type,
            "run_id": run_id,
            # 单调序号是断线续传的唯一依据：Windows 上 datetime.now() 精度约 15ms，
            # 同一批事件时间戳可能相同，用时间戳过滤会漏事件。
            "seq": seq,
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data": data,
        }
        history = self._history.setdefault(run_id, [])
        history.append(event)
        if len(history) > HISTORY_LIMIT:
            del history[: len(history) - HISTORY_LIMIT]
        for queue in list(self._subscribers.get(run_id, ())):
            queue.put_nowait(event)
        return event

    async def subscribe(
        self, run_id: str, since: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # 注意：以下到注册完成为止不能有 await，否则可能漏事件或重复推送
        for event in self._history.get(run_id, []):
            if since is not None and int(event.get("seq", 0)) <= since:
                continue
            queue.put_nowait(event)
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield {"type": "ping", "run_id": run_id, "data": {}}
                    continue
                yield event
        finally:
            subscribers = self._subscribers.get(run_id)
            if subscribers:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(run_id, None)

    def clear(self, run_id: str) -> None:
        self._history.pop(run_id, None)

    def current_seq(self, run_id: str) -> int:
        """该运行已发布的事件序号；用于客户端做增量订阅起点。"""
        return self._seq.get(run_id, 0)
