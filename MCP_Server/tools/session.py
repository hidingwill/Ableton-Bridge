"""Session & transport tool handlers for AbletonBridge."""
import json
from mcp.server.fastmcp import Context
from MCP_Server.tools._base import _tool_handler, _m4l_result, tool_error, tool_success
from MCP_Server.connections.ableton import get_ableton_connection
from MCP_Server.connections.m4l import get_m4l_connection
from MCP_Server.validation import _validate_index, _validate_index_allow_negative, _validate_range
import MCP_Server.state as state
import MCP_Server.ownership as ownership
from MCP_Server.status import build_connection_status


def register_tools(mcp):
    """Register session & transport tools with the MCP server."""

    @mcp.tool()
    @_tool_handler("getting server capabilities", requires_control=False)
    def get_server_capabilities(ctx: Context) -> str:
        """Report server version, ownership-aware connections, features, and tools.

        Call this first in any session to understand what features are available.
        Owner connection booleans are true or false. Standby connection booleans
        are null, with explicit connection-state fields explaining why.
        """
        from MCP_Server import __version__
        control = ownership.get_status()
        connections = build_connection_status(control)

        return json.dumps({
            "server_version": __version__,
            **control,
            **connections,
            "browser_cache_ready": state.browser_cache_ready.is_set(),
            "browser_cache_items": len(state.browser_cache_flat),
            "tool_count": len(mcp._tool_manager._tools) if hasattr(mcp, '_tool_manager') else 0,
            "features": {
                "grid_notation": True,
                "snapshots": True,
                "macros": True,
                "param_maps": True,
                "dashboard": state.dashboard_server is not None,
                "m4l_bridge": connections["m4l_connected"],
            },
            "store_counts": {
                "snapshots": len(state.snapshot_store),
                "macros": len(state.macro_store),
                "param_maps": len(state.param_map_store),
            },
        })


    @mcp.tool()
    @_tool_handler("releasing Ableton control", requires_control=False)
    def release_ableton_control(ctx: Context) -> str:
        """Release Ableton control owned by this MCP process.

        This never steals or releases another task's ownership. If this task is
        already on standby, the response is a harmless no-op.
        """
        result = ownership.release_control()
        data = {
            "released": result.released,
            "control": result.control,
        }
        if result.error and not result.released:
            return tool_error(result.error, data)
        if result.released:
            message = "Ableton control released."
            if result.error:
                message += f" Backend cleanup warning: {result.error}"
            return tool_success(message, data)
        return tool_success("This task did not own Ableton control.", data)


    @mcp.tool()
    @_tool_handler("getting session info")
    def get_session_info(ctx: Context) -> str:
        """Get detailed information about the current Ableton session"""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting song transport")
    def get_song_transport(ctx: Context) -> str:
        """
        Get the current transport/arrangement state of the Ableton session.

        Returns: current playback time, playing state, tempo, time signature,
        loop bracket settings, record mode, and song length.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_transport", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting loop info")
    def get_loop_info(ctx: Context) -> str:
        """Get loop bracket information including start, end, length, and current playback time."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_loop_info")
        return json.dumps(result)


    @mcp.tool()
    @_tool_handler("getting recording status")
    def get_recording_status(ctx: Context) -> str:
        """Get the current recording status including armed tracks, record mode, and overdub state."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_recording_status")
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting tempo")
    def set_tempo(ctx: Context, tempo: float) -> str:
        """
        Set the tempo of the Ableton session.

        Parameters:
        - tempo: The new tempo in BPM
        """
        _validate_range(tempo, "tempo", 20.0, 999.0)
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"

    @mcp.tool()
    @_tool_handler("tapping tempo")
    def tap_tempo(ctx: Context) -> str:
        """Tap tempo - call repeatedly to set tempo by tapping."""
        ableton = get_ableton_connection()
        result = ableton.send_command("tap_tempo")
        return f"Tap tempo registered. Current tempo: {result.get('tempo', '?')} BPM"

    @mcp.tool()
    @_tool_handler("starting playback")
    def start_playback(ctx: Context) -> str:
        """Start playing from the play position marker (like pressing Play). To resume
        from the current playhead without jumping, use continue_playing instead."""
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"

    @mcp.tool()
    @_tool_handler("stopping playback")
    def stop_playback(ctx: Context) -> str:
        """Stop playing the Ableton session."""
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"

    @mcp.tool()
    @_tool_handler("continuing playback")
    def continue_playing(ctx: Context) -> str:
        """Continue playback from the current position.

        Unlike start_playback which jumps to the play position, this resumes
        from exactly where the playhead is now. Useful after stopping to audition
        a section without losing your place.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("continue_playing")
        return f"Playback continued from beat {result.get('position', '?')}"

    @mcp.tool()
    @_tool_handler("setting song time")
    def set_song_time(ctx: Context, time: float) -> str:
        """
        Set the playback position (arrangement playhead). Moves the playhead
        to the specified beat position.

        Parameters:
        - time: The position in beats to jump to (0.0 = start of song)
        """
        ableton = get_ableton_connection()
        ableton.send_command("set_song_time", {"time": time})
        return f"Playhead set to beat {time}"

    @mcp.tool()
    @_tool_handler("setting song loop")
    def set_song_loop(ctx: Context, enabled: bool = None, start: float = None, length: float = None, end: float = None) -> str:
        """
        Control the arrangement loop bracket.

        Parameters:
        - enabled: True to enable looping, False to disable (optional)
        - start: Loop start position in beats (optional)
        - length: Loop length in beats (optional, mutually exclusive with end)
        - end: Loop end position in beats (optional, mutually exclusive with length). Computes length = end - start.
        """
        if length is not None and end is not None:
            raise ValueError("Provide either 'length' or 'end', not both")
        if end is not None:
            if start is None:
                raise ValueError("'start' is required when using 'end'")
            if end <= start:
                raise ValueError("'end' must be greater than 'start'")
            length = end - start
        params = {}
        if enabled is not None:
            params["enabled"] = enabled
        if start is not None:
            params["start"] = start
        if length is not None:
            params["length"] = length
        ableton = get_ableton_connection()
        result = ableton.send_command("set_song_loop", params)
        # Use the values we sent, with result as fallback
        loop_state = "enabled" if (enabled if enabled is not None else result.get("loop_enabled")) else "disabled"
        s = start if start is not None else result.get('loop_start', 0)
        l = length if length is not None else result.get('loop_length', 0)
        return f"Loop {loop_state}: start={s}, length={l} beats"

    @mcp.tool()
    @_tool_handler("setting metronome")
    def set_metronome(ctx: Context, enabled: bool) -> str:
        """Enable or disable the metronome.

        Parameters:
        - enabled: True to enable the metronome, False to disable
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_metronome", {"enabled": enabled})
        return f"Metronome {'enabled' if result.get('metronome', enabled) else 'disabled'}"

    @mcp.tool()
    @_tool_handler("setting arrangement overdub")
    def set_arrangement_overdub(ctx: Context, enabled: bool) -> str:
        """Enable or disable arrangement overdub mode.

        Parameters:
        - enabled: True to enable overdub, False to disable
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_overdub", {"enabled": enabled})
        return f"Arrangement overdub {'enabled' if result.get('overdub', enabled) else 'disabled'}"

    @mcp.tool()
    @_tool_handler("starting arrangement recording")
    def start_arrangement_recording(ctx: Context) -> str:
        """Start arrangement recording in Ableton."""
        ableton = get_ableton_connection()
        result = ableton.send_command("start_arrangement_recording")
        return "Arrangement recording started"

    @mcp.tool()
    @_tool_handler("stopping arrangement recording")
    def stop_arrangement_recording(ctx: Context) -> str:
        """Stop arrangement recording in Ableton."""
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_arrangement_recording")
        return "Arrangement recording stopped"

    @mcp.tool()
    @_tool_handler("performing undo")
    def undo(ctx: Context) -> str:
        """Undo the last action in Ableton.

        Useful for reverting changes made by previous tool calls. Returns whether
        the undo was performed or if there was nothing to undo.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("undo")
        if result.get("undone"):
            return "Undo performed"
        return f"Nothing to undo: {result.get('reason', 'unknown')}"

    @mcp.tool()
    @_tool_handler("performing redo")
    def redo(ctx: Context) -> str:
        """Redo the last undone action in Ableton.

        Re-applies a previously undone action.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("redo")
        if result.get("redone"):
            return "Redo performed"
        return f"Nothing to redo: {result.get('reason', 'unknown')}"

    @mcp.tool()
    @_tool_handler("re-enabling automation")
    def re_enable_automation(ctx: Context) -> str:
        """Re-enable all automation that has been manually overridden.

        When you manually adjust a parameter that has automation, Ableton disables
        the automation for that parameter (shown as an orange LED). This tool
        re-enables all overridden automation at once.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("re_enable_automation")
        return "All automation re-enabled"

    @mcp.tool()
    @_tool_handler("toggling cue point")
    def set_or_delete_cue(ctx: Context) -> str:
        """Toggle a cue point at the current playback position.

        If a cue point exists at the current position, it is deleted.
        Otherwise, a new cue point is created. Use set_song_time
        first to move the playhead to the desired location.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_or_delete_cue")
        return f"Cue point toggled at beat {result.get('position', '?')}"

    @mcp.tool()
    @_tool_handler("jumping to cue")
    def jump_to_cue(ctx: Context, direction: str) -> str:
        """Jump the playhead to the next or previous cue point.

        Parameters:
        - direction: 'next' to jump forward, 'prev' to jump backward
        """
        if direction not in ("next", "prev"):
            return "Error: direction must be 'next' or 'prev'"
        ableton = get_ableton_connection()
        result = ableton.send_command("jump_to_cue", {"direction": direction})
        if result.get("jumped"):
            return f"Jumped to {direction} cue point at beat {result.get('position', '?')}"
        return f"Cannot jump: {result.get('reason', 'no cue point found')}"

    @mcp.tool()
    @_tool_handler("capturing and inserting scene")
    def capture_and_insert_scene(ctx: Context) -> str:
        """Capture currently playing clips into a new scene (like Shift+New in Ableton)."""
        ableton = get_ableton_connection()
        result = ableton.send_command("capture_and_insert_scene", {})
        scene_idx = result.get("scene_index", "?")
        scene_name = result.get("scene_name", "?")
        return f"Captured playing clips into new scene {scene_idx}: '{scene_name}'"

    @mcp.tool()
    @_tool_handler("getting song file path")
    def get_song_file_path(ctx: Context) -> str:
        """Get the file path of the current Live Set."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_file_path", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting session record")
    def set_session_record(ctx: Context, enabled: bool) -> str:
        """Enable or disable session recording.

        Parameters:
        - enabled: True to start session recording, False to stop
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_session_record", {"enabled": enabled})
        state = "enabled" if result.get("session_record", enabled) else "disabled"
        return f"Session recording {state}"

    @mcp.tool()
    @_tool_handler("triggering session record")
    def trigger_session_record(ctx: Context, record_length: float = None) -> str:
        """Trigger a new session recording. Optionally specify a fixed bar length
        after which recording stops automatically.

        Parameters:
        - record_length: Optional number of bars to record. If omitted, recording continues until manually stopped.
        """
        params = {}
        if record_length is not None:
            params["record_length"] = record_length
        ableton = get_ableton_connection()
        result = ableton.send_command("trigger_session_record", params)
        if record_length is not None:
            return f"Session recording triggered for {record_length} bars"
        return "Session recording triggered"

    @mcp.tool()
    @_tool_handler("navigating playback")
    def navigate_playback(ctx: Context, action: str, beats: float = None) -> str:
        """Navigate the playback position: jump, scrub, or play selection.

        Parameters:
        - action: 'jump_by' (relative jump, stops playback), 'scrub_by' (relative jump, keeps playing), or 'play_selection' (play the current arrangement selection)
        - beats: Number of beats to jump/scrub (positive=forward, negative=backward). Required for jump_by and scrub_by.
        """
        if action not in ("jump_by", "scrub_by", "play_selection"):
            return "action must be 'jump_by', 'scrub_by', or 'play_selection'"
        params = {"action": action}
        if beats is not None:
            params["beats"] = beats
        ableton = get_ableton_connection()
        result = ableton.send_command("navigate_playback", params)
        pos = result.get("position", "?")
        if action == "play_selection":
            return f"Playing selection (position: {pos})"
        return f"{action} by {beats} beats (position: {pos})"

    @mcp.tool()
    @_tool_handler("stopping all clips")
    def stop_all_clips(ctx: Context) -> str:
        """Stop all playing clips in the Live Set."""
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_all_clips", {})
        return "All clips stopped"

    @mcp.tool()
    @_tool_handler("getting song settings")
    def get_song_settings(ctx: Context) -> str:
        """Get global song settings: time signature, swing amount, clip trigger quantization,
        MIDI recording quantization, arrangement overdub, back to arranger, follow song, and draw mode.
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_settings", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting song settings")
    def set_song_settings(ctx: Context,
                           signature_numerator: int = None,
                           signature_denominator: int = None,
                           swing_amount: float = None,
                           clip_trigger_quantization: int = None,
                           midi_recording_quantization: int = None,
                           back_to_arranger: bool = None,
                           follow_song: bool = None,
                           draw_mode: bool = None,
                           session_automation_record: bool = None) -> str:
        """Set global song settings. All parameters are optional — only specified values are changed.

        Parameters:
        - signature_numerator: Time signature numerator (1-99, e.g. 3 for 3/4)
        - signature_denominator: Time signature denominator (1, 2, 4, 8, or 16)
        - swing_amount: Global swing amount (0.0-1.0)
        - clip_trigger_quantization: Global clip launch quantization (0=None, 1=8 Bars, 2=4 Bars, 3=2 Bars, 4=1 Bar, 5=1/2, 6=1/2T, 7=1/4, 8=1/4T, 9=1/8, 10=1/8T, 11=1/16, 12=1/16T, 13=1/32)
        - midi_recording_quantization: MIDI input recording quantization (0=None, 1=1/4, 2=1/8, 3=1/8T, 4=1/8+1/8T, 5=1/16, 6=1/16T, 7=1/16+1/16T, 8=1/32)
        - back_to_arranger: If true, triggering a Session clip disables Arrangement playback
        - follow_song: If true, Arrangement view auto-scrolls to follow the play marker
        - draw_mode: If true, enables envelope/note draw mode
        - session_automation_record: If true, enables the Automation Arm button for session recording
        """
        params = {}
        if signature_numerator is not None:
            params["signature_numerator"] = signature_numerator
        if signature_denominator is not None:
            params["signature_denominator"] = signature_denominator
        if swing_amount is not None:
            _validate_range(swing_amount, "swing_amount", 0.0, 1.0)
            params["swing_amount"] = swing_amount
        if clip_trigger_quantization is not None:
            _validate_index(clip_trigger_quantization, "clip_trigger_quantization")
            params["clip_trigger_quantization"] = clip_trigger_quantization
        if midi_recording_quantization is not None:
            _validate_index(midi_recording_quantization, "midi_recording_quantization")
            params["midi_recording_quantization"] = midi_recording_quantization
        if back_to_arranger is not None:
            params["back_to_arranger"] = back_to_arranger
        if follow_song is not None:
            params["follow_song"] = follow_song
        if draw_mode is not None:
            params["draw_mode"] = draw_mode
        if session_automation_record is not None:
            params["session_automation_record"] = session_automation_record
        if not params:
            return "No parameters specified. Provide at least one setting to change."
        ableton = get_ableton_connection()
        result = ableton.send_command("set_song_settings", params)
        changes = [f"{k}={v}" for k, v in result.items()]
        return f"Song settings updated: {', '.join(changes)}"

    @mcp.tool()
    @_tool_handler("getting song scale")
    def get_song_scale(ctx: Context) -> str:
        """Get the song's current scale settings: root note (0-11, C=0), scale name,
        scale mode (on/off), and scale intervals. Essential for harmonically-aware
        MIDI generation and chord suggestions."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_scale", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting song scale")
    def set_song_scale(ctx: Context,
                        root_note: int = None,
                        scale_name: str = None,
                        scale_mode: bool = None) -> str:
        """Set the song's scale settings for harmonic awareness.

        Parameters:
        - root_note: Root note 0-11 (C=0, C#=1, D=2, D#=3, E=4, F=5, F#=6, G=7, G#=8, A=9, A#=10, B=11)
        - scale_name: Scale name as shown in Live (e.g. 'Major', 'Minor', 'Dorian', 'Mixolydian', 'Phrygian', 'Lydian', 'Locrian', 'Whole Tone', 'Diminished', 'Whole-Half', 'Minor Blues', 'Minor Pentatonic', 'Major Pentatonic', 'Harmonic Minor', 'Melodic Minor', 'Chromatic')
        - scale_mode: True to enable Scale Mode (highlights scale notes in MIDI editor)
        """
        params = {}
        if root_note is not None:
            _validate_range(root_note, "root_note", 0, 11)
            params["root_note"] = root_note
        if scale_name is not None:
            params["scale_name"] = scale_name
        if scale_mode is not None:
            params["scale_mode"] = scale_mode
        if not params:
            return "No parameters specified. Provide at least one scale setting."
        ableton = get_ableton_connection()
        result = ableton.send_command("set_song_scale", params)
        note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        parts = []
        if "root_note" in result:
            parts.append(f"root={note_names[result['root_note']]}")
        if "scale_name" in result:
            parts.append(f"scale={result['scale_name']}")
        if "scale_mode" in result:
            parts.append(f"mode={'on' if result['scale_mode'] else 'off'}")
        return f"Scale updated: {', '.join(parts)}"

    @mcp.tool()
    @_tool_handler("setting punch recording")
    def set_punch_recording(ctx: Context,
                             punch_in: bool = None,
                             punch_out: bool = None,
                             count_in_duration: int = None) -> str:
        """Control punch in/out recording and count-in settings.

        Parameters:
        - punch_in: Enable/disable punch-in (only record within the loop region)
        - punch_out: Enable/disable punch-out (stop recording at the loop end)
        - count_in_duration: Metronome count-in before recording (0=None, 1=1 Bar, 2=2 Bars, 3=4 Bars). Note: may be read-only in some Live versions.
        """
        params = {}
        if punch_in is not None:
            params["punch_in"] = punch_in
        if punch_out is not None:
            params["punch_out"] = punch_out
        if count_in_duration is not None:
            _validate_range(count_in_duration, "count_in_duration", 0, 3)
            params["count_in_duration"] = count_in_duration
        if not params:
            return "No parameters specified."
        ableton = get_ableton_connection()
        result = ableton.send_command("set_punch", params)
        changes = [f"{k}={v}" for k, v in result.items()]
        return f"Punch recording updated: {', '.join(changes)}"

    @mcp.tool()
    @_tool_handler("getting selection state")
    def get_selection_state(ctx: Context) -> str:
        """Get what is currently selected in Live's UI: the selected track, scene,
        detail clip, draw mode, and follow song state. Useful for context-aware assistance."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_selection_state", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting Link status")
    def get_link_status(ctx: Context) -> str:
        """Get Ableton Link sync status: whether Link is enabled and
        whether start/stop sync is active."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_link_status", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting Link")
    def set_link_enabled(ctx: Context,
                          enabled: bool = None,
                          start_stop_sync: bool = None) -> str:
        """Enable/disable Ableton Link tempo sync and start/stop synchronization.

        Parameters:
        - enabled: True to enable Link, False to disable
        - start_stop_sync: True to enable start/stop sync between Link peers
        """
        params = {}
        if enabled is not None:
            params["enabled"] = enabled
        if start_stop_sync is not None:
            params["start_stop_sync"] = start_stop_sync
        if not params:
            return "No parameters specified."
        ableton = get_ableton_connection()
        result = ableton.send_command("set_link_enabled", params)
        changes = [f"{k}={v}" for k, v in result.items()]
        return f"Link updated: {', '.join(changes)}"

    @mcp.tool()
    @_tool_handler("getting view state")
    def get_view_state(ctx: Context) -> str:
        """Get the current state of Live's application views: which views are visible
        (Browser, Arranger, Session, Detail, Detail/Clip, Detail/DeviceChain),
        the focused view, and whether Hot-Swap/browse mode is active."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_view_state", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting view")
    def set_view(ctx: Context,
                  action: str,
                  view_name: str = "") -> str:
        """Show, hide, or focus a view in Live's UI.

        Parameters:
        - action: 'show', 'hide', 'focus', or 'toggle_browse'
        - view_name: 'Browser', 'Arranger', 'Session', 'Detail', 'Detail/Clip', 'Detail/DeviceChain'
          (not needed for toggle_browse)
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_view", {"action": action, "view_name": view_name})
        return f"View {action}: {view_name}" if view_name else f"Browse mode toggled"

    @mcp.tool()
    @_tool_handler("zooming/scrolling view")
    def zoom_scroll_view(ctx: Context,
                          action: str,
                          direction: int,
                          view_name: str,
                          modifier_pressed: bool = False) -> str:
        """Zoom or scroll a view in Live's UI.

        Parameters:
        - action: 'zoom' or 'scroll'
        - direction: 0=up, 1=down, 2=left, 3=right
        - view_name: 'Arranger', 'Session', 'Browser', 'Detail/DeviceChain'
        - modifier_pressed: Modifies behavior (e.g. zoom only selected track height in Arranger)
        """
        _validate_range(direction, "direction", 0, 3)
        ableton = get_ableton_connection()
        result = ableton.send_command("zoom_scroll_view", {
            "action": action, "direction": direction,
            "view_name": view_name, "modifier_pressed": modifier_pressed
        })
        dirs = ["up", "down", "left", "right"]
        return f"View {action} {dirs[direction]}: {view_name}"

    @mcp.tool()
    @_tool_handler("getting song data")
    def get_song_data(ctx: Context, key: str) -> str:
        """Get persistent data stored in the song (survives save/load in .als file).

        Parameters:
        - key: The data key to retrieve
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_data", {"key": key})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting song data")
    def set_song_data(ctx: Context, key: str, value: str) -> str:
        """Store persistent data in the song (survives save/load in .als file).

        Parameters:
        - key: The data key to store
        - value: The string value to store
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_song_data", {"key": key, "value": value})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("ending undo step")
    def end_undo_step(ctx: Context) -> str:
        """End the current undo step — groups preceding operations into one undo action."""
        ableton = get_ableton_connection()
        result = ableton.send_command("end_undo_step", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting song length")
    def get_song_length(ctx: Context) -> str:
        """Get the total song length and last event time in beats."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_song_length", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting beat time")
    def get_beat_time(ctx: Context) -> str:
        """Get the current playback position as structured bars:beats:sub_division:ticks."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_beat_time", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting SMPTE time")
    def get_smpte_time(ctx: Context, time_format: int = 0) -> str:
        """Get the current playback position as SMPTE timecode.

        Parameters:
        - time_format: SMPTE format (0=Smpte24, 1=Smpte25, 2=Smpte29, 3=Smpte30, 4=Smpte30Drop)
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("get_smpte_time", {"time_format": time_format})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting all scales")
    def get_all_scales(ctx: Context) -> str:
        """Get all available scale names and intervals from Ableton."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_all_scales", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("nudging tempo")
    def nudge_tempo(ctx: Context, direction: str = "up") -> str:
        """Momentarily nudge the tempo up or down (like the nudge buttons).

        Parameters:
        - direction: "up" or "down"
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("nudge_tempo", {"direction": direction})
        return json.dumps(result)

    # NOTE: get_appointed_device is registered in tools/devices.py (its canonical home)

    @mcp.tool()
    @_tool_handler("getting count-in duration")
    def get_count_in_duration(ctx: Context) -> str:
        """Get the count-in duration setting (0=none, 1=1bar, 2=2bars, 3=4bars)."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_count_in_duration", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting draw mode")
    def set_draw_mode(ctx: Context, enabled: bool) -> str:
        """Toggle draw mode in Ableton's session/arrangement view.

        Parameters:
        - enabled: True to enable draw mode, False to disable
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_draw_mode", {"enabled": enabled})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting follow song")
    def set_follow_song(ctx: Context, enabled: bool) -> str:
        """Toggle the follow song (auto-scroll) setting.

        Parameters:
        - enabled: True to enable follow, False to disable
        """
        ableton = get_ableton_connection()
        result = ableton.send_command("set_follow_song", {"enabled": enabled})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("getting highlighted clip slot")
    def get_highlighted_clip_slot(ctx: Context) -> str:
        """Get the currently highlighted clip slot in session view."""
        ableton = get_ableton_connection()
        result = ableton.send_command("get_highlighted_clip_slot", {})
        return json.dumps(result)

    @mcp.tool()
    @_tool_handler("setting playback position")
    def set_playback_position(ctx: Context, position: float) -> str:
        """Jump the playback position to a specific beat in the arrangement.

        Parameters:
        - position: The target position in beats (e.g. 0.0 = bar 1, 4.0 = bar 2 in 4/4)
        """
        if not isinstance(position, (int, float)) or position < 0:
            raise ValueError("position must be a non-negative number")
        ableton = get_ableton_connection()
        ableton.send_command("set_playback_position", {"position": position})
        return f"Set playback position to beat {position}"
