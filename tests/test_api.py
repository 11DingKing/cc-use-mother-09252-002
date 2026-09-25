"""端到端 API 测试：真实 HTTP 服务 + http.client 客户端。"""
import http.client
import json
import threading
import unittest

from service_09252_002.api import create_server
from testkit import ADMIN_TOKEN, ServiceTestCase, import_payload, unit_payload


class ApiTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, token=ADMIN_TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, payload

    def test_health_no_auth(self):
        status, payload = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_unauthorized_without_token(self):
        status, payload = self.request("GET", "/mappings/whatever", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_unknown_path_404(self):
        status, payload = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_full_flow_over_http(self):
        # 创建参与者
        for name, roles, parties in (
                ("api-cn", ["authority"], ["authority:CN"]),
                ("api-de", ["authority"], ["authority:DE"]),
                ("api-reg", ["registry"], ["registry"]),
                ("api-exp", ["expert"], [])):
            status, actor = self.request("POST", "/actors", {
                "name": name, "token": f"token-{name}-0123456789",
                "roles": roles, "parties": parties})
            self.assertEqual(status, 201, actor)
        cn_tok = "token-api-cn-0123456789"
        de_tok = "token-api-de-0123456789"
        reg_tok = "token-api-reg-0123456789"
        exp_tok = "token-api-exp-0123456789"

        # 导入（201），重放同内容（200 且同 id），冲突内容（409）
        cn_payload = import_payload("CN-API", "CN", "1.0", [unit_payload("CN-API-U")])
        status, cn_view = self.request("POST", "/standards/import", cn_payload, cn_tok)
        self.assertEqual(status, 201, cn_view)
        status, replay = self.request("POST", "/standards/import", cn_payload, cn_tok)
        self.assertEqual(status, 200)
        self.assertEqual(replay["version"]["id"], cn_view["version"]["id"])
        changed = import_payload("CN-API", "CN", "1.0",
                                 [unit_payload("CN-API-U", hours=200)])
        status, conflict = self.request("POST", "/standards/import", changed, cn_tok)
        self.assertEqual(status, 409)

        de_payload = import_payload("DE-API", "DE", "1.0", [unit_payload("DE-API-U")])
        status, de_view = self.request("POST", "/standards/import", de_payload, de_tok)
        self.assertEqual(status, 201, de_view)

        # 越权：德国主管方不能导入中国标准
        status, forbidden = self.request("POST", "/standards/import", cn_payload, de_tok)
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["code"], "forbidden")

        # 发布
        cn_version = cn_view["version"]["id"]
        status, published = self.request(
            "POST", f"/versions/{cn_version}/publish", {}, cn_tok)
        self.assertEqual(status, 200, published)
        self.assertEqual(published["affected_mapping_ids"], [])
        de_version = de_view["version"]["id"]
        self.request("POST", f"/versions/{de_version}/publish", {}, de_tok)

        cn_unit = cn_view["units"][0]["id"]
        de_unit = de_view["units"][0]["id"]

        # 即席比对
        status, comparison = self.request("POST", "/compare", {
            "source_unit_id": cn_unit, "target_unit_id": de_unit}, cn_tok)
        self.assertEqual(status, 200)
        self.assertEqual(comparison["mutual_outcome"], "full")

        # 建映射（201），重复建（200 同 id）
        status, mapping = self.request("POST", "/mappings", {
            "unit_x_id": cn_unit, "unit_y_id": de_unit}, cn_tok)
        self.assertEqual(status, 201, mapping)
        mapping_id = mapping["id"]
        status, again = self.request("POST", "/mappings", {
            "unit_x_id": de_unit, "unit_y_id": cn_unit}, de_tok)
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], mapping_id)

        # 例外：专家提案
        status, exc = self.request("POST", "/exceptions", {
            "mapping_id": mapping_id, "direction": "BOTH", "effect": "conditional",
            "conditions": ["年度复核"], "reason": "试点",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z"}, exp_tok)
        self.assertEqual(status, 201, exc)
        exc_id = exc["id"]

        # 会签：缺幂等键 400；三方会签后生效；重放 200
        status, err = self.request("POST", f"/exceptions/{exc_id}/approvals", {
            "party": "authority:CN", "decision": "approve"}, cn_tok)
        self.assertEqual(status, 400)
        for token, party in ((cn_tok, "authority:CN"), (de_tok, "authority:DE"),
                             (reg_tok, "registry")):
            status, approval = self.request("POST", f"/exceptions/{exc_id}/approvals", {
                "party": party, "decision": "approve",
                "idempotency_key": f"api-{party}"}, token)
            self.assertEqual(status, 201, approval)
        status, replay = self.request("POST", f"/exceptions/{exc_id}/approvals", {
            "party": "authority:CN", "decision": "approve",
            "idempotency_key": "api-authority:CN"}, cn_tok)
        self.assertEqual(status, 200)

        status, exc_view = self.request("GET", f"/exceptions/{exc_id}")
        self.assertEqual(exc_view["effective_status"], "effective")

        # 映射视图反映例外结论
        status, mapping_view = self.request("GET", f"/mappings/{mapping_id}")
        self.assertEqual(mapping_view["mutual_outcome"], "conditional")
        self.assertTrue(mapping_view["directions"]["A_TO_B"]["source"]
                        .startswith("exception:"))

        # 追溯
        status, trace = self.request("GET", f"/mappings/{mapping_id}/trace")
        self.assertEqual(status, 200)
        self.assertEqual(len(trace["decisions"]), 2)
        self.assertEqual(len(trace["exceptions"]), 1)
        self.assertTrue(trace["events"])

        # 撤销例外后回落到规则结论
        status, revoked = self.request(
            "POST", f"/exceptions/{exc_id}/revoke", {"reason": "结束"}, reg_tok)
        self.assertEqual(status, 200)
        self.assertEqual(revoked["status"], "revoked")
        status, mapping_view = self.request("GET", f"/mappings/{mapping_id}")
        self.assertEqual(mapping_view["mutual_outcome"], "full")
        self.assertTrue(mapping_view["directions"]["A_TO_B"]["source"]
                        .startswith("rules:"))

        # 新版本发布 → 旧映射受影响 → 受影响列表可查
        cn_v2 = import_payload("CN-API", "CN", "2.0",
                               [unit_payload("CN-API-U2")], parent=cn_version)
        status, v2_view = self.request("POST", "/standards/import", cn_v2, cn_tok)
        status, published = self.request(
            "POST", f"/versions/{v2_view['version']['id']}/publish", {}, cn_tok)
        self.assertIn(mapping_id, published["affected_mapping_ids"])
        status, affected = self.request(
            "GET", f"/versions/{cn_version}/affected-mappings")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in affected["affected"]], [mapping_id])

    def test_validation_error_shape(self):
        status, payload = self.request("POST", "/mappings", {"unit_x_id": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_not_found_shape(self):
        status, payload = self.request("GET", "/mappings/map_nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
