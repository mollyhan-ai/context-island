"""解析器测试。覆盖手册第九节要求的格式容错场景。"""

from __future__ import annotations

import json
import unittest

from fixtures import good_draft  # noqa: F401  确保 sys.path 已注入
from core.parser import ParseError, parse_card


VALID = {
    "sentence": "The fund reduced its exposure to emerging markets.",
    "sense_id": "exposure.n.03",
    "translation": "该基金降低了对新兴市场的敞口。",
    "segments": [
        {"text": "The fund", "layer1": "S", "layer2": None, "is_target": False},
        {"text": "reduced", "layer1": "V", "layer2": None, "is_target": False},
        {"text": "its", "layer1": "Attr", "layer2": None, "is_target": False},
        {"text": "exposure", "layer1": "O", "layer2": None, "is_target": True},
        {"text": "to emerging markets.", "layer1": "Attr", "layer2": "PH-PREP",
         "is_target": False},
    ],
}
RAW = json.dumps(VALID, ensure_ascii=False)


class TestTolerance(unittest.TestCase):
    def test_plain_json(self):
        d = parse_card(RAW)
        self.assertEqual(d.sense_id, "exposure.n.03")
        self.assertEqual(len(d.segments), 5)

    def test_fenced_json(self):
        self.assertEqual(parse_card(f"```json\n{RAW}\n```").sense_id, "exposure.n.03")

    def test_fenced_without_language(self):
        self.assertEqual(parse_card(f"```\n{RAW}\n```").sense_id, "exposure.n.03")

    def test_leading_and_trailing_prose(self):
        wrapped = f"好的，这是结果：\n{RAW}\n希望对你有帮助。"
        self.assertEqual(parse_card(wrapped).sense_id, "exposure.n.03")

    def test_extra_unknown_fields_ignored(self):
        data = dict(VALID, confidence=0.9, model_note="仅供参考")
        self.assertEqual(len(parse_card(json.dumps(data)).segments), 5)

    def test_field_order_irrelevant(self):
        reordered = {k: VALID[k] for k in reversed(list(VALID))}
        self.assertEqual(parse_card(json.dumps(reordered)).sense_id, "exposure.n.03")

    def test_bool_as_string(self):
        data = json.loads(RAW)
        data["segments"][3]["is_target"] = "true"
        self.assertTrue(parse_card(json.dumps(data)).segments[3].is_target)

    def test_bool_as_int(self):
        data = json.loads(RAW)
        data["segments"][3]["is_target"] = 1
        self.assertTrue(parse_card(json.dumps(data)).segments[3].is_target)

    def test_layer2_empty_string_becomes_none(self):
        data = json.loads(RAW)
        data["segments"][0]["layer2"] = ""
        self.assertIsNone(parse_card(json.dumps(data)).segments[0].layer2)

    def test_layer2_null_string_becomes_none(self):
        data = json.loads(RAW)
        data["segments"][0]["layer2"] = "null"
        self.assertIsNone(parse_card(json.dumps(data)).segments[0].layer2)

    def test_braces_inside_string_do_not_break_extraction(self):
        data = json.loads(RAW)
        data["translation"] = "含有 {大括号} 的翻译"
        self.assertEqual(parse_card("说明：\n" + json.dumps(data, ensure_ascii=False))
                         .translation, "含有 {大括号} 的翻译")


class TestConstraintEight(unittest.TestCase):
    """约束八：模型不得标注词性，解析器必须丢弃。"""

    def test_model_supplied_pos_is_discarded(self):
        data = json.loads(RAW)
        for seg in data["segments"]:
            seg["pos"] = "noun"
        parsed = parse_card(json.dumps(data))
        self.assertTrue(all(s.pos is None for s in parsed.segments))


class TestFailures(unittest.TestCase):
    """不该容忍的情况必须明确失败，不猜、不修。"""

    def test_empty_input(self):
        with self.assertRaises(ParseError):
            parse_card("")

    def test_no_json_at_all(self):
        with self.assertRaises(ParseError):
            parse_card("抱歉，我无法完成这个请求。")

    def test_broken_json(self):
        with self.assertRaises(ParseError):
            parse_card('{"sentence": "abc", "segments": [')

    def test_missing_required_field(self):
        data = json.loads(RAW)
        del data["sense_id"]
        with self.assertRaises(ParseError):
            parse_card(json.dumps(data))

    def test_segments_not_a_list(self):
        data = json.loads(RAW)
        data["segments"] = "S V O"
        with self.assertRaises(ParseError):
            parse_card(json.dumps(data))

    def test_segment_missing_layer1(self):
        data = json.loads(RAW)
        del data["segments"][0]["layer1"]
        with self.assertRaises(ParseError):
            parse_card(json.dumps(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
