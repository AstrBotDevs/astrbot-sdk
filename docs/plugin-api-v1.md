# 新版插件 API

状态：草案。

本文定义新版插件第一阶段的 Python API。Capability 和 `PluginContext` 服务见 [capabilities-v1.md](./capabilities-v1.md)，插件版本与运行模式见 [plugin-runtime-model.md](./plugin-runtime-model.md)。

第一阶段不提供 session waiter、`event.wait_next()`、任意 Python 消息过滤器或平台原生 action。

## 设计结论

新版 API 遵循以下约束：

- 一个插件只使用一套 API family。新版插件不导入或注册 `astrbot.api.*`。
- 保留已经清晰且使用广泛的概念：异步方法、类型化指令参数、`yield`、`MessageChain`、`Plain`、`At`、`Reply`、`Record` 等消息段。
- 改掉旧接口中的结构性问题：空函数式 command group、堆叠装饰器、可变 event、全局 manager 和平台对象透传。
- 简单插件保持短小；只有使用 Host 能力时才接触 `ctx` 和 capability。
- 同一份插件代码在 stdio、WebSocket 和进程内适配器下保持相同语义。

保留熟悉的名称不等于运行旧版兼容层。`astrbot_sdk` 中的类型都属于新版 API，并转换为新版注册描述和 RPC DTO。

## API Family 与混用

`metadata.yaml` 必须选择旧版或新版 SDK schema。Runner 只启用对应的加载器和注册表。

允许：

- 同一个 AstrBot 实例同时运行旧版和新版插件。
- 一个插件仓库在迁移期间保存旧版和新版源码，但一次加载只能选择一个入口。
- 新版 API 保留 `yield`、`MessageChain` 和现有消息段名称。

不允许：

- 新版插件导入 `astrbot.api.*`、继承 `Star` 或接收旧版 `Context`、`AstrMessageEvent`。
- 旧版插件在同一次加载中注册 `astrbot_sdk` handler。
- 同一个方法同时使用旧版和新版装饰器。
- 在新版 handler 中传入旧版消息段、Result、Provider、Manager 或平台对象。

Runner 发现两套注册表都产生当前插件的注册项时，必须拒绝加载并返回 `MIXED_PLUGIN_API`。不能根据导入扫描决定插件类型；`metadata.yaml` 是唯一判据。

第一阶段不在 `astrbot_sdk` 中提供 `filter`、`Star`、`AstrMessageEvent`、`plain_result` 等旧名 alias。否则新版会长期保留两套写法，同名方法也容易被误解为仍有旧版的可变对象语义。需要原样运行旧源码时使用 legacy 模式；迁移为新版时使用下文的明确映射。

## 最小插件

```python
from astrbot_sdk import MessageEvent, Plugin, on


class HelloPlugin(Plugin):
    @on.command("hello")
    async def hello(self, event: MessageEvent):
        yield event.reply(f"Hello, {event.sender.name}!")
```

`metadata.yaml` 的 `runtime.entrypoint` 指向一个 `Plugin` 子类。Runner 为每个插件创建一个实例，并在调用生命周期方法和 handler 前注入 `self.ctx`。

插件不注册全局单例，不在模块导入或 `__init__` 中启动任务、连接网络或调用 Host。

## Public Imports

高频入口从顶层导出：

```python
from astrbot_sdk import MessageEvent, Plugin, PluginContext, UMO, hooks, lifecycle, on
```

领域类型从稳定子模块导入：

```python
from astrbot_sdk.config import PluginConfig
from astrbot_sdk.events import MessageType, SenderRole
from astrbot_sdk.message_components import At, Image, Plain, Record, Reply
from astrbot_sdk.messages import MessageChain
from astrbot_sdk.results import EventResult, MessageResult, Propagation
```

`astrbot_sdk` 中未记录为 public 的模块不属于兼容承诺。新版插件不导入 `astrbot.core.*`。

## Plugin

```python
ConfigT = TypeVar("ConfigT", bound=PluginConfig)


class Plugin(Generic[ConfigT]):
    config_model: ClassVar[type[ConfigT] | None]
    ctx: PluginContext[ConfigT]

    @property
    def config(self) -> ConfigT: ...

    @property
    def logger(self) -> PluginLogger: ...
```

- Runner 使用 `PluginContext` 构造插件实例，子类不得要求其他构造参数。
- 插件不覆盖 `__init__`。初始化和清理使用 `lifecycle.*`。
- `self.config` 和 `self.logger` 是 `self.ctx.config`、`self.ctx.logger` 的高频快捷入口。
- 一个 Runner 可以加载多个插件，但每个插件拥有独立实例、配置、存储和 capability。
- handler 必须是插件实例的异步方法。

### 类型化配置

```python
from pydantic import Field

from astrbot_sdk import Plugin
from astrbot_sdk.config import PluginConfig


class Config(PluginConfig):
    greeting: str = Field(default="Hello", description="Reply prefix")


class GreetingPlugin(Plugin[Config]):
    config_model = Config
```

`PluginConfig` 基于 Pydantic v2。Host 使用 JSON Schema 生成配置界面，并在配置进入 Runner 前完成校验。

没有配置的插件省略泛型参数和 `config_model`。新版插件只读取自己的类型化配置，不读取 AstrBot 全局配置。

## 注册模型

新版 SDK 使用四个 namespace：

| Namespace | 职责 |
| --- | --- |
| `on.*` | 声明外部输入如何调用插件 |
| `hooks.*` | 观察或修改 AstrBot Pipeline |
| `lifecycle.*` | 处理当前插件自身的加载、配置更新和卸载 |
| `ctx.*` | 主动调用 Host 服务 |

装饰器只为方法附加静态元数据。模块导入时不连接 Host，Runner 在插件实例创建后统一提交注册信息。

每个 `on.*` 和 `hooks.*` 注册项都有插件内唯一的 handler ID。默认 ID 是方法名；被持久化任务或路由引用时必须显式指定：

```python
@on.command("cleanup", id="cleanup-command")
async def cleanup(self, event: MessageEvent):
    ...
```

第一阶段一个方法只能有一个入口、hook 或 lifecycle 装饰器。消息类型、平台、角色和正则条件通过入口装饰器参数声明，不堆叠 filter 装饰器。

`on` 表示注册入口，filter 只是入口的匹配条件。这样不会再把 command、Tool、消息过滤器和 Pipeline hook 都放进同一个 `filter` namespace。

Python 层的常用过滤参数最终编译为协议中的类型化 `FilterSpec` 列表。后续可以增加新的 Host 内置 filter 类型，但不能把 Python callable 序列化到 Host。

下文的 handler、hook 和 lifecycle 示例均定义在 `Plugin` 子类中。

公共注册参数：

| 参数 | 语义 |
| --- | --- |
| `id` | 插件内稳定且唯一的 handler ID |
| `description` | 面向用户或管理员的简短说明；缺省时读取方法 docstring 第一段 |
| `priority` | 整数；值越大越先执行，默认 `0` |

同优先级 handler 的相对顺序不属于 API 承诺。

## Command

```python
on.command(
    path: str,
    *,
    aliases: Sequence[str] = (),
    description: str | None = None,
    message_types: Collection[MessageType] = (),
    platforms: Collection[str] = (),
    roles: Collection[SenderRole] = (),
    priority: int = 0,
    id: str | None = None,
)
```

`path` 是由空格分隔的完整指令路径：

```python
@on.command("math add")
async def add(self, event: MessageEvent, a: int, b: int):
    yield str(a + b)
```

Host 根据完整路径自动构建 command tree。无需声明只包含 `pass` 的 command group。`aliases` 同样使用完整路径。

command 参数由 Host 根据方法签名解析。`MessageEvent` 是注入参数，不计入用户参数；不需要事件信息时可以省略：

```python
@on.command("ping")
async def ping(self) -> str:
    return "pong"
```

第一阶段支持：

- `str`、`int`、`float`、`bool`；
- `Enum` 和 `Literal`；
- `T | None`；
- 带默认值的可选参数；
- 最后一个参数使用 `Annotated[str, Rest()]` 接收剩余文本。

```python
from typing import Annotated

from astrbot_sdk.commands import Rest


@on.command("ask")
async def ask(
    self,
    event: MessageEvent,
    prompt: Annotated[str, Rest()],
):
    ...
```

解析失败时 Host 返回统一参数错误，插件 handler 不会被调用。command 由用户明确调用，不需要 `message.receive`。

## Message Handler

```python
on.message(
    *,
    message_types: Collection[MessageType] = (),
    platforms: Collection[str] = (),
    roles: Collection[SenderRole] = (),
    regex: str | Pattern[str] | None = None,
    priority: int = 0,
    id: str | None = None,
    description: str | None = None,
)
```

```python
from astrbot_sdk import MessageEvent, on
from astrbot_sdk.events import MessageType


@on.message(
    message_types={MessageType.GROUP},
    regex=r"^天气\s+",
)
async def weather_message(self, event: MessageEvent):
    ...
```

规则：

- 不同参数之间是 AND 关系。
- 同一个 collection 内是 OR 关系。
- 空 collection 表示不限制该字段。
- 没有任何条件时接收所有普通平台消息。
- `platforms` 使用适配器类型字符串，不使用用户配置的实例 ID。
- `regex` 由 Host 对规范化后的 `event.text` 执行。
- 传入已编译的 `Pattern` 时，SDK 序列化 pattern 和 flags。
- Host 完成过滤后才把事件发送给 Runner。
- 第一阶段不接受 callable、lambda 或自定义 Python filter。

`on.message` 需要 `message.receive`。

## Tool Handler

静态 LLM Tool 使用 `on.tool` 注册：

```python
on.tool(
    name: str,
    *,
    description: str,
    id: str | None = None,
)
```

```python
from typing import Annotated

from pydantic import Field

from astrbot_sdk import on


@on.tool(name="get_weather", description="Get weather for a city")
async def get_weather(
    self,
    city: Annotated[str, Field(description="City name")],
) -> str:
    return f"The weather in {city} is sunny."
```

需要调用上下文时，显式声明 `ToolCallContext` 注入参数：

```python
from astrbot_sdk.tools import ToolCallContext


@on.tool(name="audit_query", description="Query the audit service")
async def audit_query(
    self,
    call: ToolCallContext,
    query: str,
) -> dict:
    self.logger.info("tool_call_id=%s", call.id)
    return {"query": query}
```

- `ToolCallContext` 不进入 Tool 参数 JSON Schema。
- Tool 调用不一定来自消息事件；该对象不提供 `reply()`、传播控制或平台对象。
- Tool 参数 Schema 来自 Python 类型注解和 `Annotated` 元数据。
- 返回值可以是 `str`、JSON value 或 `ToolResult`。
- Tool handler 不返回消息 Result，也不使用 `yield`。
- Tool handler 需要 `llm.tool.register`。
- 动态 Tool 使用 `ctx.tools`，并受同一 capability 约束。

## Lifecycle

```python
from astrbot_sdk import lifecycle
from astrbot_sdk.lifecycle import ConfigChangedEvent, ShutdownEvent


@lifecycle.startup
async def startup(self) -> None:
    self.cache = await self._load_cache()


@lifecycle.config_changed
async def config_changed(
    self,
    event: ConfigChangedEvent[Config],
) -> None:
    ...


@lifecycle.shutdown
async def shutdown(self, event: ShutdownEvent) -> None:
    await self._close_clients()
```

- `startup` 在注册校验完成、开始接收调用前执行；失败会使插件启动失败。
- `config_changed` 执行时，`self.config` 已指向校验后的新配置；event 提供 previous 和 current。
- `shutdown` 在插件停用、重载或 Host 关闭时执行，并带有 Host 指定的 deadline。
- 每种 lifecycle 一个插件只能注册一个 handler，重复注册返回 `DUPLICATE_LIFECYCLE_HANDLER`。
- lifecycle 使用 Runner 保留的固定 handler ID，不接受自定义 `id` 或 `priority`。
- lifecycle 只处理当前插件，不需要 capability。
- lifecycle handler 返回 `None`，不使用 `yield`。

## Hooks

hooks 挂入 AstrBot Pipeline 的固定阶段。每个阶段一个装饰器，不区分 observe 和 modify 命名空间：

| Decorator | 注入参数 | 可返回 | Capability |
| --- | --- | --- | --- |
| `hooks.llm_request()` | `(event, request)` | `None` | `llm.observe`；修改需 `llm.modify` |
| `hooks.llm_response()` | `(event, response)` | `None` | `llm.observe`；修改需 `llm.modify` |
| `hooks.tool_call()` | `(event, call)` | `ToolCallDecision \| None` | `llm.observe`；修改或拦截需 `llm.modify` |
| `hooks.tool_result()` | `(event, result)` | `None` | `llm.observe`；修改需 `llm.modify` |
| `hooks.message_result()` | `(event, result)` | `MessageSendDecision \| None` | `message.observe`；修改或拦截需 `message.modify` |
| `hooks.message_sent()` | `(event,)` | `None` | `message.observe` |
| `hooks.agent_start()` | `(event,)` | `None` | `llm.observe` |
| `hooks.agent_end()` | `(event,)` | `None` | `llm.observe` |

注入参数与 command、Tool handler 使用同一套类型注入规则。`event` 是触发当前 Pipeline 的 `MessageEvent`；插件通过 `ctx.llm.generate` 等主动发起的调用没有消息事件，此时 `event` 为 `None`，需要区分来源的插件声明 `event: MessageEvent | None`。hook 不使用 `yield`，在 hook 中调用 `event.reply()` 构造的 Result 会被丢弃。

### 修改模型

hook 收到的 `request`、`response`、`call`、`result` 是 Runner 本地的可写 DTO，不是 Host 内部对象。插件直接修改字段：

```python
from astrbot_sdk import MessageEvent, hooks
from astrbot_sdk.hooks import LLMRequest


@hooks.llm_request(priority=10)
async def add_instruction(
    self,
    event: MessageEvent | None,
    request: LLMRequest,
) -> None:
    request.system_prompt += "Answer concisely."
```

handler 返回后，SDK 把记录到的写操作转换为类型化修改请求发送给 Host，Host 校验授权后应用：

- 注册 hook 需要对应域的 `.observe` capability（LLM 阶段为 `llm.observe`，消息发送阶段为 `message.observe`）。
- 应用修改需要对应域的 `.modify` capability。未授予 modify 时，插件的写操作在 Runner 本地仍然生效，但不转发给 Host，修改不影响 Pipeline，并向插件 logger 写入一条 warning。插件代码路径不需要感知授权状态。
- 需要主动降级的插件可以检查 `ctx.capabilities.has(...)`。

可写 DTO 支持的写操作：字段赋值、list append/extend/clear、整体替换 list。写入未定义的字段立即抛出 `AttributeError`。

同一阶段的所有 hook 按 priority 从高到低依次执行，每个 hook 看到之前 hook 的修改应用后的 current 状态。

### 拦截

`tool_call` 和 `message_result` 阶段可以返回 Decision 拦截当前操作：

```python
from astrbot_sdk import MessageEvent, hooks
from astrbot_sdk.hooks import ToolCall, ToolCallDecision


@hooks.tool_call()
async def guard(
    self,
    event: MessageEvent | None,
    call: ToolCall,
) -> ToolCallDecision | None:
    if "drop" in call.args.get("query", ""):
        return ToolCallDecision.block(reason="Dangerous query")
    return None
```

- `ToolCallDecision.block` 阻止本次 Tool 调用。
- `MessageSendDecision.block` 阻止消息发送。
- 拦截需要对应域的 `.modify` capability；未授予时 Decision 不生效，并写入 warning 日志。
- 第一阶段 `agent_start`/`agent_end` 和 `message_sent` 没有可修改的 DTO，也没有 Decision。

hooks 返回 `None` 或 Decision，不使用 `yield`。

## UMO

`UMO`（Unified Message Origin）是消息会话的不可变值对象：

```python
@dataclass(frozen=True, slots=True)
class UMO:
    platform_id: str
    message_type: MessageType
    session_id: str
```

- `platform_id` 是 AstrBot 中唯一的平台适配器实例 ID。
- `message_type` 表示私聊、群聊等消息会话类型。
- `session_id` 是该平台实例内的会话 ID。
- UMO 可以序列化和持久化，用于主动发送、对话和 Persona 服务。
- 插件可以构造 UMO，Host 在每次调用时校验目标、capability 和 scope。
- stdio 和 WebSocket 中使用结构化对象传输，不拼接为协议字符串。
- 旧版 `unified_msg_origin` 字符串由兼容层负责转换。

UMO 只定位消息会话，不代表 AstrBot 的 LLM Conversation。

## MessageEvent

`MessageEvent` 是不可变事件上下文，不是 Host `AstrMessageEvent` 的代理：

```python
class MessageEvent:
    id: str
    umo: UMO
    platform_type: str
    message_ref: MessageRef
    message: MessageChain
    text: str
    sender: Sender
    timestamp: datetime
    command: CommandInvocation | None
    is_wake: bool

    @property
    def is_private(self) -> bool: ...

    @property
    def is_group(self) -> bool: ...

    def reply(
        self,
        content: MessageLike,
        *,
        quote: bool = False,
        propagation: Propagation = Propagation.CONTINUE,
    ) -> MessageResult: ...

    def stop(self) -> EventResult: ...
```

常用数据使用只读属性：

```python
event.text
event.sender.id
event.sender.name
event.sender.role
event.umo.platform_id
event.umo.message_type
event.umo.session_id
event.platform_type
```

`event.message` 是适配器解析后的入站消息段序列。Host 将 AstrBot 消息链逐段序列化为 SDK DTO，保留顺序和可公开字段。

`event.text` 只是把顶层 `Plain` 段连接后的便捷视图。读取 `At`、`Reply`、`Record` 等内容时应遍历 `event.message`。

不提供：

- `raw_message`；
- `event.bot`；
- platform client 或 adapter；
- 可变 extras；
- `set_result()`、`clear_result()` 等 Host 状态修改方法；
- `send()` 或 `wait_next()`。

当前事件回复使用 `event.reply()` 或直接返回、`yield` 一个 `MessageLike`。脱离当前事件主动发送使用 `ctx.messages.send(event.umo, ...)`。

## MessageChain 与消息段

`MessageChain` 是有序、不可变的消息段集合：

```python
class MessageChain(Sequence[MessageSegment]):
    segments: tuple[MessageSegment, ...]

    def __init__(self, *segments: MessageSegment) -> None: ...
```

`MessageLike` 是高频参数的联合类型：

```python
MessageLike = (
    str
    | MessageSegment
    | Sequence[MessageSegment]
    | MessageChain
)
```

字符串转换为单个 `Plain`，消息段序列转换为 `MessageChain`。

第一阶段公共消息段包括：

- `Plain`
- `At`
- `AtAll`
- `Reply`
- `Image`
- `Record`
- `Video`
- `File`
- `Node`
- `Nodes`

新版 SDK 沿用现有 AstrBot 消息段名称，不将它们重命名为 `Text`、`Mention`、`Quote` 或 `Audio`。

未知入站消息段转换为只读 `UnknownSegment`，保留段类型和可安全序列化的公开字段。第一阶段不能把 `UnknownSegment` 用于出站消息。

```python
from astrbot_sdk.message_components import Image, Plain
from astrbot_sdk.messages import MessageChain


message = MessageChain(
    Plain("Report"),
    Image.from_file("report.png"),
)
yield event.reply(message)
```

媒体消息段使用 `AssetRef`、公开 URL、Runner 本地文件或 bytes 构造：

```python
Image.from_url(url)
Image.from_file(path)
Image.from_bytes(data, media_type="image/png")
```

本地文件和 bytes 在 Result 或主动消息序列化时由 SDK 自动上传，RPC 协议只传 `AssetRef`。需要复用、持久化引用或主动下载入站媒体时，插件显式使用 `ctx.assets.upload()`、`ctx.assets.download()`。

这里的“原始消息段”指 AstrBot 适配器解析后的消息链，不是平台 SDK 的 raw event、message 或 client。

## Handler Result 与 Yield

Result 是不可变 DTO，传播行为使用显式枚举：

```python
class Propagation(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"


class EventResult:
    propagation: Propagation


class MessageResult(EventResult):
    message: MessageChain
    quote: bool
```

直接返回或 `yield` 一个 `MessageLike` 时，SDK 构造 `Propagation.CONTINUE` 的 `MessageResult`。

`on.command` 和 `on.message` handler 支持普通异步方法和异步生成器：

```python
async def handler(...) -> MessageLike | EventResult | None:
    ...


async def handler(...) -> AsyncIterator[MessageLike | EventResult | None]:
    ...
    yield result
```

规则：

- `return MessageLike`：构造当前事件的默认回复，提交最终结果，handler 不再恢复。
- `return EventResult`：提交最终结果，handler 不再恢复。
- `return None`：不提交结果，Host 继续 Pipeline。
- `yield MessageLike`：构造默认回复，提交结果并暂停 handler。
- `yield EventResult`：提交结果并暂停 handler。
- `yield None`：不提交结果，只把控制权交给后续 Pipeline。
- Host 完成对应的下游 Pipeline 后确认本次 `yield`，Runner 才恢复 handler。
- 一个 handler 可以多次 `yield`。
- `Propagation.STOP` 会停止当前事件，handler 不再恢复。

默认回复继续传播。需要停止传播时显式传入 `Propagation.STOP`。

需要引用原消息或停止传播时，使用显式 Result：

```python
yield event.reply(
    "Handled.",
    quote=True,
    propagation=Propagation.STOP,
)
```

只停止当前事件：

```python
yield event.stop()
```

`event.stop()` 构造不包含消息内容、传播行为为 `Propagation.STOP` 的 `EventResult`。

`yield` 是 Pipeline 洋葱模型的控制点。`return` 适合无需在下游执行后恢复的简单 handler，二者不是两套运行时。

hooks、生命周期和 Tool handler 不使用该 yield 协议。

## PluginContext

`self.ctx` 始终具有稳定的类型形状。服务不会因为 capability 未授予而从对象上消失：

```python
if self.ctx.capabilities.has("llm.generate"):
    response = await self.ctx.llm.generate(
        "Summarize this conversation.",
        umo=event.umo,
    )
```

- 缺少 capability 的 RPC 抛出 `CapabilityDenied`。
- handler 注册所需 capability 未授予时，Host 不注册该 handler。
- required capability 未授予时，插件不能启动。
- optional capability 未授予时，插件继续启动，对应 handler 和服务调用不可用。
- capability 校验以 Host 为准，`ctx.capabilities` 不能代替 Host 鉴权。

## 错误与取消

SDK 公共错误：

| 异常 | 语义 |
| --- | --- |
| `CapabilityDenied` | capability 或 scope 未授权 |
| `InvalidRequest` | 参数或 DTO 不合法 |
| `NotFound` | 引用的 Host 资源不存在 |
| `Conflict` | 状态冲突或重复注册 |
| `RateLimited` | 达到 Host 配额 |
| `DeadlineExceeded` | Host 调用超时 |
| `HostUnavailable` | transport 断开或 Host 不可用 |

Host 错误通过 stdio、WebSocket 和进程内适配器映射为相同的 SDK 异常。

取消使用 Python `asyncio.CancelledError`，SDK 不包装该异常。插件捕获后必须重新抛出。SDK 不自动重试发送消息、写数据等有副作用的操作。

插件定义错误在加载阶段返回稳定错误码，例如 `MIXED_PLUGIN_API`、`DUPLICATE_HANDLER_ID`、`DUPLICATE_LIFECYCLE_HANDLER` 和 `INVALID_HANDLER_SIGNATURE`。

## 从旧版迁移

新版不是旧版对象的同进程包装。迁移映射如下：

| 旧版 | 新版 |
| --- | --- |
| `Star` | `Plugin` |
| `self.context` | `self.ctx` |
| `AstrMessageEvent` | `MessageEvent` |
| `filter.command` | `on.command` |
| 空函数式 `filter.command_group` | `on.command("group subcommand")` 完整路径 |
| 堆叠 `event_message_type`、`permission_type`、`platform_adapter_type` | `on.command/on.message` 的过滤参数 |
| `filter.regex` | `on.message(regex=...)` |
| `filter.llm_tool` | `on.tool` |
| Pipeline hook 装饰器 | `hooks.*` |
| `initialize()`、`terminate()` | `lifecycle.startup`、`lifecycle.shutdown` |
| `event.message_str` | `event.text` |
| `event.get_sender_name()` | `event.sender.name` |
| `event.plain_result(text)` | `event.reply(text)` 或直接返回、`yield` 文本 |
| `event.chain_result(chain)` | `event.reply(chain)` 或直接返回、`yield` `MessageChain` |
| `event.image_result(path)` | `event.reply(Image.from_file(path))` |
| `event.unified_msg_origin`、`event.session` | `event.umo` |
| `event.send(...)` | `ctx.messages.send(event.umo, ...)` |
| Host manager、Provider、Platform 对象 | 对应的类型化 `ctx.*` 服务 |

这张表描述源码迁移，不表示两套接口可以在同一个插件中混用。旧插件不迁移时继续由旧版兼容层运行。

## 第一阶段不提供

- session waiter 和 `event.wait_next()`；
- 自定义 Python 消息 filter；
- raw platform event 和原生 action；
- Host Manager、Provider、Platform 或插件实例；
- 运行时新增 command、message handler 或 hook；
- 修改 `agent_start`/`agent_end`、`message_sent` 阶段的 Pipeline 数据；
- 从 Tool、hook 或生命周期方法 `yield` 消息结果。
