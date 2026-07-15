import asyncio
import json
import threading
import pytest
import MCP_Server.tools._base as tool_base
from MCP_Server.ownership import OwnershipManager
from MCP_Server.tools._base import _tool_handler, tool_success, tool_error, _m4l_result


class TestToolHandler:
    @pytest.mark.asyncio
    async def test_basic_success(self):
        @_tool_handler("test operation")
        def my_tool():
            return "success"

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["message"] == "success"

    @pytest.mark.asyncio
    async def test_value_error_caught(self):
        @_tool_handler("test operation")
        def my_tool():
            raise ValueError("bad input")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Invalid input" in parsed["message"]
        assert "bad input" in parsed["message"]

    @pytest.mark.asyncio
    async def test_connection_error_caught(self):
        @_tool_handler("test operation")
        def my_tool():
            raise ConnectionError("no connection")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "M4L bridge not available" in parsed["message"]

    @pytest.mark.asyncio
    async def test_generic_exception_caught(self):
        @_tool_handler("doing stuff")
        def my_tool():
            raise RuntimeError("something broke")

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "Error doing stuff" in parsed["message"]

    @pytest.mark.asyncio
    async def test_with_args(self):
        @_tool_handler("test")
        def my_tool(a, b):
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
            return json.dumps({"tracks": [1, 2, 3]})

        result = await my_tool()
        parsed = json.loads(result)
        assert parsed["tracks"] == [1, 2, 3]
        assert "status" not in parsed

    @pytest.mark.asyncio
    async def test_control_exempt_tool_does_not_claim(self, monkeypatch):
        monkeypatch.setattr(tool_base.ownership, "is_configured", lambda: True)

        def unexpected_claim(**kwargs):
            raise AssertionError("status tool attempted to claim control")

        monkeypatch.setattr(tool_base.ownership, "ensure_control", unexpected_claim)

        @_tool_handler("checking status", requires_control=False)
        def status_tool():
            return "standby"

        result = json.loads(await status_tool())
        assert result["message"] == "standby"

    @pytest.mark.asyncio
    async def test_standby_tool_returns_owner_details(
        self,
        monkeypatch,
        unused_tcp_port,
    ):
        owner = OwnershipManager(unused_tcp_port)
        standby = OwnershipManager(unused_tcp_port)
        owner.configure_backend(lambda: None, lambda: None)
        standby.configure_backend(lambda: None, lambda: None)
        assert owner.ensure_control(client_name="owner-client").acquired is True

        monkeypatch.setattr(tool_base.ownership, "is_configured", standby.is_configured)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", standby.ensure_control)

        @_tool_handler("changing Live")
        def guarded_tool():
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
        manager = OwnershipManager(unused_tcp_port)
        manager.configure_backend(lambda: None, lambda: None)
        started = threading.Event()
        finish = threading.Event()

        monkeypatch.setattr(tool_base.ownership, "is_configured", manager.is_configured)
        monkeypatch.setattr(tool_base.ownership, "ensure_control", manager.ensure_control)
        monkeypatch.setattr(tool_base.ownership, "begin_operation", manager.begin_operation)
        monkeypatch.setattr(tool_base.ownership, "end_operation", manager.end_operation)
        monkeypatch.setattr(tool_base.ownership, "get_status", manager.status)
        monkeypatch.setattr(tool_base, "_TOOL_TIMEOUT_SECONDS", 0.02)

        @_tool_handler("waiting")
        def slow_tool():
            started.set()
            finish.wait(timeout=1.0)
            return "finished"

        try:
            result = json.loads(await slow_tool())
            assert "timed out" in result["message"]
            assert started.is_set()

            busy = manager.release()
            assert busy.released is False
            assert busy.control["active_operations"] == 1

            finish.set()
            for _ in range(50):
                if manager.status()["active_operations"] == 0:
                    break
                await asyncio.sleep(0.01)

            assert manager.status()["active_operations"] == 0
            assert manager.release().released is True
        finally:
            finish.set()
            manager.shutdown()


class TestToolSuccess:
    def test_basic(self):
        result = json.loads(tool_success("Done"))
        assert result["status"] == "ok"
        assert result["message"] == "Done"

    def test_with_data(self):
        result = json.loads(tool_success("Done", {"count": 5}))
        assert result["data"]["count"] == 5


class TestToolError:
    def test_basic(self):
        result = json.loads(tool_error("Failed"))
        assert result["status"] == "error"
        assert result["message"] == "Failed"


class TestM4lResult:
    def test_success(self):
        result = _m4l_result({"status": "success", "result": {"value": 42}})
        assert result["value"] == 42

    def test_error_raises(self):
        with pytest.raises(Exception, match="M4L bridge error"):
            _m4l_result({"status": "error", "message": "device not found"})
