import pytest
import json
import gzip
import os
import tempfile
import threading
from unittest.mock import patch, MagicMock
import MCP_Server.state as state
from MCP_Server.cache.browser import (
    build_device_uri_map, save_browser_cache_to_disk,
    load_browser_cache_from_disk, populate_browser_cache, resolve_device_uri,
    get_browser_cache,
)
from MCP_Server.connections.ableton import CommandCancelled


class TestBuildDeviceUriMap:
    def test_basic_mapping(self):
        """Test that loadable items get mapped by lowercase name."""
        items = [
            {"name": "Wavetable", "uri": "query:Instruments#Wavetable", "is_loadable": True, "category": "Instruments"},
            {"name": "EQ Eight", "uri": "query:AudioEffects#EQ Eight", "is_loadable": True, "category": "Audio Effects"},
        ]
        uri_map = build_device_uri_map(items)
        assert "wavetable" in uri_map
        assert "eq eight" in uri_map

    def test_non_loadable_excluded(self):
        """Non-loadable items should not be in the URI map."""
        items = [
            {"name": "Folder", "uri": "", "is_loadable": False, "category": "Instruments"},
        ]
        uri_map = build_device_uri_map(items)
        assert "folder" not in uri_map

    def test_category_priority(self):
        """Instruments should win over lower-priority categories for same name."""
        items = [
            {"name": "Reverb", "uri": "query:AudioEffects#Reverb", "is_loadable": True, "category": "Audio Effects"},
            {"name": "Reverb", "uri": "query:Instruments#Reverb", "is_loadable": True, "category": "Instruments"},
        ]
        uri_map = build_device_uri_map(items)
        assert uri_map["reverb"] == "query:Instruments#Reverb"


class TestBrowserCacheDiskPersistence:
    def test_save_and_load_roundtrip(self):
        """Test that cache survives a save/load cycle."""
        items = [
            {"name": "TestDevice", "uri": "query:test", "is_loadable": True, "category": "Instruments"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "cache.json.gz")
            with patch('MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH', cache_path):
                with patch('MCP_Server.cache.browser.BROWSER_DISK_CACHE_PATH_LEGACY', cache_path + ".legacy"):
                    with patch('MCP_Server.cache.browser.BROWSER_DISK_CACHE_DIR', tmpdir):
                        # Set up state
                        state.browser_cache_flat = items
                        state.browser_cache_by_category = {"Instruments": items}
                        state.device_uri_map = {"testdevice": "query:test"}
                        state.browser_cache_timestamp = __import__('time').time()
                        save_browser_cache_to_disk()
                        assert os.path.exists(cache_path)

                        # Clear state and reload
                        state.browser_cache_flat = []
                        state.browser_cache_by_category = {}
                        state.device_uri_map = {}
                        loaded = load_browser_cache_from_disk()
                        assert loaded is True
                        assert len(state.browser_cache_flat) == 1
                        assert state.browser_cache_flat[0]["name"] == "TestDevice"


class TestResolveDeviceUri:
    def test_direct_uri_passthrough(self):
        """If input looks like a URI, pass it through."""
        state.device_uri_map = {}
        state.browser_cache_ready.set()
        result = resolve_device_uri("query:Instruments#Wavetable")
        assert result == "query:Instruments#Wavetable"

    def test_name_resolution(self):
        """Test resolving a device name to URI."""
        state.device_uri_map = {"wavetable": "query:Instruments#Wavetable"}
        state.browser_cache_ready.set()
        result = resolve_device_uri("Wavetable")
        assert result == "query:Instruments#Wavetable"

    def test_unknown_name_returns_input(self):
        """Unknown name should return the input as-is."""
        state.device_uri_map = {}
        state.browser_cache_flat = []
        state.browser_cache_ready.set()
        result = resolve_device_uri("NonexistentDevice")
        assert result == "NonexistentDevice"


def test_live_scan_honors_shutdown_and_preserves_existing_cache(monkeypatch):
    """A cancelled live scan must preserve the last usable browser cache."""
    stop_event = threading.Event()
    existing = [{"name": "Existing"}]
    calls = []

    class FakeConnection:
        def __init__(self, **_kwargs):
            """Create a no-op connection double."""
            pass

        def connect(self):
            """Pretend the browser-scan connection succeeds."""
            return True

        def send_command(self, _command, _params, *, timeout, stop_event):
            """Cancel the scan during its first browser command."""
            calls.append((timeout, stop_event))
            stop_event.set()
            raise CommandCancelled("stopping")

        def disconnect(self):
            """Record cleanup of the scan connection."""
            calls.append("disconnect")

    monkeypatch.setattr(
        "MCP_Server.connections.ableton.AbletonConnection",
        FakeConnection,
    )
    monkeypatch.setattr(
        "MCP_Server.cache.browser.BROWSER_CATEGORIES",
        [("instruments", "Instruments")],
    )
    monkeypatch.setattr(state, "browser_cache_flat", existing)
    monkeypatch.setattr(state, "browser_cache_timestamp", 0.0)
    monkeypatch.setattr(state, "browser_cache_populating", False)

    assert populate_browser_cache(force=True, stop_event=stop_event) is False
    assert calls[0] == (60.0, stop_event)
    assert calls[-1] == "disconnect"
    assert state.browser_cache_flat is existing
    assert state.browser_cache_populating is False
