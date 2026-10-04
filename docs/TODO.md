# 工程待办（发布前必须）

功能面之外的工程项，按优先级排序。功能面拉齐后逐项处理。

## P0 — 发布阻断项

- [ ] **子进程环境变量白名单**：`StdioPluginClient` 目前用 `os.environ.copy()`
  启动 Runner，core 环境里的 LLM API key、数据库凭证对插件进程全部可见。
  改为白名单传递（PATH、LANG、Python 运行时必需项 + 显式声明项）。
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
- [ ] **Runner supervisor**：子进程崩溃后无重启、无健康检查、无退避。
  需要 supervisor 层（崩溃检测、重启策略、连续失败熔断）。
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

## 已决策的后续功能

- [x] **旧版兼容层 P2**（已完成）：Provider 代理（`astrbot.api.provider`、
  `get_using_provider`/`text_chat`/`llm_generate`/`tool_loop_agent`）、
  hooks 兼容（`on_llm_request` 等 14 个装饰器，旧签名原地改写经追踪
  DTO 回写）、`llm_tool` + `FunctionTool` 类形式（`call`/`run` 均支持）、
  `astrbot.api.all` + `astrbot.api` 顶层命名空间、`@register` 元数据
  装饰器、conversation manager 代理（含 `add_message_pair`、
  `update_conversation(history=)` 整体替换）、StarTools
  （`get_data_dir` 与进程内路径一致）、`_conf_schema.json` 配置注入、
  GreedyStr 与旧版位置参数解析、command_group 真分组、
  Poke/Json/Share（经 UnknownSegment 映射回 core 组件）、
  custom filter 运行端求值、sp 代理、html_renderer 代理。
  2099 个真实插件导入扫描：494 直接可加载、82 个 compat 长尾（3.9%）、
  1245 个缺三方依赖（per-plugin venv，见 P0）、278 个设计排除
  （web 路由、session_waiter、平台/管线内部、全局配置、custom agent
  注册）。
- [x] **旧版兼容层 P2.5**（已完成，接口面收口）：`request_llm` yield
  （经 Provider 代理执行并可选回写 conversation）、`register_task`
  后台任务（shutdown 时取消）、`get_all_stars`/`get_registered_star`
  （启动快照 + async 变体）、TTS/STT/Embedding Provider 代理
  （`get_audio`/`get_text`/`get_embedding`）、event 补齐
  （`get_message_outline`/`is_admin`/`get_group_id`/`make_result`/
  `continue_event`/`clear_extra`/str `set_result`）、
  host-internal API 全部改为响报 IsolationUnsupportedError
  （register_commands/get_db/get_platform/get_event_queue/
  get_llm_tool_manager/register_provider）。
  复扫 2099：495 ok / 68 compat 长尾（3.2%，均为 1-2 次零散项）/
  290 设计排除 / 1246 缺依赖（扫描环境无 core 三方依赖所致，
  真实 bridge 在 core venv 中显著更好；根治靠 P0 per-plugin venv）。
  已知未做：`should_call_llm`（需要 EventResult 加语义标记，
  属协议改动）、`persona_manager`（等 personas 能力）。
- [ ] 旧版兼容层 P3（长尾，按需）：`persona_manager`（等 personas 能力）、
  `astrbot.api.web` 路由（等 web 能力设计）、`EmbeddingProvider` 等
  类型外观的剩余零散项、`star_handlers_registry` 等注册表内省
  （当前降级为空）。

- [ ] **Hook Decision 拦截**（`ToolCallDecision.block` /
  `MessageSendDecision.block`）：需要 core 在执行点增加拦截通道
  （tool 执行器的前置检查、只跳过单次发送不终止事件的通道）。
  当前 hook 只支持写操作修改，与旧版语义一致。

## 兼容性

- [ ] **Python 3.10 降级**：SDK 目前使用 PEP 695 语法（`class Plugin[ConfigT]`、
  `type` 语句），实际下限是 Python 3.12；市场承诺 3.10+。发布前改写降级，
  越早越便宜。
