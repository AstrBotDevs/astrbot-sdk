# 消息组件契约 v1

状态：草案。

本文定义新版 SDK 消息组件（`MessageSegment`）的公开契约、线上格式，以及
媒体资产的传输协议。目标是在保留插件作者熟悉概念（Plain、At、Reply、
Image……）的前提下，消除旧版组件模型的结构性问题。

## 旧模型的问题

- 媒体来源字段重叠：`file` / `url` / `path` / `_type` 四个字段并存，
  `file` 一个字段可以是 URL、本地路径、`file://` URI 或 `base64://` 伪协议。
- 组件类型携带平台行为（下载、转 base64、文件解析），类型无法只做数据。
- 每种组件的编解码、平台映射都是手写分支，加一个类型要动多处。
- 协议边界不明确：本地路径、bytes、未知段能否跨进程没有统一规则。

## 不变量

1. 组件是不可变 frozen dataclass，只有数据，没有行为。
2. 协议帧中的媒体来源只有两种形状：`AssetRef` 或 http(s) URL。
3. 本地路径、bytes、`base64://` 都不是协议概念，永远不会出现在帧里。
4. 未知入站组件降级为 `UnknownSegment`；`UnknownSegment` 不允许出站。
5. 组件类型名是协议契约，发布后不改名；新类型只能追加。
6. 解码遇到未注册的组件类型时必须降级为 `UnknownSegment`，不得报错
   （前向兼容）。

## 一等组件

第一阶段保留以下类型，名称与旧版一致：

| 组件 | 字段 |
| --- | --- |
| `Plain` | `text: str` |
| `At` | `user_id: str`, `name: str \| None` |
| `AtAll` | — |
| `Reply` | `id: str`, `sender_id`, `sender_name`, `text`（均可选） |
| `Forward` | `id: str` |
| `Face` | `id: int` |
| `Image` | `source`, `media_type: str \| None` |
| `Record` | `source`, `media_type: str \| None` |
| `Video` | `source`, `media_type: str \| None` |
| `File` | `source`, `filename: str \| None`, `media_type: str \| None` |
| `Node` | `sender_id: str`, `content: tuple[segment, ...]`, `sender_name` |
| `Nodes` | `nodes: tuple[Node, ...]` |

`source` 的两种合法形状：

```python
source: AssetRef | str  # str 必须是 http(s) URL，构造时校验
```

`Poke`、`Json`、`Share`、`Music` 等类型有真实 core 实现，但第一阶段不晋升
为一等组件，按市场需求评估后逐个追加；它们与 core 标记为 TODO 的类型
（RPS、Dice、Shake、Contact、Location）一样，入站统一降级为
`UnknownSegment`。

## 媒体构造与自动上传

本地文件和 bytes 只是构造便利，不是协议形状：

```python
Image.from_url("https://example.com/a.png")        # 直接携带 URL
Image.from_bytes(data, media_type="image/png")     # 发送前自动上传
Image.from_file("report.png")                      # 发送前自动上传
Image(source=AssetRef(id="ast_..."))               # 复用已有资产
```

SDK 在以下时机把本地来源替换为 `AssetRef`：

- Result 序列化（`yield event.reply(...)`、handler return）；
- `ctx.messages.send` 等主动发送。

替换通过 `ctx.assets.upload()` 完成，发生在 Runner 内、编码之前。因此
结果编码路径必须是 async：Loader 在编码 outbound chain 前调用
`prepare_outbound(chain)`。

入站媒体相反：Host 把平台媒体登记为资产，组件携带 `AssetRef` 到达
Runner；插件需要本地文件时显式调用 `ctx.assets.download(ref)`。

## 资产传输协议

capability `assets.transfer`，默认授予、插件命名空间隔离、带大小配额。

### 上传（Runner → Host）

```text
assets.upload_begin   {filename, media_type, size}      -> {upload_id}
assets.upload_chunk   {upload_id, seq, data_base64} × N  -> {}
assets.upload_commit  {upload_id}                        -> AssetRef
assets.upload_abort   {upload_id}                        -> {}
```

- 单片 ≤ 256 KiB（base64 前）。帧大小恒定有界。
- 每片一个 request/response，天然背压，stdio 与 WebSocket 语义一致。
- 总大小 ≤ 256 KiB 时 SDK 自动改用单次 `assets.upload`（内联 base64），
  插件无感知。
- 未完成（未 commit）的上传由 Host 定期清理。

### 下载（Host → Runner，拉模式）

```text
assets.stat  {asset_id}                  -> {size, media_type, filename}
assets.read  {asset_id, offset, length}  -> {data_base64}
```

`download()` 在 Runner 侧按片拉取、流式写入临时文件，返回 `Path`。
拉模式不需要在协议里引入推式流。

### 生命周期

- 资产归 Host 持有，按插件命名空间隔离；引用其他插件的资产返回
  `CAPABILITY_DENIED`。
- 每插件配额（总字节数 + 单文件上限）由 Host 配置，超限返回
  `RATE_LIMITED`。
- v1 不做自动 GC；持久化语义（消息引用计数、TTL）后续阶段定义。

## 线上格式

媒体组件：

```json
{
  "type": "Image",
  "source": {"kind": "asset", "id": "ast_...", "media_type": "image/png"}
}
```

```json
{
  "type": "Image",
  "source": {"kind": "url", "url": "https://example.com/a.png"}
}
```

`UnknownSegment`：

```json
{
  "type": "Unknown",
  "segment_type": "Face",
  "data": {"id": 14}
}
```

## 编解码结构

segment 编解码使用声明式注册表，不再使用 if/elif 分支：

- 每种组件一个 spec：类型名、类、编码、解码、方向（入站/出站）。
- 媒体四类型（Image、Record、Video、File）共享一个参数化 spec。
- 新增组件 = 一次注册；未注册类型按不变量 6 降级。
- core 组件 ↔ SDK 组件的 Host 侧映射复用同一份 spec 查表。

## 第一阶段不提供

- 出站 `UnknownSegment`；
- 媒体下载的推式流式协议；
- Video `cover`、音乐卡片、分享卡片等附加字段（随类型追加时评估）；
- 资产的跨插件共享与公开 URL 签发。
