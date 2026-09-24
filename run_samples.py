"""逐条跑 samples/cases.json，按 README 的输出格式打印结果。"""

import json
import sys
from pathlib import Path

from pathnorm import PathRejected, normalize

BASE = Path(__file__).resolve().parent


def format_case(case):
    try:
        path = normalize(case["root"], case["request"])
    except PathRejected as exc:
        return "%s,error,%s,%d,%s" % (case["id"], exc.code, exc.segment, exc.detail)
    return "%s,ok,%s" % (case["id"], path)


def main():
    cases = json.loads((BASE / "samples" / "cases.json").read_text(encoding="utf-8"))
    lines = [format_case(case) for case in cases]
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
