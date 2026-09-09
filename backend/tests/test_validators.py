"""校验引擎单元测试。

不需要 API Key、不需要词库文件、不需要网络。
这是手册 A 类底线要求的「第一层 mock 自动化测试」。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from fixtures import CTX, SENTENCE, good_draft
from core.models import Code, Segment
from core.validators.pipeline import run_all
from core.validators.sense import validate_sense
from core.validators.structure import (
    validate_depth,
    validate_segments_match,
    validate_tags,
    validate_target,
)


class TestHappyPath(unittest.TestCase):
    def test_good_draft_passes_all(self):
        self.assertTrue(run_all(good_draft(), CTX).passed)


class TestSegmentsMatch(unittest.TestCase):
    """校验 1：模型不得改写原句。"""

    def test_missing_word_is_caught(self):
        d = good_draft()
        d.segments[0] = Segment("The placement", "S")  # 漏掉 new
        r = validate_segments_match(d, CTX)
        self.assertFalse(r.passed)
        self.assertEqual(r.code, Code.SEG_MISMATCH)

    def test_rewritten_word_is_caught(self):
        d = good_draft()
        d.segments[1] = Segment("increased", "V")  # 把 doubled 改写了
        self.assertFalse(validate_segments_match(d, CTX).passed)

    def test_dropped_punctuation_is_caught(self):
        d = good_draft()
        d.segments.pop()  # 吞掉句号
        self.assertFalse(validate_segments_match(d, CTX).passed)

    def test_case_change_is_caught(self):
        """大小写变化必须拦下来——它是对原句的改动。"""
        d = good_draft()
        d.segments[0] = Segment("the new placement", "S")
        self.assertFalse(validate_segments_match(d, CTX).passed)

    def test_extra_whitespace_is_tolerated(self):
        """空白差异不算改写。"""
        d = good_draft()
        d.segments[0] = Segment("  The   new  placement ", "S")
        self.assertTrue(validate_segments_match(d, CTX).passed)

    def test_curly_apostrophe_is_tolerated(self):
        """模型常把 feature's 写成 feature’s，视为等价。"""
        d = good_draft()
        d.segments[2] = Segment("the feature\u2019s", "Attr")
        self.assertTrue(validate_segments_match(d, CTX).passed)

    def test_punctuation_attached_is_tolerated(self):
        """句号附着在最后一个词上，也应通过。"""
        d = good_draft()
        d.segments.pop()
        d.segments[-1] = Segment("within a week.", "A", layer2="PH-PREP")
        self.assertTrue(validate_segments_match(d, CTX).passed)

    def test_empty_segments_is_caught(self):
        d = good_draft()
        d.segments = []
        self.assertFalse(validate_segments_match(d, CTX).passed)

    def test_reordered_segments_is_caught(self):
        """打乱顺序会改变字符序列，必须拦下。"""
        d = good_draft()
        d.segments[0], d.segments[1] = d.segments[1], d.segments[0]
        self.assertFalse(validate_segments_match(d, CTX).passed)


class TestSense(unittest.TestCase):
    """校验 2：约束一的执行机制。"""

    def test_fabricated_sense_id_is_caught(self):
        d = good_draft()
        d.sense_id = "exposure.n.99"  # 词库里不存在
        r = validate_sense(d, CTX)
        self.assertFalse(r.passed)
        self.assertEqual(r.code, Code.SENSE_INVALID)

    def test_empty_sense_id_is_caught(self):
        d = good_draft()
        d.sense_id = ""
        self.assertFalse(validate_sense(d, CTX).passed)

    def test_other_word_sense_id_is_caught(self):
        d = good_draft()
        d.sense_id = "placement.n.01"
        self.assertFalse(validate_sense(d, CTX).passed)

    def test_all_valid_ids_pass(self):
        for s in CTX.word_entry.senses:
            d = good_draft()
            d.sense_id = s.sense_id
            self.assertTrue(validate_sense(d, CTX).passed, s.sense_id)


class TestTags(unittest.TestCase):
    """校验 3：约束四。"""

    def test_invented_layer1_is_caught(self):
        d = good_draft()
        d.segments[0] = Segment("The new placement", "主词")
        r = validate_tags(d, CTX)
        self.assertFalse(r.passed)
        self.assertEqual(r.code, Code.TAG_INVALID)

    def test_layer1_value_used_in_layer2_is_caught(self):
        d = good_draft()
        d.segments[4] = Segment("within a week", "A", layer2="A")
        self.assertFalse(validate_tags(d, CTX).passed)

    def test_empty_layer2_is_allowed(self):
        d = good_draft()
        d.segments[4] = Segment("within a week", "A", layer2=None)
        self.assertTrue(validate_tags(d, CTX).passed)

    def test_lowercase_tag_is_caught(self):
        """标签大小写敏感，attr 不等于 Attr。"""
        d = good_draft()
        d.segments[2] = Segment("the feature's", "attr")
        self.assertFalse(validate_tags(d, CTX).passed)


class TestTarget(unittest.TestCase):
    """校验 4。"""

    def test_no_target_is_caught(self):
        d = good_draft()
        d.segments[3] = Segment("exposure", "O", is_target=False)
        r = validate_target(d, CTX)
        self.assertFalse(r.passed)
        self.assertEqual(r.code, Code.TARGET_INVALID)

    def test_two_targets_is_caught(self):
        d = good_draft()
        d.segments[1] = replace(d.segments[1], is_target=True)
        self.assertFalse(validate_target(d, CTX).passed)


class TestDepth(unittest.TestCase):
    """约束五：解析深度限两层。"""

    def test_depth_zero_and_one_pass(self):
        d = good_draft()
        d.segments[4] = Segment("within a week", "A", layer2="PH-PREP", depth=1)
        self.assertTrue(validate_depth(d, CTX).passed)

    def test_depth_two_is_caught(self):
        d = good_draft()
        d.segments[4] = Segment("within a week", "A", depth=2)
        r = validate_depth(d, CTX)
        self.assertFalse(r.passed)
        self.assertEqual(r.code, Code.DEPTH_INVALID)


class TestPipelineOrder(unittest.TestCase):
    def test_rewrite_reported_before_tag_error(self):
        """句子被改写时，应先报 SEG_MISMATCH，而不是标签问题。

        理由：句子本身都不对了，标注是否合法没有意义。
        """
        d = good_draft()
        d.segments[0] = Segment("The placement", "非法标签")
        self.assertEqual(run_all(d, CTX).code, Code.SEG_MISMATCH)

    def test_sentence_unchanged_by_validation(self):
        """校验必须无副作用。"""
        d = good_draft()
        run_all(d, CTX)
        self.assertEqual(d.sentence, SENTENCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
