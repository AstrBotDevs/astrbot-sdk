# Runner 协议 v1

状态：已实现最小版本。

本文定义 Host 与新版插件 Runner 之间的第一版协议。Host 和 Runner
是同一条双向连接上的两个 Peer。stdio 和 WebSocket 必须复用相同的
frame 与调用语义。

## Frame

每个 frame 是一个 JSON object，必须包含：

```json
{"type":"request","id":"request-id"}
```

- `type`：frame 类型。
- `id`：请求或 invocation ID，非空字符串。
- 单个 frame 最大 8 MiB。
- stdio 使用 UTF-8 JSON Lines，一个 frame 占一行。
- stdout 只允许写协议 frame；插件日志和诊断信息写 stderr。
- WebSocket 实现应将一个 JSON object 放在一个 text message 中，不增加另一套 envelope。

协议包含六类 frame：

| `type` | 方向 | 用途 |
| --- | --- | --- |
| `request` | 双向 | 调用对端公开的方法 |
| `response` | 双向 | 请求正常完成 |
| `yield` | Runner -> Host | handler 暂停并提交一次结果 |
| `ack` | Host -> Runner | 下游 Pipeline 完成，允许 handler 恢复 |
| `cancel` | 双向 | 取消对端正在执行的请求或 invocation |
| `error` | 双向 | 请求失败，返回稳定错误码和消息 |

## Peer

每个 Peer 同时维护：

- 本端发出且等待 `response` 或 `error` 的请求；
- 对端发来且正在执行的请求；
- Host 发起的 handler invocation；
- invocation 的 `yield`、`ack` 和取消状态。

收到 `request` 后必须在独立任务中执行 handler，不能阻塞连接的读取循环。
同一个 handler invocation 尚未结束时，Runner 可以调用 Host；Host 处理该
请求时也可以继续接收其他 frame。

请求 ID 在连接内必须唯一。当前实现使用 `host:` 和 `runner:` 前缀区分
发起方，handler invocation 使用 `host-invoke:` 前缀。`response`、`error`
和 `cancel` 必须复用原请求 ID。

`yield` 和 `ack` 不是普通 RPC 的流式响应。它们只用于 Host 发起的
handler invocation。

## 初始化

Runner 启动后不立即导入插件。Host 首先发送：

```json
{
  "type": "request",
  "id": "init-1",
  "method": "initialize",
  "params": {
    "protocol_versions": [1],
    "config": {},
    "capabilities": [
      {"id": "message.receive", "scope": {}}
    ]
  }
}
```

Runner 选择共同支持的协议版本，加载 `metadata.yaml` 和 entrypoint，注入实际 capability，执行 `lifecycle.startup`，然后返回：

```json
{
  "type": "response",
  "id": "init-1",
  "result": {
    "protocol_version": 1,
    "plugin": {
      "id": "astrbot/astrbot_plugin_example",
      "name": "astrbot_plugin_example",
      "version": "1.0.0",
      "schema_version": 2
    },
    "handlers": [],
    "capabilities": ["message.receive"]
  }
}
```

`schema_version` 是 `metadata.yaml` 版本；`protocol_version` 是 Host 与 Runner 的通信版本，两者独立演进。

## Runner 调用 Host

Runner 中的类型化 `ctx.*` 服务通过普通双向 RPC 调用 Host。SDK 内部使用：

```json
{
  "type": "request",
  "id": "runner:rpc-1",
  "method": "capability.invoke",
  "params": {
    "capability": "llm.generate",
    "operation": "generate",
    "input": {
      "prompt": "hello"
    }
  }
}
```

Host 正常完成后返回：

```json
{
  "type": "response",
  "id": "runner:rpc-1",
  "result": {
    "text": "hello"
  }
}
```

规则：

- `capability.invoke` 是 SDK 内部方法，不作为插件公开 API。
- 插件调用类型化的 `ctx.llm`、`ctx.messages` 等服务。
- `capability` 是授权单位；`operation` 只选择该能力下的具体操作。
- Frame 不携带 `plugin_id`。Host 根据连接确定插件身份。
- Host 使用 `metadata.yaml` 声明、用户授权和当前 scope 的交集鉴权。
- Runner 的本地 capability 检查只用于尽早报错，Host 每次调用仍需鉴权。

handler 中调用 Host 不会破坏 `yield`：

```text
Host                         Runner
 | request invoke(call-1)      |
 | --------------------------> |
 |      request capability(r1) |
 | <-------------------------- |
 | response capability(r1)     |
 | --------------------------> |
 |      yield(call-1, seq=1)   |
 | <-------------------------- |
 | ack(call-1, seq=1)          |
 | --------------------------> |
```

## Invocation 与 `yield`

Host 使用一个 ID 发起调用：

```json
{
  "type": "request",
  "id": "call-1",
  "method": "invoke",
  "params": {
    "handler_id": "hello",
    "args": [],
    "kwargs": {}
  }
}
```

handler 每次 `yield` 时，Runner 递增 invocation 内的 `sequence`：

```json
{
  "type": "yield",
  "id": "call-1",
  "sequence": 1,
  "result": {
    "type": "message",
    "propagation": "continue",
    "message": [{"type": "Plain", "text": "hello"}],
    "quote": false
  }
}
```

Runner 此时暂停生成器。Host 完成对应的下游 Pipeline 后发送：

```json
{"type":"ack","id":"call-1","sequence":1}
```

只有收到序号相同的 `ack`，Runner 才请求生成器的下一项。handler 完成后返回：

```json
{"type":"response","id":"call-1","result":null}
```

规则：

- `yield None` 仍产生 `yield` frame，表示只向后继续 Pipeline。
- `Propagation.STOP` 会关闭生成器；Host 不发送 `ack`。
- sequence 从 `1` 开始，必须严格递增。
- Host 放弃消费 invocation 时发送 `cancel`。
- 连接断开时 Runner 取消所有未完成 invocation。

## DTO

协议只传 JSON DTO，不传 pickle、Python module、Host 对象或平台对象。

- `MessageEvent`、`MessageChain` 使用带 `$type` 的 SDK value envelope。
- 消息段使用现有名称，如 `Plain`、`At`、`Reply`、`Image`、`Record`。
- 未知入站消息段使用 `UnknownSegment`，不能用于出站结果。
- 媒体跨进程只接受公开 URL 或 `AssetRef`。
- Runner 本地 `Path` 和 `bytes` 必须先通过资产服务上传；资产服务实现前直接拒绝序列化。

## 错误

```json
{
  "type": "error",
  "id": "call-1",
  "code": "INVALID_REQUEST",
  "message": "handler_id must be a non-empty string"
}
```

- `code` 是稳定的程序判断字段。
- `message` 用于日志和诊断，不作为程序分支条件。
- Host 将 Runner 错误映射为 `RemotePluginError`。
- Runner 将 Host 错误映射为 `RemoteHostError`，类型化服务再映射为对应的
  SDK 公共异常。
- Python `asyncio.CancelledError` 在 Runner 内保持取消语义，对 Host 表示为 `CANCELLED`。
