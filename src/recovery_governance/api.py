"""灾后恢复治理服务的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import Forbidden, RecoveryError
from .service import RecoveryGovernanceService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到恢复治理领域服务，便于无网络单元测试。"""

    def __init__(self, service: RecoveryGovernanceService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise Forbidden("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _optional_actor(headers: Mapping[str, str]) -> str | None:
        actor = headers.get("x-actor-id", "").strip()
        return actor or None

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise RecoveryError("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        service = self.service
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "recovery-governance"})

            # 公众视图：无需身份，只暴露必要开放状态
            if method == "GET" and path == "/public/status":
                return Response(200, service.public_status())

            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                result = service.create_user(
                    self._actor(normalized_headers), payload["user_id"],
                    payload["display_name"], payload["role"],
                )
                return Response(201, result)

            if method == "POST" and path == "/areas":
                return Response(201, service.register_area(self._actor(normalized_headers), payload))

            if method == "POST" and path == "/evidence":
                return Response(201, service.submit_evidence(self._actor(normalized_headers), payload))

            if method == "POST" and len(parts) == 4 and parts[0] == "evidence" and parts[3] == "invalidate":
                result = service.invalidate_evidence(
                    self._actor(normalized_headers), parts[1], int(parts[2]), payload["reason"]
                )
                return Response(200, result)

            if method == "POST" and path == "/plans":
                return Response(201, service.create_plan(self._actor(normalized_headers), payload))

            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "issue":
                result = service.issue_plan(
                    self._actor(normalized_headers), parts[1], int(payload["expected_revision"])
                )
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "plans" and parts[2] == "revoke":
                result = service.revoke_plan(
                    self._actor(normalized_headers), parts[1], payload["reason"]
                )
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "stages" and parts[2] == "reviews":
                result = service.submit_review(
                    self._actor(normalized_headers), parts[1], payload["evidence_id"],
                    int(payload["evidence_version"]), payload["confirmed_items"],
                    payload.get("note", ""),
                )
                return Response(201, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "stages" and parts[2] == "activate":
                key = normalized_headers.get("idempotency-key", "").strip()
                result = service.activate_stage(
                    self._actor(normalized_headers), parts[1], key, payload.get("note", "")
                )
                return Response(200, result)

            if method == "POST" and path == "/sweep":
                return Response(200, service.sweep(self._optional_actor(normalized_headers)))

            if method == "POST" and len(parts) == 3 and parts[0] == "activations" and parts[2] == "revoke":
                result = service.revoke_activation(
                    self._actor(normalized_headers), parts[1], payload["reason"]
                )
                return Response(200, result)

            if method == "POST" and len(parts) == 3 and parts[0] == "activations" and parts[2] == "executions":
                result = service.record_execution(
                    self._actor(normalized_headers), parts[1], payload["channel"], payload["detail"]
                )
                return Response(201, result)

            if method == "GET" and len(parts) == 3 and parts[0] == "areas" and parts[2] == "projection":
                result = service.projection(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "GET" and len(parts) == 3 and parts[0] == "channels" and parts[2] == "projection":
                result = service.channel_projection(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "GET" and len(parts) == 3 and parts[0] == "activations" and parts[2] == "record":
                result = service.decision_record(self._actor(normalized_headers), parts[1])
                return Response(200, result)

            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(self._actor(normalized_headers)))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except RecoveryError as exc:
            status = getattr(exc, "status", 400)
            code = getattr(exc, "code", "recovery_error")
            return Response(status, {"error": {"code": code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RecoveryGovernance/1"

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
    parser = argparse.ArgumentParser(description="启动灾后分阶段恢复治理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("recovery_governance.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    service = RecoveryGovernanceService(connection)
    service.bootstrap()
    application = JsonApplication(service)
    server = HTTPServer((args.host, args.port), make_handler(application))
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
