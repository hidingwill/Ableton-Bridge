"""Ownership-aware connection status helpers for AbletonBridge."""

import logging
import time
from typing import Any

import MCP_Server.ownership as ownership
import MCP_Server.state as state


logger = logging.getLogger("AbletonBridge")


def build_connection_status(control: dict[str, Any]) -> dict[str, Any]:
    """Build truthful connection fields for one ownership snapshot.

    Only the owner process has a local backend to inspect. Standby processes
    therefore return ``None`` for connection booleans and use explicit states
    to distinguish an unstarted backend from unobservable remote ownership.
    """
    if control.get("control_role") != "owner":
        availability = control.get("control_availability")
        if availability == "available":
            connection_state = "not_started"
        elif availability == "owned":
            connection_state = "owned_elsewhere"
        else:
            connection_state = "unknown"

        return {
            "ableton_connection_state": connection_state,
            "ableton_connected": None,
            "m4l_connection_state": connection_state,
            "m4l_connected": None,
            "m4l_sockets_ready": None,
        }

    ableton_connected = _ableton_socket_connected()
    m4l_sockets_ready, m4l_connected = get_m4l_status()
    if m4l_connected:
        m4l_connection_state = "connected"
    elif m4l_sockets_ready:
        m4l_connection_state = "sockets_ready"
    else:
        m4l_connection_state = "disconnected"

    return {
        "ableton_connection_state": (
            "connected" if ableton_connected else "disconnected"
        ),
        "ableton_connected": ableton_connected,
        "m4l_connection_state": m4l_connection_state,
        "m4l_connected": m4l_connected,
        "m4l_sockets_ready": m4l_sockets_ready,
    }


def get_m4l_status() -> tuple[bool, bool]:
    """Return local M4L socket readiness and cached bridge responsiveness."""
    # A live ping may reconnect its UDP sockets. Protect it like any other
    # owner-local operation so manual release cannot tear the backend down
    # underneath the status request. The connection lock also makes a status
    # ping atomic with normal-tool connection replacement.
    track_operation = ownership.is_configured()
    if track_operation and not ownership.begin_operation():
        return _m4l_cached_snapshot()

    try:
        if not state.m4l_connection_lock.acquire(blocking=False):
            return _m4l_cached_snapshot()
        try:
            connection = state.m4l_connection
            sockets_ready = bool(connection and connection._connected)
            if not sockets_ready:
                state.m4l_status_snapshot = (False, False)
                return state.m4l_status_snapshot

            now = time.time()
            if now - state.m4l_ping_cache["timestamp"] < state.M4L_PING_CACHE_TTL:
                state.m4l_status_snapshot = (
                    sockets_ready,
                    bool(state.m4l_ping_cache["result"]),
                )
                return state.m4l_status_snapshot

            try:
                result = connection.ping()
            except Exception as exc:
                logger.debug("M4L status ping failed: %s", exc)
                result = False

            # ping() may reconnect its UDP sockets. Re-read readiness after the
            # probe so a failed reconnect cannot publish sockets_ready=True.
            sockets_ready = bool(
                state.m4l_connection is connection and connection._connected
            )
            connected = bool(result) if sockets_ready else False
            state.m4l_ping_cache["result"] = connected
            state.m4l_ping_cache["timestamp"] = time.time()
            state.m4l_status_snapshot = (sockets_ready, connected)
            return state.m4l_status_snapshot
        finally:
            state.m4l_connection_lock.release()
    finally:
        if track_operation:
            ownership.end_operation()


def _m4l_cached_snapshot() -> tuple[bool, bool]:
    """Return a non-mutating M4L snapshot while lifecycle state is busy."""
    return state.m4l_status_snapshot


def _ableton_socket_connected() -> bool:
    """Check the local Ableton socket without sending a protocol command."""
    connection = state.ableton_connection
    return bool(connection and connection.is_connected())
