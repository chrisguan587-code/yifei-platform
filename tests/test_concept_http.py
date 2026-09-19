import json
import subprocess
import time
import unittest
from unittest.mock import patch

from yifei_platform.concept_http import _catalog, _get, _jsonp, _members, _retry_transient_concepts, fetch_ths_json_concepts
from yifei_platform.concept_membership import _complete_report


def jsonp(total=2, codes=("000001", "000002"), name="机器人概念"):
    return "quotebridge_test(" + json.dumps({
        "block": {"name": name, "subcodeCount": total},
        "items": [{"5": code} for code in codes],
    }) + ")"


DETAIL = '<h3>机器人概念<span>885517</span></h3><input id="clid" value=\'885517\'>'


class ConceptHttpTest(unittest.TestCase):
    def test_recovery_retries_only_transient_failures_once(self):
        rows = [
            {"concept_code": "1", "concept_name": "成功", "complete": True},
            {"concept_code": "2", "concept_name": "暂时失败", "error": "ConnectionError: THS HTTP failed or empty after 3 attempts"},
            {"concept_code": "3", "concept_name": "不完整", "error": "ValueError: THS member list truncated, duplicated or invalid"},
        ]
        with patch("yifei_platform.concept_http._members", return_value={"complete": True}) as members, \
             patch("yifei_platform.concept_http.time.sleep"):
            self.assertEqual((1, 1), _retry_transient_concepts(rows, time.monotonic()+60))
        members.assert_called_once()
        self.assertTrue(rows[1]["complete"])
        self.assertIn("initial_error", rows[1])
        self.assertNotIn("complete", rows[2])

    def test_recovery_stops_on_denial_and_respects_deadline(self):
        rows = [{"concept_code": str(i), "concept_name": "x",
                 "error": "ConnectionError: THS HTTP failed or empty after 3 attempts"} for i in range(3)]
        with patch("yifei_platform.concept_http._members", side_effect=ConnectionError("access denied")) as members, \
             patch("yifei_platform.concept_http.time.sleep"):
            self.assertEqual((0, 0), _retry_transient_concepts(rows, time.monotonic()-1))
            self.assertEqual((1, 0), _retry_transient_concepts(rows, time.monotonic()+60))
        members.assert_called_once()

    def test_catalog_requires_full_unique_directory(self):
        text = "".join(f'<a href="https://q.10jqka.com.cn/gn/detail/code/{300000+i}/">概念{i}</a>'
                       for i in range(300))
        table = "<tbody>" + text + "</tbody>"
        self.assertEqual(300, len(_catalog(table)))
        for bad in ("", table.replace("概念299", "概念298"), "<html>challenge</html>",
                    text, table + '<span class="page_info">1/39</span>',
                    '<tbody>' + text + text + '</tbody>'):
            with self.assertRaises(ValueError):
                _catalog(bad)

    def test_catalog_ignores_stale_sidebar_and_rejects_truncated_table(self):
        sidebar = ''.join(f'<a href="/gn/detail/code/{300000+i}/">概念{i}</a>' for i in range(350))
        table = '<tbody>' + ''.join(f'<a href="/gn/detail/code/{300000+i}/">概念{i}</a>' for i in range(389)) + '</tbody>'
        self.assertEqual(389, len(_catalog(sidebar + table)))
        with self.assertRaises(ValueError):
            _catalog(sidebar + '<tbody><a href="/gn/detail/code/309269/">MLCC</a></tbody>')

    def test_jsonp_does_not_execute_script_or_accept_empty_html(self):
        for bad in ("", "<html>blocked</html>", "alert(1)", jsonp() + ";alert(1)"):
            with self.assertRaises(ValueError):
                _jsonp(bad)

    def test_identity_keeps_website_code_and_actual_index(self):
        with patch("yifei_platform.concept_http._get", side_effect=[DETAIL, jsonp()]):
            row = _members(("300816", "机器人概念"), time.monotonic()+10)
        self.assertEqual("300816", row["concept_code"])
        self.assertEqual("885517", row["index_code"])
        self.assertTrue(row["complete"])

    def test_large_sector_fetches_all_not_first_five_pages(self):
        codes = [f"{i:06d}" for i in range(1084)]
        with patch("yifei_platform.concept_http._get", side_effect=[
            DETAIL, jsonp(1084, codes[:15]), jsonp(1084, codes)
        ]) as get:
            row = _members(("300816", "机器人概念"), time.monotonic()+10)
        self.assertEqual(1084, len(row["member_codes"]))
        self.assertIn("d1095.js", get.call_args.args[0])

    def test_truncation_duplicate_invalid_count_and_mismatched_identity_fail(self):
        for body in (jsonp(3), jsonp(2, ["000001", "000001"]),
                     jsonp(2, ["000001", "USHA600001"]), jsonp(name="另一概念"),
                     jsonp(0, []), jsonp("2")):
            with self.subTest(body=body), patch("yifei_platform.concept_http._get", side_effect=[DETAIL, body]):
                with self.assertRaises(ValueError):
                    _members(("300816", "机器人概念"), time.monotonic()+10)

    def test_index_mapping_mismatch_fails_without_member_request(self):
        with patch("yifei_platform.concept_http._get", return_value=DETAIL) as get:
            with self.assertRaises(ValueError):
                _members(("300816", "其他名称"), time.monotonic()+10)
        get.assert_called_once()

    def test_count_changes_during_capture_are_rejected(self):
        with patch("yifei_platform.concept_http._get", side_effect=[DETAIL, jsonp(16), jsonp(17)]):
            with self.assertRaises(ValueError):
                _members(("300816", "机器人概念"), time.monotonic()+10)

    def test_retry_empty_and_transient_failure_then_success(self):
        with patch("yifei_platform.concept_http.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 28, b"", b"timeout"),
            subprocess.CompletedProcess([], 0, b"ok", b""),
        ]) as run, patch("yifei_platform.concept_http.time.sleep") as sleep:
            self.assertEqual("ok", _get("https://d.10jqka.com.cn/test", time.monotonic()+60))
        self.assertEqual(3, run.call_count)
        self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])

    def test_corrupt_encoding_retries_without_replacing_bad_bytes(self):
        with patch("yifei_platform.concept_http.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, b"\x81", b""),
            subprocess.CompletedProcess([], 0, b"ok", b""),
        ]) as run, patch("yifei_platform.concept_http.time.sleep"):
            self.assertEqual("ok", _get("https://d.10jqka.com.cn/test", time.monotonic()+60))
        self.assertEqual(2, run.call_count)

    def test_html_widget_bad_bytes_do_not_corrupt_fact_fields(self):
        with patch("yifei_platform.concept_http.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, b"<script>\x81</script>", b"")):
            self.assertIn("\ufffd", _get("http://q.10jqka.com.cn/gn/", time.monotonic()+20))
        text = '<tbody>' + ''.join(f'<a href="/gn/detail/code/{300000+i}/">概念{i}</a>' for i in range(300)) + '</tbody>'
        self.assertEqual(300, len(_catalog(text + '<script>\ufffd</script>')))
        with self.assertRaises(ValueError):
            _catalog(text.replace('概念299', '概念\ufffd'))

    def test_retry_exhaustion_and_denial_are_bounded(self):
        for status, expected in ((b"500", 3), (b"403", 1), (b"429", 1)):
            with patch("yifei_platform.concept_http.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 22, b"", b"curl: (22) The requested URL returned error: " + status)) as run, \
                 patch("yifei_platform.concept_http.time.sleep"):
                with self.assertRaises(ConnectionError):
                    _get("https://d.10jqka.com.cn/test", time.monotonic()+60)
            self.assertEqual(expected, run.call_count)

    def test_expired_source_does_not_start_request(self):
        with patch("yifei_platform.concept_http.subprocess.run") as run:
            with self.assertRaises(TimeoutError):
                _get("https://d.10jqka.com.cn/test", time.monotonic()-1)
        run.assert_not_called()

    def test_provider_cannot_mark_truncated_members_complete(self):
        rows = [{"concept_code": str(300000+i), "member_codes": ["000001"],
                 "reported_member_count": 2, "complete": True} for i in range(300)]
        report = _complete_report(source="test", taxonomy="ths_concept", started=time.perf_counter(),
            concepts=rows, reported_concept_count=300, returned_member_rows=300, valid_member_codes=300)
        self.assertFalse(report["ok"])
        self.assertFalse(any(row["complete"] for row in report["concepts"]))

    def test_denial_stops_whole_source_without_hammering_all_concepts(self):
        with patch("yifei_platform.concept_http._get", return_value="directory"), \
             patch("yifei_platform.concept_http._catalog", return_value=[(str(i),str(i)) for i in range(300)]), \
             patch("yifei_platform.concept_http._members", side_effect=ConnectionError("access denied")) as members:
            report = fetch_ths_json_concepts()
        self.assertFalse(report["ok"])
        self.assertLessEqual(members.call_count, 2)


if __name__ == "__main__":
    unittest.main()
