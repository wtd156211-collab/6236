"""pathnorm 的单元测试：规则单测、样例对照、幂等性、性能冒烟。"""

import json
import time
import unittest
from pathlib import Path

from pathnorm import PathRejected, normalize
from run_samples import format_case

BASE = Path(__file__).resolve().parent
ROOT = "/srv/tenants/acme"


class RuleTest(unittest.TestCase):
    def assertRejected(self, request, code, segment, detail, root=ROOT):
        with self.assertRaises(PathRejected) as ctx:
            normalize(root, request)
        exc = ctx.exception
        self.assertEqual(exc.code, code)
        self.assertEqual(exc.segment, segment)
        self.assertEqual(exc.detail, detail)

    def test_basic_join(self):
        self.assertEqual(
            normalize(ROOT, "reports/2026/q3.csv"),
            "/srv/tenants/acme/reports/2026/q3.csv",
        )

    def test_backslash_separator(self):
        self.assertEqual(normalize(ROOT, r"a\b\c.txt"), "/srv/tenants/acme/a/b/c.txt")

    def test_dot_and_empty_segments_folded(self):
        self.assertEqual(normalize(ROOT, "a//./b///c.txt"), "/srv/tenants/acme/a/b/c.txt")

    def test_trailing_separator_dropped(self):
        self.assertEqual(normalize(ROOT, "logs/2026/"), "/srv/tenants/acme/logs/2026")

    def test_empty_request_gives_root(self):
        self.assertEqual(normalize(ROOT, ""), ROOT)

    def test_fold_back_to_root(self):
        self.assertEqual(normalize(ROOT, "a/.."), ROOT)

    def test_root_with_trailing_separator(self):
        self.assertEqual(normalize("/srv/tenants/acme/", "x.txt"), "/srv/tenants/acme/x.txt")

    def test_case_preserved_in_output(self):
        self.assertEqual(
            normalize(ROOT, "Reports/Q3/Final.CSV"),
            "/srv/tenants/acme/Reports/Q3/Final.CSV",
        )

    def test_wildcard_looking_chars_are_literal(self):
        self.assertEqual(
            normalize(ROOT, "exports/[2026]/#1~tmp%.csv"),
            "/srv/tenants/acme/exports/[2026]/#1~tmp%.csv",
        )

    def test_absolute_path_rejected(self):
        self.assertRejected("/etc/passwd", "ABSOLUTE_PATH", 0, "prefix=/e")
        self.assertRejected("\\etc\\passwd", "ABSOLUTE_PATH", 0, "prefix=\\e")

    def test_only_separators_rejected(self):
        self.assertRejected("////", "ABSOLUTE_PATH", 0, "prefix=//")
        self.assertRejected("\\\\\\", "ABSOLUTE_PATH", 0, "prefix=\\\\")

    def test_drive_prefix_rejected(self):
        self.assertRejected("C:\\Windows\\win.ini", "ABSOLUTE_PATH", 0, "prefix=C:")
        self.assertRejected("d:/data", "ABSOLUTE_PATH", 0, "prefix=d:")

    def test_unc_prefix_rejected(self):
        self.assertRejected("\\\\server\\share\\f", "ABSOLUTE_PATH", 0, "prefix=\\\\")

    def test_escape_segment_index(self):
        self.assertRejected("../secret.txt", "ESCAPE", 1, "segment=..")
        self.assertRejected("a/b/../../../c.txt", "ESCAPE", 5, "segment=..")

    def test_escape_index_counts_empty_segments(self):
        # 切分结果是 ['a', '', '..', '..']，越界发生在第 4 段。
        self.assertRejected("a//../..", "ESCAPE", 4, "segment=..")

    def test_dot_dot_exactly_at_root_allowed(self):
        self.assertEqual(normalize(ROOT, "a/b/../../c.txt"), "/srv/tenants/acme/c.txt")

    def test_reserved_names(self):
        self.assertRejected("logs/con", "RESERVED_NAME", 2, "name=con")
        self.assertRejected("logs/LPT1.txt", "RESERVED_NAME", 2, "name=LPT1.txt")
        self.assertRejected("Aux.cfg", "RESERVED_NAME", 1, "name=Aux.cfg")
        self.assertRejected("COM9", "RESERVED_NAME", 1, "name=COM9")
        self.assertRejected("nul", "RESERVED_NAME", 1, "name=nul")

    def test_reserved_lookalikes_allowed(self):
        self.assertEqual(
            normalize(ROOT, "notes/con-notes.txt"), "/srv/tenants/acme/notes/con-notes.txt"
        )
        self.assertEqual(normalize(ROOT, "com10/a"), "/srv/tenants/acme/com10/a")
        self.assertEqual(normalize(ROOT, "lpt0"), "/srv/tenants/acme/lpt0")

    def test_bad_chars(self):
        for ch in '*?"<>|:':
            self.assertRejected("logs/a%sb" % ch, "BAD_CHARS", 2, "char=" + ch)
        self.assertRejected("logs/a\x01b", "BAD_CHARS", 2, "char=0x01")
        self.assertRejected("logs/a\x1fb", "BAD_CHARS", 2, "char=0x1f")

    def test_segment_too_long(self):
        seg = "x" * 256
        self.assertRejected("logs/" + seg, "TOO_LONG", 2, "segment_len=256")
        self.assertEqual(
            normalize(ROOT, "x" * 255, max_path_len=512), ROOT + "/" + "x" * 255
        )

    def test_path_too_long(self):
        # 根 17 + 1 + 250 = 268 > 260，单段 250 不超长。
        self.assertRejected("a" * 250, "TOO_LONG", 0, "path_len=268")
        # 根 17 + 1 + 242 = 260，恰好不超长。
        self.assertEqual(normalize(ROOT, "a" * 242), ROOT + "/" + "a" * 242)

    def test_custom_limits(self):
        self.assertEqual(
            normalize(ROOT, "a" * 300, max_segment_len=512, max_path_len=1024),
            ROOT + "/" + "a" * 300,
        )


class SamplesTest(unittest.TestCase):
    def test_all_cases_match_expected(self):
        cases = json.loads((BASE / "samples" / "cases.json").read_text(encoding="utf-8"))
        expected = (BASE / "samples" / "expected.txt").read_text(encoding="utf-8").splitlines()
        actual = [format_case(case) for case in cases]
        self.assertEqual(actual, expected)


class IdempotencyTest(unittest.TestCase):
    def _allowed_results(self):
        cases = json.loads((BASE / "samples" / "cases.json").read_text(encoding="utf-8"))
        for case in cases:
            try:
                yield case["root"], normalize(case["root"], case["request"])
            except PathRejected:
                continue

    def test_samples_are_idempotent(self):
        count = 0
        for root, result in self._allowed_results():
            count += 1
            self.assertTrue(result.startswith(root.rstrip("/")))
            relative = result[len(root.rstrip("/")):].lstrip("/")
            self.assertEqual(normalize(root, relative), result)
        self.assertEqual(count, 12)

    def test_normalize_twice_is_identity(self):
        requests = [
            "a//./b///c.txt",
            "logs/2026/",
            "a/b/../../c.txt",
            "Reports/Q3/Final.CSV",
            "exports/[2026]/#1~tmp%.csv",
            "",
            "a/..",
        ]
        for req in requests:
            once = normalize(ROOT, req)
            relative = once[len(ROOT):].lstrip("/")
            twice = normalize(ROOT, relative)
            self.assertEqual(once, twice)
            self.assertIs(type(once), str)

    def test_deterministic_byte_for_byte(self):
        req = "a//./B/../Reports/Q3.CSV"
        self.assertEqual(normalize(ROOT, req), normalize(ROOT, req))


class PerformanceTest(unittest.TestCase):
    def test_microsecond_per_normalize(self):
        requests = [
            "reports/2026/q3.csv",
            "a//./b///c.txt",
            "a/b/../../c.txt",
            "Reports/Q3/Final.CSV",
            "exports/[2026]/#1~tmp%.csv",
        ]
        rounds = 20000
        start = time.perf_counter()
        for i in range(rounds):
            normalize(ROOT, requests[i % len(requests)])
        elapsed = time.perf_counter() - start
        per_call_us = elapsed / rounds * 1e6
        print("\nperf: %d 次规范化耗时 %.3fs，单次 %.2fµs" % (rounds, elapsed, per_call_us))
        self.assertLess(per_call_us, 50.0)  # 微秒级，留足余量防抖动


if __name__ == "__main__":
    unittest.main()
