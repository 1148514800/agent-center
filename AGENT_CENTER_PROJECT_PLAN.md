# Agent 中控台项目规划书

**文档版本：1.0 · 编制日期：2026-11-15 · 产品目标版本：v0.1**

**交付对象：用户、本地开发 Agent。** 本文依据本次对话中确认的需求制定；未把其他项目、其他会话或既有个人配置作为前提。下文的实现方式和默认参数是本规划的设计决策，不是已经完成的功能。

**暂用项目名及命令名：`agent-center`。** 这只是新项目的工作名称，不指代已经安装的同名软件。正式版本以本地验收报告为准。

---

## 0. 执行摘要

本项目从空目录实现一个本地 Agent 中控台：

> 用户提出任务 → 统筹模型判断是否直接回答 → 必要时规划并选择本地 Agent → 中控台执行 CLI 调用 → 收集证据 → 统筹模型验证、修复或切换 Agent → 汇报结果。

首版技术路线为 **Python 3.14 + asyncio + SQLite + 轻量交互式 CLI**。统筹模型通过显式配置的模型 API 调用；执行端通过独立适配器接入 **Codex CLI 与 DeepSeek Harness CLI**。首版仅验收 macOS，保持核心代码与平台细节分离。

首版采用单进程、单活动任务、单活动 Agent 调用。允许在 Agent 执行时继续交流和提交需求变更，但变更在安全边界生效，不向一次性 CLI 会话强行注入新指令。每次调用启动新会话，中控台负责任务记忆。

必须自己实现的核心是：任务状态机、统筹循环、适配器契约、事件归一化、预算与审批边界、任务恢复、证据判断和 CLI。可以使用普通数据库、HTTP、校验、终端库；不 fork 一个现成中控台，不把 LangGraph、AutoGen、CrewAI 等编排框架当作项目的实际核心。

### 0.1 最重要的五项设计决策

1. **模型提建议，程序掌握执行权。** 模型不能修改预算、绕过审批、任意拼接启动命令或直接把任务标成成功。
2. **先做纵向闭环，再补齐首版。** 中间样机可以只验证一个 Agent；正式 v0.1 必须具备两个真实适配器以及本文规定的其他首版能力。
3. **中控台状态与原生会话分离。** 恢复任务不是复活进程，也不是重放上一条可能已经产生副作用的命令。
4. **完成必须有对应需求版本的证据。** Agent 退出码为零、声称成功、两个模型意见一致，都不能单独作为任务完成依据。
5. **权限能力不足时明确阻塞。** 不能因为 headless 审批不方便就使用 full-access、自动批准所有请求，或声称普通写沙箱能够阻止一切危险操作。

### 0.2 规划状态与验证边界

已经完成的是公开文档调研和设计。本报告没有连接用户的 Mac，没有运行用户的 Codex/DeepSeek CLI，没有读取其配置或验证其 API 账户。

用户已确认两个 CLI 在 macOS 上可正常使用；但具体版本、安装来源、非交互模式认证、可覆盖的模型参数和安全能力，仍必须由 M0 本机探测确认。公开上游资料不是本机兼容性的替代品。

---

## 1. 需求基线与范围

### 1.1 28 项选择的追踪表

| 编号 | 用户选择 | 首版落实方式 |
|---|---|---|
| Q01 | 先 CLI 接入，后续其他方式 | 两个一次性 CLI 适配器；不做桌面自动化 |
| Q02 | 先 CLI 界面，后 Web | CLI 与引擎解耦；不启动 HTTP 服务 |
| Q03 | 高度自主，简单问题自己答 | 模型选择直接回答或调度；关键风险由人工审批 |
| Q04 | 统筹模型通过 API | 显式 API 协议、Base URL、模型 ID、密钥引用 |
| Q05 | 首版两个 Agent | Codex 与 DeepSeek Harness 均须真实验收 |
| Q06 | 先串行，后并行及多轮协作 | 一次仅一个活动 Run；允许依次反复调用 |
| Q07 | 最终跨平台，先一个系统 | 核心逻辑不绑定 macOS；系统相关封装隔离 |
| Q08 | 首版 macOS | 真实验收 macOS；其他系统不得虚报支持 |
| Q09 | 开发任务验证，底层通用 | 验收目标抽象为 Criteria；软件测试只是首个类型 |
| Q10 | 自动目录与指定目录均支持 | 新建任务自动分配目录；现有项目显式指定路径 |
| Q11 | 常规操作自主，高风险审批 | 明确策略、后端能力检查、最小授权、拒绝降级 |
| Q12 | 历史保存与未完成任务恢复 | SQLite 状态、事件、检查点、恢复前对账 |
| Q13 | 自主决定验证深度 | 验收标准与证据要求固定；验证路径由模型选择 |
| Q14 | 用户配置预算上限 | 调用数、轮次、运行时长硬限制；费用注明估计范围 |
| Q15 | 新调用先独立，后原生会话 | 每次新的 CLI 调用；任务上下文由中控台构建 |
| Q16 | 持续对话与单次命令均支持 | `chat` 与 `run` 使用同一引擎 |
| Q17 | 运行期间可补充或修改要求 | 持久化输入箱、需求版本、安全边界重规划 |
| Q18 | 统筹模型少量基础工具 | 受限目录/文件读取、日志和状态查询；无任意 Shell |
| Q19 | 首版单任务，后多任务 | 独立 task_id；一个活动任务；无任务排队调度 |
| Q20 | 默认简洁，可详细查看 | 统一事件流、详细日志、脱敏与输出限制 |
| Q21 | 先人工登记能力，后自动学习 | 静态 Agent 描述；不做智能能力发现 |
| Q22 | 先集中配置 B，后智能选配 C | 首版中心配置模型/推理参数/启动参数；不退回纯原有配置 |
| Q23 | 先任务记忆，后长期记忆 | 不自动检索其他任务；配置不等于长期记忆 |
| Q24 | 有限重试与基本切换 | 错误分类、重试上限、同策略下切换 |
| Q25 | 前台先行，后后台服务 | 正常退出保存并终止子进程；异常退出恢复对账 |
| Q26 | 技术栈由规划决定 | 选择 Python，理由见第 3 节 |
| Q27 | 两个 CLI 已安装可用 | M0 验证非交互和权限，不重复安装、不擅自升级 |
| Q28 | 完整可观察决策记录 | 记录公开决策摘要、输入输出、工具、证据与审批；不索取隐藏推理 |

### 1.2 v0.1 必须交付

一个可安装、可测试、能实际使用的本地程序，同时覆盖：直接问答、双 CLI 自主选择与串行调用、基本交叉审查、集中配置、只读基础工具、执行中输入、需求变更、历史/恢复、预算、受限重试/切换、审批与日志。

“最小”指每项只实现必要语义，不是把用户已明确选择的功能删除。例如：支持一个简单的 `--verbose` 而非复杂终端看板；使用一张输入消息表而非消息中间件；使用明确的状态机而非可视化工作流平台。

### 1.3 明确不在 v0.1 内

Web/桌面界面、独立守护进程、多任务队列、并行 Agent、Git worktree 调度、Agent 自主互聊、连接已打开的 App、跨任务长期记忆、向量库、插件市场、自动模型能力排名、自动选择模型档位、远程 SSH 执行、云部署、多用户认证和自动更新。

不提前实现这些功能的半成品。只保留真正需要的接口边界。不得为了“未来扩展”引入微服务、Redis、Kafka、Kubernetes、复杂 DAG 编辑器或通用插件运行时。

---

## 2. 调研结果与借鉴边界

调研日期为 2026-11-15。以下是公开文档层面的核实，不是所有仓库的全面源码审计。来源索引见附录。

| 项目/资料 | 已核实的有关事实 | 本项目采用的思想 | 不采用的部分 |
|---|---|---|---|
| Codex 非交互文档 [S01] | 提供 `codex exec` 和 JSONL 事件 | 使用机器接口，不抓取 TUI 画面 | 首版不做原生会话恢复 |
| DeepSeek headless 文档 [S03] | 提供一次性任务、JSON 事件；无实时审批回传通道 | 归一化执行结果，明确审批限制 | 不接入全部插件体系 |
| MCO [S06] | CLI-first，用户选定 Agent/模型后协调执行与比较 | 适配器、doctor、保留证据 | 不照搬其显式选团队的交互；这里由统筹模型选择 Agent |
| OpenAI Symphony [S07] | 语言无关的调度/运行规范，强调运行状态对账 | 单一状态写入者、启动前对账 | 不引入工单平台、常驻服务和其实现代码 |
| LangGraph [S08] | 提供持久化检查点与中断恢复概念 | 将审批、恢复设计为显式状态 | 不把框架作为本项目运行时 |
| Cezar [S09] | 本地/服务器多 Agent 控制台，包含工作流等较广范围 | 观察最终可扩展方向 | 不复制其广泛首发功能 |

### 2.1 三个会影响实现的核实结果

**一次性 CLI 不等于可在线审批的 Agent 服务。** 首版统一采用“受限执行 → 发现授权缺口 → 保存并等待 → 用户批准后按新作用域启动下一次调用”。完整实时审批桥接后续再接 Codex App Server 或 DeepSeek ACP/SDK；Codex App Server 的审批请求/响应属于另一种协议能力。[S02][S03][S04]

**相同的权限名称不意味着相同的安全边界。** DeepSeek 文档明确其进程 SandboxMode 主要描述文件影响，网络和进程可见性不包含在该语义中。不得把 `workspace-write` 解释成“敏感读取与联网都已隔离”。[S05]

**GUI/交互登录不保证 headless 认证可用。** DeepSeek headless 默认认证路径可能不同于 GUI。M0 必须验证真实非交互调用；需要认证覆盖时使用本次调用的配置覆盖，不改动用户原有配置。[S03][S04]

---

## 3. 技术选型

### 3.1 推荐技术栈

| 层 | 选择 | 约束 |
|---|---|---|
| 运行时 | Python 3.14，锁定实际验证的补丁版本 | 不用 macOS 系统 Python；不强制升级已安装的 Agent |
| 依赖与运行 | uv、`pyproject.toml`、`uv.lock` | 交付锁文件；安装验证使用 `--locked` |
| 异步调度 | 标准库 `asyncio` | 用户输入、模型请求、输出读取、计时器不能互相阻塞 |
| 子进程 | `asyncio.create_subprocess_exec` | 参数数组；不经 `shell=True` 或拼接 Shell 字符串 |
| 持久化 | 标准库 `sqlite3` | 小事务、单写入者、模式迁移、本地磁盘 |
| 配置 | TOML + `tomllib` | 严格未知字段检查；密钥仅引用 |
| 数据校验 | Pydantic | 决策、事件、配置均强校验 |
| API 客户端 | HTTPX | 超时、重试、TLS、请求脱敏集中封装 |
| CLI 参数 | 标准库 `argparse` | 不引入额外 CLI 框架 |
| 交互输入 | `prompt_toolkit` | 异步输入及输出与提示符分离 |
| 质量工具 | pytest、pytest-asyncio、Ruff、mypy | 用 mock 与合成子进程做离线测试；真实测试单独标记 |

Python 3.14 处于官方支持版本范围；Python 原生异步子进程接口和 SQLite 能覆盖首版主要系统工作。`prompt_toolkit` 文档提供 `prompt_async()` 与 `patch_stdout()` 的协作方式；uv 提供锁定依赖后运行的机制。[S10][S11][S12][S13][S14]

### 3.2 为什么这次选择 Python，而不是 TypeScript

这是针对本项目的设计取舍，不是语言优劣排名。

当前工作重心是状态机、进程监管、SQLite、文件读取和测试，而不是前端。Python 可以用标准库完成其中多数基础工作，不必为终端首版引入前端构建链或额外数据库服务。未来 Web 客户端可以独立采用 TypeScript，通过 HTTP/SSE 访问同一应用服务，不要求前后端语言一致。

不采用大型 Agent 编排框架，是因为当前一个统筹模型、一个活动任务和一个活动 Run 用明确循环更容易检查、测试与修改。后续只有出现可证明的需求，才讨论引入更多抽象。

### 3.3 模型 API 协议

首版实现一个 **OpenAI 风格 Chat Completions + function/tool calling** 驱动，但“不绑定供应商”不等于宣称兼容所有模型。

配置必须包含 `protocol`。M0 真实测试模型能否稳定返回工具调用、是否支持所需输出上限字段和 usage。只支持 Responses 或其他原生协议的模型不能伪装兼容：输出明确的 `unsupported_protocol`。未来通过 `ModelClient` 新增驱动，而不是在原驱动里堆叠猜测。

采用 API 的显式工具调用结构，应用层仍独立校验参数；不从自然语言中用正则“挖”JSON来执行动作。官方工具调用也是模型提出调用、应用执行并回传结果的多轮机制；不同模型和端点能力需要区分。[S16]

若用户实际 API 不支持首版协议，本机报告应列为明确接入阻塞项；不得擅自换供应商、模型或账号。可以继续完成离线实现，但不能把真实统筹验收标为通过。

---

## 4. 总体架构

以下为文字结构示意，不要求绘制图片：

```text
用户
  |
CLI: chat / run / history / resume / logs / doctor
  |
Application Service（界面无关的入口）
  |
Task Engine / 单一状态写入者
  |-- 输入箱：接收问题、变更、暂停、审批
  |-- Supervisor：构建上下文，调用统筹 API，校验决策
  |-- Policy + Budget：程序端放行或拒绝
  |-- Verification：证据与验收标准
  |-- Store：任务、事件、检查点
  |
AgentAdapter
  |-- CodexExecAdapter
  `-- DeepSeekHeadlessAdapter
          |
     ProcessRunner
          |
     本地 Agent CLI → 授权的任务工作目录
```

统筹模型与本地 Agent 的职责不同：模型决定下一步“做什么”；中控台决定这一步“是否允许以及如何执行”；Agent 完成具体项目工作。

### 4.1 首版模块

| 模块 | 负责 | 不负责 |
|---|---|---|
| `cli` | 输入、简要展示、交互命令 | 不能直接改任务数据库状态 |
| `application` | 创建任务、提交消息、查询、恢复 | 不含 CLI 特有代码 |
| `engine` | 状态转移、调度循环、Run 生命周期 | 不硬编码各 CLI 的事件 |
| `supervisor` | 模型客户端、上下文构建、决策解析 | 无任意 Shell、无权限自我提升 |
| `adapters` | 原生配置映射、启动规格、事件归一化 | 不定义全局预算与任务成功 |
| `runtime` | 进程、双输出流、终止、平台封装 | 不理解任务是否完成 |
| `policy` | 访问范围、授权作用域、禁止项 | 不能把提示词当安全沙箱 |
| `storage` | SQLite、日志文件、迁移和备份 | 不独立启动调度循环 |
| `verification` | 验收标准、证据、新鲜度、结束条件 | 不固定“开发后必审查”的流水线 |

不将上述模块拆成多个服务。先是同一 Python 包里的清晰文件边界。

### 4.2 建议项目目录

```text
agent-center/
  pyproject.toml
  uv.lock
  .python-version
  README.md
  config.example.toml
  src/agent_center/
    cli.py
    application.py
    engine.py
    domain.py
    config.py
    supervisor/
      client.py
      context.py
      decisions.py
      prompts.py
    adapters/
      base.py
      codex.py
      deepseek.py
    runtime/
      process.py
      platform.py
      events.py
    policy/
      permissions.py
      approvals.py
      budget.py
    storage/
      database.py
      migrations/
      artifacts.py
      redaction.py
    verification.py
  tests/
    unit/
    integration/
    fixtures/
    live/
  docs/
    ARCHITECTURE.md
    COMPATIBILITY.md
    SECURITY_BOUNDARIES.md
    ACCEPTANCE_REPORT.md
```

文件数量可以按实际代码规模合并。不要为了符合目录图创建没有业务价值的空类、空插件和空服务。

---

## 5. 核心领域模型与不变量

### 5.1 最小领域对象

| 对象 | 关键字段 |
|---|---|
| Task | task_id、goal、workspace、status、requirements_version、state_version、config_snapshot、budget |
| UserMessage | message_id、task_id、sequence、text、intent、received_at、applied_at |
| Plan | plan_version、requirements_version、有序步骤、每步目标/完成标准/结果 |
| AgentDefinition | agent_id、adapter、名称、人工能力描述、默认配置档、启用状态 |
| AgentProfile | 模型 ID、推理参数、认证引用、受控启动参数、权限要求 |
| Run | run_id、task_id、agent_id、profile_snapshot、需求/计划版本、进程身份、状态、错误、资源用量 |
| Event | event_id、task_id、run_id、sequence、type、timestamp、payload、schema_version |
| Approval | approval_id、作用域、目标路径、原因为何、授权粒度、需求/配置版本、决定、失效条件 |
| Criterion | criterion_id、需求版本、描述、验证方式、状态、evidence_ids |
| Evidence | evidence_id、来源 Run、命令/输出/退出码、文件状态指纹、证据等级、完整性 |

第一版可以把 Plan、Criterion 等作为受校验的 JSON 放在少量表里，不必为每个概念建立复杂表关系。

### 5.2 必须一直成立的约束

- 一个数据目录同时只能有一个持有调度锁的中控进程。
- 一个活动 Task 最多一个活动 Agent Run；进度问答不是另一个执行任务。
- 每个 Run 使用固定需求版本、配置快照和权限边界；执行中不偷偷改变。
- 所有状态变化通过引擎提交；渲染器和模型回复不能直接写状态。
- 审批来自用户输入，不来自 Agent 报告、项目文件、模型判断或工具输出。
- 未处理的需求变更存在时，不允许提交 `succeeded`。
- 达到预算、权限不满足或上一个进程未清理时，不启动新 Run。
- CLI 正常完成与任务完成是两个判断层级。
- 读取其他任务日志不会自动进入当前任务上下文；跨任务信息不主动检索。

### 5.3 Task 与 Run 状态

Task 主状态：

```text
created → planning → running → verifying → succeeded
                       ↑          |
                       +-- 修复 --+

非终态：waiting_approval / paused / blocked / interrupted
终态：succeeded / failed / cancelled
```

`blocked` 表示外部条件或资源不满足，可在用户解决后恢复；`failed` 表示本轮任务终结，另行重试需显式操作。`succeeded` 后再增加功能，创建新的 Task，不修改过去已经完成的历史。

Run 状态：

```text
prepared → starting → running → completed
                         |---- failed
                         |---- timed_out
                         |---- cancelled
                         `---- unknown
```

`unknown` 表示异常退出后无法证明副作用边界；不等于 failed，更不能直接重放。

---

## 6. 统筹循环与基础工具

### 6.1 统筹决策采用有类型的动作

建议暴露少量函数工具：`inspect`、`update_plan`、`delegate`、`request_approval`、`finish_task`。模型不调用工具时可以生成直接回答；`blocked` 是引擎可根据错误或模型建议设置的状态。

所有动作携带短的公开决策说明 `reason_summary`，不要求思维链。工具参数通过 Pydantic 校验，未知字段、越界路径、未知 Agent、配置档篡改均拒绝。

示例为**本项目内部协议草案**，不是某个 CLI 的原生协议：

```json
{
  "action": "delegate",
  "task_id": "task_001",
  "requirements_version": 2,
  "plan_version": 2,
  "step_id": "step_003",
  "agent_id": "codex",
  "objective": "增加深色模式，并更新对应测试",
  "acceptance_criteria": ["criterion_004", "criterion_005"],
  "reason_summary": "该步骤主要修改实现代码，先交给已配置的编码 Agent。"
}
```

首版模型能选择 Agent，但不能选择新的模型档位或任意启动参数。引擎根据 agent_id 取中心配置中的默认 profile；这是“先 B 后 C”的明确实现。

### 6.2 程序端执行循环

```text
读取已提交的状态
→ 优先处理停止/审批等控制事件
→ 应用待处理的用户变更
→ 检查预算、工作目录和权限能力
→ 构建受限上下文，调用模型
→ 校验模型动作与状态版本
→ 执行动作 / 拒绝并返回结构化错误
→ 持久化事件与新状态
→ 继续，直到完成、阻塞、暂停或取消
```

模型一次返回多个工具调用时，程序按顺序验证和处理；第一个状态变化后，后续动作若版本已过期就重新规划，不允许暗中变为并行执行。API 的工具调用回传必须保留正确的 call_id。

流式模型响应必须完整接收并完成工具参数校验后才执行，不能在 JSON 尚未闭合时提前产生副作用。

格式错误最多纠正一次，然后停止当前决策并报告 `invalid_model_output`。纠正请求计入模型轮次和预算。不能无限“请再输出一次 JSON”。

### 6.3 统筹模型首版工具

允许：列目录、读取限定范围文本、搜索文件名与有限文本、读取任务状态/历史事件、查看 Agent 描述与配置摘要。

不允许：任意 Shell、直接编辑项目文件、安装软件、读取密钥、扫描浏览器数据、访问其他任务内容、直接调用网络抓取工具。需要这些实际执行能力时由合适 Agent 在已批准边界内完成。

文件读取要校验真实路径，限制符号链接逃逸、字节数、目录深度、文件类型；忽略大体积构建产物。先检查权限再读取，不能读完后仅在显示时遮盖敏感内容。

### 6.4 任务上下文包

每次 Agent 调用至少包含：当前目标、用户明确约束、需求版本、该步骤目标、完成标准、工作目录、允许范围、必要的前序摘要、证据引用、已有失败及禁止重复的做法、期望报告字段。

不要把完整历史全部塞给每个 Agent。摘要必须由事件索引支持；最新需求、未决审批、预算、验收标准不能因摘要压缩而丢失。项目说明和 Agent 输出是数据，不得提升为系统指令。

---

## 7. 两个 CLI 的接入方案

### 7.1 统一适配器契约

建议接口如下，名称可以微调，语义不能省略：

```python
class AgentAdapter(Protocol):
    async def probe(self, definition) -> CapabilityReport: ...
    def build_launch(self, request, profile, policy) -> LaunchSpec: ...
    def normalize_event(self, raw_event) -> list[AgentEvent]: ...
    def classify_result(self, exit_status, events) -> AgentRunResult: ...
```

`LaunchSpec` 包含绝对 executable、argv 数组、cwd、白名单 env、stdin 字节、超时和解析器版本。ProcessRunner 统一启动、读取、终止进程；不能由每个适配器独立实现一套不一致的进程管理。

CapabilityReport 至少报告：版本、安装来源、JSON 输出、非交互认证、模型覆盖、推理参数覆盖、写范围、敏感读/网络约束、审批通道、子进程终止和对应验证证据。区分 `supported / unsupported / unverified`。

### 7.2 CodexExecAdapter

使用 `codex exec` 的 JSONL 输出；需求通过标准输入传递，工作目录由 cwd/已验证的目录参数指定。新调用不使用 resume，不复用 `--last`。启动参数必须以本机 `--help` 为准。

命令形态示意，不是可在未探测机器上直接照抄的最终命令：

```text
codex exec --json --sandbox workspace-write ... < prompt.txt
```

必须明确指定适合非交互、不会自动批准越权的策略；不依赖机器上的宽松默认值。读取任务可以使用更窄的只读策略。官方当前资料已不建议沿用旧的 `--full-auto` 写法，因此不能依据旧博客硬编码参数。[S01]

完成判定读取退出状态与原生终结事件；可用时使用结构化最终输出，但 schema 合法不代表内容真实。原生 usage、命令事件、文件事件分别保留来源。

### 7.3 DeepSeekHeadlessAdapter

根据本机能力采用 `dsh --profile headless`/对应别名、`--json`、明确的 permission preset 和 `--approval-policy never`，任务走 stdin，cwd 指向任务目录。文档中的 `never` 是拒绝需要批准的动作，不是放开权限。[S03][S04]

模型、推理强度等通过本机支持的 profile/patch schema 映射。不能假定存在通用 `--model` 或照搬 Codex 参数。运行时覆盖文件使用中控台管理的临时目录，使用后清理，不能覆盖用户的全局 patch。

认证优先显式选择：验证能否使用已有认证；如果 headless 默认不使用该认证，生成最小本次覆盖或要求用户设置相应环境变量。不能读取、复制、展示用户 token。

Run 完成至少要求进程成功退出且最终事件 reason 为 completed；内部某个工具“调用完成”不代表该工具里的测试命令退出码为零。[S03]

### 7.4 集中配置的真实含义

中心配置是首版受管理参数的真相源：模型、推理参数、工作目录、超时、权限档。CLI 已有配置只能提供认证和明确许可的其他默认值；不能悄悄覆盖中心配置。

映射采用适配器白名单。不能把一个自由文本 `extra_args` 直接送进 Shell。用户手写受控参数若与沙箱、目录、模型等关键参数冲突，启动前报错。

每次记录三种状态：`requested`（中心请求）、`passed`（实际参数/覆盖）、`confirmed`（后端可确认）。CLI 不报告实际模型时，明确 `confirmed=unknown`；不得只因为配置文件写了模型就声称实际路由已验证。

### 7.5 兼容性与降级

新版本只在 doctor 和契约测试通过后使用；不自动升级 CLI。未知可忽略事件记录警告；缺失终结事件、协议畸形、关键字段缺失必须使本次结果不确定或失败。

不得以“先跑起来”为由降级到自动输入 TUI、解析彩色终端画面、无限等待权限提示或默认全权限模式。一个适配器阻塞不妨碍完成其他离线开发，但正式双 Agent 验收仍然未通过。


---

## 8. 终端交互与执行中修改需求

### 8.1 建议命令

下列命令属于计划实现的产品接口，不表示现在已存在：

```bash
agent-center doctor
agent-center doctor --live
agent-center agents
agent-center chat
agent-center chat --workspace "/absolute/path/project"
agent-center run "开发一个简单的待办事项程序"
agent-center run "修复测试失败" --workspace "/absolute/path/project"
agent-center history
agent-center show TASK_ID
agent-center logs TASK_ID --verbose
agent-center resume TASK_ID
```

`doctor` 默认只做本机静态检查，不调用付费模型；`doctor --live` 才执行明确标注的低预算真实探测。`run` 没有交互终端时不能等待隐藏的确认输入。

交互模式建议支持：

```text
/status              查看已确认进度，不调用模型
/plan                查看当前计划与需求版本
/logs                切换简要/详细显示
/add 新的要求         明确添加需求
/ask 当前情况如何     只读问答，不改计划
/pause               当前 Run 结束后暂停，不再启动新 Run
/stop                尽快终止当前 Run，保留可恢复任务
/cancel              终止任务，标记 cancelled
/approve ID          批准特定请求，展示后回显作用域
/deny ID             拒绝特定请求
/quit                保存、清理并退出，不留计划外后台进程
```

自然语言输入仍是主要方式，不要求用户每次使用命令。显式命令优先于模型意图分类；模型不能将普通讨论解释成批准风险请求。

### 8.2 单任务不意味着界面阻塞

采用一个事件循环内的异步协作：输入读取、Agent 输出读取、超时监控与 Task Engine 分离。只有 Task Engine 可以提交调度决策。

运行期间的 `/status` 直接读状态。自然语言询问可由同一统筹模型的“只读问答调用”回答，工具仅允许状态/日志读取，不能派发 Agent、改预算或推进任务。回答携带“截至事件 N/时间 T”的快照提示，避免把旧进度说成当前事实。

若运行中输入明显是一个独立的新任务，应说明首版只能运行一个任务，由用户先停止当前任务或完成后新建；不能擅自加入未实现的队列，也不能把无关目标悄悄合并进当前需求。

允许这种只读模型调用与 Agent 执行并存，不等于同时运行两个本地 Agent。状态决策仍由单一写入者串行提交。排队消息不逐 Token 启动模型调用，采用去抖/批次处理以控制消耗。

### 8.3 新要求的处理时序

例：当前 Codex 正在按 v1 需求实现页面，用户说“再增加深色模式”。

```text
1. 立即保存用户原话和 message_id。
2. 提示：已收到，待当前执行步骤结束后应用。
3. 当前 Run 保持 v1，不伪称已实时通知 Codex。
4. Run 退出并收集输出，处理输入箱。
5. 生成 requirements_version=2，必要时更新计划及验收标准。
6. 保留不受影响的 v1 证据，使受影响证据失效。
7. 使用 v2 上下文启动下一次 Run。
```

用户提出停止或明显收回授权时，不等普通边界：优先进入停止流程。进程终止可能有延迟和已经产生的副作用，界面须如实显示。

### 8.4 防止“刚改需求却被宣布完成”

最终提交 `succeeded` 必须在同一个数据库事务内检查：当前需求版本等于验证版本、没有待处理变更、没有活动 Run、没有未决审批、全部必要验收项已满足。

若用户消息在最终提交之前进入输入箱，成功提交失效，重新规划。若消息在任务已经结束之后到达，作为新任务或新请求处理，不篡改已完成历史。以数据库事件顺序定义先后，不依赖模型主观判断。

### 8.5 展示和日志

默认显示短决策说明、当前步骤、Agent、重要工具状态、审批、验证和结果。详细模式展示可观察的原生输出，但不能承诺每个 CLI 都提供完整命令细节或逐 Token 文本。

原生输出进入界面前进行转义与脱敏，防止终端控制序列影响提示符或伪造中控台审批提示。日志的“完整”指被系统接受的可观察业务记录，不要求保存隐藏推理、所有模型内部状态或无限大字节流。

### 8.6 非交互退出状态

建议：0=目标完成或简单问答完成；1=执行/验证失败；2=参数或配置错误；3=预算阻塞；4=需要人工输入/审批；5=兼容性或安全能力阻塞；130=用户中断。

非交互模式遇审批时落盘请求并返回 4，不默认同意，也不永久卡住。可通过交互 `resume` 查看审批后继续。

---

## 9. 权限、审批与信任边界

### 9.1 安全目标不能由提示词代替

首版是用户信任的本地开发工具，不是能够隔离恶意 CLI、恶意内核行为或所有供应链攻击的安全产品。

采用三层约束：中控台自身的工具/路径校验；调用前的权限和预算检查；CLI 原生可验证的执行隔离。仅仅以 cwd 启动进程不是沙箱；删除命令黑名单也不是完备隔离。

尤其要区分：禁止工作目录外写入，不等于禁止目录内误删；允许读工具链，不等于阻止读取所有敏感文件；关闭中控台网络工具，不等于子 Agent 不能联网。Codex 与 DeepSeek 的实际边界必须分别验证。[S05][S15]

### 9.2 首版默认策略

| 类别 | 处理原则 |
|---|---|
| 任务目录中的正常编辑、测试和构建 | 在已确认的 workspace-write 边界内自主执行 |
| 项目本地依赖安装 | 允许使用批准的来源与项目缓存；不能等价于任意全局安装 |
| 系统/工具链必要非敏感读取 | 限定范围、按任务需要；统筹工具严格校验 |
| 授权工作目录以外写入 | 默认拒绝；有明确需求时申请限定路径权限 |
| 受保护数据、秘密文件、浏览器资料 | 不作为普通任务输入；访问请求必须显式处理，默认拒绝 |
| 提权、修改系统设置、全局安装 | 首版不自动执行；转人工或标记 blocked |
| push、发布包、部署、发消息、外部上传 | 不纳入首版自动交付；需要另行明确授权与支持能力 |
| 破坏性操作、删除重要已有数据 | 不作为普通自动步骤；发现请求即暂停，不能切换 Agent 绕过 |
| 沙箱不可用或关键边界未验证 | 阻塞相应执行，不转全权限 |

依赖安装会执行第三方代码，不能承诺无风险。测试也可能执行项目脚本。首版自动执行只针对用户明确指定、可接受执行其代码的工作目录；未知仓库、生产数据或高敏环境应使用更强隔离，不能靠此首版默认模式承接。

已有项目启动前记录 dirty 状态。不得自动 `git reset --hard`、`git clean`、覆盖未提交修改或假设版本控制能恢复所有数据。不要为了运行 Codex 自动把整个用户目录初始化成 Git 仓库。

### 9.3 headless 审批的最小正确实现

一次性 CLI 中的内部请求可以被拒绝；中控台之后发起自身的审批，不是假装正在回复原生请求。

审批记录至少显示：具体原因、拟操作、目标路径/网络范围、有效期、下一 Run 的 Agent/配置、真实授权粒度和不可逆风险。将其绑定 `task_id + requirements_version + config_hash + scope`。

用户批准后，按允许范围创建**新的** Run。原 Run 的失败状态保留。授权默认仅本次 Run，有效期届满、需求改变、路径变化或配置变化即失效。

后端只能按目录授予权限时，必须写“本次 Run 可写此目录”，不能写“只允许执行这一条命令”。若用户要求的粒度无法落实，拒绝自动执行并转人工处理。不能用 `full-access` 冒充限定授权。

### 9.4 能力缺口处理

首版不承诺拦截任意 Agent 内部每一条命令的业务语义。比如任意脚本都可能删除一个已可写目录中的文件，简单 `rm` 黑名单无法提供完整保证。

对有明确“禁止删除/禁止读取/禁止外传”要求的任务，只有后端或独立执行环境能落实对应边界时才允许派发；否则将该任务标为 `blocked_security_capability`。可以给出用户手动完成的受限步骤，但不能偷偷放宽规则。

已有项目中若需要保护某些文件，应验证只读/拒绝访问规则；后端做不到时不能声称其已受保护。首版验收报告须分别列出：已强制、仅提示约束、未支持。只测试外部写入被拒绝，不能写成“完整安全验收通过”。

### 9.5 密钥和可信配置

中心配置保存 `api_key_env` 等引用；不保存明文 key。统筹 API 密钥不得传给 Agent 子进程。子进程环境采用明确白名单，不全量复制环境变量，不默认继承 SSH Agent、Docker Socket 等额外权限入口。

CLI 认证需要的凭证仅提供给对应可信 CLI；这并不天然保证其子命令看不到认证环境。必须通过 CLI 支持的认证/隔离机制确认边界；不能声称环境变量就是密钥保险箱。

运行日志、DB、提示词快照均本地保存，默认目录 0700、文件 0600。日志落盘前脱敏，覆盖分块边界；不要把全量环境、配置 dump、认证文件读入模型。已知密钥脱敏不能保证识别所有未知秘密，默认不收集是首要措施。

高权限配置只读自用户指定的中心配置。项目中的 `.env`、AGENTS.md、插件声明等属于不可信输入，不能自动提升中控台权限。记录各 CLI 是否加载项目/全局钩子或 MCP 等扩展；不能证明边界的扩展不在已验证执行档内。

---

## 10. 工作目录、持久化与恢复

### 10.1 目录布局

默认数据目录 `~/.agent-center`，可由显式参数或环境变量调整；数据库必须放本地磁盘，不放网络共享目录。SQLite WAL 依赖本机共享内存，不能把 WAL 数据库当作可随意共享的网络文件。[S17]

```text
~/.agent-center/
  config.toml
  state.sqlite3
  runtime.lock
  workspaces/
    task_<id>/
  tasks/
    task_<id>/
      runs/
        run_<id>/
          events.jsonl
          stderr.log
          summary.json
      exports/
  cache/
```

新任务自动创建唯一工作目录，不覆盖同名现有目录。已有项目使用用户明确指定的真实路径；拒绝 `/`、用户整个 home、CLI 配置根、中控数据根等危险默认目录。

中控审计文件放在工作目录外。需要向 Agent 提供材料时传摘要或只读引用，不默认赋予修改 DB/日志的权限。Agent 可写的 `report.json` 只能算自述材料。

### 10.2 SQLite 是唯一任务状态来源

最小表建议：`tasks`、`messages`、`runs`、`events`、`approvals`、`evidence`、`schema_migrations`。Plan/Criteria/Budget 可存受校验的 JSON。

选择“关系状态 + 追加业务事件”，不建设完整事件溯源框架。状态变化和对应事件同事务提交；终端只渲染已提交事件。原生日志文件用于诊断，不作为与 DB 并列的第二套调度真相。

SQLite 使用参数绑定、外键约束、明确事务、busy timeout；根据本机 SQLite 版本测试 WAL。事务中不能 await 模型/CLI/网络。小规模 DB 操作可以保持很短的单写入路径；导出、压缩等大 I/O 放到受控线程，不能阻塞输入。

备份使用数据库备份接口或正确关闭/检查点流程，不复制一个正在使用的 `.sqlite3` 文件而遗漏 WAL。[S13][S17]

### 10.3 启动与副作用之间没有天然原子事务

必须先写 `run_prepared`，再启动子进程，成功拿到身份后写 `run_started`。依然可能在任一边界崩溃，因此首版不承诺外部操作 exactly-once。

记录 run_id、PID、进程组、启动时间/身份、命令指纹、cwd、需求版本和最近事件。进程 ID 会复用，不能仅凭旧 PID 杀进程。

### 10.4 恢复算法

```text
取得调度锁
→ 读取非终态任务和未终结 Run
→ 验证旧进程身份；仍运行则先处理，不启动第二个写进程
→ 对无法确定结果的 Run 标记 unknown
→ 比较工作目录/文件变化与已有证据
→ 应用上次已经保存但尚未处理的用户输入
→ 给统筹模型构建恢复上下文
→ 在现有预算与权限下重新规划
```

只读、可证明幂等的检查可以自动重跑。涉及文件修改、发布、提交等副作用，必须先核实是否已经发生；不根据“未记录完成”推断“未执行”。不确定时保留现场、请求最小必要帮助。

恢复不自动清空原有预算和重试计数。用户选择提高预算时记录显式变更。

### 10.5 正常退出与异常退出

正常 `/quit`、SIGINT、SIGTERM：停止接收新调度，记录中断意图，停止活动进程组，等待回收，保存状态后退出。`/pause` 则允许当前 Run 结束，再暂停。

macOS 首版 ProcessRunner 采用独立进程组，先温和终止，宽限期后强制终止，持续读取 stdout/stderr，确认相关进程结束后才允许重试。不能只杀最外层 CLI 就宣布已停止。

SIGKILL、断电或程序崩溃时，不能保证及时写最后一个事件或自动消灭所有子进程；恢复必须识别可能遗留的进程。主动脱离进程组的恶意子进程不是首版进程管理可以完全控制的对象，这也是本项目不宣称安全隔离所有不可信代码的原因。

### 10.6 输出流与日志大小

stdout/stderr 必须同时异步读取，避免一条管道填满导致死锁；不要把全部输出一次性读入内存。Python 子进程文档明确说明了 PIPE 与 wait 的这类风险。[S11]

按来源解析 JSONL，不能对嵌套工具输出再次解释为顶层协议。允许未知非关键事件，但关键终结缺失不得标成功。日志设置容量上限，接近上限时可压缩/滚动；无法继续持久化关键证据时停止本次调用并报 `log_capacity_exceeded`，不能悄悄丢掉失败日志。

中控台不能使被 CLI 自身截断的字段恢复完整，需保留 truncated 标志；依赖该字段的验证必须标注证据不足。

---

## 11. 资源预算、重试与故障切换

以下数值是建议的可修改初始值，不是用户已经确认的固定额度。

| 参数 | 建议初值 | 语义 |
|---|---:|---|
| max_agent_calls | 12 | 包括失败尝试、审查与切换后的调用 |
| max_supervisor_calls | 40 | 包括进度问答、修复格式请求和规划 |
| max_step_retries | 2 | 同一逻辑步骤首次执行之外的重试总数 |
| per_run_timeout_seconds | 900 | 单次 Agent 运行总时限 |
| max_active_seconds | 3600 | 任务活跃执行/规划/重试等待时间累计；审批/手动暂停不计 |
| model_request_timeout_seconds | 120 | 单次统筹 API 请求时限 |
| termination_grace_seconds | 5 | 正常停止后的清理宽限期 |
| max_log_bytes_per_run | 52428800 | 示例 50 MiB 软告警/硬阻塞策略 |
| max_model_input_bytes | 131072 | 请求构造安全上限；不能假装这就是精确 token 数 |
| max_estimated_api_cost | 未配置 | 只有显式价格表和可靠用量策略下启用 |

`max_active_seconds` 的暂停、重启、重试退避计时规则必须在测试中固定。持久化累积值；运行中使用单调时钟，跨重启采用可解释的补算策略。系统时间变化不能让计时器变为负数。

### 11.1 程序负责上限，不由模型自行遵守

调度前检查剩余额度；启动前预占一个调用名额，保证异常重试不会漏记。运行到硬时限进入停止与清理流程，不能只阻止“下一次”调用而让当前进程继续无限运行。

连续只读查看、更新计划、模型重试也必须计数。Agent 调用为零不等于统筹 API 不消耗预算。

### 11.2 费用统计的诚实边界

统筹 API 费用与本地 Agent 订阅/额度分开记录。支持 usage 时记录真实 token 数据；价格来自用户配置并注明日期。CLI 未报告费用就显示 unknown，不填写 0。

单次 API 请求可能在返回 usage 前已经计费。首版所谓费用上限默认是“调度侧估算与止损”，不是供应商账单绝对保证。可以按输入估计和输出最大量预留额度；无法给出可靠上界时，不允许把它称为严格美元硬上限。

不将取消后的未知用量自动退款到预算。要绝对账单限制，需要服务商侧额度控制；首版不假设所有 API 都提供该能力。

### 11.3 错误分类

| 类别 | 处理 |
|---|---|
| API 临时超时、部分 5xx、可恢复限流 | 有上限退避；尊重 Retry-After；计入预算 |
| 认证/配置错误、未知模型 | 不盲目重试；提示修复配置 |
| 子进程无法启动、协议错误 | 查兼容性；不能当作编码能力不足而无限换 Agent |
| 沙箱拒绝/审批未授予 | 请求授权或阻塞；禁止换 Agent 绕过 |
| Agent 完成但测试失败 | 统筹读取证据、调整要求，可修复或换 Agent |
| 进程超时且副作用未知 | 清理并对账，之后才决定是否重跑 |
| 相同失败反复出现 | 停止无变化循环，输出具体阻塞与已尝试方案 |
| 磁盘满/DB写入失败 | 停止新工作，尽力清理，不继续生成无法审计的副作用 |

可切换 Agent 解决模型能力或可用性问题，但继承同一任务的权限、需求、预算和证据规则。Agent 原生内部重试次数不一定可观察，不能冒充已控制它的每次底层请求。

---

## 12. 验证与成功标准

### 12.1 三层结果必须分开

**进程层**：有没有退出、退出状态是什么、是否因超时被终止。

**Agent 层**：原生最终事件是否完成、报告是否完整、哪些内部工具失败。

**任务层**：当前需求版本的每项验收标准是否有足够证据，是否还存在未处理变更。

只有第三层满足，Task 才能成功。统筹模型可以选择验证深度，不能修改事实或把未运行测试改写为通过。

### 12.2 证据分级

| 等级 | 例子 | 能证明什么 |
|---|---|---|
| E0 自述 | Agent 说“测试通过” | 仅记录一个声明 |
| E1 原生执行记录 | CLI 工具事件中的命令、退出码、输出 | 在可信 CLI 假设下，某次检查被执行 |
| E2 交叉核对 | 原生记录与实际文件、哈希、测试报告对应 | 证据与当前成果相符 |
| E3 独立重验 | 另一次受控运行对当前成果重新检查 | 更强的验证，但仍非绝对正确性证明 |

普通开发任务至少具备与必要验收项相符的 E1/E2 证据。关键检查只有 E0 时应补验或阻塞。没有测试框架时，定义可复现的最小行为检查，不能以“项目没测试”为由自动完成。

第二个 Agent 审查是可选路径，不是每次固定程序。独立验证应尽量只读；测试缓存等必要写入单独声明，若无法限制报告目录，不能声称这是绝对只读审查。

### 12.3 证据与版本绑定

证据关联 requirements_version、计划步骤、Run、命令、时间和文件状态指纹。Git HEAD 不足以代表脏工作树；未提交/未跟踪文件也需要考虑。

后续修改影响到的检查必须失效并重验。文件变化范围无法判断时，重新运行相关完整检查，而不是乐观保留旧通过结论。工作期间用户手动编辑文件同样使证据需要复核。

Agent 修改测试让它通过不当然代表修复正确；统筹需检查验收标准是否被削弱。发现两 Agent 结论冲突时按证据处理，不以多数票判断。

### 12.4 最终报告

至少包含：完成了什么、主要产物路径、变更文件、执行过的验证及证据引用、未执行检查、失败/阻塞项、需求变更应用情况、资源用量（包括 unknown）、下一步建议。

“部分完成”与 `succeeded` 不混用。可以交付有价值的产物，但必须保持真实 Task 状态。

---

## 13. 配置规范草案

以下为中控台自己的 TOML schema 草案，不是 Codex 或 DeepSeek 原生配置；字段由本项目实现。占位值会被 doctor 检出，不允许带着占位值发真实请求。

```toml
schema_version = 1

[app]
data_dir = "~/.agent-center"
default_workspace_root = "~/.agent-center/workspaces"
platform_target = "macos"

[supervisor]
protocol = "openai_chat_completions"
base_url_env = "AGENT_CENTER_BASE_URL"
api_key_env = "AGENT_CENTER_API_KEY"
model = "REPLACE_WITH_AVAILABLE_MODEL_ID"
request_timeout_seconds = 120
max_output_tokens = 4096

[limits]
max_agent_calls = 12
max_supervisor_calls = 40
max_step_retries = 2
per_run_timeout_seconds = 900
max_active_seconds = 3600
termination_grace_seconds = 5
max_log_bytes_per_run = 52428800
max_model_input_bytes = 131072

[policy]
default_mode = "workspace-write"
approval_default = "deny"
allow_unverified_capabilities = false
allow_full_access = false
allow_global_install = false
allow_external_publish = false

[agents.codex]
enabled = true
adapter = "codex_exec"
executable = "codex"
description = "根据实际使用情况填写的编码、修复与审查能力描述"
default_profile = "codex_default"

[agents.deepseek]
enabled = true
adapter = "deepseek_headless"
executable = "dsh"
description = "根据实际使用情况填写的分析、开发与审查能力描述"
default_profile = "deepseek_default"

[profiles.codex_default]
agent_id = "codex"
model = "REPLACE_WITH_AVAILABLE_CODEX_MODEL_ID"
auth_mode = "existing_cli"
permission_mode = "workspace-write"

[profiles.deepseek_default]
agent_id = "deepseek"
model = "REPLACE_WITH_AVAILABLE_DEEPSEEK_ROUTE_ID"
auth_mode = "existing_cli"
permission_mode = "workspace-write"
```

`auth_mode=existing_cli` 表示请求复用现有认证，而不是承诺 headless 必然支持。探测失败必须报告并给出受限配置步骤，不擅自回退到其他凭证。

可选推理参数如 `reasoning_effort` 仅在对应适配器确认支持时生效；不支持就报错，不静默丢弃。Base URL 必须显式设置；远程 HTTPS 验证默认开启，不能为“解决连接问题”关闭证书验证。

中心配置的有效值、适配器版本、策略版本都生成哈希保存在 Run 快照中；真正的秘密不参与可显示快照。

---

## 14. 开发阶段与阶段门槛

阶段按能力依赖划分，不设人为工期承诺。每阶段都有可演示的结果，失败时应保留已完成的功能，而不是推倒重做。

### M0：环境探测、兼容性与安全边界

**目标**：先证实“能接”，不要先写几千行再发现 CLI 无法自动调用。

工作：确认 macOS/架构、Python/SQLite；定位两个 CLI 的绝对路径、版本和安装来源；保存脱敏 help；确认非交互认证、JSON 输出、模型覆盖和权限；使用合成临时目录验证成功、失败、取消及外部哨兵写入限制。检查参数是否支持只作用于本次调用。

交付：`docs/COMPATIBILITY.md`、`docs/SECURITY_BOUNDARIES.md`、脱敏事件 fixtures、最小可重现探测步骤。

阶段门槛：每项能力明确 supported/unsupported/unverified。没有实际测试不能勾选通过。不可兼容时可继续离线开发，但不得通过静默降级伪造双 CLI 支持。

### M1：最小纵向样机

工作：项目骨架、配置校验、模型客户端、Decision schema、基础 AgentAdapter、ProcessRunner、最小 SQLite、单次 run、直接问答和一个真实适配器。

演示：用户问简单问题时不启动 Agent；用户给简单开发任务时统筹调用真实 CLI、读结果并回报；历史能查询。

阶段门槛：错误输出不执行动作；能取消并清理；所有初版动作保留基础事件。**M1 只是内部样机，不是最终 v0.1。**

### M2：双 Agent、集中配置与质量闭环

工作：补齐第二适配器、静态能力注册、中心模型/参数映射、计划、证据、统筹选择、自动修复、有限切换与交叉审查。

演示：两个 Agent 分别完成真实小任务；在同一目录完成“实现 → 审查/验证 → 修复”串行流程。路由选择不硬编码某个品牌只能开发或只能审查。

阶段门槛：模型配置能证明已传递；测试失败不会标成功；切换不改变权限。**此时得到双 Agent 核心闭环。**

### M3：交互、运行中输入与需求版本

工作：chat、异步输入、状态查询、只读进度问答、持久化输入箱、需求版本、安全边界应用、暂停/停止/审批终端交互、简洁/详细日志。

演示：Agent 工作时可以输入；追加新要求后明确提示生效时机；旧需求的结果不能直接终结新需求。

阶段门槛：完成边界竞态、取消与审批消息优先级测试；日志不破坏输入；没有双重调度。

### M4：预算、权限、故障与恢复硬化

工作：预算持久化、有限退避、运行停止、审批作用域、日志脱敏、磁盘异常、崩溃对账、工作区锁与异常进程处理。

演示：在多个关键点强制退出再 resume；保持需求、预算和审批记录；不重复未知副作用；不同时出现两个写进程。

阶段门槛：关键安全/恢复用例全通过，不用“测试没跑”代替成功。缺失外部能力时正确 blocked。

### M5：真实验收、文档与可复现安装

工作：干净环境安装、两 CLI 的限定真实验证、所有离线测试、CLI 使用说明、故障指南、验收矩阵、已知限制、构建产物。

交付：源代码、锁文件、配置示例、测试、COMPATIBILITY/SECURITY_BOUNDARIES/ACCEPTANCE_REPORT、安装说明及本地成果。

门槛：第 15 节与独立验收清单的 P0 项满足。真实测试被额度、认证、后端限制阻塞时报告 BLOCKED，不宣称全部通过。未授权不推远端、不发布、不修改系统级配置。

---

## 15. 验收策略

完整用例见配套 `ACCEPTANCE_CHECKLIST.md`。验收分为单元、合成进程集成、真实 CLI、端到端四层。

离线测试用自己编写的 fake CLI 覆盖：双流高输出、半行 JSON、无终结事件、退出非零、子进程树、忽略温和终止、停顿、超长日志和损坏事件。Fake CLI 不能替代真实适配器验收。

真实测试使用一次性目录、合成秘密、无私人数据和明确预算。至少包含：两个 CLI 各自正常完成、各自失败/受限行为、中心模型参数映射，以及由真实统筹 API 管理的一条端到端链路。

建议开发命令：

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked mypy src
uv run --locked pytest -m "not live"
uv run --locked agent-center doctor
uv run --locked pytest -m live
uv build
```

真实测试只在用户已配置认证且有明确运行授权时运行，不把整个 pytest 默认变为付费测试。测试结果写明 OS、架构、Python/CLI 版本、模型配置（脱敏）、命令、退出码和日志路径。

### 15.1 v0.1 的不可省略验收结论

- 简单问题直接回答且没有启动 Agent。
- 两个真实 CLI 均接入，统筹模型可以自主选择并串行调用。
- 同一任务里可在真实证据基础上验证、修复或切换。
- 运行中消息不丢失，需求在明确边界生效，最终报告对应最新版本。
- 中心配置生效或明确拒绝；不偷偷改用户原有 CLI 配置。
- 高风险/权限不足不绕过；没有审批时非交互模式安全退出。
- 预算在程序端生效，恢复不重置预算。
- 正常停止清理受管进程；异常恢复不盲重跑未知副作用。
- 日志、审批、证据和状态能追踪，敏感值不出现在测试日志中。
- 安装和测试可重复，真实与模拟结果分开报告。

### 15.2 报告用语

只允许 PASS、FAIL、BLOCKED、NOT_RUN、NOT_SUPPORTED，并附证据。NOT_SUPPORTED 只能用于明确不在首版承诺内的能力；首版必需能力缺失应为 FAIL/BLOCKED，不能用这个标签规避验收。

不根据“测试总数很多”判定质量；以具体失败模式是否覆盖为准。

---

## 16. 后续迭代路线

| 迭代 | 候选功能 | 必须先解决的依赖 |
|---|---|---|
| v0.2 | 本地 Web + 可选后台服务 | Application Service 稳定；身份与本机访问边界；CLI/Web 共用事件 |
| v0.3 | 原生会话、实时审批/指令桥接 | Codex App Server / DeepSeek ACP/SDK 契约和恢复测试 |
| v0.4 | 多任务队列、并行、隔离工作区 | 并发预算、工作区锁、变更合并、进程生命周期 |
| v0.5 | 多配置档智能选择 | profile 白名单、真实成本记录、决策可解释性 |
| 后续 | 跨任务记忆、经验积累、插件 | 用户可控读写/遗忘/来源、隐私边界、权限继承 |

跨平台的具体发布顺序不在本轮强制决定。建议在引擎和适配器稳定后验证 Linux，再验证 Windows；这是建议，不改变用户“最终跨平台”的要求。没有真实进程/权限验收不能仅因为 Python 可运行就宣布某平台已支持。

扩展不意味着保证永远不重构。目标是把变化限制在适配器、平台层、持久化迁移和交互层，避免重写整个产品。

---

## 17. 开发 Agent 的执行纪律

从空项目实现；可阅读上游文档与测试思路，不复制现有产品作为主体。新增依赖要能说明其必要性；不先搭插件平台或通用框架。

先 M0 再逐阶段推进。用户已经明确的需求不再重复提问；普通实现决策自行完成并写 ADR/阶段报告。真正涉及秘密、付费权限、危险系统变更或本机接口无法满足的阻塞，才请求最小必要人工处理。

提交前检查已有改动，不能覆盖用户工作；本地提交可按阶段组织。未经授权不 push、不发布、不部署，不改变原 CLI 的默认模型和权限设置。

每阶段报告：已实现、未实现、测试命令和证据、真实/模拟范围、已知限制、下一步。发现范围冲突优先遵循本文用户需求和安全边界，不能靠删测试、放宽权限或增加 mock 伪装完成。

---

## 附录 A：公开资料索引

以下链接用于本地开发时回查原始文档。访问日期均为 2026-11-15；网页分支可能变化，M0 应额外记录本机 CLI 版本与实际 help，不把当前网页的参数当作所有版本通用参数。

- **[S01] OpenAI：Codex Non-interactive mode / CLI reference**  
  `https://learn.chatgpt.com/docs/non-interactive-mode`  
  `https://learn.chatgpt.com/docs/developer-commands?surface=cli`
- **[S02] OpenAI：Codex App Server**  
  `https://learn.chatgpt.com/docs/app-server`
- **[S03] DeepSeek Harness：headless bundle README**  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md`
- **[S04] DeepSeek Harness：CLI help / CLI composition**  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md`  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md`
- **[S05] DeepSeek Harness：Process Sandbox / local backend / approvals**  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/sandbox.md`  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox-local/README.md`  
  `https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md`
- **[S06] MCO：项目 README**  
  `https://github.com/mco-org/mco`
- **[S07] OpenAI Symphony：Service Specification**  
  `https://github.com/openai/symphony/blob/main/SPEC.md`
- **[S08] LangGraph：Persistence / Interrupts**  
  `https://docs.langchain.com/oss/python/langgraph/persistence`  
  `https://docs.langchain.com/oss/python/langgraph/interrupts`
- **[S09] Cezar：项目 README**  
  `https://github.com/open-mercato/cezar`
- **[S10] Python：版本与支持状态**  
  `https://www.python.org/downloads/`  
  `https://www.python.org/doc/versions/`
- **[S11] Python：asyncio subprocess**  
  `https://docs.python.org/3.14/library/asyncio-subprocess.html`
- **[S12] prompt_toolkit：异步输入与 stdout**  
  `https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html`  
  `https://python-prompt-toolkit.readthedocs.io/en/stable/pages/reference.html`
- **[S13] Python：sqlite3**  
  `https://docs.python.org/3.14/library/sqlite3.html`
- **[S14] uv：Locking and syncing**  
  `https://docs.astral.sh/uv/concepts/projects/sync/`
- **[S15] OpenAI：Permissions**  
  `https://learn.chatgpt.com/docs/permissions`
- **[S16] OpenAI：Function calling**  
  `https://developers.openai.com/api/docs/guides/function-calling`
- **[S17] SQLite：Write-Ahead Logging**  
  `https://www.sqlite.org/wal.html`

## 附录 B：已知未定项与处理规则

用户尚未提供具体统筹 API 的 URL、模型 ID、密钥引用、CLI 版本、真实权限配置、金额预算和正式项目名。它们不妨碍开始骨架与离线测试，但影响真实集成。

不要求用户现在重新回答长问卷。开发 Agent 先从授权的本机元数据与 help 探测；密钥必须由用户在本地环境或安全配置中提供，不能要求粘贴到对话、日志或报告。任何无法确认项保持 unknown，并明确影响哪条验收。

**最终定义：v0.1 是一个能在可验证边界内自主完成任务、允许用户随时补充要求、且出了问题仍能查清和恢复的小型中控台，而不是一套先搭出来再寻找用途的多 Agent 平台。**
