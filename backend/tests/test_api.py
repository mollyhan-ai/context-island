"""最小 API 与生成编排的离线测试。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fixtures import good_draft  # noqa: F401  注入 backend 到 sys.path
from core.lexicon import Lexicon
from core.models import Code
from server import (
    CARD_DRAFT_JSON_SCHEMA,
    CardService,
    Config,
    OpenAICompatibleClient,
    ServiceError,
    dispatch_get,
    dispatch_post,
)


GOOD = json.dumps(
    {
        "sentence": "The team ran an eval before launch.",
        "sense_id": "evaluation.n.01",
        "translation": "团队在发布前进行了一次评估。",
        "segments": [
            {"text": "The team", "layer1": "S", "is_target": False},
            {"text": " ran", "layer1": "V", "is_target": False},
            {"text": " an ", "layer1": "O", "is_target": False},
            {"text": "eval", "layer1": "O", "is_target": True},
            {
                "text": " before launch.",
                "layer1": "A",
                "layer2": "PH-PREP",
                "is_target": False,
            },
        ],
        "ambiguity": None,
    },
    ensure_ascii=False,
)


class MockLLM:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("mock 响应已耗尽")
        return self.responses.pop(0)


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _limit: int) -> bytes:
        return self.raw


def make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE word (
              lemma TEXT PRIMARY KEY,
              canonical_lemma TEXT NOT NULL,
              source TEXT NOT NULL,
              freq_bnc INTEGER,
              freq_coca INTEGER,
              level TEXT,
              zh_gloss TEXT
            );
            CREATE TABLE sense (
              sense_id TEXT PRIMARY KEY,
              pos TEXT NOT NULL,
              definition TEXT NOT NULL,
              examples TEXT
            );
            CREATE TABLE word_sense (
              lemma TEXT NOT NULL,
              sense_id TEXT NOT NULL,
              sense_rank INTEGER NOT NULL,
              PRIMARY KEY (lemma, sense_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO word VALUES (?, ?, ?, NULL, NULL, NULL, NULL)",
            ("eval", "evaluation", "curated_alias"),
        )
        conn.execute(
            "INSERT INTO sense VALUES (?, ?, ?, ?)",
            (
                "evaluation.n.01",
                "n",
                "act of ascertaining or fixing the value or worth of",
                "[]",
            ),
        )
        conn.execute(
            "INSERT INTO word_sense VALUES (?, ?, ?)",
            ("eval", "evaluation.n.01", 1),
        )


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "lexicon.db"
        make_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, *, configured: bool = True) -> Config:
        return Config(
            llm_api_key="test-key" if configured else "",
            llm_base_url="https://example.invalid/v1" if configured else "",
            llm_model="test-model" if configured else "",
            lexicon_db_path=self.db_path,
            # 内容解析与校验的用例需要验证最多三次修正生成。
            max_attempts=3,
        )

    def service(self, llm: MockLLM, *, configured: bool = True) -> CardService:
        return CardService(
            self.config(configured=configured),
            lexicon=Lexicon(self.db_path),
            llm=llm,
        )

    def test_eval_alias_is_auditable_and_wordnet_anchored(self):
        payload, status = self.service(MockLLM(GOOD)).get_word("EVAL")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        word = payload["word"]
        self.assertEqual(word["lemma"], "eval")
        self.assertEqual(word["canonical_lemma"], "evaluation")
        self.assertEqual(word["source"], "curated_alias")
        self.assertEqual(word["senses"][0]["sense_id"], "evaluation.n.01")

    def test_card_success_uses_dictionary_sense_and_system_pos(self):
        payload, status = self.service(MockLLM(GOOD)).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["card"]["sense"]["definition"],
                         "act of ascertaining or fixing the value or worth of")
        target = next(item for item in payload["card"]["segments"] if item["is_target"])
        self.assertEqual(target["pos"], "n")
        self.assertEqual(target["layer1_label"], "宾语")
        self.assertIsNone(target["layer2_label"])

    def test_repeated_initial_search_uses_process_cache(self):
        llm = MockLLM(GOOD)
        service = self.service(llm)
        request = {"word": "eval", "realm": "产品经理", "level": "B2"}

        first, first_status = service.generate_card(request)
        second, second_status = service.generate_card(request)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["attempts"], 0)
        self.assertEqual(second["card"], first["card"])
        self.assertEqual(len(llm.calls), 1)

    def test_generation_rate_limit_stops_extra_model_calls(self):
        llm = MockLLM(GOOD, GOOD)
        config = self.config()
        config = Config(
            **{
                **config.__dict__,
                "generation_rate_limit_requests": 1,
                "generation_rate_limit_window_seconds": 3600,
            }
        )
        service = CardService(config, lexicon=Lexicon(self.db_path), llm=llm)

        first, first_status = service.generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"},
            client_id="visitor-1",
        )
        second, second_status = service.generate_card(
            {"word": "eval", "realm": "工程师", "level": "B2"},
            client_id="visitor-1",
        )

        self.assertEqual(first_status, 200)
        self.assertTrue(first["ok"])
        self.assertEqual(second_status, 429)
        self.assertEqual(second["code"], "RATE_LIMITED")
        self.assertGreater(second["retry_after_seconds"], 0)
        self.assertEqual(len(llm.calls), 1)

    def test_invalid_sense_retries_with_failure_feedback(self):
        bad = json.loads(GOOD)
        bad["sense_id"] = "eval.n.99"
        llm = MockLLM(json.dumps(bad), GOOD)
        payload, status = self.service(llm).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["attempts"], 2)
        self.assertIn("SENSE_INVALID", llm.calls[1])

    def test_again_rejects_duplicate_sentence_and_requests_a_new_one(self):
        alternate = json.loads(GOOD)
        alternate["sentence"] = "The team completed an eval after launch."
        alternate["segments"] = [
            {"text": "The team", "layer1": "S", "is_target": False},
            {"text": " completed", "layer1": "V", "is_target": False},
            {"text": " an ", "layer1": "O", "is_target": False},
            {"text": "eval", "layer1": "O", "is_target": True},
            {
                "text": " after launch.",
                "layer1": "A",
                "layer2": "PH-PREP",
                "is_target": False,
            },
        ]
        llm = MockLLM(GOOD, json.dumps(alternate, ensure_ascii=False))
        payload, status = self.service(llm).generate_card(
            {
                "word": "eval",
                "realm": "产品经理",
                "level": "B2",
                "previous_sentence": "The team ran an eval before launch.",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(payload["attempt_log"][0]["code"], Code.DUPLICATE_SENTENCE)
        self.assertIn("不得原样重复", llm.calls[0])
        self.assertIn("DUPLICATE_SENTENCE", llm.calls[1])
        self.assertEqual(payload["card"]["sentence"], alternate["sentence"])

    def test_target_segment_must_contain_exact_input_word(self):
        bad = json.loads(GOOD)
        bad["sentence"] = "The team ran an assessment before launch."
        bad["segments"][3]["text"] = "assessment"
        llm = MockLLM(json.dumps(bad), GOOD)
        payload, status = self.service(llm).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(payload["attempt_log"][0]["code"], "TARGET_INVALID")
        self.assertIn("TARGET_INVALID", llm.calls[1])

    def test_parse_failures_stop_after_three_attempts(self):
        llm = MockLLM("not json", "still not json", "no json")
        payload, status = self.service(llm).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["code"], "VALIDATION_EXHAUSTED")
        self.assertEqual(payload["attempts"], 3)
        self.assertEqual(len(llm.calls), 3)

    def test_missing_word_never_calls_model(self):
        llm = MockLLM(GOOD)
        payload, status = self.service(llm).generate_card(
            {"word": "missing", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "WORD_NOT_FOUND")
        self.assertEqual(payload["attempts"], 0)
        self.assertEqual(llm.calls, [])

    def test_real_client_without_configuration_is_explicit(self):
        service = CardService(self.config(configured=False), lexicon=Lexicon(self.db_path))
        payload, status = service.generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "MODEL_NOT_CONFIGURED")
        self.assertEqual(payload["attempts"], 0)

    def test_health_stays_live_when_model_is_not_configured(self):
        service = CardService(self.config(configured=False), lexicon=Lexicon(self.db_path))

        payload, status = dispatch_get(service, "/health")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["checks"]["lexicon"]["ok"])
        self.assertFalse(payload["checks"]["model"]["ok"])

    def test_transport_timeout_is_not_retried(self):
        llm = mock.Mock(
            side_effect=[
                ServiceError(Code.MODEL_TIMEOUT, "模型请求超时"),
                GOOD,
            ]
        )
        payload, status = self.service(llm).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["code"], Code.MODEL_TIMEOUT)
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(llm.call_count, 1)

    def test_retryable_rate_limit_waits_then_retries(self):
        llm = mock.Mock(
            side_effect=[
                ServiceError(
                    Code.MODEL_ERROR,
                    "模型服务返回 HTTP 429",
                    retryable=True,
                    retry_after_seconds=2.0,
                ),
                GOOD,
            ]
        )
        with mock.patch("server.time.sleep") as sleep:
            payload, status = self.service(llm).generate_card(
                {"word": "eval", "realm": "产品经理", "level": "B2"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["attempts"], 2)
        sleep.assert_called_once_with(2.0)
        self.assertNotIn("MODEL_ERROR", llm.call_args_list[1].args[0])

    def test_non_retryable_transport_error_stops_immediately(self):
        llm = mock.Mock(
            side_effect=[
                ServiceError(Code.MODEL_ERROR, "模型服务返回 HTTP 429"),
                GOOD,
            ]
        )
        payload, status = self.service(llm).generate_card(
            {"word": "eval", "realm": "产品经理", "level": "B2"}
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["attempts"], 1)
        self.assertEqual(llm.call_count, 1)

    def test_http_routes(self):
        service = self.service(MockLLM(GOOD))
        body, status = dispatch_get(service, "/api/word/eval")
        self.assertEqual(status, 200)
        self.assertEqual(body["word"]["source"], "curated_alias")

        body, status = dispatch_post(
            service,
            "/api/card",
            {"word": "eval", "realm": "产品经理", "level": "B2"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["card"]["word"], "eval")

        body, status = dispatch_get(service, "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "NOT_FOUND")


class OpenAICompatibleClientTestCase(unittest.TestCase):
    def config(self, **overrides: object) -> Config:
        values: dict[str, object] = {
            "llm_api_key": "test-key",
            "llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "llm_model": "glm-4.7-flash",
            "lexicon_db_path": Path("unused.db"),
        }
        values.update(overrides)
        return Config(**values)

    @staticmethod
    def response(content: str = '{"ok":true}', finish_reason: str = "stop"):
        return FakeHTTPResponse(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": content},
                    }
                ]
            }
        )

    def capture_payload(self, config: Config) -> tuple[dict[str, object], str]:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(request.data)
            return self.response()

        with mock.patch("server.urllib.request.urlopen", side_effect=fake_urlopen):
            result = OpenAICompatibleClient(config)("prompt")
        self.assertEqual(result, '{"ok":true}')
        return captured["body"], str(captured["url"])

    def test_json_object_is_default_and_base_url_is_completed(self):
        payload, url = self.capture_payload(self.config())
        self.assertEqual(url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_tokens"], 2048)

    def test_full_chat_completions_url_is_not_duplicated(self):
        _, url = self.capture_payload(
            self.config(
                llm_base_url=(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions"
                )
            )
        )
        self.assertEqual(url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")

    def test_text_mode_omits_response_format_for_older_providers(self):
        payload, _ = self.capture_payload(
            self.config(llm_response_format="text", llm_max_tokens=1536)
        )
        self.assertNotIn("response_format", payload)
        self.assertEqual(payload["max_tokens"], 1536)

    def test_json_schema_mode_sends_complete_card_schema(self):
        payload, _ = self.capture_payload(
            self.config(llm_response_format="json_schema")
        )
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(
            response_format["json_schema"]["schema"], CARD_DRAFT_JSON_SCHEMA
        )

    def test_provider_extra_body_is_placed_at_top_level(self):
        payload, _ = self.capture_payload(
            self.config(llm_extra_body={"thinking": {"type": "disabled"}})
        )
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_provider_extra_body_cannot_override_core_fields(self):
        with self.assertRaisesRegex(ValueError, "response_format"):
            OpenAICompatibleClient(
                self.config(llm_extra_body={"response_format": {"type": "text"}})
            )

    def test_legacy_json_mode_still_controls_direct_config(self):
        payload, _ = self.capture_payload(self.config(json_mode=False))
        self.assertNotIn("response_format", payload)

    def test_from_env_parses_zhipu_compatibility_options(self):
        env = {
            "LLM_API_KEY": "",
            "LLM_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
            "LLM_MODEL": "glm-4.7-flash",
            "LLM_RESPONSE_FORMAT": "json_object",
            "LLM_MAX_TOKENS": "2048",
            "LLM_EXTRA_BODY_JSON": '{"thinking":{"type":"disabled"}}',
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.llm_response_format, "json_object")
        self.assertEqual(config.llm_max_tokens, 2048)
        self.assertEqual(config.llm_extra_body, {"thinking": {"type": "disabled"}})

    def test_invalid_env_extra_body_fails_before_any_request(self):
        with mock.patch.dict(
            os.environ,
            {"LLM_EXTRA_BODY_JSON": '{"messages":[]}'},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "messages"):
                Config.from_env()

    def test_safe_zhipu_defaults_use_one_long_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.llm_timeout_seconds, 120.0)
        self.assertEqual(config.max_attempts, 1)
        self.assertEqual(config.llm_max_tokens, 2048)

    @staticmethod
    def http_error(
        status: int,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            status,
            "upstream error",
            headers or {},
            io.BytesIO(body.encode("utf-8")),
        )

    def call_with_http_error(self, error: urllib.error.HTTPError) -> ServiceError:
        with mock.patch("server.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ServiceError) as caught:
                OpenAICompatibleClient(self.config())("prompt")
        return caught.exception

    def test_zhipu_1302_rate_limit_is_retryable(self):
        error = self.http_error(
            429,
            '{"error":{"code":"1302","message":"too many requests"}}',
        )
        service_error = self.call_with_http_error(error)
        self.assertTrue(service_error.retryable)
        self.assertEqual(service_error.detail, "模型服务返回 HTTP 429")

    def test_zhipu_1305_overload_is_retryable(self):
        error = self.http_error(
            429,
            '{"error":{"code":1305,"message":"busy"}}',
        )
        self.assertTrue(self.call_with_http_error(error).retryable)

    def test_other_429_is_not_retried_or_exposed(self):
        private_message = "private upstream account diagnostic"
        error = self.http_error(
            429,
            json.dumps({"error": {"code": "1113", "message": private_message}}),
        )
        service_error = self.call_with_http_error(error)
        self.assertFalse(service_error.retryable)
        self.assertNotIn(private_message, service_error.detail)

    def test_retry_after_makes_unknown_429_retryable_and_is_capped(self):
        error = self.http_error(
            429,
            '{"error":{"code":"unknown"}}',
            {"Retry-After": "60"},
        )
        service_error = self.call_with_http_error(error)
        self.assertTrue(service_error.retryable)
        self.assertEqual(service_error.retry_after_seconds, 8.0)

    def test_other_4xx_never_retries_even_with_zhipu_retry_code(self):
        error = self.http_error(
            400,
            '{"error":{"code":"1302","message":"bad request"}}',
        )
        self.assertFalse(self.call_with_http_error(error).retryable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
