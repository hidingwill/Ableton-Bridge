"""AbletonConnection — TCP socket connection to the Ableton Remote Script."""

import json
import logging
import select
import socket
import time
import threading
from dataclasses import dataclass
from typing import Dict, Any, Optional

from MCP_Server.constants import TIER_0_COMMANDS, TIER_1_COMMANDS, TIER_2_COMMANDS, MODIFYING_COMMANDS
import MCP_Server.state as state

logger = logging.getLogger("AbletonBridge")

# Phase 4.5: Non-idempotent commands should NOT be retried automatically
# because a retry could create duplicate tracks, clips, etc.
NON_IDEMPOTENT_COMMANDS = frozenset([
    "create_midi_track", "create_audio_track", "create_clip",
    "create_return_track", "create_scene", "delete_track",
    "delete_clip", "delete_scene", "delete_device",
    "duplicate_track", "duplicate_clip", "duplicate_scene", "add_notes_to_clip",
    "add_notes_extended", "delete_return_track",
])


class CommandCancelled(RuntimeError):
    """Raised when cooperative shutdown cancels an in-flight command."""


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    _udp_sock: socket.socket = None
    _udp_port: int = 9882

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            self._last_socket_open = True
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self._recv_buffer = ""  # Clear buffer on new connection
            self._last_socket_open = True
            logger.info("Connected to Ableton at %s:%s", self.host, self.port)
            return True
        except Exception as e:
            logger.error("Failed to connect to Ableton: %s", e)
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.sock = None
            self._last_socket_open = False
            return False

    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error("Error disconnecting from Ableton: %s", e)
            finally:
                self.sock = None
                self._last_socket_open = False
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except Exception:
                pass
            finally:
                self._udp_sock = None

    def __post_init__(self):
        """Initialize per-connection receive buffering and send serialization."""
        self._recv_buffer = ""
        self._send_lock = threading.Lock()
        self._last_socket_open = self.sock is not None

    def is_connected(self) -> bool:
        """Passively check whether the Remote Script socket is still open.

        The check never sends or consumes protocol data. If a command currently
        owns the send lock, return the last verified socket result so a status
        request cannot interfere with its response handling.
        """
        sock = self.sock
        if sock is None:
            self._last_socket_open = False
            return False

        try:
            sock.getpeername()
        except OSError:
            self._last_socket_open = False
            return False

        if not self._send_lock.acquire(blocking=False):
            return self._last_socket_open

        try:
            readable, _writable, _exceptional = select.select([sock], [], [], 0)
            if not readable:
                self._last_socket_open = True
                return True
            try:
                self._last_socket_open = bool(sock.recv(1, socket.MSG_PEEK))
            except (BlockingIOError, socket.timeout):
                self._last_socket_open = True
            except OSError:
                self._last_socket_open = False
        except (OSError, ValueError):
            self._last_socket_open = False
        finally:
            self._send_lock.release()
        return self._last_socket_open

    def _ensure_udp_socket(self):
        """Create a UDP socket for real-time parameter sending if not already open."""
        if self._udp_sock is None:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return self._udp_sock

    def send_udp_command(self, command_type: str, params: Dict[str, Any] = None):
        """Send a fire-and-forget UDP command to the Remote Script.

        No response is expected or waited for.
        """
        sock = self._ensure_udp_socket()
        command = {
            "type": command_type,
            "params": params or {}
        }
        payload = json.dumps(command).encode("utf-8")
        sock.sendto(payload, (self.host, self._udp_port))
        logger.debug("Sent UDP command: %s", command_type)

    def receive_full_response(
        self,
        sock,
        buffer_size=8192,
        timeout=15.0,
        stop_event: Optional[threading.Event] = None,
    ):
        """Receive a complete newline-delimited JSON response and return the parsed object"""
        deadline = time.monotonic() + timeout
        sock.settimeout(min(timeout, 0.25) if stop_event is not None else timeout)

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise CommandCancelled("Ableton command cancelled during shutdown")

                # Check if we already have a complete line in the buffer
                if '\n' in self._recv_buffer:
                    line, self._recv_buffer = self._recv_buffer.split('\n', 1)
                    line = line.strip()
                    if line:
                        try:
                            result = json.loads(line)
                        except json.JSONDecodeError:
                            logger.error("Malformed JSON from Ableton (first 200 chars): %s", line[:200])
                            raise
                        logger.debug("Received complete response (%d chars)", len(line))
                        return result

                try:
                    if stop_event is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise socket.timeout()
                        sock.settimeout(min(0.25, remaining))
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        raise Exception("Connection closed before receiving any data")

                    self._recv_buffer += chunk.decode('utf-8')
                except socket.timeout:
                    if stop_event is not None:
                        if stop_event.is_set():
                            raise CommandCancelled(
                                "Ableton command cancelled during shutdown"
                            ) from None
                        if time.monotonic() < deadline:
                            continue
                    logger.warning("Socket timeout during receive")
                    raise
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error("Socket connection error during receive: %s", e)
                    raise
        except (socket.timeout, json.JSONDecodeError, CommandCancelled):
            raise
        except Exception as e:
            logger.error("Error during receive: %s", e)
            raise

    def _reconnect(self) -> bool:
        """Force a fresh reconnection, clearing all state."""
        logger.info("Forcing reconnection to Ableton...")
        self.disconnect()
        self._recv_buffer = ""
        return self.connect()

    def send_command(
        self,
        command_type: str,
        params: Dict[str, Any] = None,
        timeout: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Includes automatic retry: if the first attempt fails due to a
        socket error, the connection is reset and the command is retried once.
        Adds small delays around modifying commands for stability.

        Non-idempotent commands (create/delete operations) are NOT retried
        to prevent duplicate side-effects (Phase 4.5).
        """
        # Phase 4.5: non-idempotent commands get a single attempt
        max_attempts = 1 if command_type in NON_IDEMPOTENT_COMMANDS else 2
        is_modifying = command_type in MODIFYING_COMMANDS

        # Determine delay tier: reduced delays since the async semaphore in
        # _tool_handler already serializes tool calls, preventing command flooding.
        # Tier 0 = no delay, Tier 1 = 10ms post, Tier 2 = 10ms pre+post
        if command_type in TIER_2_COMMANDS:
            pre_delay, post_delay = 0.01, 0.01
        elif command_type in TIER_1_COMMANDS:
            pre_delay, post_delay = 0, 0.01
        else:
            pre_delay, post_delay = 0, 0

        for attempt in range(1, max_attempts + 1):
            with self._send_lock:
                if stop_event is not None and stop_event.is_set():
                    raise CommandCancelled("Ableton command cancelled during shutdown")
                if not self.sock and not self.connect():
                    raise ConnectionError("Not connected to Ableton")
                if stop_event is not None and stop_event.is_set():
                    self.disconnect()
                    raise CommandCancelled("Ableton command cancelled during shutdown")

                command = {
                    "type": command_type,
                    "params": params or {}
                }

                try:
                    logger.debug("Sending command: %s (attempt %d)", command_type, attempt)

                    # Send the command as newline-delimited JSON
                    self.sock.sendall((json.dumps(command) + '\n').encode('utf-8'))

                    # Pre-delay: give Ableton time to process before we read the response
                    if pre_delay:
                        if stop_event is not None:
                            if stop_event.wait(pre_delay):
                                raise CommandCancelled(
                                    "Ableton command cancelled during shutdown"
                                )
                        else:
                            time.sleep(pre_delay)

                    # Set timeout based on command type (caller override takes priority)
                    if timeout is None:
                        from MCP_Server.constants import SLOW_COMMAND_TIMEOUTS
                        timeout = SLOW_COMMAND_TIMEOUTS.get(
                            command_type, 15.0 if is_modifying else 10.0
                        )
                    # Receive the response (already parsed by receive_full_response)
                    response = self.receive_full_response(
                        self.sock,
                        timeout=timeout,
                        stop_event=stop_event,
                    )
                    logger.debug("Response status: %s", response.get('status', 'unknown'))

                    if response.get("status") == "error":
                        logger.error("Ableton error: %s", response.get('message'))
                        raise Exception(response.get("message", "Unknown error from Ableton"))

                    # Post-delay: let Ableton settle before the next command
                    if post_delay:
                        if stop_event is not None:
                            if stop_event.wait(post_delay):
                                raise CommandCancelled(
                                    "Ableton command cancelled during shutdown"
                                )
                        else:
                            time.sleep(post_delay)

                    return response.get("result", {})

                except CommandCancelled:
                    self.disconnect()
                    self._recv_buffer = ""
                    raise
                except Exception as e:
                    logger.error("Command '%s' attempt %d failed: %s", command_type, attempt, e)
                    # Close the broken socket and clear buffer
                    self.disconnect()
                    self._recv_buffer = ""

                    if attempt < max_attempts:
                        # Wait briefly then retry with a fresh connection
                        if stop_event is not None:
                            if stop_event.wait(0.1):
                                raise CommandCancelled(
                                    "Ableton command cancelled during shutdown"
                                ) from None
                        else:
                            time.sleep(0.1)
                        if not self.connect():
                            raise ConnectionError("Failed to reconnect to Ableton") from e
                        logger.info("Reconnected, retrying command...")
                    else:
                        raise Exception(
                            f"Command '{command_type}' failed after {max_attempts} attempts: {e}"
                        ) from e


def get_ableton_connection():
    """Get or create a persistent Ableton connection"""

    if state.ableton_connection is not None:
        try:
            if not state.ableton_connection.is_connected():
                raise ConnectionError("Socket is no longer connected")
            state.ableton_connected_event.set()
            return state.ableton_connection
        except Exception as e:
            logger.warning("Existing connection is no longer valid: %s", e)
            try:
                state.ableton_connection.disconnect()
            except Exception:
                pass
            state.ableton_connection = None

    # Connection doesn't exist or is invalid, create a new one
    if state.ableton_connection is None:
        # Try to connect up to 3 times with a short delay between attempts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info("Connecting to Ableton (attempt %d/%d)...", attempt, max_attempts)
                state.ableton_connection = AbletonConnection(host="localhost", port=9877)
                if state.ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")

                    # Validate connection with a simple command
                    try:
                        # Get session info as a test
                        state.ableton_connection.send_command("get_session_info")
                        logger.info("Connection validated successfully")
                        state.ableton_connected_event.set()
                        return state.ableton_connection
                    except Exception as e:
                        logger.error("Connection validation failed: %s", e)
                        state.ableton_connection.disconnect()
                        state.ableton_connection = None
                        # Continue to next attempt
                else:
                    state.ableton_connection = None
            except Exception as e:
                logger.error("Connection attempt %d failed: %s", attempt, e)
                if state.ableton_connection:
                    state.ableton_connection.disconnect()
                    state.ableton_connection = None

            # Wait before trying again, but only if we have more attempts left
            if attempt < max_attempts:
                time.sleep(1.0)

        # If we get here, all connection attempts failed
        if state.ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")

    return state.ableton_connection
