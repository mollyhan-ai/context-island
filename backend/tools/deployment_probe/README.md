# 第二节专用部署探针

只测固定输入的 NLTK、WordNet、词性模型与 HTTP 启动，不含业务接口、模型调用或词库构建。

## 本次结果

见 `docs/部署体积实测报告.md` 和 `docs/部署实测证据/`。veFaaS 冷启动仍待测；本地进程启动不等于云端实例启动。随交付提供的 Linux ZIP 是探针包，不是语境岛成品部署包。

## 重建包

使用 Python 3.12。在隔离虚拟环境中运行（需要联网；生成目录必须为新的目录）：

```bash
python3 -m venv work/probe-venv
work/probe-venv/bin/python backend/tools/deployment_probe/prepare.py --work work/probe-artifacts
```

脚本按固定版本安装 Linux x86_64 / CPython 3.12 wheels，固定 NLTK 数据仓库 commit 并校验语料 SHA-256。保留第三方许可文件。用 ZIP DEFLATE level 9 压缩，每文件时间固定；记录原始字节数、包 SHA-256、逐文件清单及 PyPI 下载报告。pip 包固定版本，具体 wheel 的下载 URL 和 hash 在 `pip-linux-report.json`，不是依赖下载缓存大小。

依赖体积使用 `pip --target --no-compile` 安装目录的文件字节总和，包含 dist-info 与许可，不包含 pip、虚拟环境解释器及 pyc。不删除第三方库内容。WordNet、词性模型只打入解压后的文件，不重复携带原始 ZIP。

## 本地参考测量

```bash
work/probe-venv/bin/python -m pip install -r backend/tools/deployment_probe/requirements.lock
work/probe-venv/bin/python -B backend/tools/deployment_probe/collect_local.py --data work/probe-artifacts/nltk_data --out work/local-results.json
```

每次启动独立进程，固定输入，执行真实 POS、WordNet 查询与词形还原，收到首个完整响应后停止进程。默认 10 次。语料搜索路径严格限定，不回落到用户目录。测量包括本地进程创建和轮询开销，未清除操作系统页缓存；P95 用 nearest-rank，10 次时即最大值。`--without-wordnet` 在单独进程中验证缺语料失败。

## Linux 与只读环境待验证

有 Docker 后可先做环境近似验证。以下只是复测方法，本次没有运行：

```bash
docker run --rm --platform linux/amd64 --read-only --tmpfs /tmp --network none \
  -v "$PWD/work/probe-artifacts/linux-probe:/probe:ro" -w /probe \
  -e PYTHONPATH=/probe/vendor -e PROBE_DATA=/probe/nltk_data \
  python:3.12-slim-bullseye python3 -B probe.py
```

镜像需事先拉取。该镜像近似官方操作系统和 Python 主次版本，仍不等于 veFaaS 运行时；应记录实际镜像 digest、Python patch、glibc 和实例资源。仿真环境的耗时不作为云端性能结论。

## veFaaS 复测

使用专门测试函数，选择 Native Python 3.12，上传 `linux-probe.zip`，启动命令 `bash run.sh`，端口 8000；脚本读取平台 `_FAAS_RUNTIME_PORT`。无需模型 Key。尚未创建函数、网关或其他云资源。

1. 记录函数地域、版本、CPU/内存、并发与伸缩配置、运行时、探针包 SHA-256。
2. `/health` 只作为平台就绪探测，`/probe` 返回固定测试结果和进程 `boot_id`。启动日志输出 `probe_ready`。
3. 收集至少 10 个经平台日志确认的新实例样本。新 `boot_id` 只能证明进程不同，不能单独证明是冷启动；重复请求热实例不能算新样本。
4. 记录平台实例拉起时间、首个业务请求返回时间、请求 ID 与实例 ID，从同一时钟域日志计算第二节要求的区间。客户端请求耗时另记，不能把网络 RTT 或应用内部计时充当实例冷启动。
5. 记录每样本 WordNet 首载数据；分别报告冷启动和热请求的样本数、中位数、P95、失败数。
6. 测试结束释放专用测试资源；保留日志和数据。资源操作待明确测试函数后执行。

当前包不含 `lexicon.db`、业务 API、LLM 客户端、测试工具或 Python 解释器；后续组件进入部署包后需重新测量。测试包小于上传上限只说明这个包的上传体积合规，不表示阶段一或最终部署已经验收。
