import asyncio
import json
import threading
import pytest
import MCP_Server.tools._base as tool_base
from MCP_Server.ownership import ClaimResult, OwnershipManager
from MCP_Server.tools._base import _tool_handler, tool_success, tool_error, _m4l_result


class TestToolHandler:
    @pytest.mark.asyncio
    async def test_basic_success(self):
        """Plain tool results should use the shared success envelope."""
        @_tool_handler("test operation")
        def my_tool():
            """Return a successful plain-string tool result."""
            return "success"

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "success"

    @pytest.mark.asyncio
    async def test_value_error_caught(self):
        """Value errors should become structured invalid-input responses."""
        @_tool_handler("test operation")
        def my_tool():
            """Raise the input-validation failure under test."""
            raise ValueError("bad input")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Invalid input" in parsed["message"]
        assert "bad input" in parsed["message"]

    @pytest.mark.asyncio
    async def test_connection_error_caught(self):
        """Connection errors should become structured bridge responses."""
        @_tool_handler("test operation")
        def my_tool():
            """Raise the connection failure under test."""
            raise ConnectionError("no connection")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "M4L bridge not available" in parsed["message"]

    @pytest.mark.asyncio
    async def test_generic_exception_caught(self):
        """Unexpected exceptions should include the operation context."""
        @_tool_handler("doing stuff")
        def my_tool():
            """Raise the unexpected tool failure under test."""
            raise RuntimeError("something broke")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Error doing stuff" in parsed["message"]

    @pytest.mark.asyncio
    async def test_with_args(self):
        """The wrapper should preserve positional arguments."""
        @_tool_handler("test")
        def my_tool(a, b):
            """Combine the positional arguments for assertion."""
            return f"{a}+{b}"

        result = await my_tool(1, 2)
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "1+2"

    @pytest.mark.asyncio
    async def test_json_passthrough(self):
        """Responses already in JSON format should pass through unwrapped."""
        @_tool_handler("test")
        def my_tool():
            """Return an already structured JSON response."""
            return json.dumps({"tracks": [1, 2, 3]})

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["tracks"] == [1, 2, 3]
        assert "status" not in parsed

    @pytest.mark.asyncio
    async def test_control_exempt_tool_does_not_claim(self, monkeypatch):
        """A control-exempt status tool should never trigger auto-claiming."""
        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)

        def unexpected_claim(**kwargs):
            """Fail if the exempt tool incorrectly attempts ownership."""
            raise AssertionError("status tool attempted to claim control")

        monkeypatch.setattr(tool_base.ownership, "ensure_control", unexpected_claim)

        @_tool_handler("checking status", requires_control=False)
        def status_tool():
            """Return a standby result without requiring control."""
            return "standby"

        result = json.loads(await status_tool())
        assert result["message"] == "standby"

    @pytest.mark.asyncio
    async def test_control_exempt_tool_bypasses_ableton_semaphore(self, monkeypatch):
        """Status and release tools should remain callable during owner work."""
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)

        @_tool_handler("checking status", requires_control=False)
        def status_tool():
            """Return status while the controlled-tool semaphore is occupied."""
            return "standby"

        try:
            result = json.loads(await asyncio.wait_for(status_tool(), timeout=0.2))
            assert result["message"] == "standby"
        finally:
            semaphore.release()

    @pytest.mark.asyncio
    async def test_ownership_claim_is_time_bounded(self, monkeypatch):
        """A timed-out claim should finish safely without running the tool later."""
        started = threading.Event()
        finish = threading.Event()
        executions = []
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)

        def slow_claim(**_kwargs):
            """Hold ownership startup beyond the client-facing timeout."""
            started.set()
            finish.wait(timeout=1.0)
            return ClaimResult(
                acquired=True,
                control={"control_role": "owner"},
            )

        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", slow_claim)
        monkeypatch.setattr(tool_base, "_TOOL_TIMEOUT_SECONDS", 0.02)

        @_tool_handler("claiming control")
        def guarded_tool():
            """Record any execution after the ownership claim returns."""
            executions.append("ran")
            return "unexpected"

        try:
            result = json.loads(await guarded_tool())
            assert started.is_set()
            assert "timed out" in result["message"]
            assert semaphore.locked()
        finally:
            finish.set()
            for _ in range(50):
                if not semaphore.locked():
                    break
                await asyncio.sleep(0.01)
        assert not semaphore.locked()
        assert executions == []

    @pytest.mark.asyncio
    async def test_cancelled_ownership_claim_does_not_run_tool_later(self, monkeypatch):
        """Cancellation during a claim must abandon work that has not started."""
        started = threading.Event()
        finish = threading.Event()
        executions = []
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)

        def slow_claim(**_kwargs):
            """Hold ownership startup until after the caller is cancelled."""
            started.set()
            finish.wait(timeout=1.0)
            return ClaimResult(
                acquired=True,
                control={"control_role": "owner"},
            )

        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", slow_claim)

        @_tool_handler("claiming control")
        def guarded_tool():
            """Record any execution after the ownership claim returns."""
            executions.append("ran")
            return "unexpected"

        call = asyncio.create_task(guarded_tool())
        try:
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()

            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert semaphore.locked()
        finally:
            finish.set()
            for _ in range(50):
                if not semaphore.locked():
                    break
                await asyncio.sleep(0.01)

        assert not semaphore.locked()
        assert executions == []

    @pytest.mark.asyncio
    async def test_timeout_during_operation_registration_does_not_run_tool(
        self,
        monkeypatch,
    ):
        """Timeout while registering an operation must abandon the tool body."""
        registration_started = threading.Event()
        finish_registration = threading.Event()
        executions = []
        ended = []
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)
        monkeypatch.setattr(tool_base, "_TOOL_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)
        monkeypatch.setattr(
            tool_base.ownership,
            "ensure_control",
            lambda **_kwargs: ClaimResult(
                acquired=True,
                control={"control_role": "owner"},
            ),
        )

        def slow_begin_operation():
            """Hold operation registration beyond the client timeout."""
            registration_started.set()
            finish_registration.wait(timeout=1.0)
            return True

        monkeypatch.setattr(
            tool_base.ownership,
            "begin_operation",
            slow_begin_operation,
        )
        monkeypatch.setattr(
            tool_base.ownership,
            "end_operation",
            lambda: ended.append(True),
        )

        @_tool_handler("registering operation")
        def guarded_tool():
            """Record any execution after operation registration completes."""
            executions.append("ran")
            return "unexpected"

        try:
            result = json.loads(await guarded_tool())
            assert registration_started.is_set()
            assert "timed out" in result["message"]
            assert semaphore.locked()
        finally:
            finish_registration.set()
            for _ in range(50):
                if not semaphore.locked():
                    break
                await asyncio.sleep(0.01)

        assert not semaphore.locked()
        assert executions == []
        assert ended == [True]

    @pytest.mark.asyncio
    async def test_control_release_status_probe_runs_off_event_loop(self, monkeypatch):
        """Released-control status probing should not block the event loop."""
        event_loop_thread = threading.get_ident()
        status_threads = []
        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)
        monkeypatch.setattr(
            tool_base.ownership,
            "ensure_control",
            lambda **_kwargs: ClaimResult(
                acquired=True,
                control={"control_role": "owner"},
            ),
        )
        monkeypatch.setattr(tool_base.ownership, "begin_operation", lambda: False)
        monkeypatch.setattr(
            tool_base.ownership,
            "get_status",
            lambda: (
                status_threads.append(threading.get_ident())
                or {"control_role": "standby"}
            ),
        )

        @_tool_handler("changing Live")
        def guarded_tool():
            """Fail if owner-only work runs after control is released."""
            raise AssertionError("released control executed owner-only work")

        result = json.loads(await guarded_tool())
        assert result["status"] == "error"
        assert status_threads and status_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_standby_tool_returns_owner_details(
        self,
        monkeypatch,
        unused_tcp_port,
    ):
        """Standby errors should identify the process that currently owns control."""
        owner = OwnershipManager(unused_tcp_port)
        standby = OwnershipManager(unused_tcp_port)
        owner.configure_backend(lambda: None, lambda: None)
        standby.configure_backend(lambda: None, lambda: None)
        assert owner.ensure_control(client_name="owner-client").acquired is True

        monkeypatch.setattr(tool_base.ownership, "is_configured", standby.is_configured)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", standby.ensure_control)

        @_tool_handler("changing Live")
        def guarded_tool():
            """Fail if a standby process executes owner-only work."""
            raise AssertionError("standby executed owner-only work")

        try:
            result = json.loads(await guarded_tool())
            assert result["status"] == "error"
            control = result["data"]["control"]
            assert control["control_role"] == "standby"
            assert control["owner"]["client_name"] == "owner-client"
        finally:
            owner.shutdown()
            standby.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_keeps_control_busy_until_worker_finishes(
        self,
        monkeypatch,
        unused_tcp_port,
    ):
        """A timed-out tool should retain ownership and serialization until exit."""
        manager = OwnershipManager(unused_tcp_port)
        manager.configure_backend(lambda: None, lambda: None)
        started = threading.Event()
        finish = threading.Event()
        follower_started = threading.Event()
        semaphore = asyncio.Semaphore(1)

        monkeypatch.setattr(tool_base.ownership, "is_configured", manager.is_configured)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", manager.ensure_control)
        monkeypatch.setattr(tool_base.ownership, "begin_operation", manager.begin_operation)
        monkeypatch.setattr(tool_base.ownership, "end_operation", manager.end_operation)
        monkeypatch.setattr(tool_base.ownership, "get_status", manager.status)
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)
        monkeypatch.setattr(tool_base, "_TOOL_TIMEOUT_SECONDS", 0.02)

        @_tool_handler("waiting")
        def slow_tool():
            """Hold one controlled worker beyond the client timeout."""
            started.set()
            finish.wait(timeout=1.0)
            return "finished"

        @_tool_handler("following")
        def follower_tool():
            """Record when a second controlled tool actually begins."""
            follower_started.set()
            return "followed"

        try:
            result = json.loads(await slow_tool())
            assert "timed out" in result["message"]
            assert started.is_set()
            assert semaphore.locked()

            busy = manager.release()
            assert busy.released is False
            assert busy.control["active_operations"] == 1

            follower = asyncio.create_task(follower_tool())
            await asyncio.sleep(0.03)
            assert not follower_started.is_set()

            finish.set()
            for _ in range(50):
                if manager.status()["active_operations"] == 0:
                    break
                await asyncio.sleep(0.01)

            assert manager.status()["active_operations"] == 0
            assert json.loads(await follower)["message"] == "followed"
            assert manager.release().released is True
        finally:
            finish.set()
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_cancelled_tool_keeps_semaphore_until_worker_finishes(
        self,
        monkeypatch,
    ):
        """Caller cancellation must not release serialization before worker exit."""
        started = threading.Event()
        finish = threading.Event()
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(tool_base, "_ableton_semaphore", semaphore)

        @_tool_handler("cancellable")
        def slow_tool():
            """Block the worker until the cancellation assertion is complete."""
            started.set()
            finish.wait(timeout=1.0)
            return "finished"

        call = asyncio.create_task(slow_tool())
        try:
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()

            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert semaphore.locked()
        finally:
            finish.set()
            for _ in range(50):
                if not semaphore.locked():
                    break
                await asyncio.sleep(0.01)

        assert not semaphore.locked()

    @pytest.mark.asyncio
    async def test_timed_out_task_logs_late_exception(self, caplog):
        """Late failures should remain observable after the client times out."""
        async def fail_late():
            """Raise after the client-facing task has already detached."""
            raise RuntimeError("late worker failure")

        task = asyncio.create_task(fail_late())
        await asyncio.sleep(0)

        with caplog.at_level("WARNING", logger="AbletonBridge"):
            tool_base._consume_background_result(task)

        assert "late worker failure" in caplog.text


class TestToolSuccess:
    def test_basic(self):
        """Success responses should contain status and message fields."""
        result = json.loads(tool_success("Done"))
        assert result["status"] == "ok"
        assert result["message"] == "Done"

    def test_with_data(self):
        """Success responses should include optional structured data."""
        result = json.loads(tool_success("Done", {"count": 5}))
        assert result["data"]["count"] == 5


class TestToolError:
    def test_basic(self):
        """Error responses should contain status and message fields."""
        result = json.loads(tool_error("Failed"))
        assert result["status"] == "error"
        assert result["message"] == "Failed"


class TestM4lResult:
    def test_success(self):
        """Successful M4L envelopes should return their result payload."""
        result = _m4l_result({"status": "success", "result": {"value": 42}})
        assert result["value"] == 42

    def test_error_raises(self):
        """Failed M4L envelopes should raise with the bridge message."""
        with pytest.raises(Exception, match="M4L bridge error"):
            _m4l_result({"status": "error", "message": "device not found"})
