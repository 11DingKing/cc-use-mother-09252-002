"""Flask 接口边界：认证头解析、幂等键透传、错误翻译。

认证约定（内部服务，网关已鉴别身份）：
- ``X-Actor``: 操作者标识（必填）
- ``X-Roles``: 逗号分隔的角色（importer/mapper/expert/approver/admin）
- ``Idempotency-Key``: 写操作可选幂等键
"""
from __future__ import annotations

import os

from flask import Flask, g, jsonify, request

from .clock import Clock
from .domain import Actor, Role
from .errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError, ValidationError
from .services import RecognitionService
from .storage import Database

ERROR_STATUS = {
    ValidationError: 400,
    PermissionDeniedError: 403,
    NotFoundError: 404,
    ConflictError: 409,
}


def _current_actor() -> Actor:
    name = request.headers.get("X-Actor", "").strip()
    if not name:
        raise PermissionDeniedError("缺少 X-Actor 请求头")
    roles = frozenset(
        r.strip() for r in request.headers.get("X-Roles", "").split(",") if r.strip()
    )
    unknown = roles - Role.ALL
    if unknown:
        raise ValidationError(f"未知角色: {sorted(unknown)}")
    return Actor(name=name, roles=roles)


def _idem_key() -> str | None:
    key = request.headers.get("Idempotency-Key", "").strip()
    return key or None


def create_app(db_path: str | None = None, clock: Clock | None = None,
               service: RecognitionService | None = None) -> Flask:
    """应用工厂：测试可注入临时库路径与固定时钟。"""
    app = Flask(__name__)
    if service is None:
        path = db_path or os.environ.get("RECOGNITION_DB", "recognition.db")
        service = RecognitionService(Database(path), clock)
    app.extensions["recognition_service"] = service

    @app.before_request
    def _bind_actor() -> None:
        g.actor = _current_actor()

    @app.errorhandler(DomainError)
    def _domain_error(exc: DomainError):
        status = next((code for klass, code in ERROR_STATUS.items()
                       if isinstance(exc, klass)), 500)
        return jsonify({"error": {"type": type(exc).__name__, "message": str(exc)}}), status

    @app.errorhandler(404)
    def _not_found(_):
        return jsonify({"error": {"type": "NotFoundError", "message": "资源不存在"}}), 404

    def svc() -> RecognitionService:
        return app.extensions["recognition_service"]

    # ---------------- 导入与版本 ----------------
    @app.post("/standards/import")
    def import_standard():
        result = svc().import_standard(g.actor, request.get_json(force=True), _idem_key())
        return jsonify(result), 201

    @app.post("/standards/<code>/versions")
    def publish_version(code: str):
        result = svc().publish_version(g.actor, code, request.get_json(force=True), _idem_key())
        return jsonify(result), 201

    @app.get("/versions/<version_id>/impact")
    def version_impact(version_id: str):
        return jsonify(svc().version_impact(g.actor, version_id))

    @app.get("/standards/<code>/versions")
    def list_versions(code: str):
        return jsonify(svc().list_versions(g.actor, code))

    @app.get("/versions/<version_id>/units")
    def list_units(version_id: str):
        return jsonify(svc().list_units(g.actor, version_id))

    # ---------------- 映射 ----------------
    @app.post("/mappings")
    def create_mapping():
        result = svc().create_mapping(g.actor, request.get_json(force=True), _idem_key())
        return jsonify(result), 201

    @app.post("/mappings/<mapping_id>/approve")
    def approve_mapping(mapping_id: str):
        body = request.get_json(force=True, silent=True) or {}
        result = svc().approve(g.actor, "mapping", mapping_id,
                               str(body.get("party", "")), _idem_key())
        return jsonify(result)

    @app.post("/mappings/<mapping_id>/revoke")
    def revoke_mapping(mapping_id: str):
        body = request.get_json(force=True, silent=True) or {}
        result = svc().revoke(g.actor, "mapping", mapping_id,
                              str(body.get("reason", "")), _idem_key())
        return jsonify(result)

    @app.get("/mappings/<mapping_id>/trace")
    def trace_mapping(mapping_id: str):
        return jsonify(svc().trace_mapping(g.actor, mapping_id))

    # ---------------- 比对 ----------------
    @app.post("/comparisons")
    def compare():
        body = request.get_json(force=True) or {}
        result = svc().compare_versions(
            g.actor, str(body.get("version_a_id", "")), str(body.get("version_b_id", "")),
            _idem_key(),
        )
        return jsonify(result), 201

    # ---------------- 例外 ----------------
    @app.post("/exceptions")
    def propose_exception():
        result = svc().propose_exception(g.actor, request.get_json(force=True), _idem_key())
        return jsonify(result), 201

    @app.post("/exceptions/<exception_id>/approve")
    def approve_exception(exception_id: str):
        body = request.get_json(force=True, silent=True) or {}
        result = svc().approve(g.actor, "exception", exception_id,
                               str(body.get("party", "")), _idem_key())
        return jsonify(result)

    @app.post("/exceptions/<exception_id>/revoke")
    def revoke_exception(exception_id: str):
        body = request.get_json(force=True, silent=True) or {}
        result = svc().revoke(g.actor, "exception", exception_id,
                              str(body.get("reason", "")), _idem_key())
        return jsonify(result)

    # ---------------- 追溯 ----------------
    @app.get("/units/<unit_id>/equivalence-chain")
    def equivalence_chain(unit_id: str):
        return jsonify(svc().equivalence_chain(g.actor, unit_id))

    return app
