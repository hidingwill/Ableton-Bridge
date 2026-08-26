"""Browser: tree, items at path, find by URI, load instrument/effect, search."""

from __future__ import absolute_import, print_function, unicode_literals

import traceback

from ._helpers import get_track


_BROWSER_ROOTS = (
    "instruments", "sounds", "drums", "audio_effects", "midi_effects",
    "user_library", "user_folders", "samples", "packs", "current_project",
    "max_for_live", "plugins",
)

_MAX_CHILDREN = 200  # cap per-folder iteration to avoid hanging on huge directories
_MAX_SEARCH_RESULTS = 50  # stop traversal once we have enough matches


def _iter_children(item, max_items=_MAX_CHILDREN):
    """Yield children of a BrowserItem or elements of a list-valued root.

    Handles both real BrowserItem objects (with .children) and plain lists
    (e.g. user_folders).  Caps iteration at *max_items*.
    """
    if isinstance(item, (list, tuple)):
        for i, child in enumerate(item):
            if i >= max_items:
                break
            yield child
    elif hasattr(item, "children"):
        count = 0
        for child in item.children:
            if count >= max_items:
                break
            count += 1
            yield child


def find_browser_item_by_uri(browser_or_item, uri, max_depth=10, current_depth=0, ctrl=None):
    """Find a browser item by its URI (recursive search across all categories)."""
    try:
        if hasattr(browser_or_item, "uri") and browser_or_item.uri == uri:
            return browser_or_item
        if current_depth >= max_depth:
            return None
        # Top-level Browser object — iterate all root categories
        if hasattr(browser_or_item, "instruments"):
            for attr in _BROWSER_ROOTS:
                root = getattr(browser_or_item, attr, None)
                if root is None:
                    continue
                # user_folders is a list, not a single BrowserItem
                if attr == "user_folders":
                    try:
                        for folder in root:
                            item = find_browser_item_by_uri(
                                folder, uri, max_depth, current_depth + 1, ctrl)
                            if item:
                                return item
                    except Exception:
                        pass
                    continue
                item = find_browser_item_by_uri(
                    root, uri, max_depth, current_depth + 1, ctrl)
                if item:
                    return item
            return None
        if hasattr(browser_or_item, "children") and browser_or_item.children:
            count = 0
            for child in browser_or_item.children:
                if count >= _MAX_CHILDREN:
                    break
                count += 1
                item = find_browser_item_by_uri(child, uri, max_depth, current_depth + 1, ctrl)
                if item:
                    return item
        return None
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error finding browser item by URI: {0}".format(str(e)))
        return None


def get_browser_item(song, uri, path, ctrl=None):
    """Get a browser item by URI or path."""
    try:
        if ctrl is None:
            raise RuntimeError("get_browser_item requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")

        result = {"uri": uri, "path": path, "found": False}

        if uri:
            item = find_browser_item_by_uri(app.browser, uri, ctrl=ctrl)
            if item:
                result["found"] = True
                result["item"] = {
                    "name": item.name,
                    "is_folder": item.is_folder if hasattr(item, 'is_folder') else False,
                    "is_device": item.is_device if hasattr(item, 'is_device') else False,
                    "is_loadable": item.is_loadable if hasattr(item, 'is_loadable') else False,
                    "uri": item.uri,
                }
                return result

        if path:
            path_parts = path.split("/")
            root = path_parts[0].lower()
            current_item = None
            for attr in _BROWSER_ROOTS:
                if attr == root and hasattr(app.browser, attr):
                    current_item = getattr(app.browser, attr)
                    break
            if current_item is None:
                msg = "Unrecognized browser root '{0}'. Valid roots: {1}".format(
                    root, ", ".join("'{0}'".format(r) for r in _BROWSER_ROOTS))
                if ctrl:
                    ctrl.log_message(msg)
                result["error"] = msg
                return result

            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:
                    continue
                found = False
                for child in _iter_children(current_item):
                    if hasattr(child, "name") and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                if not found:
                    result["error"] = "Path part '{0}' not found".format(part)
                    return result

            result["found"] = True
            if isinstance(current_item, (list, tuple)):
                result["item"] = {
                    "name": root,
                    "is_folder": True,
                    "is_device": False,
                    "is_loadable": False,
                    "uri": None,
                }
            else:
                result["item"] = {
                    "name": getattr(current_item, "name", "Unknown"),
                    "is_folder": getattr(current_item, "is_folder", False),
                    "is_device": getattr(current_item, "is_device", False),
                    "is_loadable": getattr(current_item, "is_loadable", False),
                    "uri": getattr(current_item, "uri", None),
                }

        return result
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error getting browser item: " + str(e))
            ctrl.log_message(traceback.format_exc())
        raise


def load_browser_item(song, track_index, item_uri, ctrl=None, track_type="track"):
    """Load a browser item onto a track by URI. track_type: "track" | "return" | "master"."""
    try:
        track = get_track(song, track_index, track_type)
        if ctrl is None:
            raise RuntimeError("load_browser_item requires ctrl for application()")
        app = ctrl.application()

        item = find_browser_item_by_uri(app.browser, item_uri, ctrl=ctrl)
        if not item:
            raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
        if hasattr(item, 'is_loadable') and not item.is_loadable:
            raise ValueError(
                "Browser item '{0}' (URI: {1}) is not loadable".format(item.name, item_uri))

        song.view.selected_track = track
        app.browser.load_item(item)

        return {
            "loaded": True,
            "item_name": item.name,
            "track_name": track.name,
            "uri": item_uri,
        }
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error loading browser item: {0}".format(str(e)))
            ctrl.log_message(traceback.format_exc())
        raise


def load_instrument_or_effect(song, track_index, uri, ctrl=None):
    """Load an instrument or effect onto a track by URI (alias)."""
    return load_browser_item(song, track_index, uri, ctrl)


def _find_browser_item_by_name(browser, name, ctrl=None):
    """Find a loadable browser item by name in user_library and related roots."""
    name_lower = name.lower()
    # Strip .mp3/.wav/.aif extension for flexible matching
    name_stem = name_lower.rsplit(".", 1)[0] if "." in name_lower else name_lower

    def _search(parent, depth=0, max_depth=5):
        if depth >= max_depth:
            return None
        try:
            children = parent.children
        except Exception:
            return None
        if not children:
            return None
        count = 0
        for child in children:
            if count >= _MAX_CHILDREN:
                break
            count += 1
            is_folder = hasattr(child, "is_folder") and child.is_folder
            if is_folder:
                found = _search(child, depth + 1, max_depth)
                if found:
                    return found
                continue
            child_name = getattr(child, "name", "").lower()
            if child_name == name_lower or child_name == name_stem:
                if not hasattr(child, "is_loadable") or child.is_loadable:
                    return child
        return None

    # Search user_library first (most likely location for samples)
    user_lib = getattr(browser, "user_library", None)
    if user_lib:
        found = _search(user_lib)
        if found:
            if ctrl:
                ctrl.log_message("Found '{0}' by name in user_library".format(name))
            return found

    # Also try user_folders
    user_folders = getattr(browser, "user_folders", None)
    if user_folders:
        try:
            for folder in user_folders:
                found = _search(folder)
                if found:
                    if ctrl:
                        ctrl.log_message("Found '{0}' by name in user_folders".format(name))
                    return found
        except Exception:
            pass

    # Try current_project and samples
    for attr in ("current_project", "samples"):
        root = getattr(browser, attr, None)
        if root:
            found = _search(root)
            if found:
                if ctrl:
                    ctrl.log_message("Found '{0}' by name in {1}".format(name, attr))
                return found

    return None


def load_sample(song, track_index, sample_uri, ctrl=None):
    """Load a sample onto a track by URI or filename (with name-based fallback)."""
    try:
        track = get_track(song, track_index)
        if ctrl is None:
            raise RuntimeError("load_sample requires ctrl for application()")
        app = ctrl.application()

        # Strategy 1: exact URI match
        item = find_browser_item_by_uri(app.browser, sample_uri, ctrl=ctrl)

        # Strategy 2: name-based search (extract filename from URI or use as-is)
        if not item:
            if ":" in sample_uri:
                name = sample_uri.split(":")[-1].strip()
            else:
                name = sample_uri.strip()
            if name:
                if ctrl:
                    ctrl.log_message(
                        "URI match failed for '{0}', trying name search for '{1}'".format(
                            sample_uri, name))
                item = _find_browser_item_by_name(app.browser, name, ctrl=ctrl)

        if not item:
            raise ValueError("Sample '{0}' not found in browser".format(sample_uri))
        if hasattr(item, 'is_folder') and item.is_folder:
            raise ValueError(
                "'{0}' is a folder, not a loadable sample".format(
                    getattr(item, 'name', sample_uri)))
        if hasattr(item, 'is_loadable') and not item.is_loadable:
            raise ValueError(
                "Sample item '{0}' (URI: {1}) is not loadable".format(item.name, sample_uri))

        song.view.selected_track = track
        app.browser.load_item(item)

        return {
            "loaded": True,
            "item_name": item.name,
            "track_name": track.name,
            "uri": getattr(item, "uri", sample_uri),
        }
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error loading sample: {0}".format(str(e)))
            ctrl.log_message(traceback.format_exc())
        raise


def _process_item(item):
    """Build a dict for a browser item (no children recursion)."""
    if not item:
        return None
    return {
        "name": item.name if hasattr(item, "name") else "Unknown",
        "is_folder": (hasattr(item, "is_folder") and item.is_folder) or (hasattr(item, "children") and bool(item.children)),
        "is_device": hasattr(item, "is_device") and item.is_device,
        "is_loadable": hasattr(item, "is_loadable") and item.is_loadable,
        "uri": item.uri if hasattr(item, "uri") else None,
        "children": [],
    }


def get_browser_tree(song, category_type, ctrl=None):
    """Get a simplified tree of browser categories."""
    try:
        if ctrl is None:
            raise RuntimeError("get_browser_tree requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")
        if not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError("Browser is not available in the Live application")

        browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith("_")]
        if ctrl:
            ctrl.log_message("Available browser attributes: {0}".format(browser_attrs))

        result = {
            "type": category_type,
            "categories": [],
            "available_categories": browser_attrs,
        }

        _categories = [
            ("instruments", "Instruments"),
            ("sounds", "Sounds"),
            ("drums", "Drums"),
            ("audio_effects", "Audio Effects"),
            ("midi_effects", "MIDI Effects"),
        ]
        for attr_name, display_name in _categories:
            if (category_type == "all" or category_type == attr_name) and hasattr(app.browser, attr_name):
                try:
                    item = _process_item(getattr(app.browser, attr_name))
                    if item:
                        item["name"] = display_name
                        result["categories"].append(item)
                except Exception as e:
                    if ctrl:
                        ctrl.log_message("Error processing {0}: {1}".format(attr_name, str(e)))

        # Try additional browser categories
        known = {"instruments", "sounds", "drums", "audio_effects", "midi_effects"}
        for attr in browser_attrs:
            if attr not in known and (category_type == "all" or category_type == attr):
                try:
                    bitem = getattr(app.browser, attr)
                    if hasattr(bitem, "children") or hasattr(bitem, "name"):
                        category = _process_item(bitem)
                        if category:
                            category["name"] = attr.capitalize()
                            result["categories"].append(category)
                except Exception as e:
                    if ctrl:
                        ctrl.log_message("Error processing {0}: {1}".format(attr, str(e)))

        if ctrl:
            ctrl.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result["categories"])
            ))
        return result
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error getting browser tree: {0}".format(str(e)))
            ctrl.log_message(traceback.format_exc())
        raise


def get_browser_items_at_path(song, path, ctrl=None):
    """Get browser items at a specific path."""
    try:
        if ctrl is None:
            raise RuntimeError("get_browser_items_at_path requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")
        if not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError("Browser is not available in the Live application")

        if not path or not path.strip():
            raise ValueError("Invalid path: empty or blank")
        browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith("_")]
        path_parts = path.split("/")

        root_category = path_parts[0].lower()
        current_item = None

        _category_map = {
            "instruments": "instruments",
            "sounds": "sounds",
            "drums": "drums",
            "audio_effects": "audio_effects",
            "midi_effects": "midi_effects",
        }
        if root_category in _category_map and hasattr(app.browser, _category_map[root_category]):
            current_item = getattr(app.browser, _category_map[root_category])
        else:
            found = False
            for attr in browser_attrs:
                if attr.lower() == root_category:
                    try:
                        current_item = getattr(app.browser, attr)
                        found = True
                        break
                    except Exception as e:
                        if ctrl:
                            ctrl.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
            if not found:
                return {
                    "path": path,
                    "error": "Unknown or unavailable category: {0}".format(root_category),
                    "available_categories": browser_attrs,
                    "items": [],
                }

        # Navigate through path
        for i in range(1, len(path_parts)):
            part = path_parts[i]
            if not part:
                continue
            if not isinstance(current_item, (list, tuple)) and not hasattr(current_item, "children"):
                return {
                    "path": path,
                    "error": "Item at '{0}' has no children".format("/".join(path_parts[:i])),
                    "items": [],
                }
            found = False
            for child in _iter_children(current_item):
                if hasattr(child, "name") and child.name.lower() == part.lower():
                    current_item = child
                    found = True
                    break
            if not found:
                return {
                    "path": path,
                    "error": "Path part '{0}' not found".format(part),
                    "items": [],
                }

        # Get items at current path
        items = []
        for child in _iter_children(current_item):
            if len(items) >= _MAX_CHILDREN:
                break
            item_info = {
                "name": getattr(child, "name", "Unknown"),
                "is_folder": getattr(child, "is_folder", False) or (hasattr(child, "children") and bool(child.children)),
                "is_device": getattr(child, "is_device", False),
                "is_loadable": getattr(child, "is_loadable", False),
                "uri": getattr(child, "uri", None),
            }
            items.append(item_info)

        is_list_root = isinstance(current_item, (list, tuple))
        result = {
            "path": path,
            "name": getattr(current_item, "name", "Unknown") if not is_list_root else path_parts[0],
            "uri": getattr(current_item, "uri", None) if not is_list_root else None,
            "is_folder": is_list_root or getattr(current_item, "is_folder", False) or (hasattr(current_item, "children") and bool(current_item.children)),
            "truncated": len(items) >= _MAX_CHILDREN,
            "is_device": False if is_list_root else getattr(current_item, "is_device", False),
            "is_loadable": False if is_list_root else getattr(current_item, "is_loadable", False),
            "items": items,
        }

        if ctrl:
            ctrl.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
        return result
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error getting browser items at path: {0}".format(str(e)))
            ctrl.log_message(traceback.format_exc())
        raise


def search_browser(song, query, category, ctrl=None):
    """Search the browser for items matching a query."""
    try:
        if ctrl is None:
            raise RuntimeError("search_browser requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")
        if not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError("Browser is not available in the Live application")

        results = []
        query_lower = query.lower()

        def search_item(item, depth=0, max_depth=5):
            if len(results) >= _MAX_SEARCH_RESULTS:
                return
            if depth >= max_depth:
                return
            if not item:
                return
            if hasattr(item, "name") and query_lower in item.name.lower():
                result_item = {
                    "name": item.name,
                    "is_folder": (hasattr(item, "is_folder") and item.is_folder) or (hasattr(item, "children") and bool(item.children)),
                    "is_device": hasattr(item, "is_device") and item.is_device,
                    "is_loadable": hasattr(item, "is_loadable") and item.is_loadable,
                    "uri": item.uri if hasattr(item, "uri") else None,
                }
                results.append(result_item)
            if hasattr(item, "children"):
                try:
                    children = item.children
                except Exception:
                    return
                if children:
                    count = 0
                    for child in children:
                        if count >= _MAX_CHILDREN:
                            break
                        count += 1
                        search_item(child, depth + 1, max_depth)

        if category == "all":
            for attr in _BROWSER_ROOTS:
                if len(results) >= _MAX_SEARCH_RESULTS:
                    break
                if attr == "user_folders":
                    try:
                        for folder in getattr(app.browser, "user_folders", []):
                            if len(results) >= _MAX_SEARCH_RESULTS:
                                break
                            search_item(folder)
                    except Exception:
                        pass
                    continue
                root = getattr(app.browser, attr, None)
                if root is not None:
                    search_item(root)
        elif category in _BROWSER_ROOTS:
            if category == "user_folders":
                try:
                    for folder in getattr(app.browser, "user_folders", []):
                        if len(results) >= _MAX_SEARCH_RESULTS:
                            break
                        search_item(folder)
                except Exception:
                    pass
            else:
                root = getattr(app.browser, category, None)
                if root is not None:
                    search_item(root)
        else:
            msg = "Invalid browser category '{0}'. Valid: 'all', {1}".format(
                category, ", ".join("'{0}'".format(r) for r in _BROWSER_ROOTS))
            if ctrl:
                ctrl.log_message(msg)
            raise ValueError(msg)

        return {
            "query": query,
            "category": category,
            "results": results[:50],
            "total_found": len(results),
        }
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error searching browser: {0}".format(str(e)))
            ctrl.log_message(traceback.format_exc())
        raise


def get_user_library(song, ctrl=None):
    """Get the user library browser tree."""
    try:
        if ctrl is None:
            raise RuntimeError("get_user_library requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")

        items = []
        if hasattr(app.browser, "user_library"):
            user_lib = app.browser.user_library
            if hasattr(user_lib, "children"):
                for child in user_lib.children:
                    if len(items) >= _MAX_CHILDREN:
                        break
                    items.append({
                        "name": child.name if hasattr(child, "name") else "Unknown",
                        "is_folder": (hasattr(child, "is_folder") and child.is_folder) or (hasattr(child, "children") and bool(child.children)),
                        "uri": child.uri if hasattr(child, "uri") else None,
                    })
        return {"items": items, "count": len(items)}
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error getting user library: {0}".format(str(e)))
        raise


def get_user_folders(song, ctrl=None):
    """Get user-configured sample folders from Ableton's browser."""
    try:
        if ctrl is None:
            raise RuntimeError("get_user_folders requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")

        items = []
        if hasattr(app.browser, "user_folders"):
            for folder in app.browser.user_folders:
                folder_items = []
                if hasattr(folder, "children"):
                    for child in folder.children:
                        if len(folder_items) >= _MAX_CHILDREN:
                            break
                        folder_items.append({
                            "name": child.name if hasattr(child, "name") else "Unknown",
                            "uri": child.uri if hasattr(child, "uri") else None,
                        })
                items.append({
                    "name": folder.name if hasattr(folder, "name") else "Unknown",
                    "uri": folder.uri if hasattr(folder, "uri") else None,
                    "items": folder_items,
                })
        return {"folders": items, "count": len(items)}
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error getting user folders: {0}".format(str(e)))
        raise


def get_device_presets(song, track_index, device_index, track_type="track", ctrl=None):
    """Get available presets for a device by navigating the browser."""
    track = get_track(song, track_index, track_type)

    if device_index < 0 or device_index >= len(track.devices):
        raise IndexError("Device index {0} out of range".format(device_index))

    device = track.devices[device_index]
    class_name = device.class_name if hasattr(device, 'class_name') else ""
    device_name = device.name if hasattr(device, 'name') else ""

    # Note: VST/AU internal presets are NOT accessible through the Live API
    # We can only browse Ableton's preset library for native devices
    presets = []

    browser = None
    if ctrl is not None:
        try:
            app = ctrl.application()
            if app:
                browser = app.browser
        except Exception:
            pass

    if browser is None:
        return {"device_name": device_name, "presets": [], "note": "Browser not available"}

    # For native devices, try to find presets in the browser
    # Navigate through instruments or audio_effects categories
    try:
        # Search through browser items matching the device class name
        categories_to_search = []
        if hasattr(browser, 'instruments'):
            categories_to_search.append(browser.instruments)
        if hasattr(browser, 'audio_effects'):
            categories_to_search.append(browser.audio_effects)
        if hasattr(browser, 'midi_effects'):
            categories_to_search.append(browser.midi_effects)

        for category in categories_to_search:
            if not hasattr(category, 'children'):
                continue
            for child in category.children:
                child_name = child.name if hasattr(child, 'name') else ""
                if child_name.lower() == class_name.lower() or child_name.lower() == device_name.lower():
                    # Found the device category - list its presets
                    if hasattr(child, 'children'):
                        for preset in child.children:
                            preset_name = preset.name if hasattr(preset, 'name') else ""
                            preset_uri = preset.uri if hasattr(preset, 'uri') else ""
                            if preset_name:
                                presets.append({
                                    "name": preset_name,
                                    "uri": preset_uri,
                                    "is_folder": bool(preset.is_folder) if hasattr(preset, 'is_folder') else False,
                                })
                    break
    except Exception as e:
        return {"device_name": device_name, "presets": [], "error": str(e)}

    return {
        "device_name": device_name,
        "class_name": class_name,
        "preset_count": len(presets),
        "presets": presets,
        "note": "VST/AU internal presets are not accessible via the Live API" if not presets else "",
    }


def load_device_preset(song, track_index, device_index, preset_uri, track_type="track", ctrl=None):
    """Load a preset onto a device using hot-swap."""
    track = get_track(song, track_index, track_type)

    if device_index < 0 or device_index >= len(track.devices):
        raise IndexError("Device index {0} out of range".format(device_index))

    device = track.devices[device_index]

    browser = None
    if ctrl is not None:
        try:
            app = ctrl.application()
            if app:
                browser = app.browser
        except Exception:
            pass

    if browser is None:
        raise ValueError("Browser not available")

    if not preset_uri:
        raise ValueError("preset_uri is required")

    # Use the browser to load the preset via hot-swap
    try:
        # Find the browser item by URI
        item = find_browser_item_by_uri(browser, preset_uri, ctrl=ctrl)
        if item is None:
            raise ValueError("Could not find preset with URI: {0}".format(preset_uri))

        # Hot-swap the preset onto the device
        if hasattr(browser, 'load_item'):
            browser.load_item(item)
        else:
            raise ValueError("Browser load_item not available")

    except AttributeError:
        raise ValueError("Preset loading not supported in this Ableton version")

    return {
        "device_name": device.name if hasattr(device, 'name') else "",
        "preset_uri": preset_uri,
        "loaded": True,
    }


def preview_browser_item(song, uri=None, action="preview", ctrl=None):
    """Preview (audition) a browser item, or stop the current preview.

    Args:
        uri: The URI of the browser item to preview (required for 'preview' action).
        action: 'preview' to start previewing, 'stop' to stop the current preview.
    """
    try:
        if ctrl is None:
            raise RuntimeError("preview_browser_item requires ctrl for application()")
        app = ctrl.application()
        if not app:
            raise RuntimeError("Could not access Live application")
        browser = app.browser
        if action == "stop":
            browser.stop_preview()
            return {"action": "stop", "previewing": False}
        elif action == "preview":
            if not uri:
                raise ValueError("uri is required for preview action")
            item = find_browser_item_by_uri(browser, uri, ctrl=ctrl)
            if item is None:
                raise ValueError("Browser item not found for URI: {0}".format(uri))
            browser.preview_item(item)
            return {
                "action": "preview",
                "uri": uri,
                "name": getattr(item, "name", "Unknown"),
                "previewing": True,
            }
        else:
            raise ValueError("action must be 'preview' or 'stop', got '{0}'".format(action))
    except Exception as e:
        if ctrl:
            ctrl.log_message("Error previewing browser item: {0}".format(str(e)))
        raise
