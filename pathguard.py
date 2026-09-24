"""多租户文件网关的路径规范化库。

把用户请求里的相对路径折叠、校验后拼到租户根目录下面，
保证结果一定落在根目录里面。只用标准库。

规则见 README.md 的规则表；这里只列实现要点：

* 单遍扫描：一次循环完成切段、逐段校验与 ``..`` 折叠，
  最后只做一次 ``join`` 拼出绝对路径，不在循环里反复
  ``split``/``join``/字符串相加；
* 根目录的解析结果（折叠后的段、拼接好的前缀、小写形式）
  按根目录字符串缓存，重复请求不重复解析；
* 段序号在扫描分隔符时顺手数出来，空段也占一个位置。
"""

from __future__ import annotations

__all__ = [
    "normalize",
    "format_line",
    "Result",
    "ABSOLUTE_PATH",
    "ESCAPE",
    "RESERVED_NAME",
    "BAD_CHARS",
    "TOO_LONG",
    "DEFAULT_MAX_SEGMENT_LEN",
    "DEFAULT_MAX_PATH_LEN",
]

# 拒绝原因码
ABSOLUTE_PATH = "ABSOLUTE_PATH"
ESCAPE = "ESCAPE"
RESERVED_NAME = "RESERVED_NAME"
BAD_CHARS = "BAD_CHARS"
TOO_LONG = "TOO_LONG"

DEFAULT_MAX_SEGMENT_LEN = 255
DEFAULT_MAX_PATH_LEN = 260

# ASCII 控制字符（0x00-0x1F）加上 * ? " < > | :
_BAD_CHARS = frozenset(chr(c) for c in range(0x20)) | frozenset('*?"<>|:')

_RESERVED_NAMES = frozenset(
    ("con", "prn", "aux", "nul")
    + tuple("com%d" % i for i in range(1, 10))
    + tuple("lpt%d" % i for i in range(1, 10))
)

# 只折叠 ASCII A-Z，其他字符原样（规则表要求按 ASCII 小写比）
_ASCII_LOWER_TABLE = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _ascii_lower(text):
    return text.translate(_ASCII_LOWER_TABLE)


class Result:
    """一次规范化的结果：``ok`` 为真时 ``path`` 是规范化后的绝对路径，
    否则 ``code``/``segment_index``/``detail`` 描述拒绝原因。"""

    __slots__ = ("ok", "path", "code", "segment_index", "detail")

    def __init__(self, ok, path=None, code=None, segment_index=0, detail=""):
        self.ok = ok
        self.path = path
        self.code = code
        self.segment_index = segment_index
        self.detail = detail

    def __eq__(self, other):
        return (
            isinstance(other, Result)
            and self.ok == other.ok
            and self.path == other.path
            and self.code == other.code
            and self.segment_index == other.segment_index
            and self.detail == other.detail
        )

    def __repr__(self):
        if self.ok:
            return "Result(ok, path=%r)" % (self.path,)
        return "Result(error, code=%r, segment_index=%r, detail=%r)" % (
            self.code,
            self.segment_index,
            self.detail,
        )


def _error(code, segment_index, detail):
    return Result(False, code=code, segment_index=segment_index, detail=detail)


class _RootInfo:
    """根目录的缓存解析结果。

    * ``joined``: 折叠后的根目录，如 ``/srv/tenants/acme``（根为 ``/`` 时是 ``/``）；
    * ``prefix``: 拼子路径时用的前缀，与 ``joined`` 相同，但根为 ``/`` 时是空串，
      避免拼出 ``//x``；
    * ``lower``: ``joined`` 的 ASCII 小写，用于整体包含检查；
    * ``lower_prefix``: 小写形式加末尾分隔符，用于按段边界比较。
    """

    __slots__ = ("joined", "prefix", "lower", "lower_prefix")


_root_cache = {}


def _root_info(root):
    info = _root_cache.get(root)
    if info is None:
        segments = [
            seg for seg in root.replace("\\", "/").split("/") if seg and seg != "."
        ]
        joined = "/" + "/".join(segments)
        lower = _ascii_lower(joined)
        info = _RootInfo()
        info.joined = joined
        info.prefix = joined if joined != "/" else ""
        info.lower = lower
        info.lower_prefix = lower if lower.endswith("/") else lower + "/"
        _root_cache[root] = info
    return info


def normalize(root, request,
              max_segment_len=DEFAULT_MAX_SEGMENT_LEN,
              max_path_len=DEFAULT_MAX_PATH_LEN):
    """把 ``request``（相对路径）规范化到 ``root`` 下面。

    成功返回 ``Result(ok=True, path=<绝对路径>)``；
    越界或非法返回 ``Result(ok=False, code=..., segment_index=..., detail=...)``。
    同一份输入跑两遍结果逐字节一样。
    """
    if request:
        first = request[0]
        if first == "/" or first == "\\":
            # 以分隔符开头（含「只有分隔符」与 UNC 的 \\ 前缀）
            return _error(ABSOLUTE_PATH, 0, "prefix=" + request[:2])
        if (
            len(request) >= 2
            and request[1] == ":"
            and ("A" <= first <= "Z" or "a" <= first <= "z")
        ):
            # 盘符前缀，形如 C:
            return _error(ABSOLUTE_PATH, 0, "prefix=" + request[:2])

    info = _root_info(root)

    # 单遍扫描：边切段边校验边折叠，stack 是已经确认的段
    stack = []
    segment_index = 0
    start = 0
    length = len(request)
    i = 0
    while i <= length:
        if i == length or request[i] == "/" or request[i] == "\\":
            segment_index += 1
            segment = request[start:i]
            start = i + 1
            if not segment or segment == ".":
                # 空段（连续分隔符）与当前目录，直接丢弃
                pass
            elif segment == "..":
                if stack:
                    stack.pop()
                else:
                    # 已经在根上再往上弹，越界
                    return _error(ESCAPE, segment_index, "segment=..")
            else:
                if len(segment) > max_segment_len:
                    return _error(
                        TOO_LONG, segment_index,
                        "segment_len=%d" % len(segment),
                    )
                bad = None
                for ch in segment:
                    if ch in _BAD_CHARS:
                        bad = ch
                        break
                if bad is not None:
                    if bad < " ":
                        detail = "char=0x%02x" % ord(bad)
                    else:
                        detail = "char=" + bad
                    return _error(BAD_CHARS, segment_index, detail)
                dot = segment.find(".")
                stem = segment if dot < 0 else segment[:dot]
                if _ascii_lower(stem) in _RESERVED_NAMES:
                    return _error(RESERVED_NAME, segment_index, "name=" + segment)
                stack.append(segment)
        i += 1

    if stack:
        path = info.prefix + "/" + "/".join(stack)
    else:
        # 空请求或折叠后回到根：结果就是根目录本身
        path = info.joined

    if len(path) > max_path_len:
        return _error(TOO_LONG, 0, "path_len=%d" % len(path))

    # 硬约束：拼出来的绝对路径必须落在根目录里面（按段边界、不区分大小写）
    lowered = _ascii_lower(path)
    if lowered != info.lower and not lowered.startswith(info.lower_prefix):
        return _error(ESCAPE, 0, "path_outside_root")

    return Result(True, path=path)


def format_line(case_id, result):
    """按 README 的输出格式把一条用例的结果格式化成一行（不含换行）。"""
    if result.ok:
        return "%s,ok,%s" % (case_id, result.path)
    return "%s,error,%s,%d,%s" % (
        case_id,
        result.code,
        result.segment_index,
        result.detail,
    )
