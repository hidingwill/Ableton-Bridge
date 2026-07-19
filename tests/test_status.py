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
    monkeypatch.setattr(connection_status.ownership, "is_configured", lambda: True)
    monkeypatch.setattr(connection_status.ownership, "begin_operation", lambda: False)
    monkeypatch.setattr(
        connection_status.ownership,
        "end_operation",
        lambda: pytest.fail("unacquired operation was ended"),
    )

    assert connection_status.get_m4l_status() == (True, True)
    m4l.ping.assert_not_called()


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
