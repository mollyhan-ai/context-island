"""模型输出解析器。

手册第九节「格式约束三件套」要求解析器兼容多种等价格式。
本模块是纯函数，输入字符串，输出 CardDraft 或抛 ParseError。

明确不做的事：不尝试修复结构性损坏的 JSON。
解析失败就是 PARSE_FAILED，交给重试，不猜。
"""

from __future__ import annotations

import json
import re

from .models import CardDraft, Segment

__all__ = ["parse_card", "ParseError"]


class ParseError(ValueError):
    """解析失败。调用方据此走重试。"""


# 代码块包裹：```json ... ``` 或 ``` ... ```
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)

# 全角引号、弯引号统一为半角，模型偶发用中文标点写 JSON
_QUOTE_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "：": ":", "，": ",",
})


def _strip_fence(raw: str) -> str:
    m = _FENCE.search(raw)
    return m.group(1) if m else raw


def _extract_object(text: str) -> str:
    """从可能带解释文字的输出里，截出最外层的 JSON 对象。

    用括号配对而不是正则，因为字符串里可能含大括号。
    """
    start = text.find("{")
    if start == -1:
        raise ParseError("输出中没有找到 JSON 对象")

    depth = 0
    in_str = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    raise ParseError("JSON 对象未闭合")


def _as_bool(v) -> bool:
    """模型有时把布尔写成字符串或 0/1。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _as_int(v, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return default


def _clean_optional(v) -> str | None:
    """空字符串、null、"null"、"none" 一律视为无值。"""
    if v is None:
        return None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.lower() in ("null", "none", "n/a", "-"):
        return None
    return s


def parse_card(raw: str) -> CardDraft:
    """把模型的原始输出解析成 CardDraft。

    容忍：代码块包裹、前后带解释文字、字段顺序不同、多余字段、
    中文标点、布尔写成字符串。
    不容忍：JSON 结构损坏、必填字段缺失。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ParseError("输出为空")

    text = _strip_fence(raw)
    text = _extract_object(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 只在这里做一次标点兜底，避免过早改动合法内容
        try:
            data = json.loads(text.translate(_QUOTE_MAP))
        except json.JSONDecodeError as e:
            raise ParseError(f"JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ParseError("顶层不是对象")

    for key in ("sentence", "sense_id", "segments"):
        if key not in data:
            raise ParseError(f"缺少必填字段: {key}")

    if not isinstance(data["segments"], list):
        raise ParseError("segments 不是数组")

    segments: list[Segment] = []
    for i, item in enumerate(data["segments"]):
        if not isinstance(item, dict):
            raise ParseError(f"segments[{i}] 不是对象")
        if "text" not in item or "layer1" not in item:
            raise ParseError(f"segments[{i}] 缺少 text 或 layer1")
        segments.append(
            Segment(
                text=str(item["text"]),
                layer1=str(item["layer1"]).strip(),
                layer2=_clean_optional(item.get("layer2")),
                is_target=_as_bool(item.get("is_target")),
                depth=_as_int(item.get("depth"), 0),
                note=_clean_optional(item.get("note")),
                pos=None,  # 约束八：模型不得标注词性，一律丢弃
            )
        )

    return CardDraft(
        sentence=str(data["sentence"]),
        sense_id=str(data["sense_id"]).strip(),
        translation=str(data.get("translation", "")),
        segments=segments,
        ambiguity=data.get("ambiguity"),
    )
