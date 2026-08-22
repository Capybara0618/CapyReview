# CapyReview 面试问题清单与口语化回答

这份题库只从简历中面试官能够看到的内容出发。回答不是逐字背诵稿，建议记住每题的两三个关键词，再用自己的话展开。

## 使用方法

第一轮先掌握第 1～15 题，做到能在 3 分钟内讲清项目。第二轮按模块准备追问。最后重点练习“压力与真实性问题”，因为这些问题最能区分真正做过项目和只会念简历。

回答时始终守住三个口径：

- CapyReview 的核心是 **PR 审查 Agent 的运行与治理框架**，不是宣称训练了更强的代码模型。
- Multi-Agent 的价值是 **职责隔离、按风险分配成本和增加证据门禁**，不直接宣称一定比单 Agent 准。
- Evaluation Harness 用来 **持续发现问题和比较版本**。当前质量评测暴露了召回偏低的问题，不把弱结果包装成提升。

---

## 一、高频必问题

### 1. 你先简单介绍一下 CapyReview。

我会这样回答：

> CapyReview 是一个面向 GitHub PR 的代码审查 Agent Runtime Harness。GitHub 通过 Webhook 把 PR 事件推给系统，任务进入 Redis Streams，PostgreSQL 保存任务状态、Checkpoint 和 Trace。系统先解析 Diff，再由 Risk Router 决定只跑 Correctness Reviewer，还是并行跑 Security 和 Correctness Reviewer。Reviewer 可以在有界 Agent Loop 里通过 GitHub MCP 补充代码、历史和 CI 证据，最后经过确定性的证据校验和独立 Judge 复核，生成结构化 Finding、Markdown 报告和 PR 评论。除此之外，我还做了上下文预算、仓库 Memory、版本化 Review Skills、自进化门禁和离线评测。

一句话收尾：

> 我想解决的不只是“让 LLM 看一次 Diff”，而是让一次审查任务可恢复、可追踪、可评测，也能逐步沉淀审查经验。

### 2. 这个项目解决了什么实际问题？

> 单次把 PR Diff 丢给模型当然也能得到结果，但真实审查有几个问题：大 Diff 可能超上下文，模型需要补充仓库证据，接口会超时或失败，输出还可能引用不存在的行。另外，模型说得像不像不等于证据是否成立。所以我把审查拆成路由、分析、取证、校验和裁决几层，并用 Runtime 管理失败恢复和过程状态。

### 3. 为什么这个场景需要 Agent，而不是一次 LLM 调用？

> 如果只是小 Diff 的粗略建议，一次 LLM 调用就够了。CapyReview 使用 Agent 的原因是模型有时需要根据当前判断继续读取固定 Commit 的源码、搜索调用方、查看文件历史或者读取 CI 失败日志，下一步动作取决于上一步 Observation。这个“分析—取证—再判断”的闭环才是 Agent Loop。项目不是为了套 Agent 概念，而是把需要外部证据的审查任务做成有界循环。

### 4. 系统的输入和输出分别是什么？

> 核心输入是仓库、PR 编号、固定的 head commit 和 unified diff。系统内部会把 Diff 解析成文件、Hunk 和新增行。最终输出是一组结构化 Finding，每条包含路径、问题行、严重程度、解释、修复建议、测试建议和证据引用，然后再生成 Markdown 报告和 GitHub PR 评论。

### 5. 整条链路里哪些地方使用了 LLM？

> 主要有四处。第一是 Security 和 Correctness Reviewer 做代码分析和工具决策；第二是 Judge 对已经通过结构校验的 Finding 做语义复核；第三是 Skill Evolution 根据失败案例生成候选 Skill 版本；第四是离线评测中用语义 Matcher 判断 Finding 和人工 Golden Issue 是否描述同一个问题。Diff 解析、Risk Router、Evidence Validator、上下文预算、版本门禁和任务状态机都是确定性逻辑。

### 6. 你认为项目最核心的三个亮点是什么？

> 第一是可恢复的 Runtime Harness，把 LLM 调用和工具 Observation 做成可持久化的执行边界；第二是证据驱动的多 Agent 审查，不让模型只凭感觉下结论；第三是把上下文、Memory、Skill 和离线评测串起来，让系统能够积累经验并验证新版本，而不是只改 Prompt 靠主观感受。

### 7. 一条 PR 从进入系统到输出结果，大致怎么走？

> GitHub Webhook 触发后，服务先验签和去重，再创建任务并放进 Redis Streams。Worker 取到任务后拉取 Diff，Runtime 进入 Planning，解析文件和新增行；然后 Risk Router 分流 Reviewer。Reviewer 在 Agent Loop 里读取上下文，必要时调用 GitHub MCP。候选 Finding 先经过 Evidence Validator，再交给 Judge，没通过的可以有一次受限的反思修正。最后结果去重排序、写入 PostgreSQL，并按配置更新 PR 评论。

### 8. 这个项目里的 Agent 到底是什么？

> 我没有把每个普通函数都叫 Agent。真正的 Reviewer Agent 是由角色 Prompt、上下文、工具集合、循环状态和停止条件组成的执行单元。Risk Router 和 Evidence Validator 虽然是流程角色，但本质是确定性组件；Judge 使用 LLM，但它只裁决候选 Finding，不拥有完整的自由工具循环。

### 9. 这是代码审查工具，为什么项目名称强调 Runtime Harness？

> 因为我主要想展示的不是某个模型能找出多少 Bug，而是怎样可靠地运行一个会调用工具、会失败、耗时较长的 Agent 任务。Harness 管的是状态、预算、重试、Checkpoint、取消、续跑和 Trace。PR 审查是承载这些工程问题的具体场景。

### 10. 你个人主要完成了哪些工作？

> 这是我的个人项目，架构和核心实现都是我自己完成的。我重点做了 Runtime 和细粒度 Checkpoint、多 Reviewer 编排、GitHub MCP 适配、上下文与 Memory、Skill 版本演化、PostgreSQL/Redis Streams 工程链路，以及真实 PR Evaluation Harness。模型本身没有训练，我做的是模型外部的 Agent 系统工程。

### 11. 为什么不直接接一个现成的代码审查 API？

> 接现成 API 可以更快得到一个产品，但我这个项目的目标是研究 Agent Runtime、证据获取和评测这些工程问题。因此我保留通用 LLM 接口，把主要工作放在运行时和治理层。这样模型可以替换，系统的状态、工具、安全边界和评测逻辑仍然成立。

### 12. 你如何证明它不只是一个 Toy Demo？

> 我不会只用“功能很多”来证明。工程上它有 PostgreSQL 持久化、Redis Streams 消费组、Webhook 幂等、细粒度 Checkpoint、固定 Commit 的 MCP 取证和可恢复任务；验证上有故障注入、真实 PR 上下文测试和独立的质量评测集。更重要的是，评测发现召回不足时我保留了结果，没有把它改写成虚假的提升。

### 13. 你为什么选择 PR 审查作为 Agent 场景？

> PR 审查的输入和结果都比较清楚：输入是 Diff 和仓库证据，输出可以落到具体新增行；同时它又天然需要工具调用、长上下文、证据校验和人工反馈。这个场景既能体现 Agent 的动态决策，也方便做离线评测，不会变成完全主观的聊天 Demo。

### 14. 当前使用的是什么模型？模型是项目亮点吗？

> 当前接的是 DeepSeek 官方模型，Reviewer 和 Judge 使用同一底座但角色 Prompt 不同。模型不是项目的主要卖点，换成其他支持结构化输出的模型，Runtime、MCP、Memory、Skill 和 Evaluation 仍然可以复用。

### 15. 如果只让你画一张架构图，你会怎么画？

> 我会分三层。入口层是 FastAPI、GitHub Webhook 和 Redis Streams；执行层是 Runtime Harness，内部是 Risk Router、Reviewer Agent Loops、Evidence Validator 和 Judge；支撑层是 PostgreSQL、GitHub MCP、Context/Memory、Skills 和 Evaluation。主链从 Webhook 向下走，所有状态和证据最终落 PostgreSQL。

---

## 二、Runtime Harness 与 Agent Loop

### 16. Runtime Harness 和 Agent Loop 是同一个东西吗？

> 不是。Agent Loop 是 Reviewer 内部的微循环，基本形式是“模型判断—调用工具—获得 Observation—继续判断—输出 Final”。Runtime Harness 是外层执行控制，它管理任务状态、节点预算、失败重试、Checkpoint、取消、续跑和 Trace。可以理解为 Loop 负责思考和行动，Harness 负责让这段过程可靠地运行。

### 17. 你的 Harness 是怎么实现的？

> 我实现了两个层次。外层 ReviewHarness 用任务状态机串起 Planning、Executing 和 Reviewing；内层 AgentRuntime 把 Reviewer 执行拆成命名节点，并对每个节点设置最大步数、超时、重试和取消检查。重要中间结果通过 PostgreSQL Checkpoint 持久化，恢复时从最近一个完成边界继续。

### 18. Planning、Executing、Reviewing 三个阶段分别做什么？

> Planning 不调用 LLM，负责解析 unified diff、验证新增行并生成执行输入。Executing 是真正的多 Agent 审查，里面包含路由、Reviewer Loop、证据校验和 Judge。Reviewing 负责把已经批准的 Finding 汇总成报告、风险摘要和协作信息，也不再次做代码分析。

### 19. Agent Loop 一轮具体发生什么？

> 每轮先组装 System Prompt、Assignment、Skill、工具 Schema、当前 Diff、历史 Observation、反馈和 Memory。模型只能选择两类动作：调用一个工具，或者输出 Final Findings。调用工具后，参数先校验，再执行，结果无论成功还是失败都会变成结构化 Observation，下一轮模型可以根据这个结果继续；达到步数上限时会强制进入 Final。

### 20. 为什么要做“有界” Agent Loop？

> 因为工具调用很容易出现无效搜索、重复调用或模型一直不肯结束。如果没有边界，成本和延迟都不可控。我同时限制最大轮数、总超时、Observation 长度和重试次数，最后一轮还会移除工具选择，要求模型直接给出 Final。

### 21. 节点重试和任务恢复有什么区别？

> 节点重试处理的是短暂错误，比如一次 LLM 超时，当前进程里重新执行这个节点。任务恢复处理的是进程中断或服务重启，它会从 PostgreSQL 读取 Checkpoint，跳过已经完成的副作用。一个是局部重试，一个是跨进程续跑。

### 22. Checkpoint 为什么要做到 Tool Observation、Reviewer Final 和 Judge Decision？

> 早期如果只在大阶段结束时保存，Reviewer 已经调完工具或模型后崩溃，恢复时还会重复花钱。现在把三个不可忽略的结果分别落盘：工具 Observation、Reviewer Final、Judge Decision。恢复时先查这个逻辑步骤有没有完成，有就直接复用结果，没有才重新调用。

### 23. “重复 LLM 调用为 0”是怎么保证的？

> 这里不是说正常流程只调用一次，而是故障恢复没有把已经成功的 LLM 步骤再调用一遍。每个可恢复步骤有稳定的任务、Agent、阶段和轮次标识，成功结果先持久化，恢复时按这个标识读取。只有没有成功 Checkpoint 的步骤才会重新执行。

### 24. 恢复步骤是怎么唯一标识的？

> 我使用任务、Agent、阶段、轮次和批次这类稳定的业务字段定位步骤。任务输入又被固定 Commit 和冻结版本约束，所以恢复时能够判断某一步是否已经完成。这样键本身可读，Trace 也更容易排查。

### 25. 取消和续跑怎么实现？

> 取消不是直接杀线程，而是在任务表记录 cancel_requested。Runtime 在节点边界和循环中检查这个标记，安全地把任务转成 CANCELLED。续跑会清除取消标记，把非成功任务重新放回队列，然后读取已有 Checkpoint 继续；已经 SUCCESS 的任务不会重复续跑。

### 26. Run Trace 记录什么？

> Trace 记录任务状态变化、节点开始和结束、重试、工具 Observation、Checkpoint 保存或恢复、Reviewer Final、Judge Decision，以及错误原因。它的作用不是展示一段漂亮思维链，而是回答“任务跑到哪里、为什么失败、恢复时跳过了什么”。模型的隐藏推理不会保存。

### 27. 30 轮细粒度恢复测试是怎么测的？

> 我分别在 Tool Observation、Reviewer Final 和 Judge Decision 落盘之后注入异常，每个位置做 10 轮，共 30 轮。然后重启或续跑任务，检查能否成功结束、最终状态是否一致、Trace 是否完整，以及已完成的 LLM 调用是否被重复。这个测试证明的是恢复语义，不是模型审查准确率。

### 28. 为什么不用 LangGraph？

> LangGraph 完全可以做这类流程，我不是因为它做不到才自己写。这个项目的主拓扑比较固定，核心难点是 PostgreSQL 里的任务状态、细粒度副作用边界和我自己的恢复语义，所以我实现了一个更小的 Runtime，能够直接控制每个 Checkpoint 字段。如果以后出现大量动态分支、子图、长时间人工中断和复杂循环，我会优先考虑 LangGraph，而不是继续扩展自研框架。

### 29. 自研 Runtime 会不会重复造轮子？

> 有这个代价，所以我刻意把范围控制得很小，只实现这个项目真正需要的状态机、节点执行、预算、Checkpoint 和 Trace，没有去做通用图 DSL。它既是项目的学习重点，也让我能够解释每个恢复边界。生产项目如果已有成熟框架和团队规范，我不会为了自研而自研。

---

## 三、Multi-Agent、Risk Router 与 Judge

### 30. 为什么是 Security 和 Correctness 两个 Reviewer？

> 这两个角色关注的失败模式不同。Security Reviewer 更关注信任边界、鉴权、注入、敏感数据和危险调用；Correctness Reviewer 更关注运行时错误、数据一致性、异常处理和行为回归。拆开后每个 Prompt 更聚焦，也方便按风险只支付必要的调用成本。

### 31. 两个 Reviewer 用的是同一个模型，还能叫 Multi-Agent 吗？

> 可以。Agent 的差异不只来自模型，还来自角色目标、System Prompt、可见上下文、工具集合和独立状态。这里是同一底座的两个角色实例，而不是两个不同厂商模型。我不会把它包装成模型集成，它的价值是任务分工。

### 32. 两个 Reviewer 都运行时有先后顺序吗？

> 没有业务先后依赖，高风险 PR 会并行执行两个 Reviewer。它们共享的是固定 Diff 和任务元数据，不互相读取对方的中间输出。等两边都完成后，再统一做证据校验、Judge 和去重排序。

### 33. Risk Router 是 LLM 吗？

> 不是，是确定性规则路由。它从解析后的 Diff 中看危险调用、敏感路径和变更规模。普通 PR 只分给 Correctness Reviewer；命中高风险信号时，同时分给 Security 和 Correctness Reviewer。

### 34. 为什么不用 LLM 做 Router？

> 当前路由空间只有两条，输入也都是路径、危险 API 和文件数量这类结构化信号，规则更快、可解释，也不会额外消耗一次模型调用。它的目标不是判断代码有没有 Bug，只是判断是否需要增加 Security 审查。将来领域更多、意图更模糊时，可以增加轻量模型路由，但现在没有必要。

### 35. 静态 Router 怎么保证可靠？

> 我把它设计成偏保守的成本路由，而不是最终安全裁判。敏感路径、危险调用和大变更任意命中就升级双 Reviewer，漏掉路由不代表系统完全不看，因为 Correctness Reviewer 始终运行。Router 的规则可以用已标注 PR 做覆盖测试，后续重点看高风险增援覆盖率和误增援带来的成本。

### 36. Router 的设计依据是什么？

> 依据是代码审查中比较稳定的可观察信号：认证授权、密钥和配置等敏感路径；命令执行、反序列化、动态 SQL 等危险调用；以及跨文件的大规模变更。它们不是用来直接判漏洞，而是用来判断审查深度，所以规则不需要非常复杂。

### 37. Reviewer 的 Assignment 长什么样？

> 它是一个精简结构，包含 Agent 名称、审查目标、文件列表、风险域、关注行、轮次和路由原因。Assignment 的作用是让编排和 Trace 知道“谁为什么审哪些文件”。当前 Prompt 主要注入目标、文件和风险域，关注行更多用于计划和追踪，不会假装每个字段都参与了模型推理。

### 38. 两个 Reviewer 的输出怎么统一？

> 都必须输出同一个 Finding Schema，包括 path、line、severity、title、explanation、evidence、fix、test 和 evidence_refs。统一 Schema 后，下游 Evidence Validator、Judge、去重和报告不需要理解 Reviewer 的内部差异。

### 39. Evidence Validator 做什么？为什么不用 LLM？

> 它做的是能够确定性验证的事情：路径是否属于本次 PR、行号是否是新增行、引用代码是否真的出现在对应位置，以及引用的 MCP Observation 是否成功且存在。这里用规则比 LLM 更可靠，也更便宜。它不判断“这个问题语义上是否严重”，那是 Judge 的工作。

### 40. Judge 会拿到什么信息？

> Judge 拿到候选 Finding、对应新增行的紧凑代码视图，以及 Evidence Validator 整理出的有效证据包。它不需要重新看整份 PR，也不能凭空新增 Finding，只能对候选结果批准或拒绝，并给出置信度和理由。

### 41. 为什么还需要 LLM Judge？

> Evidence Validator 只能证明“引用是真实的”，不能证明“结论一定成立”。比如代码确实在这一行，但 Reviewer 对业务语义的理解可能错。Judge 负责做第二次语义复核，把结构真实性和语义合理性分开。

### 42. Reviewer 和 Judge 都用同一个模型，会不会互相认同？

> 确实存在相关性，所以它不是完全独立的外部真相。我通过角色 Prompt、输入范围和权限做隔离：Reviewer 能分析和取证，Judge 只看候选及证据，不能发明新问题。离线评测也表明 Judge 会大量拒绝候选，说明门禁不是形式上的；但更强的方案是使用不同模型或抽样人工复核，我会把这点作为限制说明。

### 43. Finding 被拒绝后会发生什么？

> 系统允许一次受限的 Reflection。Reviewer 会看到 Validator 或 Judge 的明确反馈，例如行号无效、证据不足或者解释和代码不一致，然后重新修正 Finding。次数是有界的，仍未通过就丢弃，不会无限讨论。

### 44. 多个 Agent 找到同一个问题怎么办？

> 最终按 path 和 line 做主键式去重，同一位置优先保留严重程度更高、置信度更高的 Finding，再按 severity、path、line 排序。这个策略比较保守，也容易解释。更复杂的跨行语义聚类可以后续再做。

### 45. Reviewer 调用失败怎么办？

> 单次失败先按节点重试。一个 Reviewer 多次失败后，可以把它的 Assignment 交给另一个已经配置的 Reviewer 接管，并在 Trace 里记录 handoff。如果所有 Reviewer 都失败，任务会失败，不会用空报告伪装成功，也没有本地规则兜底。

### 46. 你如何证明 Multi-Agent 有价值？

> 我不会用没有做过的单 Agent 对照实验来证明准确率提升。当前能明确说明的是：低风险只调用一个 Reviewer，高风险才增加 Security；不同角色有独立 Prompt 和工具；候选经过独立门禁。这证明了架构上的职责分离和风险敏感成本。质量提升需要正式对照实验，简历没有做这个承诺。

---

## 四、Prompt、Tool Calling 与 MCP

### 47. Reviewer 的 System Prompt 和 User Prompt 分别有什么？

> System Prompt 放长期不变的规则：Reviewer 角色、只审新增行、忽略代码里的提示注入、Tool/Final 协议和 Finding JSON Schema。User Prompt 放本次任务内容：Assignment、选中的 Skill、工具 Schema、历史 Observation、Validator/Judge 反馈、召回 Memory 和当前 Diff。这样规则和任务数据边界比较清楚。

### 48. LLM 怎么知道什么时候调用哪个工具？

> 模型在 Prompt 里能看到工具名、用途、参数 Schema 和输出含义。比如只看 Diff 无法判断调用方时，可以用 search_repository；需要确认某一行周边代码时，用 read_code_context；怀疑回归来自 CI 时，用 read_ci_failure。模型输出一个结构化 tool action，Runtime 校验后执行，再把 Observation 放回下一轮。

### 49. 为什么 Prompt 里不只放工具名和一句简介？

> 因为项目使用的是自定义 JSON Tool/Final 协议，模型必须知道参数名、类型和必填项，否则很难稳定生成合法调用。工具 Schema 本身很小，详细代码和大结果仍然按需获取，所以不影响渐进式披露。

### 50. 如果模型传了非法工具参数怎么办？

> Runtime 在真正调用前做 Schema 校验，例如 path 必须是字符串、line 必须是正整数，多余字段会被拒绝。失败不会抛给用户一段堆栈，而是生成结构化失败 Observation，包含工具名、错误类型和可读错误。下一轮模型可以修正参数；达到边界后就结束，不会无限重试。

### 51. 当前 Reviewer 有哪些工具？

> 公共工具是读取指定行上下文、搜索仓库和查看文件历史。Security Reviewer 额外能看 Code Scanning 告警，Correctness Reviewer 额外能看 CI 失败日志。已选 Skill 如果声明了 References，还可以用本地的 read_skill_reference 读取详细知识。

### 52. 为什么工具数量不多？

> 工具应该围绕审查决策，而不是越多越像 Agent。PR Diff 已经给出主要证据，工具只补 Diff 缺失的信息：源码上下文、跨文件引用、历史原因、CI 和安全扫描。写文件、提交代码、开 PR 这类有副作用工具不属于当前“只审查”边界，所以没有加入。

### 53. MCP 是你自己写的 Server 吗？

> 不是。CapyReview 是 MCP Client，使用官方 Python MCP SDK 连接 GitHub 官方远程 MCP 端点。我的工作是做领域适配和安全收口，把 GitHub 的通用工具封装成 Reviewer 更容易使用的只读审查工具。

### 54. 你底层用了 GitHub MCP 的哪些能力？

> 主要是读取文件、搜索代码、列提交、读取 Actions 和 Job 日志、读取 Code Scanning Alerts。对 Reviewer 暴露时，我把它们映射成 read_code_context、search_repository、read_file_history、read_ci_failure 和 read_code_scanning_findings。

### 55. 为什么不把 GitHub MCP 原始工具全部交给模型？

> 原始工具面太大，模型更容易选错，也增加越权风险。适配层会自动注入仓库、PR 和固定 head commit，并限制路径、返回窗口和结果数量。Reviewer 只能读取本任务需要的内容，不能改仓库，也不能把目标切到其他 Repo。

### 56. 为什么固定 Commit，而不是直接读分支最新代码？

> PR 审查过程中分支可能继续 push。如果工具读的是移动中的 HEAD，Diff 和源码证据可能不属于同一个版本，结果无法复现。所以任务创建时冻结 head commit，所有 MCP 读取都绑定这个 Commit。

### 57. GitHub MCP 调用失败怎么办？有本地兜底吗？

> 参数错误、鉴权失败、超时或上游异常都会变成失败 Observation，模型下一轮可以换查询或直接基于已有 Diff 得出保守结论。系统没有偷偷切换到本地规则 Reviewer；如果关键步骤最终失败，就按 Runtime 语义重试或让任务失败。

### 58. read_skill_reference 也是 MCP Tool 吗？

> 不是。它是本地 Agent Tool，只能读取当前已经激活 Skill 声明的 References。GitHub MCP 用来获取仓库外部证据，Skill Reference 是项目内部版本化知识，两者来源和信任边界不同。

### 59. 项目用了渐进式披露吗？

> 用了，但没有为了概念做多层工具发现。工具数量很少，所以 Schema 一开始就给模型；真正大的内容按需披露：GitHub 源码通过 MCP 读取，Skill 的详细 References 只有需要时读取，上下文超限时 Diff 才压缩或分批。

---

## 五、上下文压缩与 Memory

### 60. 一次 Reviewer 调用的上下文由哪些部分组成？

> 可以记成三类：规则、当前证据、历史经验。规则是 System Prompt、Assignment、Skill 和工具 Schema；当前证据是 Diff、工具 Observation 和反馈；历史经验是当前仓库召回的 Memory。所有部分都会计入预算，不是只计算 Diff。

### 61. 什么时候触发上下文压缩？

> 先完整组装一次输入并估算 Token，能放进预算就完全不压缩。只有超预算才先处理 Diff；如果压缩后的单次输入仍然超限，再按 Hunk 分批调用 Reviewer。这个策略的原则是“小 PR 不动，大 PR 才降级”。

### 62. 第一次压缩具体做了什么？

> 它把 unified diff 中没有发生变化的上下文行删除，保留文件头、Hunk Header，以及全部新增行和删除行。也就是把“帮助人阅读的周边旧代码”先去掉，但不丢真正的变更。需要的周边源码可以再通过 GitHub MCP 按固定 Commit 读取。

### 63. 如果压缩后仍然超限，按 Hunk 分批是什么意思？

> Hunk 是一个文件中某个连续变更区域，由 unified diff 的 `@@ ... @@` 标记。分批不是把所有 Hunk 都塞进一次请求，而是把不同 Hunk 分成多次 Reviewer 调用，每一批都满足上下文预算，最后统一合并 Findings。所以单次窗口变小，但总调用次数可能增加。

### 64. 分批后总 Token 可能更多，为什么还叫上下文压缩？

> 真正的压缩发生在第一步：删除未修改上下文。分批是压缩仍不足时的容量兜底，它解决的是单次上下文窗口，而不保证总成本一定下降。简历中的 14.5% 统计的是超限样本经过完整策略后的累计输入 Token，因此已经把多批调用计算进去了。

### 65. 14.5% 这个数字是怎么得到的？

> 我在 50 条真实 GitHub PR 上记录完整输入和最终各批次输入。41 条本来能放下，不触发压缩；9 条超限，6 条最终需要分批。对这 9 条超限样本，把所有批次的输入 Token 加总后，相比直接重复携带完整输入平均降低 14.5%，同时全部新增和删除行仍被覆盖。

### 66. 为什么上下文预算是 12K？

> 这是应用层预算，不是模型的物理最大窗口。我故意给输出、工具 Observation 和后续迭代留出空间，避免一开始把窗口塞满。预算是可配置的，12K 让真实 PR 数据里既有直接通过的样本，也能覆盖压缩和分批路径。

### 67. Memory 里保存什么？

> 当前跨任务 Memory 只有两类。Episodic Memory 保存过去任务中的 Finding 结果和任务摘要；Semantic Memory 保存人工反馈，例如某类 Finding 被判为误报、漏报或接受。当前任务的工具 Observation 和 Checkpoint 不写进长期 Memory，它们属于 Runtime State。

### 68. 为什么没有强行保留 Working Memory 三层概念？

> 因为当前任务内状态已经由 Agent Loop 和 Checkpoint 管理，再复制成 Working Memory 只会概念重复。我的口径是：短期状态归 Runtime，跨任务案例归 Episodic/Semantic Memory，可复用流程归 Skill。这样边界更清楚。

### 69. Memory 是怎么召回的？

> 先按 repository 做硬过滤，避免不同仓库互相污染。查询由 Assignment 的目标、文件和风险域组成，然后做确定性的词项相关性评分，零重叠直接过滤，综合覆盖度、具体性、人工重要度和少量时序因素，最多取 6 条。注入 Prompt 前还会限制单条长度。

### 70. 为什么不用向量数据库做 RAG？

> 当前数据量小，而且路径、规则名、风险域和错误类型这些词很有辨识度，仓库过滤加词项召回足够可解释。为了在简历里出现 RAG 而引入向量库反而会增加复杂度。等 Memory 规模变大、同义表达明显增加时，我会考虑 BM25 加 Embedding 的混合召回。

### 71. 会把这个仓库的所有 Memory 都注入吗？

> 不会。先限制当前仓库，再按任务相关性取 Top-K，而且只注入压缩后的摘要。无关历史不会进入上下文，Memory 也不能挤占 System、Skill、工具 Schema和变更证据这些必需内容。

### 72. Memory 和 Skill 有什么区别？

> Memory 是过去发生过什么，比如“这个仓库某种警告曾经被人工判为误报”；Skill 是以后遇到某类任务应该怎么审，比如认证安全的检查流程和工具使用顺序。Memory 是案例，Skill 是版本化的方法。

### 73. 如何避免错误 Memory 反复误导模型？

> 第一层是仓库隔离和 Top-K，减少无关信息；第二层是记录来源和反馈类型，不把模型的所有猜测都当成知识；第三层是 Memory 只是参考，最终 Finding 仍要经过新增行校验和 Judge。更完整的生产方案还应增加过期、降权和人工删除机制，这是当前可以继续完善的地方。

---

## 六、Agent Skills 与自进化

### 74. 当前有多少个 Skill？分别是什么？

> 目前有三个正式审查 Skill：认证安全、数据库迁移和异步可靠性。数量不多是有意的，我希望每个 Skill 都有明确触发信号、审查流程、工具指导、References 和评测案例，而不是堆很多只有一句 Prompt 的 Skill。

### 75. Skill 是怎么选择的？

> Agent Loop 开始前，Selector 根据 PR 的风险域以及路径和 Diff 中的 signals 做确定性匹配。命中认证相关信号就加载认证安全 Skill，命中迁移文件就加载数据库迁移 Skill，一次可以激活多个；没有命中就使用基础 Reviewer。它选择的是审查方法，不是在判断最终有没有问题。

### 76. 为什么 Skill 不让 Agent 在每一轮随意切换？

> Skill 数量少、类别明确，而且任务一开始就能从 PR 特征判断领域。每轮重新选择会增加不确定性，也影响 Checkpoint 恢复的可复现性。所以任务开始时冻结 Skill 版本；循环中只能按需读取这个 Skill 已声明的 Reference，不能随意换成另一个 Skill。

### 77. 一个 Skill 的结构是什么？

> 每个 Skill 是一个正式目录，核心是 `SKILL.md`。顶部 YAML 放 name、description、domains 和 signals，正文放审查 Workflow、Tool Guidance 和 Reference 链接；`references/` 里是更详细的领域知识。Skill 不是一段随手拼接的 Prompt，而是可校验、可版本化的审查包。

### 78. Reference 是什么？

> Reference 是 Skill 自带的细节知识，比如认证安全中的会话固定检查表。SKILL.md 放短流程，Reference 放长说明，Reviewer 只有需要时才通过 read_skill_reference 读取。它是本地版本化 Markdown，不是网络搜索，也不是另一个 Skill。

### 79. Skill 是固定的吗？

> Skill 的类别目前是受控的，格式也固定，但内容和版本可以演化。系统自动改的是已有 Skill 的 Workflow、Tool Guidance 和 References，不会让模型在线上随意发明一个新类别。新 Skill 类别需要人工注册元数据和评测案例。

### 80. Skill Evolution 的完整流程是什么？

> Reviewer 或 Judge 的审查失败，以及人工标记的误报、漏报，会先沉淀为失败案例。某个 Skill 累积到阈值后，系统把当前 Skill 包和经过脱敏的案例交给 LLM，生成候选 SKILL.md 和 References。候选先做格式与安全校验，再在 Validation 和 Holdout 案例上回放，满足门禁才激活，否则只保存为 rejected 或 deferred 版本。

### 81. Skill Evolution 哪些环节用了 LLM？

> LLM 主要做两件事：根据案例修改候选 Skill 内容，以及在隔离回放中作为 Reviewer 运行候选策略。案例归档、候选格式检查、安全限制、指标计算、版本激活和回滚都是确定性代码。不是让一个 LLM 自己说“我进步了”就上线。

### 82. 哪些失败会进入自进化？

> 只有和已激活 Skill 有关系的审查失败才有资格，例如 Finding 因证据或 Judge 原因被拒，以及人工标记的 false positive 或 missed issue。网络超时、MCP 鉴权失败这类基础设施错误会标记为不适合演化，因为改 Skill 解决不了它。accepted 反馈进入 Memory，但不当作失败案例。

### 83. 为什么要累计 3 个案例才生成候选？

> 单个失败可能是偶然模型波动，直接改 Skill 很容易过拟合。默认 3 个是项目规模下的工程折中，让同类问题至少重复出现。生产环境中这个阈值应该根据案例量和误报成本调大，不把 3 说成通用最佳值。

### 84. Validation 和 Holdout 门禁怎么做？

> Validation 用来判断候选是否对目标问题有改善，Holdout 用来防止只记住这几个失败案例。当前回放使用单 Reviewer，比较 F1、严重问题召回和干净样本准确率；候选至少要有小幅提升，同时保护指标不能退化，才允许激活。它是版本门禁，不是完整生产质量评测。

### 85. 为什么演化回放不用完整 Multi-Agent 链？

> 门禁需要频繁运行，完整链路成本高，而且很难判断提升来自 Skill 还是 Router/Judge 波动。所以我用单 Reviewer 隔离 Skill 变量，先做低成本回归门禁；真正的大版本仍应该再跑完整 Evaluation Harness。

### 86. 这真的能叫“自进化”吗？模型权重又没变。

> 我说的自进化是运行策略资产的版本演化，不是模型训练。系统能从运行失败和人工反馈自动形成候选 Skill，自动回放、通过门禁后激活，也能回滚。它比人工直接改 Prompt 多了案例触发、版本管理和非退化验证，但我不会说成模型自己学习了新权重。

### 87. 为什么还需要人工反馈？

> 因为代码审查没有完全可靠的自动真值，误报和漏报最终仍需要开发者确认。人工反馈不是每条都从零标注，而是纠正系统最有价值的错误；系统负责把这些反馈结构化、聚类到 Skill，并用于后续版本门禁。

### 88. 如何回滚 Skill？任务恢复时会不会用到新版本？

> Skill 版本保存在 PostgreSQL，同一个 Skill 只有一个 active 版本。回滚只是把旧版本重新设为 active，不删除历史。任务创建时会冻结所用的 Skill 版本和模型信息，所以旧任务续跑仍使用原版本，不会因为线上刚激活新 Skill 而产生前后不一致。

---

## 七、Evaluation Harness 与量化指标

### 89. 你的 50 条真实 PR 数据从哪里来？

> 来自 Code Review Bench 的真实开源 PR，我把数据源 Commit 固定下来，再筛成 50 条统一数据集，同时用于上下文和质量评测。覆盖多个真实项目和多种语言，重点不是追求大规模，而是让输入、Golden Issue 和实验版本能够复现。

### 90. 173 个 Golden Issue 都是什么？

> 它们来自真实 PR 中经过人工确认的审查问题。173 条中有 139 条属于我当前评分范围内的 bug、security、concurrency、data 和 API 类问题，其余非核心评论不计入主质量分数；还有少量只包含非核心评论的 PR 作为负向控制。不能把 173 全部说成模型必须命中的同一种标签。

### 91. 为什么只用 50 条，数量是不是太少？

> 对论文结论来说当然不够，但对个人实习项目的一次冻结工程评测是可接受的。我的重点是把数据版本、开发集/测试集隔离、逐样本结果、断点续跑和指标计算做完整。结论只限定在这组数据，不外推成生产准确率。

### 92. 数据怎么划分？

> 按来源仓库做平衡划分，每个来源项目取 2 条开发样本和 8 条测试样本，总共 10 条 development、40 条 test。开发集用于调 Prompt 和门禁，测试集只做冻结评测，避免看着测试结果反复修改。

### 93. Golden Issue 没有统一行号，怎么和 Finding 匹配？

> 我先让生产审查链输出结构化 Finding，再用一个独立语义 Matcher 判断 Candidate 和 Golden 是否描述同一个底层缺陷或失败机制。然后做一对一匹配，一个 Finding 不能重复命中多个 Golden，一个 Golden 也只能被命中一次。Matcher 不重新审代码，只做问题语义对齐。

### 94. 用 LLM 做 Matcher，会不会自己给自己打分？

> 有这个风险，所以它和 Reviewer 使用不同 Prompt、只看紧凑的问题描述，并强制一对一输出和置信度。它比关键词匹配更适合自然语言 Golden，但不等于绝对客观。更严格的方案是人工抽样复核或使用不同模型做 Matcher，我会把 Matcher 版本和原始匹配结果一起保存，保证能够审计。

### 95. Evaluation Harness 统计哪些指标？

> 质量上统计 Precision、Recall、F1、高严重度召回和每个 PR 的误报数；工程上统计执行成功率、P50/P95 时延、Token 和 LLM 调用次数。Evidence Validator 和 Judge 还记录候选漏斗，能看到问题是在 Reviewer 没找到、证据门禁被拒，还是 Judge 被拒。

### 96. 简历为什么只写构建了评测，没有写 F1？

> 因为这次真实 PR 冻结测试的结果不够好，不适合包装成系统优势。简历第五点想表达的是我有一套可复现的评测基础设施，而不是声称模型质量领先。这个 Harness 的价值恰恰是把召回不足暴露出来，让后续优化有依据。

### 97. 如果面试官继续追问当前真实结果，你怎么回答？

> 我会如实说：40 条冻结测试任务执行成功率是 100%，但质量 Precision 约 42.9%、Recall 约 2.7%、F1 约 5.0%，高严重问题召回约 5.9%。163 个 Reviewer 候选中，Evidence Validator 拒绝了 116 个，Judge 又拒绝了 35 个，最终只剩 7 个，所以主要问题是门禁过严和 Reviewer 取证不足。这个结果不能证明模型效果好，但能证明 Evaluation Harness 找到了明确瓶颈。

### 98. 这么低的 F1，项目还有价值吗？

> 有，但价值要说准确。它不是一个已经超过成熟代码审查产品的模型，而是一个把运行、取证、门禁、演化和评测打通的 Agent 工程项目。低 F1 说明当前策略不适合直接上线，也给出了下一步优化方向；如果我隐藏这个结果，反而说明评测只是装饰。

### 99. “定位质量”是怎么算的？

> 运行时对每条 Finding 强制校验 path、line 和 evidence，必须落在 PR 的真实新增行上，所以最终 Finding 的结构定位是有效的。但原始 Golden Issue 没有统一 path-line 标注，因此我不会声称计算了对 Golden 的行号准确率。简历中的定位质量是评测维度和结构约束，不是一个已经公布的行级 Accuracy。

### 100. 为什么需要断点续跑？

> 40 条真实 PR 的评测耗时和 Token 成本都比较高，中途可能遇到 API 限流或进程退出。Harness 会逐样本写 `case-results.jsonl` 并刷新到磁盘，恢复时校验模型、数据源 Commit、代码 Commit 和 split，跳过已完成样本。这样既节约成本，也防止混合不同实验版本。

### 101. 质量评测是否开启了 GitHub MCP 和 Memory？

> 这次冻结测试没有开启 GitHub MCP，Memory 也是 cold-start，并把 Agent Loop 最大步数限制为 2，主要是控制公网依赖和实验成本。所以它更接近只基于 Diff 的严格冷启动测试，不能把结果描述成系统所有能力完全开启后的上限。

### 102. 这套数据和评测有什么局限？

> 第一，只有 50 条 PR，代表性有限；第二，开源静态语料可能被基础模型见过；第三，Golden 评论可能不完整，也没有统一行号；第四，语义 Matcher 自身有波动；第五，冻结测试为了成本关闭了 MCP 和热 Memory。这些限制都应该跟结论一起说。

---

## 八、FastAPI、Webhook、PostgreSQL 与 Redis Streams

### 103. Webhook 是什么？在项目里怎么用？

> Webhook 可以理解成 GitHub 主动调用我的 HTTP 接口。PR opened、reopened 或 synchronize 时，GitHub 把事件和 PR 信息推到 FastAPI；系统不需要不停轮询。接口验签、校验时间窗口和事件类型后创建任务，真正耗时的审查放到队列异步执行。

### 104. Webhook 怎么防伪造和重复投递？

> GitHub 用共享 secret 对原始请求体做 HMAC-SHA256，服务端用恒定时间比较验签；同时检查事件时间是否在允许窗口内。每次事件还有 X-GitHub-Delivery，PostgreSQL 对 delivery_id 做唯一约束。同一个 delivery 再来会返回已有 task_id；同一个 ID 如果对应不同请求内容会直接拒绝。

### 105. 为什么 Webhook 处理时不直接运行 Agent？

> Agent 审查可能要几十秒甚至更久，如果在 Webhook 请求里同步跑，很容易超时，GitHub 又会重投。接口只做安全校验、落任务和入队，然后快速返回 202。Worker 异步执行，用户可以按 task_id 查询状态。

### 106. PostgreSQL 保存什么？

> 它是正式运行模式的事实来源，保存任务、原始 Diff、状态、Trace、Checkpoint、Agent 消息、Webhook 投递、Memory、失败案例、评测案例、Evolution Run 和 Skill 版本。重要状态不放在进程内，所以服务重启后能够恢复。

### 107. Redis Streams 在项目里负责什么？

> Redis Streams 只负责异步任务投递，包含普通 review 和 skill-evolution 任务。Worker 使用 Consumer Group 读取消息，业务完成或已正确重新入队后才 ACK。任务最终状态仍以 PostgreSQL 为准，Redis 不是结果数据库。

### 108. Redis Streams 如何处理 Worker 崩溃？

> 消息被 Worker 读取后会进入 pending。如果 Worker 在 ACK 前崩溃，超过 lease 时间后，其他消费者通过 XAUTOCLAIM 接管。普通异常在最大次数内重新入队，永久错误或重试耗尽会把终态失败写进任务；然后原消息才 ACK。

### 109. 队列重新投递会不会导致任务重复执行？

> 队列层是至少一次投递，所以我不假设消息绝对只来一次。任务层通过 task_id、状态和 Checkpoint 做幂等：已经 SUCCESS 的任务不会重新审，完成的细粒度步骤也会直接复用。Webhook delivery_id 又解决了入口重复创建任务的问题。

### 110. 为什么正式模式只保留 PostgreSQL 和 Redis Streams？

> 早期 SQLite 和内存队列方便 Demo，但会让恢复和多 Worker 语义不一致。现在正式运行只有 PostgreSQL 和 Redis Streams，开发、演示和部署走同一条路径，避免出现本地能跑、正式模式另一套逻辑。代价是启动依赖更多，所以用 Docker Compose 做一行启动。

### 111. 项目如何一行启动？

> 根目录准备 `.env`，至少配置 DeepSeek Key；GitHub 集成再配置 Token 和 Webhook Secret。执行 `docker compose up --build` 会启动 FastAPI、Worker、PostgreSQL 和 Redis。数据库表由服务初始化，健康接口会显示 database、queue 和模型状态。

### 112. 这个系统能支持高并发吗？

> 我不会把它包装成高并发系统。当前架构支持横向增加 Worker，Redis Consumer Group 分配任务，PostgreSQL 保存共享状态；但 Reviewer 内部还有 LLM 限流、GitHub API 配额和连接池容量，需要做全链路压测才能给吞吐数字。简历没有写高并发，所以我只说具备扩展基础。

### 113. 有哪些安全边界？

> 入口层有 Webhook HMAC、重放窗口和请求大小限制；模型层把 Diff 和工具结果标成不可信数据，防止代码中的 Prompt Injection 改写系统规则；工具层只开放 GitHub 只读白名单，仓库和 Commit 由系统注入；输出层要求新增行证据并经过 Validator 和 Judge。当前没有执行仓库代码，也没有写仓库权限。

### 114. GitHub 评论怎么避免重复刷屏？

> 评论正文带有 CapyReview 的固定 marker。提交结果时先查 PR 现有评论，如果找到同一个 marker 就 PATCH 更新，找不到才 POST 新评论。GitHub API 对限流和部分 5xx 还做了有界重试。

---

## 九、压力问题、缺陷与行为问题

### 115. 做这个项目时最大的困难是什么？

> 对我来说最难的不是接模型，而是确定什么结果算“已经完成，恢复时不能重复”。一开始只在大阶段保存 Checkpoint，进程如果在工具或 Judge 调用后中断，恢复会重复花费。后来我把副作用边界下沉到 Tool Observation、Reviewer Final 和 Judge Decision，并用故障注入验证，这让我真正理解了 Agent Runtime 和普通工作流代码的区别。

### 116. 你遇到的第二个困难是什么？

> 第二个是上下文管理。最初我尝试给不同内容设计很多优先级，规则越来越难讲，也不一定真实有效。后来收敛成一个简单策略：完整输入能放下就不动；超限先只删除 Diff 的未修改上下文；还超限才按 Hunk 分批；Memory 做仓库过滤和 Top-K。这个版本更容易验证，也更容易维护。

### 117. Evaluation 暴露了什么问题？你怎么分析？

> 最大问题是召回过低。候选漏斗显示 Reviewer 产生了 163 个候选，Evidence Validator 拒绝 116 个，Judge 又拒绝 35 个，最终只有 7 个。说明系统不是完全没产生想法，而是证据引用不稳定、门禁过严，同时冻结评测关闭了 MCP。下一步应该先做门禁分层和证据修复，而不是盲目增加更多 Agent。

### 118. 如果让你继续优化，优先做什么？

> 第一，优化 Evidence Validator，把可修复的引用问题返回 Reviewer，而不是直接丢弃；第二，用开发集校准 Judge 的批准标准，并区分高严重度和普通问题的门槛；第三，在可控子集开启 GitHub MCP，比较取证对召回和成本的影响；第四，增加人工抽查语义 Matcher。等这些完成后再讨论更多 Skill 或 Reviewer。

### 119. 为什么不直接删掉 Evidence Validator 和 Judge，提高召回？

> 那样 Recall 可能会上升，但 PR 评论里的幻觉和误报也会显著增加。正确做法不是取消门禁，而是把门禁从“一刀切拒绝”改成可诊断、可修复的流程，例如行号可以自动校正，证据不足可以要求补一次 MCP 取证，高风险问题保留人工复核。我要优化的是精确率和召回的平衡。

### 120. 为什么不用一个更强模型直接解决所有问题？

> 更强模型可能提升审查质量，但解决不了任务重启后的副作用重复、Webhook 幂等、固定 Commit 取证、数据版本隔离和回归评测。这些是模型外部的系统问题。项目的设计允许以后替换模型，但不会把工程可靠性寄托在模型更聪明上。

### 121. Multi-Agent 会不会只是增加成本？

> 会增加，所以我没有所有 PR 都跑双 Reviewer。Risk Router 让普通 PR 只跑 Correctness，高风险才增加 Security，并且两者并行降低墙钟时间。是否值得最终要看高风险召回和调用成本的对照数据；当前架构提供了按风险控制成本的能力，但我不夸大尚未做的收益实验。

### 122. Skill、Memory、MCP、上下文压缩这么多概念，会不会过度设计？

> 如果它们职责重叠就是过度设计，所以我给每个组件设了清楚边界：MCP 获取当前仓库证据，Memory 召回过去案例，Skill 提供领域审查流程，Context Manager 只负责把这些内容放进预算，Runtime 管执行和恢复。任何模块如果不能对应独立输入、输出和测试，我都会倾向删除。

### 123. 当前项目最明显的不足是什么？

> 最明显的是质量效果还没达到可直接用于生产 PR 门禁的水平，尤其真实冻结测试 Recall 很低。另外数据集规模有限，Judge 和 Matcher 与 Reviewer 使用同一模型也存在相关性，Memory 召回还是简单词项方式。这个项目目前更适合展示 Agent 工程能力，而不是宣称替代资深 Reviewer。

### 124. 你做过哪些取舍？

> 我删掉了本地规则 Reviewer、自动修复、多租户、灰度发布和复杂观测等与简历主线无关的能力；正式模式统一为 PostgreSQL 和 Redis Streams。我也没有为了热点强加向量 RAG、LLM Router 或大量 MCP 工具。取舍标准是：能不能服务 PR 审查主链，能不能讲清输入输出，能不能被测试验证。

### 125. 如果面试官让你现场演示，你演示什么？

> 我会先用 `docker compose up --build` 启动系统，再提交一条可控 PR Diff 或重放 Webhook。重点展示任务状态、Router 路由、Reviewer 工具 Observation、Evidence/Judge 结果、Trace 和最终 Markdown 报告；然后在一个已准备的故障点演示取消或续跑。不会现场跑完整 50 条评测，因为耗时和 API 成本都不适合面试。

### 126. 如果只能保留一个模块，你保留哪个？

> 我会保留 Runtime Harness。Reviewer、模型和工具都可能替换，但任务状态、预算、Checkpoint、取消续跑和 Trace 是整个系统能可靠运行的基础。它也是这个项目和普通“一次 Prompt 调模型”最主要的区别。

### 127. 如果进入团队，你会继续自研还是迁移到 LangGraph？

> 我会先看团队已有基础设施和需求复杂度。如果现有 Runtime 的恢复语义已经稳定、流程仍然小，就不为换框架而换；如果要接大量子图、人工审批、长期挂起任务和更多团队协作，我会评估迁移 LangGraph。迁移时先保持任务状态和 Checkpoint 契约不变，再替换编排层。

### 128. 你从这个项目中最大的收获是什么？

> 我最大的收获是，Agent 系统的难点不只是 Prompt 和 Tool Calling，而是上下文、状态、副作用、证据和评测怎样组成一个闭环。模型输出不稳定是事实，工程要做的是给它明确边界、保存可恢复结果、记录过程，并用真实数据发现问题，而不是只展示一次成功 Demo。

---

## 十、必须背熟的数字卡片

- Runtime：30 轮细粒度故障恢复，Tool Observation、Reviewer Final、Judge Decision 各 10 轮；恢复、状态一致、Trace 完整均为 100%，恢复过程重复 LLM 调用为 0。
- Context：50 条真实 PR；41 条直接满足预算，9 条触发压缩，6 条继续分批；超限样本累计输入 Token 平均降低 14.5%，变更行覆盖与预算满足率 100%。
- Dataset：50 条真实开源 PR，10 条 development、40 条 test；173 个人工 Golden Issue，其中 139 条属于核心评分范围。
- 冻结质量测试：40/40 执行成功；Precision 42.9%、Recall 2.7%、F1 5.0%、高严重问题召回 5.9%。这组数字只在被追问时如实说明，不包装为简历亮点。
- 候选漏斗：Reviewer 163 个候选，Evidence Validator 拒绝 116 个，Judge 拒绝 35 个，最终 7 个；主要瓶颈是门禁过严与取证不足。
- Context 配置：应用层 12K Token 预算，预留 2.5K；这是项目预算，不是模型最大上下文。
- Memory：同仓库硬过滤，Episodic/Semantic 两类，最多召回 6 条。
- Skills：认证安全、数据库迁移、异步可靠性，共 3 个正式 Skill。

---

## 十一、容易说错的口径

面试时不要这样说：

- “Multi-Agent 把 F1 从某个数字提升到了某个数字。”当前没有有效的单 Agent 对照实验。
- “Risk Router 能判断 PR 有没有安全漏洞。”它只决定是否增加 Security Reviewer。
- “CapyReview 自己实现了 GitHub MCP Server。”项目是 MCP Client，并做了领域适配。
- “Memory 是向量 RAG。”当前是仓库过滤后的确定性词项 Top-K。
- “Agent 在循环里可以随意发现和切换 Skill。”Skill 在任务开始前选择并冻结，循环中只可读取已声明 Reference。
- “173 个 Golden Issue 全部参与 F1。”核心评分范围是 139 条，测试 split 中实际是 112 条。
- “定位准确率是 100%。”100% 是上下文中的变更行覆盖；Finding 只保证落在真实新增行，没有对 Golden 计算行级 Accuracy。
- “真实质量测试开启了全部 MCP 和 Memory。”冻结测试关闭 GitHub MCP，采用 cold-start Memory，Loop 上限为 2。
- “LangGraph 做不到细粒度恢复。”它能做；本项目自研是为了固定小流程和自定义持久化边界。
- “系统已经可以替代人工 Code Review。”当前 Evaluation 证明还不可以，项目价值主要是 Agent Runtime 和评测工程。

---

## 十二、30 秒极速回答模板

如果面试官时间很短，可以回答：

> CapyReview 是我做的 GitHub PR 审查 Agent Runtime Harness。它通过 Webhook 和 Redis Streams 异步接收任务，用状态机和细粒度 Checkpoint 管理 Reviewer Agent Loop；Risk Router 按 PR 风险决定运行 Correctness，还是并行增加 Security Reviewer。Reviewer 可以通过官方 GitHub MCP 获取固定 Commit 的代码和 CI 证据，Finding 再经过新增行校验和 Judge 复核。系统还支持大 Diff 上下文预算、仓库 Memory、版本化 Review Skills 和真实 PR 离线评测。我的重点不是训练模型，而是把一次不稳定的 LLM 审查变成可恢复、可追踪、可评测的工程流程。
