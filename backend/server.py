#!/usr/bin/env python3
"""语境岛阶段一最小 HTTP 服务。

启动后提供：
  GET  /health
  GET  /api/word/{lemma}
  POST /api/card

运行时只依赖 Python 标准库与 backend/core。模型通过通用的
OpenAI-compatible /chat/completions 接口调用。
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from core.grammar_spec import LAYER1, LAYER2, spec_for_prompt
from core.lexicon import Lexicon, LexiconError, LexiconRecord, normalize_lemma
from core.models import CardDraft, Code, ValidationContext
from core.parser import ParseError, parse_card
from core.validators.pipeline import run_all


ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "core" / "prompts" / "card_v1.md"
WORD_RE = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")
LEVELS = frozenset(("A2", "B1", "B2"))
MAX_BODY_BYTES = 64 * 1024
MAX_ERROR_BODY_BYTES = 16 * 1024
MAX_RETRY_DELAY_SECONDS = 8.0
ZHIPU_RETRYABLE_CODES = frozenset(("1302", "1305"))
RESPONSE_FORMATS = frozenset(("text", "json_object", "json_schema"))
PROTECTED_EXTRA_BODY_FIELDS = frozenset(
    ("model", "messages", "response_format", "stream", "max_tokens")
)

# `json_schema` 仅供明确支持 Structured Outputs 的模型按需启用。
# 默认仍使用四家 OpenAI-compatible Chat Completions 接口的共同交集
# `json_object`，并继续由本地 parser + validators 做最终判定。
CARD_DRAFT_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "sentence": {"type": "string"},
        "sense_id": {"type": "string"},
        "translation": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "layer1": {"type": "string", "enum": list(LAYER1)},
                    "layer2": {
                        "anyOf": [
                            {"type": "string", "enum": list(LAYER2)},
                            {"type": "null"},
                        ]
                    },
                    "is_target": {"type": "boolean"},
                    "depth": {"type": "integer", "enum": [0, 1]},
                    "note": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                },
                "required": [
                    "text",
                    "layer1",
                    "layer2",
                    "is_target",
                    "depth",
                    "note",
                ],
                "additionalProperties": False,
            },
        },
        "ambiguity": {"type": "null"},
    },
    "required": [
        "sentence",
        "sense_id",
        "translation",
        "segments",
        "ambiguity",
    ],
    "additionalProperties": False,
}


class ServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Config:
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    lexicon_db_path: Path
    llm_timeout_seconds: float = 120.0
    max_attempts: int = 1
    llm_response_format: str = "json_object"
    llm_max_tokens: int | None = 2048
    llm_extra_body: dict[str, object] = field(default_factory=dict)
    # 兼容旧的直接构造调用；环境变量请改用 LLM_RESPONSE_FORMAT。
    json_mode: bool | None = None
    host: str = "127.0.0.1"
    port: int = 8767
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8766",
        "http://localhost:8766",
    )
    generation_rate_limit_requests: int = 0
    generation_global_limit_requests: int = 0
    generation_rate_limit_window_seconds: int = 3600
    trust_proxy: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        raw_db = os.environ.get("LEXICON_DB_PATH", "data/lexicon.db")
        db_path = Path(raw_db)
        if not db_path.is_absolute():
            db_path = ROOT / db_path

        try:
            timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "120"))
        except ValueError:
            timeout = 120.0
        try:
            attempts = int(os.environ.get("LLM_MAX_ATTEMPTS", "1"))
        except ValueError:
            attempts = 1
        try:
            port = int(os.environ.get("PORT", "8767"))
        except ValueError:
            port = 8767

        raw_response_format = os.environ.get("LLM_RESPONSE_FORMAT", "").strip()
        if raw_response_format:
            response_format = raw_response_format.lower()
        else:
            legacy_json_mode = os.environ.get("LLM_JSON_MODE", "1").strip().lower()
            response_format = (
                "text" if legacy_json_mode in ("0", "false", "no") else "json_object"
            )
        if response_format not in RESPONSE_FORMATS:
            choices = ", ".join(sorted(RESPONSE_FORMATS))
            raise ValueError(f"LLM_RESPONSE_FORMAT 必须是 {choices} 之一")

        raw_max_tokens = os.environ.get("LLM_MAX_TOKENS", "2048").strip()
        if raw_max_tokens:
            try:
                max_tokens = int(raw_max_tokens)
            except ValueError as exc:
                raise ValueError("LLM_MAX_TOKENS 必须是正整数") from exc
            if max_tokens < 1:
                raise ValueError("LLM_MAX_TOKENS 必须是正整数")
        else:
            max_tokens = None

        raw_extra_body = os.environ.get("LLM_EXTRA_BODY_JSON", "").strip()
        if raw_extra_body:
            try:
                extra_body = json.loads(raw_extra_body)
            except json.JSONDecodeError as exc:
                raise ValueError("LLM_EXTRA_BODY_JSON 必须是有效 JSON 对象") from exc
            if not isinstance(extra_body, dict):
                raise ValueError("LLM_EXTRA_BODY_JSON 必须是 JSON 对象")
        else:
            extra_body = {}
        _check_extra_body(extra_body)

        origins = tuple(
            item.strip().rstrip("/")
            for item in os.environ.get(
                "ALLOWED_ORIGINS", "http://127.0.0.1:8766,http://localhost:8766"
            ).split(",")
            if item.strip()
        )

        def non_negative_int(name: str, default: int) -> int:
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                value = default
            return max(0, value)

        return cls(
            llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
            llm_base_url=os.environ.get("LLM_BASE_URL", "").strip(),
            llm_model=os.environ.get("LLM_MODEL", "").strip(),
            lexicon_db_path=db_path.resolve(),
            llm_timeout_seconds=max(1.0, min(timeout, 120.0)),
            max_attempts=max(1, min(attempts, 3)),
            llm_response_format=response_format,
            llm_max_tokens=max_tokens,
            llm_extra_body=extra_body,
            host=os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=max(1, min(port, 65535)),
            allowed_origins=origins,
            generation_rate_limit_requests=non_negative_int(
                "GENERATION_RATE_LIMIT_REQUESTS", 0
            ),
            generation_global_limit_requests=non_negative_int(
                "GENERATION_GLOBAL_LIMIT_REQUESTS", 0
            ),
            generation_rate_limit_window_seconds=max(
                1, non_negative_int("GENERATION_RATE_LIMIT_WINDOW_SECONDS", 3600)
            ),
            trust_proxy=os.environ.get("TRUST_PROXY", "0").strip().lower()
            in ("1", "true", "yes"),
        )

    @property
    def model_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def chat_completions_url(self) -> str:
        base = self.llm_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    @property
    def effective_response_format(self) -> str:
        if self.json_mode is not None:
            return "json_object" if self.json_mode else "text"
        return self.llm_response_format


def _check_extra_body(extra_body: dict[str, object]) -> None:
    protected = sorted(PROTECTED_EXTRA_BODY_FIELDS.intersection(extra_body))
    if protected:
        joined = ", ".join(protected)
        raise ValueError(
            f"LLM_EXTRA_BODY_JSON 不能覆盖核心字段：{joined}；请使用对应的专用配置"
        )


def _response_format_payload(response_format: str) -> dict[str, object] | None:
    if response_format == "text":
        # 对较旧的兼容接口，省略字段比显式发送 {type:text} 更稳妥。
        return None
    if response_format == "json_object":
        return {"type": "json_object"}
    if response_format == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "context_island_card_draft",
                "strict": True,
                "schema": CARD_DRAFT_JSON_SCHEMA,
            },
        }
    choices = ", ".join(sorted(RESPONSE_FORMATS))
    raise ValueError(f"llm_response_format 必须是 {choices} 之一")


def _provider_error_code(raw: bytes) -> str | None:
    """只提取用于重试判定的业务码，不保留或透传上游原始错误体。"""
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    code: object | None = None
    if isinstance(error, dict):
        code = error.get("code")
    if code is None:
        code = data.get("code")
    if not isinstance(code, (str, int)) or isinstance(code, bool):
        return None
    normalized = str(code).strip()
    return normalized or None


def _retry_after(headers: object) -> tuple[bool, float | None]:
    """返回 Retry-After 是否存在，以及可解析并截断后的等待秒数。"""
    try:
        raw = headers.get("Retry-After")  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return False, None
    if raw is None or not str(raw).strip():
        return False, None

    value = str(raw).strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return True, None
    return True, min(max(seconds, 0.0), MAX_RETRY_DELAY_SECONDS)


def _retry_delay(attempt: int, retry_after_seconds: float | None) -> float:
    if retry_after_seconds is None:
        retry_after_seconds = float(2 ** max(0, attempt - 1))
    return min(max(retry_after_seconds, 0.5), MAX_RETRY_DELAY_SECONDS)


class OpenAICompatibleClient:
    def __init__(self, config: Config):
        self.config = config
        _response_format_payload(config.effective_response_format)
        _check_extra_body(config.llm_extra_body)

    def __call__(self, prompt: str) -> str:
        if not self.config.model_configured:
            raise ServiceError("MODEL_NOT_CONFIGURED", "模型尚未配置")

        payload: dict[str, object] = {
            "model": self.config.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only the requested JSON object. Do not invent dictionary senses.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        response_format = _response_format_payload(
            self.config.effective_response_format
        )
        if response_format is not None:
            payload["response_format"] = response_format
        if self.config.llm_max_tokens is not None:
            payload["max_tokens"] = self.config.llm_max_tokens
        # 厂商扩展参数在原始 HTTP 请求中位于顶层，例如 thinking / enable_thinking。
        payload.update(self.config.llm_extra_body)

        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.llm_api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.llm_timeout_seconds
            ) as response:
                raw = response.read(MAX_BODY_BYTES * 16)
        except (TimeoutError, socket.timeout) as exc:
            raise ServiceError(Code.MODEL_TIMEOUT, "模型请求超时") from exc
        except urllib.error.HTTPError as exc:
            # 限长读取只为识别智谱业务码；不记录、不保留、不透传原始响应体。
            try:
                error_body = exc.read(MAX_ERROR_BODY_BYTES)
            except (OSError, ValueError):
                error_body = b""
            provider_code = _provider_error_code(error_body)
            has_retry_after, retry_after_seconds = _retry_after(exc.headers)
            retryable = exc.code >= 500 or (
                exc.code == HTTPStatus.TOO_MANY_REQUESTS
                and (
                    provider_code in ZHIPU_RETRYABLE_CODES
                    or has_retry_after
                )
            )
            raise ServiceError(
                Code.MODEL_ERROR,
                f"模型服务返回 HTTP {exc.code}",
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ServiceError(Code.MODEL_TIMEOUT, "模型请求超时") from exc
            raise ServiceError(Code.MODEL_ERROR, "无法连接模型服务") from exc

        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ServiceError(Code.MODEL_ERROR, "模型输出达到长度上限")
            content = choice["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                )
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content 为空")
            return content
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServiceError(Code.MODEL_ERROR, "模型响应结构不兼容") from exc


def _sense_payload(record: LexiconRecord) -> list[dict[str, object]]:
    return [asdict(sense) for sense in record.entry.senses]


def _word_payload(record: LexiconRecord) -> dict[str, object]:
    return {
        "lemma": record.entry.lemma,
        "canonical_lemma": record.canonical_lemma,
        "source": record.source,
        "senses": _sense_payload(record),
        "zh_gloss": record.entry.zh_gloss,
        "freq_bnc": record.entry.freq_bnc,
        "freq_coca": record.entry.freq_coca,
    }


def _render_prompt(
    template: str,
    record: LexiconRecord,
    realm: str,
    level: str,
    previous_failure: str | None,
    previous_sentence: str = "",
) -> str:
    senses = "\n".join(
        f"- {sense.sense_id} [{sense.pos}]: {sense.definition}"
        for sense in record.entry.senses
    )
    feedback = ""
    if previous_failure:
        feedback = (
            "# 上一次输出未通过\n"
            f"失败原因：{previous_failure}\n"
            "请修正这一点并重新输出完整 JSON；不要复述失败原因。"
        )
    values = {
        "{{word}}": record.entry.lemma,
        "{{realm}}": realm,
        "{{level}}": level,
        "{{sense_list}}": senses,
        "{{grammar_spec}}": spec_for_prompt(),
        "{{retry_feedback}}": feedback,
    }
    rendered = template
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if previous_sentence:
        rendered += (
            "\n\n# 换一个例句\n"
            "请生成与下面这句明显不同的新例句，不得原样重复：\n"
            f"{previous_sentence}"
        )
    return rendered


def _card_payload(
    record: LexiconRecord,
    draft: CardDraft,
    realm: str,
    level: str,
) -> dict[str, object]:
    selected = record.entry.find(draft.sense_id)
    if selected is None:  # run_all 已保证，保留防御分支。
        raise ServiceError(Code.SENSE_INVALID, "命中义项无法回填")

    segments = []
    for segment in draft.segments:
        item = asdict(segment)
        # 目标词词性来自已经校验过的 WordNet sense，不采信模型输出。
        item["pos"] = selected.pos if segment.is_target else None
        item["layer1_label"] = LAYER1[segment.layer1]
        item["layer2_label"] = LAYER2.get(segment.layer2) if segment.layer2 else None
        segments.append(item)

    return {
        "word": record.entry.lemma,
        "canonical_lemma": record.canonical_lemma,
        "source": record.source,
        "realm": realm,
        "level": level,
        "sentence": draft.sentence,
        "translation": draft.translation,
        "segments": segments,
        "sense_id": selected.sense_id,
        "sense": asdict(selected),
        "senses": _sense_payload(record),
        "zh_gloss": record.entry.zh_gloss,
        "audio": None,
        "phonetics": [],
        "ambiguity": draft.ambiguity,
    }


class GenerationLimiter:
    """限制真实模型生成频率；进程缓存命中不占用额度。"""

    def __init__(self, per_client: int, global_limit: int, window_seconds: int):
        self.per_client = max(0, per_client)
        self.global_limit = max(0, global_limit)
        self.window_seconds = max(1, window_seconds)
        self._global: deque[float] = deque()
        self._clients: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_id: str) -> tuple[bool, int]:
        if self.per_client == 0 and self.global_limit == 0:
            return True, 0

        now = time.monotonic()
        cutoff = now - self.window_seconds
        key = client_id or "anonymous"
        with self._lock:
            while self._global and self._global[0] <= cutoff:
                self._global.popleft()

            client = self._clients.setdefault(key, deque())
            while client and client[0] <= cutoff:
                client.popleft()

            waits: list[float] = []
            if self.global_limit and len(self._global) >= self.global_limit:
                waits.append(self._global[0] + self.window_seconds - now)
            if self.per_client and len(client) >= self.per_client:
                waits.append(client[0] + self.window_seconds - now)
            if waits:
                return False, max(1, int(max(waits) + 0.999))

            self._global.append(now)
            client.append(now)
            return True, 0


def _target_contains_word(draft: CardDraft, word: str) -> bool:
    """确认模型把精确输入词单独标成目标片段。

    这一步放在服务编排层，不改变既有校验器的公共签名。当前阶段要求例句
    使用输入词本身；以后若允许词形变化，应由确定性的词形层扩展此规则。
    """
    targets = [segment for segment in draft.segments if segment.is_target]
    if len(targets) != 1:
        return False
    return targets[0].text.strip().casefold() == word.casefold()


class CardService:
    def __init__(
        self,
        config: Config,
        lexicon: Lexicon | None = None,
        llm: Callable[[str], str] | None = None,
        prompt_template: str | None = None,
    ):
        self.config = config
        self.lexicon = lexicon or Lexicon(config.lexicon_db_path)
        self.llm = llm or OpenAICompatibleClient(config)
        self.prompt_template = prompt_template or PROMPT_PATH.read_text(encoding="utf-8")
        # 原型试用时同一词和领域经常被重复搜索。成功结果只缓存在当前
        # 服务进程内，避免每次都重新等待远程模型；“再来一句”会绕过缓存。
        self._card_cache: dict[tuple[str, str, str], dict[str, object]] = {}
        self._card_cache_lock = threading.Lock()
        self._generation_limiter = GenerationLimiter(
            config.generation_rate_limit_requests,
            config.generation_global_limit_requests,
            config.generation_rate_limit_window_seconds,
        )

    def health(self) -> tuple[dict[str, object], int]:
        lexicon_ok, lexicon_detail = self.lexicon.check()
        model_ok = self.config.model_configured
        payload = {
            "ok": lexicon_ok and model_ok,
            "checks": {
                "lexicon": {"ok": lexicon_ok, "detail": lexicon_detail},
                "model": {
                    "ok": model_ok,
                    "detail": "configured" if model_ok else "LLM_API_KEY、LLM_BASE_URL 或 LLM_MODEL 缺失",
                },
            },
        }
        return payload, HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE

    def get_word(self, lemma: str) -> tuple[dict[str, object], int]:
        try:
            record = self.lexicon.lookup(lemma)
        except LexiconError:
            return {"ok": False, "code": "LEXICON_ERROR", "detail": "词库暂不可用"}, HTTPStatus.SERVICE_UNAVAILABLE
        if record is None:
            return {
                "ok": False,
                "code": Code.WORD_NOT_FOUND,
                "detail": f"词库中暂无 {normalize_lemma(lemma)!r}",
            }, HTTPStatus.NOT_FOUND
        return {"ok": True, "word": _word_payload(record)}, HTTPStatus.OK

    def generate_card(
        self, data: object, client_id: str = "anonymous"
    ) -> tuple[dict[str, object], int]:
        if not isinstance(data, dict):
            return _bad_request("请求体必须是 JSON 对象")

        word = normalize_lemma(str(data.get("word", "")))
        realm = str(data.get("realm", "")).strip()
        level = str(data.get("level", "B2")).strip().upper()
        previous_sentence = str(data.get("previous_sentence", "")).strip()
        if not WORD_RE.fullmatch(word):
            return _bad_request("word 必须是单个英文单词，可包含连字符或撇号")
        if not realm or len(realm) > 64:
            return _bad_request("realm 不能为空且不能超过 64 个字符")
        if level not in LEVELS:
            return _bad_request("level 必须是 A2、B1 或 B2")
        if len(previous_sentence) > 500:
            return _bad_request("previous_sentence 不能超过 500 个字符")
        try:
            record = self.lexicon.lookup(word)
        except LexiconError:
            return {
                "ok": False,
                "code": "LEXICON_ERROR",
                "detail": "词库暂不可用",
                "attempts": 0,
            }, HTTPStatus.SERVICE_UNAVAILABLE
        if record is None:
            return {
                "ok": False,
                "code": Code.WORD_NOT_FOUND,
                "detail": f"词库中暂无 {word!r}，未调用模型生成释义",
                "attempts": 0,
            }, HTTPStatus.NOT_FOUND

        if not self.config.model_configured and isinstance(self.llm, OpenAICompatibleClient):
            return {
                "ok": False,
                "code": "MODEL_NOT_CONFIGURED",
                "detail": "模型尚未配置，无法生成真实卡片",
                "attempts": 0,
            }, HTTPStatus.SERVICE_UNAVAILABLE

        cache_key = (word, realm, level)
        if not previous_sentence:
            with self._card_cache_lock:
                cached_card = self._card_cache.get(cache_key)
            if cached_card is not None:
                return {
                    "ok": True,
                    "card": copy.deepcopy(cached_card),
                    "attempts": 0,
                    "elapsed_ms": 0.0,
                    "attempt_log": [],
                    "cached": True,
                }, HTTPStatus.OK

        allowed, retry_after = self._generation_limiter.allow(client_id)
        if not allowed:
            return {
                "ok": False,
                "code": "RATE_LIMITED",
                "detail": "公开体验请求较多，请稍后再试",
                "retry_after_seconds": retry_after,
                "attempts": 0,
            }, HTTPStatus.TOO_MANY_REQUESTS

        started = time.monotonic()
        previous_failure: str | None = None
        attempt_log: list[dict[str, object]] = []
        last_code = Code.MODEL_ERROR

        for attempt in range(1, self.config.max_attempts + 1):
            attempt_started = time.monotonic()
            prompt = _render_prompt(
                self.prompt_template,
                record,
                realm,
                level,
                previous_failure,
                previous_sentence,
            )
            try:
                raw = self.llm(prompt)
                draft = parse_card(raw)
                validation = run_all(
                    draft,
                    ValidationContext(word_entry=record.entry, target_lemma=word),
                )
                if not validation.passed:
                    last_code = validation.code or Code.MODEL_ERROR
                    previous_failure = f"{last_code}: {validation.detail or ''}".strip()
                    attempt_log.append(
                        _attempt_payload(attempt, last_code, validation.detail, attempt_started)
                    )
                    continue

                if previous_sentence and re.sub(r"\s+", " ", draft.sentence).strip().casefold() == re.sub(
                    r"\s+", " ", previous_sentence
                ).strip().casefold():
                    last_code = Code.DUPLICATE_SENTENCE
                    detail = "新例句与上一句相同"
                    previous_failure = f"{Code.DUPLICATE_SENTENCE}: {detail}"
                    attempt_log.append(
                        _attempt_payload(attempt, Code.DUPLICATE_SENTENCE, detail, attempt_started)
                    )
                    continue

                if not _target_contains_word(draft, word):
                    last_code = Code.TARGET_INVALID
                    detail = f"目标词 {word!r} 未被单独标为目标片段"
                    previous_failure = f"{Code.TARGET_INVALID}: {detail}"
                    attempt_log.append(
                        _attempt_payload(attempt, Code.TARGET_INVALID, detail, attempt_started)
                    )
                    continue

                elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                attempt_log.append(_attempt_payload(attempt, None, None, attempt_started))
                card = _card_payload(record, draft, realm, level)
                if not previous_sentence:
                    with self._card_cache_lock:
                        self._card_cache[cache_key] = copy.deepcopy(card)
                return {
                    "ok": True,
                    "card": card,
                    "attempts": attempt,
                    "elapsed_ms": elapsed_ms,
                    "attempt_log": attempt_log,
                    "cached": False,
                }, HTTPStatus.OK
            except ParseError as exc:
                last_code = Code.PARSE_FAILED
                previous_failure = f"{Code.PARSE_FAILED}: {exc}"
                attempt_log.append(
                    _attempt_payload(attempt, Code.PARSE_FAILED, str(exc), attempt_started)
                )
            except ServiceError as exc:
                last_code = exc.code
                attempt_log.append(
                    _attempt_payload(attempt, exc.code, exc.detail, attempt_started)
                )
                # 客户端超时并不保证上游已取消推理。立即重试可能与仍在途的
                # 首请求重叠并触发并发限流，因此超时在本次请求内直接结束。
                if exc.code == Code.MODEL_TIMEOUT:
                    break
                if exc.retryable and attempt < self.config.max_attempts:
                    time.sleep(_retry_delay(attempt, exc.retry_after_seconds))
                    # 传输错误不是模型内容错误，不把它写进下一次 Prompt。
                    previous_failure = None
                    continue
                break
            except Exception:
                last_code = Code.MODEL_ERROR
                attempt_log.append(
                    _attempt_payload(attempt, Code.MODEL_ERROR, "模型调用失败", attempt_started)
                )
                break

        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        only_model_failures = all(
            item["code"] in (Code.MODEL_TIMEOUT, Code.MODEL_ERROR, "MODEL_NOT_CONFIGURED")
            for item in attempt_log
        )
        final_code = last_code if only_model_failures else Code.VALIDATION_EXHAUSTED
        return {
            "ok": False,
            "code": final_code,
            "detail": (
                "模型调用失败"
                if only_model_failures
                else f"生成结果连续 {len(attempt_log)} 次未通过解析或校验"
            ),
            "attempts": len(attempt_log),
            "elapsed_ms": elapsed_ms,
            "attempt_log": attempt_log,
        }, HTTPStatus.BAD_GATEWAY


def _attempt_payload(
    attempt: int,
    code: str | None,
    detail: str | None,
    started: float,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "code": code,
        "detail": detail,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _bad_request(detail: str) -> tuple[dict[str, object], int]:
    return {"ok": False, "code": "INVALID_REQUEST", "detail": detail}, HTTPStatus.BAD_REQUEST


def dispatch_get(service: CardService, path: str) -> tuple[dict[str, object], int]:
    """纯路由函数，便于在禁止监听端口的环境中离线测试。"""
    parsed = urllib.parse.urlsplit(path)
    if parsed.path == "/health":
        return service.health()
    prefix = "/api/word/"
    if parsed.path.startswith(prefix):
        lemma = urllib.parse.unquote(parsed.path[len(prefix):])
        return service.get_word(lemma)
    return {"ok": False, "code": "NOT_FOUND", "detail": "接口不存在"}, HTTPStatus.NOT_FOUND


def dispatch_post(
    service: CardService, path: str, data: object, client_id: str = "anonymous"
) -> tuple[dict[str, object], int]:
    parsed = urllib.parse.urlsplit(path)
    if parsed.path == "/api/card":
        return service.generate_card(data, client_id=client_id)
    return {"ok": False, "code": "NOT_FOUND", "detail": "接口不存在"}, HTTPStatus.NOT_FOUND


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "SentenceScope/0.1"

    @property
    def service(self) -> CardService:
        return self.server.service  # type: ignore[attr-defined]

    def _cors_origin(self) -> str | None:
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and origin in self.service.config.allowed_origins:
            return origin
        return None

    def _send(self, payload: dict[str, object], status: int) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        payload, status = dispatch_get(self.service, self.path)
        self._send(payload, status)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/card":
            payload, status = dispatch_post(self.service, self.path, None)
            self._send(payload, status)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(*_bad_request("Content-Length 无效"))
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(*_bad_request("请求体为空或过大"))
            return
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(*_bad_request("请求体不是有效 JSON"))
            return
        client_id = self.client_address[0]
        if self.service.config.trust_proxy:
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
            if forwarded:
                client_id = forwarded
        payload, status = dispatch_post(self.service, self.path, data, client_id)
        self._send(payload, status)

    def log_message(self, fmt: str, *args: object) -> None:
        # 只记录方法、路径与状态；模型密钥、Prompt 和请求体从不进入日志。
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: CardService):
        self.service = service
        super().__init__(address, ApiHandler)


def main() -> int:
    config = Config.from_env()
    service = CardService(config)
    server = ApiServer((config.host, config.port), service)
    print(f"SentenceScope API: http://{config.host}:{config.port}")
    print(f"Lexicon: {config.lexicon_db_path}")
    print("Model: configured" if config.model_configured else "Model: not configured")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
