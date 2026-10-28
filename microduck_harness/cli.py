from __future__ import annotations
import argparse
import os
import uvicorn

from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="Microduck nanoMuse / AgentJev / GLM harness")
    parser.add_argument("service", choices=["gateway", "executor", "vision"], nargs="?", default="gateway")
    parser.add_argument("--config", help="Service JSON configuration file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if args.service == "gateway":
        from .gateway import create_app
        settings = Settings.load(args.config)
        if not loopback and not settings.gateway_token:
            parser.error("non-loopback gateway requires GATEWAY_TOKEN")
        app = create_app(settings)
        port = args.port or 8765
    elif args.service == "executor":
        from .executor import create_executor_app, ExecutorSettings
        from dataclasses import asdict
        import json
        values = asdict(ExecutorSettings.from_env())
        if args.config:
            with open(args.config) as source:
                values.update(json.load(source))
        values["bind_host"] = args.host
        app = create_executor_app(values)
        port = args.port or 8766
    else:
        from .vision import create_vision_app
        if args.config:
            os.environ["VISION_CONFIG"] = args.config
        if not os.environ.get("VISION_TOKEN"):
            parser.error("vision service requires VISION_TOKEN")
        app = create_vision_app()
        port = args.port or 8767
    # One process owns the robot task lifecycle; never use multiple workers.
    uvicorn.run(app, host=args.host, port=port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
