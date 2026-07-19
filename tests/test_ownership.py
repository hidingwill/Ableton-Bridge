"""Ownership coordination and multi-client MCP availability tests."""

import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from MCP_Server.ownership import OwnershipManager


def _configured_manager(port, *, start=None, stop=None, environment=None):
    """Build an ownership manager with lightweight lifecycle callbacks."""
    manager = OwnershipManager(port, environment=environment)
    manager.configure_backend(start or (lambda: None), stop or (lambda: None))
    return manager


def test_one_owner_and_standby_metadata(unused_tcp_port):
    """A standby should report reliable metadata for the current owner."""
    first = _configured_manager(
        unused_tcp_port,
        environment={"CODEX_THREAD_ID": "task-owner"},
    )
    second = _configured_manager(unused_tcp_port)
    try:
        claimed = first.ensure_control(client_name="Codex")
        standby = second.ensure_control(client_name="Other client")

        assert claimed.acquired is True
        assert standby.acquired is False
        assert standby.control["control_role"] == "standby"
        assert standby.control["control_availability"] == "owned"
        assert standby.control["owner"]["instance_id"] == claimed.control["owner"]["instance_id"]
        assert standby.control["owner"]["client_name"] == "Codex"
        assert standby.control["owner"]["task_id"] == "task-owner"
        assert standby.control["owner"]["process_id"] == os.getpid()
    finally:
        first.shutdown()
        second.shutdown()


def test_release_allows_standby_to_claim(unused_tcp_port):
    """Manual release should let a waiting process become the next owner."""
    starts = []
    stops = []
    first = _configured_manager(
        unused_tcp_port,
        start=lambda: starts.append("first"),
        stop=lambda: stops.append("first"),
    )
    second = _configured_manager(
        unused_tcp_port,
        start=lambda: starts.append("second"),
        stop=lambda: stops.append("second"),
    )
    try:
        assert first.ensure_control().acquired is True
        assert second.ensure_control().acquired is False
        assert first.release().released is True
        assert second.ensure_control().acquired is True
        assert starts == ["first", "second"]
        assert stops == ["first"]
    finally:
        first.shutdown()
        second.shutdown()


def test_shutdown_automatically_releases_control(unused_tcp_port):
    """Normal process shutdown should release control for another manager."""
    owner = _configured_manager(unused_tcp_port)
    next_owner = _configured_manager(unused_tcp_port)
    try:
        assert owner.ensure_control().acquired is True
        owner.shutdown()
        assert next_owner.ensure_control().acquired is True
    finally:
        owner.shutdown()
        next_owner.shutdown()


def test_simultaneous_claim_has_exactly_one_winner(unused_tcp_port):
    """Concurrent claims should produce exactly one backend owner."""
    managers = [
        _configured_manager(unused_tcp_port),
        _configured_manager(unused_tcp_port),
    ]
    barrier = threading.Barrier(3)
    results = []

    def claim(manager):
        """Attempt one claim after all contenders reach the barrier."""
        barrier.wait()
        results.append(manager.ensure_control().acquired)

    threads = [threading.Thread(target=claim, args=(manager,)) for manager in managers]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2.0)

        assert sorted(results) == [False, True]
    finally:
        for manager in managers:
            manager.shutdown()


def test_backend_start_failure_releases_port(unused_tcp_port):
    """A cleanly handled startup failure should make the port claimable again."""
    stopped = []

    def fail_start():
        """Simulate Ableton being unavailable during backend startup."""
        raise RuntimeError("Live unavailable")

    failing = _configured_manager(
        unused_tcp_port,
        start=fail_start,
        stop=lambda: stopped.append(True),
    )
    replacement = _configured_manager(unused_tcp_port)
    try:
        result = failing.ensure_control()

        assert result.acquired is False
        assert "Live unavailable" in result.error
        assert stopped == [True]
        assert replacement.ensure_control().acquired is True
    finally:
        failing.shutdown()
        replacement.shutdown()


def test_failed_start_retains_port_until_partial_cleanup_finishes(unused_tcp_port):
    """Failed startup should retain ownership while cleanup remains incomplete."""
    cleanup_ready = {"value": False}

    def fail_start():
        """Simulate startup failing after owner resources may have begun."""
        raise RuntimeError("Live unavailable")

    failing = _configured_manager(
        unused_tcp_port,
        start=fail_start,
        stop=lambda: cleanup_ready["value"],
    )
    replacement = _configured_manager(unused_tcp_port)
    try:
        result = failing.ensure_control()
        assert result.acquired is False
        assert "ownership was retained" in result.error
        assert result.control["control_role"] == "owner"
        assert replacement.ensure_control().acquired is False

        cleanup_ready["value"] = True
        assert failing.release().released is True
        assert replacement.ensure_control().acquired is True
    finally:
        cleanup_ready["value"] = True
        failing.shutdown()
        replacement.shutdown()


def test_startup_and_shutdown_transitions_are_serialized(unused_tcp_port):
    """Shutdown should wait for an in-progress startup transition."""
    start_entered = threading.Event()
    allow_start = threading.Event()
    shutdown_entered = threading.Event()
    shutdown_done = threading.Event()
    claims = []
    stops = []

    def start():
        """Pause backend startup until the shutdown race is observable."""
        start_entered.set()
        allow_start.wait(timeout=1.0)

    def shutdown():
        """Request shutdown and record when the transition completes."""
        shutdown_entered.set()
        manager.shutdown()
        shutdown_done.set()

    manager = _configured_manager(
        unused_tcp_port,
        start=start,
        stop=lambda: stops.append(True),
    )
    claim_thread = threading.Thread(
        target=lambda: claims.append(manager.ensure_control()),
    )
    shutdown_thread = threading.Thread(target=shutdown)
    try:
        claim_thread.start()
        assert start_entered.wait(timeout=1.0)
        shutdown_thread.start()
        assert shutdown_entered.wait(timeout=1.0)

        # Shutdown must wait for startup instead of tearing resources down
        # underneath it and then allowing startup to publish a stale owner.
        assert not shutdown_done.wait(timeout=0.05)
        allow_start.set()
        claim_thread.join(timeout=1.0)
        shutdown_thread.join(timeout=1.0)

        assert len(claims) == 1
        assert claims[0].acquired is True
        assert stops == [True]
        assert manager.status()["control_role"] == "standby"
        assert manager.status()["control_availability"] == "available"
    finally:
        allow_start.set()
        manager.shutdown()


def test_incomplete_cleanup_retains_port_ownership(unused_tcp_port):
    """A false cleanup result should retain ownership until a later retry."""
    cleanup_ready = {"value": False}
    owner = _configured_manager(
        unused_tcp_port,
        stop=lambda: cleanup_ready["value"],
    )
    standby = _configured_manager(unused_tcp_port)
    try:
        assert owner.ensure_control().acquired is True

        incomplete = owner.release()
        assert incomplete.released is False
        assert "still stopping" in incomplete.error
        assert incomplete.control["control_role"] == "owner"
        assert standby.ensure_control().acquired is False

        cleanup_ready["value"] = True
        assert owner.release().released is True
        assert standby.ensure_control().acquired is True
    finally:
        cleanup_ready["value"] = True
        owner.shutdown()
        standby.shutdown()


def test_cleanup_exception_retains_port_ownership(unused_tcp_port):
    """A cleanup exception should retain ownership and surface its reason."""
    cleanup_raises = {"value": True}

    def stop():
        """Fail cleanup until the test allows a successful retry."""
        if cleanup_raises["value"]:
            raise RuntimeError("dashboard still bound")
        return True

    owner = _configured_manager(unused_tcp_port, stop=stop)
    standby = _configured_manager(unused_tcp_port)
    try:
        assert owner.ensure_control().acquired is True

        failed = owner.release()
        assert failed.released is False
        assert "dashboard still bound" in failed.error
        assert standby.ensure_control().acquired is False

        cleanup_raises["value"] = False
        assert owner.release().released is True
        assert standby.ensure_control().acquired is True
    finally:
        cleanup_raises["value"] = False
        owner.shutdown()
        standby.shutdown()


def test_release_refuses_active_operation(unused_tcp_port):
    """Manual release should refuse while an owner-dependent operation runs."""
    manager = _configured_manager(unused_tcp_port)
    try:
        assert manager.ensure_control().acquired is True
        assert manager.begin_operation() is True

        busy = manager.release()
        assert busy.released is False
        assert "still running" in busy.error
        assert busy.control["active_operations"] == 1

        manager.end_operation()
        assert manager.release().released is True
    finally:
        manager.shutdown()


def test_release_cancels_cooperative_background_worker(
    monkeypatch,
    unused_tcp_port,
):
    """Cancellable owner services should not block an intentional handoff."""
    import MCP_Server.server as server_module

    stop_event = threading.Event()
    started = threading.Event()
    workers = []

    def background(stop):
        """Wait until backend teardown requests cancellation."""
        started.set()
        stop.wait(timeout=1.0)

    def start():
        """Start one representative owner background service."""
        worker = threading.Thread(
            target=server_module._run_control_background,
            args=(background, stop_event),
        )
        workers.append(worker)
        worker.start()

    def stop():
        """Cooperatively stop and join every background service."""
        stop_event.set()
        for worker in workers:
            worker.join(timeout=1.0)
        return all(not worker.is_alive() for worker in workers)

    manager = _configured_manager(unused_tcp_port, start=start, stop=stop)
    monkeypatch.setattr(
        server_module.ownership,
        "begin_operation",
        manager.begin_operation,
    )
    monkeypatch.setattr(server_module.ownership, "end_operation", manager.end_operation)
    try:
        assert manager.ensure_control().acquired is True
        assert started.wait(timeout=1.0)
        assert manager.status()["active_operations"] == 0

        released = manager.release()

        assert released.released is True
        assert stop_event.is_set()
        assert all(not worker.is_alive() for worker in workers)
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=1.0)
        manager.shutdown()


def test_release_retains_control_for_uncooperative_background_worker(
    monkeypatch,
    unused_tcp_port,
):
    """A service that ignores cancellation should retain ownership until stopped."""
    import MCP_Server.server as server_module

    stop_event = threading.Event()
    started = threading.Event()
    finish = threading.Event()
    workers = []

    def background(_stop):
        """Ignore cooperative cancellation until explicitly released by the test."""
        started.set()
        finish.wait(timeout=1.0)

    def start():
        """Start one simulated stuck owner background service."""
        worker = threading.Thread(
            target=server_module._run_control_background,
            args=(background, stop_event),
        )
        workers.append(worker)
        worker.start()

    def stop():
        """Request cancellation and report whether the worker actually stopped."""
        stop_event.set()
        for worker in workers:
            worker.join(timeout=0.01)
        return all(not worker.is_alive() for worker in workers)

    owner = _configured_manager(unused_tcp_port, start=start, stop=stop)
    standby = _configured_manager(unused_tcp_port)
    monkeypatch.setattr(
        server_module.ownership,
        "begin_operation",
        owner.begin_operation,
    )
    monkeypatch.setattr(server_module.ownership, "end_operation", owner.end_operation)
    try:
        assert owner.ensure_control().acquired is True
        assert started.wait(timeout=1.0)

        incomplete = owner.release()

        assert incomplete.released is False
        assert stop_event.is_set()
        assert owner.status()["control_role"] == "owner"
        assert standby.ensure_control().acquired is False

        finish.set()
        assert owner.release().released is True
        assert standby.ensure_control().acquired is True
    finally:
        stop_event.set()
        finish.set()
        for worker in workers:
            worker.join(timeout=1.0)
        owner.shutdown()
        standby.shutdown()


def test_shutdown_retains_control_until_active_operation_finishes(unused_tcp_port):
    """Forced shutdown must retain the port while timed-out tool work remains."""
    stops = []
    owner = _configured_manager(
        unused_tcp_port,
        stop=lambda: stops.append(True),
    )
    standby = _configured_manager(unused_tcp_port)
    try:
        assert owner.ensure_control().acquired is True
        assert owner.begin_operation() is True

        owner.shutdown()

        status = owner.status()
        assert status["control_role"] == "owner"
        assert status["active_operations"] == 1
        assert standby.ensure_control().acquired is False

        owner.end_operation()
        owner.shutdown()

        assert stops == [True, True]
        assert standby.ensure_control().acquired is True
    finally:
        owner.end_operation()
        owner.shutdown()
        standby.shutdown()


def test_unrelated_listener_is_reported_as_unknown(unused_tcp_port):
    """An unrelated process on the lock port should be classified as unknown."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", unused_tcp_port))
    listener.listen(1)
    manager = _configured_manager(unused_tcp_port)
    try:
        status = manager.status()
        claim = manager.ensure_control()

        assert status["control_role"] == "standby"
        assert status["control_availability"] == "occupied_unknown"
        assert status["owner"] is None
        assert claim.acquired is False
        assert "unknown process" in claim.error
    finally:
        listener.close()
        manager.shutdown()


def test_non_object_status_payload_is_reported_as_unknown(unused_tcp_port):
    """Valid non-object JSON must not be mistaken for bridge owner metadata."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", unused_tcp_port))
    listener.listen(1)

    def respond():
        """Return a syntactically valid but structurally invalid payload."""
        client, _address = listener.accept()
        try:
            client.sendall(b"[]\n")
        finally:
            client.close()

    responder = threading.Thread(target=respond)
    responder.start()
    manager = _configured_manager(unused_tcp_port)
    try:
        status = manager.status()
        assert status["control_role"] == "standby"
        assert status["control_availability"] == "occupied_unknown"
        assert status["owner"] is None
    finally:
        listener.close()
        responder.join(timeout=1.0)
        manager.shutdown()


def test_dashboard_shutdown_tolerates_unstarted_thread(monkeypatch):
    """A startup/shutdown race must not abort dashboard cleanup."""
    import MCP_Server.state as state
    from MCP_Server.dashboard.server import stop_dashboard_server

    class FakeServer:
        should_exit = False

    server = FakeServer()
    observed_stop = []
    pending = threading.Thread(
        target=lambda: observed_stop.append(server.should_exit),
        name="pending-dashboard",
    )
    monkeypatch.setattr(state, "dashboard_server", server)
    monkeypatch.setattr(state, "dashboard_thread", pending)

    assert stop_dashboard_server() is False

    assert server.should_exit is True
    assert state.dashboard_server is server
    assert state.dashboard_thread is pending

    # If start() wins after teardown, the stop signal is already visible and
    # the pending server can exit instead of becoming a leaked owner resource.
    pending.start()
    pending.join(timeout=1.0)
    assert observed_stop == [True]
    assert stop_dashboard_server() is True
    assert state.dashboard_server is None
    assert state.dashboard_thread is None


def test_dashboard_shutdown_retains_state_after_join_timeout(monkeypatch):
    """Dashboard state should remain published while its thread is alive."""
    import MCP_Server.state as state
    from MCP_Server.dashboard.server import stop_dashboard_server

    class FakeServer:
        should_exit = False

    class FakeThread:
        ident = 123
        name = "slow-dashboard"

        def __init__(self):
            """Start as a simulated live dashboard thread."""
            self.alive = True
            self.joined = False

        def join(self, timeout):
            """Record the bounded join without completing the thread."""
            assert timeout == 3.0
            self.joined = True

        def is_alive(self):
            """Return the test-controlled liveness state."""
            return self.alive

    server = FakeServer()
    thread = FakeThread()
    monkeypatch.setattr(state, "dashboard_server", server)
    monkeypatch.setattr(state, "dashboard_thread", thread)

    assert stop_dashboard_server() is False
    assert thread.joined is True
    assert state.dashboard_server is server
    assert state.dashboard_thread is thread

    thread.alive = False
    assert stop_dashboard_server() is True
    assert state.dashboard_server is None
    assert state.dashboard_thread is None


def test_backend_shutdown_continues_past_unstarted_thread(monkeypatch):
    """An unstarted worker must not prevent the remaining owner cleanup."""
    import MCP_Server.server as server_module
    import MCP_Server.state as state

    cleanup = []
    stop_event = threading.Event()
    pending = threading.Thread(
        target=lambda: cleanup.append(("pending", stop_event.is_set())),
        name="pending-control-worker",
    )

    class FakeConnection:
        def __init__(self, name):
            """Name a backend connection double for cleanup ordering."""
            self.name = name

        def disconnect(self):
            """Record disconnection during best-effort cleanup."""
            cleanup.append((self.name, True))

    connected = threading.Event()
    connected.set()
    monkeypatch.setattr(
        server_module,
        "stop_dashboard_server",
        lambda: cleanup.append(("dashboard", True)),
    )
    monkeypatch.setattr(state, "control_stop_event", stop_event)
    monkeypatch.setattr(state, "control_background_threads", [pending])
    monkeypatch.setattr(state, "ableton_connection", FakeConnection("ableton"))
    monkeypatch.setattr(state, "m4l_connection", FakeConnection("m4l"))
    monkeypatch.setattr(state, "ableton_connected_event", connected)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": True, "timestamp": 1.0})

    assert server_module._stop_control_backend() is False

    assert stop_event.is_set()
    assert cleanup == [
        ("dashboard", True),
        ("ableton", True),
        ("m4l", True),
    ]
    assert state.control_background_threads == [pending]
    assert state.control_stop_event is stop_event
    assert state.ableton_connection is None
    assert state.m4l_connection is None
    assert not state.ableton_connected_event.is_set()
    assert state.m4l_ping_cache == {"result": False, "timestamp": 0.0}

    # A delayed start still sees cancellation and does not restore resources.
    pending.start()
    pending.join(timeout=1.0)
    assert cleanup[-1] == ("pending", True)
    assert server_module._stop_control_backend() is True
    assert state.control_background_threads == []
    assert state.control_stop_event is None


def test_backend_shutdown_retains_live_worker_after_join_timeout(monkeypatch):
    """Backend state should retain a worker that survives its join timeout."""
    import MCP_Server.server as server_module
    import MCP_Server.state as state

    class FakeThread:
        ident = 456
        name = "slow-cache-worker"

        def __init__(self):
            """Start as a simulated live cache worker."""
            self.alive = True

        def join(self, timeout):
            """Accept the bounded join while remaining alive."""
            assert timeout == 3.0

        def is_alive(self):
            """Return the test-controlled liveness state."""
            return self.alive

    stop_event = threading.Event()
    worker = FakeThread()
    monkeypatch.setattr(server_module, "stop_dashboard_server", lambda: True)
    monkeypatch.setattr(state, "control_stop_event", stop_event)
    monkeypatch.setattr(state, "control_background_threads", [worker])
    monkeypatch.setattr(state, "ableton_connection", None)
    monkeypatch.setattr(state, "m4l_connection", None)
    monkeypatch.setattr(state, "ableton_connected_event", threading.Event())
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": True, "timestamp": 1.0})

    assert server_module._stop_control_backend() is False
    assert stop_event.is_set()
    assert state.control_background_threads == [worker]
    assert state.control_stop_event is stop_event

    worker.alive = False
    assert server_module._stop_control_backend() is True
    assert state.control_background_threads == []
    assert state.control_stop_event is None


@pytest.mark.asyncio
async def test_two_stdio_clients_keep_tools_while_control_is_owned(unused_tcp_port_factory):
    """Regression proof: a lock collision must not abort either MCP handshake."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    lock_port = unused_tcp_port_factory()
    dashboard_port = unused_tcp_port_factory()
    owner = _configured_manager(lock_port)
    try:
        assert owner.ensure_control(client_name="integration-owner").acquired is True

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["ABLETON_BRIDGE_LOCK_PORT"] = str(lock_port)
        env["ABLETON_BRIDGE_DASHBOARD_PORT"] = str(dashboard_port)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "MCP_Server.server"],
            cwd=root,
            env=env,
        )
        with tempfile.TemporaryFile(mode="w+") as errors:
            async with stdio_client(params, errlog=errors) as (read_a, write_a):
                async with ClientSession(read_a, write_a) as client_a:
                    await client_a.initialize()
                    async with stdio_client(params, errlog=errors) as (read_b, write_b):
                        async with ClientSession(read_b, write_b) as client_b:
                            await client_b.initialize()

                            tools_a = await client_a.list_tools()
                            tools_b = await client_b.list_tools()
                            names_a = {tool.name for tool in tools_a.tools}
                            names_b = {tool.name for tool in tools_b.tools}

                            assert names_a == names_b
                            assert len(names_a) > 300
                            assert "release_ableton_control" in names_a

                            status_result = await client_b.call_tool("get_server_capabilities", {})
                            status = json.loads(status_result.content[0].text)
                            assert status["control_role"] == "standby"
                            assert status["control_availability"] == "owned"
                            assert status["owner"]["client_name"] == "integration-owner"
                            assert status["ableton_connection_state"] == "owned_elsewhere"
                            assert status["ableton_connected"] is None
                            assert status["m4l_connection_state"] == "owned_elsewhere"
                            assert status["m4l_connected"] is None
                            assert status["m4l_sockets_ready"] is None

                            release_result = await client_b.call_tool("release_ableton_control", {})
                            release = json.loads(release_result.content[0].text)
                            assert release["status"] == "ok"
                            assert release["data"]["released"] is False
                            assert owner.status()["control_role"] == "owner"
    finally:
        owner.shutdown()


@pytest.mark.asyncio
async def test_stdio_status_reports_unstarted_backend_without_claiming(
    unused_tcp_port_factory,
):
    """A free standby should report null connections and leave control free."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    lock_port = unused_tcp_port_factory()
    dashboard_port = unused_tcp_port_factory()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["ABLETON_BRIDGE_LOCK_PORT"] = str(lock_port)
    env["ABLETON_BRIDGE_DASHBOARD_PORT"] = str(dashboard_port)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "MCP_Server.server"],
        cwd=root,
        env=env,
    )

    with tempfile.TemporaryFile(mode="w+") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                status_result = await client.call_tool("get_server_capabilities", {})
                status = json.loads(status_result.content[0].text)

                assert len(tools.tools) > 300
                assert status["control_role"] == "standby"
                assert status["control_availability"] == "available"
                assert status["ableton_connection_state"] == "not_started"
                assert status["ableton_connected"] is None
                assert status["m4l_connection_state"] == "not_started"
                assert status["m4l_connected"] is None
                assert status["m4l_sockets_ready"] is None

                contender = _configured_manager(lock_port)
                try:
                    assert contender.ensure_control().acquired is True
                finally:
                    contender.shutdown()
