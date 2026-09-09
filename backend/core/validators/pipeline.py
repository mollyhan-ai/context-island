"""校验管线。按固定顺序跑完各道校验，返回第一个失败。

顺序是有意的：先确认句子没被改写（校验 1），再看标注是否合法。
如果句子本身就被改了，后面的标注校验没有意义。
"""

from __future__ import annotations

from ..models import CardDraft, ValidationContext, ValidationResult
from .sense import validate_sense
from .structure import (
    validate_depth,
    validate_segments_match,
    validate_tags,
    validate_target,
)

__all__ = ["run_all", "PIPELINE"]

# (名称, 校验函数)。阶段二会在这里追加难度校验与歧义检测。
PIPELINE = [
    ("segments_match", validate_segments_match),
    ("sense", validate_sense),
    ("tags", validate_tags),
    ("target", validate_target),
    ("depth", validate_depth),
]


def run_all(draft: CardDraft, ctx: ValidationContext) -> ValidationResult:
    """跑完整条管线，返回第一个失败；全部通过返回 ok。"""
    for _name, fn in PIPELINE:
        result = fn(draft, ctx)
        if not result.passed:
            return result
    return ValidationResult.ok()
