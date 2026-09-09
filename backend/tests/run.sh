#!/usr/bin/env bash
# 第一层验收：mock 自动化测试。不需要 API Key、词库或网络。
cd "$(dirname "$0")" && python3 -m unittest discover -s . -p "test_*.py" -v
