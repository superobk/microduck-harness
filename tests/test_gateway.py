"""Actual loopback HTTP + official MCP client protocol integration tests."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import pytest_asyncio
import uvicorn

from microduck_harness.backends import DemoWorld
from microduck_harness.config import Settings
from microduck_harness.engine import Engine
from microduck_harness.gateway import create_app


def tool_object(result):
    assert not result.isError, result.content
    text_blocks = [part.text for part in result.content if part.type == "text"]
    assert text_blocks, "MCP result must preserve a text JSON block for clients without structuredContent"
    decoded = json.loads(text_blocks[0])
    assert isinstance(decoded, dict)
    if result.structuredContent is not None:
        assert result.structuredContent == decoded
    return decoded


class CountingWorld(DemoWorld):
    def __init__(self, settings):
        super().__init__(settings, realtime=False)
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        return await super().stop()


@pytest_asyncio.fixture
async def network_gateway(tmp_path):
    token = "test-gateway-token-never-for-production"
    settings = Settings(database=str(tmp_path / "gateway.sqlite3"), gateway_token=token)
    world = CountingWorld(settings)
    engine = Engine(settings, backend=world)
    app = create_app(settings, engine)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False,
        lifespan="on", timeout_graceful_shutdown=3,
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
                raise AssertionError("Uvicorn exited before startup")
            await asyncio.sleep(.01)
        assert server.started
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}", token=token,
                              engine=engine, world=world, app=app)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


@asynccontextmanager
async def mcp_session(gateway):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + gateway.token},
        trust_env=False, timeout=httpx.Timeout(10, read=30),
    ) as http:
        async with streamable_http_client(gateway.url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                yield session, initialized


@pytest.mark.asyncio
async def test_official_mcp_initialize_list_observe_start_poll_cancel(network_gateway):
    gateway = network_gateway
    pulse_entered = asyncio.Event()

    async def delayed_pulse(action, observation, request_id):
        pulse_entered.set()
        await asyncio.Event().wait()  # A cancel must interrupt this task, not await natural completion.

    gateway.world.execute = delayed_pulse
    async with mcp_session(gateway) as (session, initialized):
        assert initialized.serverInfo.name == "Microduck Harness"
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert {"duck_get_state", "duck_observe", "duck_start_task", "duck_task_status",
                "duck_cancel_task", "duck_stop_walk"} <= set(tools)
        assert tools["duck_observe"].annotations.readOnlyHint is True
        assert tools["duck_start_task"].annotations.readOnlyHint is False
        observed = tool_object(await session.call_tool("duck_observe", {}))
        assert observed["source"] == "kinematic-demo"
        assert observed["pose_valid"] is True
        assert gateway.world.stop_calls == 0
        assert gateway.world.actions == []

        arguments = {"instruction": "探索工作区域", "request_id": "mcp-integration-1"}
        accepted = tool_object(await session.call_tool("duck_start_task", arguments))
        assert accepted["status"] == "accepted" and accepted["verified_success"] is False
        await asyncio.wait_for(pulse_entered.wait(), 2)
        status = tool_object(await session.call_tool("duck_task_status", {"task_id": accepted["id"]}))
        assert status["status"] == "running" and status["verified_success"] is False

        duplicate = tool_object(await session.call_tool("duck_start_task", arguments))
        assert duplicate["id"] == accepted["id"]
        conflict = await session.call_tool("duck_start_task", {
            "instruction": "找到 paper", "request_id": arguments["request_id"],
        })
        # MCP tool-domain errors are isError:true, not a REST HTTP409 response.
        assert conflict.isError is True

        cancelled = tool_object(await session.call_tool("duck_cancel_task", {"task_id": accepted["id"]}))
        assert cancelled["task"]["status"] == "cancelled"
        final = tool_object(await session.call_tool("duck_task_status", {"task_id": accepted["id"]}))
        assert final["status"] == "cancelled"
        assert final["verified_success"] is False
        assert gateway.world.stop_calls >= 2


@pytest.mark.asyncio
async def test_dashboard_health_and_readonly_endpoints_issue_no_motion_commands(network_gateway):
    gateway = network_gateway
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as http:
        page = await http.get("/")
        assert page.status_code == 200
        assert "html" in page.headers["content-type"]
        health = await http.get("/health")
        assert health.status_code == 200
        assert health.json()["hardware_tested"] is False
        headers = {"Authorization": "Bearer " + gateway.token}
        assert (await http.get("/api/state", headers=headers)).status_code == 200
        assert (await http.get("/api/observe", headers=headers)).status_code == 200
    async with mcp_session(gateway) as (session, _):
        await session.list_tools()
        state = tool_object(await session.call_tool("duck_get_state", {}))
        assert state["robot"]["servo_count"] == 15
        tool_object(await session.call_tool("duck_observe", {}))
    assert gateway.world.actions == []
    assert gateway.world.stop_calls == 0
    assert gateway.engine.runner is None


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
@pytest.mark.asyncio
async def test_rest_and_mcp_require_valid_bearer_token(network_gateway, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    async with httpx.AsyncClient(base_url=network_gateway.url, trust_env=False) as http:
        assert (await http.get("/api/state", headers=headers)).status_code == 401
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=headers)
        assert response.status_code == 401
    assert network_gateway.world.stop_calls == 0


@pytest.mark.asyncio
async def test_cross_origin_access_is_rejected_even_with_valid_bearer(network_gateway):
    gateway = network_gateway
    headers = {"Authorization": "Bearer " + gateway.token, "Origin": "https://unrelated.example"}
    async with httpx.AsyncClient(base_url=gateway.url, trust_env=False) as http:
        assert (await http.get("/api/state", headers=headers)).status_code == 403
        assert (await http.post("/api/tasks", json={"request_id": "cross-origin", "instruction": "探索"}, headers=headers)).status_code == 403
        assert (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=headers)).status_code == 403
        headers["Origin"] = gateway.url
        assert (await http.get("/api/state", headers=headers)).status_code == 200
    assert gateway.world.stop_calls == 0


@pytest.mark.asyncio
async def test_rest_task_idempotency_and_conflicts_use_http409(network_gateway):
    gateway = network_gateway
    pulse_entered = asyncio.Event()

    async def delayed_pulse(action, observation, request_id):
        pulse_entered.set()
        await asyncio.Event().wait()

    gateway.world.execute = delayed_pulse
    headers = {"Authorization": "Bearer " + gateway.token}
    async with httpx.AsyncClient(base_url=gateway.url, headers=headers, trust_env=False) as http:
        payload = {"request_id": "rest-idempotency", "instruction": "探索"}
        first = await http.post("/api/tasks", json=payload)
        assert first.status_code == 202
        await asyncio.wait_for(pulse_entered.wait(), 2)
        duplicate = await http.post("/api/tasks", json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["id"] == first.json()["id"]
        changed = await http.post("/api/tasks", json={**payload, "instruction": "找到 paper"})
        assert changed.status_code == 409
        another = await http.post("/api/tasks", json={**payload, "request_id": "new-owner"})
        assert another.status_code == 409
        cancelled = await http.post("/api/tasks/" + first.json()["id"] + "/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["task"]["status"] == "cancelled"


@pytest.mark.parametrize("start_demo", [False, True])
@pytest.mark.asyncio
async def test_smoke_cli_uses_real_transport_and_defaults_to_readonly(network_gateway, start_demo):
    gateway = network_gateway

    async def delayed_pulse(action, observation, request_id):
        await asyncio.Event().wait()

    if start_demo:
        gateway.world.execute = delayed_pulse
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_mcp.py"
    command = [sys.executable, str(script), "--url", gateway.url + "/mcp"]
    if start_demo:
        command.append("--start-demo")
    process = await asyncio.create_subprocess_exec(
        *command, env={**os.environ, "GATEWAY_TOKEN": gateway.token},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, errors = await asyncio.wait_for(process.communicate(), 20)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, errors.decode()
    assert gateway.token not in output.decode() and gateway.token not in errors.decode()
    events = [json.loads(line) for line in output.decode().splitlines()]
    assert events[0]["kind"] == "initialized"
    assert events[-1]["kind"] == "complete"
    if start_demo:
        final = next(e["value"] for e in events if e["kind"] == "final_task_status")
        assert final["status"] == "cancelled"
        assert events[-1]["value"]["demo_only"] is True
    else:
        assert events[-1]["value"] == {"read_only": True, "task_started": False}
        assert gateway.world.stop_calls == 0 and gateway.world.actions == []
