"""Device/parameter tool handlers for AbletonBridge."""
import json
import time
import logging
from typing import Dict, Any, List, Optional
from mcp.server.fastmcp import Context
from MCP_Server.tools._base import _tool_handler, _m4l_result
from MCP_Server.connections.ableton import get_ableton_connection
from MCP_Server.connections.m4l import get_m4l_connection
from MCP_Server.connections.extensions_sdk import get_sdk_client
from MCP_Server.validation import _validate_index, _validate_range
from MCP_Server.cache.browser import resolve_device_uri, resolve_sample_uri, get_browser_cache
import MCP_Server.state as state

logger = logging.getLogger("AbletonBridge")


# ==========================================================================
# DEVICE_PROPERTIES knowledge base
# ==========================================================================

DEVICE_PROPERTIES: Dict[str, Dict[str, Dict[str, Any]]] = {

    # ===== Wavetable (InstrumentVector) =====================================
    "InstrumentVector": {
        # --- Voice / Unison ---
        "unison_mode": {
            "description": "Unison stacking mode",
            "type": "enum",
            "values": {
                0: "None", 1: "Classic", 2: "Shimmer",
                3: "Noise", 4: "Phase Sync", 5: "Position Spread",
            },
        },
        "unison_voice_count": {
            "description": "Number of unison voices",
            "type": "int", "min": 2, "max": 8,
        },
        "poly_voices": {
            "description": "Polyphony voice count (value + 2 = actual voices)",
            "type": "enum",
            "values": {
                0: "2 voices", 1: "3 voices", 2: "4 voices", 3: "5 voices",
                4: "6 voices", 5: "7 voices", 6: "8 voices",
            },
        },
        "mono_poly": {
            "description": "Mono/Poly voice mode",
            "type": "enum",
            "values": {0: "Mono", 1: "Poly"},
        },

        # --- Filter ---
        "filter_routing": {
            "description": "Routing between Filter 1 and Filter 2",
            "type": "enum",
            "values": {0: "Serial", 1: "Parallel", 2: "Split"},
        },

        # --- Oscillator Effect Mode ---
        "oscillator_1_effect_mode": {
            "description": "Oscillator 1 warp effect type",
            "type": "enum",
            "values": {0: "None", 1: "FM", 2: "Classic", 3: "Modern", 4: "Ping Pong"},
        },
        "oscillator_2_effect_mode": {
            "description": "Oscillator 2 warp effect type",
            "type": "enum",
            "values": {0: "None", 1: "FM", 2: "Classic", 3: "Modern", 4: "Ping Pong"},
        },

        # --- Wavetable Selection ---
        "oscillator_1_wavetable_category": {
            "description": "Osc 1 wavetable category index",
            "type": "int", "min": 0,
            "note": "Changing this updates the list from oscillator_1_wavetables",
        },
        "oscillator_1_wavetable_index": {
            "description": "Osc 1 wavetable index within current category",
            "type": "int", "min": 0,
        },
        "oscillator_2_wavetable_category": {
            "description": "Osc 2 wavetable category index",
            "type": "int", "min": 0,
            "note": "Changing this updates the list from oscillator_2_wavetables",
        },
        "oscillator_2_wavetable_index": {
            "description": "Osc 2 wavetable index within current category",
            "type": "int", "min": 0,
        },

        # --- Read-only Lists ---
        "oscillator_wavetable_categories": {
            "description": "Available wavetable category names (comma-separated)",
            "type": "list", "readonly": True,
        },
        "oscillator_1_wavetables": {
            "description": "Available wavetable names for Osc 1 in current category",
            "type": "list", "readonly": True,
        },
        "oscillator_2_wavetables": {
            "description": "Available wavetable names for Osc 2 in current category",
            "type": "list", "readonly": True,
        },
        "visible_modulation_target_names": {
            "description": "Modulation target parameter names (comma-separated)",
            "type": "list", "readonly": True,
        },
    },

    # ===== Drift (DriftDevice) =================================================
    "DriftDevice": {
        # --- Voice ---
        "voice_mode_index": {
            "description": "Voice mode (mono/poly)",
            "type": "int", "min": 0,
            "note": "Use voice_mode_list to get available mode names",
        },
        "voice_mode_list": {
            "description": "Available voice mode names",
            "type": "list", "readonly": True,
        },
        "voice_count_index": {
            "description": "Voice count setting",
            "type": "int", "min": 0,
            "note": "Use voice_count_list to get available counts",
        },
        "voice_count_list": {
            "description": "Available voice count settings",
            "type": "list", "readonly": True,
        },
        "pitch_bend_range": {
            "description": "MIDI pitch bend range in semitones",
            "type": "int", "min": 0, "max": 48,
        },

        # --- Modulation Matrix ---
        "mod_matrix_filter_source_1_index": {
            "description": "Filter frequency mod source 1 index",
            "type": "int", "min": 0,
        },
        "mod_matrix_filter_source_1_list": {
            "description": "Available filter mod source 1 names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_filter_source_2_index": {
            "description": "Filter frequency mod source 2 index",
            "type": "int", "min": 0,
        },
        "mod_matrix_filter_source_2_list": {
            "description": "Available filter mod source 2 names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_lfo_source_index": {
            "description": "LFO amount mod source index",
            "type": "int", "min": 0,
        },
        "mod_matrix_lfo_source_list": {
            "description": "Available LFO mod source names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_pitch_source_1_index": {
            "description": "Pitch mod source 1 index",
            "type": "int", "min": 0,
        },
        "mod_matrix_pitch_source_1_list": {
            "description": "Available pitch mod source 1 names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_pitch_source_2_index": {
            "description": "Pitch mod source 2 index",
            "type": "int", "min": 0,
        },
        "mod_matrix_pitch_source_2_list": {
            "description": "Available pitch mod source 2 names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_shape_source_index": {
            "description": "Shape mod source index",
            "type": "int", "min": 0,
        },
        "mod_matrix_shape_source_list": {
            "description": "Available shape mod source names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_source_1_index": {
            "description": "Custom mod slot 1 source index",
            "type": "int", "min": 0,
        },
        "mod_matrix_source_1_list": {
            "description": "Available custom mod slot 1 source names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_source_2_index": {
            "description": "Custom mod slot 2 source index",
            "type": "int", "min": 0,
        },
        "mod_matrix_source_2_list": {
            "description": "Available custom mod slot 2 source names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_source_3_index": {
            "description": "Custom mod slot 3 source index",
            "type": "int", "min": 0,
        },
        "mod_matrix_source_3_list": {
            "description": "Available custom mod slot 3 source names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_target_1_index": {
            "description": "Custom mod slot 1 target index",
            "type": "int", "min": 0,
        },
        "mod_matrix_target_1_list": {
            "description": "Available custom mod slot 1 target names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_target_2_index": {
            "description": "Custom mod slot 2 target index",
            "type": "int", "min": 0,
        },
        "mod_matrix_target_2_list": {
            "description": "Available custom mod slot 2 target names",
            "type": "list", "readonly": True,
        },
        "mod_matrix_target_3_index": {
            "description": "Custom mod slot 3 target index",
            "type": "int", "min": 0,
        },
        "mod_matrix_target_3_list": {
            "description": "Available custom mod slot 3 target names",
            "type": "list", "readonly": True,
        },
    },

    # ===== Meld (MeldDevice) ===================================================
    "MeldDevice": {
        "selected_engine": {
            "description": "Oscillator engine selector",
            "type": "enum",
            "values": {0: "Engine A", 1: "Engine B"},
        },
        "unison_voices": {
            "description": "Unison voice count",
            "type": "enum",
            "values": {0: "Off", 1: "2", 2: "3", 3: "4"},
        },
        "mono_poly": {
            "description": "Mono/Poly voice mode",
            "type": "enum",
            "values": {0: "Mono", 1: "Poly"},
        },
        "poly_voices": {
            "description": "Polyphony voice count",
            "type": "enum",
            "values": {
                0: "2", 1: "3", 2: "4", 3: "5",
                4: "6", 5: "8", 6: "12",
            },
        },
    },

    # ===== Roar (RoarDevice) ===================================================
    "RoarDevice": {
        "routing_mode_index": {
            "description": "Routing mode used by Roar",
            "type": "int", "min": 0,
            "note": "Use routing_mode_list to get available mode names",
        },
        "routing_mode_list": {
            "description": "Available routing mode names",
            "type": "list", "readonly": True,
        },
        "env_listen": {
            "description": "Envelope Input Listen toggle",
            "type": "enum",
            "values": {0: "Off", 1: "On"},
        },
    },

    # ===== Spectral Resonator (SpectralResonatorDevice) ========================
    "SpectralResonatorDevice": {
        "frequency_dial_mode": {
            "description": "Freq control mode",
            "type": "enum",
            "values": {0: "Hertz", 1: "MIDI Note"},
        },
        "midi_gate": {
            "description": "MIDI gate switch",
            "type": "enum",
            "values": {0: "Off", 1: "On"},
        },
        "mod_mode": {
            "description": "Modulation mode",
            "type": "enum",
            "values": {0: "None", 1: "Chorus", 2: "Wander", 3: "Granular"},
        },
        "mono_poly": {
            "description": "Mono/Poly switch",
            "type": "enum",
            "values": {0: "Mono", 1: "Poly"},
        },
        "pitch_mode": {
            "description": "Pitch mode",
            "type": "enum",
            "values": {0: "Internal", 1: "MIDI"},
        },
        "pitch_bend_range": {
            "description": "Pitch bend range in semitones",
            "type": "int", "min": 0, "max": 48,
        },
        "polyphony": {
            "description": "Polyphony voice count",
            "type": "enum",
            "values": {0: "2", 1: "4", 2: "8", 3: "16"},
        },
    },

    # ===== Shifter (ShifterDevice) =============================================
    "ShifterDevice": {
        "pitch_bend_range": {
            "description": "Pitch bend range for MIDI pitch mode",
            "type": "int", "min": 0, "max": 48,
        },
        "pitch_mode_index": {
            "description": "Pitch mode",
            "type": "enum",
            "values": {0: "Internal", 1: "MIDI"},
        },
    },

    # ===== Drum Cell (DrumCellDevice) ==========================================
    "DrumCellDevice": {
        "gain": {
            "description": "Sample gain (normalized value)",
            "type": "float", "min": 0.0, "max": 1.0,
        },
    },
}


# ==========================================================================
# DEVICE_PROPERTIES helper functions
# ==========================================================================

def _get_property_info(device_class: str, property_name: str) -> Optional[Dict[str, Any]]:
    """Look up property metadata from the DEVICE_PROPERTIES knowledge base."""
    device_props = DEVICE_PROPERTIES.get(device_class, {})
    return device_props.get(property_name)


def _format_property_value(prop_info: Optional[Dict[str, Any]], value) -> str:
    """Format a property value with its human-readable label."""
    if prop_info and prop_info.get("type") == "enum" and "values" in prop_info:
        if isinstance(value, (int, float)):
            label = prop_info["values"].get(int(value))
            if label:
                return f"{value} ({label})"
    return str(value)


def _format_property_options(prop_info: Optional[Dict[str, Any]]) -> str:
    """Build a human-readable options string for enum properties."""
    if not prop_info or prop_info.get("type") != "enum":
        return ""
    values = prop_info.get("values", {})
    if not values:
        return ""
    return "Options: " + ", ".join(f"{k}={v}" for k, v in sorted(values.items()))


def _validate_property_value(
    prop_info: Optional[Dict[str, Any]], property_name: str, value: float
) -> None:
    """Validate a value against known property constraints.

    Raises ValueError if validation fails.  Does nothing for unknown properties.
    """
    if prop_info is None:
        return  # Unknown property — let the bridge handle it

    if prop_info.get("readonly"):
        raise ValueError(
            f"Property '{property_name}' is read-only and cannot be set."
        )

    if prop_info["type"] == "enum":
        valid_keys = list(prop_info.get("values", {}).keys())
        if int(value) not in valid_keys:
            options = ", ".join(f"{k}={v}" for k, v in sorted(prop_info["values"].items()))
            raise ValueError(
                f"Invalid value {int(value)} for '{property_name}'. Valid options: {options}"
            )
    elif prop_info["type"] in ("int", "float"):
        min_val = prop_info.get("min")
        max_val = prop_info.get("max")
        if min_val is not None and value < min_val:
            raise ValueError(f"Value {value} below minimum {min_val} for '{property_name}'.")
        if max_val is not None and value > max_val:
            raise ValueError(f"Value {value} above maximum {max_val} for '{property_name}'.")


def _m4l_batch_set_params(m4l, track_index, device_index, parameters):
    """Set multiple hidden parameters by sending individual set_hidden_param
    commands sequentially.

    Returns a dict with keys: params_set, params_failed, total_requested, errors.
    """
    ok = 0
    failed = 0
    errors: List[str] = []
    for p in parameters:
        try:
            result = m4l.send_command("set_hidden_param", {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": int(p["index"]),
                "value": float(p["value"]),
            })
            if result.get("status") == "success":
                ok += 1
            else:
                failed += 1
                errors.append(f"[{p['index']}]: {result.get('message', '?')}")
        except Exception as e:
            failed += 1
            errors.append(f"[{p['index']}]: {str(e)}")
        # Small delay to let Ableton breathe when setting many params
        if len(parameters) > 6:
            time.sleep(0.05)
    return {
        "params_set": ok,
        "params_failed": failed,
        "total_requested": ok + failed,
        "errors": errors,
    }


# ==========================================================================
# Tool registration
# ==========================================================================

def register_tools(mcp):

    # ------------------------------------------------------------------
    # Core device parameter tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting device parameters")
    def get_device_parameters(ctx: Context, track_index: int, device_index: int,
                               track_type: str = "track") -> str:
        """
        Get all parameters and their current values for a device on a track.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - track_type: Type of track: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        # Tier 1: Extensions SDK (async, more reliable for VST3/AU on Apple Silicon)
        # Guard track_type first — avoids the ping cache check for return/master tracks
        sdk = get_sdk_client() if track_type == "track" else None
        if sdk:
            try:
                result = sdk.get_params(track_index, device_index)
                result["_source"] = "extensions_sdk"
                return json.dumps(result)
            except Exception as e:
                logger.debug("SDK bridge get_params failed, falling back to _Framework: %s", e)

        # Tier 2: _Framework Remote Script (always available)
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })
        result["_source"] = "_framework"
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting device info")
    def get_device_info(ctx: Context, track_index: int, device_index: int, track_type: str = "track") -> str:
        """Get detailed information about a specific device including its type classification.

        Returns device class, type (native/vst/vst3/au/m4l), chain/drum pad capabilities,
        and parameter count.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - track_type: "track", "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_info", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting plugin info")
    def get_plugin_info(ctx: Context, track_index: int, device_index: int, track_type: str = "track") -> str:
        """Get plugin-specific information and guidance for a device.

        Extends get_device_info with plugin-aware analysis: whether VST/AU
        parameters are fully exposed, guidance on the Configure button, and
        device-type-specific tips.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - track_type: "track", "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()

        info = ableton.send_command("get_device_info", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })

        device_type = info.get("device_type", "native")
        param_count = info.get("parameter_count", 0)
        class_name = info.get("class_name", "")

        result = {
            "name": info.get("name", ""),
            "class_name": class_name,
            "device_type": device_type,
            "parameter_count": param_count,
            "is_plugin": device_type in ("vst", "vst3", "au"),
            "is_configured": param_count > 32 if device_type in ("vst", "vst3", "au") else None,
            "guidance": [],
        }

        if device_type in ("vst", "vst3", "au"):
            if param_count <= 32:
                result["guidance"].append(
                    "This plugin exposes only {0} parameters (default limit is 32). "
                    "To access more parameters, open the device in Ableton, click "
                    "the Configure button (wrench icon), and manually add parameters.".format(param_count)
                )
            else:
                result["guidance"].append(
                    "This plugin has {0} exposed parameters (Configure has been used).".format(param_count)
                )
            result["guidance"].append(
                "VST/AU internal presets are NOT accessible via the scripting API. "
                "Use Ableton's preset system (get_device_presets) for saved presets."
            )
        elif device_type == "m4l":
            result["guidance"].append(
                "Max for Live device. Use get_device_hidden_parameter and "
                "set_device_hidden_parameter (M4L bridge) for full parameter access."
            )
        elif device_type == "native":
            result["guidance"].append(
                "Native Ableton device. All parameters are fully accessible."
            )

        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting device parameter")
    def set_device_parameter(ctx: Context, track_index: int, device_index: int,
                              parameter_name: str, value: float,
                              track_type: str = "track") -> str:
        """
        Set a device parameter value.

        Use for a single standard parameter change. For multiple params at once,
        use set_device_parameters instead. For hidden/non-automatable params, use
        set_device_hidden_parameter (requires M4L bridge).

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - parameter_name: The name of the parameter to set
        - value: The new value for the parameter (will be clamped to min/max)
        - track_type: Type of track: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        # Tier 1: Extensions SDK
        sdk = get_sdk_client() if track_type == "track" else None
        if sdk:
            try:
                result = sdk.set_param(
                    track_index, device_index, value, param_name=parameter_name
                )
                pname = result.get("parameter", parameter_name)
                clamped = " (clamped to valid range)" if result.get("clamped") else ""
                return f"Set parameter '{pname}' to {result.get('value')}{clamped} [Extensions SDK]"
            except Exception as e:
                logger.debug("SDK bridge set_param failed, falling back to _Framework: %s", e)

        # Tier 2: _Framework Remote Script
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "value": value,
            "track_type": track_type,
        })
        pname = result.get('parameter', parameter_name)
        if result.get("clamped", False):
            return f"Set parameter '{pname}' to {result.get('value')} (value was clamped to valid range)"
        return f"Set parameter '{pname}' to {result.get('value')}"

    @mcp.tool()
    @_tool_handler("setting device parameters")
    def set_device_parameters(ctx: Context, track_index: int, device_index: int,
                               parameters: str, track_type: str = "track") -> str:
        """
        Set multiple device parameters in a single call (much faster than setting one at a time).

        ALWAYS prefer this over calling set_device_parameter multiple times.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - parameters: JSON string of parameter list, e.g. '[{"name": "Filter Freq", "value": 0.5}, {"name": "Resonance", "value": 0.3}]'
        - track_type: Type of track: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        params_list = json.loads(parameters) if isinstance(parameters, str) else parameters
        if not isinstance(params_list, list) or not params_list:
            return "Error: parameters must be a non-empty JSON array of {name, value} objects"

        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameters_batch", {
            "track_index": track_index,
            "device_index": device_index,
            "parameters": params_list,
            "track_type": track_type,
        })

        device_name = result.get("device_name", "?")
        results = result.get("results", [])
        ok = [r for r in results if "error" not in r]
        errs = [r for r in results if "error" in r]

        summary = f"Set {len(ok)} parameters on '{device_name}'"
        if errs:
            summary += f" ({len(errs)} not found: {', '.join(r['name'] for r in errs)})"
        return summary

    # ------------------------------------------------------------------
    # Real-time (UDP) parameter tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("sending real-time parameter")
    def realtime_set_parameter(ctx: Context, track_index: int, device_index: int,
                               parameter_name: str, value: float,
                               track_type: str = "track") -> str:
        """
        Set a device parameter via UDP for real-time control (fire-and-forget, no confirmation).

        Use this instead of set_device_parameter when you need rapid parameter changes
        (e.g., filter sweeps, volume ramps) where response confirmation is not needed.
        The value is applied immediately with minimal latency.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - parameter_name: The name of the parameter to set
        - value: The new value for the parameter (will be clamped to min/max)
        - track_type: Type of track: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"
        ableton = get_ableton_connection()
        ableton.send_udp_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "value": value,
            "track_type": track_type,
        })
        return f"Sent real-time parameter update: '{parameter_name}' = {value} (fire-and-forget via UDP)"

    @mcp.tool()
    @_tool_handler("sending real-time batch parameters")
    def realtime_batch_set_parameters(ctx: Context, track_index: int, device_index: int,
                                      parameters: str, track_type: str = "track") -> str:
        """
        Set multiple device parameters at once via UDP for real-time control (fire-and-forget).

        Use for rapid multi-param changes (e.g., morphing presets in real-time).
        No response confirmation — fire-and-forget. For confirmed batch updates,
        use set_device_parameters instead.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        - parameters: JSON string of parameter list, e.g. '[{"name": "Filter Freq", "value": 0.5}]'
        - track_type: Type of track: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        params_list = json.loads(parameters) if isinstance(parameters, str) else parameters
        if not isinstance(params_list, list) or not params_list:
            return "Error: parameters must be a non-empty JSON array of {name, value} objects"

        ableton = get_ableton_connection()
        ableton.send_udp_command("batch_set_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "parameters": params_list,
            "track_type": track_type,
        })
        return f"Sent real-time batch update for {len(params_list)} parameters (fire-and-forget via UDP)"

    # ------------------------------------------------------------------
    # Device lifecycle tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("deleting device")
    def delete_device(ctx: Context, track_index: int, device_index: int) -> str:
        """
        Delete a device from a track.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device to delete
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_device", {
            "track_index": track_index,
            "device_index": device_index
        })
        return f"Deleted device '{result.get('device_name', 'unknown')}' from track {track_index}"

    @mcp.tool()
    @_tool_handler("setting device enabled")
    def set_device_enabled(ctx: Context, track_index: int, device_index: int,
                             enabled: bool, track_type: str = "track") -> str:
        """Toggle a device on or off (bypass).

        Parameters:
        - track_index: The track index
        - device_index: The device index
        - enabled: True to activate, False to bypass
        - track_type: "track", "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_enabled", {
            "track_index": track_index, "device_index": device_index,
            "enabled": enabled, "track_type": track_type,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("loading instrument")
    def load_instrument_or_effect(ctx: Context, track_index: int, uri: str,
                                  track_type: str = "track") -> str:
        """
        Load an instrument or effect onto a track using its URI or device name.

        General-purpose device loader. Works for instruments, audio effects, MIDI
        effects, and presets. For native-only devices on Live 12.3+,
        insert_device_by_name is faster.

        Parameters:
        - track_index: The index of the track to load the instrument on
        - uri: The URI of the instrument/effect, OR a device name (resolved automatically).

        You can pass any Ableton instrument, audio effect, or MIDI effect name
        directly — no need to call search_browser first.  The server resolves the
        name to the correct URI using the browser cache.

        Common examples:
          Instruments: Analog, Drift, Operator, Sampler, Simpler, Wavetable
          Audio Effects: Reverb, Compressor, EQ Eight, Delay, Auto Filter, Limiter
          MIDI Effects: Arpeggiator, Chord, Scale, Velocity

        Examples:
          load_instrument_or_effect(track_index=0, uri="Analog")
          load_instrument_or_effect(track_index=2, uri="Reverb")
          load_instrument_or_effect(track_index=1, uri="Compressor")

        For presets or third-party items, use search_browser() to find the full URI.
        """
        _validate_index(track_index, "track_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"
        uri = resolve_device_uri(uri)
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri,
            "track_type": track_type
        })

        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"

    @mcp.tool()
    @_tool_handler("inserting device by name")
    def insert_device_by_name(ctx: Context, track_index: int,
                               device_name: str,
                               target_index: int = None) -> str:
        """Insert a native Live device by name into a track's device chain.
        Faster than load_instrument_or_effect but native devices only (not plugins
        or M4L). Available since Live 12.3.

        Parameters:
        - track_index: Track to insert device into
        - device_name: Name as shown in Live's UI (e.g. 'Compressor', 'EQ Eight', 'Reverb', 'Auto Filter')
        - target_index: Position in the device chain (optional, defaults to end)
        """
        params = {"track_index": track_index, "device_name": device_name}
        if target_index is not None:
            params["target_index"] = target_index
        ableton = get_ableton_connection()
        result = ableton.send_command("insert_device", params)
        return f"Device '{device_name}' inserted on track {track_index}"

    # ------------------------------------------------------------------
    # Compressor sidechain
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting compressor sidechain")
    def get_compressor_sidechain(ctx: Context, track_index: int, device_index: int) -> str:
        """Get side-chain routing info for a Compressor device.

        Parameters:
        - track_index: The index of the track containing the Compressor
        - device_index: The index of the Compressor device on the track

        Returns the current side-chain input routing type and channel, plus lists
        of all available input routing options. The device must be a Compressor.
        Use this before set_compressor_sidechain to see available routing options.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_compressor_sidechain", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting compressor sidechain")
    def set_compressor_sidechain(ctx: Context, track_index: int, device_index: int,
                                  input_type: str = None, input_channel: str = None,
                                  source_track_name: str = None, track_type: str = "track") -> str:
        """Set side-chain routing on a Compressor device.

        Two modes:
        1. By display name: provide input_type and/or input_channel
        2. By track name: provide source_track_name to auto-resolve routing

        Parameters:
        - track_index: The index of the track containing the Compressor
        - device_index: The index of the Compressor device on the track
        - input_type: Side-chain source type display name (e.g. a track name, 'Ext. In'). Optional.
        - input_channel: Side-chain source channel display name (e.g. 'Post FX', 'Pre FX'). Optional.
        - source_track_name: Name of the track to use as sidechain source (auto-resolves routing). Optional.
        - track_type: "track", "return", or "master" (used with source_track_name)

        Use get_compressor_sidechain first to see available routing options.
        Works with Compressor, Glue Compressor, and Multiband Dynamics.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")

        if source_track_name:
            ableton = get_ableton_connection()
            result = ableton.send_command("set_sidechain_by_name", {
                "track_index": track_index,
                "device_index": device_index,
                "source_track_name": source_track_name,
                "track_type": track_type,
            })
            return json.dumps(result)

        params = {"track_index": track_index, "device_index": device_index}
        if input_type is not None:
            params["input_type"] = input_type
        if input_channel is not None:
            params["input_channel"] = input_channel
        ableton = get_ableton_connection()
        result = ableton.send_command("set_compressor_sidechain", params)
        changes = [f"{k}={v}" for k, v in result.items()
                   if k not in ("track_index", "device_index", "device_name")]
        device_name = result.get("device_name", "?")
        return f"Compressor '{device_name}' sidechain updated: {', '.join(changes) if changes else 'no changes'}"

    # ------------------------------------------------------------------
    # EQ Eight
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting EQ8 properties")
    def get_eq8_properties(ctx: Context, track_index: int, device_index: int) -> str:
        """Get EQ Eight-specific properties beyond standard device parameters.

        Parameters:
        - track_index: The index of the track containing the EQ Eight
        - device_index: The index of the EQ Eight device on the track

        Returns edit_mode (0=A curve, 1=B curve), global_mode (0=Stereo, 1=L/R, 2=M/S),
        oversample (boolean), and selected_band (0-7). The device must be an EQ Eight.
        Use get_device_parameters for the standard EQ band parameters (frequency, gain, Q, etc.).
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_eq8_properties", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting EQ8 properties")
    def set_eq8_properties(ctx: Context, track_index: int, device_index: int,
                            edit_mode: int = None, global_mode: int = None,
                            oversample: bool = None, selected_band: int = None) -> str:
        """Set EQ Eight-specific properties.

        Parameters:
        - track_index: The index of the track containing the EQ Eight
        - device_index: The index of the EQ Eight device on the track
        - edit_mode: 0 for curve A, 1 for curve B. Optional.
        - global_mode: 0 for Stereo, 1 for Left/Right, 2 for Mid/Side. Optional.
        - oversample: True to enable oversampling, False to disable. Optional.
        - selected_band: Select an EQ band (0-7) for editing. Optional.

        The device must be an EQ Eight. Set any combination of properties in a single call.
        Use get_device_parameters + set_device_parameter for the standard band parameters
        (frequency, gain, Q, type, etc.).
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        params = {"track_index": track_index, "device_index": device_index}
        if edit_mode is not None:
            _validate_range(edit_mode, "edit_mode", 0, 1)
            params["edit_mode"] = edit_mode
        if global_mode is not None:
            _validate_range(global_mode, "global_mode", 0, 2)
            params["global_mode"] = global_mode
        if oversample is not None:
            params["oversample"] = oversample
        if selected_band is not None:
            _validate_range(selected_band, "selected_band", 0, 7)
            params["selected_band"] = selected_band
        ableton = get_ableton_connection()
        result = ableton.send_command("set_eq8_properties", params)
        device_name = result.get("device_name", "?")
        changes = [f"{k}={v}" for k, v in result.items()
                   if k not in ("track_index", "device_index", "device_name")]
        return f"EQ Eight '{device_name}' updated: {', '.join(changes) if changes else 'no changes'}"

    # ------------------------------------------------------------------
    # Hybrid Reverb
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting Hybrid Reverb IR")
    def get_hybrid_reverb_ir(ctx: Context, track_index: int, device_index: int) -> str:
        """Get impulse response (IR) configuration from a Hybrid Reverb device.

        Parameters:
        - track_index: The index of the track containing the Hybrid Reverb
        - device_index: The index of the Hybrid Reverb device on the track

        Returns the list of IR categories and files, the currently selected category
        and file indices, and time shaping parameters (attack_time, decay_time,
        size_factor, time_shaping_on). The device must be a Hybrid Reverb.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_hybrid_reverb_ir", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting Hybrid Reverb IR")
    def set_hybrid_reverb_ir(ctx: Context, track_index: int, device_index: int,
                              ir_category_index: int = None, ir_file_index: int = None,
                              ir_attack_time: float = None, ir_decay_time: float = None,
                              ir_size_factor: float = None, ir_time_shaping_on: bool = None) -> str:
        """Set impulse response (IR) configuration on a Hybrid Reverb device.

        Parameters:
        - track_index: The index of the track containing the Hybrid Reverb
        - device_index: The index of the Hybrid Reverb device on the track
        - ir_category_index: Index into ir_category_list to select an IR category. Optional.
        - ir_file_index: Index into ir_file_list to select an IR file within the current category. Optional.
        - ir_attack_time: IR attack time (float). Optional.
        - ir_decay_time: IR decay time (float). Optional.
        - ir_size_factor: IR size scaling factor (float). Optional.
        - ir_time_shaping_on: True to enable time shaping, False to disable. Optional.

        The device must be a Hybrid Reverb. Use get_hybrid_reverb_ir first to see available
        categories and files. When changing both category and file, set them in the same call
        — the category is applied first, then the file index within the new category.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        params = {"track_index": track_index, "device_index": device_index}
        if ir_category_index is not None:
            _validate_index(ir_category_index, "ir_category_index")
            params["ir_category_index"] = ir_category_index
        if ir_file_index is not None:
            _validate_index(ir_file_index, "ir_file_index")
            params["ir_file_index"] = ir_file_index
        if ir_attack_time is not None:
            params["ir_attack_time"] = ir_attack_time
        if ir_decay_time is not None:
            params["ir_decay_time"] = ir_decay_time
        if ir_size_factor is not None:
            params["ir_size_factor"] = ir_size_factor
        if ir_time_shaping_on is not None:
            params["ir_time_shaping_on"] = ir_time_shaping_on
        ableton = get_ableton_connection()
        result = ableton.send_command("set_hybrid_reverb_ir", params)
        device_name = result.get("device_name", "?")
        changes = [f"{k}={v}" for k, v in result.items()
                   if k not in ("track_index", "device_index", "device_name")]
        return f"Hybrid Reverb '{device_name}' IR updated: {', '.join(changes) if changes else 'no changes'}"

    # ------------------------------------------------------------------
    # Simpler
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting Simpler properties")
    def get_simpler_properties(ctx: Context, track_index: int, device_index: int) -> str:
        """Get Simpler device and sample properties: playback mode, voices, retrigger,
        sample markers, gain, warp settings, slicing config, and all warp engine parameters.

        Parameters:
        - track_index: The index of the track containing the Simpler
        - device_index: The index of the Simpler device on the track
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_simpler_properties", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting Simpler properties")
    def set_simpler_properties(ctx: Context, track_index: int, device_index: int,
                                playback_mode: int = None, voices: int = None,
                                retrigger: bool = None, slicing_playback_mode: int = None,
                                start_marker: int = None, end_marker: int = None,
                                gain: float = None, warp_mode: int = None,
                                warping: bool = None, slicing_style: int = None,
                                slicing_sensitivity: float = None,
                                slicing_beat_division: int = None,
                                beats_granulation_resolution: int = None,
                                beats_transient_envelope: float = None,
                                beats_transient_loop_mode: int = None,
                                complex_pro_formants: float = None,
                                complex_pro_envelope: float = None,
                                texture_grain_size: float = None,
                                texture_flux: float = None,
                                tones_grain_size: float = None) -> str:
        """Set Simpler device and sample properties. All parameters are optional.

        Parameters:
        - track_index, device_index: Identify the Simpler device
        - playback_mode: 0=Classic, 1=One-Shot, 2=Slicing
        - voices: Number of polyphony voices
        - retrigger: True/False for retrigger mode
        - slicing_playback_mode: 0=Mono, 1=Poly, 2=Thru
        - start_marker, end_marker: Sample start/end in sample time
        - gain: Sample gain
        - warp_mode: Warp mode index
        - warping: True/False to enable warping
        - slicing_style: 0=Transient, 1=Beat, 2=Region, 3=Manual
        - slicing_sensitivity: 0.0-1.0 sensitivity for auto-slicing
        - slicing_beat_division: Beat division index for beat slicing
        - beats_granulation_resolution, beats_transient_envelope, beats_transient_loop_mode: Beats warp params
        - complex_pro_formants, complex_pro_envelope: Complex Pro warp params
        - texture_grain_size, texture_flux: Texture warp params
        - tones_grain_size: Tones warp param

        Use get_simpler_properties first to see current values and available options.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        params = {"track_index": track_index, "device_index": device_index}
        local_vars = {
            "playback_mode": playback_mode, "voices": voices, "retrigger": retrigger,
            "slicing_playback_mode": slicing_playback_mode,
            "start_marker": start_marker, "end_marker": end_marker, "gain": gain,
            "warp_mode": warp_mode, "warping": warping,
            "slicing_style": slicing_style, "slicing_sensitivity": slicing_sensitivity,
            "slicing_beat_division": slicing_beat_division,
            "beats_granulation_resolution": beats_granulation_resolution,
            "beats_transient_envelope": beats_transient_envelope,
            "beats_transient_loop_mode": beats_transient_loop_mode,
            "complex_pro_formants": complex_pro_formants,
            "complex_pro_envelope": complex_pro_envelope,
            "texture_grain_size": texture_grain_size, "texture_flux": texture_flux,
            "tones_grain_size": tones_grain_size,
        }
        for k, v in local_vars.items():
            if v is not None:
                params[k] = v
        ableton = get_ableton_connection()
        result = ableton.send_command("set_simpler_properties", params)
        device_name = result.get("device_name", "?")
        changes = [f"{k}={v}" for k, v in result.items()
                   if k not in ("track_index", "device_index", "device_name")]
        return f"Simpler '{device_name}' updated: {', '.join(changes) if changes else 'no changes'}"

    @mcp.tool()
    @_tool_handler("performing Simpler action")
    def simpler_sample_action(ctx: Context, track_index: int, device_index: int,
                               action: str, beats: float = None) -> str:
        """Perform an action on a Simpler device's loaded sample.

        Parameters:
        - track_index: The track containing the Simpler
        - device_index: The Simpler device index
        - action: 'reverse' (reverse the sample), 'crop' (crop to start/end markers),
                  'warp_as' (warp sample to specified beat count), 'warp_double' (double the warp length),
                  'warp_half' (halve the warp length)
        - beats: Required for 'warp_as' — number of beats to warp the sample to
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if action not in ("reverse", "crop", "warp_as", "warp_double", "warp_half"):
            return "action must be 'reverse', 'crop', 'warp_as', 'warp_double', or 'warp_half'"
        params = {"track_index": track_index, "device_index": device_index, "action": action}
        if beats is not None:
            params["beats"] = beats
        ableton = get_ableton_connection()
        result = ableton.send_command("simpler_sample_action", params)
        device_name = result.get("device_name", "?")
        return f"Simpler '{device_name}': {action} completed"

    @mcp.tool()
    @_tool_handler("managing sample slices")
    def manage_sample_slices(ctx: Context, track_index: int, device_index: int,
                              action: str, slice_time: int = None,
                              new_time: int = None) -> str:
        """Manage slice points on a Simpler device's sample.

        Parameters:
        - track_index: The track containing the Simpler
        - device_index: The Simpler device index
        - action: 'insert' (add a slice at slice_time), 'move' (move slice from slice_time to new_time),
                  'remove' (remove slice at slice_time), 'clear' (remove all slices), 'reset' (reset to default slices)
        - slice_time: Required for insert, move, remove — the slice time position in sample time
        - new_time: Required for move — the destination time position
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if action not in ("insert", "move", "remove", "clear", "reset"):
            return "action must be 'insert', 'move', 'remove', 'clear', or 'reset'"
        params = {"track_index": track_index, "device_index": device_index, "action": action}
        if slice_time is not None:
            params["slice_time"] = slice_time
        if new_time is not None:
            params["new_time"] = new_time
        ableton = get_ableton_connection()
        result = ableton.send_command("manage_sample_slices", params)
        device_name = result.get("device_name", "?")
        count = result.get("slice_count", "?")
        return f"Simpler '{device_name}': {action} done ({count} slices)"

    # ------------------------------------------------------------------
    # Transmute
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting Transmute properties")
    def get_transmute_properties(ctx: Context, track_index: int, device_index: int) -> str:
        """Get Transmute-specific properties: frequency dial mode, pitch mode, mod mode,
        mono/poly mode, MIDI gate mode, polyphony, and pitch bend range.
        Each mode property includes the current index and a list of available options.

        Parameters:
        - track_index: The index of the track containing the Transmute device
        - device_index: The index of the Transmute device on the track
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_transmute_properties", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting Transmute properties")
    def set_transmute_properties(ctx: Context, track_index: int, device_index: int,
                                  frequency_dial_mode_index: int = None,
                                  pitch_mode_index: int = None,
                                  mod_mode_index: int = None,
                                  mono_poly_index: int = None,
                                  midi_gate_index: int = None,
                                  polyphony: int = None,
                                  pitch_bend_range: int = None) -> str:
        """Set Transmute-specific properties. All parameters are optional — only specified values are changed.

        Parameters:
        - track_index: The index of the track containing the Transmute device
        - device_index: The index of the Transmute device on the track
        - frequency_dial_mode_index: Index into frequency_dial_mode_list
        - pitch_mode_index: Index into pitch_mode_list
        - mod_mode_index: Index into mod_mode_list
        - mono_poly_index: Index into mono_poly_list (0=Mono, 1=Poly typically)
        - midi_gate_index: Index into midi_gate_list
        - polyphony: Number of polyphony voices
        - pitch_bend_range: Pitch bend range in semitones

        Use get_transmute_properties first to see available mode lists and current values.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        params = {"track_index": track_index, "device_index": device_index}
        if frequency_dial_mode_index is not None:
            params["frequency_dial_mode_index"] = frequency_dial_mode_index
        if pitch_mode_index is not None:
            params["pitch_mode_index"] = pitch_mode_index
        if mod_mode_index is not None:
            params["mod_mode_index"] = mod_mode_index
        if mono_poly_index is not None:
            params["mono_poly_index"] = mono_poly_index
        if midi_gate_index is not None:
            params["midi_gate_index"] = midi_gate_index
        if polyphony is not None:
            params["polyphony"] = polyphony
        if pitch_bend_range is not None:
            params["pitch_bend_range"] = pitch_bend_range
        ableton = get_ableton_connection()
        result = ableton.send_command("set_transmute_properties", params)
        device_name = result.get("device_name", "?")
        changes = [f"{k}={v}" for k, v in result.items()
                   if k not in ("track_index", "device_index", "device_name")]
        return f"Transmute '{device_name}' updated: {', '.join(changes) if changes else 'no changes'}"

    # ------------------------------------------------------------------
    # Drum Rack pads
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("setting drum pad")
    def set_drum_pad(ctx: Context, track_index: int, device_index: int,
                      note: int, mute: bool = None, solo: bool = None) -> str:
        """Set mute or solo state on a drum pad by MIDI note number.

        Parameters:
        - track_index: The index of the track containing the Drum Rack
        - device_index: The index of the Drum Rack device on the track
        - note: MIDI note number (0-127) identifying the pad (e.g. 36=C1 kick)
        - mute: True to mute the pad, False to unmute. Optional.
        - solo: True to solo the pad, False to unsolo. Optional.

        Use get_drum_pads first to see available pads and their note numbers.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_range(note, "note", 0, 127)
        params = {"track_index": track_index, "device_index": device_index, "note": note}
        if mute is not None:
            params["mute"] = mute
        if solo is not None:
            params["solo"] = solo
        ableton = get_ableton_connection()
        result = ableton.send_command("set_drum_pad", params)
        return f"Drum pad '{result.get('name', '?')}' (note {note}): mute={result.get('mute')}, solo={result.get('solo')}"

    @mcp.tool()
    @_tool_handler("copying drum pad")
    def copy_drum_pad(ctx: Context, track_index: int, device_index: int,
                       source_note: int, dest_note: int) -> str:
        """Copy the contents of one drum pad to another.

        Parameters:
        - track_index: The index of the track containing the Drum Rack
        - device_index: The index of the Drum Rack device on the track
        - source_note: MIDI note of the pad to copy FROM (0-127)
        - dest_note: MIDI note of the pad to copy TO (0-127)

        Copies the device chain (instrument + effects) from the source pad
        to the destination pad. The destination pad's previous contents are replaced.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_range(source_note, "source_note", 0, 127)
        _validate_range(dest_note, "dest_note", 0, 127)
        ableton = get_ableton_connection()
        result = ableton.send_command("copy_drum_pad", {
            "track_index": track_index,
            "device_index": device_index,
            "source_note": source_note,
            "dest_note": dest_note,
        })
        return f"Copied drum pad from note {source_note} ('{result.get('source_name', '?')}') to note {dest_note}"

    @mcp.tool()
    @_tool_handler("getting drum pads")
    def get_drum_pads(ctx: Context, track_index: int, device_index: int) -> str:
        """Get information about all drum pads in a Drum Rack device.

        Parameters:
        - track_index: The index of the track containing the Drum Rack
        - device_index: The index of the Drum Rack device on the track

        Returns a list of pads with their MIDI note number, name, mute, and solo states.
        Use this to inspect drum pad assignments before modifying them with set_drum_pad.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_drum_pads", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Rack variations
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting rack variations")
    def get_rack_variations(ctx: Context, track_index: int, device_index: int) -> str:
        """Get variation info for a Rack device (macro snapshots).

        Parameters:
        - track_index: The index of the track containing the Rack
        - device_index: The index of the Rack device

        Returns the number of stored variations, which variation is currently selected,
        and whether the rack has macro mappings. Use with rack_variation_action to
        store, recall, or delete variations.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_rack_variations", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("performing rack variation action")
    def rack_variation_action(ctx: Context, track_index: int, device_index: int,
                               action: str, variation_index: int = None) -> str:
        """Perform a variation action on a Rack device (macro snapshots).

        Parameters:
        - track_index: The index of the track containing the Rack
        - device_index: The index of the Rack device
        - action: One of 'store' (save current macros as new variation),
                  'recall' (load a stored variation), 'delete' (remove a variation),
                  'randomize' (randomize all macro values)
        - variation_index: Required for 'recall' and 'delete'. The 0-based variation index.

        Use get_rack_variations first to see how many variations exist.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if action not in ("store", "recall", "delete", "randomize"):
            raise ValueError("action must be 'store', 'recall', 'delete', or 'randomize'")
        if action in ("recall", "delete") and variation_index is None:
            raise ValueError(f"variation_index is required for '{action}'")
        params = {
            "track_index": track_index,
            "device_index": device_index,
            "action": action,
        }
        if variation_index is not None:
            params["variation_index"] = variation_index
        ableton = get_ableton_connection()
        result = ableton.send_command("rack_variation_action", params)
        device_name = result.get("device_name", "?")
        if action == "store":
            return f"Stored new variation on '{device_name}' (now {result.get('variation_count', '?')} variations)"
        elif action == "recall":
            return f"Recalled variation {variation_index} on '{device_name}'"
        elif action == "delete":
            return f"Deleted variation {variation_index} from '{device_name}' ({result.get('variation_count', '?')} remaining)"
        else:
            return f"Randomized macros on '{device_name}'"

    # ------------------------------------------------------------------
    # Rack macro values
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting macro values")
    def get_macro_values(ctx: Context, track_index: int, device_index: int) -> str:
        """Get the current macro knob values for an Instrument Rack.

        Parameters:
        - track_index: The index of the track containing the device
        - device_index: The index of the device on the track
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_macro_values", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting rack macro")
    def set_rack_macro(ctx: Context, track_index: int, device_index: int,
                       macro_index: int, value: float,
                       track_type: str = "track") -> str:
        """Set the value of a specific macro knob on an Instrument/Audio Effect Rack.

        Parameters:
        - track_index: The index of the track containing the Rack
        - device_index: The index of the Rack device on the track
        - macro_index: Which macro to set (0-based, typically 0-7 or 0-15)
        - value: Target value (0.0 to 1.0)
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if macro_index < 0:
            raise ValueError("macro_index must be >= 0")
        if not (0.0 <= value <= 1.0):
            raise ValueError("value must be between 0.0 and 1.0")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("set_macro_value", {
            "track_index": track_index,
            "device_index": device_index,
            "macro_index": macro_index,
            "value": value,
            "track_type": track_type,
        })
        macro_name = result.get("macro_name", f"Macro {macro_index + 1}")
        return f"Set {macro_name} to {result.get('value', value):.3f} on track {track_index} device {device_index}"

    # ------------------------------------------------------------------
    # Chain selector & chain operations
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting chain selector")
    def get_chain_selector(ctx: Context, track_index: int, device_index: int,
                            track_type: str = "track") -> str:
        """Get the chain selector value and range for a Rack device.

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("get_chain_selector", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting chain selector")
    def set_chain_selector(ctx: Context, track_index: int, device_index: int,
                            value: float, track_type: str = "track") -> str:
        """Set the chain selector value for a Rack device.

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - value: The chain selector value (typically 0-127)
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("set_chain_selector", {
            "track_index": track_index,
            "device_index": device_index,
            "value": value,
            "track_type": track_type,
        })
        return f"Set chain selector to {result.get('chain_selector', value)}"

    @mcp.tool()
    @_tool_handler("inserting chain")
    def insert_chain(ctx: Context, track_index: int, device_index: int,
                      index: int = 0, track_type: str = "track") -> str:
        """Insert a new chain into a Rack device (Live 12.3+).

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - index: Position to insert the chain at (default: 0)
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_index(index, "index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("insert_chain", {
            "track_index": track_index,
            "device_index": device_index,
            "index": index,
            "track_type": track_type,
        })
        return f"Inserted new chain at index {index} in rack on track {track_index}"

    @mcp.tool()
    @_tool_handler("inserting device into chain")
    def chain_insert_device(ctx: Context, track_index: int, device_index: int,
                             chain_index: int, device_name: str,
                             target_index: int = None,
                             track_type: str = "track") -> str:
        """Insert a device into a chain of a Rack device (Live 12.3+).

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - chain_index: The index of the chain within the rack
        - device_name: The browser name/URI of the device to insert
        - target_index: Position in the chain's device list to insert at (default: end)
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_index(chain_index, "chain_index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        params = {
            "track_index": track_index,
            "device_index": device_index,
            "chain_index": chain_index,
            "device_name": device_name,
            "track_type": track_type,
        }
        if target_index is not None:
            _validate_index(target_index, "target_index")
            params["target_index"] = target_index
        ableton = get_ableton_connection()
        result = ableton.send_command("chain_insert_device", params)
        return f"Inserted '{device_name}' into chain {chain_index} of rack on track {track_index}"

    @mcp.tool()
    @_tool_handler("deleting chain device")
    def delete_chain_device(ctx: Context, track_index: int, device_index: int,
                             chain_index: int, chain_device_index: int,
                             track_type: str = "track") -> str:
        """Delete a device from within a chain of a Rack device.

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - chain_index: The index of the chain within the rack
        - chain_device_index: The index of the device within the chain to delete
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_index(chain_index, "chain_index")
        _validate_index(chain_device_index, "chain_device_index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_chain_device", {
            "track_index": track_index,
            "device_index": device_index,
            "chain_index": chain_index,
            "chain_device_index": chain_device_index,
            "track_type": track_type,
        })
        return f"Deleted device {chain_device_index} from chain {chain_index} of rack on track {track_index}"

    @mcp.tool()
    @_tool_handler("setting chain properties")
    def set_chain_properties(ctx: Context, track_index: int, device_index: int,
                              chain_index: int, mute: bool = None, solo: bool = None,
                              name: str = None, color_index: int = None,
                              volume: float = None, panning: float = None,
                              track_type: str = "track") -> str:
        """Set properties of a chain within a Rack device.

        Parameters:
        - track_index: The index of the track
        - device_index: The index of the Rack device
        - chain_index: The index of the chain
        - mute: Mute state of the chain
        - solo: Solo state of the chain
        - name: Name of the chain
        - color_index: Color index (0-69)
        - volume: Chain volume (0.0 to 1.0)
        - panning: Chain panning (-1.0 to 1.0)
        - track_type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_index(chain_index, "chain_index")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        params = {
            "track_index": track_index,
            "device_index": device_index,
            "chain_index": chain_index,
            "track_type": track_type,
        }
        if mute is not None:
            params["mute"] = mute
        if solo is not None:
            params["solo"] = solo
        if name is not None:
            params["name"] = name
        if color_index is not None:
            _validate_range(color_index, "color_index", 0, 69)
            params["color_index"] = int(color_index)
        if volume is not None:
            _validate_range(volume, "volume", 0.0, 1.0)
            params["volume"] = volume
        if panning is not None:
            _validate_range(panning, "panning", -1.0, 1.0)
            params["panning"] = panning
        ableton = get_ableton_connection()
        result = ableton.send_command("set_chain_properties", params)
        changed = result.get("changed", [])
        return f"Updated chain {chain_index} properties: {', '.join(changed) if changed else 'no changes'}"

    @mcp.tool()
    @_tool_handler("moving device")
    def move_device(ctx: Context, track_index: int, device_index: int,
                     dest_track_index: int, dest_position: int,
                     track_type: str = "track") -> str:
        """Move a device from one track/position to another.

        Parameters:
        - track_index: Source track index
        - device_index: Index of the device to move
        - dest_track_index: Destination track index
        - dest_position: Position in the destination track's device chain
        - track_type: Source track type: "track" (default), "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        _validate_index(dest_track_index, "dest_track_index")
        _validate_index(dest_position, "dest_position")
        if track_type not in ("track", "return", "master"):
            raise ValueError("track_type must be 'track', 'return', or 'master'")
        ableton = get_ableton_connection()
        result = ableton.send_command("move_device", {
            "track_index": track_index,
            "device_index": device_index,
            "dest_track_index": dest_track_index,
            "dest_position": dest_position,
            "track_type": track_type,
        })
        return f"Moved device from track {track_index} position {device_index} to track {dest_track_index} position {dest_position}"

    # ------------------------------------------------------------------
    # Audio-to-MIDI conversion tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("converting audio to MIDI")
    def audio_to_midi(ctx: Context, track_index: int, clip_index: int,
                       conversion_type: str) -> str:
        """Convert an audio clip to a MIDI clip using Ableton's audio-to-MIDI algorithms.

        Parameters:
        - track_index: The index of the track containing the audio clip
        - clip_index: The index of the clip slot containing the audio clip
        - conversion_type: 'drums' (percussive audio to drum MIDI),
                           'harmony' (polyphonic audio to chord MIDI),
                           'melody' (monophonic audio to single-note MIDI)

        Creates a new MIDI track with the converted clip. The original audio clip
        is not modified. This is equivalent to right-clicking an audio clip and
        selecting "Convert Drums/Harmony/Melody to New MIDI Track" in Ableton.

        Requires Live 12+.
        """
        _validate_index(track_index, "track_index")
        _validate_index(clip_index, "clip_index")
        if conversion_type not in ("drums", "harmony", "melody"):
            raise ValueError("conversion_type must be 'drums', 'harmony', or 'melody'")
        ableton = get_ableton_connection()
        result = ableton.send_command("audio_to_midi", {
            "track_index": track_index,
            "clip_index": clip_index,
            "conversion_type": conversion_type,
        }, timeout=30.0)
        return f"Converted audio clip '{result.get('source_clip', '?')}' to MIDI ({conversion_type}). A new MIDI track was created."

    @mcp.tool()
    @_tool_handler("creating MIDI track with Simpler")
    def create_midi_track_with_simpler(ctx: Context, track_index: int, clip_index: int) -> str:
        """Create a new MIDI track with a Simpler instrument loaded with an audio clip's sample.

        Parameters:
        - track_index: The index of the track containing the source audio clip
        - clip_index: The index of the clip slot containing the audio clip

        Creates a new MIDI track with a Simpler device that has the audio clip's
        sample loaded. You can then play the sample chromatically via MIDI.

        Requires Live 12+.
        """
        _validate_index(track_index, "track_index")
        _validate_index(clip_index, "clip_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track_with_simpler", {
            "track_index": track_index,
            "clip_index": clip_index,
        }, timeout=20.0)
        return f"Created MIDI track with Simpler from audio clip '{result.get('source_clip', '?')}'"

    @mcp.tool()
    @_tool_handler("converting Simpler to Drum Rack")
    def sliced_simpler_to_drum_rack(ctx: Context, track_index: int, device_index: int) -> str:
        """Convert a sliced Simpler device into a Drum Rack.

        Parameters:
        - track_index: The index of the track containing the Simpler
        - device_index: The index of the Simpler device on the track

        The Simpler must be in Slicing mode (not Classic or One-Shot).
        Each slice becomes a separate pad in the Drum Rack, allowing
        independent processing and effects per slice.

        Requires Live 12+.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("sliced_simpler_to_drum_rack", {
            "track_index": track_index,
            "device_index": device_index,
        }, timeout=20.0)
        return f"Converted Simpler '{result.get('source_device', '?')}' to Drum Rack"

    # ------------------------------------------------------------------
    # Tuning system
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("getting tuning system")
    def get_tuning_system(ctx: Context) -> str:
        """Get the current tuning system: name, pseudo-octave in cents,
        reference pitch, and note tunings. Useful for microtonal music."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_tuning_system", {})
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Looper
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("controlling looper")
    def control_looper(ctx: Context, track_index: int, device_index: int,
                        action: str, clip_slot_index: int = None) -> str:
        """Control a Looper device with specialized actions.

        Parameters:
        - track_index: Track containing the Looper
        - device_index: Device index of the Looper
        - action: 'record', 'overdub', 'play', 'stop', 'clear', 'undo',
                  'double_speed', 'half_speed', 'double_length', 'half_length',
                  'export' (exports to a clip slot, requires clip_slot_index)
        - clip_slot_index: Required for 'export' action — the target clip slot
        """
        params = {"track_index": track_index, "device_index": device_index, "action": action}
        if clip_slot_index is not None:
            params["clip_slot_index"] = clip_slot_index
        ableton = get_ableton_connection()
        result = ableton.send_command("control_looper", params)
        return json.dumps(result)

    # ------------------------------------------------------------------
    # Device LOM property tools (M4L)
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("listing device properties")
    def list_device_properties(
        ctx: Context,
        track_index: int,
        device_index: int
    ) -> str:
        """List all known LOM properties for a device from the knowledge base.

        Shows available device-level properties including their types, valid
        values, descriptions, and whether they are settable or read-only.

        Currently supported: Wavetable (InstrumentVector).
        More devices will be added over time.

        Use get_device_property() / set_device_property() to read/write these.
        Use discover_device_params() for indexed parameters instead.

        Requires the AbletonBridge M4L device to be loaded on any track.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")

        m4l = get_m4l_connection()
        result = m4l.send_command("get_device_property", {
            "track_index": track_index,
            "device_index": device_index,
            "property_name": "class_name"
        })

        data = _m4l_result(result)
        device_name = data.get("device_name", "Unknown")
        device_class = data.get("value", "Unknown")

        if device_class not in DEVICE_PROPERTIES:
            supported = ", ".join(sorted(DEVICE_PROPERTIES.keys()))
            return (
                f"Device: {device_name} ({device_class})\n"
                f"No property knowledge base for class '{device_class}'.\n"
                f"You can still use get_device_property() / set_device_property() "
                f"with any valid LOM property name.\n"
                f"Supported classes: {supported}"
            )

        props = DEVICE_PROPERTIES[device_class]
        settable = {k: v for k, v in props.items() if not v.get("readonly")}
        readonly = {k: v for k, v in props.items() if v.get("readonly")}

        msg = f"Device: {device_name} ({device_class})\n"
        msg += f"Known properties: {len(props)} ({len(settable)} settable, {len(readonly)} read-only)\n"

        if settable:
            msg += "\n--- Settable Properties ---\n"
            for pname, info in settable.items():
                msg += f"\n  {pname}"
                if info.get("description"):
                    msg += f" — {info['description']}"
                if info["type"] == "enum" and "values" in info:
                    opts = ", ".join(f"{k}={v}" for k, v in sorted(info["values"].items()))
                    msg += f"\n    Values: {opts}"
                elif info["type"] in ("int", "float"):
                    parts = []
                    if "min" in info:
                        parts.append(f"min={info['min']}")
                    if "max" in info:
                        parts.append(f"max={info['max']}")
                    if parts:
                        msg += f"\n    Range: {', '.join(parts)}"
                if info.get("note"):
                    msg += f"\n    Note: {info['note']}"

        if readonly:
            msg += "\n\n--- Read-Only Properties ---\n"
            for pname, info in readonly.items():
                msg += f"\n  {pname}"
                if info.get("description"):
                    msg += f" — {info['description']}"

        return msg

    @mcp.tool()
    @_tool_handler("getting device property")
    def get_device_property(
        ctx: Context,
        track_index: int,
        device_index: int,
        property_name: str
    ) -> str:
        """Read a device-level LOM property (not an indexed parameter).

        Reads properties directly on the device object in the Live Object Model.
        For supported devices (currently: Wavetable/InstrumentVector), the response
        includes human-readable labels, descriptions, and available options.

        Use list_device_properties() to see all known properties for a device.
        Use discover_device_params() for indexed parameters instead.

        Requires the AbletonBridge M4L device to be loaded on any track.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if not isinstance(property_name, str) or not property_name.strip():
            raise ValueError("property_name must be a non-empty string.")

        m4l = get_m4l_connection()
        result = m4l.send_command("get_device_property", {
            "track_index": track_index,
            "device_index": device_index,
            "property_name": property_name.strip()
        })

        data = _m4l_result(result)
        device_name = data.get("device_name", "Unknown")
        device_class = data.get("device_class", "Unknown")
        prop = data.get("property_name", property_name)
        val = data.get("value", "?")

        # Look up property in knowledge base
        prop_info = _get_property_info(device_class, prop)
        val_str = _format_property_value(prop_info, val)

        msg = f"Device: {device_name} ({device_class})\n"
        msg += f"Property '{prop}' = {val_str}"

        if prop_info:
            if prop_info.get("description"):
                msg += f"\n  {prop_info['description']}"
            if prop_info.get("readonly"):
                msg += "\n  (read-only)"
            options = _format_property_options(prop_info)
            if options:
                msg += f"\n  {options}"
            if prop_info.get("note"):
                msg += f"\n  Note: {prop_info['note']}"

        return msg

    @mcp.tool()
    @_tool_handler("setting device property")
    def set_device_property(
        ctx: Context,
        track_index: int,
        device_index: int,
        property_name: str,
        value: float
    ) -> str:
        """Set a device-level LOM property (not an indexed parameter).

        Sets properties directly on the device object in the Live Object Model.
        For supported devices (currently: Wavetable/InstrumentVector), the value
        is validated against the knowledge base before sending.

        Use list_device_properties() to see all known properties and valid values.
        Use get_device_property() to read the current value first.

        Requires the AbletonBridge M4L device to be loaded on any track.
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if not isinstance(property_name, str) or not property_name.strip():
            raise ValueError("property_name must be a non-empty string.")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("value must be a number.")

        prop_name = property_name.strip()
        m4l = get_m4l_connection()

        # Pre-validate: get device class, then check knowledge base
        class_result = m4l.send_command("get_device_property", {
            "track_index": track_index,
            "device_index": device_index,
            "property_name": "class_name"
        })
        device_class = None
        class_data = _m4l_result(class_result)
        device_class = class_data.get("value")
        if isinstance(device_class, str):
            prop_info = _get_property_info(device_class, prop_name)
            _validate_property_value(prop_info, prop_name, value)

        result = m4l.send_command("set_device_property", {
            "track_index": track_index,
            "device_index": device_index,
            "property_name": prop_name,
            "value": float(value)
        })

        data = _m4l_result(result)
        device_name = data.get("device_name", "Unknown")
        resp_class = data.get("device_class", device_class or "Unknown")
        prop = data.get("property_name", prop_name)
        old_val = data.get("old_value", "?")
        new_val = data.get("new_value", "?")
        success = data.get("success", False)

        prop_info = _get_property_info(resp_class, prop)
        old_str = _format_property_value(prop_info, old_val)
        new_str = _format_property_value(prop_info, new_val)

        msg = (
            f"Device: {device_name} ({resp_class})\n"
            f"Property '{prop}': {old_str} -> {new_str}"
        )
        if not success:
            msg += " (WARNING: value may not have changed — property might be read-only or value out of range)"
        return msg

    # ------------------------------------------------------------------
    # View / selection tools
    # ------------------------------------------------------------------

    @mcp.tool()
    @_tool_handler("selecting device")
    def select_device_in_view(ctx: Context, track_index: int, device_index: int,
                                track_type: str = "track") -> str:
        """Select a device to show in Ableton's detail view.

        Parameters:
        - track_index: The track index
        - device_index: The device index on the track
        - track_type: "track", "return", or "master"
        """
        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("select_device", {
            "track_index": track_index, "device_index": device_index,
            "track_type": track_type,
        })
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting selected parameter")
    def get_selected_parameter(ctx: Context) -> str:
        """Get the currently selected parameter in Ableton's detail view."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_selected_parameter", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("selecting instrument")
    def select_instrument(ctx: Context, track_index: int) -> str:
        """Select and show the first instrument device on a track.

        Parameters:
        - track_index: The track index
        """
        _validate_index(track_index, "track_index")
        ableton = get_ableton_connection()
        result = ableton.send_command("select_instrument", {"track_index": track_index})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting appointed device")
    def get_appointed_device(ctx: Context) -> str:
        """Get info about the currently selected/appointed device."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_appointed_device", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting bridge status")
    def get_bridge_status(ctx: Context) -> str:
        """
        Check which parameter transport layers are currently active.

        AbletonBridge uses two tiers for standard device parameter control:
          1. Extensions SDK bridge (optional, best for VST3/AU, Live 12.4.5+ Suite)
          2. _Framework Remote Script (always active)

        The M4L bridge is a parallel tier for hidden/undiscoverable parameters
        (discover_params, get_hidden_params) — not a fallback in the standard flow.

        get_device_parameters and set_device_parameter prefer SDK when available.
        """
        lines = []

        # Extensions SDK
        sdk = get_sdk_client()
        if sdk:
            version = sdk.get_version()
            try:
                tracks = sdk.get_tracks()
                track_count = len(tracks.get("tracks", []))
            except Exception:
                track_count = "?"
            lines.append(
                f"Extensions SDK bridge: ACTIVE (HTTP port 9883, v{version})\n"
                f"  → get_device_parameters / set_device_parameter prefer this tier\n"
                f"  → Tracks visible: {track_count}"
            )
        else:
            lines.append(
                "Extensions SDK bridge: NOT RUNNING (port 9883)\n"
                "  → Requires AbletonParameterBridge + Live 12.4.5+ Suite\n"
                "  → See docs/extensions_sdk_bridge.md for setup"
            )

        # M4L
        try:
            from MCP_Server.connections.m4l import get_m4l_connection
            m4l = get_m4l_connection()
            lines.append(
                f"M4L bridge: ACTIVE (UDP 9878/9879, v{state.m4l_bridge_version or 'unknown'})\n"
                "  → Provides hidden params, rack internals, audio analysis"
            )
        except Exception:
            lines.append(
                "M4L bridge: NOT LOADED\n"
                "  → Load the AbletonBridge M4L device on any track in Ableton"
            )

        # _Framework
        from MCP_Server.connections.ableton import get_ableton_connection
        try:
            ableton = get_ableton_connection()
            lines.append("_Framework Remote Script: ACTIVE (TCP 9877) — base layer, always available")
        except Exception:
            lines.append("_Framework Remote Script: NOT CONNECTED (TCP 9877)")

        return "\n\n".join(lines)

