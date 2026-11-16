# 安全边界与能力状态

2026-11-16。本程序是原生 Agent 的协调器，不是 OS 沙箱。不依赖 Docker、OrbStack 或 Podman，不实现自有 Seatbelt。未修改用户原有 CLI 配置；默认不启用真实 Agent。

## Agent Center 自身控制

- 统筹模型只有类型化动作，无任意 Shell 工具。开发委派必须匹配当前 Plan；验证命令只从保存的 criterion 提取。
- `ProcessRunner` 在产品中只启动适配器的 CLI，管理 argv、环境白名单、日志、超时、TERM/KILL 和受管进程组；不直接启动用户项目的 pytest/Python/构建。它的进程管理不是隔离机制。
- 任务版本、Plan 哈希、Run、审批、预算和证据持久化到 SQLite。独立验证消耗同一预算；不确定 Run 阻止重放。
- 验证只读；精确命令、cwd、命令退出码/完成状态、CLI 退出码和终结事件必须同时成立。最终文字及 tool_result completed 不是测试通过证据。
- 工作区、Plan、安装/权限配置变化使证据失效。日志截断、缺字段、取消和超时保持 UNVERIFIED。已验证的命令非零退出记录 FAILED，不能满足验收。
- 日志权限为 0600、目录为 0700；容量限制、脱敏、并发排空输出。统筹 API key 不传递给 Agent。DeepSeek 使用独立 DSH_HOME 与单次路由 overlay。
- 管理读取限于任务工作区；读取工具检查路径、敏感文件名、内容大小及潜在秘密。指纹只用于一致性判断，不向模型公开文件内容。它不是针对外部并发写入的不可变快照。
- 批准签名绑定需求/配置/目标/有效期。批准不等于 OS 权限变更。当前窄范围授权映射未验证，因此批准后仍阻塞；不修改原生配置、不原地恢复 Headless 审批。

## 由 Agent 原生执行环境提供

Agent 负责项目编辑、Shell、pytest、构建与运行，使用自己的原生沙箱、权限策略和审批机制。Center 传入 read-only/workspace-write 与拒绝升级策略，不调用全访问、yolo、自动审批或绕过参数。

Codex 原生 Seatbelt 的文件效果和本机网络限制已有独立可信探针；内置模式允许外部读取。单次细粒度 profile 的读取限制也已探测，但尚未接入生产适配器。DeepSeek 原生库的文件效果限制有探针；其临时目录写入例外、开放读取和开放网络已单独记录。原生库返回的 full 不能被解释为所有安全边界都已验证。

## 九项能力矩阵

状态只使用 SUPPORTED / UNSUPPORTED / UNVERIFIED。下表评估当前生产适配器是否已经有足够证据授权该能力；原生独立探针的局部成功不会提升整条调用链。

| 能力 | Codex | DeepSeek Headless |
|---|---|---|
| command_execution_supported | UNVERIFIED | UNVERIFIED |
| read_only_supported | UNVERIFIED | UNVERIFIED |
| workspace_write_supported | UNVERIFIED | UNVERIFIED |
| filesystem_read_boundary_verified | UNSUPPORTED（当前内置模式） | UNSUPPORTED |
| filesystem_write_boundary_verified | UNVERIFIED（原生探针局部证实） | UNVERIFIED（存在临时目录例外） |
| network_boundary_verified | UNVERIFIED（原生本机 TCP 探针拒绝） | UNSUPPORTED |
| approval_response_supported | UNSUPPORTED（当前 one-shot 适配器） | UNSUPPORTED |
| structured_command_exit_verified | UNVERIFIED（未有真实成功事件，cwd 待确认） | UNSUPPORTED（当前 Headless 投影丢失字段） |
| cancellation_verified | UNVERIFIED（真实超时已终止，完整后代清理待验） | UNVERIFIED |

配置内状态附 installation_identity 与 evidence_reference。安装身份覆盖二进制/ASAR、OS、权限与模型配置、适配器实现；变化后旧状态不能用于执行。手动写 SUPPORTED 不是完成探针：应附真实证据，可信本地操作者负责录入。旧单布尔门禁不能放行。

当前保守门禁要求所有项目执行具备已证实的文件读/写、网络和取消边界，验证另需结构化命令退出证据。DeepSeek 当前不能满足这些保护要求，因此不自动降级安全要求或换另一 Agent 绕过。

## 明确限制

本程序不防御恶意 CLI、主机管理员、内核或被替换的依赖。沙箱中的项目测试仍是不可信代码；退出零只证明登记命令的执行结果，不能替代对验收标准质量的审查。文件指纹存在外部并发编辑竞态；过大的工作区拒绝指纹，不跳过相关依赖目录。

原生模型文件工具、扩展、GUI 凭证隔离、真实模型路由、全面网络行为和取消后后代进程清理尚未验证。空认证 CLI 探针未产生项目命令；不把它们写成真实项目测试或端到端通过。详见 [兼容性](COMPATIBILITY.md) 与 [验收报告](ACCEPTANCE_REPORT.md)。
