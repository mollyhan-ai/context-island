"""语法标签体系（PRD 5.1 / 约束四）。

这里是标签集的唯一事实来源。Prompt 注入、输出校验、前端渲染都从这里取，
不允许在别处硬编码标签字符串。

约束四：模型不得使用本集合之外的术语。
"""

from __future__ import annotations

# ---------- 第一层：句子主干成分 ----------

LAYER1: dict[str, str] = {
    "S": "主语",
    "V": "谓语",
    "O": "宾语",
    "P": "表语",
    "C": "补语",
    "A": "状语",
    "Attr": "定语",
}

# ---------- 第二层：从句与短语类型 ----------

LAYER2: dict[str, str] = {
    "CL-N": "名词性从句",
    "CL-REL": "定语从句",
    "CL-ADV": "状语从句",
    "PH-INF": "不定式短语",
    "PH-GER": "动名词短语",
    "PH-PART": "分词短语",
    "PH-PREP": "介词短语",
}

# ---------- 解析深度（约束五）----------

# 0 = 主句层，1 = 从句或短语内部。不允许 2 及以上。
MAX_DEPTH = 1


def is_valid_layer1(tag: str) -> bool:
    return tag in LAYER1


def is_valid_layer2(tag: str | None) -> bool:
    """layer2 允许为空。"""
    return tag is None or tag in LAYER2


def spec_for_prompt() -> str:
    """渲染成注入 Prompt 的标签说明。

    Prompt 里的标签集必须与校验用的标签集同源，否则会出现
    "模型按 A 集合输出、系统按 B 集合校验" 的静默错位。
    """
    lines = ["## 第一层：句子主干成分（每个片段必须且只能有一个）", ""]
    lines += [f"- `{k}` {v}" for k, v in LAYER1.items()]
    lines += ["", "## 第二层：从句与短语类型（可为空）", ""]
    lines += [f"- `{k}` {v}" for k, v in LAYER2.items()]
    lines += [
        "",
        "## 规则",
        "",
        "1. 不得使用以上集合之外的术语。",
        f"2. 解析深度最多两层（depth 取值 0 或 {MAX_DEPTH}），不递归到第三层。",
        "3. 目标词所在片段必须被单独标出。",
    ]
    return "\n".join(lines)
