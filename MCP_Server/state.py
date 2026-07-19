"""Centralized global mutable state for AbletonBridge MCP server.

All runtime state that was previously scattered as module-level globals in
server.py is collected here so that every module can access it via a single
``import MCP_Server.state as state`` (or ``from MCP_Server import state``).

Variable names have **no** underscore prefix -- they are accessed as e.g.
``state.ableton_connection``, ``state.snapshot_store``, etc.
"""

import os
import threading
from collections import deque
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------
ableton_connection: Optional[Any] = None  # AbletonConnection | None
m4l_connection: Optional[Any] = None      # M4LConnection | None
m4l_connection_lock: threading.RLock = threading.RLock()
m4l_status_snapshot: tuple[bool, bool] = (False, False)

# ---------------------------------------------------------------------------
# Feature stores (in-memory, lost on restart)
# ---------------------------------------------------------------------------
snapshot_store: Dict[str, Dict[str, Any]] = {}
macro_store: Dict[str, Dict[str, Any]] = {}
param_map_store: Dict[str, Dict[str, Any]] = {}
effect_chain_store: Dict[str, Dict[str, Any]] = {}
store_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Dashboard / telemetry state
# ---------------------------------------------------------------------------
server_start_time: float = 0.0
tool_call_log: deque = deque(maxlen=500)
tool_call_counts: Dict[str, int] = {}
tool_call_lock: threading.Lock = threading.Lock()
dashboard_server: Optional[Any] = None  # uvicorn.Server | None
dashboard_thread: Optional[threading.Thread] = None
server_log_buffer: deque = deque(maxlen=1000)
server_log_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Browser cache
# ---------------------------------------------------------------------------
browser_cache_flat: List[Dict[str, Any]] = []           # flat list for fast substring search
browser_cache_by_category: Dict[str, List[Dict[str, Any]]] = {}  # display_name -> items
browser_cache_timestamp: float = 0.0
browser_cache_lock: threading.Lock = threading.Lock()
browser_cache_populating: bool = False                   # prevents duplicate scans
device_uri_map: Dict[str, str] = {}                      # lowercase name -> URI

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
browser_cache_ready: threading.Event = threading.Event()   # set when cache populated
ableton_connected_event: threading.Event = threading.Event()  # set on first connect

# ---------------------------------------------------------------------------
# M4L ping cache
# ---------------------------------------------------------------------------
m4l_ping_cache: Dict[str, Any] = {"result": False, "timestamp": 0.0}
M4L_PING_CACHE_TTL: float = 5.0

# ---------------------------------------------------------------------------
# M4L bridge version (populated after successful ping)
# ---------------------------------------------------------------------------
m4l_bridge_version: str = ""

# ---------------------------------------------------------------------------
# Config (from environment or defaults)
# ---------------------------------------------------------------------------
DASHBOARD_PORT: int = int(os.environ.get("ABLETON_BRIDGE_DASHBOARD_PORT", "9880"))
SINGLETON_LOCK_PORT: int = int(os.environ.get("ABLETON_BRIDGE_LOCK_PORT", "9881"))

# ---------------------------------------------------------------------------
# Control-owner backend lifecycle
# ---------------------------------------------------------------------------
control_stop_event: Optional[threading.Event] = None
control_background_threads: List[threading.Thread] = []

# ---------------------------------------------------------------------------
# MCP server instance (set by server.py after creating the FastMCP object)
# ---------------------------------------------------------------------------
mcp_instance: Optional[Any] = None  # FastMCP | None
