# backend

## 当前状态

阶段一的**校验引擎、解析器和最小生成 API 已完成离线测试**（当前 74 个用例；
历史变异检查不计作本轮新增验证）。

这部分是刻意先做的：技术适配声明 8.3 要求校验层能脱离模型、词库和网络独立运行，
所以它在没有任何外部依赖的环境里就能写完并验证。

## 跑测试

```bash
bash tests/run.sh
```

测试不需要 API Key、不需要外网。

## 已完成

| 模块 | 说明 |
| --- | --- |
| `core/grammar_spec.py` | 语法标签体系，PRD 5.1 的唯一事实来源。Prompt 注入与校验同源 |
| `core/models.py` | 数据模型，零外部依赖 |
| `core/parser.py` | 容错 JSON 解析器，覆盖手册第九节要求的格式容错 |
| `core/validators/structure.py` | 校验 1、3、4 + 约束五深度校验 |
| `core/validators/sense.py` | 校验 2，约束一的执行机制 |
| `core/validators/pipeline.py` | 校验管线 |
| `tests/test_architecture.py` | **强制断言校验层零外部依赖**，防止架构腐化 |
| `core/lexicon.py` | 标准库 SQLite 只读查询，向校验层提供 `WordEntry` |
| `data/build_lexicon.py` | 构建期合并 WordNet 与 ECDICT，支持可审计人工别名 |
| `server.py` | 标准库 HTTP 服务、OpenAI-compatible 模型调用与最多三次重试 |
| `tests/test_api.py` | API、重试、缺词不调用模型、`eval` 别名的离线 mock 测试 |

## 本地运行

仓库已包含构建好的 `data/lexicon.db`。需要重建时，在安装了 NLTK 且有
WordNet 语料的构建环境执行：

```bash
python3 data/build_lexicon.py \
  --nltk-data /path/to/nltk_data \
  --ecdict-csv /path/to/ecdict.csv
```

构建脚本每次从空的临时数据库生成，再原子替换正式文件，不会追加重复记录。
当前数据库包含 83,119 个单词、117,659 个 WordNet 义项。`eval` 是显式维护的
`curated_alias`，指向真实的 `evaluation.n.01`；API 会同时返回
`canonical_lemma=evaluation` 和 `source=curated_alias`，模型不生成它的释义。
当前数据库中 82,108 个词带有 ECDICT 整词中文释义，覆盖率 98.78%；该释义不与
WordNet 的单个义项强行对齐。WordNet 3.0 与 ECDICT 的许可原文分别保存在
`data/WORDNET_LICENSE.txt` 和 `data/ECDICT_LICENSE.txt`。

本地 `.env` 已预填阿里云百炼北京地域 `qwen3.8-flash` 的
OpenAI-compatible 地址与模型名，但密钥保持为空。填写 `LLM_API_KEY` 后启动：

```bash
bash run_dev.sh
```

默认监听 `http://127.0.0.1:8767`。接口为：

```text
GET  /health
GET  /api/word/{lemma}
POST /api/card     {"word":"eval","realm":"产品经理","level":"B2"}
```

成功响应为 `{ok:true, card, attempts, elapsed_ms, attempt_log}`。卡片中的释义由
WordNet 按校验后的 `sense_id` 回填；模型输出不存在的义项、错误标签、错误片段，
或目标词没有被单独标成目标片段时，可携带失败原因重新生成，最多三次。
词库缺词先返回 `WORD_NOT_FOUND`，不会调用模型。

模型未配置时，词库查询仍可使用；`POST /api/card` 会明确返回
`MODEL_NOT_CONFIGURED`，不会把预置内容或 mock 结果冒充真实生成。

### OpenAI-compatible 模型配置

模型地址与模型名完全由 `LLM_BASE_URL` 和 `LLM_MODEL` 控制。地址既可填写 API
基址，也可直接填写完整的 `/chat/completions` 地址，服务不会重复拼接路径。

`LLM_RESPONSE_FORMAT` 支持 `text`、`json_object` 和 `json_schema`。默认使用
`json_schema`，由 `qwen3.8-flash` 严格约束 CardDraft 的字段与类型；输出仍须经过本地
解析器和语义校验器。切换到只支持 `json_object` 的模型时，需要同步修改此配置。

各提供方的非标准顶层参数可以放入 `LLM_EXTRA_BODY_JSON`。本原型对 Qwen 使用
`'{"enable_thinking":false}'`，减少这个简单结构化任务的等待时间。服务会拒绝用这个字段覆盖 `model`、`messages`、
`response_format`、`stream` 或 `max_tokens`，避免配置绕过核心约束。

Qwen 原型默认设置 `LLM_MAX_TOKENS=2048` 和 `LLM_TIMEOUT_SECONDS=60`。单张卡片
输出较短，2048 可以避免异常长输出。切换提供方时可按其文档调整或将 token 上限留空。

传输超时不会在同一次前端请求中自动重试。HTTP 429 只有在智谱业务码为 `1302`
（账户速率限制）、`1305`（平台过载），或响应包含 `Retry-After` 时才会按最多 8 秒的
等待重试；其他 4xx 立即停止。上游错误体最多读取 16 KiB，只在内存中提取业务码，
不会写入日志或返回前端。解析失败与内容校验失败仍可按 `LLM_MAX_ATTEMPTS` 修正重试。

| 提供方 | `LLM_BASE_URL` 示例 | 稳妥响应格式 |
| --- | --- | --- |
| 智谱 BigModel | `https://open.bigmodel.cn/api/paas/v4` | `json_object` |
| DeepSeek | `https://api.deepseek.com` | `json_object` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `json_schema`（Qwen3.8-Flash） |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `json_object`；确认模型支持后可用 `json_schema` |

模型名会随提供方上下线而变化，应从对应控制台复制当前可用的精确 ID。本原型只发送
目标词、所选领域、难度、WordNet 英文义项和固定提示词。

### 当前诚实边界

- 目标片段的 `pos` 由命中的 WordNet 义项确定性回填。
- 其他片段的 `pos` 目前为 `null`；阶段一的 NLTK 词性合并层仍未实现，因而还不
  能把当前 API 称为完整阶段一验收结果。
- 动态卡片的音标与真人录音尚未接入 API，返回 `phonetics: []`、`audio: null`。
- `zh_gloss` 来自 ECDICT 的整词中文释义；模型不会补写或修改中文词义。
- 难度规则、歧义检测和降级卡属于后续阶段，当前校验耗尽直接失败。

## 仍待做

- [ ] NLTK + WordNet 部署体积与冷启动实测
- [x] `data/build_lexicon.py` WordNet 词库构建脚本
- [x] 词库查询接口 `GET /api/word/{lemma}`
- [x] 模型调用与重试状态机 `POST /api/card`
- [x] 导入 ECDICT 的词级中文参考与词频字段
- [ ] NLTK 非目标片段词性标注与合并
- [ ] 动态音标与真人录音查询
- [ ] 最小验收界面（含调试区）
- [ ] 配置模型后运行真实模型冒烟测试 `tests/test_smoke_real.py`
- [ ] 义项粒度验证 + A2 可读性评估

## 一处技术偏离，需要你决定

技术适配声明写的是 Pydantic v2，但校验层目前用标准库 dataclass。

起因是开发环境无网络装不了 Pydantic，但结果反而更贴合架构原则——
**校验层本来就不该依赖 Web 框架的模型库**。接口层照常用 Pydantic 做请求响应，
校验层保持零依赖，两者用简单的转换函数衔接。

建议保持现状。如果你希望统一成 Pydantic，改动量不大，但会让
`test_architecture.py` 的禁止清单需要重新斟酌。

测试同理：目前用标准库 unittest，装了 pytest 后可以直接被 pytest 收集，无需改写。
