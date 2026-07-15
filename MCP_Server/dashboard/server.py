"""Web status dashboard HTTP server for AbletonBridge.

Provides a lightweight Starlette/Uvicorn HTTP server that serves the
real-time status dashboard and a JSON API endpoint.  All mutable state
is accessed via ``MCP_Server.state``.
"""

import logging
import time
import threading
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

import MCP_Server.state as state
from MCP_Server.dashboard.html import DASHBOARD_HTML

logger = logging.getLogger("AbletonBridge")


# ---------------------------------------------------------------------------
# Dashboard log handler
# ---------------------------------------------------------------------------

class DashboardLogHandler(logging.Handler):
    """Captures log records into the dashboard ring buffer.

    Stores lightweight tuples (created_float, level_str, message_str) to
    avoid formatting timestamps on every log message.  Timestamps are
    formatted only when the dashboard is actually viewed.
    """

    def emit(self, record):
        try:
            with state.server_log_lock:
                state.server_log_buffer.append(
                    (record.created, record.levelname, record.getMessage())
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helper: summarize tool arguments for the dashboard log
# ---------------------------------------------------------------------------

def summarize_args(args: dict) -> str:
    """Create a short summary of tool arguments for the dashboard log."""
    if not args:
        return ""
    parts = []
    for k, v in list(args.items())[:3]:
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "..."
        parts.append(f"{k}={sv}")
    suffix = f" +{len(args)-3} more" if len(args) > 3 else ""
    return ", ".join(parts) + suffix


# ---------------------------------------------------------------------------
# Status data helpers
# ---------------------------------------------------------------------------

def get_server_version() -> str:
    """Get server version from package metadata, with fallback."""
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("ableton-bridge")
    except Exception:
        from MCP_Server import __version__
        return __version__


def get_m4l_status() -> tuple:
    """Return (sockets_ready, bridge_responding) with cached ping."""
    sockets_ready = bool(state.m4l_connection and state.m4l_connection._connected)
    if not sockets_ready:
        return False, False

    now = time.time()
    if now - state.m4l_ping_cache["timestamp"] < state.M4L_PING_CACHE_TTL:
        return sockets_ready, state.m4l_ping_cache["result"]

    try:
        result = state.m4l_connection.ping()
    except Exception as e:
        logger.debug("Dashboard M4L ping failed: %s", e)
        result = False

    state.m4l_ping_cache["result"] = result
    state.m4l_ping_cache["timestamp"] = now
    return sockets_ready, result


def build_status_json() -> dict:
    """Collect all dashboard status data into a JSON-serializable dict."""
    ableton_connected = False
    if state.ableton_connection and state.ableton_connection.sock:
        try:
            state.ableton_connection.sock.getpeername()
            ableton_connected = True
        except Exception:
            pass

    m4l_sockets_ready, m4l_connected = get_m4l_status()

    with state.tool_call_lock:
        recent = list(state.tool_call_log)
        total = sum(state.tool_call_counts.values())
        top_tools = sorted(state.tool_call_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    with state.server_log_lock:
        # Format timestamps from stored tuples (created_float, level, msg)
        server_logs = [
            {"ts": datetime.fromtimestamp(ts).strftime("%H:%M:%S"), "level": lvl, "msg": msg}
            for ts, lvl, msg in state.server_log_buffer
        ]

    # Dynamic tool count via the mcp instance stored in state
    mcp = state.mcp_instance
    tool_count = len(mcp._tool_manager._tools) if mcp and hasattr(mcp, '_tool_manager') else 331

    return {
        "version": get_server_version(),
        "uptime_seconds": round(time.time() - state.server_start_time, 1) if state.server_start_time else 0,
        "ableton_connected": ableton_connected,
        "m4l_connected": m4l_connected,
        "m4l_sockets_ready": m4l_sockets_ready,
        "store_counts": {
            "snapshots": len(state.snapshot_store),
            "macros": len(state.macro_store),
            "param_maps": len(state.param_map_store),
        },
        "total_tool_calls": total,
        "top_tools": top_tools,
        "recent_calls": recent,
        "server_logs": server_logs,
        "tool_count": tool_count,
    }


# ---------------------------------------------------------------------------
# Dashboard HTTP server lifecycle
# ---------------------------------------------------------------------------

def start_dashboard_server():
    """Start the dashboard HTTP server on a background thread."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route
    import uvicorn

    async def dashboard_page(request):
        return HTMLResponse(DASHBOARD_HTML)

    async def api_status(request):
        return JSONResponse(build_status_json())

    app = Starlette(routes=[
        Route("/", dashboard_page),
        Route("/api/status", api_status),
    ])

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=state.DASHBOARD_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    state.dashboard_server = server

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.close()
            if state.dashboard_server is server:
                state.dashboard_server = None
            if state.dashboard_thread is threading.current_thread():
                state.dashboard_thread = None

    thread = threading.Thread(target=_run, daemon=True, name="dashboard-http")
    state.dashboard_thread = thread
    thread.start()
    logger.info("Dashboard started at http://127.0.0.1:%d", state.DASHBOARD_PORT)


def stop_dashboard_server() -> bool:
    """Signal shutdown and report whether the dashboard thread has exited."""
    server = state.dashboard_server
    thread = state.dashboard_thread
    if server:
        server.should_exit = True
    stopped = True
    if thread is threading.current_thread():
        stopped = False
    elif thread:
        # Teardown may overlap the narrow window after the thread is
        # published to shared state but before start() runs.  join() raises
        # RuntimeError for an unstarted thread, so keep cleanup best-effort.
        if thread.ident is None:
            stopped = False
        else:
            try:
                thread.join(timeout=3.0)
            except RuntimeError as exc:
                logger.warning("Could not join dashboard thread: %s", exc)
                stopped = False
            else:
                stopped = not thread.is_alive()

    if not stopped:
        logger.warning(
            "Dashboard shutdown is incomplete; retaining server and thread state"
        )
        return False

    if state.dashboard_server is server:
        state.dashboard_server = None
    if state.dashboard_thread is thread:
        state.dashboard_thread = None
    if server or thread:
        logger.info("Dashboard server stopped")
    return True
