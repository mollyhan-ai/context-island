"""只读 SQLite 词库访问层。

运行时只依赖 Python 标准库。数据库中的释义由构建脚本从 WordNet 写入；
模型不会参与释义生成。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import Sense, WordEntry

__all__ = ["Lexicon", "LexiconRecord", "LexiconError", "normalize_lemma"]


class LexiconError(RuntimeError):
    """词库不存在、损坏或结构不兼容。"""


@dataclass(frozen=True)
class LexiconRecord:
    """词库查询结果及其可审计来源。"""

    entry: WordEntry
    canonical_lemma: str
    source: str


def normalize_lemma(value: str) -> str:
    """将用户输入规范化为词库键；不猜测别名或拼写。"""
    return value.strip().casefold()


class Lexicon:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise LexiconError(f"词库不存在: {self.path}")
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:
            raise LexiconError(f"词库无法打开: {exc}") from exc

    def check(self) -> tuple[bool, str]:
        """做一次轻量只读检查，供 /health 使用。"""
        try:
            with self._connect() as conn:
                conn.execute("SELECT lemma FROM word LIMIT 1").fetchone()
                conn.execute("SELECT sense_id FROM sense LIMIT 1").fetchone()
            return True, "ready"
        except (LexiconError, sqlite3.Error) as exc:
            return False, str(exc)

    def lookup(self, lemma: str) -> LexiconRecord | None:
        key = normalize_lemma(lemma)
        if not key:
            return None

        try:
            with self._connect() as conn:
                word = conn.execute(
                    """
                    SELECT lemma, canonical_lemma, source, zh_gloss, freq_bnc, freq_coca
                    FROM word
                    WHERE lemma = ?
                    """,
                    (key,),
                ).fetchone()
                if word is None:
                    return None

                rows = conn.execute(
                    """
                    SELECT s.sense_id, s.pos, s.definition, s.examples,
                           ws.sense_rank
                    FROM word_sense AS ws
                    JOIN sense AS s ON s.sense_id = ws.sense_id
                    WHERE ws.lemma = ?
                    ORDER BY ws.sense_rank, s.sense_id
                    """,
                    (key,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise LexiconError(f"词库查询失败: {exc}") from exc

        senses: list[Sense] = []
        for row in rows:
            try:
                decoded = json.loads(row["examples"] or "[]")
            except (TypeError, json.JSONDecodeError):
                decoded = []
            examples = tuple(str(item) for item in decoded if isinstance(item, str))
            senses.append(
                Sense(
                    sense_id=row["sense_id"],
                    pos=row["pos"],
                    definition=row["definition"],
                    examples=examples,
                    sense_rank=row["sense_rank"],
                )
            )

        if not senses:
            raise LexiconError(f"词库条目 {key!r} 没有关联义项")

        entry = WordEntry(
            lemma=word["lemma"],
            senses=tuple(senses),
            zh_gloss=word["zh_gloss"],
            freq_bnc=word["freq_bnc"],
            freq_coca=word["freq_coca"],
        )
        return LexiconRecord(
            entry=entry,
            canonical_lemma=word["canonical_lemma"] or word["lemma"],
            source=word["source"] or "wordnet",
        )
