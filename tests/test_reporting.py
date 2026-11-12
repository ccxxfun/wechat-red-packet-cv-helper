"""Standard-library tests for the isolated daily reporting module."""

from datetime import date
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from reporting import (DailyReporter, ReportingConfig, clean_message,
                       parse_amount, run_daily_notification)

URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=not-a-real-secret"


class FakeResponse:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps(self.result).encode("utf-8")


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ReportingConfig(default_messages=("恭喜发财", "祝你好运"),
                                      removal_phrases=("红包封面", "已领取"),
                                      header="慈善日报", footer="感谢支持")
        self.reporter = DailyReporter(self.root / "state.sqlite3", self.root / "records",
                                      self.config)
        self.day = date(2026, 11, 12)

    def test_amount_examples_and_ambiguity(self):
        self.assertEqual(parse_amount("领取 ¥1.20"), Decimal("1.20"))
        self.assertEqual(parse_amount("1元"), Decimal("1.00"))
        self.assertEqual(parse_amount("0.50元"), Decimal("0.50"))
        self.assertEqual(parse_amount("￥1,234.56"), Decimal("1234.56"))
        for value in ("1.234元", "1,23元", "十元", "1元 2元", "¥-1", "1.5", "¥1000000000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_amount(value)

    def test_clean_multiple_default_and_literals(self):
        self.assertEqual(clean_message("红包封面 祝你好运 已领取", self.config), "")
        self.assertEqual(clean_message("已领取 谢谢红包封面", self.config), "谢谢")
        self.assertEqual(clean_message("恭喜发财", self.config), "")

    def test_record_dedup_restarts_and_totals(self):
        self.assertTrue(self.reporter.record_packet(" 张 三 ", "¥1.20", "已领取 恭喜发财",
                                                     event_id="packet-1", occurred_at=self.day))
        self.assertFalse(self.reporter.record_packet("张 三", "1.20元", "",
                                                      event_id="packet-1", occurred_at=self.day))
        reopened = DailyReporter(self.root / "state.sqlite3", self.root / "records", self.config)
        self.assertTrue(reopened.record_packet("张 三", "0.50元", "谢谢",
                                                event_id="packet-2", occurred_at=self.day))
        self.assertTrue(reopened.record_packet("李四", "1元", "祝你好运",
                                                occurred_at=self.day))
        self.assertFalse(reopened.record_packet("李四", "1.00元", "",
                                                 occurred_at=self.day))
        content = reopened.render_day(self.day)
        self.assertTrue(content.startswith("慈善日报\n2026-11-12 红包记录\n"))
        self.assertIn("张 三：1.70元", content)
        self.assertIn("李四：1.00元", content)
        self.assertIn("今日合计功德善款2.70元", content)
        self.assertTrue(content.endswith("感谢支持\n"))
        path = self.root / "records" / "2026-11-12.txt"
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        path.unlink()
        self.assertFalse(reopened.record_packet("张 三", "¥1.20", event_id="packet-1",
                                                 occurred_at=self.day))
        self.assertTrue(path.exists())
        self.assertEqual(reopened.render_day("2026-11-13").count("今日合计功德善款0.00元"), 1)

    def test_stable_id_preserves_identical_distinct_packets(self):
        for id_ in ("a", "b"):
            self.assertTrue(self.reporter.record_packet("同一人", "1元", event_id=id_,
                                                        occurred_at=self.day))
        self.assertIn("今日合计功德善款2.00元", self.reporter.render_day(self.day))

    def test_reject_host_and_insecure_scheme_before_open(self):
        for url in ("http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
                    "https://qyapi.weixin.qq.com.evil.test/cgi-bin/webhook/send?key=secret",
                    "https://qyapi.weixin.qq.com@evil.test/cgi-bin/webhook/send?key=secret",
                    "https://qyapi.weixin.qq.com:444/cgi-bin/webhook/send?key=secret",
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret&x=1"):
            with self.subTest(url=url), patch("urllib.request.build_opener") as opened:
                with self.assertRaises(ValueError):
                    self.reporter.notify_day(self.day, url)
                opened.assert_not_called()

    @patch("urllib.request.build_opener")
    def test_notify_once_and_failure_retry(self, make_opener):
        opener = make_opener.return_value
        opener.open.side_effect = [FakeResponse({"errcode": 1, "errmsg": "secret"}),
                                   FakeResponse({"errcode": 0})]
        self.reporter.record_packet("张三", "¥0.50", occurred_at=self.day)
        with self.assertRaisesRegex(RuntimeError, "rejected") as error:
            self.reporter.notify_day(self.day, URL, timeout=3)
        self.assertNotIn("not-a-real-secret", str(error.exception))
        self.assertTrue(self.reporter.notify_day(self.day, URL, timeout=3))
        self.assertFalse(self.reporter.notify_day(self.day, URL, timeout=3))
        self.assertEqual(opener.open.call_count, 2)
        request, = opener.open.call_args.args
        payload = json.loads(request.data)
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("今日合计功德善款0.50元", payload["text"]["content"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        self.assertFalse(DailyReporter(self.root / "state.sqlite3", self.root / "records")
                         .notify_day(self.day, URL))

    @patch("urllib.request.build_opener")
    def test_scheduled_entry_point_and_network_error_retry(self, make_opener):
        opener = make_opener.return_value
        opener.open.side_effect = [OSError("temporary failure"), FakeResponse({"errcode": 0})]
        with self.assertRaises(OSError):
            run_daily_notification(self.root / "state.sqlite3", self.root / "records",
                                   self.day, URL)
        self.assertTrue(self.reporter.run_daily_notification(self.day, URL))
        self.assertFalse(self.reporter.run_daily_notification(self.day, URL))
        self.assertEqual(opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
