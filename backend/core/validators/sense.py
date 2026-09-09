"""义项校验（校验 2）。落实 PRD 约束一。

约束一：释义不由模型生成。模型只输出 sense_id，释义文本由系统按编号回填。
模型返回的 sense_id 若不在该词的义项集合内，视为结构校验失败，走有限重试；
重试仍失败时降级为「未能判定义项」，展示全部义项，
**不得由模型补写一条释义**。

本模块不得 import 任何模型客户端。
"""

from __future__ import annotations

from ..models import CardDraft, Code, ValidationContext, ValidationResult

__all__ = ["validate_sense"]


def validate_sense(draft: CardDraft, ctx: ValidationContext) -> ValidationResult:
    """校验 2：sense_id 必须存在于该词的义项集合中。

    这道校验是约束一的执行机制。它不检查释义内容对不对
    （那是评测集的事），只检查编号是不是我们词库里真实存在的。
    一旦模型编造编号，这里必须拦下来。
    """
    sid = (draft.sense_id or "").strip()

    if not sid:
        return ValidationResult.fail(Code.SENSE_INVALID, "sense_id 为空")

    valid = ctx.word_entry.sense_ids()
    if sid not in valid:
        preview = ", ".join(sorted(valid)[:5])
        more = "…" if len(valid) > 5 else ""
        return ValidationResult.fail(
            Code.SENSE_INVALID,
            f"sense_id {sid!r} 不在 {ctx.word_entry.lemma!r} 的义项集合内"
            f"（合法值 {len(valid)} 个: {preview}{more}）",
        )

    return ValidationResult.ok()
