#!/usr/bin/env python3
"""逐条跑 samples/cases.json，按 README 的输出格式打印结果（行尾 LF）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathguard import format_line, normalize


def main(argv):
    base = os.path.dirname(os.path.abspath(__file__))
    cases_path = argv[1] if len(argv) > 1 else os.path.join(
        base, "samples", "cases.json"
    )
    with open(cases_path, encoding="utf-8") as fh:
        cases = json.load(fh)
    for case in cases:
        result = normalize(case["root"], case["request"])
        print(format_line(case["id"], result))


if __name__ == "__main__":
    main(sys.argv)
