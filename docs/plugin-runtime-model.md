# AstrBot Plugin SDK 运行模型

AstrBot 的 SDK Bridge 使用独立 Runner 执行声明了 `schema_version: 2` 和
`runtime.api: sdk` 的插件。插件继承 `astrbot_sdk.Plugin`，仅依赖 SDK
公开接口。使用 `astrbot.api` 的插件由 AstrBot 自带的进程内加载器运行。

```text
AstrBot Core ←→ SDK Bridge ←→ stdio / WebSocket ←→ Plugin Runner
```

Runner 读取并校验 `metadata.yaml`，注册 SDK handler，通过握手向 Host 报告
插件身份和 handler 元数据。Host 把 handler 接入消息管线。元数据缺失或无效时，
Runner 报错。参见 [metadata-v2.md](./metadata-v2.md)。

SDK 用类型化接口发送 capability 请求。Host 将授权绑定到 Runner 连接，
并在执行 RPC 前检查请求所需的 capability。能力范围仅涵盖 Host 提供的服务，
进程对本机文件系统和网络的访问由部署环境控制。参见
[capabilities-v1.md](./capabilities-v1.md)。

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

新版 SDK 保留 `yield` 作为 Pipeline handler 的主要写法。
