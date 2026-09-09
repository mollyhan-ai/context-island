"""结构类校验（技术开发文档 8.1 的校验 1、3、4，外加约束五的深度校验）。

本模块不得 import 任何模型客户端、HTTP 库或 Web 框架。
tests/test_architecture.py 会强制断言这一点。
"""

from __future__ import annotations

import re
import unicodedata

from ..grammar_spec import MAX_DEPTH, is_valid_layer1, is_valid_layer2
from ..models import CardDraft, Code, ValidationContext, ValidationResult

__all__ = [
    "validate_segments_match",
    "validate_tags",
    "validate_target",
    "validate_depth",
]

_WS = re.compile(r"\s+")

# 撇号与引号的等价形式。模型常把 the feature's 写成 the feature’s。
_EQUIV = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ",
})


def _normalize(s: str) -> str:
    """规范化用于比较的文本。

    做：去掉全部空白、统一撇号/引号/连字符的等价形式、Unicode NFKC。
    不做：不忽略大小写、不忽略标点。

    为什么不忽略大小写和标点：约束一的实质是「模型不得改写原句」。
    放宽这两项等于给改写开口子——把 The 写成 the、把句号吞掉，
    都是对原句的改动，必须拦下来。

    为什么去掉全部空白而不是合并空白：片段拼接时用什么分隔符
    （空格还是直接相连）取决于标点归属，把空白整个抽掉可以让
    「标点单独成段」和「标点附着在词后」两种切法都通过，
    而字符序列本身仍然被严格比对。
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_EQUIV)
    return _WS.sub("", s)


def validate_segments_match(
    draft: CardDraft, ctx: ValidationContext | None = None
) -> ValidationResult:
    """校验 1：segments 拼接后必须与 sentence 完全一致。

    这是四道校验里最重要的一道——它保证模型没有在标注过程中
    偷偷改写、增删句子内容。
    """
    if not draft.segments:
        return ValidationResult.fail(Code.SEG_MISMATCH, "segments 为空")

    joined = _normalize("".join(seg.text for seg in draft.segments))
    origin = _normalize(draft.sentence)

    if joined != origin:
        return ValidationResult.fail(
            Code.SEG_MISMATCH,
            f"拼接结果与原句不一致\n  原句: {origin!r}\n  拼接: {joined!r}",
        )
    return ValidationResult.ok()


def validate_tags(
    draft: CardDraft, ctx: ValidationContext | None = None
) -> ValidationResult:
    """校验 3：layer1 必须在第一层标签集内；layer2 为空或在第二层标签集内。"""
    for i, seg in enumerate(draft.segments):
        if not is_valid_layer1(seg.layer1):
            return ValidationResult.fail(
                Code.TAG_INVALID, f"segments[{i}] 的 layer1 非法: {seg.layer1!r}"
            )
        if not is_valid_layer2(seg.layer2):
            return ValidationResult.fail(
                Code.TAG_INVALID, f"segments[{i}] 的 layer2 非法: {seg.layer2!r}"
            )
    return ValidationResult.ok()


def validate_target(
    draft: CardDraft, ctx: ValidationContext | None = None
) -> ValidationResult:
    """校验 4：有且仅有一个片段的 is_target 为真。"""
    hits = [i for i, seg in enumerate(draft.segments) if seg.is_target]
    if len(hits) == 0:
        return ValidationResult.fail(Code.TARGET_INVALID, "没有片段被标为目标词")
    if len(hits) > 1:
        return ValidationResult.fail(
            Code.TARGET_INVALID, f"有 {len(hits)} 个片段被标为目标词: {hits}"
        )
    return ValidationResult.ok()


def validate_depth(
    draft: CardDraft, ctx: ValidationContext | None = None
) -> ValidationResult:
    """约束五：解析深度限两层，depth 只能是 0 或 1。"""
    for i, seg in enumerate(draft.segments):
        if seg.depth < 0 or seg.depth > MAX_DEPTH:
            return ValidationResult.fail(
                Code.DEPTH_INVALID,
                f"segments[{i}] 的 depth={seg.depth} 超出允许范围 0..{MAX_DEPTH}",
            )
    return ValidationResult.ok()
