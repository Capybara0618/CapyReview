# CapyReview

CapyReview 是一个面向 GitHub Pull Request 的风险驱动 Agent 代码审查系统。它使用
DeepSeek 官方 API 作为唯一模型入口，由 FastAPI 提供 HTTP 与 Webhook 接口，并围绕一条
可解释的审查链路组织核心能力：

```text
PR Diff / GitHub Webhook
          │
          ▼
   Bounded Agent Runtime
          │
          ▼
      Risk Router
       ├─ routine ───────► Correctness Reviewer
       └─ high risk ─────► Security Reviewer + Correctness Reviewer
                                  │
                                  ▼
                  Evidence Validator ─► Independent LLM Judge
                                  │
                                  ▼
                 structured findings / trace / Markdown report
```

核心模块包括：

- 有界 Agent Runtime：步骤与时间预算、节点重试、取消、续跑和 Run Trace；Checkpoint
  覆盖 Agent Loop Observation、Reviewer Final 与 Judge Decision 三个细粒度边界。
- 风险路由：普通变更只调用 Correctness Reviewer；高风险或复杂变更才并行调用 Security
  与 Correctness Reviewer。
- 证据与裁决：Finding 必须通过路径、行号和引用文本的统一校验，再由独立 LLM Judge
  语义复核；Webhook 任务还可读取固定到 PR Head Commit 的有限文件上下文。
- Context 与 Memory：风险优先压缩大 Diff，并检索、沉淀仓库级审查记忆。
- ReviewPolicy Evolution：人工反馈生成候选策略，经 Validation/Holdout 门禁后才能激活，
  支持版本追踪与回滚。
- 可复现执行：任务创建时冻结模型名与 ReviewPolicy 版本，并在结果中汇总真实 LLM 调用数、
  Prompt/Completion Token 和请求延迟。
- 单系统 Evaluation：对完整 CapyReview 工作流运行一次 100 条受控 Diff 评测，不设置对比组。

## 快速开始

推荐使用 Docker Desktop。首次使用只需复制环境模板：

```powershell
Copy-Item .env.example .env
```

在项目根目录 `.env` 中至少填写：

```env
DEEPSEEK_API_KEY=你的官方DeepSeekAPIKey
DEEPSEEK_MODEL=deepseek-chat
```

之后每次启动只需要一行命令：

```powershell
docker compose up --build
```

打开：

- Web 控制台：`http://127.0.0.1:8080/`
- FastAPI 文档：`http://127.0.0.1:8080/docs`
- 健康检查：`http://127.0.0.1:8080/health`

Compose 会同时启动 PostgreSQL、Redis Streams Worker 和 CapyReview。服务可以在未填写密钥时启动并返回配置状态，但真正发起审查必须配置
`DEEPSEEK_API_KEY`。CapyReview 固定连接 DeepSeek 官方地址
`https://api.deepseek.com`，不需要配置 Base URL 或 Provider。

## 发起一次审查

同步审查：

```powershell
$body = @{
  repository = 'demo/api'
  pull_request = 12
  diff = "--- a/app.py`n+++ b/app.py`n@@ -1 +1,2 @@`n-old = value`n+result = eval(user_input)`n"
} | ConvertTo-Json

$result = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8080/v1/reviews `
  -ContentType 'application/json' `
  -Body $body
```

异步审查只需增加查询参数：

```powershell
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8080/v1/reviews?async=true' `
  -ContentType 'application/json' `
  -Body $body
```

查询任务与 Markdown 报告：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/tasks/<task-id>
Invoke-WebRequest http://127.0.0.1:8080/v1/tasks/<task-id>/report
```

## GitHub Webhook

CapyReview 接收 GitHub `pull_request` Webhook，并处理 `opened`、`reopened` 和
`synchronize` 三种动作。请求会经过 HMAC-SHA256 签名校验、时间窗口校验和 Delivery ID
幂等校验，然后异步下载 Diff 并创建审查任务。任务会保存 PR Head Commit，使
`read_file_context` 工具始终读取同一版本的代码，而不是随默认分支漂移。

在 `.env` 中配置：

```env
CAPYREVIEW_GITHUB_WEBHOOK_SECRET=一个随机且足够长的Webhook密钥
CAPYREVIEW_GITHUB_TOKEN=可选的fine-grained PAT
CAPYREVIEW_AUTO_POST_REVIEW=false
```

GitHub Webhook 地址：

```text
https://<你的公网HTTPS地址>/webhooks/github
```

私有仓库需要具有目标仓库读取权限的 fine-grained PAT。若希望审查完成后更新 PR 评论，
将 `CAPYREVIEW_AUTO_POST_REVIEW` 设为 `true`，并授予 Pull requests 写权限。

## ReviewPolicy Evolution

项目只保留一条策略版本链。用户将审查结果标记为 `false_positive`、`missed_issue` 或
`accepted` 后，反馈会进入仓库级 Memory，并可用于生成候选 ReviewPolicy。候选不能直接
生效，必须满足：

1. 策略内容通过安全与完整性检查；
2. Validation 得分达到配置的最小提升；
3. Validation 受保护指标不退化；
4. Holdout 指标不退化；
5. 评测过程没有模型或运行时错误。

相关接口：

- `GET /v1/evolution/status`
- `GET /v1/evolution/runs`
- `POST /v1/evolution/auto`
- `POST /v1/evolution/propose`
- `GET /v1/skills/{skill_name}/versions`
- `POST /v1/skills/{skill_name}/versions/{version}/activate`

激活策略作为版本化 system-prompt 指令注入 Security 与 Correctness Reviewer；它不是可执行
代码，也不会自行扫描 Diff 或生成 Finding。

## 评测

### 工程能力基准

```powershell
python scripts/run_engineering_benchmarks.py
```

该命令生成三组 JSON/Markdown 证据：50 轮任务级 Runtime 故障注入、30 轮细粒度 Agent
恢复测试，以及 30 条大 Diff 上下文压力测试。细粒度测试分别覆盖 Agent Loop
Observation、Reviewer Final 和 Judge Decision；当前报告中恢复成功率、状态一致率与
Trace 完整率均为 100%，重复 LLM 调用为 0。上下文测试的风险证据保留率与 Token 预算
满足率均为 100%，平均输入 Token 缩减 95.2%。以上均为受控工程测试，不代表线上 SLA
或真实 PR 检出效果。

### 100 条单系统 LLM 评测

先运行一条风险样本和一条干净样本的预检：

```powershell
python scripts/run_llm_evaluation.py --smoke
```

正式运行完整 100 条数据集：

```powershell
python scripts/run_llm_evaluation.py
```

评测集包含 40 条风险 Diff、60 条干净 Diff，覆盖 10 个受控仓库。脚本只评测完整的
CapyReview 工作流，输出 `experiment.json`、`case-results.jsonl`、JSON 报告和 Markdown
报告，并保存数据、源码、配置和 Prompt 指纹。调用失败仍保留在分母中。

评测指标包括 Precision、Recall、F1、严重级别准确率、高风险召回率、干净 PR 准确率、
证据有效率、执行成功率和候选过滤漏斗。数据为 `synthetic-controlled`，不能表述为真实
生产 PR 的泛化效果。

> 旧版规则对比实验不属于当前 DeepSeek 单系统 Evaluation 协议，不能作为当前架构的
> 效果指标。该历史数字现由与生产代码隔离的确定性规则基准负责复现，仅用于解释旧实验
> 的计算来源；详见[历史受控规则基准](docs/CONTROLLED_RULE_BENCHMARK.md)。

复现历史受控规则基准无需 API Key：

```powershell
python scripts/run_controlled_rule_benchmark.py
```

## 存储与队列

CapyReview 只有一套正式运行架构：

- PostgreSQL：持久化任务、Checkpoint、Trace、Finding、反馈、Memory 和策略版本；
- Redis Streams：异步审查任务投递、Consumer Group、ACK、租约回收与有界重试。

启动完整系统：

```powershell
docker compose up --build
```

Compose 会读取根目录 `.env`，启动 PostgreSQL、Redis 与 CapyReview，并将服务暴露在
`http://127.0.0.1:8080`。

生产启动路径不会回退到本地文件数据库或进程内任务队列；缺少 PostgreSQL/Redis 配置或连接失败时会直接报错。单元测试使用不参与应用启动的纯内存 Test Double。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 运行时、队列与 DeepSeek 配置状态 |
| `GET` | `/api/dashboard` | 仪表盘汇总 |
| `GET` | `/api/tasks` | 任务列表 |
| `GET` | `/api/evaluation` | 最新完整评测摘要 |
| `POST` | `/v1/reviews` | 同步创建审查 |
| `POST` | `/v1/reviews?async=true` | 异步创建审查 |
| `GET` | `/v1/tasks/{task_id}` | 状态、Trace 与报告数据 |
| `GET` | `/v1/tasks/{task_id}/report` | Markdown 报告 |
| `GET/POST` | `/v1/tasks/{task_id}/feedback` | 查询或提交审查反馈 |
| `POST` | `/v1/tasks/{task_id}/cancel` | 请求取消任务 |
| `POST` | `/v1/tasks/{task_id}/resume` | 从已保存状态续跑 |
| `POST` | `/webhooks/github` | 接收 GitHub PR Webhook |
| `GET/POST` | `/v1/evolution/*` | ReviewPolicy 状态、运行与候选生成 |
| `GET/POST` | `/v1/skills/{skill_name}/versions/*` | 查询、激活或回滚策略版本 |

## 测试

```powershell
python -m unittest discover -s tests -v
```

真实 PostgreSQL/Redis 集成测试使用独立端口 overlay，避免占用本机已有的数据库端口：

```powershell
docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d postgres redis
$env:CAPYREVIEW_TEST_DATABASE_URL='postgresql://capyreview:capyreview-local@127.0.0.1:55432/capyreview'
$env:CAPYREVIEW_TEST_REDIS_URL='redis://127.0.0.1:56379/15'
python -m unittest tests.test_infrastructure_integration -v
```

更多材料：

- [LLM 评测协议](docs/LLM_EVALUATION_SPEC.md)
- [最新工程能力基准报告](output/engineering-evaluation/20260809T041909Z/engineering-benchmark-report.md)
