# Agent Center v0.1 原生沙箱架构修正验收

## 2026-11-18：当前 Mac 同步树的 Linux 离线增量验证

本节是本次增量工作的结果；下文 2026-11-16 的 Mac 探针记录为历史证据，本次未重跑。保留当前工作树已有提交与未提交改动，未进行从零架构替换、提交或推送。

- 创建任务之前由模型返回直接回答或类型化执行意图；普通问答一次 mock 响应，无任务、Run 或执行工作区。独立会话可重启恢复；任务追加入口明确分开，受单任务 / 未对账 Run 阻塞的执行请求保留并可显式重试。
- 删除按钮与确认 API 已实现。事务清理内部关联记录；活动 job / 执行时段、未知或未对账 Run 拒绝删除；工作区与日志文件保留。
- 统筹模型、协议、思考强度、地址和密钥通过 config 旁的 0600 私有文件原子持久化并重新加载。密钥不进入 API 响应、浏览器 localStorage 或任务配置快照。不支持的协议 / effort 在调用前拒绝；自动值省略参数。
- 连接面板空状态保留添加 Agent 与统筹配置入口；只展示显式新增或兼容的旧配置 Agent，安装发现不代表添加。
- Web API revision 服务端与 UI 同步为 **4**；SQLite 增加会话表和 migration 7，TOML schema_version 仍为 1。

最终命令与结果：

| 检查 | 结果 |
|---|---|
| `uv run --locked ruff check .` | PASS |
| `uv run --locked mypy src` | PASS，22 个源文件 |
| `uv run --locked pytest -m 'not live'` | PASS，213 passed（新功能测试文件 37 项） |
| `node --check src/agent_center/web/app.js` | PASS |
| `git diff --check` | PASS |

测试仅使用 httpx.MockTransport、fake / mock CLI、内存 HTTP 流及临时数据库 / 配置目录。没有真实统筹 API 请求或用户 API 配额消耗，没有启动 / 重启用户数据上的 Web 服务，没有访问用户 Mac 或真实 ~/.agent-center。未创建 Page；未进行浏览器 UI 验收，未执行或宣称 live 测试通过。

边界：私有文件依靠本机文件权限保护，并非加密密钥链；第三方模型的参数兼容性及真实 GUI / 原生 Agent 联调仍未验收。一个前序测试运行中，既有 `test_process_reads_both_streams_without_deadlock` 出现一次截断断言失败；相关实现与测试未修改，后续完整运行通过，本次不宣称修复该不稳定性。

---


日期：2026-11-16（Asia/Shanghai）。环境：macOS **26.6 / 25G72 arm64**，Python 3.14.10，SQLite 3.53.1，Codex CLI 0.166.0，DeepSeek Harness 0.2.2-rc.3。

**结论：架构改造及离线回归通过；完整 v0.1 验收仍 BLOCKED。真实模型项目执行及 E 端到端没有通过。** 原有未提交工作已保留，当前修改未提交、未推送。下列结果替代此前“等待自建隔离测试后端”的规划；不增加容器或 OS 沙箱。

## 本轮实现

- VerificationService 已由永久拒绝桩改为原生 Agent 验证协调：当前 Plan → 固定 pytest argv → 能力选择 → 独立只读 Run → 事件/命令核验 → 证据与失效门禁。
- Agent Center 负责管理操作，不直接执行用户项目的测试或代码。生产 ProcessRunner 只有 TaskEngine 的 Agent CLI 调用；其他管理子进程仅为版本及进程身份检查。
- Codex 明确传入根命令审批策略 never、原生 sandbox、JSONL，忽略用户配置与 execpolicy rules；不使用危险绕过参数。
- DeepSeek 保留独立 DSH_HOME 和单次模型 overlay，修正 Electron ASAR 入口检查，关联 tool_call/tool_result。当前 Headless 无结构化命令退出证据，不以 completed 或输出文字放行。
- 新增九项能力矩阵、安装/OS/权限/适配器指纹、证据引用；旧单布尔配置不能授权。验证证据绑定 Plan、任务版本、criterion、Run 和完整工作区指纹。DB 迁移保留原数据。
- 审批请求与授权落实分离；无法准确应用窄范围授权时仍 BLOCKED。预检切换不能改变权限模式；原生拒绝不触发更宽松 Agent 回退。外层取消现在同时停止受管验证 CLI。

## U：单元测试

命令：`.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/unit`

**PASS：82 passed**。覆盖两个适配器参数、JSONL 解析与截断、Codex 命令/实际 cwd/退出码/status/output、DeepSeek 配对/终结原因/文本退出标记拒绝、伪造最终报告、能力矩阵、权限与安装变更失效、Plan/工作区/Run 证据绑定，以及原有状态、预算、审批、存储回归。

测试中的 SUPPORTED 能力和成功命令事件均为显式合成数据，不是本机 Agent 的安全认证。

## I：模拟 CLI 与进程测试

命令：`.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/integration`

**PASS：12 passed**。合成 CLI 真实子进程覆盖独立验证 Run、正确事件、错误命令退出码、无终结事件、破损日志、文字伪造成功、取消及超时、日志限额、双流排空和后代进程清理。临时项目内放置了会创建外部哨兵的测试文件，合成 CLI 只生成事件；该文件未被中控或宿主 Python 执行。

合成成功路径能提交当前 criterion 的完整证据；错误或证据不足不能完成任务。此处没有执行真实用户项目 pytest，不计入 L/E。

## L：真实原生 CLI / 原生库探针

| 项目 | 结果 | 证据与边界 |
|---|---|---|
| CLI 版本和帮助 | PASS | 实际检查 Codex exec/root/sandbox 与 DeepSeek headless help；含审批、权限、JSON 和 session 参数。 |
| Codex 原生 Seatbelt | 局部 PASS | [原始记录](evidence/native-sandbox-2026-11-16.json)：read-only 禁止工作区写，workspace 允许工作区写；两者拒绝探针中的工作区外写入。真实退出码为 0/1。 |
| Codex 读取边界 | 内置模式 UNSUPPORTED | 两种内置模式均读到外部非敏感哨兵，不能声称敏感读取隔离。 |
| Codex 网络边界 | 局部 PASS，产品仍 UNVERIFIED | 连接已监听 loopback TCP 的命令收到 EPERM；不代表所有网络或模型工具边界已验证。 |
| Codex 单次 Permission Profile | 局部 PASS | 同一原始记录中 ac-restricted 允许工作区读取，拒绝外部读取、写入和本机网络。只通过单次配置授权明确 runtime 读取，未写用户配置；尚未接入生产适配器。 |
| DeepSeek 原生 Seatbelt 库 | 局部 PASS | [普通 home 哨兵](evidence/deepseek-native-sandbox-2026-11-16.json)：使用安装包 LocalSandboxProvider.confine，read-only 拒写，workspace-write 工作区内可写、普通外部目录拒写。不是完整 Headless 命令链测试。 |
| DeepSeek 临时目录写入 | 存在例外 | [临时目录记录](evidence/deepseek-temp-boundary-2026-11-16.json)：workspace-write 可写系统临时目录中的外部哨兵，不能宣称严格只写工作区。 |
| DeepSeek 读取与网络 | UNSUPPORTED | 两种模式均可读外部哨兵、可连接已监听的 loopback TCP。文件效果 full 不代表网络/敏感读取隔离。 |
| Codex 真实 exec --json 失败路径 | PASS（拒绝成功判定） | [stdout](evidence/codex-cli-2026-11-16/stdout.log)、[stderr](evidence/codex-cli-2026-11-16/stderr.log)：生成空 HOME/CODEX_HOME，无继承 key，使用非业务 probe 模型。收到 401，15 秒超时被 TERM，退出 -15；解析未判成功，0 个命令。 |
| DeepSeek 真实 headless --json 失败路径 | PASS（拒绝成功判定） | [stdout](evidence/deepseek-cli-2026-11-16/stdout.log)、[stderr](evidence/deepseek-cli-2026-11-16/stderr.log)：空 HOME/DSH_HOME，无继承 key，明确 deepseek-official 路由；MISSING_CREDENTIAL、final.reason.kind=error、退出 1，0 个命令。 |
| 真实成功 command_execution / tool_call 与命令退出 | NOT_RUN | 无已认证模型执行命令。Codex 实际 cwd 字段及正常终结仍待核实；DeepSeek 安装源码确认 Headless 投影丢失 canonical 命令退出 DTO，当前协议不满足验证要求。 |
| 真正取消与完整后代清理 | UNVERIFIED | Codex 真实超时停止有记录，但无真实 Agent 子命令树；DeepSeek 未进行运行中断。不得拿 I 层取消测试代替。 |
| 真实认证/模型路由/GUI 凭证隔离 | UNVERIFIED | 只做空凭证失败路径，未读取原认证、未修改原配置。 |

复现脚本：`tests/live/probe_native.py`、`tests/live/probe_deepseek.cjs`、`tests/live/probe_cli.py`。这些脚本不会被默认 pytest 收集；CLI 失败探针可能发生无认证请求，费用未知不记为 0。原始 [CLI 元数据](evidence/cli-failure-probes-2026-11-16.json) 单独保存。所有哨兵与临时目录均已清理，原始观测保留。

## E：真实端到端

**BLOCKED / NOT_RUN**。没有“真实统筹规划 → 真实 Agent 修改临时项目 → 原生沙箱 pytest → 真实证据 → 必要修复 → 完成”的成功记录。

阻塞点：生产 Codex 内置模式未提供已验证的读取边界与实际命令 cwd；细粒度配置尚未接入完整执行链。DeepSeek 无网络/敏感读隔离，当前 Headless JSONL 无足够结构化退出证据。真实统筹/模型路由和认证也未验证。为完成演示而降低门禁、自动扩大权限或采用宿主项目测试均不允许。

## 静态检查和交付

`ruff check src tests`、`ruff format --check src tests`、`mypy src` 通过；示例 TOML 加载成功，两个 Agent 均 `allow_execution=false`。`git diff --check` 通过。

已更新 ARCHITECTURE、COMPATIBILITY、SECURITY_BOUNDARIES、本报告、ACCEPTANCE_CHECKLIST 和项目规划修订说明。新增能力矩阵及原生探针证据。能力责任与未验证项详见 [安全边界](SECURITY_BOUNDARIES.md)。没有发布、部署、push、修改用户原生配置或启用真实 Agent。
