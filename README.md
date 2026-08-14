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
                                  ├─ read-only GitHub MCP evidence
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
- 工具与证据：Reviewer 通过官方 GitHub MCP 按需读取代码上下文、仓库搜索、文件历史、
  CI 失败和 Code Scanning 告警；仓库、PR 与 Head Commit 由任务注入，不交给模型填写。
  Finding 显式引用 `O1/O2` Observation；Evidence Validator 只把成功且被引用的 MCP 结果
  整理为证据包，再交给独立 LLM Judge 语义复核。
- Context 与 Memory：完整输入超预算时才压缩 Diff；压缩仅移除未修改上下文并保留全部增删行，
  仍超限则按 Hunk 分 Batch 审查；同时检索、沉淀仓库级 Episodic/Semantic 长期记忆；
  当前任务状态由 Agent Loop 与 Checkpoint 管理，不重复写入 Memory。
- 正式 Review Skills：系统按 Reviewer 领域与 Diff 风险信号一次性选择并注入匹配的短
  `SKILL.md`；Evidence/Judge 驳回会先回流原
  Reviewer 一次，只有仍未解决的语义失败和人工误报/漏报才会推动对应 Skill 演化。
- 可复现执行：任务创建时冻结模型名与 Skill 版本集合，并在结果中汇总真实 LLM 调用数、
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
`read_code_context` 与文件历史读取始终固定到同一版本，而不是随默认分支漂移。

在 `.env` 中配置：

```env
CAPYREVIEW_GITHUB_WEBHOOK_SECRET=一个随机且足够长的Webhook密钥
CAPYREVIEW_GITHUB_TOKEN=用于GitHub MCP与PR集成的fine-grained PAT
CAPYREVIEW_AUTO_POST_REVIEW=false
```

GitHub Webhook 地址：

```text
https://<你的公网HTTPS地址>/webhooks/github
```

手动提交 Diff 时可以不配置 GitHub Token；Webhook 审查若要使用远程仓库工具，则需要具有
目标仓库读取权限的 fine-grained PAT。若希望审查完成后更新 PR 评论，将
`CAPYREVIEW_AUTO_POST_REVIEW` 设为 `true`，并授予 Pull requests 写权限。

### GitHub MCP 工具边界

CapyReview 是 MCP Client，不是 MCP Server。项目使用官方 Python SDK `mcp>=2,<3`，固定连接
GitHub 官方远程端点 `https://api.githubcopilot.com/mcp/`，并通过 `X-MCP-Tools` 只开放所需的
只读底层工具。模型实际看到的是五个字段精简的领域工具：

- `read_code_context(path, line)`：读取分配文件在 PR Head Commit 的固定 41 行窗口；
- `search_repository(query, path?)`：搜索当前仓库，仓库限定由系统追加；
- `read_file_history(path)`：读取分配文件在当前 Head 之前的最近 5 次提交；
- `read_ci_failure(check_name?)`：读取当前 Head 的失败 CI 与末尾日志；
- `read_code_scanning_findings(severity?)`：读取并过滤到当前 Head 的开放扫描告警。

Security Reviewer 获得代码上下文、仓库搜索、文件历史和扫描告警；Correctness Reviewer 将
扫描告警替换为 CI 失败。Memory 在 Reviewer 执行前自动召回，不是 Tool；`changed_line` 只属于
Evidence Validator。MCP 参数错误、认证失败或上游错误会变成下一轮可见的失败 Observation，
不会回退到本地规则或旧工具；Observation 保存后，任务恢复不会重复同一次外部调用。Agent
Loop 的最后一步固定为 Final-only，防止模型把全部步数预算耗在连续取证上。

每个 Tool Observation 使用任务内顺序编号 `O1/O2/...`。Reviewer 只能通过 `evidence_refs`
显式引用成功的 GitHub MCP 结果；Skill 加载、失败调用和未引用结果不会进入 Judge 证据包。

### 分层上下文预算

每次 Reviewer 模型调用都按三层构造完整输入：

1. **先完整组装：** System Prompt、Assignment、输出契约、已选 `SKILL.md`、Tool Schema、
   完整 Diff、Observation、反馈和 Memory 一起计入输入预算；能够容纳时不压缩。
2. **超限才压缩：** 解析 Git Unified Diff，保留文件路径、Hunk Header 与全部增删行，只移除
   未修改上下文；缺失代码由 Reviewer 通过 GitHub MCP 按 Commit 和行号补取。
3. **分 Batch 兜底：** 紧凑变更视图仍超限时，按原始 Hunk/变更块顺序生成多个预算内 Batch，
   每批分别运行 Agent Loop，最终统一验证、去重和裁决，不通过风险优先级丢弃代码。

Memory 只在当前仓库召回相关 Episodic/Semantic Top-K；工具输出在调用源头限制返回窗口和数量。

Agent Loop 最后一轮移除 Tool Schema 并强制 Final-only。每轮 `context_window_prepared` Trace
都携带 Context Manifest，记录 System、Skill、Tool、Diff、Observation 与 Memory 的估算 Token，
以及各类内容的保留/丢弃数量。

## Formal Review Skill Evolution

项目将认证安全、数据库迁移和异步可靠性等专业流程组织成符合 Agent Skills 规范的
`SKILL.md + references/` 包。系统根据当前 Diff 和 Reviewer 领域自动选择匹配的短
`SKILL.md`，并在 Agent Loop 开始前注入上下文；较长参考资料由 Reviewer 通过
`read_skill_reference` 渐进读取。

Evidence Validator 或 Judge 驳回候选后，系统把结构化原因返回原 Reviewer 一次，并保存独立
Reflection Checkpoint。修正成功不进入演化；仍未解决的反思失败与人工 `false_positive`、
`missed_issue` 反馈绑定到当时激活的 Skill。同一 Skill 每累计 3 条，通过 Redis Streams 异步
调用 LLM 在当前版本基础上生成候选包。工具、模型或基础设施错误只进入 Trace，不参与 Skill
演化。候选不能直接生效，必须满足：

1. `SKILL.md`、frontmatter 和引用文件通过格式、安全与非执行性检查；
2. Validation 综合得分达到配置的最小提升；
3. Validation/Holdout 的综合分、高风险召回和干净样本准确率不退化；
4. 评测过程没有模型或运行时错误；若发生临时错误则标记 `deferred`，稍后重试。

相关接口：

- `GET /v1/evolution/status`
- `GET /v1/evolution/runs`
- `POST /v1/evolution/auto`
- `POST /v1/evolution/propose`
- `GET /v1/skills/{skill_name}/versions`
- `POST /v1/skills/{skill_name}/versions/{version}/activate`

激活版本作为可发现的正式 Skill 进入注册表；命中领域与信号后自动进入对应 Reviewer 上下文，
Reference 仍由 Reviewer 按需读取。
生成 Skill 不允许携带脚本、命令、工具定义或绕过 Evidence/Judge 的指令。任务创建时冻结
Skill 版本集合，续跑不会静默切换版本。

## 评测

### 工程能力基准

```powershell
python scripts/run_engineering_benchmarks.py
```

该命令生成三组 JSON/Markdown 证据：50 轮任务级 Runtime 故障注入、30 轮细粒度 Agent
恢复测试，以及 30 条大 Diff 上下文压力测试。细粒度测试分别覆盖 Agent Loop
Observation、Reviewer Final 和 Judge Decision；当前报告中恢复成功率、状态一致率与
Trace 完整率均为 100%，重复 LLM 调用为 0。上下文测试按完整模型请求计入规则层、Diff、
Observation 与 Memory；当前报告中压缩触发率、Batch 触发率、变更行覆盖率和 Token 预算
满足率均为 100%，平均单次输入 Token 缩减 92.8%，所有 Batch 累计 Token 为原完整请求的
62.3%。单次缩减与累计开销分开统计；以上均为受控工程测试，不代表线上 SLA
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

- PostgreSQL：持久化任务、Checkpoint、Trace、Finding、反馈、Memory 和 Skill 版本；
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
| `GET/POST` | `/v1/evolution/*` | Formal Skill 状态、运行与候选生成 |
| `GET/POST` | `/v1/skills/{skill_name}/versions/*` | 查询、激活或回滚 Skill 版本 |

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

官方远程 GitHub MCP 契约测试是显式 opt-in，避免普通单测访问公网：

```powershell
$env:CAPYREVIEW_TEST_GITHUB_TOKEN='只读测试Token'
$env:CAPYREVIEW_TEST_GITHUB_REPOSITORY='owner/repo'
$env:CAPYREVIEW_TEST_GITHUB_HEAD_COMMIT='完整CommitSHA'
$env:CAPYREVIEW_TEST_GITHUB_FILE='README.md'
python -m unittest tests.integration.test_github_mcp -v
```

更多材料：

- [LLM 评测协议](docs/LLM_EVALUATION_SPEC.md)
- [最新工程能力基准报告](output/engineering-evaluation/20260809T041909Z/engineering-benchmark-report.md)
