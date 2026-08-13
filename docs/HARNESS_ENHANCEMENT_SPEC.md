# CapyReview Harness 增强规格

## 目标

在现有 CapyReview 代码上增量增强领域型 Agent Runtime Harness，使恢复边界与昂贵、非确定性的 LLM 调用对齐，同时保持项目精简、可解释和适合面试展示。

本轮增强完成后，系统应当支持：

1. 已完成的 Reviewer 在任务恢复时不再重复调用；
2. Agent Loop 在工具 Observation 已保存后从下一轮继续；
3. 已完成的 Judge Decision 在报告阶段故障后可以复用；
4. 确定性的 Diff 解析、风险路由、证据校验和报告组装允许重新计算；
5. Trace 能说明哪些结果被执行、保存或恢复。

## 已确认设计

### 保留外层生命周期

```text
Planning -> Executing -> Reviewing
```

- `Planning`：解析 Unified Diff；
- `Executing`：执行 Router、Reviewer、Validator、Judge；
- `Reviewing`：组装 ReviewReport。

不修改现有 TaskState、API 路径和前端状态名称。

### 增加内层恢复边界

```text
Executing
|- reviewer:A01
|  `- loop:A01
|- reviewer:A02
|  `- loop:A02
`- judge
```

- `loop:<assignment_id>`：仅保存 `next_step` 和已完成的 `observations`；
- `reviewer:<assignment_id>`：保存 Reviewer 最终 Findings；
- `judge`：保存对候选 Findings 的最终 Decisions；
- Reviewer Final 保存成功后删除对应临时 Loop Checkpoint；
- 没有细粒度 Checkpoint 的旧任务按现有逻辑从 Executing 开始执行。

### Checkpoint 保持最小模型

复用现有 Checkpoint 存储，不新增表，不使用输入哈希或上下文指纹。逻辑模型保持为：

```text
task_id
checkpoint_key
status
attempt
data
updated_at
```

唯一身份由 `task_id + checkpoint_key` 表达。错误详情继续写入 Trace，不在每条 Checkpoint 中重复存储。

### 任务配置冻结

同一个 `task_id` 的 Diff 和执行配置不可原地修改。任务创建时在现有任务输入 JSON 中记录模型和 Skill 版本集合；续跑使用原任务配置。需要使用新 Skill 重新审查时创建新任务。

### 后续能力增强

细粒度恢复稳定后，按顺序继续：

1. 删除 Evidence Validator 对固定 `rule_id` 的硬编码，只保留通用证据来源校验；
2. 增加固定到 PR Head Commit 的只读 `read_code_context` GitHub MCP 工具；
3. 必要时再增加受限 `search_repository`，不提供 Shell；
4. 记录每个任务的 LLM 调用次数、Token、延迟、重试、工具调用和恢复次数；
5. 重新运行工程故障注入与正式单系统评测。

## 技术栈

- Python 3、`unittest`
- FastAPI / Uvicorn
- PostgreSQL：任务、Checkpoint、Trace、Memory、Policy
- Redis Streams：异步投递、ACK、重试和回收
- DeepSeek 官方 API：Reviewer 与 Judge
- Docker Compose：唯一正式启动方式

不新增工作流框架、数据库、消息队列或模型供应商。

## 命令

```powershell
# 单个测试模块
python -m unittest tests.test_runtime_memory_context -v

# 完整单元测试
python -m unittest discover -s tests -v

# PostgreSQL / Redis 集成测试
docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d postgres redis
$env:CAPYREVIEW_TEST_DATABASE_URL='postgresql://capyreview:capyreview-local@127.0.0.1:55432/capyreview'
$env:CAPYREVIEW_TEST_REDIS_URL='redis://127.0.0.1:56379/15'
python -m unittest tests.test_infrastructure_integration -v

# 工程故障测试
python scripts/run_engineering_benchmarks.py

# 一行启动
docker compose up -d --build
```

正式 DeepSeek 100 条评测会产生费用，仅在用户明确授权后运行。

## 项目结构

```text
capyreview/runtime.py          通用 AgentRuntime 与 AgentLoop
capyreview/harness.py          PR 审查外层三阶段生命周期
capyreview/agents.py           Router、Reviewer 协作、Validator、Judge
capyreview/postgres_store.py   PostgreSQL Checkpoint 与任务状态
capyreview/github.py           GitHub Diff 与评论固定工作流
capyreview/mcp.py              GitHub MCP 只读取证工具与可信任务参数绑定
tests/                         单元与基础设施集成测试
docs/                          架构、评测和面试说明
tasks/                         当前实施计划与任务清单
```

## 代码风格

保持现有直接、显式的 Python 风格，不引入抽象工厂或通用 DAG DSL。新增接口优先使用可选回调，避免 `AgentLoop` 直接依赖 PostgreSQL：

```python
result = agent_loop.run(
    stepper,
    tools,
    initial_state,
    event_sink=emit,
    resume_state=saved_loop,
    checkpoint_sink=save_loop,
)
```

Checkpoint Key 使用可读业务名称：

```text
loop:A01
reviewer:A01
judge
```

## 测试策略

### 小型单元测试

- AgentLoop 能从已有 Observation 和 `next_step` 恢复；
- Reviewer Final 可以直接恢复为 Finding；
- Judge Decision 可以恢复且仍受 Evidence grounded 硬门禁约束；
- Evidence Validator 不依赖固定 `rule_id`；
- 仓库工具校验路径、Commit 和返回预算。

### 中型组件测试

- 一个 Reviewer 完成、另一个失败时，续跑不重复第一个 Reviewer；
- Agent Loop 在 Observation 后故障，续跑从下一步继续；
- Judge 完成、Reviewing 失败后，续跑不重复 Judge；
- 旧任务无细粒度 Checkpoint 时保持原行为。

### 基础设施测试

- PostgreSQL 支持任意可读 Checkpoint Key 的保存、覆盖、读取与删除；
- Redis 重复投递时最终任务状态与结果一致；
- Docker Compose 启动后健康检查仍报告 PostgreSQL 与 Redis Streams。

## 边界

### 始终执行

- 每个行为变化先写失败测试；
- 每个增量完成后运行聚焦测试；
- 提交前运行完整单元测试；
- Checkpoint 只保存恢复必需数据；
- 只读仓库工具固定到目标 PR Commit，并限制路径和输出长度；
- 保留旧任务的安全回退路径。

### 需要先确认

- 新增数据库表或列；
- 新增第三方依赖；
- 运行付费 DeepSeek 评测；
- 增加任何可写、Shell 或测试执行工具；
- 修改公开 API、TaskState 或简历内容。

### 禁止

- 使用 SHA256 等不透明输入指纹控制恢复；
- 将模型、Policy 等相同信息复制到每条 Checkpoint；
- 引入多租户、RBAC、灰度发布或通用 DAG 平台；
- 通过删除测试掩盖回归；
- 提交 `.env`、API Key 或真实仓库私密代码。

## 成功标准

1. Reviewer、Agent Loop、Judge 三个恢复边界均有先失败后通过的测试；
2. 故障恢复不会重复调用已经持久化成功的 Reviewer 或 Judge；
3. Agent Loop 恢复时保留 Observation，并从记录的下一轮继续；
4. 旧任务、同步审查、异步审查、取消和外层 Resume 均无回归；
5. 不新增 Checkpoint 表和第三方依赖；
6. Evidence Validator 固定规则硬编码被删除；
7. 至少一个只读仓库上下文工具完成端到端验证；
8. 完整测试、真实 PostgreSQL/Redis 集成测试和 Docker 一行启动通过；
9. 工程报告能区分任务级恢复和 Agent 执行级恢复；
10. 文档不宣称 Token 级恢复、严格 exactly-once 或通用 Runtime 平台。

## 非目标

- Token 级或流式生成中间状态恢复；
- 任意 DAG、分支回放或 Time Travel；
- 严格 exactly-once；
- LLM Router；
- 更多 Reviewer Agent；
- 自动代码修复；
- Shell、测试执行或容器沙箱；
- 多模型平台与模型能力对比实验。

## 开放问题

当前没有阻塞实现的问题。若现有 PostgreSQL Checkpoint API 已支持任意 Key 和删除操作，则不修改数据库结构；否则优先补充最小删除方法，而不是新增表。
