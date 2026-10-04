# 插件元数据 v2

状态：草案。

新版插件继续使用仓库根目录的 `metadata.yaml`，不增加 `manifest.yaml`。v2 在现有插件元数据上增加运行时和 capability 声明。

## 完整示例

```yaml
schema_version: 2

name: astrbot_plugin_greeting
display_name: Greeting
desc: 回复问候并可选使用 LLM
author: Soulter
version: 1.0.0
repo: https://github.com/Soulter/astrbot_plugin_greeting
astrbot_version: ">=5.0,<6"
support_platforms:
  - aiocqhttp
  - telegram

runtime:
  api: sdk
  entrypoint: main:GreetingPlugin
  sdk_version: ">=0.1,<0.2"

capabilities:
  required:
    - id: message.receive
  optional:
    - id: llm.generate
```

## 版本识别

| 条件 | 加载方式 |
| --- | --- |
| 没有 `schema_version` | 旧版插件 |
| `schema_version: 2` 且 `runtime.api: sdk` | 新版 SDK 插件 |
| 只有其中一项，或值不受支持 | 拒绝加载 |

插件类型只由 `metadata.yaml` 决定。Loader 不扫描 Python 导入、基类或装饰器来猜测类型，也不会在新版加载失败后回退到旧版。

`schema_version` 只表示 `metadata.yaml` 的结构版本，不是插件版本、SDK 版本或 RPC 协议版本。

## 现有字段

v2 保留当前字段及语义：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `name` | 是 | 插件包名 |
| `display_name` | 否 | 展示名称 |
| `desc` | 是 | 插件说明 |
| `short_desc` | 否 | 短说明 |
| `author` | 是 | 作者 |
| `version` | 是 | 插件版本 |
| `repo` | 否 | 仓库地址 |
| `astrbot_version` | 否 | 兼容的 AstrBot PEP 440 版本范围 |
| `support_platforms` | 否 | 支持的平台适配器 ID |
| `i18n` | 否 | 国际化元数据 |
| `pages` | 否 | 插件 Pages 元数据 |

插件 ID 继续由 AstrBot 根据 `author` 和 `name` 生成，不新增第二套身份字段。

## `runtime`

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `api` | 是 | 第一阶段固定为 `sdk` |
| `entrypoint` | 是 | `<module>:<Plugin 子类>`，模块路径相对插件根目录 |
| `sdk_version` | 是 | 插件兼容的 `astrbot-sdk` PEP 440 版本范围 |

例如 `main:GreetingPlugin` 表示导入 `main` 模块中的 `GreetingPlugin`。

运行模式和 transport 不写入插件元数据：

- 新版插件默认隔离运行，用户可以改为非隔离运行。
- stdio 或 WebSocket 由 Host 按部署方式选择。
- 同一插件代码不能依赖具体 transport。

## `capabilities`

```yaml
capabilities:
  required:
    - id: message.send
  optional:
    - id: web.route
      scope:
        paths:
          - /callback
```

每一项使用统一的对象格式：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `id` | 是 | [capabilities-v1.md](./capabilities-v1.md) 定义的 capability ID |
| `scope` | 否 | 该 capability 定义的限制条件 |

规则：

- `required` 被拒绝时，插件不能启动。
- `optional` 被拒绝时，插件仍可启动，对应 handler 不注册，相关调用返回 `CapabilityDenied`。
- 同一个 capability ID 不能重复，也不能同时出现在两组中。
- 未声明的 capability 一律不授予。
- capability 和 scope 由 Host 在注册及每次调用时校验。

没有额外 Host 能力需求时，两组可以省略：

```yaml
capabilities: {}
```

## 不放入元数据的内容

- Python 依赖继续由 `requirements.txt` 或项目打包配置声明。
- 用户实际授予的 capability 保存在 Host 配置中，不能由插件仓库写入。
- RPC 协议版本在 Runner 与 Host 握手时协商。
- stdio、WebSocket、进程路径和认证信息属于部署配置。
