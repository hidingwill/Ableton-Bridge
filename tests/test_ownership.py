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
    manager = OwnershipManager(port, environment=environment)
    manager.configure_backend(start or (lambda: None), stop or (lambda: None))
    return manager


def test_one_owner_and_standby_metadata(unused_tcp_port):
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
    managers = [
        _configured_manager(unused_tcp_port),
        _configured_manager(unused_tcp_port),
    ]
    barrier = threading.Barrier(3)
    results = []

    def claim(manager):
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
    stopped = []

    def fail_start():
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


def test_release_refuses_active_operation(unused_tcp_port):
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


def test_unrelated_listener_is_reported_as_unknown(unused_tcp_port):
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


@pytest.mark.asyncio
async def test_two_stdio_clients_keep_tools_while_control_is_owned(unused_tcp_port_factory):
    """Regression proof: a lock collision must not abort either MCP handshake."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    lock_port = unused_tcp_port_factory()
    dashboard_port = unused_tcp_port_factory()
    owner = _configured_manager(lock_port)
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
    try:
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

                            release_result = await client_b.call_tool("release_ableton_control", {})
                            release = json.loads(release_result.content[0].text)
                            assert release["status"] == "ok"
                            assert release["data"]["released"] is False
                            assert owner.status()["control_role"] == "owner"
    finally:
        owner.shutdown()
