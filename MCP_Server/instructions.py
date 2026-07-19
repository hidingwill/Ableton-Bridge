"""MCP server instructions for AbletonBridge.

Injected into the client's system context during initialization to guide
cross-tool usage patterns. See: https://blog.modelcontextprotocol.io/posts/2025-11-03-using-server-instructions/
"""

SERVER_INSTRUCTIONS = """
AbletonBridge provides 347 tools for controlling Ableton Live sessions. This guidance covers cross-tool relationships, sequencing, and constraints not documented on individual tools.

## Startup

Call get_server_capabilities first in every session. It reports control_role, control_availability, owner process metadata, connection state, browser cache state, and tool count. This status call does not claim control. Connection booleans are true or false only for the owner; a standby reports null because its backend is not running locally. features.m4l_bridge mirrors m4l_connected, including null on standby. Interpret ableton_connection_state and m4l_connection_state instead: not_started means the first normal tool can attempt to claim and connect, owned_elsewhere means another task owns control and its connection health is not visible here, and unknown means port 9881 is occupied by an unrelated process.

The first normal Ableton tool call automatically claims control when control_availability is "available". If another task owns control, tools return a structured ownership error instead of disappearing. Ask the owning task to call release_ableton_control when an intentional handoff is needed. Never assume control can be stolen or released by a standby task.

## Compound Tools

These combine multiple operations into one call (3-5x fewer round-trips). Prefer them over manual multi-step sequences:
- create_instrument_track — track + instrument + name + color
- create_drum_track — MIDI track + Drum Rack + clip + drum pattern (8 styles)
- create_clip_with_notes — clip creation + note writing
- batch_set_mixer — volume/pan/mute/solo for multiple tracks
- apply_effect_chain — load multiple effects sequentially
- setup_send_return — return track + effect + send level mapping
- get_full_session_state — session + all tracks + returns + scenes in one query
- save_effect_chain / load_effect_chain — template system. Caveat: load restores devices only; parameters require a separate restore step.

## Track Indexing

Track indices shift after create/delete — call get_all_tracks_info to refresh. Return tracks have their own 0-based index (Send A = return 0, Send B = return 1), separate from regular tracks. The `track_type` parameter ("track", "return", "master") selects which namespace — used by set_mixer, get_device_parameters, and others. Master track has no pan parameter (silently ignored if set).

## Clips & Notes

Session clips are addressed by (track_index, clip_index) where clip_index is the slot row. Arrangement clips live on the timeline — use get_arrangement_clips to list them; create_arrangement_midi_clip (Live 12.1+) / create_arrangement_audio_clip (Live 12.2+) to create them.

A clip must exist before writing notes: create_clip first, then add_notes_to_clip — or use create_clip_with_notes. get_clip_notes with time_span=0 returns the entire clip.

add_notes_extended supports Live 11+ note properties: probability (0.0-1.0 float, NOT percentage), velocity_deviation (-127 to +127 signed).

Time values are always in beats. Session clips: beats from clip start. Arrangement clips: beats from song position 0.

## Grid Notation

For drum patterns and rhythmic content, prefer grid_to_clip (ASCII grid notation) over individual note dictionaries. Grid notation is more compact and readable. Use clip_to_grid to read existing clips as grids. Auto-detects drum vs melodic content.

## Device Parameters

get_device_parameters returns exact parameter names and min/max ranges. Use these names exactly — they are case-sensitive. Values outside min/max are silently clamped.

For batch changes, set_device_parameters (pass a JSON dict of name:value pairs) is 3-5x faster than repeated set_device_parameter calls. realtime_set_parameter sends via UDP (fire-and-forget, no confirmation) — use for live filter sweeps and real-time control.

VST/AU plugins expose only ~32 automatable parameters by default. Use get_plugin_info for guidance on Configure mode.

Hidden parameters (via M4L): addressed by parameter_index (int), not name. Index 0 ("Device On") is auto-skipped in batch operations for safety.

## Mixer

set_mixer unifies track/return/master control via the track_type parameter. Key ranges:
- volume: 0.0-1.0 (0.85 ≈ 0 dB, not 1.0)
- pan: -1.0 (L) to 1.0 (R). Master has no pan.

For Split Stereo panning, call set_panning_mode(mode=1) first, then set_split_stereo_pan for independent L/R control.

Routing: always call get_track_routing before set_track_routing — you need the exact display names from Ableton's available routing list.

## Automation

create_clip_automation writes envelopes inside session clips (time = beats from clip start). create_track_automation writes to arrangement timeline lanes (time = beats from song start). Do not confuse them — they target different contexts.

Use as few points as possible — Ableton interpolates linearly between breakpoints. A ramp needs only 2 points; a triangle needs 3. Points are auto-reduced if more than 20 are submitted.

create_automation_curve generates shaped envelopes (sine, exponential, s_curve, triangle, sawtooth, etc.). The cycles parameter is cycles per clip length, not per beat. create_step_automation creates held-value steps.

Automation values are in the parameter's native range (usually 0.0-1.0 normalized).

## Browser & Loading

load_instrument_or_effect resolves built-in Ableton device names directly — "Wavetable", "Operator", "Drift", "Compressor", "EQ Eight", etc. Use search_browser only when loading user presets, third-party plugins, or items whose exact name is unknown (results are instant via cached index). Call refresh_browser_cache after installing new packs.

## Creative Tools

Each generative tool (scale_constrained_generate, generate_chord_progression, generate_bass_line, harmonize_melody, quantize_to_scale) takes its own scale_name and root parameters. For consistency across a session, read get_song_scale first and pass its root_note and scale_name to each creative tool call.

Drum pattern tools (generate_euclidean_rhythm, generate_drum_pattern) use standard GM layout: kick=36, snare=38, hihat=42, open_hat=46. Use quantize_to_scale for post-hoc scale correction of melodic output.

## M4L Bridge

When this task owns control, check m4l_connected from get_server_capabilities before calling any M4L tool. If false, use standard get_device_parameters / set_device_parameter instead. A standby value of null does not mean the M4L bridge is disconnected; inspect m4l_connection_state and ownership first.

Key M4L subsystems:
- **Note surgery**: get_clip_notes_with_ids → modify_clip_notes / remove_clip_notes_by_id for in-place, non-destructive editing (Live 11+).
- **Snapshots**: snapshot_device_state captures all params. morph_between_snapshots interpolates between two snapshots (quantized params snap at the 0.5 midpoint). In-memory only — lost on server restart.
- **Observation**: observe_property → get_property_changes → stop_observing for event-driven monitoring (~10 ms latency).
- **Audio analysis**: analyze_track_audio — track_index -1 = M4L device track, -2 = master, 0+ = regular track.
- **AB compare**: device_ab_compare for preset comparison (Live 12.3+).

## Undo & Safety

Call end_undo_step after a sequence of operations to group them into a single undo action. Create/delete commands (tracks, clips, scenes, devices) are non-idempotent — do not retry on error.

Compound workflow tools are NOT atomic: partial failure (e.g., instrument fails to load) leaves partial state. Snapshots, macros, and parameter maps are in-memory only (lost on restart). Effect chain templates persist to disk.

## Input Constraints

- Notes: max 10,000 per call. Each note: {pitch, start_time, duration, velocity} — pitch 0-127, velocity 1-127, duration > 0, start_time ≥ 0.
- Automation: max 500 points per call (auto-reduced to preserve shape).
- Batch parameters: max 200 per call. Batch tracks: max 50.
- Time values throughout are in beats (1.0 = one quarter note).
""".strip()
