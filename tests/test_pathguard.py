"""pathguard 的 unittest 测试。

跑法：python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathguard import (
    ABSOLUTE_PATH,
    BAD_CHARS,
    ESCAPE,
    RESERVED_NAME,
    TOO_LONG,
    format_line,
    normalize,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = "/srv/tenants/acme"


def load_samples():
    with open(os.path.join(BASE, "samples", "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    with open(os.path.join(BASE, "samples", "expected.txt"), encoding="utf-8") as fh:
        expected = [line for line in fh.read().split("\n") if line]
    return cases, expected


def strip_root(path, root=ROOT):
    """把规范化结果去掉根前缀，还原成可以再次喂给 normalize 的请求。"""
    if path == root:
        return ""
    assert path.startswith(root + "/"), path
    return path[len(root) + 1:]


class SamplesTest(unittest.TestCase):
    """samples 里的 28 条用例逐条对上 expected.txt。"""

    def test_cases_match_expected(self):
        cases, expected = load_samples()
        self.assertEqual(len(cases), len(expected))
        actual = [
            format_line(case["id"], normalize(case["root"], case["request"]))
            for case in cases
        ]
        self.assertEqual(actual, expected)

    def test_samples_are_idempotent(self):
        cases, _ = load_samples()
        for case in cases:
            first = normalize(case["root"], case["request"])
            if not first.ok:
                continue
            again = normalize(case["root"], strip_root(first.path, _joined_root(case["root"])))
            self.assertTrue(again.ok, case["id"])
            self.assertEqual(first.path, again.path, case["id"])


def _joined_root(root):
    segments = [s for s in root.replace("\\", "/").split("/") if s and s != "."]
    return "/" + "/".join(segments)


class FoldingTest(unittest.TestCase):
    def test_backslash_is_separator(self):
        r = normalize(ROOT, "reports\\2026\\q3.csv")
        self.assertEqual(r.path, ROOT + "/reports/2026/q3.csv")

    def test_dot_and_repeated_separators_collapse(self):
        r = normalize(ROOT, "a//./b///c.txt")
        self.assertEqual(r.path, ROOT + "/a/b/c.txt")

    def test_trailing_separator_dropped(self):
        self.assertEqual(
            normalize(ROOT, "logs/2026/").path,
            normalize(ROOT, "logs/2026").path,
        )

    def test_empty_request_is_root(self):
        self.assertEqual(normalize(ROOT, "").path, ROOT)

    def test_fold_back_to_root(self):
        self.assertEqual(normalize(ROOT, "a/..").path, ROOT)

    def test_root_with_trailing_separator(self):
        r = normalize("/srv/tenants/acme/", "x.txt")
        self.assertEqual(r.path, ROOT + "/x.txt")

    def test_case_preserved_in_output(self):
        r = normalize(ROOT, "Reports/Q3/Final.CSV")
        self.assertEqual(r.path, ROOT + "/Reports/Q3/Final.CSV")

    def test_wildcard_looking_chars_are_literal(self):
        r = normalize(ROOT, "exports/[2026]/#1~tmp%.csv")
        self.assertTrue(r.ok)
        self.assertEqual(r.path, ROOT + "/exports/[2026]/#1~tmp%.csv")

    def test_del_and_unicode_allowed(self):
        r = normalize(ROOT, "d\x7felete/中文目录/文件.txt")
        self.assertTrue(r.ok)


class AbsolutePathTest(unittest.TestCase):
    def assert_absolute(self, request, prefix):
        r = normalize(ROOT, request)
        self.assertFalse(r.ok, request)
        self.assertEqual(r.code, ABSOLUTE_PATH)
        self.assertEqual(r.segment_index, 0)
        self.assertEqual(r.detail, "prefix=" + prefix)

    def test_leading_slash(self):
        self.assert_absolute("/etc/passwd", "/e")

    def test_leading_backslash(self):
        self.assert_absolute("\\etc\\passwd", "\\e")

    def test_only_separators(self):
        self.assert_absolute("////", "//")
        self.assert_absolute("\\\\\\", "\\\\")
        self.assert_absolute("/", "/")

    def test_unc_prefix(self):
        self.assert_absolute("\\\\server\\share\\file", "\\\\")

    def test_drive_prefix(self):
        self.assert_absolute("C:\\Windows\\win.ini", "C:")
        self.assert_absolute("c:/windows", "c:")
        self.assert_absolute("D:relative.txt", "D:")

    def test_single_letter_without_colon_is_relative(self):
        r = normalize(ROOT, "C/Windows")
        self.assertTrue(r.ok)


class EscapeTest(unittest.TestCase):
    def assert_escape(self, request, segment_index):
        r = normalize(ROOT, request)
        self.assertFalse(r.ok, request)
        self.assertEqual(r.code, ESCAPE)
        self.assertEqual(r.segment_index, segment_index)
        self.assertEqual(r.detail, "segment=..")

    def test_escape_at_first_segment(self):
        self.assert_escape("../secret.txt", 1)

    def test_escape_after_deep_pop(self):
        self.assert_escape("a/b/../../../c.txt", 5)

    def test_dotdot_exactly_at_root_is_allowed(self):
        r = normalize(ROOT, "a/b/../../c.txt")
        self.assertEqual(r.path, ROOT + "/c.txt")

    def test_empty_segments_count_for_position(self):
        # a(1) ""(2) ..(3) 弹出 a，..(4) 在根上再弹，越界在第 4 段
        self.assert_escape("a//../../x", 4)

    def test_backslash_dotdot(self):
        self.assert_escape("a\\..\\..\\x", 3)


class ReservedNameTest(unittest.TestCase):
    def assert_reserved(self, request, segment_index, name):
        r = normalize(ROOT, request)
        self.assertFalse(r.ok, request)
        self.assertEqual(r.code, RESERVED_NAME)
        self.assertEqual(r.segment_index, segment_index)
        self.assertEqual(r.detail, "name=" + name)

    def test_reserved_names(self):
        self.assert_reserved("logs/con", 2, "con")
        self.assert_reserved("logs/LPT1.txt", 2, "LPT1.txt")
        self.assert_reserved("Aux.cfg", 1, "Aux.cfg")
        self.assert_reserved("NUL", 1, "NUL")
        self.assert_reserved("com9/ok.txt", 1, "com9")
        self.assert_reserved("a/b/PrN", 3, "PrN")

    def test_lookalikes_are_allowed(self):
        for ok_name in ("con-notes.txt", "com0", "com10", "lpt0", "auxiliary"):
            r = normalize(ROOT, "notes/" + ok_name)
            self.assertTrue(r.ok, ok_name)

    def test_stem_before_first_dot_is_reserved(self):
        # 「con..txt」的主干（第一个点之前）是 con，照样拒绝
        self.assert_reserved("con..txt", 1, "con..txt")


class BadCharsTest(unittest.TestCase):
    def assert_bad_char(self, request, segment_index, detail):
        r = normalize(ROOT, request)
        self.assertFalse(r.ok, request)
        self.assertEqual(r.code, BAD_CHARS)
        self.assertEqual(r.segment_index, segment_index)
        self.assertEqual(r.detail, detail)

    def test_each_illegal_char(self):
        for ch in '*?"<>|':
            self.assert_bad_char("logs/a%sb.txt" % ch, 2, "char=" + ch)

    def test_colon_inside_segment(self):
        self.assert_bad_char("logs/a:b.txt", 2, "char=:")

    def test_control_chars(self):
        self.assert_bad_char("logs/a\x01b.txt", 2, "char=0x01")
        self.assert_bad_char("logs/a\x00b.txt", 2, "char=0x00")
        self.assert_bad_char("logs/a\x1fb.txt", 2, "char=0x1f")

    def test_first_bad_char_reported(self):
        self.assert_bad_char("a*b?.txt", 1, "char=*")


class TooLongTest(unittest.TestCase):
    def test_segment_too_long(self):
        segment = "x" * 300 + ".txt"
        r = normalize(ROOT, "logs/" + segment)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, TOO_LONG)
        self.assertEqual(r.segment_index, 2)
        self.assertEqual(r.detail, "segment_len=304")

    def test_segment_exactly_at_limit_allowed(self):
        segment = "x" * 255
        r = normalize(ROOT, "logs/" + segment, max_path_len=1000)
        self.assertTrue(r.ok)

    def test_path_too_long(self):
        request = "/".join(["seg"] * 70 + ["file.txt"])
        r = normalize(ROOT, request)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, TOO_LONG)
        self.assertEqual(r.segment_index, 0)
        self.assertEqual(r.detail, "path_len=306")

    def test_custom_limits(self):
        r = normalize(ROOT, "abcdef/gh", max_segment_len=5)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, TOO_LONG)
        self.assertEqual(r.detail, "segment_len=6")
        r = normalize(ROOT, "a/b", max_path_len=len(ROOT) + 3)
        self.assertFalse(r.ok)
        self.assertEqual(r.code, TOO_LONG)
        self.assertEqual(r.segment_index, 0)


class IdempotencyTest(unittest.TestCase):
    """规范化必须幂等：结果去掉根前缀再规范化一次，逐字节不变。"""

    REQUESTS = [
        "",
        "a/..",
        "a//./b///c.txt",
        "logs/2026/",
        "reports\\2026\\q3.csv",
        "Reports/Q3/Final.CSV",
        "exports/[2026]/#1~tmp%.csv",
        "a/b/../../c.txt",
        "a/b/c/d/e/f/g/h.txt",
        "notes/con-notes.txt",
        "x.txt",
    ]

    def test_idempotent(self):
        for request in self.REQUESTS:
            first = normalize(ROOT, request)
            self.assertTrue(first.ok, request)
            second = normalize(ROOT, strip_root(first.path))
            self.assertTrue(second.ok, request)
            self.assertEqual(first.path, second.path, request)

    def test_idempotent_with_trailing_slash_root(self):
        root = "/srv/tenants/acme/"
        for request in self.REQUESTS:
            first = normalize(root, request)
            self.assertTrue(first.ok, request)
            second = normalize(root, strip_root(first.path))
            self.assertEqual(first.path, second.path, request)

    def test_deterministic(self):
        for request in self.REQUESTS + ["../x", "logs/con", "logs/a*b"]:
            first = normalize(ROOT, request)
            second = normalize(ROOT, request)
            self.assertEqual(first, second, request)
            self.assertEqual(
                format_line("id", first), format_line("id", second), request
            )


class FormatTest(unittest.TestCase):
    def test_ok_line(self):
        r = normalize(ROOT, "a/b.txt")
        self.assertEqual(format_line("case-1", r), "case-1,ok," + ROOT + "/a/b.txt")

    def test_error_line(self):
        r = normalize(ROOT, "../x")
        self.assertEqual(format_line("case-2", r), "case-2,error,ESCAPE,1,segment=..")

    def test_detail_has_no_comma(self):
        cases, _ = load_samples()
        for case in cases:
            line = format_line(case["id"], normalize(case["root"], case["request"]))
            if ",error," in line:
                detail = line.split(",", 4)[4]
                self.assertNotIn(",", detail)


class PerformanceTest(unittest.TestCase):
    """性能冒烟：几千次判断要在毫秒级完成，单次微秒级。"""

    def test_throughput(self):
        requests = [
            "reports/2026/q3.csv",
            "a//./b///c.txt",
            "a/b/../../c.txt",
            "exports/[2026]/#1~tmp%.csv",
            "../escape",
            "logs/con",
        ]
        rounds = 20000
        start = time.perf_counter()
        for i in range(rounds):
            normalize(ROOT, requests[i % len(requests)])
        elapsed = time.perf_counter() - start
        per_call_us = elapsed / rounds * 1e6
        # 非常宽松的上限，只为挡住明显退化；正常应在微秒级
        self.assertLess(per_call_us, 100.0)
        print(
            "\nperf: %d 次规范化耗时 %.3fs，单次 %.2f µs"
            % (rounds, elapsed, per_call_us)
        )


if __name__ == "__main__":
    unittest.main()
