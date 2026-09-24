"""多租户文件网关的路径规范化库。

把用户请求里的相对路径规范化到服务端根目录之下；越界、保留设备名、
非法字符、超长一律拒绝，并给出原因码、出问题的段序号和 detail。
规则与输出格式见 README.md。
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "PathRejected",
    "normalize",
    "DEFAULT_MAX_SEGMENT_LEN",
    "DEFAULT_MAX_PATH_LEN",
]

DEFAULT_MAX_SEGMENT_LEN = 255
DEFAULT_MAX_PATH_LEN = 260

# 比较口径：ASCII 小写。用 translate 表比 str.lower() 快，且只折 A-Z。
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)

# 保留设备名的主干（第一个点之前的部分，ASCII 小写后比较）。
_RESERVED_STEMS = frozenset(
    ("con", "prn", "aux", "nul")
    + tuple("com%d" % i for i in range(1, 10))
    + tuple("lpt%d" % i for i in range(1, 10))
)

_SPLIT_RE = re.compile(r"[/\\]")
_BAD_CHARS_RE = re.compile(r'[\x00-\x1f*?"<>|:]')


class PathRejected(Exception):
    """请求路径被拒绝。

    code:    原因码（ABSOLUTE_PATH / ESCAPE / RESERVED_NAME / BAD_CHARS / TOO_LONG）
    segment: 出问题的段在原始请求按分隔符切分后的位置（从 1 开始，空段也算）；
             整体性问题为 0。
    detail:  形如 segment=.. / name=LPT1.txt / char=* / path_len=306，不含逗号。
    """

    def __init__(self, code: str, segment: int, detail: str) -> None:
        super().__init__("%s at segment %d: %s" % (code, segment, detail))
        self.code = code
        self.segment = segment
        self.detail = detail


@lru_cache(maxsize=256)
def _parse_root(root: str):
    """解析根目录并缓存结果。

    返回 (display, lower_segments, joiner)：
    - display:         折叠分隔符与末尾分隔符后的根目录展示串，输出直接以它为前缀；
    - lower_segments:  按段 ASCII 小写后的元组，供硬约束的按段比较用；
    - joiner:          拼请求段时根与第一段之间的连接符（根为 "/" 时为空串）。

    缓存只是 root 字符串的纯函数，不改变任何结果。
    """
    if not root:
        raise ValueError("root 不能为空")
    leading = root[0] in "/\\"
    segments = []
    for seg in _SPLIT_RE.split(root):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if segments:
                segments.pop()
            continue
        segments.append(seg)
    display = "/".join(segments)
    if leading:
        display = "/" + display
    if not display:
        display = "/"
    joiner = "" if display.endswith("/") else "/"
    return display, tuple(seg.translate(_ASCII_LOWER) for seg in segments), joiner


def _under_root(final: str, root_display: str, root_lower) -> bool:
    """硬约束兜底：final 必须以根目录为前缀（按段比较、不区分大小写）。"""
    if final == root_display or final.startswith(root_display + "/"):
        return True
    if root_display.endswith("/") and final.startswith(root_display):
        return True
    # 慢路径：大小写不一致时按段小写比较（构造上不会走到，防御用）。
    final_segments = [s for s in _SPLIT_RE.split(final) if s]
    if len(final_segments) < len(root_lower):
        return False
    return all(
        seg.translate(_ASCII_LOWER) == want
        for seg, want in zip(final_segments, root_lower)
    )


def normalize(
    root: str,
    request: str,
    max_segment_len: int = DEFAULT_MAX_SEGMENT_LEN,
    max_path_len: int = DEFAULT_MAX_PATH_LEN,
) -> str:
    """把 request 规范化成 root 之下的绝对路径。

    成功返回规范化后的绝对路径（保留请求原始大小写，分隔符统一为 "/"）；
    失败抛 PathRejected。单遍扫描：一次切分、一次拼接，长度随扫描累计。
    """
    root_display, root_lower, joiner = _parse_root(root)

    # 整体性问题：绝对路径 / 盘符 / UNC，段序号一律 0。
    if request:
        first = request[0]
        if first == "/" or first == "\\":
            raise PathRejected("ABSOLUTE_PATH", 0, "prefix=" + request[:2])
        if (
            len(request) >= 2
            and request[1] == ":"
            and ("A" <= first <= "Z" or "a" <= first <= "z")
        ):
            raise PathRejected("ABSOLUTE_PATH", 0, "prefix=" + request[:2])

    kept = []     # 折叠后的段（保留原始大小写）
    lengths = []  # 每段对最终路径长度的贡献（含前面的分隔符）
    total = len(root_display)
    pending_sep = 0 if root_display.endswith("/") else 1

    for index, seg in enumerate(_SPLIT_RE.split(request), 1):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if not kept:
                raise PathRejected("ESCAPE", index, "segment=..")
            kept.pop()
            total -= lengths.pop()
            continue
        bad = _BAD_CHARS_RE.search(seg)
        if bad is not None:
            ch = bad.group()
            shown = ch if ord(ch) >= 0x20 else "0x%02x" % ord(ch)
            raise PathRejected("BAD_CHARS", index, "char=" + shown)
        stem = seg.partition(".")[0]
        if stem.translate(_ASCII_LOWER) in _RESERVED_STEMS:
            raise PathRejected("RESERVED_NAME", index, "name=" + seg)
        if len(seg) > max_segment_len:
            raise PathRejected("TOO_LONG", index, "segment_len=%d" % len(seg))
        kept.append(seg)
        lengths.append(pending_sep + len(seg))
        total += pending_sep + len(seg)
        pending_sep = 1

    if total > max_path_len:
        raise PathRejected("TOO_LONG", 0, "path_len=%d" % total)

    final = root_display + joiner + "/".join(kept) if kept else root_display

    # 硬约束：拼接结果必须落在根目录内（按段、不区分大小写）。
    if not _under_root(final, root_display, root_lower):
        raise PathRejected("ESCAPE", 0, "prefix=" + root_display)
    return final
