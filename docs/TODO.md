# 工程待办（发布前必须）

功能面之外的工程项，按优先级排序。功能面拉齐后逐项处理。

## P0 — 发布阻断项

- [x] **子进程环境变量白名单**（已完成）：`runtime/env.py` 的
  `runner_env()` 统一构造 Runner 环境——只继承 PATH/HOME/locale/temp/
  XDG/proxy/CA 覆盖，主动设 `PYTHONNOUSERSITE=1`，不传任何 PYTHON*
  （PYTHONPATH 会污染插件 venv 解析）；LLM key、DB 凭证等 core
  secret 全部隔离。显式项（`ASTRBOT_SDK_DATA_DIR`）经 `_extra_env` 覆盖层传入；venv 安装
  子进程复用同一张白名单。
- [x] **per-plugin 依赖环境（venv）**：`sdk_bridge/venv.py` 已实现。
  插件在 pyproject.toml（`[project.dependencies]`，优先）或
  requirements.txt 声明依赖；bridge 启动时创建
  `data/plugin_venvs/{root_dir_name}`（`--system-site-packages` +
  `site.addsitedir` .pth 链接 core venv 的 site-packages，editable
  安装的 astrbot_sdk 亦可见），经 `python -m uv pip`（core 已依赖 uv
  wheel，用户机无需预装 uv；缺失时回退 tomllib + pip）安装，
  内容指纹（声明 + Python 版本）命中标记则跳过重装。无依赖声明的
  插件继续使用 core 解释器，零成本。dynamic dependencies 响报。
  后续：dashboard 安装流接入（装插件时即建 venv 并展示安装日志）、
  插件卸载时清理 venv 目录。
- [x] **Runner supervisor**（已完成）：`sdk_bridge/supervisor.py` 的
  `RunnerSupervisor` 接管 client 生命周期——意外退出按 1s→30s 指数
  退避重启（60s 稳定后计数清零），60s 窗口 5 次崩溃打开熔断
  （插件保持注册、调用即报 CircuitOpenError、registry 标记错误），
  协议级 ping（连续 3 次无应答 kill 走崩溃路径），主动 stop 不重启。
  配套协议修复：Runner 新增 ping 方法；Peer 请求处理捕获
  BaseException——插件代码的 SystemExit 不再让握手静默挂死
  （曾导致启动失败必等 10s 超时）。
- [ ] **用户授权流**：目前桥接自动授予插件声明的全部 capability（竖切临时
  行为）。需要用户在安装/启用时的授权 UI 与持久化授权表，运行只授予
  声明 ∩ 授权。

## P1 — 完整性

- [ ] **SDK 插件生命周期接入**：`_terminate_plugin`、热重载、
  `inactivated_plugins`（禁用/启用）目前都不认识桥接插件；dashboard 上
  可见但不可管理。
- [ ] **运行中授权变更**：授权在启动时定死；协议设计了"原子替换快照"
  （`ctx.capabilities` 快照替换），需要 Host → Runner 的更新通道。
- [ ] **配额与可观测性**：KV 的大小限制与配额（文档已承诺）；capability
  调用/拒绝的审计日志增强与 metrics。
## event-context 合并后的跟进

主仓库的 conversation 后端将重构为 event log（线性事件 + 完整 rebase，
分支在 AstrBot-event-context）。合并后跟进：

- [ ] `ConversationPatch`/`ctx.conversations.update` 支持 `expected_revision`，
  对齐 event-context 的乐观并发（event_writer）。
- [ ] 评估在 `ctx.conversations` 增加事件读写（seq 游标 append/list/latest）
  与 `fork_conversation`。
- [ ] custom agent runner 的 `AgentStep`/`AgentEvent` 直接采用 event-context
  的类型化 agent 事件（turn/request/tool/message/rebase + event_id），
  填充当前预留的 `AgentResponse.steps`。
- [ ] 桥接测试的 `FakeConversationManager` 补 `revision` 字段，保持与真实
  ConversationManager 形状一致。

## SDK 服务

- [x] **会话等待**：`ctx.sessions.wait()` 在 Runner 求值过滤器，由 Host
  认领并拦截匹配消息。断线时 Host 清理本 Runner 的 waiter。
- [x] **Web 路由**：`ctx.web.route` 支持逐块传输响应，大请求体按需拉取。
- [x] **插件页面**：Runner 提供页面和 i18n 清单，并按需流式传输资源文件。
- [ ] **Hook Decision 拦截**（`ToolCallDecision.block` /
  `MessageSendDecision.block`）：需要 Core 在工具执行和单次发送前应用拦截。

## 兼容性

- [ ] **Python 3.10 降级**：SDK 目前使用 PEP 695 语法（`class Plugin[ConfigT]`、
  `type` 语句），实际下限是 Python 3.12；市场承诺 3.10+。发布前改写降级，
  越早越便宜。
