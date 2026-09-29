"""无第三方依赖的灾后分阶段恢复编排 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import RecoveryError, ValidationFailed
from .service import RecoveryService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到恢复编排服务，便于无网络单元测试。"""

    def __init__(self, service: RecoveryService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            if method == "GET" and path == "/public/status":
                return Response(200, self.service.public_status())
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]),
                )
            if method == "POST" and path == "/plans":
                return Response(201, self.service.create_plan(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "activate":
                return Response(200, self.service.activate_plan(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "retire":
                return Response(200, self.service.retire_plan(actor, parts[1], payload["reason"]))
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "plans"
                and parts[2] == "phases"
                and parts[4] == "reviews"
            ):
                return Response(
                    201,
                    self.service.review_phase(
                        actor, parts[1], parts[3], payload["decision"], payload.get("note", "")
                    ),
                )
            if (
                method == "POST"
                and len(parts) == 5
                and parts[0] == "plans"
                and parts[2] == "phases"
                and parts[4] == "confirmations"
            ):
                return Response(
                    201,
                    self.service.confirm_pass_item(
                        actor, parts[1], parts[3], payload["item_key"], payload.get("note", "")
                    ),
                )
            if method == "POST" and path == "/evidence":
                return Response(201, self.service.submit_evidence(actor, payload))
            if (
                method == "POST"
                and len(parts) == 3
                and parts[0] == "evidence"
                and parts[2] == "invalidate"
            ):
                return Response(200, self.service.invalidate_evidence(actor, parts[1], payload["reason"]))
            if method == "POST" and path == "/directives":
                return Response(201, self.service.issue_directive(actor, payload))
            if method == "POST" and parts == ["directives", "expire"]:
                return Response(200, self.service.expire_directives(actor))
            if (
                method == "POST"
                and len(parts) == 3
                and parts[0] == "directives"
                and parts[2] == "withdraw"
            ):
                return Response(
                    200, self.service.withdraw_directive(actor, int(parts[1]), payload["reason"])
                )
            if (
                method == "GET"
                and len(parts) == 3
                and parts[0] == "directives"
                and parts[2] == "basis"
            ):
                return Response(200, self.service.decision_basis(actor, int(parts[1])))
            if method == "GET" and len(parts) == 3 and parts[0] == "zones" and parts[2] == "projection":
                return Response(200, self.service.zone_projection(actor, parts[1]))
            if (
                method == "GET"
                and len(parts) == 4
                and parts[0] == "zones"
                and parts[2] == "interfaces"
            ):
                return Response(200, self.service.interface_status(actor, parts[1], parts[3]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RecoveryError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RecoveryOps/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动灾后分阶段恢复编排 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("recovery_ops.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(RecoveryService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
