"""测试夹具。全部手工构造，不读词库、不调模型、不联网。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import CardDraft, Segment, Sense, ValidationContext, WordEntry  # noqa: E402


EXPOSURE = WordEntry(
    lemma="exposure",
    zh_gloss="n. 暴露；曝光；接触",
    freq_coca=1834,
    senses=(
        Sense("exposure.n.01", "n", "vulnerability to the elements", sense_rank=1),
        Sense("exposure.n.02", "n", "the act of subjecting someone to an influence", sense_rank=2),
        Sense("exposure.n.03", "n", "presentation to view in an open or public manner", sense_rank=3),
        Sense("exposure.n.04", "n", "the disclosure of something secret", sense_rank=4),
    ),
)

CTX = ValidationContext(word_entry=EXPOSURE, target_lemma="exposure")

SENTENCE = "The new placement doubled the feature's exposure within a week."


def good_draft() -> CardDraft:
    """一份四道校验全过的样本。"""
    return CardDraft(
        sentence=SENTENCE,
        sense_id="exposure.n.03",
        translation="新的位置让这个功能的曝光量在一周内翻了一倍。",
        segments=[
            Segment("The new placement", "S"),
            Segment("doubled", "V"),
            Segment("the feature's", "Attr"),
            Segment("exposure", "O", is_target=True),
            Segment("within a week", "A", layer2="PH-PREP"),
            Segment(".", "A"),
        ],
    )
