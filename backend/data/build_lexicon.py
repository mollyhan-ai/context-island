#!/usr/bin/env python3
"""将 WordNet 展平为运行时只读的 SQLite 词库。

NLTK 仅在构建阶段使用。每次构建都先生成一个新数据库，再原子替换目标文件；
相同 WordNet 数据与别名配置会产生相同的逻辑数据，不会追加重复行。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "lexicon.db"
DEFAULT_DEV_NLTK_DATA = HERE.parents[2] / "measurement" / "artifacts" / "nltk_data"

# 产品领域里常见、但 WordNet 未单列为 lemma 的缩写。
# 每一条必须人工审核，并且只能引用真实 WordNet sense_id；释义仍从 WordNet 回填。
CURATED_ALIASES: dict[str, dict[str, object]] = {
    "eval": {
        "canonical_lemma": "evaluation",
        "sense_ids": ("evaluation.n.01",),
        "reason": "AI/ML 产品语境中 evaluation 的常用缩写",
    },
}


SCHEMA = """
PRAGMA page_size = 4096;
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE word (
  lemma           TEXT PRIMARY KEY,
  canonical_lemma TEXT NOT NULL,
  source          TEXT NOT NULL,
  freq_bnc        INTEGER,
  freq_coca       INTEGER,
  level           TEXT,
  zh_gloss        TEXT
);

CREATE TABLE sense (
  sense_id   TEXT PRIMARY KEY,
  pos        TEXT NOT NULL,
  definition TEXT NOT NULL,
  examples   TEXT
);

CREATE TABLE word_sense (
  lemma      TEXT NOT NULL REFERENCES word(lemma),
  sense_id   TEXT NOT NULL REFERENCES sense(sense_id),
  sense_rank INTEGER NOT NULL,
  PRIMARY KEY (lemma, sense_id)
);

CREATE TABLE curated_alias (
  lemma           TEXT PRIMARY KEY REFERENCES word(lemma),
  canonical_lemma TEXT NOT NULL,
  reason          TEXT NOT NULL,
  sense_ids       TEXT NOT NULL
);

CREATE INDEX idx_word_sense_lemma ON word_sense(lemma);
"""


def _normalise_wordnet_lemma(value: str) -> str:
    return value.replace("_", " ").casefold()


def _source_fingerprint(nltk_data: Path) -> str:
    """记录实际构建源，不把机器绝对路径写入数据库。"""
    corpus = nltk_data / "corpora" / "wordnet"
    digest = hashlib.sha256()
    for name in ("data.noun", "data.verb", "data.adj", "data.adv", "index.sense"):
        path = corpus / name
        if not path.is_file():
            continue
        digest.update(name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int((value or "").strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _level_from_ranks(bnc: int | None, coca: int | None) -> str | None:
    ranks = [rank for rank in (bnc, coca) if rank is not None]
    if not ranks:
        return None
    easiest_rank = min(ranks)
    if easiest_rank <= 2000:
        return "A2"
    if easiest_rank <= 4000:
        return "B1"
    if easiest_rank <= 8000:
        return "B2"
    return None


def _load_ecdict(
    path: Path,
    wanted: set[str],
) -> dict[str, tuple[int | None, int | None, str | None]]:
    """流式读取 ECDICT，只保留当前 WordNet 词库会使用的字段。"""
    rows: dict[str, tuple[int | None, int | None, str | None]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"word", "translation", "bnc", "frq"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise RuntimeError(f"ECDICT CSV 缺少字段: {sorted(required)}")
        for item in reader:
            lemma = (item.get("word") or "").strip().casefold()
            if lemma not in wanted:
                continue
            raw_translation = (item.get("translation") or "").strip()
            # ECDICT CSV 用字面量 `\\n` 分隔词性或来源说明；入库时恢复成
            # 真正换行，前端可直接按行展示。
            translation = raw_translation.replace("\\n", "\n") or None
            rows[lemma] = (
                _positive_int(item.get("bnc")),
                _positive_int(item.get("frq")),
                translation,
            )
    return rows


def build(output: Path, nltk_data: Path, ecdict_csv: Path | None = None) -> dict[str, object]:
    try:
        import nltk
        from nltk.corpus import wordnet as wn
    except ImportError as exc:
        raise RuntimeError(
            "构建词库需要 NLTK；运行服务不需要。请用已安装 NLTK 的构建环境执行。"
        ) from exc

    nltk.data.path[:] = [str(nltk_data.resolve())]
    try:
        wn.ensure_loaded()
    except LookupError as exc:
        raise RuntimeError(f"指定目录中没有可用 WordNet 语料: {nltk_data}") from exc

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)

    sense_rows: dict[str, tuple[str, str, str]] = {}
    links: dict[str, list[tuple[str, int]]] = defaultdict(list)

    try:
        for synset in sorted(wn.all_synsets(), key=lambda item: item.name()):
            sense_id = synset.name()
            pos = synset.pos()
            if pos == "s":
                pos = "adj"
            else:
                pos = {"n": "n", "v": "v", "a": "adj", "r": "adv"}.get(pos, pos)
            sense_rows[sense_id] = (
                pos,
                synset.definition(),
                json.dumps(list(synset.examples()), ensure_ascii=False, separators=(",", ":")),
            )
            for lemma_obj in synset.lemmas():
                lemma = _normalise_wordnet_lemma(lemma_obj.name())
                if " " in lemma:
                    continue  # 当前产品输入契约是一个英文单词。
                links[lemma].append((sense_id, int(lemma_obj.count())))

        for alias, spec in sorted(CURATED_ALIASES.items()):
            canonical = str(spec["canonical_lemma"])
            ids = tuple(str(value) for value in spec["sense_ids"])
            missing = [sense_id for sense_id in ids if sense_id not in sense_rows]
            if missing:
                raise RuntimeError(f"别名 {alias!r} 引用了不存在的 WordNet 义项: {missing}")
            if canonical not in links:
                raise RuntimeError(f"别名 {alias!r} 的 canonical lemma 不存在: {canonical!r}")
            links[alias] = [(sense_id, len(ids) - index) for index, sense_id in enumerate(ids)]

        ecdict_rows: dict[str, tuple[int | None, int | None, str | None]] = {}
        if ecdict_csv is not None:
            ecdict_csv = ecdict_csv.resolve()
            if not ecdict_csv.is_file():
                raise RuntimeError(f"ECDICT CSV 不存在: {ecdict_csv}")
            wanted = set(links)
            wanted.update(
                str(spec["canonical_lemma"]) for spec in CURATED_ALIASES.values()
            )
            ecdict_rows = _load_ecdict(ecdict_csv, wanted)

        conn = sqlite3.connect(temp_path)
        try:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN")
            metadata = [
                ("schema_version", "1"),
                ("source", "WordNet via NLTK" + (" + ECDICT" if ecdict_csv else "")),
                ("source_fingerprint_sha256", _source_fingerprint(nltk_data)),
                ("curated_aliases", json.dumps(CURATED_ALIASES, ensure_ascii=False, sort_keys=True)),
            ]
            if ecdict_csv is not None:
                metadata.append(("ecdict_fingerprint_sha256", _file_fingerprint(ecdict_csv)))
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                metadata,
            )
            conn.executemany(
                "INSERT INTO sense(sense_id, pos, definition, examples) VALUES (?, ?, ?, ?)",
                (
                    (sense_id, values[0], values[1], values[2])
                    for sense_id, values in sorted(sense_rows.items())
                ),
            )

            word_rows = []
            link_rows = []
            for lemma, candidates in sorted(links.items()):
                alias_spec = CURATED_ALIASES.get(lemma)
                canonical = str(alias_spec["canonical_lemma"]) if alias_spec else lemma
                source = "curated_alias" if alias_spec else "wordnet"
                # 人工别名沿用 canonical lemma 的整词释义，避免 `eval` 这类
                # 字符串在词典中命中与当前别名义项无关的编程解释。
                ecdict = ecdict_rows.get(canonical if alias_spec else lemma)
                bnc, coca, zh_gloss = ecdict if ecdict else (None, None, None)
                level = _level_from_ranks(bnc, coca)
                word_rows.append((lemma, canonical, source, bnc, coca, level, zh_gloss))
                # WordNet 个别 synset 会含多个规范化后同名的 Lemma；同一词与
                # 同一 sense 只能保留一条关联，计数取较大值。
                best_count: dict[str, int] = {}
                for sense_id, count in candidates:
                    best_count[sense_id] = max(count, best_count.get(sense_id, 0))
                ranked = sorted(best_count.items(), key=lambda value: (-value[1], value[0]))
                link_rows.extend(
                    (lemma, sense_id, rank)
                    for rank, (sense_id, _count) in enumerate(ranked, start=1)
                )

            conn.executemany(
                """
                INSERT INTO word(
                  lemma, canonical_lemma, source, freq_bnc, freq_coca, level, zh_gloss
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                word_rows,
            )
            conn.executemany(
                "INSERT INTO word_sense(lemma, sense_id, sense_rank) VALUES (?, ?, ?)",
                link_rows,
            )
            conn.executemany(
                """
                INSERT INTO curated_alias(lemma, canonical_lemma, reason, sense_ids)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        alias,
                        str(spec["canonical_lemma"]),
                        str(spec["reason"]),
                        json.dumps(list(spec["sense_ids"]), ensure_ascii=False),
                    )
                    for alias, spec in sorted(CURATED_ALIASES.items())
                ),
            )
            conn.commit()
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()

        os.replace(temp_path, output)
        os.chmod(output, 0o644)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    words_with_chinese = sum(
        1
        for lemma in links
        if (ecdict_rows.get(
            str(CURATED_ALIASES[lemma]["canonical_lemma"])
            if lemma in CURATED_ALIASES
            else lemma
        ) or (None, None, None))[2]
    )
    return {
        "output": str(output),
        "words": len(links),
        "senses": len(sense_rows),
        "word_senses": sum(len({sense_id for sense_id, _count in values}) for values in links.values()),
        "curated_aliases": len(CURATED_ALIASES),
        "words_with_chinese": words_with_chinese,
        "chinese_coverage_percent": round(words_with_chinese / len(links) * 100, 2),
        "source_fingerprint_sha256": _source_fingerprint(nltk_data),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--nltk-data",
        type=Path,
        default=Path(os.environ.get("NLTK_DATA", DEFAULT_DEV_NLTK_DATA)),
    )
    parser.add_argument(
        "--ecdict-csv",
        type=Path,
        help="可选 ECDICT CSV；导入整词中文参考、BNC 与 COCA 词频秩",
    )
    args = parser.parse_args(argv)
    try:
        result = build(args.output, args.nltk_data, args.ecdict_csv)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
