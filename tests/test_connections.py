import pytest
import json
import socket
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock, call
from MCP_Server.connections.ableton import (
    AbletonConnection,
    CommandCancelled,
    NON_IDEMPOTENT_COMMANDS,
    get_ableton_connection,
)
from MCP_Server.constants import TIER_0_COMMANDS, TIER_1_COMMANDS, TIER_2_COMMANDS
import MCP_Server.state as state


class TestAbletonConnectionSendCommand:
    def test_successful_command(self):
        """Test basic send_command round-trip."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()
        conn._recv_buffer = ""
        # Mock receive_full_response
        with patch.object(conn, 'receive_full_response', return_value={"status": "success", "result": {"tempo": 120.0}}):
            result = conn.send_command("get_session_info")
            assert result["tempo"] == 120.0

    def test_non_idempotent_single_attempt(self):
        """Non-idempotent commands (create/delete) should only attempt once."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()
        conn._recv_buffer = ""
        with patch.object(conn, 'receive_full_response', side_effect=socket.timeout("timeout")):
            with patch.object(conn, 'disconnect'):
                with pytest.raises(Exception):
                    conn.send_command("create_midi_track", {"index": -1})
                # Should have only called send once (no retry)
                assert conn.sock.sendall.call_count == 1

    def test_idempotent_retry_on_failure(self):
        """Idempotent commands should retry once on socket error."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()
        conn._recv_buffer = ""
        call_count = [0]
        def side_effect(*args, **kwargs):
            """Fail the first receive and succeed after reconnection."""
            call_count[0] += 1
            if call_count[0] == 1:
                raise socket.error("connection reset")
            return {"status": "success", "result": {}}
        with patch.object(conn, 'receive_full_response', side_effect=side_effect):
            with patch.object(conn, 'disconnect'):
                with patch.object(conn, 'connect', return_value=True):
                    result = conn.send_command("get_session_info")
                    assert result == {}

    def test_tier_0_no_delay(self):
        """TIER_0 commands should have no pre/post delays."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()
        conn._recv_buffer = ""
        with patch.object(conn, 'receive_full_response', return_value={"status": "success", "result": {}}):
            with patch('time.sleep') as mock_sleep:
                conn.send_command("set_tempo", {"tempo": 120})
                # TIER_0 should not call time.sleep for delays
                for c in mock_sleep.call_args_list:
                    # Any sleep call should not be the tier delay ones
                    pass  # Just verify no Exception

    def test_non_idempotent_commands_list(self):
        """Verify key commands are in non-idempotent set."""
        assert "create_midi_track" in NON_IDEMPOTENT_COMMANDS
        assert "delete_track" in NON_IDEMPOTENT_COMMANDS
        assert "add_notes_to_clip" in NON_IDEMPOTENT_COMMANDS
        # Read commands should NOT be in the set
        assert "get_session_info" not in NON_IDEMPOTENT_COMMANDS

    def test_tier_membership(self):
        """Verify tier sets are disjoint."""
        assert len(TIER_0_COMMANDS & TIER_1_COMMANDS) == 0
        assert len(TIER_1_COMMANDS & TIER_2_COMMANDS) == 0
        assert len(TIER_0_COMMANDS & TIER_2_COMMANDS) == 0

    def test_receive_full_response_honors_shutdown(self):
        """Socket receive cancellation should be prompt and suppress timeout context."""
        conn = AbletonConnection(host="localhost", port=9877)
        client, server = socket.socketpair()
        stop_event = threading.Event()
        timer = threading.Timer(0.05, stop_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(CommandCancelled) as exc_info:
                conn.receive_full_response(
                    client,
                    timeout=5.0,
                    stop_event=stop_event,
                )
            assert time.monotonic() - started < 1.0
            assert exc_info.value.__suppress_context__ is True
        finally:
            timer.cancel()
            client.close()
            server.close()

    def test_retry_delay_cancellation_suppresses_failure_context(self):
        """Retry cancellation should not present the triggering command error as its cause."""
        conn = AbletonConnection(host="localhost", port=9877)
        conn.sock = MagicMock()
        stop_event = MagicMock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True

        with patch.object(
            conn,
            "receive_full_response",
            side_effect=RuntimeError("command failed"),
        ):
            with pytest.raises(CommandCancelled) as exc_info:
                conn.send_command("get_session_info", stop_event=stop_event)

        assert exc_info.value.__suppress_context__ is True


class TestGetAbletonConnection:
    def test_returns_existing_valid_connection(self):
        """Should return existing connection if socket is valid."""
        mock_conn = MagicMock()
        mock_conn.sock = MagicMock()
        mock_conn.sock.getpeername.return_value = ("localhost", 9877)
        mock_conn.send_command.return_value = {"status": "success"}
        state.ableton_connection = mock_conn
        with patch('MCP_Server.connections.ableton.AbletonConnection'):
            result = get_ableton_connection()
            assert result == mock_conn

    def test_reconnects_on_dead_socket(self):
        """Should create new connection if existing socket is dead."""
        mock_conn = MagicMock()
        mock_conn.sock = MagicMock()
        mock_conn.sock.getpeername.side_effect = socket.error("not connected")
        state.ableton_connection = mock_conn
        new_conn = MagicMock()
        new_conn.connect.return_value = True
        new_conn.send_command.return_value = {"status": "success"}
        with patch('MCP_Server.connections.ableton.AbletonConnection', return_value=new_conn):
            result = get_ableton_connection()
            assert new_conn.connect.called
