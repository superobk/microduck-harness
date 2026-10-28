"""Run the pinned calibration UI with the reviewed two-tag camera extension.

The upstream snapshot is immutable. This adapter changes only the camera target
allowlist, honest displayed deployment instructions, and the default port. All
native verification, CSRF, explicit save/apply and backup behavior is retained.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from .native import TARGETS, UPSTREAM, verify_upstream


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_components():
    """Load exact upstream editors; no config is read or mutated by loading."""
    verify_upstream()
    calibration = load_module("duck_calibrate", UPSTREAM / "calibration/common/duck_calibrate.py")
    camera = load_module("camera_config", UPSTREAM / "calibration/integration-0.16.3/web/camera_config.py")
    return calibration, camera


class TextTemplate:
    def __init__(self, text: str):
        self.text = text

    def read_text(self, encoding="utf-8"):
        return self.text


def load_web(target: str, port: int = 8768):
    if target not in TARGETS:
        raise ValueError("camera integration requires a reviewed native source tag")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("web port must be 1..65535")
    calibration, camera = load_components()
    original_camera = camera.Camera

    class Camera(original_camera):
        """The same native receipt contract now exists in both generated patches."""

        def status(self):
            result = super().status()
            # Verification already checks the actual runtime tag/PID/config; we
            # extend its feature allowlist only after providing that native port.
            writable = (result["runtime_verified"] and not result["override"]
                        and result["saved_error"] is None and self.target in TARGETS)
            result["can_save"] = writable
            result["can_apply"] = writable and self.enable_apply and not self.demo
            result["integration"] = "microduck-harness two-tag camera source adapter"
            result["validation_level"] = "source-application and synthetic software tests; hardware validation required"
            return result

        def preview(self, body):
            if not isinstance(body, dict) or "rotation" not in body:
                raise calibration.CalibrationError("missing camera rotation")
            if self.target not in TARGETS:
                raise calibration.CalibrationError("unsupported camera integration tag")
            value = camera.angle(body["rotation"])
            text = self.config.read_text("utf-8")
            updated = camera.edit(text, value)
            snippet = "# follows board camera default" if value is None else f"[media]\ncamera_rotate = {value}\n"
            return {"ok": True, "rotation": value, "snippet": snippet, "config": updated}

    camera.Camera = Camera
    web_path = UPSTREAM / "calibration/integration-0.16.3/web/calibration_web.py"
    web = load_module("microduck_tools_integrated_web", web_path)
    html = web_path.with_name("index.html").read_text(encoding="utf-8")
    version = target.removeprefix("daemon-v")
    html = html.replace("0.16.3 扩展 · 顺时针校正", version+" 适配 · 顺时针校正")
    html = html.replace("sudo python3 web/calibration_web.py --target daemon-v0.16.3",
                        f"sudo python3 scripts/tools_web.py --target {target} --port {port}")
    html = html.replace("8765", str(port))
    html = html.replace("先在 macOS/Linux 开发机用对应版本的补丁构建并运行 cargo test，采用官方签名更新方式部署。",
                        "先在支持的构建环境应用对应版本的源码补丁、运行原生测试并构建，按统一部署指南安装；本工作台不会构建或安装。")
    html = html.replace("<small>校准中心 / v2</small>",
                        "<small>校准中心 / v2 · Harness 适配</small>")
    html = html.replace("0.16.3 统一扩展（含摄像头）",
                        version+" 源码适配（含摄像头，原生构建与实机待验证）")
    html = html.replace("官方机器人软件兼容</div>",
                        "源码补丁及软件测试已核对；实机待验证</div>")
    web.PREVIEW_TEMPLATE = TextTemplate(html)
    return web


def main(argv: list[str] | None = None):
    # Use the original parser so every existing flag has exactly its upstream
    # meaning. Target choice is parsed once before initializing live telemetry.
    web = load_web("daemon-v0.16.3")
    parser = web.parser()
    parser.description = "Pinned microduck-tools calibration/camera configuration; localhost, source-adapted for two tags."
    parser.set_defaults(port=8768)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.camera_web_port <= 65535:
        parser.error("web ports must be 1..65535")
    if args.listen not in ("127.0.0.1", "::1", "localhost"):
        parser.error("calibration UI is local-only; use localhost and SSH forwarding")
    web = load_web(args.target, args.port)
    context = web.Context(args)
    server = web.ThreadingHTTPServer((args.listen, args.port), web.new_handler(context))
    print(f"Microduck configuration: http://127.0.0.1:{args.port}/ ({args.target})", flush=True)
    print("Native source adapter: saved configuration is distinct from verified runtime. No robotd restart or motor write.", flush=True)
    print("Camera apply is disabled unless --enable-camera-apply was explicitly selected.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        context.monitor.close()
        server.server_close()
    return 0
