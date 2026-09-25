"""接口边界：基于标准库 http.server 的 JSON REST 适配器。

仅负责协议解析、认证与错误映射；业务规则全部在应用服务中。
认证方式：除 GET /health 外，所有请求需携带 `Authorization: Bearer <令牌>`。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .services import (
    ConflictError,
    ForbiddenError,
    MutualRecognitionService,
    NotFoundError,
    ServiceError,
    UnauthorizedError,
    ValidationError,
)

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}


def _create_actor(service, actor, body, query):
    view = service.create_actor(
        actor, name=body.get("name"), token=body.get("token"),
        roles=body.get("roles") or [], parties=body.get("parties") or [],
    )
    return 201, view


def _import_standard(service, actor, body, query):
    view, created = service.import_standard(actor, body)
    return (201 if created["version"] or created["standard"] else 200), view


def _get_standard(service, actor, body, query, standard_id):
    return 200, service.get_standard_view(actor, standard_id)


def _get_version(service, actor, body, query, version_id):
    return 200, service.get_version_view(actor, version_id)


def _publish_version(service, actor, body, query, version_id):
    return 200, service.publish_version(actor, version_id)


def _retire_version(service, actor, body, query, version_id):
    return 200, service.retire_version(actor, version_id)


def _affected_mappings(service, actor, body, query, version_id):
    return 200, service.list_affected_mappings(actor, version_id)


def _compare(service, actor, body, query):
    return 200, service.compare_units(
        actor, _require(body, "source_unit_id"), _require(body, "target_unit_id"))


def _create_mapping(service, actor, body, query):
    view, created = service.create_mapping(
        actor, _require(body, "unit_x_id"), _require(body, "unit_y_id"))
    return (201 if created else 200), view


def _get_mapping(service, actor, body, query, mapping_id):
    return 200, service.get_mapping_view(actor, mapping_id)


def _recompute_mapping(service, actor, body, query, mapping_id):
    return 200, service.recompute_mapping(actor, mapping_id)


def _revoke_mapping(service, actor, body, query, mapping_id):
    return 200, service.revoke_mapping(actor, mapping_id, reason=body.get("reason", ""))


def _trace_mapping(service, actor, body, query, mapping_id):
    return 200, service.trace_mapping(actor, mapping_id)


def _recognition_path(service, actor, body, query, unit_id):
    target = (query.get("to") or [None])[0]
    if not target:
        raise ValidationError("缺少查询参数 to")
    return 200, service.recognition_path(actor, unit_id, target)


def _propose_exception(service, actor, body, query):
    return 201, service.propose_exception(actor, body)


def _get_exception(service, actor, body, query, exception_id):
    return 200, service.get_exception(actor, exception_id)


def _approve_exception(service, actor, body, query, exception_id):
    view, created = service.approve_exception(
        actor, exception_id,
        party=body.get("party"), decision=body.get("decision"),
        idempotency_key=body.get("idempotency_key"),
    )
    return (201 if created else 200), view


def _revoke_exception(service, actor, body, query, exception_id):
    return 200, service.revoke_exception(actor, exception_id,
                                         reason=body.get("reason", ""))


def _require(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"缺少字段：{field}")
    return value


ROUTES = [
    ("POST", re.compile(r"^/actors$"), _create_actor),
    ("POST", re.compile(r"^/standards/import$"), _import_standard),
    ("GET", re.compile(r"^/standards/(?P<standard_id>[^/]+)$"), _get_standard),
    ("GET", re.compile(r"^/versions/(?P<version_id>[^/]+)$"), _get_version),
    ("POST", re.compile(r"^/versions/(?P<version_id>[^/]+)/publish$"), _publish_version),
    ("POST", re.compile(r"^/versions/(?P<version_id>[^/]+)/retire$"), _retire_version),
    ("GET", re.compile(r"^/versions/(?P<version_id>[^/]+)/affected-mappings$"), _affected_mappings),
    ("POST", re.compile(r"^/compare$"), _compare),
    ("POST", re.compile(r"^/mappings$"), _create_mapping),
    ("GET", re.compile(r"^/mappings/(?P<mapping_id>[^/]+)$"), _get_mapping),
    ("POST", re.compile(r"^/mappings/(?P<mapping_id>[^/]+)/recompute$"), _recompute_mapping),
    ("POST", re.compile(r"^/mappings/(?P<mapping_id>[^/]+)/revoke$"), _revoke_mapping),
    ("GET", re.compile(r"^/mappings/(?P<mapping_id>[^/]+)/trace$"), _trace_mapping),
    ("GET", re.compile(r"^/units/(?P<unit_id>[^/]+)/recognition-path$"), _recognition_path),
    ("POST", re.compile(r"^/exceptions$"), _propose_exception),
    ("GET", re.compile(r"^/exceptions/(?P<exception_id>[^/]+)$"), _get_exception),
    ("POST", re.compile(r"^/exceptions/(?P<exception_id>[^/]+)/approvals$"), _approve_exception),
    ("POST", re.compile(r"^/exceptions/(?P<exception_id>[^/]+)/revoke$"), _revoke_exception),
]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: MutualRecognitionService = None  # 由 make_handler 注入

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, *args):  # 保持静默，测试与运行日志由调用方负责
        pass

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
        except ServiceError as exc:
            status = _ERROR_STATUS.get(exc.code, 500)
            payload = {"error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - 接口边界兜底
            status = 500
            payload = {"error": {"code": "internal_error", "message": str(exc)}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str):
        parsed = urlsplit(self.path)
        path = parsed.path
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        actor = self._authenticate()
        query = parse_qs(parsed.query)
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                body = self._read_json() if method == "POST" else {}
                return handler(self.service, actor, body, query, **match.groupdict())
        raise NotFoundError(f"路径不存在：{method} {path}")

    def _authenticate(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise UnauthorizedError("缺少 Authorization: Bearer 头")
        return self.service.authenticate(header[len("Bearer "):].strip())

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须为 JSON 对象")
        return data


def make_handler(service: MutualRecognitionService):
    class Handler(_Handler):
        pass

    Handler.service = service
    return Handler


def create_server(service: MutualRecognitionService, host: str, port: int
                  ) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.daemon_threads = True
    return server
