"""数据模型。

刻意只用标准库，不引入 Pydantic 或任何 Web 框架的类型。
理由：本模块被 validators 依赖，而 validators 必须能脱离一切外部依赖
独立运行（技术适配声明 8.3）。接口层可以再包一层 Pydantic 做请求响应，
但校验核心不应该被那一层污染。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ==================== 词库侧 ====================


@dataclass(frozen=True)
class Sense:
    """一个义项。sense_id 即 WordNet synset 名称（PRD 5.4.1）。"""

    sense_id: str
    pos: str  # n / v / adj / adv
    definition: str  # 英文释义
    examples: tuple[str, ...] = ()
    sense_rank: int | None = None  # 1 最常用


@dataclass(frozen=True)
class WordEntry:
    """词库中的一个词。"""

    lemma: str
    senses: tuple[Sense, ...]
    zh_gloss: str | None = None  # 整词中文参考，不对应具体义项
    freq_bnc: int | None = None
    freq_coca: int | None = None

    def sense_ids(self) -> frozenset[str]:
        return frozenset(s.sense_id for s in self.senses)

    def find(self, sense_id: str) -> Sense | None:
        for s in self.senses:
            if s.sense_id == sense_id:
                return s
        return None


# ==================== 模型输出侧 ====================


@dataclass
class Segment:
    """句子的一个片段。

    pos 由 NLP 层在校验后填入，模型输出时必须为 None（约束八）。
    depth: 0 主句层，1 从句或短语内部（约束五）。
    """

    text: str
    layer1: str
    layer2: str | None = None
    is_target: bool = False
    depth: int = 0
    note: str | None = None
    pos: str | None = None


@dataclass
class CardDraft:
    """模型输出的直接映射，未经回填。

    校验只面对这个结构，不接触任何回填后的数据。
    """

    sentence: str
    sense_id: str
    translation: str
    segments: list[Segment] = field(default_factory=list)
    ambiguity: Any | None = None


# ==================== 校验侧 ====================


@dataclass(frozen=True)
class ValidationContext:
    """校验所需的全部外部信息。

    刻意做成可手工构造的普通数据，这样单元测试不需要词库、
    不需要模型、不需要网络。
    """

    word_entry: WordEntry
    target_lemma: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    code: str | None = None
    detail: str | None = None

    @staticmethod
    def ok() -> "ValidationResult":
        return ValidationResult(True)

    @staticmethod
    def fail(code: str, detail: str = "") -> "ValidationResult":
        return ValidationResult(False, code, detail)


# ==================== 错误码（技术开发文档第五节）====================


class Code:
    WORD_NOT_FOUND = "WORD_NOT_FOUND"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_ERROR = "MODEL_ERROR"
    PARSE_FAILED = "PARSE_FAILED"
    SEG_MISMATCH = "SEG_MISMATCH"
    SENSE_INVALID = "SENSE_INVALID"
    TAG_INVALID = "TAG_INVALID"
    TARGET_INVALID = "TARGET_INVALID"
    DEPTH_INVALID = "DEPTH_INVALID"
    DUPLICATE_SENTENCE = "DUPLICATE_SENTENCE"
    VALIDATION_EXHAUSTED = "VALIDATION_EXHAUSTED"
