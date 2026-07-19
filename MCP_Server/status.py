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
    connection = state.m4l_connection
    sockets_ready = bool(connection and connection._connected)
    if not sockets_ready:
        return False, False

    now = time.time()
    if now - state.m4l_ping_cache["timestamp"] < state.M4L_PING_CACHE_TTL:
        return sockets_ready, state.m4l_ping_cache["result"]

    # A live ping may reconnect its UDP sockets. Protect it like any other
    # owner-local operation so manual release cannot tear the backend down
    # underneath the status request. If release already started, report the
    # last cached result without touching M4L.
    track_operation = ownership.is_configured()
    if track_operation and not ownership.begin_operation():
        return sockets_ready, state.m4l_ping_cache["result"]

    try:
        connection = state.m4l_connection
        sockets_ready = bool(connection and connection._connected)
        if not sockets_ready:
            return False, False

        try:
            result = connection.ping()
        except Exception as exc:
            logger.debug("M4L status ping failed: %s", exc)
            result = False

        if state.m4l_connection is connection:
            state.m4l_ping_cache["result"] = result
            state.m4l_ping_cache["timestamp"] = time.time()
        return sockets_ready, result
    finally:
        if track_operation:
            ownership.end_operation()


def _ableton_socket_connected() -> bool:
    """Check the local Ableton socket without sending a protocol command."""
    connection = state.ableton_connection
    return bool(connection and connection.is_connected())
