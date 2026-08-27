"""Synchronous daemon-owned bridge to the async MCP SDK boundary."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from mishkan.mcp.models import McpCallRequest, McpCallResult, McpConnectionRecord
from mishkan.mcp.service import McpService


class McpServiceRunner:
    def __init__(self, service: McpService) -> None:
        self._service = service
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="mishkan-mcp",
        )
        self._cancellations: dict[UUID, threading.Event] = {}
        self._lock = threading.Lock()

    def connect(
        self,
        connection_id: str,
        *,
        principal: str,
        credentials: Mapping[str, str],
    ) -> McpConnectionRecord:
        future = self._executor.submit(
            asyncio.run,
            self._service.connect(connection_id, principal=principal, credentials=credentials),
        )
        return future.result()

    def invoke(
        self,
        request: McpCallRequest,
        *,
        credentials: Mapping[str, str],
        cancellation_requested: Callable[[], bool],
        poll_seconds: float,
    ) -> McpCallResult:
        signal = threading.Event()
        with self._lock:
            self._cancellations[request.id] = signal
        try:
            future = self._executor.submit(
                asyncio.run,
                self._monitored_invoke(
                    request,
                    credentials=credentials,
                    cancellation_requested=cancellation_requested,
                    cancellation_signal=signal,
                    poll_seconds=poll_seconds,
                ),
            )
            return future.result()
        finally:
            with self._lock:
                self._cancellations.pop(request.id, None)

    def cancel(self, request_id: object) -> None:
        identity = request_id if isinstance(request_id, UUID) else UUID(str(request_id))
        with self._lock:
            signal = self._cancellations.get(identity)
        if signal is not None:
            signal.set()

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    async def _monitored_invoke(
        self,
        request: McpCallRequest,
        *,
        credentials: Mapping[str, str],
        cancellation_requested: Callable[[], bool],
        cancellation_signal: threading.Event,
        poll_seconds: float,
    ) -> McpCallResult:
        task = asyncio.create_task(self._service.invoke(request, credentials=credentials))
        while not task.done():
            await asyncio.wait({task}, timeout=poll_seconds)
            if not task.done() and (cancellation_requested() or cancellation_signal.is_set()):
                self._service.request_cancellation(request.id)
        return await task

    def __enter__(self) -> McpServiceRunner:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
