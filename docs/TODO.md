# 工程待办（发布前必须）

功能面之外的工程项，按优先级排序。功能面拉齐后逐项处理。

## P0 — 发布阻断项

- [x] **子进程环境变量白名单**（已完成）：`runtime/env.py` 的
  `runner_env()` 统一构造 Runner 环境——只继承 PATH/HOME/locale/temp/
  XDG/proxy/CA 覆盖，主动设 `PYTHONNOUSERSITE=1`，不传任何 PYTHON*
  （PYTHONPATH 会污染插件 venv 解析）；LLM key、DB 凭证等 core
  secret 全部隔离。显式项（`ASTRBOT_DATA_PATH`、
  `ASTRBOT_SDK_DATA_DIR`）经 `_extra_env` 覆盖层传入；venv 安装
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
- [ ] **远端 Runner 的模块身份（虚拟根）**：loader 目前靠
  `ASTRBOT_DATA_PATH` 或 `<root>/data/plugins/<name>` 布局解析 AstrBot
  根目录，把 legacy 插件按真实点分路径 `data.plugins.<dir>.main` 导入
  （spawn 子进程/Flask instance path/资源 introspection 全依赖此身份）。
  远端 WS Runner 所在机器没有 AstrBot 根，两条线索都没有，会静默回退
  旧的合成命名空间——上述坑在远端重新出现。定案方向：A 为主——约定
  远端 runner 把插件放在 `<runner_root>/data/plugins/<name>/` 并自设
  `ASTRBOT_DATA_PATH`（零新代码，随远端部署文档落地）；B 为兜底——
  检测不到根时在 sys.modules 手工挂 `data`/`data.plugins` 命名空间包、
  `__path__` 指向插件真实父目录后照常 import（约十几行，跨平台）。

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

- [x] **session 等待能力（session_waiter，设计已定稿 v2，已实现）**：
  语料 57 个插件使用（多轮问答/向导是真实需求）。core 机制 =
  全局 session 注册表 + maxsize 优先级 ALL 事件拦截（匹配则
  trigger + stop_event）+ plugin 进程内 future。隔离模式定稿：

  - 插件面 API 为线性等待：`async with ctx.sessions.wait(umo,
    filter, timeout=60) as s` + `await s.next(timeout=...)`（逐轮
    可覆盖默认超时）+ `s.ask(...)` 便捷发送。旧版的
    keep/stop/history_chains 概念分别消解为"再调一次 next"、
    "退出 async with"、"插件本地持有已收到的 event"。
  - `SessionFilter` 保持代码形态并在 Runner 侧求值：旧 API 形状
    全保留，语料 10+ 种自定义纯代码 filter 全部兼容（先前"声明式
    字段键、纯代码响报"方案废弃）。Host 只按 umo 做候选
    narrowing，匹配判定与拦截执行分离。
  - capability 定为 `message.wait`（op=register/rearm/stop），与
    `message.send`（发）/`message.receive`（看）并列第三动词
    "等"（认领+拦截）；拦截吞消息是 receive 之外的增量授权。
    legacy 插件进 `_LEGACY_GRANT_IDS`。
  - RPC：plugin→Host 走 `message.wait`(op)；Host→plugin 通知为
    `message.wait.consider/matched/timeout`。注册时插件用 filter
    对发起事件算出 key 上报，Host 建镜像注册表；入站事件 umo
    命中时扇出 consider，Runner 回算各 waiter 的 key，Host 比对
    认领（跨插件撞键先到先得并响报）后 stop_event 并 matched
    投递。consider 带 1s RPC 超时，未答按未匹配放行并告警。
  - 计时在 Host 侧：投递 matched 时武装、rearm 重置、超时推
    `message.wait.timeout`（插件 next() 收 TimeoutError）；插件
    崩溃/断线由 supervisor 立即清理其全部 waiter，不泄漏会话。
    用户在插件处理间隙发消息由 Runner 本地排队，next() 立取。
  - 与进程内 USER_SESSIONS 机制共存：两套拦截各自生效，已被
    一方吞掉的事件另一方不可见。
  - legacy 的 `@session_waiter` 装饰器在 Runner 内用该能力完整
    复刻（含 keep/stop/get_history_chains 语义）。
- [ ] **utils.io 工具 shim（按需）**：语料 28 个插件导入
  `astrbot.core.utils.io`（多为 `download_image_by_url`/
  `save_temp_img` 等不碰 host 状态的纯工具），可在 compat 层给
  纯函数 shim（下载走 plugin 进程自己，临时文件走 data/temp
  目录），把这类从设计排除救回。

- [x] **web.route（HTTP over RPC，已完成）**：`ctx.web.route` 注册的路由
  经 bridge 挂进 dashboard `registered_web_apis`，请求消毒（剥
  Cookie/Authorization）后回放为 WebRequestInfo，响应为
  WebResponseInfo + 逐 chunk ack 的背压流（SSE/大文件无特例）；
  大请求体经 read_body 反向拉取。旧版 `register_web_api` 在 Runner 内
  以真实 Quart test_request_context 回放（palworld 的
  `quart.g.username`/`jsonify`/`request.args` 全保真），starlette 的
  FileResponse/StreamingResponse 亦支持。palworld 验收通过
  （33/33）。多值 query、multipart 表单、UploadFile 走
  `astrbot.api.web` facade。
- [x] **views over RPC（已完成 v1 纯代理）**：Runner 的
  `invoke_views` 提供 `manifest`（页面清单 + i18n JSON 内联）与
  `read`（文件分 chunk 流式 + 穿越防护，双保险在 Runner 与
  serving 层各做一次）。bridge 启动时拉一次 manifest 缓存，
  `StarMetadata.views`（yaml 的 views/pages 声明）与 i18n 全部
  就位；PluginPageService 对 bridge 插件的 discover/serve 全部
  改走管线，HTML/CSS 重写与 iframe 沙箱行为与进程内插件一致，
  bridge 插件的磁盘直读路径被显式封死。缓存作为后续优化项保留。
- [x] **插件语料复扫（2026-10-05，2384 个插件）**：views 已从极少数
  走向实用——34 个插件声明 views/pages（0914 快照仅 5 个），31 个实际
  附带页面资产（33 个页面），字段只用 name/title/description/
  entry_file（均为默认 index.html），当前实现全覆盖；i18n 走
  `.astrbot-plugin/i18n/*.json` 目录发现（10+ 插件），与元数据无关。
  Web API 呈爆发式增长：269 个插件调用 register_web_api（0914 仅
  25 个），api.web facade 229 个、quart 方言 146 个；handler 中
  `request.files` 59 个、form 36 个、流式 29 个——quart 回放原生
  解析 multipart、facade 支持 UploadFile，均已覆盖。websocket 命中
  23 个但均为插件自管出站连接（register_web_api 本身不支持 ws
  路由），非兼容缺口。Blueprint 9 个，属插件自建 Quart app。结论：
  web/views 管线实现与真实语料吻合，无新增兼容缺口。
- [x] **插件语料真实导入扫（2026-10-05，2396 个插件根，core venv
  实测加载）**：经 `load_legacy_plugin` 全量真实导入（非静态扫描），
  两遍制（批量 + 可疑类单进程复跑消除批处理假象）。结果：
  **1544 ok（64.4%）**、382 设计排除（15.9%，astrbot.core 内部/
  register_platform_adapter 等，响报）、261 缺三方依赖（10.9%，
  per-plugin venv 范畴）、81 compat 长尾（3.4%）、70 error（2.9%）、
  30 invalid_def（1.3%）、26 无 metadata。长尾明细（均为 1-5 次零散
  项，后续按需收口）：legacy 类型外观（`Group`×9、`ContentPart`/
  `ThinkPart`/`AssistantMessageSegment`/`bind_checkpoint_messages`、
  `RerankProvider`×4、`StarMetadata`/`command_management`/`star_map`、
  `Location`/`Unknown`/`ComponentTypes`/`ComponentType`/`Dice`、
  `PlatformStatus`/`MessageSesion`/`Platform`、`VERSION`、
  `EventResultType`/`CommandResult`）；facade 属性缺口
  （`Context.platform_manager`、`CompatConfig` 任意键访问、
  `Image.to_dict`、`html_render`、`api.web.PluginRequest` 导出、
  `get_astrbot_root`）。复扫后修复：StarTools/sp/HtmlRenderer 三个
  facade 绑定提前到 import 之前（模块级调用 StarTools 的 10 个插件
  全部救回——8 个直接可加载，2 个各归其真实分类），ok 升至
  1552（64.8%）。error 桶大头：10 个模块级 StarTools 调用
  （loader 在 import 后才绑定，可将绑定提前）、4 个
  register_platform_adapter 属设计排除误分类，其余为坏插件
  （硬编码 /AstrBot 路径、缺配置键、import 期要 API key、语法错误）。
  invalid_def 中 ~8 个为 legacy tool 参数缺类型标注/docstring 类型
  （当前偏严，可放宽默认 str）。
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
- [x] **真实插件 bridge 级验收**（2026-10-05，32/33 通过）：33 个真实
  插件端到端经 SDKPluginBridge 验证（命令/旧版位置参数/hook 改写/
  工具注册与调用/配置注入/生命周期/per-plugin venv 含 playwright 与
  rapidocr+opencv 重量级安装）。修复了验收发现的 6 个问题：legacy
  grant 集缺 pipeline observe/modify、astrbot 自依赖过滤、工具签名
  variadic 容忍、空 __tool_params__ 逃逸、declared-but-empty 参数
  短路、重插件 initialize 超时（start_timeout=60s）。唯一未过：
  palworld（register_web_api，设计排除）。
- [x] **全量真实插件验收**（2026-10-05，2384 个插件，1750 ok / 634 fail，
  73.4%）：corpus `~/astrbot-plugin-research-1005` 全部 2384 个插件根，
  每插件独立 venv（`--system-site-packages` + core site-packages 链接）、
  uv 安装声明依赖、legacy 握手 + 全量 capability grant，测完即删 venv。
  本轮修复吃掉的分桶：config schema 嵌套 items 展开（76）、utils.io
  shim（25）、Location/Unknown（7）、VERSION 注入（4）、Group/
  PlatformStatus（6）、session_lock（14）、ContentPart（4）、
  stdout 污染协议帧（7 中 3 恢复——插件 print/pip 子进程写 fd 1，
  Runner 现在把 fd 1 重定向到 stderr、协议帧走 dup 后的私有 fd）。
  二轮复扫（同日，1879 ok / 505 fail，78.8%）：platform.raw 落地后
  `astrbot.core.platform.sources` 一桶基本消除；metadata.yaml 缺失
  容忍 + 握手 ctx 解析修复又吃 20+。三轮复扫（同日，1896 ok /
  488 fail，79.5%）：config.write 能力落地（CompatConfig.save_config
  走 RPC 全量快照，Host 侧 clear+update 后 save_config_async 提交，
  仅 legacy 可声明），21 个 save_config 插件 17 个恢复；其余 4 个
  属于其他已知桶。新发现缺口：130 个插件同步迭代
  `Context.get_all_providers()`（compat 当时是 async RPC，迭代
  coroutine 直接 TypeError；smart_imagechat_hub 在 initialize 期
  触发）。四轮复扫（2026-10-06，1954 ok / 430 fail，82.0%）：
  Context 快照接口落地（见下），60 个插件恢复、2 个回退
  （zai 上游删 API 的环境漂移；shutup_when_muted 缺
  AiocqhttpMessageEvent.send_message 类方法桩，已补）。`get_config`
  62、persona_manager 5、`get_all_providers` 130 三桶消除。
  剩余失败分桶（设计排除为主）：
  `astrbot.core` 直接导入 74+52+6、`Context.get_config()` 62、
  `register_platform_adapter` 19+9、
  `activate_llm_tool`/`get_llm_tool_manager` 7+6、`persona_manager` 5
  （等 personas 能力）、`pipeline` 9、`star.config`、
  `MediaResolver`/`compress_image`/`convert_audio`/`RerankProvider`
  （候选：并入 assets 能力）、`extract_quoted_message_images`
  （需平台适配器访问，host 能力候选）、`get_db` 4。
  插件自身问题：未声明/装不上三方依赖 17+4、`data.plugin*` 相对导入
  16、缺字体/数据文件、zbar 原生库、UTF-16/BOM 配置文件、只读文件
  系统假设。另有 4 个插件 initialize 期下载大型数据集超握手 60s
  超时（maimaidx/rollpigs/bilibili_learning_bot/local_reminiscence，
  进程内模式可加载但启动慢；TimeoutError 空消息问题已修复为可读
  报错）。过程中修复：legacy handler 发现改用
  `inspect.getattr_static`（插件类上的裸 assert property 不再
  中断加载）。
- [x] 旧版兼容层 platform.raw（已完成，仅 legacy）：133 个插件直接
  import `astrbot.core.platform.sources.*`（468 处 aiocqhttp）做两件事——
  isinstance 事件类判断、`event.bot.api.call_action` 原生 OneBot
  逃生舱。方案（用户拍板选项 2）：compat 新增 `platform_events.py`，
  按事件 platform 元数据选择真实的 facade 子类（isinstance 诚实，
  未知平台回退基类、isinstance 返回 False）；`LegacyBotProxy` 镜像
  aiocqhttp CQHttp 动态方法面，`bot.api.call_action`/`bot.<method>`
  经 `platform.raw` capability 转发到 Host 适配器（按 platform_id→
  meta().name 解析 adapter.bot，无 call_action 则回退同名方法，结果
  强制 JSON 安全）。**该能力只授予 legacy 插件**：bridge 对新 SDK
  插件声明 platform.raw 直接拒绝加载，新 SDK 公共 API 无对应面。
  wechatpadpro/gewechat 社区适配器为 import-only 壳（非 Host 平台）。
- [x] 旧版兼容层 Context 快照接口（2026-10-06）：`get_config(umo)`、
  `get_all_providers()`、`get_using_provider(umo)`（含 tts/stt）、
  `provider_manager`（provider_insts/stt/tts/embedding 列表、
  `curr_provider_inst`、`personas`、`persona_mgr`）、`persona_manager`
  （`get_persona_v3_by_id`、`get_default_persona_v3`、
  `resolve_selected_persona`）由握手时下发的 host 快照同步服务，
  与进程内 core 的同步语义对齐（原 async RPC 迭代 coroutine 的
  TypeError 桶消除）。方案：bridge 在插件启动时构建 JSON 快照
  （providers/defaults/umo 偏好/personas/全局配置），随 initialize
  握手 `host_info.snapshot` 下发；compat `host_snapshot.py` 复刻
  core 的 umo 路由（UmopConfigRouter fnmatch）与
  `_resolve_using_provider` 解析逻辑。**已知限制**：快照在加载时
  冻结，运行时 Host 侧改配置/增删 provider/persona 需重载插件才
  可见。`get_config` 返回的是黑名单递归脱敏副本（key/secret/
  password/token/credential 词命中即置空，用户拍板黑名单）。
  persona prompt 写入（meme_manager 式 `persona["prompt"] = x`）经
  `persona.write` 能力（仅 legacy）转发，Host 只改内存 personas_v3
  不落盘；persona CRUD（增删 persona）本轮抛
  IsolationUnsupportedError，等正式 personas 能力设计。
- [x] 旧版兼容层 manager facades（2026-10-06）：Context 上的 manager
  类对象全部落地——`platform_manager`（platform_insts/meta()/get_client()/
  bot；`event_queue` 属性抛 IsolationUnsupportedError）、
  `astrbot_config_mgr`（confs/default_conf/get_conf(umo)/get_conf_list/
  get_conf_info + `.ucr` 路由增删走 config.write RPC 并本地镜像）、
  `cron_manager`（add_basic_job/delete_job/list_jobs 走 `cron.schedule`
  RPC，handler 经 invoke_cron 帧回调插件进程；`SchedulerFacade.add_job`
  同步 fire-and-forget 对齐进程内语义，支持字符串别名/Date/Cron/
  Interval trigger 序列化）、`kb_manager`（list/get/create/delete/retrieve
  走 `kb` RPC；KbHelper 文档操作不 RPC）、`message_history_manager`
  （7 个操作走 `message.history` RPC）。读路径全部握手快照同步服务，
  写/操作方法透明 RPC 代理（用户拍板形状）。三个新 capability
  （cron.schedule/kb/message.history）均仅 legacy 可声明。
  **降级决策**：`ProviderManagerFacade.register_provider_change_hook`
  接受注册、告警一次、永不触发（standalone_profile 自带 reconcile
  可正常降级）；快照冻结限制同 Context 快照接口条目。
  **设计排除（事件注入类）**：rokid_bridge（自定义 Platform 适配器需
  event_queue）、smart_followup/image_generation/alipay_website（构造
  CronMessageEvent 注入 event_queue 的主动唤醒）。后三者相关 import
  （cron.events/tools.message_tools/astr_main_agent/utils.history_saver/
  entities.ToolCallsResult）已加 shim——插件可加载，走到注入点
  响亮报错。utils.config_number（coerce_int_config）为纯函数，
  compat 本地复刻。五轮复扫（同日，1966 ok / 418 fail，82.5%）：
  本轮 12 个恢复（rss_tool/bookkeeper/redpacket_notify/gcard_keeper/
  standalone_profile/staging_sync/smart_followup/image_generation/
  alipay_website/live_stream_companion/shutup_when_muted/tg_button）；
  31 个命中改动面的既有通过插件抽样复测零回退
  （event.bot.api/platform_manager/cron/history/kb/config_mgr/
  change_hook 各路径）。
- [ ] 旧版兼容层 P3（长尾，按需）：`astrbot.api.web` 路由
  （等 web 能力设计）、`EmbeddingProvider` 等类型外观的剩余零散项、
  `star_handlers_registry` 等注册表内省（当前降级为空）、
  persona CRUD（见上）。

- [ ] **Hook Decision 拦截**（`ToolCallDecision.block` /
  `MessageSendDecision.block`）：需要 core 在执行点增加拦截通道
  （tool 执行器的前置检查、只跳过单次发送不终止事件的通道）。
  当前 hook 只支持写操作修改，与旧版语义一致。

- [ ] **本地媒体组件的 host→plugin 保真**：`to_sdk_chain` 遇到本地文件
  媒体（无 http URL 的 Image/Record/Video/File）目前降级为惰性
  UnknownSegment，SDK 插件在 hook/事件里看不到真实图片。正解是 bridge
  把本地文件登记进 host AssetStore、向插件发 AssetRef（插件可经
  assets.download 取回）。顺带解决整链 set 写回时未知段无法重建 core
  组件的问题（当前 Poke/Json/Share 有特判，其余 loud fail）。

## 兼容性

- [ ] **Python 3.10 降级**：SDK 目前使用 PEP 695 语法（`class Plugin[ConfigT]`、
  `type` 语句），实际下限是 Python 3.12；市场承诺 3.10+。发布前改写降级，
  越早越便宜。
