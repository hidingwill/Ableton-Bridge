"""Browser cache management for AbletonBridge.

Scans Ableton's browser tree and caches all items for instant search.
All mutable state is stored in ``MCP_Server.state`` -- this module only
contains the logic that reads / writes that state.
"""

import os
import json
import gzip
import time
import logging
import threading
from typing import Dict, Any, List, Optional
from collections import deque

import MCP_Server.state as state
from MCP_Server.constants import (
    CATEGORY_PRIORITY,
    BROWSER_CATEGORIES,
    BROWSER_CACHE_MAX_DEPTH,
    BROWSER_CACHE_MAX_ITEMS,
    BROWSER_CACHE_TTL,
    BROWSER_DISK_CACHE_DIR,
    BROWSER_DISK_CACHE_PATH,
    BROWSER_DISK_CACHE_PATH_LEGACY,
    BROWSER_DISK_CACHE_MAX_AGE,
)

logger = logging.getLogger("AbletonBridge")


# ---------------------------------------------------------------------------
# Device URI map builder
# ---------------------------------------------------------------------------

def build_device_uri_map(flat_items: List[Dict[str, Any]]) -> Dict[str, str]:
    """Build a lowercase-name -> URI lookup from the flat browser cache.

    Only includes loadable items with a non-empty URI.
    For duplicate names, prefers is_device=True items, then higher-priority
    categories (Instruments > Audio Effects > MIDI Effects > Sounds > Drums).
    """
    uri_map: Dict[str, str] = {}
    quality_map: Dict[str, tuple] = {}

    for item in flat_items:
        if not item.get("is_loadable") or not item.get("uri"):
            continue

        name_lower = item.get("search_name", item.get("name", "").lower())
        if not name_lower:
            continue

        is_device = item.get("is_device", False)
        cat_priority = CATEGORY_PRIORITY.get(item.get("category", ""), 99)
        new_quality = (is_device, -cat_priority)

        if name_lower not in uri_map or new_quality > quality_map[name_lower]:
            uri_map[name_lower] = item["uri"]
            quality_map[name_lower] = new_quality

    return uri_map


# ---------------------------------------------------------------------------
# Disk cache persistence
# ---------------------------------------------------------------------------

def save_browser_cache_to_disk() -> bool:
    """Persist the in-memory browser cache to a JSON file on disk."""
    try:
        with state.browser_cache_lock:
            if not state.browser_cache_flat:
                return False
            data = {
                "version": 1,
                "timestamp": state.browser_cache_timestamp,
                "flat": state.browser_cache_flat,
                "by_category": state.browser_cache_by_category,
                "device_uri_map": state.device_uri_map,
            }

        os.makedirs(BROWSER_DISK_CACHE_DIR, exist_ok=True)
        tmp_path = BROWSER_DISK_CACHE_PATH + ".tmp"
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, BROWSER_DISK_CACHE_PATH)
        # Remove legacy uncompressed cache if it exists
        if os.path.exists(BROWSER_DISK_CACHE_PATH_LEGACY):
            try:
                os.remove(BROWSER_DISK_CACHE_PATH_LEGACY)
            except OSError:
                pass
        logger.info("Browser cache saved to disk (%d items, gzip)", len(data["flat"]))
        return True
    except Exception as e:
        logger.warning("Failed to save browser cache to disk: %s", e)
        return False


def load_browser_cache_from_disk() -> bool:
    """Load browser cache from disk into the in-memory globals.

    Returns True if a valid, non-stale disk cache was loaded.
    """
    try:
        # Clean up stale .tmp file from a previous interrupted save
        tmp_path = BROWSER_DISK_CACHE_PATH + ".tmp"
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.info("Cleaned up stale browser cache .tmp file")
            except OSError:
                pass

        cache_path = None
        if os.path.exists(BROWSER_DISK_CACHE_PATH):
            cache_path = BROWSER_DISK_CACHE_PATH
        elif os.path.exists(BROWSER_DISK_CACHE_PATH_LEGACY):
            cache_path = BROWSER_DISK_CACHE_PATH_LEGACY
        if cache_path is None:
            logger.info("No disk cache found")
            return False

        opener = gzip.open if cache_path.endswith(".gz") else open
        with opener(cache_path, "rt", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict) or data.get("version") != 1:
            logger.warning("Disk cache has unknown format, ignoring")
            return False

        flat = data.get("flat", [])
        by_cat = data.get("by_category", {})
        uri_map = data.get("device_uri_map", {})
        disk_timestamp = data.get("timestamp", 0.0)

        if not flat:
            logger.info("Disk cache is empty, ignoring")
            return False

        age = time.time() - disk_timestamp
        if age > BROWSER_DISK_CACHE_MAX_AGE:
            logger.info("Disk cache is %.1f hours old (max %.1f), ignoring",
                        age / 3600, BROWSER_DISK_CACHE_MAX_AGE / 3600)
            return False

        with state.browser_cache_lock:
            state.browser_cache_flat = flat
            state.browser_cache_by_category = by_cat
            state.device_uri_map = uri_map
            state.browser_cache_timestamp = disk_timestamp

        state.browser_cache_ready.set()
        logger.info("Loaded browser cache from disk: %d items, %d categories, %d device URIs (%.1f min old)",
                    len(flat), len(by_cat), len(uri_map), age / 60)
        return True

    except Exception as e:
        logger.warning("Failed to load browser cache from disk: %s", e)
        return False


# ---------------------------------------------------------------------------
# Live browser scan
# ---------------------------------------------------------------------------

def populate_browser_cache(
    force: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    """Scan Ableton's browser tree and cache all items for instant search.

    Uses a breadth-first walk up to depth 3 across 11 browser categories.
    Each command is rate-limited (50ms gap) to avoid overwhelming Ableton's
    socket handler.  Items are capped at 1500 per category.

    Uses a **dedicated TCP connection** to avoid corrupting the shared global
    connection when the BFS scan sends many rapid commands.
    """
    from MCP_Server.connections.ableton import AbletonConnection, CommandCancelled

    now = time.time()
    with state.browser_cache_lock:
        if not force and state.browser_cache_flat and (now - state.browser_cache_timestamp) < BROWSER_CACHE_TTL:
            return True  # cache is still fresh
        if state.browser_cache_populating:
            return True  # another thread is already scanning
        state.browser_cache_populating = True

    # Use a dedicated connection so rapid BFS commands don't corrupt the
    # shared global socket (which other tools need concurrently).
    ableton = AbletonConnection(host="localhost", port=9877)

    try:
        if stop_event is not None and stop_event.is_set():
            return False
        try:
            if not ableton.connect():
                logger.warning("Browser cache: cannot connect to Ableton")
                return False
        except Exception as e:
            logger.warning("Browser cache: cannot connect to Ableton: %s", e)
            return False

        if stop_event is not None and stop_event.is_set():
            return False

        logger.info("Browser cache: starting scan...")
        flat_items: List[Dict[str, Any]] = []
        by_display: Dict[str, List[Dict[str, Any]]] = {}
        total = 0

        for path_root, display_name in BROWSER_CATEGORIES:
            if stop_event is not None and stop_event.is_set():
                return False
            category_items: List[Dict[str, Any]] = []
            cat_count = 0

            # BFS queue: (browser_path, depth)
            queue = deque([(path_root, 0)])

            while queue and cat_count < BROWSER_CACHE_MAX_ITEMS:
                if stop_event is not None and stop_event.is_set():
                    return False
                current_path, depth = queue.popleft()

                try:
                    result = ableton.send_command(
                        "get_browser_items_at_path",
                        {"path": current_path},
                        timeout=60.0,
                        stop_event=stop_event,
                    )
                except CommandCancelled:
                    logger.info("Browser cache scan cancelled during shutdown")
                    return False
                except Exception as e:
                    if stop_event is not None and stop_event.is_set():
                        return False
                    logger.warning("Browser cache: failed to read '%s': %s", current_path, e)
                    # Try to re-establish connection before continuing
                    if stop_event is not None:
                        if stop_event.wait(2.0):
                            return False
                    else:
                        time.sleep(2.0)
                    try:
                        ableton.disconnect()
                        if not ableton.connect():
                            logger.warning("Browser cache: lost connection, skipping '%s'", display_name)
                            break
                    except Exception:
                        logger.warning("Browser cache: lost connection, skipping '%s'", display_name)
                        break
                    continue

                if "error" in result:
                    continue

                for item in result.get("items", []):
                    if cat_count >= BROWSER_CACHE_MAX_ITEMS:
                        break

                    name = item.get("name", "")
                    if not name:
                        continue

                    item_path = f"{current_path}/{name}"
                    entry = {
                        "name": name,
                        "search_name": name.lower(),
                        "uri": item.get("uri", ""),
                        "is_loadable": item.get("is_loadable", False),
                        "is_folder": item.get("is_folder", False),
                        "is_device": item.get("is_device", False),
                        "category": display_name,
                        "path": item_path,
                    }
                    category_items.append(entry)
                    flat_items.append(entry)
                    cat_count += 1
                    total += 1

                    # Enqueue folders for deeper scanning
                    if item.get("is_folder", False) and depth < BROWSER_CACHE_MAX_DEPTH:
                        queue.append((item_path, depth + 1))

                # Rate-limit to avoid overwhelming Ableton's socket handler
                if stop_event is not None:
                    if stop_event.wait(0.01):
                        return False
                else:
                    time.sleep(0.01)

            by_display[display_name] = category_items
            logger.info("Browser cache: '%s' — %d items", display_name, len(category_items))

        if stop_event is not None and stop_event.is_set():
            return False

        device_map = build_device_uri_map(flat_items)
        with state.browser_cache_lock:
            state.browser_cache_flat = flat_items
            state.browser_cache_by_category = by_display
            state.device_uri_map = device_map
            state.browser_cache_timestamp = time.time()

        state.browser_cache_ready.set()
        logger.info("Browser cache: %d items, %d categories, %d device names mapped", total, len(by_display), len(device_map))
        save_browser_cache_to_disk()
        return True

    finally:
        with state.browser_cache_lock:
            state.browser_cache_populating = False
        # Always close the dedicated connection when done
        try:
            ableton.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# URI resolution helpers
# ---------------------------------------------------------------------------

def resolve_device_uri(uri_or_name: str) -> str:
    """Resolve a device name or URI to a loadable URI.

    If the input already looks like a URI (contains ':' or '#'), return as-is.
    Otherwise, look up the name in the dynamic device URI map built from
    the browser cache.  Waits for the warmup thread if the map is empty.
    """
    if ":" in uri_or_name or "#" in uri_or_name:
        return uri_or_name

    name_lower = uri_or_name.strip().lower()

    # Fast O(1) lookup in the dynamic device URI map
    with state.browser_cache_lock:
        resolved = state.device_uri_map.get(name_lower)
    if resolved:
        logger.info("Resolved device name '%s' to URI '%s'", uri_or_name, resolved)
        return resolved

    # Map is empty — wait (bounded) for warmup thread to populate it
    logger.info("Device map empty, waiting for browser cache warmup (max 5s)...")
    state.browser_cache_ready.wait(timeout=5.0)
    with state.browser_cache_lock:
        resolved = state.device_uri_map.get(name_lower)
    if resolved:
        logger.info("Resolved device name '%s' to URI '%s'", uri_or_name, resolved)
        return resolved

    # Fallback: linear scan for exact name match (take snapshot under lock)
    with state.browser_cache_lock:
        cache_snapshot = state.browser_cache_flat
    if cache_snapshot:
        logger.warning("Device '%s' not in URI map, falling back to O(n) scan of %d items", uri_or_name, len(cache_snapshot))
    for item in cache_snapshot:
        if item.get("search_name") == name_lower and item.get("is_loadable") and item.get("uri"):
            resolved = item["uri"]
            logger.info("Resolved device name '%s' via cache scan to URI '%s'", uri_or_name, resolved)
            return resolved

    logger.warning("Could not resolve '%s' to a known URI, passing through as-is", uri_or_name)
    return uri_or_name


def resolve_sample_uri(uri_or_name: str) -> str:
    """Resolve a sample filename, query:UserLibrary URI, or LOM URI.

    Handles three input formats:
    1. ``query:UserLibrary#subfolder:filename.mp3`` — extracts filename, searches cache/live
    2. Real LOM URI (contains ':' but not 'query:') — returned as-is
    3. Plain filename or substring — searched in cache then live User Library
    """
    from MCP_Server.connections.ableton import get_ableton_connection

    filename: str = ""  # set when parsing query: format

    # --- Handle query:UserLibrary#subfolder:filename format ---
    if uri_or_name.startswith("query:"):
        # "query:UserLibrary#eleven_labs_audio:filename.mp3" → filename = "filename.mp3"
        parts = uri_or_name.split(":")
        filename = parts[-1].strip() if len(parts) >= 3 else ""
        if filename:
            filename_lower = filename.lower()
            with state.browser_cache_lock:
                snapshot = list(state.browser_cache_flat)
            # exact name match
            for item in snapshot:
                if item.get("search_name") == filename_lower and item.get("uri"):
                    logger.info("Resolved query URI '%s' to '%s'", uri_or_name, item["uri"])
                    return item["uri"]
            # substring fallback
            for item in snapshot:
                if filename_lower in item.get("search_name", "") and item.get("uri"):
                    logger.info("Resolved query URI '%s' to '%s' (substring)", uri_or_name, item["uri"])
                    return item["uri"]
        # Not in cache — fall through to live lookup below

    # --- Already a real LOM URI (has ":" but not "query:") ---
    if (":" in uri_or_name or "#" in uri_or_name) and not uri_or_name.startswith("query:"):
        return uri_or_name

    # --- Plain filename: search cache ---
    name_lower = (filename or uri_or_name).strip().lower()
    with state.browser_cache_lock:
        snapshot = list(state.browser_cache_flat)
    # exact match
    for item in snapshot:
        if item.get("search_name") == name_lower and item.get("is_loadable") and item.get("uri"):
            logger.info("Resolved sample name '%s' to URI '%s'", uri_or_name, item["uri"])
            return item["uri"]
    # substring match
    for item in snapshot:
        sn = item.get("search_name", "")
        if name_lower in sn and item.get("is_loadable") and item.get("uri"):
            logger.info("Resolved sample name '%s' to URI '%s' (substring)", uri_or_name, item["uri"])
            return item["uri"]

    # --- Cache miss: live lookup of user_library subfolders ---
    _MAX_LIVE_LOOKUP_FOLDERS = 10
    try:
        logger.info("Sample '%s' not in cache, trying live User Library lookup", uri_or_name)
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path",
                                      {"path": "user_library"}, timeout=10.0)
        folder_count = 0
        for sub in result.get("items", []):
            if not sub.get("is_folder"):
                # Check non-folder items at root level too
                item_name = sub.get("name", "").lower()
                if name_lower in item_name and sub.get("uri"):
                    logger.info("Resolved sample '%s' via live lookup to '%s'",
                                uri_or_name, sub["uri"])
                    return sub["uri"]
                continue
            if folder_count >= _MAX_LIVE_LOOKUP_FOLDERS:
                break
            folder_count += 1
            time.sleep(0.05)
            sub_result = ableton.send_command(
                "get_browser_items_at_path",
                {"path": "user_library/" + sub["name"]},
                timeout=10.0,
            )
            for item in sub_result.get("items", []):
                if item.get("is_folder"):
                    continue
                item_name = item.get("name", "").lower()
                if name_lower in item_name and item.get("uri"):
                    logger.info("Resolved sample '%s' via live lookup to '%s'",
                                uri_or_name, item["uri"])
                    return item["uri"]
    except Exception as exc:
        logger.warning("Live User Library lookup failed: %s", exc)

    logger.warning("Could not resolve sample '%s' to a known URI, passing through as-is", uri_or_name)
    return uri_or_name


# ---------------------------------------------------------------------------
# Cache accessor
# ---------------------------------------------------------------------------

def get_browser_cache() -> List[Dict[str, Any]]:
    """Get the flat browser cache. Use refresh_browser_cache to force a rescan."""
    with state.browser_cache_lock:
        return state.browser_cache_flat
