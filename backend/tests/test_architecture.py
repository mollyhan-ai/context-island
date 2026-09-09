"""架构约束测试。

技术适配声明 8.3：validators/ 目录下任何模块不得 import 模型客户端。

这条测试本身就是约束的执行机制。没有它，约束只是文档里的一句话，
几个月后某次「顺手」的改动就会把模型调用引进校验层，
而双层验收会在无人察觉的情况下退化成一层。
"""

from __future__ import annotations

import ast
import os
import unittest

import fixtures  # noqa: F401  注入 sys.path

VALIDATORS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core", "validators",
)

# 禁止出现在校验层的依赖
FORBIDDEN_PREFIXES = (
    "openai", "anthropic", "httpx", "requests", "urllib", "aiohttp",
    "fastapi", "starlette", "flask", "django",
    "sqlite3", "sqlalchemy",
    "nltk", "spacy",
)


def _imported_modules(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 是相对导入，属于本项目内部，不算外部依赖
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


class TestValidatorPurity(unittest.TestCase):
    def test_no_forbidden_imports(self):
        offenders: list[str] = []

        for name in sorted(os.listdir(VALIDATORS_DIR)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(VALIDATORS_DIR, name)
            for mod in _imported_modules(path):
                if mod in FORBIDDEN_PREFIXES:
                    offenders.append(f"{name} 导入了 {mod}")

        self.assertEqual(
            offenders, [],
            "校验层必须保持零外部依赖，否则无法脱离模型独立运行:\n  "
            + "\n  ".join(offenders),
        )

    def test_validators_dir_is_not_empty(self):
        """防止上面那条测试因为目录空了而假绿。"""
        py = [n for n in os.listdir(VALIDATORS_DIR)
              if n.endswith(".py") and n != "__init__.py"]
        self.assertGreaterEqual(len(py), 3, f"校验模块过少: {py}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
