"""跨时区生效：例外期限以 UTC 比较，任意偏移表示法等价。"""
from service_09252_002.ports import parse_instant
from service_09252_002.services import ValidationError
from testkit import ServiceTestCase, unit_payload


class CrossTimezoneTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.actors = self.make_standard_actors()
        # 规则结论为不可互认的一对单元（等级 3 vs 5），便于观察例外翻转
        self.cn_unit, self.de_unit = self.make_pair(
            self.actors,
            cn_unit=unit_payload("CN-TZ", level=3),
            de_unit=unit_payload("DE-TZ", level=5),
        )
        self.mapping_id = self.make_mapping(
            self.actors, self.cn_unit, self.de_unit)["id"]
        view = self.service.get_mapping_view(self.actors["cn"], self.mapping_id)
        assert view["mutual_outcome"] == "none"

    def _approved_exception(self, valid_from, valid_until, key_prefix="tz"):
        exc = self.service.propose_exception(self.actors["expert"], {
            "mapping_id": self.mapping_id,
            "direction": "BOTH",
            "effect": "full",
            "reason": "跨境试点互认",
            "valid_from": valid_from,
            "valid_until": valid_until,
        })
        self.approve_all(self.actors, exc["id"], key_prefix=key_prefix)
        return exc

    def _current_outcome(self):
        return self.service.get_mapping_view(
            self.actors["cn"], self.mapping_id)["mutual_outcome"]

    def test_positive_offset_window(self):
        # 北京时间 2026-10-01 00:00 生效 = UTC 2026-09-30 16:00
        exc = self._approved_exception("2026-10-01T00:00:00+08:00",
                                       "2026-12-01T00:00:00+08:00")
        self.assertEqual(exc["valid_from"], "2026-09-30T16:00:00+00:00")
        self.assertEqual(exc["valid_until"], "2026-11-30T16:00:00+00:00")

        self.clock.set(parse_instant("2026-09-30T15:59:59Z"))
        self.assertEqual(self._current_outcome(), "none")
        view = self.service.get_exception(self.actors["cn"], exc["id"])
        self.assertEqual(view["effective_status"], "approved_pending")

        self.clock.set(parse_instant("2026-09-30T16:00:00Z"))
        self.assertEqual(self._current_outcome(), "full")
        view = self.service.get_exception(self.actors["cn"], exc["id"])
        self.assertEqual(view["effective_status"], "effective")

        # 终点为开区间：北京时间 2026-12-01 00:00 起不再生效
        self.clock.set(parse_instant("2026-11-30T15:59:59Z"))
        self.assertEqual(self._current_outcome(), "full")
        self.clock.set(parse_instant("2026-11-30T16:00:00Z"))
        self.assertEqual(self._current_outcome(), "none")
        view = self.service.get_exception(self.actors["cn"], exc["id"])
        self.assertEqual(view["effective_status"], "expired")

    def test_negative_offset_window(self):
        # 美东时间 2026-10-01 09:00 -05:00 = UTC 14:00
        self._approved_exception("2026-10-01T09:00:00-05:00",
                                 "2026-10-02T09:00:00-05:00", key_prefix="neg")
        self.clock.set(parse_instant("2026-10-01T13:59:59Z"))
        self.assertEqual(self._current_outcome(), "none")
        self.clock.set(parse_instant("2026-10-01T14:00:00Z"))
        self.assertEqual(self._current_outcome(), "full")
        self.clock.set(parse_instant("2026-10-02T14:00:00Z"))
        self.assertEqual(self._current_outcome(), "none")

    def test_equivalent_offsets_are_same_instant(self):
        # 同一瞬时的两种表示：+08:00 与 Z 等价
        exc = self._approved_exception("2026-10-01T08:00:00+08:00",
                                       "2026-10-03T00:00:00Z", key_prefix="eq")
        self.assertEqual(exc["valid_from"], "2026-10-01T00:00:00+00:00")
        self.clock.set(parse_instant("2026-10-01T00:00:00Z"))
        self.assertEqual(self._current_outcome(), "full")

    def test_naive_time_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.propose_exception(self.actors["expert"], {
                "mapping_id": self.mapping_id,
                "direction": "BOTH",
                "effect": "full",
                "reason": "x",
                "valid_from": "2026-10-01T00:00:00",  # 无时区偏移
                "valid_until": "2026-12-01T00:00:00Z",
            })

    def test_garbage_time_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.propose_exception(self.actors["expert"], {
                "mapping_id": self.mapping_id,
                "direction": "BOTH",
                "effect": "full",
                "reason": "x",
                "valid_from": "not-a-time",
                "valid_until": "2026-12-01T00:00:00Z",
            })


if __name__ == "__main__":
    import unittest
    unittest.main()
