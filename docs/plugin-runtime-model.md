# AstrBot 插件运行模型

本文定义 AstrBot SDK、插件版本、运行模式和 Host 能力之间的关系。

## 两个独立维度

插件版本和运行模式是两个独立维度。

插件分为：

- 旧版插件：使用现有的 `astrbot.api.*`、`Star`、`filter`、`Context`、`AstrMessageEvent` 等接口。
- 新版插件：使用 `astrbot_sdk`，并通过 `metadata.yaml` 声明需要的 Host 能力。

运行模式分为：

- 隔离运行：插件在独立环境中运行，通过 stdio 或 WebSocket 与 AstrBot Host 通信。
- 非隔离运行：插件与 AstrBot Host 运行在同一 Python 进程中。

四种组合都需要有明确行为：

| 插件类型 | 隔离运行 | 非隔离运行 |
| --- | --- | --- |
| 旧版插件 | 由 SDK 旧版兼容层运行 | 由 AstrBot 现有插件加载器运行 |
| 新版插件 | 默认模式；通过 SDK 和 RPC 调用 Host | 通过 SDK 的进程内适配器调用 Host |

新插件默认隔离运行。用户可以修改运行模式。

## 组件边界

```text
AstrBot Host
├── Plugin Manager
├── Capability Manager
├── Pipeline
└── RPC Peer
        │
        │ stdio / WebSocket
        │
Plugin Runner
├── astrbot-sdk
├── Plugin Loader
├── RPC Peer
└── Plugin
```

- **Host** 持有 AstrBot 的真实状态和能力，包括消息、LLM、存储、平台接口和 Pipeline。
- **Runner** 管理插件进程、加载插件、消费插件的异步生成器，并转发 RPC。
- **SDK** 定义插件可见的类型和接口，不向插件暴露 Host 内部对象。
- **Plugin** 只依赖 SDK 或旧版公开接口。

## SDK 的职责

`astrbot-sdk` 是一个独立发行包，负责四部分：

```text
astrbot_sdk/
├── api/            # 新版插件公开接口
├── compat/v1/      # 旧版公开接口的兼容实现
├── protocol/       # RPC 消息、序列化类型和错误
└── runtime/        # Runner、transport 和调用状态
```

目录名称只表达职责，最终 Python 包结构可以调整。

隔离环境只安装插件依赖和 `astrbot-sdk`，不安装完整的 AstrBot Host。SDK 不能导入 `astrbot.core.*`，也不能持有 Host 中对象的引用。

## 旧版插件兼容

旧版插件隔离运行时不修改源码，原有导入继续有效：

```python
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
```

Runner 在导入旧版插件前安装兼容导入映射：

```text
astrbot.api.event  -> astrbot_sdk.compat.v1.event
astrbot.api.star   -> astrbot_sdk.compat.v1.star
```

兼容层实现旧版公开接口，并把调用转换为 SDK RPC。它不复制 AstrBot Host 的内部实现。

`astrbot-sdk` 不应直接提供顶层 `astrbot` 包。这样会与非隔离运行时的真实 `astrbot` 包冲突。旧导入路径由 Runner 的 import hook 在隔离进程中提供。

兼容范围只包括公开插件 API。直接导入 `astrbot.core.*`、数据库连接、平台实例或其他内部对象的插件不保证能够隔离运行。

旧版插件非隔离运行时继续使用 AstrBot 当前的 `astrbot.api` 和加载器，不经过 SDK 兼容层。

## 新版插件与能力

新插件在 `metadata.yaml` 中声明所需的 Host 能力。用户安装或启用插件时决定实际授予哪些能力。

能力由 Host 校验，不由 SDK 自行校验。Host 在建立 Runner 连接时绑定插件
身份；后续 RPC Frame 不携带 Runner 自行填写的插件 ID。Host 根据连接身份
和当前授权决定是否执行。未授权请求返回稳定、可识别的权限错误。

SDK 负责：

- 暴露与能力对应的接口。
- 通过已绑定身份的连接发送调用参数和必要的调用上下文。
- 把 Host 权限错误转换为 SDK 异常。

Host 负责：

- 保存用户授权结果。
- 把插件身份绑定到 Runner 连接。
- 在每次调用时校验能力。
- 记录能力调用和拒绝结果。
- 在插件停用或权限变更后立即更新授权。

能力控制的是 Host 提供的能力。它不自动限制插件对文件系统、网络和操作系统 API 的访问；这些限制需要进程沙箱或容器提供。

新插件选择非隔离运行时，与 Host 共享 Python 进程。此时能力授权仍可约束正常的 SDK 调用，但不能作为安全边界，因为插件代码可以访问进程内模块和对象。非隔离模式等同于信任该插件。

## 插件版本识别

插件版本必须由仓库根目录现有的 `metadata.yaml` 明确标识，不能通过扫描导入语句或类名推断。新版插件不增加独立的 `manifest.yaml`。

- 未声明 `schema_version` 时，按现有旧版插件加载。
- `schema_version: 2` 且 `runtime.api: sdk` 时，使用新版 SDK 加载流程。
- 出现部分新版字段但格式不完整时，元数据无效，不能静默回退到旧版加载。
- metadata schema 与 RPC 协议版本分别演进，不能共用一个版本号。

完整字段见 [metadata-v2.md](./metadata-v2.md)。

## API Family 与混用

插件类型决定一次加载使用的 API family：

```text
legacy metadata -> 旧版加载器 + compat/v1 注册表
SDK metadata    -> 新版加载器 + astrbot_sdk 注册表
```

同一个 AstrBot 实例可以同时运行多个旧版和新版插件；同一个插件实例不能同时使用两套 API。

- 新版插件不能导入 `astrbot.api.*`、继承 `Star`、接收旧版 `Context` 或注册旧版 filter。
- 旧版插件不能在同一次加载中注册新版 `on.*`、`hooks.*` 或 `lifecycle.*` handler。
- 一个仓库可以在迁移期间保留两个入口，但 `metadata.yaml` 一次只能选择一个。
- 新版沿用 `yield`、`MessageChain`、`Plain`、`At`、`Reply`、`Record` 等名称，不表示启用了旧版兼容层。

Runner 在 legacy 模式下只启用旧版 import hook，在 SDK 模式下不启用该 hook。非隔离运行时虽然完整的 `astrbot` 包可见，加载器仍必须隔离两套注册归属。

如果当前插件同时产生旧版和新版注册项，Host 必须拒绝整个插件，返回稳定错误码 `MIXED_PLUGIN_API`，不能只丢弃其中一部分。直接导入 `astrbot.core.*` 的新版插件属于不受支持的实现；非隔离模式也不能把这种用法视为 SDK 兼容承诺。

## Transport 与协议

stdio 和 WebSocket 是同一套 RPC 协议的两种 transport。插件 API、消息结构和调用语义不能因 transport 改变。

已实现的第一版 frame 和调用流程见 [protocol-v1.md](./protocol-v1.md)。

```text
Plugin API <-> RPC Protocol <-> stdio | WebSocket <-> Host
```

协议至少需要支持：

- 握手和协议版本协商；
- 连接绑定的插件身份与授权上下文；
- 请求、响应和稳定错误码；
- 事件推送；
- 流式数据；
- 调用取消和超时；
- `yield` 的暂停与恢复。

stdio 主要用于 Host 启动的本地插件进程。WebSocket 可用于独立启动或远程连接的 Runner。认证方式可以不同，RPC 语义必须一致。

## `yield` 与 Pipeline

`yield` 是插件参与 AstrBot Pipeline 洋葱模型的控制点，不只是发送多条消息的语法。

非隔离运行时，插件每次 `yield` 后，控制权进入后续 Pipeline；后续阶段执行完成后，再从插件生成器的 `yield` 之后继续。

隔离运行必须保留相同语义：

```text
Host 启动一次插件调用
  -> Runner 启动插件异步生成器
  -> 插件 yield
  -> Runner 把 yield 值发送给 Host
  -> Host 更新 event/result 并执行后续 Pipeline
  -> Host 向 Runner 确认本次 yield 已完成
  -> Runner 恢复插件生成器
```

每次插件调用都需要独立的调用 ID。每次 `yield` 需要递增序号，Host 必须完成对应的下游 Pipeline 后才能确认。Runner 收到确认前不能继续执行生成器。

基本语义：

- `yield result`：提交结果，并把控制权交给后续 Pipeline；消息事件通常使用 `yield event.reply(...)`。
- `yield` 或 `yield None`：不提交结果，只把控制权交给后续 Pipeline。
- 插件停止、Host 取消、超时和异常必须跨 transport 传播。
- 连接断开后，未完成的生成器必须被取消，不能静默继续运行。

新版 SDK 保留 `yield` 作为 Pipeline handler 的主要写法。旧版的 `yield event.plain_result(...)` 必须由兼容层保持相同行为。

## 加载流程

旧版插件隔离运行：

```text
读取未声明 schema 的 metadata.yaml
-> 创建隔离环境并安装 astrbot-sdk
-> 启动 Runner
-> 启用旧版 import hook
-> 导入插件
-> 通过兼容层注册 handler
-> 开始处理 Host 事件
```

新版插件隔离运行：

```text
读取 metadata.yaml v2 和能力声明
-> 获取用户授权
-> 创建隔离环境并安装匹配版本的 astrbot-sdk
-> 启动 Runner 并协商协议版本
-> 导入插件
-> 注册 handler
-> 开始处理 Host 事件
```

非隔离运行不需要 transport。SDK 使用进程内适配器调用 Host，但应保持与隔离模式一致的公开接口和结果语义。
