from __future__ import annotations

import asyncio
import threading

import pytest

from guardian.runtime import GuardianRuntimeError, GuardianSupervisor


def test_supervisor_runs_scheduler_and_submits_commands_off_caller_thread() -> None:
    scheduler_started = threading.Event()
    release_scheduler = asyncio.Event()
    calls: list[tuple[str, object, bool, int]] = []

    async def scheduler(notify: bool, mark_ready) -> None:
        calls.append(("scheduler", None, notify, threading.get_ident()))
        scheduler_started.set()
        mark_ready()
        await release_scheduler.wait()

    async def scan(market, notify: bool):
        calls.append(("scan", market, notify, threading.get_ident()))
        return {"status": "success"}

    async def delivery(channel):
        calls.append(("delivery", channel, True, threading.get_ident()))

    caller_thread = threading.get_ident()
    supervisor = GuardianSupervisor(
        notify=True,
        scheduler_runner=scheduler,
        scan_runner=scan,
        delivery_test_runner=delivery,
    )
    supervisor.start()
    assert scheduler_started.wait(1)
    assert supervisor.status.state == "RUNNING"
    assert supervisor.submit_scan("US").startswith("scan-")
    assert supervisor.submit_delivery_test("whatsapp").startswith("delivery-")

    for _attempt in range(100):
        if len(calls) == 3:
            break
        threading.Event().wait(0.01)
    assert [(kind, value) for kind, value, _notify, _thread in calls] == [
        ("scheduler", None),
        ("scan", "US"),
        ("delivery", "whatsapp"),
    ]
    assert all(item[3] != caller_thread for item in calls)
    supervisor.stop()
    assert supervisor.status.state == "STOPPED"


def test_supervisor_surfaces_safe_degraded_state_for_command_failure() -> None:
    async def scheduler(_notify: bool, mark_ready) -> None:
        mark_ready()
        await asyncio.Event().wait()

    async def failing_scan(_market, _notify: bool):
        raise RuntimeError("https://secret.invalid/private-token")

    supervisor = GuardianSupervisor(
        notify=False,
        scheduler_runner=scheduler,
        scan_runner=failing_scan,
    )
    supervisor.start()
    for _attempt in range(100):
        if supervisor.status.state == "RUNNING":
            break
        threading.Event().wait(0.01)
    supervisor.submit_scan()
    for _attempt in range(100):
        if supervisor.status.state == "DEGRADED":
            break
        threading.Event().wait(0.01)
    assert supervisor.status.state == "DEGRADED"
    assert supervisor.status.error_code == "runtimeerror"
    assert "private-token" not in repr(supervisor.status)
    supervisor.stop()


def test_supervisor_rejects_commands_before_start() -> None:
    supervisor = GuardianSupervisor(notify=False)
    with pytest.raises(GuardianRuntimeError):
        supervisor.submit_scan()


def test_supervisor_stays_starting_until_startup_watchdog_marks_ready() -> None:
    scheduler_entered = threading.Event()
    release_startup = threading.Event()
    keep_running = asyncio.Event()

    async def scheduler(_notify: bool, mark_ready) -> None:
        scheduler_entered.set()
        await asyncio.to_thread(release_startup.wait)
        mark_ready()
        await keep_running.wait()

    supervisor = GuardianSupervisor(notify=False, scheduler_runner=scheduler)
    supervisor.start()
    assert scheduler_entered.wait(1)
    assert supervisor.status.state == "STARTING"

    release_startup.set()
    for _attempt in range(100):
        if supervisor.status.state == "RUNNING":
            break
        threading.Event().wait(0.01)
    assert supervisor.status.state == "RUNNING"
    supervisor.stop()
