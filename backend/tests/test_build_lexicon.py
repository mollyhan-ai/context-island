"""词库构建中的 ECDICT 字段导入测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from data.build_lexicon import _level_from_ranks, _load_ecdict


class EcdictImportTestCase(unittest.TestCase):
    def test_stream_import_keeps_only_requested_basic_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ecdict.csv"
            path.write_text(
                "word,translation,bnc,frq,oxford,collins\n"
                'evaluation,"n. 评估, 评价\\n[计] 求值",2732,2354,1,5\n'
                'ignored,"n. 忽略",1,1,1,5\n',
                encoding="utf-8",
            )
            rows = _load_ecdict(path, {"evaluation"})

        self.assertEqual(rows, {"evaluation": (2732, 2354, "n. 评估, 评价\n[计] 求值")})

    def test_level_uses_the_more_frequent_available_rank(self):
        self.assertEqual(_level_from_ranks(5000, 1800), "A2")
        self.assertEqual(_level_from_ranks(3000, None), "B1")
        self.assertEqual(_level_from_ranks(None, 7000), "B2")
        self.assertIsNone(_level_from_ranks(9000, 12000))


if __name__ == "__main__":
    unittest.main()
