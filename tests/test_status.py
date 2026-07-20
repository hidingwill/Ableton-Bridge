"""Ownership-aware connection status tests."""

import json
import threading
from unittest.mock import MagicMock

import pytest

import MCP_Server.state as state
import MCP_Server.status as connection_status
from MCP_Server.ownership import OwnershipManager


def _control(role: str, availability: str) -> dict:
    """Build the ownership fields needed by the connection status helper."""
    return {
        "control_role": role,
        "control_availability": availability,
        "owner": None,
        "active_operations": 0,
    }


@pytest.mark.parametrize(
    ("availability", "expected_state"),
    [
        ("available", "not_started"),
        ("owned", "owned_elsewhere"),
        ("occupied_unknown", "unknown"),
    ],
)
def test_standby_connections_are_unknown_without_local_probes(
    monkeypatch,
    availability,
    expected_state,
):
    """Standby status should explain nulls without inspecting backend state."""
    monkeypatch.setattr(
        connection_status,
        "_ableton_socket_connected",
        lambda: pytest.fail("standby inspected the local Ableton socket"),
    )
    monkeypatch.setattr(
        connection_status,
        "get_m4l_status",
        lambda: pytest.fail("standby pinged the local M4L bridge"),
    )

    result = connection_status.build_connection_status(
        _control("standby", availability)
    )

    assert result == {
        "ableton_connection_state": expected_state,
        "ableton_connected": None,
        "m4l_connection_state": expected_state,
        "m4l_connected": None,
        "m4l_sockets_ready": None,
    }


def test_owner_reports_connected_backends(monkeypatch):
    """An owner should report verified local Ableton and M4L connections."""
    ableton = MagicMock()
    ableton.is_connected.return_value = True
    m4l = MagicMock(_connected=True)
    m4l.ping.return_value = True
    monkeypatch.setattr(state, "ableton_connection", ableton)
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})

    result = connection_status.build_connection_status(
        _control("owner", "owned")
    )

    assert result == {
        "ableton_connection_state": "connected",
        "ableton_connected": True,
        "m4l_connection_state": "connected",
        "m4l_connected": True,
        "m4l_sockets_ready": True,
    }
    m4l.ping.assert_called_once_with()


def test_owner_reports_dead_ableton_socket_and_missing_m4l(monkeypatch):
    """An owner should use false only after local backend inspection."""
    ableton = MagicMock()
    ableton.is_connected.return_value = False
    monkeypatch.setattr(state, "ableton_connection", ableton)
    monkeypatch.setattr(state, "m4l_connection", None)

    result = connection_status.build_connection_status(
        _control("owner", "owned")
    )

    assert result == {
        "ableton_connection_state": "disconnected",
        "ableton_connected": False,
        "m4l_connection_state": "disconnected",
        "m4l_connected": False,
        "m4l_sockets_ready": False,
    }


def test_owner_distinguishes_m4l_sockets_from_bridge_response(monkeypatch):
    """Bound M4L sockets without a ping response should remain distinguishable."""
    ableton = MagicMock()
    ableton.is_connected.return_value = True
    m4l = MagicMock(_connected=True)
    m4l.ping.return_value = False
    monkeypatch.setattr(state, "ableton_connection", ableton)
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})

    result = connection_status.build_connection_status(
        _control("owner", "owned")
    )

    assert result["ableton_connection_state"] == "connected"
    assert result["m4l_connection_state"] == "sockets_ready"
    assert result["m4l_connected"] is False
    assert result["m4l_sockets_ready"] is True


def test_m4l_status_rechecks_sockets_after_failed_ping_reconnect(monkeypatch):
    """A ping that loses its sockets must not preserve pre-ping readiness."""
    m4l = MagicMock(_connected=True)

    def lose_sockets():
        """Model send_command exhausting its reconnect attempt."""
        m4l._connected = False
        return False

    m4l.ping.side_effect = lose_sockets
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})
    monkeypatch.setattr(state, "m4l_status_snapshot", (True, False))
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: False)

    assert connection_status.get_m4l_status() == (False, False)
    assert state.m4l_status_snapshot == (False, False)


def test_live_m4l_status_probe_prevents_concurrent_release(
    monkeypatch,
    unused_tcp_port,
):
    """Release must not disconnect M4L while an owner status ping is active."""
    manager = OwnershipManager(unused_tcp_port)
    manager.configure_backend(lambda: None, lambda: True)
    started = threading.Event()
    finish = threading.Event()
    results = []
    m4l = MagicMock(_connected=True)

    def blocking_ping():
        """Hold the status probe until release has observed the operation."""
        started.set()
        finish.wait(timeout=1.0)
        return True

    m4l.ping.side_effect = blocking_ping
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})
    monkeypatch.setattr(connection_status.ownership, "is_configured", manager.is_configured)
    monkeypatch.setattr(connection_status.ownership, "begin_operation", manager.begin_operation)
    monkeypatch.setattr(connection_status.ownership, "end_operation", manager.end_operation)

    worker = threading.Thread(
        target=lambda: results.append(connection_status.get_m4l_status()),
    )
    try:
        assert manager.ensure_control().acquired is True
        worker.start()
        assert started.wait(timeout=1.0)

        release = manager.release()
        assert release.released is False
        assert release.control["active_operations"] == 1

        finish.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert results == [(True, True)]
        assert manager.release().released is True
    finally:
        finish.set()
        if worker.ident is not None:
            worker.join(timeout=1.0)
        manager.shutdown()


def test_m4l_status_does_not_ping_after_release_starts(monkeypatch):
    """A failed operation lease should fall back to the last cached result."""
    m4l = MagicMock(_connected=True)
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": True, "timestamp": 0.0})
    monkeypatch.setattr(state, "m4l_status_snapshot", (True, True))
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: True)
    monkeypatch.setattr(connection_status.ownership, "begin_operation", lambda: False)
    monkeypatch.setattr(
        connection_status.ownership,
        "end_operation",
        lambda: pytest.fail("unacquired operation was ended"),
    )

    assert connection_status.get_m4l_status() == (True, True)
    m4l.ping.assert_not_called()


def test_status_ping_serializes_m4l_connection_replacement(monkeypatch):
    """A status ping must finish before a normal tool replaces its connection."""
    import MCP_Server.connections.m4l as m4l_module

    ping_started = threading.Event()
    finish_ping = threading.Event()
    replacement_done = threading.Event()
    status_results = []
    replacement_results = []
    calls = []
    old = MagicMock(_connected=True)
    new = MagicMock(_connected=True)
    new.connect.return_value = True
    new.ping.return_value = True

    def old_ping():
        """Block only the status ping; the replacement verification then fails."""
        calls.append("ping")
        if len(calls) == 1:
            ping_started.set()
            finish_ping.wait(timeout=1.0)
        return False

    def replace_connection():
        """Run the normal connection replacement path in a competing thread."""
        try:
            replacement_results.append(m4l_module.get_m4l_connection())
        finally:
            replacement_done.set()

    old.ping.side_effect = old_ping
    monkeypatch.setattr(state, "m4l_connection", old)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: False)
    monkeypatch.setattr(m4l_module, "M4LConnection", lambda: new)

    status_worker = threading.Thread(
        target=lambda: status_results.append(connection_status.get_m4l_status()),
    )
    replacement_worker = threading.Thread(target=replace_connection)
    try:
        status_worker.start()
        assert ping_started.wait(timeout=1.0)

        replacement_worker.start()
        assert replacement_done.wait(timeout=0.05) is False
        assert state.m4l_connection is old

        finish_ping.set()
        status_worker.join(timeout=1.0)
        replacement_worker.join(timeout=1.0)

        assert not status_worker.is_alive()
        assert not replacement_worker.is_alive()
        assert status_results == [(True, False)]
        assert replacement_results == [new]
        assert state.m4l_connection is new
        assert old.disconnect.call_count == 1
        assert state.m4l_ping_cache["result"] is True
        assert state.m4l_ping_cache["timestamp"] > 0.0
    finally:
        finish_ping.set()
        if status_worker.ident is not None:
            status_worker.join(timeout=1.0)
        if replacement_worker.ident is not None:
            replacement_worker.join(timeout=1.0)


def test_m4l_status_uses_cache_while_connection_transaction_is_busy(monkeypatch):
    """Status should remain responsive instead of waiting for M4L replacement."""
    lock_held = threading.Event()
    release_lock = threading.Event()
    status_done = threading.Event()
    results = []
    m4l = MagicMock(_connected=True)
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": True, "timestamp": 0.0})
    monkeypatch.setattr(state, "m4l_status_snapshot", (True, True))
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: False)

    def hold_connection_transaction():
        """Keep the state lock busy until status has taken its fallback path."""
        with state.m4l_connection_lock:
            lock_held.set()
            release_lock.wait(timeout=1.0)

    def read_status():
        """Record completion without ever waiting for the held state lock."""
        try:
            results.append(connection_status.get_m4l_status())
        finally:
            status_done.set()

    holder = threading.Thread(target=hold_connection_transaction)
    reader = threading.Thread(target=read_status)
    try:
        holder.start()
        assert lock_held.wait(timeout=1.0)
        reader.start()

        assert status_done.wait(timeout=0.2)
        assert results == [(True, True)]
        m4l.ping.assert_not_called()
    finally:
        release_lock.set()
        if holder.ident is not None:
            holder.join(timeout=1.0)
        if reader.ident is not None:
            reader.join(timeout=1.0)


def test_m4l_status_fallback_never_combines_connection_generations(monkeypatch):
    """A busy transaction exposes one immutable snapshot, never hybrid state."""
    lock_held = threading.Event()
    release_lock = threading.Event()
    status_done = threading.Event()
    results = []
    new_connection = MagicMock(_connected=True)
    monkeypatch.setattr(state, "m4l_connection", None)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": True, "timestamp": 1.0})
    monkeypatch.setattr(state, "m4l_status_snapshot", (False, False))
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: False)

    def publish_partial_generation():
        """Pause on deliberately inconsistent raw fields inside the state lock."""
        with state.m4l_connection_lock:
            state.m4l_connection = new_connection
            lock_held.set()
            release_lock.wait(timeout=1.0)

    def read_status():
        """Read the last atomic snapshot without inspecting partial fields."""
        try:
            results.append(connection_status.get_m4l_status())
        finally:
            status_done.set()

    publisher = threading.Thread(target=publish_partial_generation)
    reader = threading.Thread(target=read_status)
    try:
        publisher.start()
        assert lock_held.wait(timeout=1.0)
        reader.start()

        assert status_done.wait(timeout=0.2)
        assert results == [(False, False)]
        new_connection.ping.assert_not_called()
    finally:
        release_lock.set()
        if publisher.ident is not None:
            publisher.join(timeout=1.0)
        if reader.ident is not None:
            reader.join(timeout=1.0)


@pytest.mark.asyncio
async def test_capabilities_tool_and_resource_share_standby_contract(monkeypatch):
    """Both public capability surfaces should expose identical tri-state fields."""
    from mcp.server.fastmcp import FastMCP
    import MCP_Server.server as server_module
    import MCP_Server.tools.session as session_tools

    control = _control("standby", "available")
    monkeypatch.setattr(session_tools.ownership, "get_status", lambda: control)
    monkeypatch.setattr(server_module.ownership, "get_status", lambda: control)

    mcp = FastMCP("status-test")
    session_tools.register_tools(mcp)
    tool = mcp._tool_manager._tools["get_server_capabilities"]
    tool_result = json.loads(await tool.fn(MagicMock()))
    resource_result = json.loads(server_module.resource_capabilities())

    expected = {
        "ableton_connection_state": "not_started",
        "ableton_connected": None,
        "m4l_connection_state": "not_started",
        "m4l_connected": None,
        "m4l_sockets_ready": None,
    }
    for key, value in expected.items():
        assert tool_result[key] == value
        assert resource_result[key] == value
    assert tool_result["features"]["m4l_bridge"] is None


def test_dashboard_status_keeps_owner_booleans(monkeypatch):
    """The owner-only dashboard should retain booleans and add explicit states."""
    import MCP_Server.dashboard.server as dashboard

    ableton = MagicMock()
    ableton.is_connected.return_value = True
    m4l = MagicMock(_connected=True)
    m4l.ping.return_value = True
    monkeypatch.setattr(dashboard.ownership, "get_status", lambda: _control("owner", "owned"))
    monkeypatch.setattr(state, "ableton_connection", ableton)
    monkeypatch.setattr(state, "m4l_connection", m4l)
    monkeypatch.setattr(state, "m4l_ping_cache", {"result": False, "timestamp": 0.0})

    result = dashboard.build_status_json()

    assert result["ableton_connection_state"] == "connected"
    assert result["ableton_connected"] is True
    assert result["m4l_connection_state"] == "connected"
    assert result["m4l_connected"] is True
    assert result["m4l_sockets_ready"] is True
