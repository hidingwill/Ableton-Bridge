"""M4LConnection — UDP connection to the Max for Live bridge device."""

import socket
import json
import logging
import time
import threading
import uuid
import base64
import struct
from dataclasses import dataclass, field
from typing import Dict, Any, List

import MCP_Server.state as state

logger = logging.getLogger("AbletonBridge")


@dataclass
class M4LConnection:
    """UDP connection to the Max for Live bridge device.

    The M4L bridge provides deep LOM access for hidden device parameters.
    Communication uses two UDP ports:
      - send_port (9878): MCP server -> M4L device (commands)
      - recv_port (9879): M4L device -> MCP server (responses)
    """
    send_host: str = "127.0.0.1"
    send_port: int = 9878
    recv_port: int = 9879
    send_sock: socket.socket = None
    recv_sock: socket.socket = None
    _connected: bool = False
    _send_lock: threading.Lock = field(default_factory=threading.Lock)

    def connect(self) -> bool:
        """Set up UDP sockets for M4L communication."""
        try:
            self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Use exclusive binding -- prevents a second instance from sharing this port
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.recv_sock.bind(("127.0.0.1", self.recv_port))
            self.recv_sock.settimeout(5.0)
            self._connected = True
            logger.info("M4L UDP sockets ready (send->:%d, recv<-:%d)", self.send_port, self.recv_port)
            return True
        except Exception as e:
            logger.error("Failed to set up M4L UDP connection: %s", e)
            self.disconnect()
            return False

    def disconnect(self):
        """Close UDP sockets."""
        for s in (self.send_sock, self.recv_sock):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self.send_sock = None
        self.recv_sock = None
        self._connected = False

    @staticmethod
    def _build_osc_message(address: str, osc_args: list = None) -> bytes:
        """Build an OSC message with typed arguments.

        Each arg is a tuple of (type, value):
          ('i', 42)  -- 32-bit int
          ('f', 3.14) -- 32-bit float
          ('s', 'hi') -- null-terminated padded string
        """
        def _osc_string(s: str) -> bytes:
            b = s.encode("utf-8") + b"\x00"
            b += b"\x00" * ((4 - len(b) % 4) % 4)
            return b

        osc_args = osc_args or []
        msg = _osc_string(address)
        type_tag = "," + "".join(t for t, _ in osc_args)
        msg += _osc_string(type_tag)
        for t, v in osc_args:
            if t == "s":
                msg += _osc_string(str(v))
            elif t == "i":
                msg += struct.pack(">i", int(v))
            elif t == "f":
                msg += struct.pack(">f", float(v))
        return msg

    def _build_osc_packet(self, command_type: str, params: Dict[str, Any], request_id: str) -> bytes:
        """Build the OSC packet for a given command type."""
        if command_type == "ping":
            return self._build_osc_message("/ping", [("s", request_id)])
        elif command_type == "discover_params":
            return self._build_osc_message("/discover_params", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", request_id),
            ])
        elif command_type == "get_hidden_params":
            return self._build_osc_message("/get_hidden_params", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", request_id),
            ])
        elif command_type == "set_hidden_param":
            return self._build_osc_message("/set_hidden_param", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["parameter_index"]),
                ("f", params["value"]),
                ("s", request_id),
            ])
        elif command_type == "get_device_property":
            return self._build_osc_message("/get_device_property", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", params["property_name"]),
                ("s", request_id),
            ])
        elif command_type == "set_device_property":
            return self._build_osc_message("/set_device_property", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", params["property_name"]),
                ("f", params["value"]),
                ("s", request_id),
            ])
        elif command_type == "batch_set_hidden_params":
            # Use compact JSON (no spaces) + URL-safe base64 without padding.
            # Max's OSC/symbol handling mangles +, /, and = characters.
            params_json = json.dumps(params["parameters"], separators=(",", ":"))
            params_b64 = base64.urlsafe_b64encode(params_json.encode("utf-8")).decode("ascii").rstrip("=")
            return self._build_osc_message("/batch_set_hidden_params", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", params_b64),
                ("s", request_id),
            ])
        # --- Phase 7: Cue Points ---
        elif command_type == "get_cue_points":
            return self._build_osc_message("/get_cue_points", [
                ("s", request_id),
            ])
        elif command_type == "jump_to_cue_point":
            return self._build_osc_message("/jump_to_cue_point", [
                ("i", params["cue_point_index"]),
                ("s", request_id),
            ])
        # --- Phase 8: Groove Pool ---
        elif command_type == "get_groove_pool":
            return self._build_osc_message("/get_groove_pool", [
                ("s", request_id),
            ])
        elif command_type == "set_groove_properties":
            props_json = json.dumps(params["properties"], separators=(",", ":"))
            props_b64 = base64.urlsafe_b64encode(props_json.encode("utf-8")).decode("ascii").rstrip("=")
            return self._build_osc_message("/set_groove_properties", [
                ("i", params["groove_index"]),
                ("s", props_b64),
                ("s", request_id),
            ])
        # --- Phase 6: Event Monitoring ---
        elif command_type == "observe_property":
            return self._build_osc_message("/observe_property", [
                ("s", params["lom_path"]),
                ("s", params["property_name"]),
                ("s", request_id),
            ])
        elif command_type == "stop_observing":
            return self._build_osc_message("/stop_observing", [
                ("s", params["lom_path"]),
                ("s", params["property_name"]),
                ("s", request_id),
            ])
        elif command_type == "get_observed_changes":
            return self._build_osc_message("/get_observed_changes", [
                ("s", request_id),
            ])
        # --- Phase 9: Clean Params ---
        elif command_type == "set_param_clean":
            return self._build_osc_message("/set_param_clean", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["parameter_index"]),
                ("f", params["value"]),
                ("s", request_id),
            ])
        # --- Phase 5: Audio Analysis ---
        elif command_type == "analyze_audio":
            track_index = params.get("track_index", -1) if params else -1
            return self._build_osc_message("/analyze_audio", [
                ("i", track_index),
                ("s", request_id),
            ])
        elif command_type == "analyze_spectrum":
            return self._build_osc_message("/analyze_spectrum", [
                ("s", request_id),
            ])
        # --- Cross-Track MSP Analysis ---
        elif command_type == "analyze_cross_track":
            return self._build_osc_message("/analyze_cross_track", [
                ("i", params.get("track_index", 0)),
                ("i", params.get("wait_ms", 500)),
                ("s", request_id),
            ])
        # --- Phase 10: App Version Detection ---
        elif command_type == "get_app_version":
            return self._build_osc_message("/get_app_version", [("s", request_id)])
        # --- Phase 11: Automation State Introspection ---
        elif command_type == "get_automation_states":
            return self._build_osc_message("/get_automation_states", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", request_id),
            ])
        # --- Phase F1: Wire orphaned chain OSC builders ---
        elif command_type == "discover_chains":
            extra = params.get("extra_path", "")
            osc_args = [("i", params["track_index"]), ("i", params["device_index"])]
            if extra:
                osc_args.append(("s", extra))
            osc_args.append(("s", request_id))
            return self._build_osc_message("/discover_chains", osc_args)
        elif command_type == "get_chain_device_params":
            return self._build_osc_message("/get_chain_device_params", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("i", params["chain_device_index"]),
                ("s", request_id),
            ])
        elif command_type == "set_chain_device_param":
            return self._build_osc_message("/set_chain_device_param", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("i", params["chain_device_index"]),
                ("i", params["parameter_index"]),
                ("f", params["value"]),
                ("s", request_id),
            ])
        # --- Phase 12: Note Surgery by ID ---
        elif command_type == "get_clip_notes_by_id":
            return self._build_osc_message("/get_clip_notes_by_id", [
                ("i", params["track_index"]),
                ("i", params["clip_index"]),
                ("s", request_id),
            ])
        elif command_type == "modify_clip_notes":
            mods_json = json.dumps(params["modifications"], separators=(",", ":"))
            mods_b64 = base64.urlsafe_b64encode(mods_json.encode("utf-8")).decode("ascii").rstrip("=")
            return self._build_osc_message("/modify_clip_notes", [
                ("i", params["track_index"]),
                ("i", params["clip_index"]),
                ("s", mods_b64),
                ("s", request_id),
            ])
        elif command_type == "remove_clip_notes_by_id":
            ids_json = json.dumps(params["note_ids"], separators=(",", ":"))
            ids_b64 = base64.urlsafe_b64encode(ids_json.encode("utf-8")).decode("ascii").rstrip("=")
            return self._build_osc_message("/remove_clip_notes_by_id", [
                ("i", params["track_index"]),
                ("i", params["clip_index"]),
                ("s", ids_b64),
                ("s", request_id),
            ])
        # --- Phase 13: Chain-Level Mixing ---
        elif command_type == "get_chain_mixing":
            return self._build_osc_message("/get_chain_mixing", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("s", request_id),
            ])
        elif command_type == "set_chain_mixing":
            props_json = json.dumps(params["properties"], separators=(",", ":"))
            props_b64 = base64.urlsafe_b64encode(props_json.encode("utf-8")).decode("ascii").rstrip("=")
            return self._build_osc_message("/set_chain_mixing", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("s", props_b64),
                ("s", request_id),
            ])
        # --- Phase 14: Device AB Comparison ---
        elif command_type == "device_ab_compare":
            return self._build_osc_message("/device_ab_compare", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", params["action"]),
                ("s", request_id),
            ])
        # --- Phase 15: Clip Scrubbing ---
        elif command_type == "clip_scrub":
            return self._build_osc_message("/clip_scrub", [
                ("i", params["track_index"]),
                ("i", params["clip_index"]),
                ("s", params["action"]),
                ("f", params.get("beat_time", 0.0)),
                ("s", request_id),
            ])
        # --- Phase 16: Split Stereo Panning ---
        elif command_type == "get_split_stereo":
            return self._build_osc_message("/get_split_stereo", [
                ("i", params["track_index"]),
                ("s", request_id),
            ])
        elif command_type == "set_split_stereo":
            return self._build_osc_message("/set_split_stereo", [
                ("i", params["track_index"]),
                ("f", params["left"]),
                ("f", params["right"]),
                ("s", request_id),
            ])
        # --- Phase 17: Extended LOM Operations ---
        elif command_type == "rack_insert_chain":
            return self._build_osc_message("/rack_insert_chain", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params.get("chain_index", 0)),
                ("s", request_id),
            ])
        elif command_type == "chain_insert_device_m4l":
            return self._build_osc_message("/chain_insert_device_m4l", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("s", params["device_uri"]),
                ("i", params.get("target_index", 0)),
                ("s", request_id),
            ])
        elif command_type == "set_drum_chain_note":
            return self._build_osc_message("/set_drum_chain_note", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["chain_index"]),
                ("i", params["note"]),
                ("s", request_id),
            ])
        elif command_type == "get_take_lanes":
            return self._build_osc_message("/get_take_lanes", [
                ("i", params["track_index"]),
                ("s", request_id),
            ])
        elif command_type == "rack_store_variation":
            return self._build_osc_message("/rack_store_variation", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("s", request_id),
            ])
        elif command_type == "rack_recall_variation":
            return self._build_osc_message("/rack_recall_variation", [
                ("i", params["track_index"]),
                ("i", params["device_index"]),
                ("i", params["variation_index"]),
                ("s", request_id),
            ])
        elif command_type == "create_arrangement_midi_clip_m4l":
            return self._build_osc_message("/create_arrangement_midi_clip_m4l", [
                ("i", params["track_index"]),
                ("f", params["time"]),
                ("f", params["length"]),
                ("s", request_id),
            ])
        elif command_type == "create_arrangement_audio_clip_m4l":
            return self._build_osc_message("/create_arrangement_audio_clip_m4l", [
                ("i", params["track_index"]),
                ("f", params["time"]),
                ("f", params["length"]),
                ("s", request_id),
            ])
        else:
            raise ValueError(f"Unknown M4L command: {command_type}")

    def _drain_recv_socket(self):
        """Drain any stale data from the receive socket."""
        self.recv_sock.setblocking(False)
        try:
            for _ in range(100):
                self.recv_sock.recvfrom(65535)
        except (BlockingIOError, OSError):
            pass
        self.recv_sock.setblocking(True)

    def send_command(self, command_type: str, params: Dict[str, Any] = None, timeout: float = None) -> Dict[str, Any]:
        """Send a command to the M4L bridge using native OSC messages.

        Includes automatic reconnect: if the send or receive fails, the
        UDP sockets are recreated and the command is retried once.

        A threading.Lock serializes access so that concurrent tool threads
        cannot interleave send/recv operations on the shared UDP sockets.
        """
        params = params or {}
        request_id = str(uuid.uuid4())[:8]
        osc = self._build_osc_packet(command_type, params, request_id)

        # Commands that use chunked async processing in the M4L bridge
        # need longer timeouts to account for discovery + response delays.
        if timeout is not None:
            pass  # caller override takes priority
        elif command_type == "batch_set_hidden_params":
            param_count = len(params.get("parameters", []))
            # ~150ms per param (chunk delay + LOM overhead), minimum 10s
            timeout = max(10.0, param_count * 0.15)
        elif command_type in ("discover_params", "get_hidden_params"):
            # Chunked discovery: ~50ms per 4 params + chunked response sending
            timeout = 15.0
        elif command_type == "analyze_cross_track":
            # Cross-track: wait_ms + overhead for send routing + restore + response
            wait_ms = params.get("wait_ms", 500)
            timeout = max(3.0, (wait_ms / 1000.0) + 1.5)
        else:
            timeout = 5.0

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            with self._send_lock:
                if not self._connected:
                    if not self.connect():
                        raise ConnectionError("Could not establish M4L UDP connection.")

                # Drain any stale data in the recv socket before sending
                self._drain_recv_socket()
                self.recv_sock.settimeout(timeout)

                try:
                    self.send_sock.sendto(osc, (self.send_host, self.send_port))
                except Exception as e:
                    logger.error("Failed to send UDP command to M4L (attempt %d): %s", attempt, e)
                    if attempt < max_attempts:
                        self.disconnect()
                        time.sleep(0.2)
                        continue
                    raise ConnectionError("Failed to send command to M4L bridge.")

                try:
                    data, _addr = self.recv_sock.recvfrom(65535)
                    result = self._parse_m4l_response(data)

                    # Handle chunked responses from the M4L bridge.
                    # Large responses (>1500 chars JSON) are split into multiple
                    # UDP packets, each wrapped in an envelope: {"_c":idx,"_t":total,"_d":"base64piece"}
                    if "_c" in result and "_t" in result:
                        result = self._reassemble_chunked_response(result)

                    # Verify request_id matches -- drain stale responses if mismatch
                    resp_id = result.get("id", "")
                    if resp_id and resp_id != request_id:
                        for _drain in range(5):
                            logger.warning("M4L response id mismatch: expected %s, got %s -- draining", request_id, resp_id)
                            try:
                                data, _addr = self.recv_sock.recvfrom(65535)
                                result = self._parse_m4l_response(data)
                                if "_c" in result and "_t" in result:
                                    result = self._reassemble_chunked_response(result)
                                resp_id = result.get("id", "")
                                if not resp_id or resp_id == request_id:
                                    break
                            except socket.timeout:
                                raise Exception(f"Timeout waiting for correct M4L response (expected {request_id})")
                        else:
                            raise Exception(
                                f"M4L response ID mismatch: expected {request_id}, "
                                f"could not find matching response after 5 drain attempts"
                            )
                    return result
                except socket.timeout:
                    logger.warning("M4L response timeout (attempt %d)", attempt)
                    if attempt < max_attempts:
                        self.disconnect()
                        time.sleep(0.2)
                        continue
                    raise Exception("Timeout waiting for M4L bridge response. Is the M4L device loaded?")

    def send_command_with_retry(self, command_type: str, params: Dict[str, Any] = None, timeout: float = None, max_attempts: int = 3) -> Dict[str, Any]:
        """Send command with retry logic for 'busy' responses from M4L bridge."""
        if max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        last_result = None
        for attempt in range(max_attempts):
            result = self.send_command(command_type, params, timeout)
            if result.get("status") == "error" and "busy" in result.get("message", "").lower():
                delay = 0.5 * (attempt + 1)
                logger.warning("M4L bridge busy, retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, max_attempts)
                time.sleep(delay)
                last_result = result
                continue
            return result
        logger.error("M4L bridge remained busy after %d attempts for '%s'", max_attempts, command_type)
        return last_result

    @staticmethod
    def _parse_m4l_response(data: bytes) -> Dict[str, Any]:
        """Parse the response from the M4L bridge.

        Max's udpsend wraps the base64 string as an OSC message:
          [base64_string\\0...padding][,\\0\\0\\0]
        The OSC address (first null-terminated string) contains our
        base64-encoded JSON response.  The bridge uses URL-safe base64
        (- instead of +, _ instead of /, no = padding).
        """
        # Extract the OSC address = first null-terminated string in the packet
        null_pos = data.find(b"\x00")
        if null_pos > 0:
            osc_address = data[:null_pos].decode("utf-8", errors="replace").strip()
        else:
            osc_address = data.decode("utf-8", errors="replace").strip()

        # The OSC address is our base64-encoded JSON response
        # (udpsend uses the outlet symbol as the OSC address)
        # URL-safe base64 is the common path (v2.0.0+ bridge)
        try:
            padded = osc_address + "=" * (-len(osc_address) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, base64.binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Fallback: try standard base64
        try:
            decoded = base64.b64decode(osc_address).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, base64.binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Fallback: try raw JSON (in case response wasn't base64-encoded)
        try:
            return json.loads(osc_address)
        except (json.JSONDecodeError, ValueError):
            pass

        # Last resort: strip all nulls and try
        cleaned = data.replace(b"\x00", b"").strip()
        text = cleaned.decode("utf-8", errors="replace").strip()
        # Remove trailing comma from OSC type tag
        text = text.rstrip(",").strip()
        try:
            padded = text + "=" * (-len(text) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, base64.binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
            pass
        try:
            decoded = base64.b64decode(text).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, base64.binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
            pass

        raise json.JSONDecodeError("Could not parse M4L response", text, 0)

    def _reassemble_chunked_response(self, first_chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Reassemble a chunked response from the M4L bridge.

        Large responses are split into multiple UDP packets, each containing:
          {"_c": chunk_index, "_t": total_chunks, "_d": "url_safe_base64_piece"}
        Each _d piece decodes to a fragment of the original JSON string.
        We collect all chunks, decode each _d, concatenate, and parse.
        """
        total = first_chunk["_t"]
        logger.info("M4L chunked response: %d total chunks", total)

        # Store chunks by index
        chunks: Dict[int, str] = {first_chunk["_c"]: first_chunk["_d"]}

        # Collect remaining chunks
        # Give extra time: 100ms per chunk + 5s base
        chunk_timeout = max(5.0, total * 0.1 + 5.0)
        self.recv_sock.settimeout(chunk_timeout)

        while len(chunks) < total:
            try:
                data, _ = self.recv_sock.recvfrom(65535)
                parsed = self._parse_m4l_response(data)
                if "_c" in parsed and "_t" in parsed:
                    idx = parsed["_c"]
                    if idx in chunks:
                        logger.warning("M4L chunk reassembly: duplicate chunk %d, ignoring", idx)
                        continue
                    chunks[idx] = parsed["_d"]
                    if len(chunks) % 5 == 0:
                        logger.info("M4L chunk reassembly: %d/%d", len(chunks), total)
                else:
                    # Got a non-chunk response (maybe from another command?)
                    logger.warning("M4L chunk reassembly: got non-chunk packet, ignoring")
            except socket.timeout:
                missing = sorted(set(range(total)) - set(chunks.keys()))
                logger.error(
                    "M4L chunk reassembly: timeout after %d/%d chunks, missing: %s",
                    len(chunks), total, missing[:10]
                )
                raise Exception(
                    f"Timeout receiving chunked M4L response ({len(chunks)}/{total} chunks, "
                    f"missing: {missing[:10]})"
                )

        # Reassemble: decode each piece and concatenate
        json_parts = []
        for i in range(total):
            piece_b64 = chunks[i]
            padded = piece_b64 + "=" * (-len(piece_b64) % 4)
            piece_json = base64.urlsafe_b64decode(padded).decode("utf-8")
            json_parts.append(piece_json)

        full_json = "".join(json_parts)
        logger.info("M4L chunked response reassembled: %d chars from %d chunks", len(full_json), total)
        return json.loads(full_json)

    def ping(self) -> bool:
        """Check if the M4L bridge device is responding."""
        try:
            result = self.send_command("ping")
            success = result.get("status") == "success"
            if success:
                self._check_bridge_version(result)
            return success
        except Exception as e:
            logger.debug("M4L ping failed: %s", e)
            return False

    @staticmethod
    def _check_bridge_version(ping_result: Dict[str, Any]):
        """Extract bridge version from ping response and compare with server version.

        Stores the bridge version in state and logs a warning if the major/minor
        versions don't match the MCP server version.
        """
        from MCP_Server import __version__ as server_version

        inner = ping_result.get("result") or {}
        bridge_version = inner.get("version", "") if isinstance(inner, dict) else ""
        if not bridge_version:
            # Older bridge versions may not include a version field
            logger.info("M4L bridge did not report a version (older bridge?)")
            return

        state.m4l_bridge_version = bridge_version

        # Compare major.minor parts for compatibility
        try:
            server_parts = server_version.split(".")[:2]
            bridge_parts = bridge_version.split(".")[:2]
            if server_parts != bridge_parts:
                logger.warning(
                    "Version mismatch: MCP server v%s, M4L bridge v%s. "
                    "Some features may not work correctly. "
                    "Please update both components to matching versions.",
                    server_version,
                    bridge_version,
                )
            else:
                logger.info(
                    "M4L bridge version %s matches server version %s",
                    bridge_version,
                    server_version,
                )
        except (ValueError, IndexError):
            logger.warning(
                "Could not parse versions for comparison: server=%s, bridge=%s",
                server_version,
                bridge_version,
            )


def get_m4l_connection() -> M4LConnection:
    """Get or create a connection to the M4L bridge device.

    Always attempts a fresh connection if the existing one is dead.
    Uses a cached ping result to avoid a full UDP round trip on every call.
    """
    with state.m4l_connection_lock:
        connection = state.m4l_connection

        # If we have a connected instance, verify it still works.
        if connection is not None and connection._connected:
            # Use cached ping result if recent enough (avoids ~50-200ms round trip)
            now = time.time()
            if (now - state.m4l_ping_cache["timestamp"]) < state.M4L_PING_CACHE_TTL:
                if state.m4l_ping_cache["result"]:
                    state.m4l_status_snapshot = (True, True)
                    return connection
            # Cache expired or stale, do a live ping.
            if connection.ping():
                state.m4l_ping_cache = {
                    "result": True,
                    "timestamp": time.time(),
                }
                state.m4l_status_snapshot = (True, True)
                return connection
            # Ping failed -- tear down and try fresh.
            logger.warning("M4L bridge ping failed on existing connection, reconnecting...")
            state.m4l_status_snapshot = (False, False)
            connection.disconnect()
            if state.m4l_connection is connection:
                state.m4l_connection = None
            state.m4l_ping_cache = {"result": False, "timestamp": 0.0}

        # Invalidate the observable generation before constructing a private
        # replacement. Lock-free status readers can only see this coherent,
        # pessimistic snapshot while the transaction is in progress.
        state.m4l_status_snapshot = (False, False)
        state.m4l_ping_cache = {"result": False, "timestamp": 0.0}

        # Create and connect privately before publishing the replacement.
        connection = M4LConnection()
        if not connection.connect():
            raise ConnectionError(
                "Could not initialise M4L bridge UDP sockets. "
                "Check that port 9879 is not already in use."
            )

        state.m4l_connection = connection
        state.m4l_status_snapshot = (True, False)

        # Quick ping to verify the device is actually responding.
        if not connection.ping():
            state.m4l_ping_cache = {
                "result": False,
                "timestamp": time.time(),
            }
            state.m4l_status_snapshot = (bool(connection._connected), False)
            logger.warning("M4L UDP sockets ready but bridge device is not responding.")
            # Keep the sockets open -- the device might be loaded later.
            raise ConnectionError(
                "M4L bridge device is not responding. "
                "Make sure the AbletonBridge M4L device is loaded on a track in Ableton."
            )

        state.m4l_ping_cache = {
            "result": True,
            "timestamp": time.time(),
        }
        state.m4l_status_snapshot = (True, True)
        logger.info("M4L bridge connection established and verified.")
        return connection


def _m4l_batch_set_params(
    m4l: M4LConnection,
    track_index: int,
    device_index: int,
    parameters: List[Dict],
) -> Dict[str, Any]:
    """Set multiple hidden parameters by sending individual set_hidden_param
    commands sequentially.  More reliable than the base64-encoded batch OSC
    approach which can fail with longer payloads in Max.

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


def _m4l_result(result: dict) -> dict:
    """Extract result data from M4L response, or raise on error."""
    if result.get("status") == "success":
        return result.get("result", {})
    msg = result.get("message", "Unknown error")
    raise Exception(f"M4L bridge error: {msg}")
