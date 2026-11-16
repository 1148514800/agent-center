# 本机原生 CLI 兼容性 — 2026-11-16

环境：macOS 26.6 / 25G72 arm64，Codex CLI 0.166.0，DeepSeek Harness 0.2.2-rc.3，开发 Python 3.14.10。探针只使用一次性目录和非敏感哨兵；未修改用户 Codex/DeepSeek 配置。模型任务没有成功执行。

## Codex

`codex --version`、`codex exec --help`、`codex --help` 和 `codex sandbox --help` 已检查。当前命令映射：

```text
codex --ask-for-approval never exec --json --ephemeral --ignore-user-config
  --ignore-rules -c sandbox_workspace_write.network_access=false
  --sandbox read-only|workspace-write --cd WORKSPACE --model MODEL
```

`--ask-for-approval` 位于根命令；`never` 拒绝升级请求，不绕过沙箱。适配器不发出 yolo、full-access、自动审查放行或沙箱绕过选项。`CODEX_HOME` 可用于已有认证，忽略用户配置并不意味着忽略所有系统托管配置。

真实 `codex sandbox -P :read-only|:workspace` 探针已通过原生 Seatbelt 运行可信命令。只读拒绝工作区写入；workspace 允许工作区写入；两者拒绝探针中的越界写入及 loopback TCP 连接。两者均能读取工作区外的非敏感哨兵：内置模式不能宣称敏感读取隔离。网络结果只覆盖本机 TCP 探针，不泛化为所有外传途径。

Permission Profiles 研究：本版本支持 `-P` 原生 sandbox 命令；以单次 `-c` 传入命名 profile 的 filesystem/network 表可拒绝根目录读取，只允许 workspace、minimal runtime 与明确的 Python runtime 读取，并禁用网络。实测读工作区成功、读外部哨兵被拒绝，无需修改 `~/.codex/config.toml`。这目前是探针配置，未接入生产适配器，不能为其默认 legacy sandbox 配置背书。生产接入前仍需验证模型文件工具、实际命令事件、所有 runtime 路径及配置优先级。

[官方 Permission Profiles 文档](https://learn.chatgpt.com/docs/permissions) 指出，旧 `--sandbox` 设置会优先于 `default_permissions`，两种配置不能混用。将来的适配器必须分别构造并重新验证，不能叠加后假设细粒度配置已生效。

真实 `exec --json` 空认证探针得到 401 重试事件，15 秒超时后进程退出 -15；未出现命令事件或正常终结。命令事件的实际 cwd 字段、exit_code/status/aggregated_output 及正常 turn.completed/turn.failed 仍未实测。解析器不会从 `--cd` 或最终文字推断命令 cwd；当前版本若缺该字段，验证只能 UNVERIFIED。

## DeepSeek Harness

通过 Electron launcher 调用 ASAR 内 `lib/bin.js`；普通 Python `Path.is_file()` 不能检查 ASAR 内部文件，因此适配器允许已存在的 ASAR 包路径，最终启动仍由 Electron 验证。缺失入口不会降级到其他执行器。

已检查 `--profile headless --help`：支持 `--permission-mode read-only|workspace-write`、`--approval-policy never`、`--json`、`--add-dir` 和 `--session-id`。后者用于采用已有持久化会话，未知 ID 报错；独立 Run 不传入旧 session ID，不传额外写目录。

每次调用写入私有模型路由 overlay，使用独立 `DSH_HOME`，不继承 GUI token 或统筹 API key。安装文档说明 Headless 不消费 GUI 托管凭证，但还可能读取 cwd/DSH_HOME 的 `.env`；显式环境白名单不能单独证明完整认证隔离。空 HOME/DSH_HOME 的真实 CLI 探针返回 `MISSING_CREDENTIAL`、`final.reason.kind=error`、进程退出 1。真实已认证模型路由、API key 与 GUI 的全面隔离尚未验证。

直接调用已安装 `LocalSandboxProvider.confine`（未经修改的原生库，不自建 Seatbelt）的探针显示：read-only 拒绝工作区及外部写入；workspace-write 允许工作区写入、拒绝普通 home 下的外部写入，但允许系统临时目录内的外部写入。两种模式均允许外部读取与 loopback TCP 连接。库返回的 `enforcement=full` 指文件效果沙箱，不是网络/敏感读取隔离。该探针不等同 Headless 完整执行链已验证。

安装源码确认 Headless `tool_call` 有 callId/tool/input，`tool_result` 有 callId/status/result。`result` 是 Bash 输出渲染后的文本，未传递执行器内部 canonical `cwd/exitCode/timedOut/aborted` DTO。解析器关联 call/result，拒绝重复或悬空 call；不把 completed 当命令退出零，也不从可伪造输出标记提取成功。当前原生协议的结构化命令退出证明为 UNSUPPORTED，验证保持 UNVERIFIED。

Headless 无实时审批回应：需要审批时拒绝、保存现场、返回中控；审批记录不能恢复旧进程。

## 证据与仍未验证项目

[Codex sandbox 探针](evidence/native-sandbox-2026-11-16.json)、[DeepSeek 原生库探针](evidence/deepseek-native-sandbox-2026-11-16.json)、[DeepSeek 临时目录例外](evidence/deepseek-temp-boundary-2026-11-16.json)、[真实 CLI 失败路径](evidence/cli-failure-probes-2026-11-16.json)。可复现脚本位于 `tests/live/`，默认 pytest 不运行这些脚本。

真实成功命令事件、完整原生取消/后代清理、真实模型路由、统筹端到端均未通过。两个 Agent 的能力结论独立；原生探针结果不会自动写入或启用产品配置。
