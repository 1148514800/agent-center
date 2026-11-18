# Agent Center v0.1：原生 Agent 执行与验证协调

修订：2026-11-16（Asia/Shanghai）。保留 Python + SQLite。没有强制容器依赖，也不实现 OS 沙箱。

```text
用户 / CLI
    ↓
TaskEngine ── ModelClient（规划，只有类型化动作，无 Shell 工具）
    ├── 状态、Plan、预算、审批、恢复、允许范围内的文件读取
    ├── VerificationService（验收协调、证据核验、失效判断）
    │       ↓ 独立验证 Run，固定 Plan 命令，只读权限
    └── CodexExecAdapter / DeepSeekHeadlessAdapter
              ↓ LaunchSpec：原生权限参数、独立环境、stdin
         ProcessRunner（仅管理 Agent CLI 及其进程组、超时、日志）
              ↓
         Agent 原生沙箱 / 权限 / 审批机制
              ↓
         用户项目编辑、Shell、pytest、构建、运行

SQLite：任务/需求版本、Plan、Run 快照、预算、审批、事件、验证证据
```

## 职责边界

Agent Center 启动和监控 CLI，检查调度资格，管理状态和预算，协调审批，解析事件，记录日志和证据。`ProcessRunner` 不是项目命令执行后端；生产调用方只向它传递适配器构建的 Agent CLI。项目代码、Shell、测试和构建必须由 Agent 原生执行环境处理。仓库自身 U/I 测试运行的是控制器代码及受控合成 CLI，不属于用户项目验证。

`Store` 是任务状态真相源。状态与对应事件原子提交，Run 创建检查任务版本、待处理需求和未解决进程。重启后不确定的 Run 保持 unknown，不能自动重放。

创建任务之前，由统筹模型根据当前独立会话上下文决定是否需要 CLI：直接回答复用本次响应（一次调用），只保存到 conversations/chat_messages，不产生任务、Run 或自动工作区。类型化 request_execution 仅代表执行意图；通过单任务与未对账 Run 检查后才创建任务，再走原有规划、权限、预算、审批和独立验证流程。路由调用计入升级后任务的统筹预算。系统不使用关键词规则或独立复杂度分类器，也不在创建任务、追加需求时预先标记“必须执行”。模型可直接回答，或保存 Plan 后委派 CLI；只有模型已选择规划或执行的工作才需要按现有流程验证后完成。旧数据库的 `execution_required` 分类字段在打开时自动移除，任务历史与证据保留。

## 验证流程

1. `verify` 只接受 criterion ID；从当前持久化 Plan 取出命令，不采用模型本次提供的命令。
2. 检查需求版本、待处理消息、criterion 唯一性、明确测试路径和固定 argv。
3. 选择配置为 read-only 且具备本次所需能力的 Agent。能力包含读取、写入、网络边界与取消，不能从其他 Agent 推导。没有合格 Agent 时记录 UNVERIFIED 并阻塞，不自动换更宽松配置。
4. 每项 criterion 创建独立 CLI Run，与开发 Run 分离，使用相同预算、日志和进程恢复机制。不恢复旧原生会话。
5. Agent 使用原生只读执行环境运行保存的命令。当前固定格式：

   `/usr/bin/env PYTEST_ADDOPTS= PYTEST_PLUGINS= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -p no:cacheprovider -c /dev/null -o addopts= -q tests/test_example.py`

   只允许明确登记的相对 `.py` 文件，拒绝选项注入、父目录、符号链接和 Shell 追加命令；模型不能修改验证 argv。固定 env 清除 PYTEST_ADDOPTS/PYTEST_PLUGINS 并禁用自动插件，`-c /dev/null -o addopts=` 排除项目 pytest 配置追加项；`-B` 禁止本次 Python 写 pyc，禁用 cacheprovider。测试需要写操作而只读沙箱拒绝时，不扩大权限。
6. 核对 CLI 退出码、正常终结事件、完整日志、命令、实际 cwd、命令退出码、完成状态和输出字段。额外命令、Shell 包装、缺字段、超时、取消、截断、最终文字报告都不能补足证据。
7. 完整且退出零才是 PASSED；已证实的测试退出非零是 FAILED，可返回规划修复；缺证据为 UNVERIFIED 并阻塞。
8. 证据绑定 task、requirements_version、criterion、run、Plan SHA-256、前后工作区指纹和能力安装指纹。输出保留有界摘要和哈希。当前指纹覆盖工作区文件、目录、权限位与符号链接，不忽略依赖目录；超过 256 MiB 或 100,000 项、目录不可读或包含特殊文件时拒绝指纹计算。
9. Finish 再检查当前工作区、Plan、安装/权限能力、需求和未完成工作；SQLite 事务核对精确 Plan 与已完成 Run 的证据绑定。文件系统不是不可变快照，外部并发编辑仍可能竞态。

## 能力与审批

`AgentCapabilities` 的九个独立状态使用 SUPPORTED / UNSUPPORTED / UNVERIFIED，另附证据引用和安装指纹。指纹覆盖 CLI 二进制、DeepSeek 包、OS、适配器实现和权限/模型配置；发生变化旧结论不能授权执行。旧 `capability_status`、`write_sandbox_verified` 仅为配置兼容元数据，不再授权。

审批是用户决定，不能授予原生 CLI 不支持的权限。原生结构化权限拒绝会结束当前 Run、保存请求并进入统一审批；没有审批管理器则 BLOCKED。只有适配器能够准确实现所批范围时才可创建新 Run。目前两适配器未实现经验证的窄范围授权映射，因此批准后仍 BLOCKED。DeepSeek Headless 没有实时审批响应通道，不能假称继续原进程。

真实 Agent 默认关闭。原生库探针、CLI 失败路径、模拟验证和真实端到端分别报告，见 [验收报告](ACCEPTANCE_REPORT.md)。


## 会话、删除与本地设置（Web API revision 4）

新会话与选中任务明确分开：选中任务的输入是追加需求；新会话是独立问题，不自动附到当前任务。CLI 普通输入走独立会话，`/add` 才追加到选中任务。会话分别保存上下文；升级为任务只复制该会话截至执行请求的内容。上下文超过字节限制时保留消息并报错，不默认转执行。单任务占用或存在未对账 Run 时，执行请求保存为 execution_blocked，说明任务 ID；显式重试使用已保存的决定，不重做语义分类。模型 / 协议错误同样保留消息，绝不以未匹配文本作为执行回退。

删除使用 SQLite 事务先检查 Run 与执行时段，再按依赖顺序删除 evidence_criteria、evidence、approvals、events、runs、messages 和 tasks。会话任务链接 ON DELETE SET NULL；工作区和日志文件不删除。活动网页 job、活动时段、未知或未对账 Run 均拒绝删除，API 必须携带任务 ID 确认。

统筹设置扩展现有本地注册机制：旧 agents.json 中的 supervisor 仍可读取，新保存写入所选 config.toml 旁的 config.toml.supervisor.json。该文件同时包含模型 / 协议 / 思考强度 / 地址 / 密钥，0600 临时文件写入、fsync、原子替换并同步目录；新目录为 0700，加载拒绝过宽权限或符号链接。配置 PrivateAttr 在内存中保存地址 / 密钥，model_dump、任务快照和 API 不序列化密钥；不写入 Agent 环境。无需改动基础 TOML，也不改原生 CLI 配置。路径跟随 --config / AGENT_CENTER_CONFIG，--data-dir 仅影响 Store / runtime。旧配置可加载；不支持的思考强度在保存或发送前拒绝，默认不发送 reasoning_effort。

Agent 配置增加可选 user_added：新注册显式为 true，离线探测默认 false；无该字段的旧 TOML 使用实际模型配置兼容。连接与能力及 health API 只展示已添加 Agent；发现安装本身不构成接入，统筹设置与添加入口始终存在。
