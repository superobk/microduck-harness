from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Settings
from .engine import Conflict, Engine
from .models import StartRequest
from .model_config import ModelConfigError, ModelConfigConflict, ModelConfigUpdate
from . import __version__


def create_app(settings: Settings | None = None, engine: Engine | None = None):
    settings = settings or Settings.load()
    engine = engine or Engine(settings)
    mcp = FastMCP(
        "Microduck Harness", stateless_http=True, json_response=True,
        instructions=("Operate a full 15-servo Microduck through bounded explore/find/approach goals. "
                      "Accepted tasks are not completed tasks. Poll task status and report verified_success. "
                      "Never claim a pickup: this version has no autonomous manipulation tool. "
                      "Stopping a chat does not stop the robot; call duck_cancel_task or duck_stop_walk."),
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    mutation = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

    @mcp.tool(annotations=readonly)
    async def duck_get_state() -> dict:
        """Get mode, robot status, active task, bounded recent tasks and decision metrics."""
        return await engine.status()

    @mcp.tool(annotations=readonly)
    async def duck_observe() -> dict:
        """Get timestamped pose, detections and measured distance; missing evidence stays null."""
        return (await engine.observe()).model_dump()

    @mcp.tool(annotations=mutation)
    async def duck_start_task(instruction: str, request_id: str) -> dict:
        """Start one bounded explore/find/approach task. Use a unique request_id for a new goal.

        Example: instruction='靠近 paper 并报告位置', request_id='approach-001'.
        Repeating the same ID with the identical instruction returns the original receipt.
        Returns immediately. Completion must be read using duck_task_status.
        """
        return await engine.start(StartRequest(request_id=request_id, instruction=instruction))

    @mcp.tool(annotations=readonly)
    async def duck_task_status(task_id: str) -> dict:
        """Read a task receipt. succeeded and verified_success describe observed task evidence."""
        return engine.task(task_id) or {"error": "unknown task_id"}

    @mcp.tool(annotations=mutation)
    async def duck_cancel_task(task_id: str) -> dict:
        """Cancel the active task, fence delayed actions and request a walking stop."""
        return await engine.cancel(task_id)

    @mcp.tool(annotations=mutation)
    async def duck_stop_walk() -> dict:
        """Cancel harness autonomy and zero walking velocity. Does not torque-off or abort other robotd skills."""
        return await engine.cancel()

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield
        await engine.close()

    app = FastAPI(title="Microduck Harness", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    app.state.mcp = mcp

    @app.middleware("http")
    async def guard_access(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/mcp"):
            # No cross-origin browser writes to a local robot gateway.
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "cross-origin access is not enabled"}, status_code=403)
            if settings.gateway_token:
                supplied = request.headers.get("authorization", "")
                if not hmac.compare_digest(supplied, "Bearer "+settings.gateway_token):
                    return JSONResponse({"detail": "bearer token required"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ModelConfigError)
    async def model_config_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, ModelConfigConflict) else 422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default validation payload includes raw `input`, which could
        # echo an operator's submitted API key. Keep locations/messages only.
        return JSONResponse({"detail": [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                                        for error in exc.errors()]}, status_code=422)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(Path(__file__).parent/"static"/"index.html")

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.mode, "decision_mode": engine.settings.decision_mode,
                "version": __version__, "hardware_tested": False,
                "model_configuration": engine.model_configuration_summary()}

    @app.get("/api/model-config")
    async def model_configuration():
        return engine.model_configuration_status()

    @app.patch("/api/model-config")
    async def update_model_configuration(change: ModelConfigUpdate):
        if not settings.gateway_token:
            raise HTTPException(403, "set GATEWAY_TOKEN once before editing model settings through the operator API")
        return await engine.update_model_configuration(change)

    @app.get("/api/state")
    async def state():
        return await engine.status()

    @app.get("/api/observe")
    async def observe():
        try:
            return (await engine.observe()).model_dump()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/tasks", status_code=202)
    async def start(request: StartRequest):
        return await engine.start(request)

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        result = engine.task(task_id)
        if not result:
            raise HTTPException(404, "unknown task_id")
        return result

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return await engine.cancel(task_id)

    @app.post("/api/stop")
    async def stop():
        return await engine.cancel()

    @app.get("/api/events")
    async def events(after: int = 0):
        return {"events": engine.journal.events(after)}

    @app.get("/api/frame")
    async def frame():
        if settings.mode == "demo":
            raise HTTPException(404, "demo has synthetic observations, not a camera")
        client = engine.backend.client
        try:
            response = await client.get(settings.vision_url.rstrip("/")+"/image",
                                        headers={"Authorization": "Bearer "+settings.vision_token})
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise ValueError("vision response is not an image")
            return Response(response.content, media_type=response.headers["content-type"],
                            headers={"Cache-Control": "no-store"})
        except Exception:
            raise HTTPException(503, "no fresh camera image available") from None

    # Official SDK handles initialize, tools/list, tools/call, JSON-RPC and transport lifecycle.
    app.mount("/", mcp_app)
    return app

