# AbletonBridge / init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

from . import handlers

# Constants for socket communication
DEFAULT_PORT = 9877
UDP_REALTIME_PORT = 9882
HOST = "localhost"

# -----------------------------------------------------------------------
# Command dispatch tables
# -----------------------------------------------------------------------
# Both modifying and read-only commands are dispatched on Ableton's main
# thread via schedule_message + queue (Live API is not thread-safe).
# The distinction controls timeout behaviour and future optimisation.
#
# Each value is a lambda(song, p, ctrl) that extracts parameters from *p*
# and calls the appropriate handler.  The dict keys double as the
# MODIFYING_COMMANDS / READ_ONLY_COMMANDS membership sets used by
# _process_command for routing.
# -----------------------------------------------------------------------

_MODIFYING_HANDLERS = {
    # --- Session ---
    "set_tempo": lambda song, p, ctrl: handlers.session.set_tempo(song, p.get("tempo", 120.0), ctrl),
    "start_playback": lambda song, p, ctrl: handlers.session.start_playback(song, ctrl),
    "stop_playback": lambda song, p, ctrl: handlers.session.stop_playback(song, ctrl),
    "set_song_time": lambda song, p, ctrl: handlers.session.set_song_time(song, p.get("time", 0.0), ctrl),
    "set_song_loop": lambda song, p, ctrl: handlers.session.set_song_loop(song, p.get("enabled"), p.get("start"), p.get("length"), ctrl),
    "set_loop_start": lambda song, p, ctrl: handlers.session.set_loop_start(song, p.get("position", 0.0), ctrl),
    "set_loop_end": lambda song, p, ctrl: handlers.session.set_loop_end(song, p.get("position", 0.0), ctrl),
    "set_loop_length": lambda song, p, ctrl: handlers.session.set_loop_length(song, p.get("length", 4.0), ctrl),
    "set_playback_position": lambda song, p, ctrl: handlers.session.set_playback_position(song, p.get("position", 0.0), ctrl),
    "set_arrangement_overdub": lambda song, p, ctrl: handlers.session.set_arrangement_overdub(song, p.get("enabled", False), ctrl),
    "start_arrangement_recording": lambda song, p, ctrl: handlers.session.start_arrangement_recording(song, ctrl),
    "stop_arrangement_recording": lambda song, p, ctrl: handlers.session.stop_arrangement_recording(song, p.get("stop_playback", True), ctrl),
    "set_metronome": lambda song, p, ctrl: handlers.session.set_metronome(song, p.get("enabled", True), ctrl),
    "tap_tempo": lambda song, p, ctrl: handlers.session.tap_tempo(song, ctrl),
    "undo": lambda song, p, ctrl: handlers.session.undo(song, ctrl),
    "redo": lambda song, p, ctrl: handlers.session.redo(song, ctrl),
    "continue_playing": lambda song, p, ctrl: handlers.session.continue_playing(song, ctrl),
    "re_enable_automation": lambda song, p, ctrl: handlers.session.re_enable_automation(song, ctrl),
    "set_or_delete_cue": lambda song, p, ctrl: handlers.session.set_or_delete_cue(song, ctrl),
    "jump_to_cue": lambda song, p, ctrl: handlers.session.jump_to_cue(song, p.get("direction", "next"), ctrl),
    "set_groove_settings": lambda song, p, ctrl: handlers.session.set_groove_settings(
        song, p.get("groove_amount"), p.get("groove_index"),
        p.get("timing_amount"), p.get("quantization_amount"),
        p.get("random_amount"), p.get("velocity_amount"), ctrl),
    "set_song_settings": lambda song, p, ctrl: handlers.session.set_song_settings(
        song, p.get("signature_numerator"), p.get("signature_denominator"),
        p.get("swing_amount"), p.get("clip_trigger_quantization"),
        p.get("midi_recording_quantization"), p.get("back_to_arranger"),
        p.get("follow_song"), p.get("draw_mode"),
        p.get("session_automation_record"), ctrl),
    "trigger_session_record": lambda song, p, ctrl: handlers.session.trigger_session_record(song, p.get("record_length"), ctrl),
    "navigate_playback": lambda song, p, ctrl: handlers.session.navigate_playback(song, p.get("action", "play_selection"), p.get("beats"), ctrl),
    "select_scene": lambda song, p, ctrl: handlers.session.select_scene(song, p.get("scene_index", 0), ctrl),
    "select_track": lambda song, p, ctrl: handlers.session.select_track(song, p.get("track_index", 0), p.get("track_type", "track"), ctrl),
    "set_detail_clip": lambda song, p, ctrl: handlers.session.set_detail_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_song_scale": lambda song, p, ctrl: handlers.session.set_song_scale(
        song, p.get("root_note"), p.get("scale_name"), p.get("scale_mode"), ctrl),
    "set_punch": lambda song, p, ctrl: handlers.session.set_punch(
        song, p.get("punch_in"), p.get("punch_out"), p.get("count_in_duration"), ctrl),
    "set_link_enabled": lambda song, p, ctrl: handlers.session.set_link_enabled(
        song, p.get("enabled"), p.get("start_stop_sync"), ctrl),
    "set_view": lambda song, p, ctrl: handlers.session.set_view(
        song, p.get("action", "show"), p.get("view_name", ""), ctrl),
    "zoom_scroll_view": lambda song, p, ctrl: handlers.session.zoom_scroll_view(
        song, p.get("action", "scroll"), p.get("direction", 0),
        p.get("view_name", ""), p.get("modifier_pressed", False), ctrl),
    "stop_all_clips": lambda song, p, ctrl: handlers.session.stop_all_clips(song, ctrl),
    "capture_and_insert_scene": lambda song, p, ctrl: handlers.session.capture_and_insert_scene(song, ctrl),
    "set_session_record": lambda song, p, ctrl: handlers.session.set_session_record(song, p.get("enabled", False), ctrl),
    "set_song_data": lambda song, p, ctrl: handlers.session.set_song_data(
        song, p.get("key", ""), p.get("value", ""), ctrl),
    "end_undo_step": lambda song, p, ctrl: handlers.session.end_undo_step(song, ctrl),
    "nudge_tempo": lambda song, p, ctrl: handlers.session.nudge_tempo(
        song, p.get("direction", "up"), ctrl),
    "set_draw_mode": lambda song, p, ctrl: handlers.session.set_draw_mode(
        song, p.get("enabled", True), ctrl),
    "set_follow_song": lambda song, p, ctrl: handlers.session.set_follow_song(
        song, p.get("enabled", True), ctrl),
    "select_device": lambda song, p, ctrl: handlers.session.select_device(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("track_type", "track"), ctrl),
    "select_instrument": lambda song, p, ctrl: handlers.session.select_instrument(
        song, p.get("track_index", 0), ctrl),

    # --- Tracks ---
    "create_midi_track": lambda song, p, ctrl: handlers.tracks.create_midi_track(song, p.get("index", -1), ctrl),
    "create_audio_track": lambda song, p, ctrl: handlers.tracks.create_audio_track(song, p.get("index", -1), ctrl),
    "create_return_track": lambda song, p, ctrl: handlers.tracks.create_return_track(song, ctrl),
    "set_track_name": lambda song, p, ctrl: handlers.tracks.set_track_name(song, p.get("track_index", 0), p.get("name", ""), ctrl),
    "delete_track": lambda song, p, ctrl: handlers.tracks.delete_track(song, p.get("track_index", 0), ctrl),
    "duplicate_track": lambda song, p, ctrl: handlers.tracks.duplicate_track(song, p.get("track_index", 0), ctrl),
    "set_track_color": lambda song, p, ctrl: handlers.tracks.set_track_color(song, p.get("track_index", 0), p.get("color_index", 0), ctrl),
    "arm_track": lambda song, p, ctrl: handlers.tracks.arm_track(song, p.get("track_index", 0), ctrl),
    "disarm_track": lambda song, p, ctrl: handlers.tracks.disarm_track(song, p.get("track_index", 0), ctrl),
    "group_tracks": lambda song, p, ctrl: handlers.tracks.group_tracks(song, p.get("track_indices", []), p.get("name", ""), ctrl),
    "set_track_routing": lambda song, p, ctrl: handlers.tracks.set_track_routing(
        song, p.get("track_index", 0),
        p.get("input_type"), p.get("input_channel"),
        p.get("output_type"), p.get("output_channel"), ctrl),
    "set_track_monitoring": lambda song, p, ctrl: handlers.tracks.set_track_monitoring(song, p.get("track_index", 0), p.get("state", 1), ctrl),
    "create_midi_track_with_simpler": lambda song, p, ctrl: handlers.tracks.create_midi_track_with_simpler(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_track_fold": lambda song, p, ctrl: handlers.tracks.set_track_fold(song, p.get("track_index", 0), p.get("fold_state", True), ctrl),
    "create_take_lane": lambda song, p, ctrl: handlers.tracks.create_take_lane(song, p.get("track_index", 0), ctrl),
    "insert_device": lambda song, p, ctrl: handlers.tracks.insert_device(
        song, p.get("track_index", 0), p.get("device_name", ""),
        p.get("target_index"), ctrl),
    "delete_return_track": lambda song, p, ctrl: handlers.tracks.delete_return_track(song, p.get("return_index", 0), ctrl),
    "set_track_collapse": lambda song, p, ctrl: handlers.tracks.set_track_collapse(song, p.get("track_index", 0), p.get("collapsed", True), ctrl),
    "jump_in_running_session_clip": lambda song, p, ctrl: handlers.tracks.jump_in_running_session_clip(
        song, p.get("track_index", 0), p.get("amount", 0.0), ctrl),
    "set_track_data": lambda song, p, ctrl: handlers.tracks.set_track_data(
        song, p.get("track_index", 0), p.get("key", ""), p.get("value", ""), ctrl),
    "set_implicit_arm": lambda song, p, ctrl: handlers.tracks.set_implicit_arm(
        song, p.get("track_index", 0), p.get("enabled", True), ctrl),

    # --- Clips ---
    "create_clip": lambda song, p, ctrl: handlers.clips.create_clip(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("length", 4.0), ctrl),
    "add_notes_to_clip": lambda song, p, ctrl: handlers.clips.add_notes_to_clip(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("notes", []), ctrl),
    "set_clip_name": lambda song, p, ctrl: handlers.clips.set_clip_name(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("name", ""), ctrl),
    "fire_clip": lambda song, p, ctrl: handlers.clips.fire_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "stop_clip": lambda song, p, ctrl: handlers.clips.stop_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "delete_clip": lambda song, p, ctrl: handlers.clips.delete_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "duplicate_clip": lambda song, p, ctrl: handlers.clips.duplicate_clip(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("target_clip_index", 0), ctrl),
    "set_clip_looping": lambda song, p, ctrl: handlers.clips.set_clip_looping(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("looping", True), ctrl),
    "set_clip_loop_points": lambda song, p, ctrl: handlers.clips.set_clip_loop_points(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("loop_start", 0.0), p.get("loop_end", 4.0), ctrl),
    "set_clip_color": lambda song, p, ctrl: handlers.clips.set_clip_color(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("color_index", 0), ctrl),
    "crop_clip": lambda song, p, ctrl: handlers.clips.crop_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "duplicate_clip_loop": lambda song, p, ctrl: handlers.clips.duplicate_clip_loop(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_clip_start_end": lambda song, p, ctrl: handlers.clips.set_clip_start_end(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("start_marker"), p.get("end_marker"), ctrl),
    "set_clip_pitch": lambda song, p, ctrl: handlers.clips.set_clip_pitch(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("pitch_coarse"), p.get("pitch_fine"), ctrl),
    "set_clip_launch_mode": lambda song, p, ctrl: handlers.clips.set_clip_launch_mode(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("launch_mode", 0), ctrl),
    "set_clip_launch_quantization": lambda song, p, ctrl: handlers.clips.set_clip_launch_quantization(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("quantization", 14), ctrl),
    "set_clip_legato": lambda song, p, ctrl: handlers.clips.set_clip_legato(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("legato", False), ctrl),
    "audio_to_midi": lambda song, p, ctrl: handlers.clips.audio_to_midi(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("conversion_type", "melody"), ctrl),
    "duplicate_clip_region": lambda song, p, ctrl: handlers.clips.duplicate_clip_region(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("region_start", 0.0), p.get("region_length", 4.0),
        p.get("destination_time", 0.0), p.get("pitch", -1),
        p.get("transposition_amount", 0), ctrl),
    "move_clip_playing_pos": lambda song, p, ctrl: handlers.clips.move_clip_playing_pos(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("time", 0.0), ctrl),
    "set_clip_grid": lambda song, p, ctrl: handlers.clips.set_clip_grid(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("grid_quantization"), p.get("grid_is_triplet"), ctrl),
    "set_clip_follow_actions": lambda song, p, ctrl: handlers.clips.set_clip_follow_actions(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("follow_action_0"), p.get("follow_action_1"),
        p.get("follow_action_probability"), p.get("follow_action_time"),
        p.get("follow_action_enabled"), p.get("follow_action_linked"),
        p.get("follow_action_return_to_zero"), ctrl),
    "set_clip_properties": lambda song, p, ctrl: handlers.clips.set_clip_properties(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("muted"), p.get("velocity_amount"), p.get("groove"),
        p.get("signature_numerator"), p.get("signature_denominator"),
        p.get("ram_mode"), p.get("warping"), p.get("gain"), ctrl),
    "select_all_notes": lambda song, p, ctrl: handlers.clips.select_all_notes(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_clip_start_time": lambda song, p, ctrl: handlers.clips.set_clip_start_time(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("time", 0.0), ctrl),
    "stop_track_clips": lambda song, p, ctrl: handlers.clips.stop_track_clips(
        song, p.get("track_index", 0), ctrl),
    "create_arrangement_midi_clip": lambda song, p, ctrl: handlers.clips.create_arrangement_midi_clip(
        song, p.get("track_index", 0), p.get("time", 0.0), p.get("length", 4.0), ctrl),
    "create_arrangement_audio_clip": lambda song, p, ctrl: handlers.clips.create_arrangement_audio_clip(
        song, p.get("track_index", 0), p.get("time", 0.0), p.get("length", 4.0), ctrl),
    "deselect_all_notes": lambda song, p, ctrl: handlers.clips.deselect_all_notes(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_fire_button_state": lambda song, p, ctrl: handlers.clips.set_fire_button_state(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("state", True), ctrl),
    "clip_scrub_native": lambda song, p, ctrl: handlers.clips.clip_scrub_native(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("position", 0.0), ctrl),
    "clip_stop_scrub": lambda song, p, ctrl: handlers.clips.clip_stop_scrub(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "duplicate_clip_slot": lambda song, p, ctrl: handlers.clips.duplicate_clip_slot(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "set_clip_slot_properties": lambda song, p, ctrl: handlers.clips.set_clip_slot_properties(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("has_stop_button"), p.get("color_index"), ctrl),

    # --- Mixer ---
    "set_track_volume": lambda song, p, ctrl: handlers.mixer.set_track_volume(song, p.get("track_index", 0), p.get("volume", 0.85), ctrl),
    "set_track_pan": lambda song, p, ctrl: handlers.mixer.set_track_pan(song, p.get("track_index", 0), p.get("pan", 0.0), ctrl),
    "set_track_mute": lambda song, p, ctrl: handlers.mixer.set_track_mute(song, p.get("track_index", 0), p.get("mute", False), ctrl),
    "set_track_solo": lambda song, p, ctrl: handlers.mixer.set_track_solo(song, p.get("track_index", 0), p.get("solo", False), ctrl),
    "set_track_arm": lambda song, p, ctrl: handlers.mixer.set_track_arm(song, p.get("track_index", 0), p.get("arm", False), ctrl),
    "set_track_send": lambda song, p, ctrl: handlers.mixer.set_track_send(song, p.get("track_index", 0), p.get("send_index", 0), p.get("value", 0.0), ctrl),
    "set_return_track_volume": lambda song, p, ctrl: handlers.mixer.set_return_track_volume(song, p.get("return_track_index", 0), p.get("volume", 0.85), ctrl),
    "set_return_track_pan": lambda song, p, ctrl: handlers.mixer.set_return_track_pan(song, p.get("return_track_index", 0), p.get("pan", 0.0), ctrl),
    "set_return_track_mute": lambda song, p, ctrl: handlers.mixer.set_return_track_mute(song, p.get("return_track_index", 0), p.get("mute", False), ctrl),
    "set_return_track_solo": lambda song, p, ctrl: handlers.mixer.set_return_track_solo(song, p.get("return_track_index", 0), p.get("solo", False), ctrl),
    "set_master_volume": lambda song, p, ctrl: handlers.mixer.set_master_volume(song, p.get("volume", 0.85), ctrl),
    "set_crossfade_assign": lambda song, p, ctrl: handlers.mixer.set_crossfade_assign(song, p.get("track_index", 0), p.get("assign", 0), ctrl),
    "set_crossfader": lambda song, p, ctrl: handlers.mixer.set_crossfader(song, p.get("value", 0.5), ctrl),
    "set_cue_volume": lambda song, p, ctrl: handlers.mixer.set_cue_volume(song, p.get("value", 0.85), ctrl),
    "set_track_delay": lambda song, p, ctrl: handlers.mixer.set_track_delay(song, p.get("track_index", 0), p.get("delay", 0.0), ctrl),
    "set_panning_mode": lambda song, p, ctrl: handlers.mixer.set_panning_mode(song, p.get("track_index", 0), p.get("mode", 0), ctrl),
    "set_split_stereo_pan": lambda song, p, ctrl: handlers.mixer.set_split_stereo_pan(
        song, p.get("track_index", 0), p.get("left"), p.get("right"), ctrl),

    # --- Scenes ---
    "create_scene": lambda song, p, ctrl: handlers.scenes.create_scene(song, p.get("index", -1), p.get("name", ""), ctrl),
    "delete_scene": lambda song, p, ctrl: handlers.scenes.delete_scene(song, p.get("scene_index", 0), ctrl),
    "duplicate_scene": lambda song, p, ctrl: handlers.scenes.duplicate_scene(song, p.get("scene_index", 0), ctrl),
    "fire_scene": lambda song, p, ctrl: handlers.scenes.fire_scene(song, p.get("scene_index", 0), ctrl),
    "set_scene_name": lambda song, p, ctrl: handlers.scenes.set_scene_name(song, p.get("scene_index", 0), p.get("name", ""), ctrl),
    "set_scene_tempo": lambda song, p, ctrl: handlers.scenes.set_scene_tempo(song, p.get("scene_index", 0), p.get("tempo", 0), ctrl),
    "set_scene_follow_actions": lambda song, p, ctrl: handlers.scenes.set_scene_follow_actions(
        song, p.get("scene_index", 0),
        p.get("follow_action_0"), p.get("follow_action_1"),
        p.get("follow_action_probability"), p.get("follow_action_time"),
        p.get("follow_action_enabled"), p.get("follow_action_linked"), ctrl),
    "fire_scene_as_selected": lambda song, p, ctrl: handlers.scenes.fire_scene_as_selected(song, p.get("scene_index", 0), ctrl),
    "set_scene_color": lambda song, p, ctrl: handlers.scenes.set_scene_color(song, p.get("scene_index", 0), p.get("color_index", 0), ctrl),

    # --- Devices ---
    "set_device_parameter": lambda song, p, ctrl: handlers.devices.set_device_parameter(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("parameter_name", ""), p.get("value", 0.0),
        p.get("track_type", "track"), p.get("value_display"), ctrl),
    "set_device_parameters_batch": lambda song, p, ctrl: handlers.devices.set_device_parameters_batch(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("parameters", []), p.get("track_type", "track"), ctrl),
    "delete_device": lambda song, p, ctrl: handlers.devices.delete_device(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_macro_value": lambda song, p, ctrl: handlers.devices.set_macro_value(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("macro_index", 0), p.get("value", 0.0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_drum_pad": lambda song, p, ctrl: handlers.devices.set_drum_pad(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("note", 36), p.get("mute"), p.get("solo"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "copy_drum_pad": lambda song, p, ctrl: handlers.devices.copy_drum_pad(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("source_note", 36), p.get("dest_note", 37),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "rack_variation_action": lambda song, p, ctrl: handlers.devices.rack_variation_action(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("action", "recall"), p.get("variation_index"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "sliced_simpler_to_drum_rack": lambda song, p, ctrl: handlers.devices.sliced_simpler_to_drum_rack(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_compressor_sidechain": lambda song, p, ctrl: handlers.devices.set_compressor_sidechain(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("input_type"), p.get("input_channel"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_sidechain_by_name": lambda song, p, ctrl: handlers.devices.set_sidechain_by_name(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("source_track_name", ""), track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_eq8_properties": lambda song, p, ctrl: handlers.devices.set_eq8_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("edit_mode"), p.get("global_mode"),
        p.get("oversample"), p.get("selected_band"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_hybrid_reverb_ir": lambda song, p, ctrl: handlers.devices.set_hybrid_reverb_ir(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("ir_category_index"), p.get("ir_file_index"),
        p.get("ir_attack_time"), p.get("ir_decay_time"),
        p.get("ir_size_factor"), p.get("ir_time_shaping_on"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_transmute_properties": lambda song, p, ctrl: handlers.devices.set_transmute_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("frequency_dial_mode_index"), p.get("pitch_mode_index"),
        p.get("mod_mode_index"), p.get("mono_poly_index"),
        p.get("midi_gate_index"), p.get("polyphony"),
        p.get("pitch_bend_range"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_simpler_properties": lambda song, p, ctrl: handlers.devices.set_simpler_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("playback_mode"), p.get("voices"), p.get("retrigger"),
        p.get("slicing_playback_mode"),
        p.get("start_marker"), p.get("end_marker"), p.get("gain"),
        p.get("warp_mode"), p.get("warping"),
        p.get("slicing_style"), p.get("slicing_sensitivity"),
        p.get("slicing_beat_division"),
        p.get("beats_granulation_resolution"),
        p.get("beats_transient_envelope"),
        p.get("beats_transient_loop_mode"),
        p.get("complex_pro_formants"), p.get("complex_pro_envelope"),
        p.get("texture_grain_size"), p.get("texture_flux"),
        p.get("tones_grain_size"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "simpler_sample_action": lambda song, p, ctrl: handlers.devices.simpler_sample_action(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("action", "reverse"), p.get("beats"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "manage_sample_slices": lambda song, p, ctrl: handlers.devices.manage_sample_slices(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("action", "insert"), p.get("slice_time"), p.get("new_time"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_chain_selector": lambda song, p, ctrl: handlers.devices.set_chain_selector(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("value", 0.0), track_type=p.get("track_type", "track"), ctrl=ctrl),
    "insert_chain": lambda song, p, ctrl: handlers.devices.insert_chain(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("index", 0), track_type=p.get("track_type", "track"), ctrl=ctrl),
    "chain_insert_device": lambda song, p, ctrl: handlers.devices.chain_insert_device(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("chain_index", 0), p.get("device_name", ""),
        p.get("target_index"), track_type=p.get("track_type", "track"), ctrl=ctrl),
    "delete_chain_device": lambda song, p, ctrl: handlers.devices.delete_chain_device(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("chain_index", 0), p.get("chain_device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_chain_properties": lambda song, p, ctrl: handlers.devices.set_chain_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("chain_index", 0), p.get("mute"), p.get("solo"),
        p.get("name"), p.get("color_index"), p.get("volume"), p.get("panning"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "move_device": lambda song, p, ctrl: handlers.devices.move_device(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("dest_track_index", 0), p.get("dest_position", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "set_device_enabled": lambda song, p, ctrl: handlers.devices.set_device_enabled(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("enabled", True), track_type=p.get("track_type", "track"), ctrl=ctrl),

    # --- Browser ---
    "load_browser_item": lambda song, p, ctrl: handlers.browser.load_browser_item(song, p.get("track_index", 0), p.get("item_uri", ""), ctrl, p.get("track_type", "track")),
    "load_instrument_or_effect": lambda song, p, ctrl: handlers.browser.load_instrument_or_effect(song, p.get("track_index", 0), p.get("uri", ""), ctrl),
    "load_sample": lambda song, p, ctrl: handlers.browser.load_sample(song, p.get("track_index", 0), p.get("sample_uri", ""), ctrl),
    "preview_browser_item": lambda song, p, ctrl: handlers.browser.preview_browser_item(song, p.get("uri"), p.get("action", "preview"), ctrl),
    "load_device_preset": lambda song, p, ctrl: handlers.browser.load_device_preset(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("preset_uri", ""), track_type=p.get("track_type", "track"), ctrl=ctrl),

    # --- MIDI ---
    "add_notes_extended": lambda song, p, ctrl: handlers.midi.add_notes_extended(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("notes", []), ctrl),
    "remove_notes_range": lambda song, p, ctrl: handlers.midi.remove_notes_range(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("from_time", 0.0), p.get("time_span", 0.0),
        p.get("from_pitch", 0), p.get("pitch_span", 128), ctrl),
    "clear_clip_notes": lambda song, p, ctrl: handlers.midi.clear_clip_notes(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "quantize_clip_notes": lambda song, p, ctrl: handlers.midi.quantize_clip_notes(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("grid_size", 0.25), ctrl),
    "transpose_clip_notes": lambda song, p, ctrl: handlers.midi.transpose_clip_notes(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("semitones", 0), ctrl),
    "capture_midi": lambda song, p, ctrl: handlers.midi.capture_midi(song, ctrl),
    "apply_groove": lambda song, p, ctrl: handlers.midi.apply_groove(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("groove_amount", 0.0), ctrl),

    # --- Automation ---
    "create_clip_automation": lambda song, p, ctrl: handlers.automation.create_clip_automation(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), p.get("automation_points", []), ctrl),
    "clear_clip_automation": lambda song, p, ctrl: handlers.automation.clear_clip_automation(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), ctrl),
    "create_track_automation": lambda song, p, ctrl: handlers.automation.create_track_automation(
        song, p.get("track_index", 0), p.get("parameter_name", ""),
        p.get("automation_points", []), ctrl),
    "clear_track_automation": lambda song, p, ctrl: handlers.automation.clear_track_automation(
        song, p.get("track_index", 0), p.get("parameter_name", ""),
        p.get("start_time", 0.0), p.get("end_time", 0.0), ctrl),
    "delete_time": lambda song, p, ctrl: handlers.automation.delete_time(song, p.get("start_time", 0.0), p.get("end_time", 0.0), ctrl),
    "duplicate_time": lambda song, p, ctrl: handlers.automation.duplicate_time(song, p.get("start_time", 0.0), p.get("end_time", 0.0), ctrl),
    "insert_silence": lambda song, p, ctrl: handlers.automation.insert_silence(song, p.get("position", 0.0), p.get("length", 0.0), ctrl),
    "clear_clip_envelope": lambda song, p, ctrl: handlers.automation.clear_clip_envelope(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), ctrl),
    "clear_all_clip_envelopes": lambda song, p, ctrl: handlers.automation.clear_all_clip_envelopes(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "create_step_automation": lambda song, p, ctrl: handlers.automation.create_step_automation(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), p.get("steps", []), ctrl),

    # --- Arrangement ---
    "duplicate_clip_to_arrangement": lambda song, p, ctrl: handlers.arrangement.duplicate_clip_to_arrangement(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("time", 0.0), ctrl),
    "move_arrangement_clip": lambda song, p, ctrl: handlers.arrangement.move_arrangement_clip(
        song, p.get("track_index", 0), p.get("clip_index_in_arrangement", 0),
        p.get("new_start_time", 0.0), ctrl),
    "delete_arrangement_clip": lambda song, p, ctrl: handlers.arrangement.delete_arrangement_clip(
        song, p.get("track_index", 0), p.get("clip_index_in_arrangement", 0), ctrl),
    "set_arrangement_clip_properties": lambda song, p, ctrl: handlers.arrangement.set_arrangement_clip_properties(
        song, p.get("track_index", 0), p.get("clip_index_in_arrangement", 0),
        p.get("muted"), p.get("gain"), p.get("name"), p.get("color_index"),
        p.get("loop_start"), p.get("loop_end"), p.get("looping"),
        p.get("start_marker"), p.get("end_marker"),
        p.get("pitch_coarse"), p.get("pitch_fine"), ctrl),

    # --- Audio ---
    "set_warp_mode": lambda song, p, ctrl: handlers.audio.set_warp_mode(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("warp_mode", "beats"), ctrl),
    "set_clip_warp": lambda song, p, ctrl: handlers.audio.set_clip_warp(song, p.get("track_index", 0), p.get("clip_index", 0), p.get("warping_enabled", True), ctrl),
    "reverse_clip": lambda song, p, ctrl: handlers.audio.reverse_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "freeze_track": lambda song, p, ctrl: handlers.audio.freeze_track(song, p.get("track_index", 0), ctrl),
    "unfreeze_track": lambda song, p, ctrl: handlers.audio.unfreeze_track(song, p.get("track_index", 0), ctrl),

    # --- Warp markers ---
    "add_warp_marker": lambda song, p, ctrl: handlers.clips.add_warp_marker(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("beat_time", 0.0), p.get("sample_time"), ctrl),
    "move_warp_marker": lambda song, p, ctrl: handlers.clips.move_warp_marker(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("beat_time", 0.0), p.get("beat_time_distance", 0.0), ctrl),
    "remove_warp_marker": lambda song, p, ctrl: handlers.clips.remove_warp_marker(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("beat_time", 0.0), ctrl),

    # --- Looper ---
    "control_looper": lambda song, p, ctrl: handlers.devices.control_looper(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("action", "play"), p.get("clip_slot_index"),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
}

_READONLY_HANDLERS = {
    # --- Session ---
    "get_session_info": lambda song, p, ctrl: handlers.session.get_session_info(song, ctrl),
    "get_song_transport": lambda song, p, ctrl: handlers.session.get_song_transport(song, ctrl),
    "get_loop_info": lambda song, p, ctrl: handlers.session.get_loop_info(song, ctrl),
    "get_recording_status": lambda song, p, ctrl: handlers.session.get_recording_status(song, ctrl),
    "get_cue_points": lambda song, p, ctrl: handlers.session.get_cue_points(song, ctrl),
    "get_groove_pool": lambda song, p, ctrl: handlers.session.get_groove_pool(song, ctrl),
    "get_song_settings": lambda song, p, ctrl: handlers.session.get_song_settings(song, ctrl),
    "get_song_scale": lambda song, p, ctrl: handlers.session.get_song_scale(song, ctrl),
    "get_selection_state": lambda song, p, ctrl: handlers.session.get_selection_state(song, ctrl),
    "get_link_status": lambda song, p, ctrl: handlers.session.get_link_status(song, ctrl),
    "get_tuning_system": lambda song, p, ctrl: handlers.session.get_tuning_system(song, ctrl),
    "get_view_state": lambda song, p, ctrl: handlers.session.get_view_state(song, ctrl),
    "get_playing_clips": lambda song, p, ctrl: handlers.session.get_playing_clips(song, ctrl),
    "get_song_file_path": lambda song, p, ctrl: handlers.session.get_song_file_path(song, ctrl),

    # --- Tracks ---
    "get_track_info": lambda song, p, ctrl: handlers.tracks.get_track_info(song, p.get("track_index", 0), ctrl),
    "get_all_tracks_info": lambda song, p, ctrl: handlers.tracks.get_all_tracks_info(song, ctrl),
    "get_return_tracks_info": lambda song, p, ctrl: handlers.tracks.get_return_tracks_info(song, ctrl),
    "get_track_routing": lambda song, p, ctrl: handlers.tracks.get_track_routing(song, p.get("track_index", 0), ctrl),
    "get_track_meters": lambda song, p, ctrl: handlers.tracks.get_track_meters(song, p.get("track_index", 0), ctrl),
    "get_take_lanes": lambda song, p, ctrl: handlers.tracks.get_take_lanes(song, p.get("track_index", 0), ctrl),

    # --- Clips ---
    "get_clip_info": lambda song, p, ctrl: handlers.clips.get_clip_info(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "get_clip_follow_actions": lambda song, p, ctrl: handlers.clips.get_clip_follow_actions(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "get_clip_properties": lambda song, p, ctrl: handlers.clips.get_clip_properties(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),

    # --- Scenes ---
    "get_scene_follow_actions": lambda song, p, ctrl: handlers.scenes.get_scene_follow_actions(song, p.get("scene_index", 0), ctrl),

    # --- Mixer ---
    "get_scenes": lambda song, p, ctrl: handlers.mixer.get_scenes(song, ctrl),
    "get_return_tracks": lambda song, p, ctrl: handlers.mixer.get_return_tracks(song, ctrl),
    "get_return_track_info": lambda song, p, ctrl: handlers.mixer.get_return_track_info(song, p.get("return_track_index", 0), ctrl),
    "get_master_track_info": lambda song, p, ctrl: handlers.mixer.get_master_track_info(song, ctrl),
    "get_crossfader": lambda song, p, ctrl: handlers.mixer.get_crossfader(song, ctrl),
    "get_track_delay": lambda song, p, ctrl: handlers.mixer.get_track_delay(song, p.get("track_index", 0), ctrl),

    # --- Devices ---
    "get_device_parameters": lambda song, p, ctrl: handlers.devices.get_device_parameters(
        song, p.get("track_index", 0), p.get("device_index", 0),
        p.get("track_type", "track"), ctrl),
    "get_macro_values": lambda song, p, ctrl: handlers.devices.get_macro_values(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_drum_pads": lambda song, p, ctrl: handlers.devices.get_drum_pads(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_rack_variations": lambda song, p, ctrl: handlers.devices.get_rack_variations(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_compressor_sidechain": lambda song, p, ctrl: handlers.devices.get_compressor_sidechain(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_eq8_properties": lambda song, p, ctrl: handlers.devices.get_eq8_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_hybrid_reverb_ir": lambda song, p, ctrl: handlers.devices.get_hybrid_reverb_ir(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_transmute_properties": lambda song, p, ctrl: handlers.devices.get_transmute_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_simpler_properties": lambda song, p, ctrl: handlers.devices.get_simpler_properties(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_chain_selector": lambda song, p, ctrl: handlers.devices.get_chain_selector(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),
    "get_device_info": lambda song, p, ctrl: handlers.devices.get_device_info(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),

    # --- Browser ---
    "get_browser_item": lambda song, p, ctrl: handlers.browser.get_browser_item(song, p.get("uri"), p.get("path"), ctrl),
    "get_browser_tree": lambda song, p, ctrl: handlers.browser.get_browser_tree(song, p.get("category_type", "all"), ctrl),
    "get_browser_items_at_path": lambda song, p, ctrl: handlers.browser.get_browser_items_at_path(song, p.get("path", ""), ctrl),
    "search_browser": lambda song, p, ctrl: handlers.browser.search_browser(song, p.get("query", ""), p.get("category", "all"), ctrl),
    "get_user_library": lambda song, p, ctrl: handlers.browser.get_user_library(song, ctrl),
    "get_user_folders": lambda song, p, ctrl: handlers.browser.get_user_folders(song, ctrl),
    "get_device_presets": lambda song, p, ctrl: handlers.browser.get_device_presets(
        song, p.get("track_index", 0), p.get("device_index", 0),
        track_type=p.get("track_type", "track"), ctrl=ctrl),

    # --- MIDI ---
    "get_clip_notes": lambda song, p, ctrl: handlers.midi.get_clip_notes(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("start_time", 0.0), p.get("time_span", 0.0),
        p.get("start_pitch", 0), p.get("pitch_span", 128), ctrl),
    "get_notes_extended": lambda song, p, ctrl: handlers.midi.get_notes_extended(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("start_time", 0.0), p.get("time_span", 0.0), ctrl),

    # --- Automation ---
    "get_clip_automation": lambda song, p, ctrl: handlers.automation.get_clip_automation(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), ctrl),
    "list_clip_automated_params": lambda song, p, ctrl: handlers.automation.list_clip_automated_params(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),

    # --- Audio ---
    "get_audio_clip_info": lambda song, p, ctrl: handlers.audio.get_audio_clip_info(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "analyze_audio_clip": lambda song, p, ctrl: handlers.audio.analyze_audio_clip(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "get_warp_markers": lambda song, p, ctrl: handlers.clips.get_warp_markers(song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),

    # --- Arrangement ---
    "get_arrangement_clips": lambda song, p, ctrl: handlers.arrangement.get_arrangement_clips(song, p.get("track_index", 0), ctrl),
    "get_arrangement_clip_info": lambda song, p, ctrl: handlers.arrangement.get_arrangement_clip_info(
        song, p.get("track_index", 0), p.get("clip_index_in_arrangement", 0), ctrl),

    # --- v4.0: New read-only commands ---
    # Automation
    "get_clip_automation_value": lambda song, p, ctrl: handlers.automation.get_clip_automation_value(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), p.get("time", 0.0), ctrl),
    "get_clip_automation_hires": lambda song, p, ctrl: handlers.automation.get_clip_automation_hires(
        song, p.get("track_index", 0), p.get("clip_index", 0),
        p.get("parameter_name", ""), p.get("sample_count", 128), ctrl),

    # Clips
    "get_selected_notes": lambda song, p, ctrl: handlers.clips.get_selected_notes(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),
    "clip_beat_to_sample_time": lambda song, p, ctrl: handlers.clips.clip_beat_to_sample_time(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("beat_time", 0.0), ctrl),
    "clip_sample_to_beat_time": lambda song, p, ctrl: handlers.clips.clip_sample_to_beat_time(
        song, p.get("track_index", 0), p.get("clip_index", 0), p.get("sample_time", 0.0), ctrl),
    "get_clip_slot_properties": lambda song, p, ctrl: handlers.clips.get_clip_slot_properties(
        song, p.get("track_index", 0), p.get("clip_index", 0), ctrl),

    # Session
    "get_song_data": lambda song, p, ctrl: handlers.session.get_song_data(
        song, p.get("key", ""), ctrl),
    "get_song_length": lambda song, p, ctrl: handlers.session.get_song_length(song, ctrl),
    "get_beat_time": lambda song, p, ctrl: handlers.session.get_beat_time(song, ctrl),
    "get_smpte_time": lambda song, p, ctrl: handlers.session.get_smpte_time(
        song, p.get("time_format", 0), ctrl),
    "get_all_scales": lambda song, p, ctrl: handlers.session.get_all_scales(song, ctrl),
    "get_appointed_device": lambda song, p, ctrl: handlers.session.get_appointed_device(song, ctrl),
    "get_count_in_duration": lambda song, p, ctrl: handlers.session.get_count_in_duration(song, ctrl),
    "get_highlighted_clip_slot": lambda song, p, ctrl: handlers.session.get_highlighted_clip_slot(song, ctrl),
    "get_selected_parameter": lambda song, p, ctrl: handlers.session.get_selected_parameter(song, ctrl),

    # Tracks
    "get_track_data": lambda song, p, ctrl: handlers.tracks.get_track_data(
        song, p.get("track_index", 0), p.get("key", ""), ctrl),
    "get_track_input_meters": lambda song, p, ctrl: handlers.tracks.get_track_input_meters(
        song, p.get("track_index"), ctrl),
}


def create_instance(c_instance):
    """Create and return the AbletonBridge script instance"""
    return AbletonBridge(c_instance)


class AbletonBridge(ControlSurface):
    """AbletonBridge Remote Script for Ableton Live"""

    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonBridge Remote Script initializing...")

        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.client_sockets = []
        self._client_lock = threading.Lock()  # protects client_threads and client_sockets
        self.server_thread = None
        self.running = False

        # UDP real-time parameter server
        self.udp_sock = None
        self.udp_thread = None
        self.udp_running = False

        # Start the socket servers
        self.start_server()
        self.start_udp_server()

        self.log_message("AbletonBridge initialized")

        # Show a message in Ableton
        self.show_message("AbletonBridge: TCP " + str(DEFAULT_PORT) + " / UDP " + str(UDP_REALTIME_PORT))

    @property
    def _song(self):
        """Always return the current song, even after File > New"""
        return self.song()

    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonBridge disconnecting...")
        self.running = False

        # Close UDP socket FIRST to unblock recvfrom(), THEN clear the flag.
        # The UDP thread's exception handler checks udp_running, so it will
        # exit silently when it sees the flag is False.
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except (OSError, socket.error):
                pass
            self.udp_sock = None
        self.udp_running = False

        if self.udp_thread and self.udp_thread.is_alive():
            self.udp_thread.join(3.0)

        # Close all client sockets so their threads can exit
        with self._client_lock:
            socks_to_close = self.client_sockets[:]
            self.client_sockets = []
        for sock in socks_to_close:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, socket.error):
                pass
            try:
                sock.close()
            except (OSError, socket.error):
                pass

        # Stop the listening server socket (no shutdown needed for listening sockets)
        if self.server:
            try:
                self.server.close()
            except (OSError, socket.error):
                pass

        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(3.0)

        # Wait briefly for client threads to exit
        with self._client_lock:
            threads_to_join = self.client_threads[:]
        for client_thread in threads_to_join:
            if client_thread.is_alive():
                client_thread.join(3.0)

        ControlSurface.disconnect(self)
        self.log_message("AbletonBridge disconnected")

    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)

            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()

            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonBridge: Error starting server - " + str(e))

    def start_udp_server(self):
        """Start the UDP real-time parameter server in a separate thread."""
        try:
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.bind((HOST, UDP_REALTIME_PORT))
            self.udp_sock.settimeout(1.0)

            self.udp_running = True
            self.udp_thread = threading.Thread(target=self._udp_server_loop)
            self.udp_thread.daemon = True
            self.udp_thread.start()

            self.log_message("UDP real-time server started on port " + str(UDP_REALTIME_PORT))
        except Exception as e:
            self.udp_running = False
            self.log_message("Error starting UDP server: " + str(e))

    def _udp_server_loop(self):
        """UDP server loop - receives fire-and-forget parameter updates."""
        while self.udp_running:
            try:
                data, addr = self.udp_sock.recvfrom(4096)
                if not data:
                    continue

                try:
                    command = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as parse_err:
                    self.log_message(
                        "UDP: malformed packet from {0}: {1}".format(addr, parse_err))
                    continue

                self._process_udp_command(command)

            except socket.timeout:
                continue
            except Exception as e:
                if self.udp_running:
                    self.log_message("UDP server error: " + str(e))
                time.sleep(0.1)

    def _process_udp_command(self, command):
        """Process a UDP command. Fire-and-forget - no response sent.

        IMPORTANT: This runs on the UDP thread.  Do NOT access self._song
        here — the Live API is not thread-safe.  Instead, capture only the
        plain-data cmd/params on this thread and defer all Live API access
        (including self._song) to the scheduled task that runs on the main
        thread.  If schedule_message fails, drop the update with a log
        message rather than calling the task inline from the wrong thread.
        """
        cmd = command.get("type", "")
        params = command.get("params", {})

        if cmd == "set_device_parameter":
            def task():
                try:
                    handlers.devices.set_device_parameter(
                        self._song,
                        params.get("track_index", 0),
                        params.get("device_index", 0),
                        params.get("parameter_name", ""),
                        params.get("value", 0.0),
                        params.get("track_type", "track"),
                        ctrl=self,
                    )
                except Exception as e:
                    self.log_message("UDP set_device_parameter error: " + str(e))
            try:
                self.schedule_message(0, task)
            except AssertionError:
                self.log_message("UDP set_device_parameter: schedule_message unavailable, dropping update")

        elif cmd == "batch_set_device_parameters":
            def task():
                try:
                    handlers.devices.set_device_parameters_batch(
                        self._song,
                        params.get("track_index", 0),
                        params.get("device_index", 0),
                        params.get("parameters", []),
                        params.get("track_type", "track"),
                        ctrl=self,
                    )
                except Exception as e:
                    self.log_message("UDP batch_set error: " + str(e))
            try:
                self.schedule_message(0, task)
            except AssertionError:
                self.log_message("UDP batch_set: schedule_message unavailable, dropping update")

    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            self.server.settimeout(1.0)

            while self.running:
                try:
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonBridge: Client connected")

                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()

                    with self._client_lock:
                        self.client_threads.append(client_thread)
                        self.client_sockets.append(client)
                        # Clean up finished client threads
                        self.client_threads = [t for t in self.client_threads if t.is_alive()]

                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)

            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))

    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(5.0)
        buffer = ''

        try:
            while self.running:
                try:
                    try:
                        data = client.recv(8192)
                    except socket.timeout:
                        continue

                    if not data:
                        self.log_message("Client disconnected")
                        break

                    # Accumulate data (replace invalid UTF-8 instead of crashing)
                    buffer += data.decode('utf-8', errors='replace')

                    # Process all complete newline-delimited messages
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            command = json.loads(line)
                        except ValueError:
                            self.log_message("Invalid JSON received, skipping: " + line[:100])
                            continue

                        self.log_message("Received command: " + str(command.get("type", "unknown")))

                        response = self._process_command(command)

                        response_str = json.dumps(response) + '\n'
                        try:
                            client.sendall(response_str.encode('utf-8'))
                        except (OSError, socket.error):
                            self.log_message("Client disconnected during response send")
                            break

                        # Small breathing room between consecutive commands to prevent
                        # flooding Ableton's main thread scheduler during rapid bursts.
                        # The MCP server serializes tool calls via asyncio semaphore,
                        # so 1ms is sufficient to yield the thread.
                        time.sleep(0.001)

                    # 1MB safety limit
                    if len(buffer) > 1048576:
                        self.log_message("Buffer overflow (>1MB without newline), disconnecting client")
                        try:
                            err = json.dumps({"status": "error", "message": "Request too large (>1MB)"}) + '\n'
                            client.sendall(err.encode('utf-8'))
                        except Exception:
                            pass
                        break

                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())

                    error_response = {"status": "error", "message": self._safe_error_message(e)}
                    try:
                        client.sendall((json.dumps(error_response) + '\n').encode('utf-8'))
                    except Exception:
                        break

                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except (OSError, socket.error):
                pass
            try:
                client.close()
            except (OSError, socket.error):
                pass
            with self._client_lock:
                try:
                    self.client_sockets.remove(client)
                except ValueError:
                    pass
            self.log_message("Client handler stopped")

    # ------------------------------------------------------------------
    # Error sanitisation
    # ------------------------------------------------------------------

    def _safe_error_message(self, e):
        """Return a client-safe error message.

        ValueError/IndexError messages are kept (user-input validation).
        KeyError -> "Missing required parameter: <key>"
        TypeError -> "Invalid parameter type"
        Everything else gets a generic message; details stay in the log.
        """
        if isinstance(e, (ValueError, IndexError)):
            return str(e)
        if isinstance(e, KeyError):
            return "Missing required parameter: {0}".format(e)
        if isinstance(e, TypeError):
            return "Invalid parameter type"
        if isinstance(e, queue.Empty):
            return "Operation timed out"
        return "Internal error - check Ableton log for details"

    # ------------------------------------------------------------------
    # Command routing
    # ------------------------------------------------------------------

    def _process_command(self, command):
        """Process a command from the client and return a response."""
        command_type = command.get("type", "")
        params = command.get("params", {})
        response = {"status": "success", "result": {}}

        try:
            if command_type in _MODIFYING_HANDLERS:
                response = self._dispatch_on_main_thread(command_type, params)
            elif command_type in _READONLY_HANDLERS:
                response = self._dispatch_on_main_thread_readonly(command_type, params)
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = self._safe_error_message(e)

        return response

    def _dispatch_on_main_thread_impl(self, dispatch_fn, command_type, params, timeout_msg):
        """Schedule a command on Ableton's main thread and wait for the result."""
        response_queue = queue.Queue()

        def main_thread_task():
            try:
                result = dispatch_fn(command_type, params)
                response_queue.put({"status": "success", "result": result})
            except Exception as e:
                self.log_message("Error in main thread task: " + str(e))
                self.log_message(traceback.format_exc())
                response_queue.put({"status": "error", "message": self._safe_error_message(e)})

        try:
            self.schedule_message(0, main_thread_task)
        except AssertionError:
            self.log_message("TCP command: schedule_message unavailable, returning error")
            return {"status": "error", "message": "Ableton scheduling unavailable — try again shortly"}

        try:
            return response_queue.get(timeout=10.0)
        except queue.Empty:
            return {"status": "error", "message": timeout_msg}

    def _dispatch_on_main_thread(self, command_type, params):
        return self._dispatch_on_main_thread_impl(
            self._dispatch_modifying, command_type, params,
            "Timeout waiting for operation to complete")

    def _dispatch_on_main_thread_readonly(self, command_type, params):
        return self._dispatch_on_main_thread_impl(
            self._dispatch_read_only, command_type, params,
            "Timeout waiting for read-only operation to complete")

    # ------------------------------------------------------------------
    # Modifying command dispatch
    # ------------------------------------------------------------------

    def _dispatch_modifying(self, cmd, p):
        """Route a modifying command to the appropriate handler function."""
        handler = _MODIFYING_HANDLERS.get(cmd)
        if handler is None:
            raise ValueError("Unknown modifying command: {0}".format(cmd))
        return handler(self._song, p, self)

    # ------------------------------------------------------------------
    # Read-only command dispatch
    # ------------------------------------------------------------------

    def _dispatch_read_only(self, cmd, p):
        """Route a read-only command to the appropriate handler function."""
        handler = _READONLY_HANDLERS.get(cmd)
        if handler is None:
            raise ValueError("Unknown read-only command: {0}".format(cmd))
        return handler(self._song, p, self)
