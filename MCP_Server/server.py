"""AbletonBridge MCP Server — main entry point.

This is the orchestrator that wires together all modules.
Tool handlers live in MCP_Server/tools/*.py
Connection classes live in MCP_Server/connections/*.py
Cache logic lives in MCP_Server/cache/*.py
Dashboard lives in MCP_Server/dashboard/*.py
All mutable runtime state lives in MCP_Server/state.py
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import asyncio
import concurrent.futures
import logging
import time
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# MCP framework
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Internal modules
# ---------------------------------------------------------------------------
import MCP_Server.state as state
import MCP_Server.ownership as ownership
from MCP_Server.status import build_connection_status
from MCP_Server.connections.ableton import get_ableton_connection
from MCP_Server.connections.m4l import M4LConnection
from MCP_Server.cache.browser import load_browser_cache_from_disk, populate_browser_cache
from MCP_Server.dashboard.server import (
    start_dashboard_server,
    stop_dashboard_server,
    DashboardLogHandler,
    summarize_args,
)
from MCP_Server.tools import register_all_tools

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("AbletonBridge")


# ===================================================================
# M4L auto-connect (background thread)
# ===================================================================

def _m4l_auto_connect(stop_event: threading.Event):
    """Background thread: create UDP sockets once, retry ping until M4L responds."""
    if stop_event.is_set():
        return

    # Create sockets once — don't tear them down between retries
    conn = M4LConnection()
    if not conn.connect():
        logger.warning("M4L auto-connect: could not bind UDP sockets")
        return

    if stop_event.is_set():
        conn.disconnect()
        return

    state.m4l_connection = conn

    # Build a raw OSC ping packet
    ping_id = "autocon"
    ping_osc = M4LConnection._build_osc_message("/ping", [("s", ping_id)])

    for attempt in range(1, 16):  # 15 attempts, ~2 s apart
        if stop_event.is_set():
            return
        try:
            # Drain stale data
            conn._drain_recv_socket()
            conn.recv_sock.settimeout(2.0)

            # Send ping
            conn.send_sock.sendto(ping_osc, (conn.send_host, conn.send_port))

            # Wait for response
            data, _addr = conn.recv_sock.recvfrom(65535)
            result = conn._parse_m4l_response(data)
            if result.get("status") == "success":
                logger.info("M4L bridge auto-connected on attempt %d", attempt)
                state.m4l_ping_cache["result"] = True
                state.m4l_ping_cache["timestamp"] = time.time()
                # Check bridge version compatibility
                M4LConnection._check_bridge_version(result)
                return
        except TimeoutError:
            logger.info(
                "M4L auto-connect %d/15: no response (timeout), retrying...",
                attempt,
            )
        except Exception as e:
            if stop_event.is_set():
                return
            logger.info("M4L auto-connect %d/15: %s", attempt, e)
        if stop_event.wait(2.0):
            return

    logger.warning(
        "M4L bridge not available after 15 attempts — will retry when needed"
    )


# ===================================================================
# Browser cache warmup (background thread)
# ===================================================================

def _browser_cache_warmup(stop_event: threading.Event):
    """Background thread: load disk cache instantly, then refresh from Ableton."""
    from MCP_Server.constants import BROWSER_DISK_CACHE_MAX_AGE

    # Step 1: Load from disk (instant, works even before Ableton connects)
    disk_loaded = load_browser_cache_from_disk()
    if disk_loaded:
        age = time.time() - state.browser_cache_timestamp
        if age < BROWSER_DISK_CACHE_MAX_AGE:
            logger.info(
                "Browser cache ready from disk (%.0fs old, skipping rescan)", age
            )
            return
        logger.info(
            "Browser cache loaded from disk (%.0fs old, will refresh)", age
        )

    # Step 2: Wait for Ableton connection, then do a live scan to refresh
    deadline = time.monotonic() + 30.0
    while not state.ableton_connected_event.is_set():
        if stop_event.wait(0.1) or time.monotonic() >= deadline:
            return
    if not (state.ableton_connection and state.ableton_connection.sock):
        logger.warning(
            "Browser cache warmup: Ableton not connected after 30s, skipping live scan"
        )
        return
    if stop_event.wait(0.5):  # brief settle after connection confirmed
        return
    try:
        populate_browser_cache(stop_event=stop_event)
    except Exception as e:
        logger.warning("Browser cache warmup failed: %s", e)


# ===================================================================
# Control-owner backend lifecycle
# ===================================================================

def _run_control_background(target, stop_event: threading.Event):
    """Run cancellable owner-only background work."""
    target(stop_event)


def _start_control_backend():
    """Start resources that must exist in exactly one MCP process."""
    logger.info("Starting Ableton control backend")
    stop_event = threading.Event()
    state.control_stop_event = stop_event
    state.control_background_threads = []
    state.ableton_connected_event.clear()

    # Live connectivity is required for a successful ownership claim.
    get_ableton_connection()

    try:
        start_dashboard_server()
    except Exception as e:
        logger.warning("Dashboard failed to start: %s", e)

    for target, name in (
        (_m4l_auto_connect, "m4l-auto-connect"),
        (_browser_cache_warmup, "browser-cache-warmup"),
    ):
        thread = threading.Thread(
            target=_run_control_background,
            args=(target, stop_event),
            daemon=True,
            name=name,
        )
        state.control_background_threads.append(thread)
        thread.start()


def _stop_control_backend() -> bool:
    """Stop owner-only resources and report when handoff is safe."""
    cleanup_complete = True
    stop_event = state.control_stop_event
    if stop_event is not None:
        stop_event.set()

    try:
        dashboard_stopped = stop_dashboard_server()
    except Exception as exc:
        dashboard_stopped = False
        logger.warning("Dashboard shutdown failed during release: %s", exc)
    if dashboard_stopped is False:
        cleanup_complete = False

    ableton_connection = state.ableton_connection
    if ableton_connection:
        logger.info("Disconnecting from Ableton")
        try:
            ableton_connection.disconnect()
        except Exception as exc:
            cleanup_complete = False
            logger.warning("Ableton disconnect failed during release: %s", exc)
        else:
            if state.ableton_connection is ableton_connection:
                state.ableton_connection = None

    m4l_connection = state.m4l_connection
    if m4l_connection:
        logger.info("Disconnecting M4L bridge")
        try:
            m4l_connection.disconnect()
        except Exception as exc:
            cleanup_complete = False
            logger.warning("M4L disconnect failed during release: %s", exc)
        else:
            if state.m4l_connection is m4l_connection:
                state.m4l_connection = None

    remaining_threads = []
    for thread in list(state.control_background_threads):
        stopped = False
        if thread is not threading.current_thread() and thread.ident is not None:
            try:
                thread.join(timeout=3.0)
            except RuntimeError as exc:
                logger.warning(
                    "Could not join control background thread %s: %s",
                    thread.name,
                    exc,
                )
            else:
                stopped = not thread.is_alive()
        if not stopped:
            cleanup_complete = False
            remaining_threads.append(thread)
            logger.warning(
                "Control background thread %s is still stopping",
                thread.name,
            )

    state.control_background_threads = remaining_threads
    if cleanup_complete:
        state.control_stop_event = None
    state.ableton_connected_event.clear()
    state.m4l_ping_cache = {"result": False, "timestamp": 0.0}
    return cleanup_complete


# ===================================================================
# Server lifespan — MCP availability is independent of backend ownership
# ===================================================================

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle."""
    try:
        logger.info("AbletonBridge server starting up")
        state.server_start_time = time.time()

        # Bound the thread pool used by asyncio.to_thread() to prevent
        # excessive thread creation. With the tool semaphore limiting
        # concurrent TCP operations to 1, most workers stay idle; 8 provides
        # headroom for background tasks (browser cache, M4L, dashboard).
        loop = asyncio.get_event_loop()
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=8)
        )

        ownership.configure_backend(_start_control_backend, _stop_control_backend)

        # Load saved effect chain templates from disk
        try:
            from MCP_Server.tools.workflows import load_chain_templates_from_disk
            load_chain_templates_from_disk()
        except Exception as e:
            logger.warning("Could not load chain templates: %s", e)

        yield {}

    finally:
        ownership.shutdown()
        ownership.unconfigure_backend()
        logger.info("AbletonBridge server shut down")


# ===================================================================
# Create the MCP server instance
# ===================================================================

from MCP_Server.instructions import SERVER_INSTRUCTIONS

mcp = FastMCP("AbletonBridge", instructions=SERVER_INSTRUCTIONS, lifespan=server_lifespan)
state.mcp_instance = mcp


# ===================================================================
# Register all tool modules
# ===================================================================

register_all_tools(mcp)


# ===================================================================
# Register MCP prompts
# ===================================================================

from MCP_Server.prompts import register_prompts
register_prompts(mcp)


# ===================================================================
# MCP Resources — expose live session data via resource URIs
# ===================================================================

@mcp.resource("ableton://session")
def resource_session() -> str:
    """Current Ableton session info (tempo, tracks, transport state)."""
    return _run_controlled_resource("get_session_info")


@mcp.resource("ableton://tracks")
def resource_tracks() -> str:
    """All track information including devices, clips, and routing."""
    return _run_controlled_resource("get_all_tracks_info")


def _run_controlled_resource(command: str) -> str:
    """Run an Ableton resource read under the same ownership contract as tools."""
    import json

    claim = ownership.ensure_control()
    if not claim.acquired:
        return json.dumps({
            "status": "error",
            "message": claim.error,
            "data": {"control": claim.control},
        })
    if not ownership.begin_operation():
        return json.dumps({
            "status": "error",
            "message": "Ableton control was released before the resource read began.",
            "data": {"control": ownership.get_status()},
        })
    try:
        ableton = get_ableton_connection()
        return json.dumps(ableton.send_command(command))
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        ownership.end_operation()


@mcp.resource("ableton://capabilities")
def resource_capabilities() -> str:
    """Return version, ownership, and role-aware connection status."""
    import json
    from MCP_Server import __version__
    control = ownership.get_status()
    result = {
        "server_version": __version__,
        **control,
        **build_connection_status(control),
        "m4l_bridge_version": state.m4l_bridge_version or "unknown",
        "browser_cache_ready": state.browser_cache_ready.is_set(),
        "browser_cache_items": len(state.browser_cache_flat),
    }
    return json.dumps(result)


# ===================================================================
# Tool call instrumentation — captures every tool call for the dashboard
# ===================================================================

_original_call_tool = mcp.call_tool


async def _instrumented_call_tool(name: str, arguments: dict) -> Any:
    """Wrap every tool call to record metrics for the dashboard."""
    start = time.time()
    error_msg = None
    try:
        result = await _original_call_tool(name, arguments)
        return result
    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        duration = time.time() - start
        entry = {
            "tool": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round(duration * 1000, 1),
            "error": error_msg,
            "args_summary": summarize_args(arguments),
        }
        with state.tool_call_lock:
            state.tool_call_log.append(entry)
            state.tool_call_counts[name] = state.tool_call_counts.get(name, 0) + 1


mcp.call_tool = _instrumented_call_tool


# ===================================================================
# Dashboard log handler — pipe all log records to the dashboard buffer
# ===================================================================

logging.getLogger().addHandler(DashboardLogHandler())


# ===================================================================
# Entry point
# ===================================================================

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
