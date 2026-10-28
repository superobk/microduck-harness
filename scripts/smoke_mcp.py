#!/usr/bin/env python3
"""Official MCP client smoke test. Default is read-only; demo mutation is explicit.

Examples:
    python scripts/smoke_mcp.py --url http://127.0.0.1:8765/mcp
    GATEWAY_TOKEN=... python scripts/smoke_mcp.py --url http://127.0.0.1:8765/mcp --start-demo

Never put a bearer token in a command-line argument or URL. --start-demo verifies
that the gateway reports demo mode before it submits any task.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def object_result(result) -> dict:
    if result.isError:
        # Do not echo arbitrary remote tool output or exception detail.
        raise RuntimeError("MCP tool reported an error")
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for part in result.content:
        if part.type == "text":
            value = json.loads(part.text)
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP tool did not return a JSON object")


def emit(kind: str, value):
    print(json.dumps({"kind": kind, "value": value}, ensure_ascii=False), flush=True)


async def run(args) -> None:
    token = os.getenv(args.token_env, "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=httpx.Timeout(15, read=45)) as http:
        async with streamable_http_client(args.url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                emit("initialized", {"server": initialized.serverInfo.name, "protocol": initialized.protocolVersion})
                listed = await session.list_tools()
                emit("tools", [tool.name for tool in listed.tools])
                state = object_result(await session.call_tool("duck_get_state", {}))
                emit("state", state)
                observation = object_result(await session.call_tool("duck_observe", {}))
                emit("observation", observation)
                if not args.start_demo:
                    emit("complete", {"read_only": True, "task_started": False})
                    return
                if state.get("mode") != "demo" or state.get("robot", {}).get("backend") != "kinematic-demo":
                    raise RuntimeError("--start-demo refuses a gateway that is not the kinematic demo")
                task = object_result(await session.call_tool("duck_start_task", {
                    "request_id": "mcp-smoke-" + uuid.uuid4().hex[:12], "instruction": "探索工作区域",
                }))
                emit("task_accepted", task)
                task_id = task["id"]
                try:
                    status = object_result(await session.call_tool("duck_task_status", {"task_id": task_id}))
                    emit("task_status", status)
                finally:
                    cancelled = object_result(await session.call_tool("duck_cancel_task", {"task_id": task_id}))
                    emit("cancel_receipt", cancelled)
                final = object_result(await session.call_tool("duck_task_status", {"task_id": task_id}))
                emit("final_task_status", final)
                emit("complete", {"read_only": False, "demo_only": True})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp", help="StreamableHTTP MCP endpoint")
    parser.add_argument("--token-env", default="GATEWAY_TOKEN", help="Name of environment variable holding bearer token")
    parser.add_argument("--start-demo", action="store_true", help="Start, poll and cancel one task only after checking demo mode")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        parser.error("--url must be HTTP(S), without embedded credentials, query or fragment")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Smoke test interrupted; check task status if a demo was started.", file=sys.stderr)
        return 130
    except Exception:
        # Async transports may wrap HTTP failures in ExceptionGroup. Do not print
        # unreviewed URLs/response bodies; server status and auth can be checked separately.
        print("MCP smoke test failed. Check endpoint, bearer token, server logs and observation availability.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
