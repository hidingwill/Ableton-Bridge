import pytest
from unittest.mock import MagicMock, patch
import MCP_Server.state as state


@pytest.fixture
def mock_ableton():
    """Mock AbletonConnection that returns success responses."""
    conn = MagicMock()
    conn.sock = MagicMock()
    conn.send_command = MagicMock(return_value={"status": "success"})
    return conn


@pytest.fixture
def mock_m4l():
    """Mock M4LConnection that returns success responses."""
    conn = MagicMock()
    conn.send_sock = MagicMock()
    conn.recv_sock = MagicMock()
    conn.send_command = MagicMock(return_value={"status": "success", "result": {}})
    conn.send_command_with_retry = MagicMock(return_value={"status": "success", "result": {}})
    return conn


@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state between tests."""
    original_ableton = state.ableton_connection
    original_m4l = state.m4l_connection
    original_m4l_ping_cache = state.m4l_ping_cache.copy()
    original_m4l_status_snapshot = state.m4l_status_snapshot
    original_snapshots = state.snapshot_store.copy()
    original_macros = state.macro_store.copy()
    original_param_maps = state.param_map_store.copy()
    original_chains = state.effect_chain_store.copy()
    yield
    state.ableton_connection = original_ableton
    state.m4l_connection = original_m4l
    state.m4l_ping_cache = original_m4l_ping_cache
    state.m4l_status_snapshot = original_m4l_status_snapshot
    state.snapshot_store = original_snapshots
    state.macro_store = original_macros
    state.param_map_store = original_param_maps
    state.effect_chain_store = original_chains


@pytest.fixture
def patch_ableton(mock_ableton):
    """Patch the Ableton connection in state."""
    with patch.object(state, 'ableton_connection', mock_ableton):
        with patch('MCP_Server.connections.ableton.get_ableton_connection', return_value=mock_ableton):
            yield mock_ableton


@pytest.fixture
def patch_m4l(mock_m4l):
    """Patch the M4L connection in state."""
    with patch.object(state, 'm4l_connection', mock_m4l):
        with patch('MCP_Server.connections.m4l.get_m4l_connection', return_value=mock_m4l):
            yield mock_m4l
