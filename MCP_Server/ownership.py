"""Single-owner coordination for Ableton backend resources.

Every MCP process can expose the complete tool surface, but only one process
may own the Live, M4L, and dashboard connections.  Ownership is represented by
an exclusive loopback listener.  The same listener also exposes a tiny JSON
status response so standby processes can describe the current owner.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Union

import MCP_Server.state as state

logger = logging.getLogger("AbletonBridge")

_STATUS_PROTOCOL_VERSION = 1
_STATUS_TIMEOUT_SECONDS = 0.25
_Port = Union[int, Callable[[], int]]


@dataclass(frozen=True)
class ClaimResult:
    """Result of an automatic ownership claim."""

    acquired: bool
    control: dict
    error: Optional[str] = None


@dataclass(frozen=True)
class ReleaseResult:
    """Result of releasing ownership held by this process."""

    released: bool
    control: dict
    error: Optional[str] = None


class OwnershipManager:
    """Coordinate one backend owner across multiple local MCP processes."""

    def __init__(
        self,
        port: _Port,
        *,
        host: str = "127.0.0.1",
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._port = port
        self._host = host
        self._environment = environment if environment is not None else os.environ
        self._instance_id = str(uuid.uuid4())
        self._lock = threading.RLock()
        self._listener: Optional[socket.socket] = None
        self._responder_stop: Optional[threading.Event] = None
        self._responder_thread: Optional[threading.Thread] = None
        self._owner: Optional[dict] = None
        self._phase = "standby"
        self._active_operations = 0
        self._start_backend: Optional[Callable[[], None]] = None
        self._stop_backend: Optional[Callable[[], None]] = None

    @property
    def port(self) -> int:
        return self._port() if callable(self._port) else self._port

    def configure_backend(
        self,
        start_backend: Callable[[], None],
        stop_backend: Callable[[], None],
    ) -> None:
        """Configure lifecycle callbacks used when ownership changes."""
        with self._lock:
            self._start_backend = start_backend
            self._stop_backend = stop_backend

    def unconfigure_backend(self) -> None:
        """Remove lifecycle callbacks after the MCP lifespan ends."""
        with self._lock:
            self._start_backend = None
            self._stop_backend = None

    def is_configured(self) -> bool:
        with self._lock:
            return self._start_backend is not None and self._stop_backend is not None

    def ensure_control(self, *, client_name: Optional[str] = None) -> ClaimResult:
        """Return local ownership, claiming and starting the backend if free."""
        with self._lock:
            if self._listener is not None:
                if self._phase == "owner":
                    self._record_client_name(client_name)
                    return ClaimResult(True, self._local_status_locked())
                return ClaimResult(
                    False,
                    self._local_status_locked(),
                    "Ableton control is currently changing state. Try again shortly.",
                )

            if not self.is_configured():
                return ClaimResult(
                    False,
                    self._standby_status("available"),
                    "Ableton control lifecycle is not configured.",
                )

            try:
                listener = self._bind_listener()
            except OSError:
                listener = None

            if listener is None:
                control = self.status()
                if control["control_availability"] == "owned":
                    message = "Ableton control is owned by another task."
                else:
                    message = (
                        f"Ableton control is unavailable because loopback port "
                        f"{self.port} is occupied by an unknown process."
                    )
                return ClaimResult(False, control, message)

            self._listener = listener
            self._owner = self._build_owner_metadata(client_name)
            self._phase = "starting"
            self._start_status_responder_locked()
            start_backend = self._start_backend

        try:
            assert start_backend is not None
            start_backend()
        except Exception as exc:
            logger.error("Ableton control startup failed: %s", exc)
            self._cleanup_failed_start()
            return ClaimResult(
                False,
                self.status(),
                f"Could not start the Ableton control backend: {exc}",
            )

        with self._lock:
            self._phase = "owner"
            logger.info(
                "Ableton control acquired on port %d by process %d",
                self.port,
                os.getpid(),
            )
            return ClaimResult(True, self._local_status_locked())

    def release(self, *, force: bool = False) -> ReleaseResult:
        """Release local ownership; never release another process's ownership."""
        with self._lock:
            if self._listener is None:
                return ReleaseResult(False, self.status())

            if self._phase != "owner" and not force:
                return ReleaseResult(
                    False,
                    self._local_status_locked(),
                    "Ableton control is currently changing state. Try again shortly.",
                )

            if self._active_operations and not force:
                count = self._active_operations
                return ReleaseResult(
                    False,
                    self._local_status_locked(),
                    f"Cannot release Ableton control while {count} operation(s) are still running.",
                )

            self._phase = "releasing"
            stop_backend = self._stop_backend

        cleanup_error = None
        try:
            if stop_backend is not None:
                stop_backend()
        except Exception as exc:
            cleanup_error = str(exc)
            logger.error("Ableton control cleanup failed: %s", exc)
        finally:
            self._close_local_ownership()

        logger.info("Ableton control released by process %d", os.getpid())
        return ReleaseResult(True, self.status(), cleanup_error)

    def shutdown(self) -> None:
        """Best-effort automatic release during MCP process shutdown."""
        with self._lock:
            if self._listener is None:
                return
        self.release(force=True)

    def begin_operation(self) -> bool:
        """Register backend work, refusing work that starts after release."""
        with self._lock:
            if not self.is_configured():
                return True
            if self._listener is None or self._phase not in {"starting", "owner"}:
                return False
            self._active_operations += 1
            return True

    def end_operation(self) -> None:
        """Mark a registered backend operation complete."""
        with self._lock:
            if self._active_operations:
                self._active_operations -= 1

    def status(self) -> dict:
        """Return local role plus best-effort metadata for a remote owner."""
        with self._lock:
            if self._listener is not None:
                return self._local_status_locked()
        return self._probe_remote_owner()

    def _bind_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._host, self.port))
            listener.listen(8)
            listener.settimeout(_STATUS_TIMEOUT_SECONDS)
            return listener
        except Exception:
            listener.close()
            raise

    def _start_status_responder_locked(self) -> None:
        assert self._listener is not None
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._serve_status,
            args=(self._listener, stop_event),
            daemon=True,
            name="ableton-owner-status",
        )
        self._responder_stop = stop_event
        self._responder_thread = thread
        thread.start()

    def _serve_status(
        self,
        listener: socket.socket,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                client.settimeout(_STATUS_TIMEOUT_SECONDS)
                with self._lock:
                    payload = {
                        "service": "AbletonBridge",
                        "protocol": _STATUS_PROTOCOL_VERSION,
                        "owner": dict(self._owner) if self._owner else None,
                        "active_operations": self._active_operations,
                    }
                client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                client.close()

    def _probe_remote_owner(self) -> dict:
        try:
            with socket.create_connection(
                (self._host, self.port),
                timeout=_STATUS_TIMEOUT_SECONDS,
            ) as client:
                client.settimeout(_STATUS_TIMEOUT_SECONDS)
                chunks = []
                size = 0
                while size < 65536:
                    chunk = client.recv(65536 - size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if b"\n" in chunk:
                        break
                raw = b"".join(chunks)
        except OSError as exc:
            if isinstance(exc, ConnectionRefusedError) or exc.errno == errno.ECONNREFUSED:
                return self._standby_status("available")
            return self._standby_status("occupied_unknown")

        try:
            payload = json.loads(raw.decode("utf-8").strip())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._standby_status("occupied_unknown")

        if (
            payload.get("service") != "AbletonBridge"
            or payload.get("protocol") != _STATUS_PROTOCOL_VERSION
            or not isinstance(payload.get("owner"), dict)
        ):
            return self._standby_status("occupied_unknown")

        return {
            "control_role": "standby",
            "control_availability": "owned",
            "owner": payload["owner"],
            "active_operations": payload.get("active_operations", 0),
        }

    def _standby_status(self, availability: str) -> dict:
        return {
            "control_role": "standby",
            "control_availability": availability,
            "owner": None,
            "active_operations": 0,
        }

    def _local_status_locked(self) -> dict:
        return {
            "control_role": "owner",
            "control_availability": "owned",
            "owner": dict(self._owner) if self._owner else None,
            "active_operations": self._active_operations,
        }

    def _build_owner_metadata(self, client_name: Optional[str]) -> dict:
        owner = {
            "process_id": os.getpid(),
            "parent_process_id": os.getppid(),
            "instance_id": self._instance_id,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }
        if client_name:
            owner["client_name"] = client_name
        task_id = self._environment.get("CODEX_THREAD_ID")
        if task_id:
            owner["task_id"] = task_id
        return owner

    def _record_client_name(self, client_name: Optional[str]) -> None:
        if client_name and self._owner and "client_name" not in self._owner:
            self._owner["client_name"] = client_name

    def _cleanup_failed_start(self) -> None:
        stop_backend = None
        with self._lock:
            stop_backend = self._stop_backend
        try:
            if stop_backend is not None:
                stop_backend()
        except Exception as exc:
            logger.warning("Cleanup after backend startup failure failed: %s", exc)
        finally:
            self._close_local_ownership()

    def _close_local_ownership(self) -> None:
        with self._lock:
            stop_event = self._responder_stop
            responder = self._responder_thread
            listener = self._listener
            if stop_event is not None:
                stop_event.set()
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass

        if responder is not None and responder is not threading.current_thread():
            responder.join(timeout=1.0)

        with self._lock:
            self._listener = None
            self._responder_stop = None
            self._responder_thread = None
            self._owner = None
            self._phase = "standby"
            self._active_operations = 0


_manager = OwnershipManager(lambda: state.SINGLETON_LOCK_PORT)


def configure_backend(
    start_backend: Callable[[], None],
    stop_backend: Callable[[], None],
) -> None:
    _manager.configure_backend(start_backend, stop_backend)


def unconfigure_backend() -> None:
    _manager.unconfigure_backend()


def is_configured() -> bool:
    return _manager.is_configured()


def ensure_control(*, client_name: Optional[str] = None) -> ClaimResult:
    return _manager.ensure_control(client_name=client_name)


def release_control(*, force: bool = False) -> ReleaseResult:
    return _manager.release(force=force)


def shutdown() -> None:
    _manager.shutdown()


def begin_operation() -> bool:
    return _manager.begin_operation()


def end_operation() -> None:
    _manager.end_operation()


def get_status() -> dict:
    return _manager.status()
