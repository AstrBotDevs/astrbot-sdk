# 第一阶段 Capability 与 PluginContext

状态：草案。

本文定义新版插件第一阶段可申请的 capability，以及 `PluginContext`（下文简称 `ctx`）公开的接口边界。旧版插件兼容层不受本文的接口形状约束。

## 市场使用情况

数据来源：`/Users/moonshot/astrbot-plugin-analysis-0530`，抓取时间为 2026-05-30。

本次复核跳过了插件仓库中的测试目录，解析到 1180 个插件、8503 个 Python 文件。统计按插件去重，数字用于判断覆盖面，不作为 API 兼容承诺。

| 旧接口或行为 | 使用插件数 |
| --- | ---: |
| 插件数据目录 | 330 |
| 主动发送消息 | 256 |
| 平台原生 action | 168 |
| 读取 AstrBot 全局配置 | 159 |
| 直接调用 Provider `text_chat` | 158 |
| 获取当前 Provider | 140 |
| 按 ID 获取 Provider | 100 |
| `llm_generate` | 73 |
| HTML 渲染 | 66 |
| 插件 KV | 50 |
| 注册 LLM Tool | 165 |
| session waiter | 50 |
| conversation manager | 71 |
| persona manager | 32 |
| 注册 Web API | 20 |
| cron manager | 8 |
| 获取插件列表或元数据 | 28 |

事件和 Pipeline 扩展同样是主要用法：

| 注册方式 | 使用插件数 |
| --- | ---: |
| `command` | 763 |
| `event_message_type` | 339 |
| `permission_type` | 185 |
| `llm_tool` | 165 |
| `on_llm_request` | 133 |
| `on_decorating_result` | 95 |
| `on_llm_response` | 89 |
| `regex` | 74 |
| `session_waiter` | 50 |
| `after_message_sent` | 43 |

## Capability 规则

Capability 是用户授权和 Host 鉴权的单位，不与 Python 方法一一对应。

- `metadata.yaml` 声明插件必需和可选的 capability。
- 用户授权结果是插件声明与用户选择的交集。
- Host 在 handler 注册和每次 RPC 调用时检查授权。
- SDK 中的本地检查只用于提前报错，不能代替 Host 鉴权。
- capability 可以带 scope。scope 用于限制消息来源、Web 路由等资源。
- metadata schema 见 [metadata-v2.md](./metadata-v2.md)；本文只固定 capability ID 和语义。

隔离运行时，连接由 Host 绑定插件身份。Host 不信任 RPC 请求中由插件自行填写的插件 ID。

非隔离运行时，Host 仍检查正常的 SDK 调用，但 capability 不能构成安全边界。

## 默认可用接口

以下接口不需要用户授权。它们只能访问插件自身资源或当前调用上下文：

| 接口 | 语义 |
| --- | --- |
| `ctx.plugin` | 当前插件的 ID、名称、版本和运行模式 |
| `ctx.config` | 当前插件自己的类型化配置 |
| `ctx.logger` | 带插件身份的日志 |
| `ctx.capabilities` | 当前插件最近一次同步的 capability 授权 |
| `ctx.storage.data_dir` | Runner 本地的插件专属持久化目录 |
| `ctx.storage.get/set/delete` | Host 保存的插件专属 KV |
| `ctx.assets.upload/download` | Runner 与 Host 之间传输消息附件 |
| `lifecycle.*` | 当前插件的加载、配置更新和卸载 |
| `on.command` | 处理用户明确调用的插件命令 |
| `event.reply` | 为当前消息事件构造回复 Result |
| `yield` | 把控制权交给后续 Pipeline |

默认接口必须有插件级命名空间、大小限制和配额。插件数据目录是 Runner 本地路径；Host 不能直接使用该路径。SDK 在序列化消息时把本地图片、文件、音频和视频转换为 `AssetRef`。

`event.reply` 不直接发送消息。插件通过 `yield event.reply(...)` 提交 Result，把控制权交给后续 Pipeline。直接返回或 `yield` 一个 `MessageLike` 具有相同的默认回复语义。这个过程不需要 `message.send`。

## 第一阶段 Capability

### 消息

| Capability | 允许的行为 |
| --- | --- |
| `message.send` | 主动向一个 `UMO` 发送消息或 reaction；包括定时、后台和跨事件发送 |
| `message.receive` | 注册 `on.message` handler，接收未明确调用插件的普通平台消息 |
| `message.wait` | 认领一个会话的后续消息并拦截管线；多轮问答/向导的线性等待 |
| `message.observe` | 只读观察消息发送 Pipeline，例如发送完成事件 |
| `message.modify` | 在发送前读取、修改或阻止消息结果 |

`message.receive` 覆盖通用消息 handler、正则匹配和消息类型匹配。Host 必须先执行可序列化的 handler filter，再把匹配的事件发送给 Runner。Filter 会收窄插件实际收到的事件，但不能代替 capability 授权。

用户明确调用的 command 不需要 `message.receive`。需要持续处理某个 UMO 的插件应保存自己的会话状态，并使用受 `message.receive` 约束的 `on.message` handler。

### 模型与 Agent

| Capability | 允许的行为 |
| --- | --- |
| `llm.generate` | 调用聊天模型、流式生成，并读取可用聊天 Provider 的公开元数据 |
| `llm.agent` | 启动可执行 Tool 的多步 Agent loop |
| `llm.embed` | 调用 Embedding Provider |
| `speech.transcribe` | 调用 STT Provider |
| `speech.synthesize` | 调用 TTS Provider |
| `llm.tool.register` | 向 AstrBot Agent 注册当前插件提供的 Tool |
| `llm.observe` | 只读观察 LLM request、response、Agent 和 Tool 调用 Pipeline |
| `llm.modify` | 读取并修改 LLM request、response、Tool call 或 Tool result |

`llm.generate` 不返回 Provider 实例。选择模型使用 `provider_id`，调用结果使用 SDK DTO。

调用 `ctx.llm.run_agent` 必须同时拥有 `llm.generate` 和 `llm.agent`。Agent 调用插件 Tool 时，Tool handler 仍受当前插件授权约束。

注册 hook 需要对应域的 `.observe`。应用 hook 的修改或 Decision 需要对应域的 `.modify`；`.modify` 隐含对应域的 `.observe`。

hook 收到的是 Runner 本地可写 DTO，不是 Host 内部可变对象。插件直接修改字段，SDK 把记录到的写操作转换为类型化修改请求，由 Host 校验授权后应用。未授予 `modify` 时，写操作在 Runner 本地生效但不转发，不影响 Pipeline。进程内适配器也必须保持这一约束。

### AstrBot 数据

| Capability | 允许的行为 |
| --- | --- |
| `conversation.read` | 读取指定 UMO 的当前对话、对话列表和消息上下文 |
| `conversation.write` | 创建、切换、更新和删除对话，追加对话消息 |
| `persona.read` | 读取 Persona 列表、指定 Persona 和 UMO 当前生效的 Persona |

`conversation.write` 不隐含 `conversation.read`。需要读后写的插件必须同时声明两项。

第一阶段不提供 Persona 写入。

### Host 扩展

| Capability | 允许的行为 |
| --- | --- |
| `render.image` | 使用 Host 的 HTML 或文本转图片服务 |
| `web.route` | 在当前插件的路由命名空间下注册 HTTP handler |
| `scheduler.manage` | 创建和管理属于当前插件的持久化任务 |
| `plugin.inspect` | 读取已安装插件的公开元数据 |

`web.route` 只能注册到 Host 分配的插件前缀，不能覆盖 AstrBot 或其他插件的路由。

`scheduler.manage` 只能访问当前插件创建的任务。持久化任务引用 handler ID，不能持久化 Python callable。

`plugin.inspect` 只返回 `PluginInfo`，不返回插件实例、模块、配置或运行时对象。

## PluginContext

第一阶段的 `ctx` 由服务对象组成：

```python
class PluginContext(Generic[ConfigT]):
    plugin: PluginInfo
    config: ConfigT
    logger: PluginLogger
    capabilities: CapabilitySet

    storage: PluginStorage
    assets: AssetService
    messages: MessageService
    llm: LLMService
    conversations: ConversationService
    personas: PersonaService
    render: RenderService
    scheduler: SchedulerService
    web: WebService
    tools: ToolService
    plugins: PluginRegistryService
```

SDK 返回的对象必须是不可变 DTO、值对象或带 ID 的 Ref。`ctx` 不返回 Host 中的 Manager、Provider、Platform、数据库模型或插件实例。

`Plugin.config` 和 `Plugin.logger` 分别是 `ctx.config`、`ctx.logger` 的只读快捷入口。Host 服务不在 `Plugin` 上重复代理。

`ctx.capabilities` 是 Host 最近一次同步的只读授权快照：

```python
class CapabilitySet:
    def has(self, capability_id: str) -> bool: ...
    def get(self, capability_id: str) -> CapabilityGrant | None: ...


class CapabilityGrant:
    id: str
    scope: Mapping[str, JSONValue]
```

授权变化由 Host 原子替换整个快照。该接口用于 optional capability 的功能降级，不能代替 Host 在注册和 RPC 调用时的鉴权。

### Storage 与资产

```python
class PluginStorage:
    data_dir: Path

    async def get(self, key: str, default: JSONValue = None) -> JSONValue: ...
    async def set(self, key: str, value: JSONValue | bytes) -> None: ...
    async def delete(self, key: str) -> None: ...


class AssetService:
    async def upload(
        self,
        source: Path | bytes,
        *,
        filename: str | None = None,
        media_type: str | None = None,
    ) -> AssetRef: ...

    async def download(self, asset: AssetRef) -> Path: ...
```

`AssetRef` 可以通过 stdio 或 WebSocket 传输。消息段通过本地文件或 bytes 构造时，SDK 在序列化 Result 或主动消息时自动上传；协议中只传 `AssetRef`，不传 Runner 本地路径。显式 `upload()` 用于复用或持久化资产引用。

### 消息

```python
@dataclass(frozen=True, slots=True)
class UMO:
    platform_id: str
    message_type: MessageType
    session_id: str


class MessageService:
    async def send(
        self,
        umo: UMO,
        content: MessageLike,
    ) -> SendReceipt: ...

    async def react(
        self,
        message: MessageRef,
        emoji: str,
    ) -> None: ...
```

UMO 是结构化、可持久化的消息会话值对象，不是需要解析的拼接字符串。插件可以构造 UMO；Host 在调用时校验目标、capability 和 scope。

`MessageService` 只负责脱离当前事件的主动操作。当前事件上的回复属于 `MessageEvent`：

```python
class MessageEvent:
    umo: UMO
    platform_type: str
    message: MessageChain

    def reply(
        self,
        content: MessageLike,
        *,
        quote: bool = False,
        propagation: Propagation = Propagation.CONTINUE,
    ) -> MessageResult: ...
```

`event.reply` 是纯 Result 构造操作，不产生立即发送的副作用。传播控制使用 `Propagation.CONTINUE` 或 `Propagation.STOP`。

`event.umo.platform_id` 标识平台适配器实例，`event.platform_type` 表示适配器类型。二者都不提供 platform client、adapter 或原生 action 调用。

`on.message` 产生的 filter spec 必须可序列化。任意 Python predicate 不能传给 Host。

消息 handler 收到的 `MessageEvent`、回复 Result 和消息组件不放在 `ctx` 中。它们属于事件与结果类型。

多轮问答通过 `message.wait` 能力的线性会话等待完成：

```python
class SessionService:
    def wait(
        self,
        event: MessageEvent | None = None,
        *,
        filter: SessionFilter | None = None,
        timeout: float = 60.0,
    ) -> SessionWait: ...

class SessionWait:
    async def next(self, timeout: float | None = None) -> MessageEvent: ...
    async def ask(
        self,
        content: MessageLike,
        timeout: float | None = None,
    ) -> MessageEvent: ...
```

`SessionFilter` 是代码形态的会话键映射，在 Runner 侧求值；内置 `DefaultSessionFilter`（按 UMO）与 `SenderSessionFilter`（按 UMO + 发送者）。匹配到的入站消息被 Host 拦截并投递给等待方，对其他插件与管线不可见；`next()` 逐轮重置 Host 侧计时，超时抛 `TimeoutError`，退出 `async with` 即释放会话认领。

### LLM

```python
class LLMService:
    async def current_provider(
        self,
        kind: ProviderKind,
        *,
        umo: UMO | None = None,
    ) -> ProviderInfo | None: ...

    async def list_providers(
        self,
        kind: ProviderKind,
    ) -> list[ProviderInfo]: ...

    async def generate(
        self,
        request: str | ChatRequest,
        *,
        umo: UMO | None = None,
        provider_id: str | None = None,
        system_prompt: str | None = None,
    ) -> ChatResponse: ...

    def stream(
        self,
        request: str | ChatRequest,
        *,
        umo: UMO | None = None,
        provider_id: str | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[ChatChunk]: ...

    async def run_agent(self, request: AgentRequest) -> AgentResponse: ...
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
    async def transcribe(self, request: TranscriptionRequest) -> Transcript: ...
    async def synthesize(self, request: SpeechRequest) -> AssetRef: ...
```

传入 `str` 是单轮文本生成的快捷写法。需要消息历史、多模态输入、Tool 或高级参数时传入 `ChatRequest`；此时不能再传快捷关键字参数。

`ChatRequest` 使用 `provider_id` 和可选 `umo` 选择 Provider。`ProviderInfo` 只包含 ID、类型、模型名和显示信息。

`current_provider` 和 `list_providers` 只返回插件已获得对应模型 capability 的 Provider 类型。

不提供 `get_using_provider()` 或 `get_provider_by_id()` 形式的 Provider 代理。旧版兼容层可以提供代理，但新版接口直接表达操作。

### Conversation

```python
class ConversationService:
    async def current(self, umo: UMO) -> Conversation | None: ...
    async def get(
        self,
        umo: UMO,
        conversation_id: str,
    ) -> Conversation | None: ...
    async def list(
        self,
        umo: UMO,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[Conversation]: ...

    async def create(
        self,
        umo: UMO,
        *,
        title: str | None = None,
        persona_id: str | None = None,
        messages: Sequence[Message] = (),
    ) -> Conversation: ...
    async def set_current(
        self,
        umo: UMO,
        conversation_id: str,
    ) -> None: ...
    async def update(
        self,
        umo: UMO,
        conversation_id: str,
        patch: ConversationPatch,
    ) -> Conversation: ...
    async def append(
        self,
        umo: UMO,
        conversation_id: str,
        messages: Sequence[Message],
    ) -> None: ...
    async def delete(
        self,
        umo: UMO,
        conversation_id: str,
    ) -> None: ...
```

`Conversation.messages` 是结构化消息列表，不使用 JSON 字符串保存到 DTO。

### Persona

```python
class PersonaService:
    async def current(
        self,
        umo: UMO,
        *,
        conversation_id: str | None = None,
    ) -> PersonaInfo: ...

    async def get(self, persona_id: str) -> PersonaInfo | None: ...
    async def list(self) -> list[PersonaInfo]: ...
```

### 渲染

```python
class RenderService:
    async def html(
        self,
        template: str,
        data: Mapping[str, JSONValue],
        *,
        options: RenderOptions | None = None,
    ) -> AssetRef: ...

    async def text(
        self,
        text: str,
        *,
        options: TextRenderOptions | None = None,
    ) -> AssetRef: ...
```

### 定时任务

```python
class SchedulerService:
    async def upsert(
        self,
        job_id: str,
        schedule: Schedule,
        *,
        handler_id: str,
        payload: JSONValue = None,
        enabled: bool = True,
    ) -> ScheduledJob: ...

    async def get(self, job_id: str) -> ScheduledJob | None: ...
    async def list(self) -> list[ScheduledJob]: ...
    async def delete(self, job_id: str) -> None: ...
    async def run(self, job_id: str) -> None: ...
```

`Schedule` 第一阶段支持 `At`、`Interval` 和 `Cron`。Host 触发任务时，通过 RPC 调用 Runner 中已注册的 `handler_id`。

### Web 路由

```python
class WebService:
    def register(
        self,
        route: RouteSpec,
        handler: WebHandler,
    ) -> RouteRef: ...

    def unregister(self, route: RouteRef) -> None: ...
    def url_for(self, route: RouteRef) -> str: ...
```

Web request、upload 和 response 使用 SDK 类型。Host HTTP 对象不能传入 Runner。

### Tool

```python
class ToolService:
    def register(self, tool: ToolDefinition, handler: ToolHandler) -> ToolRef: ...
    def unregister(self, tool: ToolRef) -> None: ...
```

插件只能管理自己注册的 Tool。第一阶段不提供全局 Tool 的启用、停用和删除。

### 插件元数据

```python
class PluginRegistryService:
    async def get(self, plugin_id: str) -> PluginInfo | None: ...
    async def list(self) -> list[PluginInfo]: ...
```

## Handler Namespace

新版 SDK 把插件交互分为四类：

| Namespace | 职责 |
| --- | --- |
| `on.*` | 声明什么外部输入会调用插件 |
| `hooks.*` | 挂入 AstrBot 内部 Pipeline |
| `lifecycle.*` | 处理当前插件自身的生命周期 |
| `ctx.*` | 主动调用 Host 服务 |

每种 lifecycle 一个插件只能注册一个 handler。它们只处理当前插件，不需要 capability。

`on.command` 和 `on.message` 是不同授权语义。command 由用户明确调用，默认可用；`on.message` 观察普通平台消息，需要 `message.receive`。

正则表达式是 message filter，不是独立事件类型。新版接口使用 `on.message(regex=...)`；旧版兼容层继续支持 `filter.regex`。

`hooks.*` 使用独立的 SDK namespace，但不创建同名 capability。hook 是接入机制，实际权限由它观察或修改的 Pipeline 资源决定。

## Handler 注册与 Capability

不是所有能力都通过 `ctx` 调用。Runner 在注册 handler 时也要检查 capability。

| Handler | Capability |
| --- | --- |
| `on.command` | 默认可用 |
| `on.message`、regex、消息类型 handler | `message.receive` |
| `on.tool` | `llm.tool.register` |
| `hooks.llm_request/llm_response/tool_call/tool_result` | `llm.observe`；修改或拦截需 `llm.modify` |
| `hooks.agent_start/agent_end` | `llm.observe` |
| `hooks.message_result` | `message.observe`；修改或拦截需 `message.modify` |
| `hooks.message_sent` | `message.observe` |
| Web route | `web.route` |
| scheduled job | `scheduler.manage` |
| `lifecycle.startup/config_changed/shutdown` | 默认可用 |

`permission_type`、平台类型和消息类型等声明是 handler filter，用于继续收窄触发范围，不是独立 capability。

## 旧接口到新接口

| 旧接口 | 新接口 |
| --- | --- |
| `context.send_message` | `ctx.messages.send` |
| `event.unified_msg_origin`、`event.session` | `event.umo` |
| `event.send` | `ctx.messages.send(event.umo, ...)` |
| `session_waiter` | 第一阶段新版不提供；旧版兼容层处理 |
| `filter.regex`、`filter.event_message_type` | `on.message` 的可序列化 filter |
| LLM、Agent、Tool 和消息 Pipeline hook | `hooks.*` |
| `get_using_provider`、`get_provider_by_id` | `ctx.llm.current_provider`、`ctx.llm.list_providers` |
| `provider.text_chat`、`context.llm_generate` | `ctx.llm.generate` |
| `context.tool_loop_agent` | `ctx.llm.run_agent` |
| `conversation_manager` | `ctx.conversations` |
| `persona_manager` | `ctx.personas` |
| `event.bot.api.call_action` | 第一阶段新版不提供；旧版兼容层处理 |
| `html_render`、`text_to_image` | `ctx.render.html`、`ctx.render.text` |
| `put_kv_data`、`get_kv_data`、`delete_kv_data` | `ctx.storage.set/get/delete` |
| `StarTools.get_data_dir` | `ctx.storage.data_dir` |
| `register_web_api` | `ctx.web.register` 或 Web route decorator |
| `cron_manager` | `ctx.scheduler` |
| `get_all_stars`、`get_registered_star` | `ctx.plugins.list/get` |
| `context.get_config()` | 不提供原始映射；改用对应的类型化服务 |

## 第一阶段不提供

以下接口只由旧版兼容层处理，不进入新版 `ctx`：

- AstrBot 全局配置对象；
- Provider、ProviderManager 和全局 ToolManager；
- Platform、PlatformManager 和平台 client；
- 平台适配器原生 action；
- 新版 session waiter；
- AstrBot 数据库和 ORM 对象；
- Event Queue；
- 插件实例和 Star registry；
- Persona 创建、修改和删除；
- 平台消息历史库的直接读写；
- Knowledge Base 管理；
- Provider 和 Platform Adapter 注册；
- 其他插件的启用、停用、重载和卸载。

后续阶段应根据明确用例增加类型化服务，不恢复 Manager 透传。

## 协议约束

- stdio、WebSocket 和进程内适配器提供同一套公开语义。
- 所有参数、返回值和异常都有稳定的协议类型。
- RPC 支持 deadline、取消和流式背压。
- 本地文件通过 `AssetRef` 传输。
- callback 在协议中表示为 Runner 注册的 handler ID。
- Host 返回 `CAPABILITY_DENIED` 时包含 capability ID，不包含敏感授权状态。
- capability 或 scope 在运行中被撤销后，下一次调用立即失败；已有流和任务由 Host 取消。
