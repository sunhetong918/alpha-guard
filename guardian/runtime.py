"""Background asyncio owner used by the foreground Guardian process."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Literal

Market = Literal["US", "HK"]
ReadyCallback = Callable[[], None]
SchedulerRunner = Callable[[bool, ReadyCallback], Coroutine[Any, Any, None]]
ScanRunner = Callable[
    [Market | None, bool], Coroutine[Any, Any, dict[str, Any]]
]
DeliveryTestRunner = Callable[
    [Literal["telegram", "whatsapp"]], Coroutine[Any, Any, None]
]


@dataclass(frozen=True, slots=True)
class GuardianRuntimeStatus:
    state: Literal["STARTING", "RUNNING", "DEGRADED", "STOPPED"]
    error_code: str | None = None


class GuardianRuntimeError(RuntimeError):
    pass


class GuardianSupervisor:
    """Run the scheduler and command coroutines on one dedicated event loop.

    Qt remains the IPC/UI event loop.  All provider and delivery work stays on
    this background loop, so a slow network request cannot freeze local health
    or status calls.
    """

    def __init__(
        self,
        *,
        notify: bool,
        scheduler_runner: SchedulerRunner | None = None,
        scan_runner: ScanRunner | None = None,
        delivery_test_runner: DeliveryTestRunner | None = None,
    ) -> None:
        self._notify = notify
        self._scheduler_runner = scheduler_runner or _default_scheduler_runner
        self._scan_runner = scan_runner or _default_scan_runner
        self._delivery_test_runner = (
            delivery_test_runner or _default_delivery_test_runner
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._started = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._error_code: str | None = None
        self._futures: set[Future[Any]] = set()

    @property
    def status(self) -> GuardianRuntimeStatus:
        thread = self._thread
        if thread is None:
            return GuardianRuntimeStatus("STOPPED")
        if self._error_code is not None:
            return GuardianRuntimeStatus("DEGRADED", self._error_code)
        if thread.is_alive() and not self._stopped.is_set():
            if not self._ready.is_set():
                return GuardianRuntimeStatus("STARTING")
            return GuardianRuntimeStatus("RUNNING")
        return GuardianRuntimeStatus("STOPPED")

    def start(self, *, timeout_seconds: float = 5.0) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._started.clear()
            self._ready.clear()
            self._stopped.clear()
            self._error_code = None
            self._thread = threading.Thread(
                target=self._thread_main,
                name="alpha-guard-runtime",
                daemon=True,
            )
            self._thread.start()
        if not self._started.wait(timeout_seconds):
            self.stop(timeout_seconds=1.0)
            raise GuardianRuntimeError("Guardian runtime did not start")

    def submit_scan(self, market: Market | None = None) -> str:
        if market not in {None, "US", "HK"}:
            raise ValueError("unsupported market")
        return self._submit(self._scan_runner(market, self._notify), "scan")

    def submit_delivery_test(
        self, channel: Literal["telegram", "whatsapp"]
    ) -> str:
        if channel not in {"telegram", "whatsapp"}:
            raise ValueError("unsupported channel")
        return self._submit(self._delivery_test_runner(channel), "delivery")

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        thread = self._thread
        loop = self._loop
        task = self._scheduler_task
        if loop is not None and loop.is_running():
            if task is not None:
                loop.call_soon_threadsafe(task.cancel)
            loop.call_soon_threadsafe(_cancel_tasks, loop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_seconds)
        self._stopped.set()

    def _submit(self, awaitable: Coroutine[Any, Any, Any], prefix: str) -> str:
        loop = self._loop
        if (
            loop is None
            or not loop.is_running()
            or self.status.state not in {"RUNNING", "DEGRADED"}
        ):
            awaitable.close()
            raise GuardianRuntimeError("Guardian runtime is unavailable")
        request_id = f"{prefix}-{uuid.uuid4().hex[:16]}"
        future: Future[Any] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._command_done)
        return request_id

    def _command_done(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001 - background trust boundary
            self._error_code = _error_code(exc)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        task: asyncio.Task[None] = loop.create_task(
            self._scheduler_runner(self._notify, self._mark_ready)
        )
        self._scheduler_task = task
        self._started.set()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - sanitized service boundary
            self._error_code = _error_code(exc)
        finally:
            if not self._ready.is_set() and self._error_code is None:
                self._error_code = "startup_incomplete"
            _cancel_tasks(loop)
            pending = asyncio.all_tasks(loop)
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None
            self._scheduler_task = None
            self._stopped.set()

    def _mark_ready(self) -> None:
        """Publish RUNNING only after the startup watchdog has completed."""

        self._ready.set()


def _cancel_tasks(loop: asyncio.AbstractEventLoop) -> None:
    current = asyncio.current_task(loop=loop)
    for task in asyncio.all_tasks(loop):
        if task is not current:
            task.cancel()


async def _default_scheduler_runner(
    notify: bool,
    mark_ready: ReadyCallback,
) -> None:
    from main import _serve_scheduler

    await _serve_scheduler(notify=notify, on_ready=mark_ready)


async def _default_scan_runner(
    market: Market | None, notify: bool
) -> dict[str, Any]:
    from main import run_stock_scan

    return await run_stock_scan(market=market, notify=notify)


async def _default_delivery_test_runner(
    channel: Literal["telegram", "whatsapp"],
) -> None:
    from config import get_settings
    from main import STATE_PATH
    from notifier.mobile import deliver_mobile
    from state import StateStore

    settings = get_settings()
    selected_settings = settings.model_copy(
        update=(
            {"whatsapp_enabled": False}
            if channel == "telegram"
            else {"notifications_enabled": False}
        )
    )
    now = _utc_now()
    with StateStore(STATE_PATH) as store:
        report = await deliver_mobile(
            store,
            business_key=f"channel-test:{channel}:{uuid.uuid4().hex}",
            kind="trust",
            payload="Alpha Guard 通道测试：仅验证提醒链路，不执行交易。",
            settings=selected_settings,
            now=now,
        )
    selected = next(
        (item for item in report.channels if item.channel == channel), None
    )
    if selected is None or not selected.accepted:
        raise GuardianRuntimeError("delivery test was not accepted")


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    raw = type(exc).__name__.lower()
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in raw
    ).strip("_")
    return (normalized or "unknown")[:64]


__all__ = [
    "GuardianRuntimeError",
    "GuardianRuntimeStatus",
    "GuardianSupervisor",
]
