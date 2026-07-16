"""Shared tool infrastructure: decorators, helpers, error formatting."""
import asyncio
import functools
import json
import logging

import MCP_Server.ownership as ownership

logger = logging.getLogger("AbletonBridge")

# Limits concurrent tool executions that use the Ableton TCP connection.
# Set to 1 because the TCP protocol is strictly request-response on a single socket.
# This prevents thread pool exhaustion and ensures orderly command dispatch.
_ableton_semaphore = asyncio.Semaphore(1)

# Absolute timeout for any single tool call (prevents a stuck tool from
# blocking the semaphore indefinitely).
_TOOL_TIMEOUT_SECONDS = 120.0


class _ControlReleasedError(RuntimeError):
    """Raised when queued backend work starts after ownership was released."""


def _tool_handler(error_prefix: str, *, requires_control: bool = True):
    """Decorator that wraps tool functions with standard error handling.

    Runs the synchronous tool function in a thread pool via asyncio.to_thread()
    so it doesn't block the FastMCP async event loop during TCP/UDP I/O.

    An asyncio.Semaphore gates entry so that only one tool occupies the thread
    pool (and the shared TCP socket) at a time. An outer timeout ensures a
    stuck tool releases the semaphore after _TOOL_TIMEOUT_SECONDS.

    All plain-string returns are wrapped in tool_success() for consistent JSON
    envelope. Returns that are already JSON (start with '{' or '[') pass through.

    Catches ValueError -> tool_error("Invalid input: ..."),
    ConnectionError -> tool_error("M4L bridge not available: ..."),
    Exception -> tool_error("Error {prefix}: ...")
    """
    def decorator(func):
        """Decorate one synchronous tool function with the shared contract."""
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            """Execute the wrapped tool through ownership and timeout guards."""
            async def invoke():
                """Claim control when required and run one guarded tool call."""
                track_control = requires_control and ownership.is_configured()
                if track_control:
                    claim_task = asyncio.create_task(
                        asyncio.to_thread(
                            ownership.ensure_control,
                            client_name=_get_client_name(args, kwargs),
                        )
                    )
                    try:
                        claim = await asyncio.wait_for(
                            asyncio.shield(claim_task),
                            timeout=_TOOL_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        claim_task.add_done_callback(_consume_background_result)
                        raise
                    if not claim.acquired:
                        return tool_error(
                            claim.error or "Ableton control is unavailable.",
                            {"control": claim.control},
                        )

                task = asyncio.create_task(
                    asyncio.to_thread(
                        _run_sync_tool,
                        func,
                        args,
                        kwargs,
                        track_control,
                    )
                )
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_TOOL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # The worker thread cannot be cancelled. Keep observing it
                    # so ownership remains busy until the real work finishes.
                    task.add_done_callback(_consume_background_result)
                    raise

            try:
                if requires_control:
                    async with _ableton_semaphore:
                        result = await invoke()
                else:
                    # Status and release are recovery paths.  They must remain
                    # callable while an owner-dependent tool holds the socket
                    # semaphore or an ownership claim is still starting.
                    result = await invoke()
                if isinstance(result, str):
                    stripped = result.strip()
                    if stripped.startswith(("{", "[")):
                        return result  # already structured JSON
                    return tool_success(result)
                return result
            except asyncio.TimeoutError:
                logger.error("Tool timed out after %ds: %s", _TOOL_TIMEOUT_SECONDS, error_prefix)
                return tool_error(f"Tool timed out after {_TOOL_TIMEOUT_SECONDS}s: {error_prefix}")
            except ValueError as e:
                return tool_error(f"Invalid input: {e}")
            except ConnectionError as e:
                return tool_error(f"M4L bridge not available: {e}")
            except _ControlReleasedError as e:
                control = await asyncio.to_thread(ownership.get_status)
                return tool_error(str(e), {"control": control})
            except Exception as e:
                logger.error("Error %s: %s", error_prefix, e)
                return tool_error(f"Error {error_prefix}: {e}")
        return wrapper
    return decorator


def _run_sync_tool(func, args: tuple, kwargs: dict, track_control: bool):
    """Run a sync tool while tracking work that may outlive its async timeout."""
    if track_control and not ownership.begin_operation():
        raise _ControlReleasedError(
            "Ableton control was released before this operation began. Try again."
        )
    try:
        return func(*args, **kwargs)
    finally:
        if track_control:
            ownership.end_operation()


def _consume_background_result(task: asyncio.Task) -> None:
    """Retrieve a timed-out task's result so late exceptions are not leaked."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("Could not inspect timed-out tool task: %s", exc)
    else:
        if exc is not None:
            logger.warning("Timed-out tool task finished with error: %s", exc)


def _get_client_name(args: tuple, kwargs: dict) -> str | None:
    """Read the MCP initialize client name from a tool Context when available."""
    for candidate in (*args, *kwargs.values()):
        try:
            params = candidate.session.client_params
            name = params.clientInfo.name if params and params.clientInfo else None
        except (AttributeError, ValueError):
            continue
        if isinstance(name, str) and name:
            return name
    return None


def _m4l_result(result: dict) -> dict:
    """Extract result data from M4L response, or raise on error."""
    if result.get("status") == "success":
        return result.get("result", {})
    msg = result.get("message", "Unknown error")
    raise Exception(f"M4L bridge error: {msg}")


def tool_success(message: str, data: dict = None) -> str:
    """Create a standardized success response."""
    result = {"status": "ok", "message": message}
    if data:
        result["data"] = data
    return json.dumps(result)


def tool_error(message: str, data: dict = None) -> str:
    """Create a standardized error response."""
    result = {"status": "error", "message": message}
    if data:
        result["data"] = data
    return json.dumps(result)


def _report_progress(ctx, current: float, total: float, message: str = None):
    """Report progress from a sync tool thread.

    ctx.report_progress() is async, but tools run in asyncio.to_thread().
    This helper bridges the gap by scheduling the coroutine on the event loop.
    Fails silently if the event loop is unavailable.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(
                ctx.report_progress(current, total, message), loop
            )
    except Exception:
        pass  # progress is best-effort
