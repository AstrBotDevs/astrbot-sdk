# AstrBot 新 SDK 测试环境搭建指南（内部测试版）

> 面向团队同步测试与交接。当前为开发分支状态，**不要用于生产环境**。
> 建议在独立的测试目录/测试机上进行，或先备份 `data/` 目录。

## 1. 架构一句话

Core 通过 **SDK Bridge** 把插件放到独立的 **Runner 子进程**里运行，插件与 Core
之间通过 JSON-RPC（stdio 或 WebSocket）通信。每个 SDK 插件一个进程，崩溃不连坐；
旧版（legacy）插件默认仍在进程内运行，但可以一键切换到隔离模式。

```
┌─────────────┐   stdio / WebSocket   ┌──────────────────┐
│  AstrBot    │ ◄──── JSON-RPC ────►  │  Plugin Runner    │
│  Core       │   (capabilities)      │  (每插件一进程)    │
└─────────────┘                       └──────────────────┘
```

## 2. 涉及仓库与分支

| 仓库 | 分支 | 说明 |
|---|---|---|
| [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) | `codex/sdk-plugin-bridge` | Core 侧：bridge、dashboard 运行环境切换 |
| [AstrBotDevs/astrbot-sdk](https://github.com/AstrBotDevs/astrbot-sdk) | `main` | 新 SDK 包（`astrbot_sdk`），含 legacy 兼容层 |

## 3. 环境要求

- Python **3.12+**（推荐用 [uv](https://docs.astral.sh/uv/) 管理）
- uv（`curl -LsSf https://astral.sh/uv/install.sh | sh`，或 `pip install uv`）
- 可选：Node.js 18+ 与 pnpm（需要看 WebUI 的"运行环境"开关时）

> 插件虚拟环境的依赖安装由 Core venv 里的 `uv` Python 包完成（`uv sync` 会自动带上），
> 失败时回退 stdlib pip。不强制要求系统 PATH 上有 uv 二进制。

## 4. 搭建步骤

```bash
# 1) Core
git clone https://github.com/AstrBotDevs/AstrBot.git
cd AstrBot
git checkout codex/sdk-plugin-bridge

# 2) SDK 包（放到 packages/astrbot-sdk-new，该目录被 gitignore）
mkdir -p packages
git clone https://github.com/AstrBotDevs/astrbot-sdk.git packages/astrbot-sdk-new
# 默认 main 分支即可

# 3) 安装依赖 + 以 editable 方式安装 SDK
uv sync
uv pip install -e packages/astrbot-sdk-new

# 4) 启动
uv run main.py
```

启动日志中出现 `sdk_bridge` 相关字样（如 `Loaded SDK plugin ... (isolated)`）即说明
bridge 已生效。若看到"检测到新版 SDK 插件，但当前环境未安装 astrbot-sdk"，说明第 3 步
的 editable 安装没成功。

### 可选：WebUI（查看/切换运行环境）

```bash
cd dashboard
pnpm install   # 首次
pnpm dev       # http://localhost:3000
```

插件卡片上新增**运行环境**标识与切换入口（隔离 / 进程内）。

## 5. 验证：跑通一个 SDK 插件

```bash
# 把示例插件复制到插件目录
cp -r packages/astrbot-sdk-new/examples/hello data/plugins/astrbot_sdk_hello
```

重启 Core（或在 WebUI 重载插件），WebChat 里发送：

```
/hello
```

应收到 `Hello, <你的名字>!`。插件卡片上应显示**隔离**运行环境。

SDK 插件的判定依据是 `metadata.yaml`：

```yaml
schema_version: 2
runtime:
  api: sdk
  entrypoint: main:HelloPlugin
  sdk_version: ">=0.1,<0.2"
```

## 6. 测试重点：三种运行模式

### 6.1 新 SDK 插件（始终隔离）

`schema_version: 2` + `runtime.api: sdk`，自动走隔离 Runner，无需任何开关。

### 6.2 旧版插件 → 隔离模式（重点测试对象）

现有插件**不用改代码**即可隔离运行（legacy 兼容层）。两种方式开启：

- **WebUI**：插件卡片 → 运行环境 → 切换为"隔离"（持久化，重启生效）
- **metadata.yaml**：加一行声明（优先级低于 WebUI 的手动覆盖）

```yaml
runtime:
  isolated: true
```

切换后插件会在独立进程里加载。请重点回归你常用插件的核心功能：
指令、LLM 调用、tool、消息发送、配置读写、插件页面（views）。

### 6.3 旧版插件 → 进程内（默认）

什么都不做就是原来的行为。用于对照实验：同一个插件在两种模式下的行为应一致。

> 注意：从"隔离"切回"进程内"时，该插件的独立虚拟环境（如有）会被删除。

## 7. 插件依赖隔离（per-plugin venv）

隔离模式下的插件可以声明自己的依赖，与 Core 环境隔离：

- 插件目录放 `requirements.txt` 或 `pyproject.toml`
- 首次加载时 Core 自动在插件目录下建 `.venv` 并安装依赖（指纹不变则跳过）
- 需要代理/镜像源时，设置 `UV_INDEX_URL` / `PIP_INDEX_URL` 环境变量后重启 Core

## 8. 外部 Runner（可选：插件跑在另一台机器）

插件进程也可以不在本机，通过 WebSocket 拨入 Core。Core 侧配置
`data/config/sdk_bridge_external.json`：

```json
{
  "listen": "127.0.0.1:6195",
  "plugins": [
    { "name": "my_plugin", "plugin_id": "xxx", "token": "...", "capabilities": [] }
  ]
}
```

适合验证"Core 与插件不同机部署"的场景。一般用不到，可跳过。

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| `initialize plugin metadata is invalid` | SDK 包版本太旧，`cd packages/astrbot-sdk-new && git pull` 后重启 |
| 插件列表里看不到刚切的插件 | 看 Core 日志 `Failed to load SDK plugin ...`，把 traceback 贴出来 |
| 依赖装不上 / 建 venv 失败 | 检查网络与 `UV_INDEX_URL`；删除插件目录下 `.venv` 后重载 |
| 隔离模式某功能报错、进程内正常 | 就是兼容层缺口，**请直接反馈**（见下） |
| 禁用插件重启后又自动启用 | 早期 bug 已修；如复现请反馈 |

日志关键词：`sdk_bridge`、`Plugin Runner`。

## 10. 反馈方式

反馈时请带上：

1. 插件名 + 来源（链接）
2. 运行模式（隔离 / 进程内）
3. Core 日志中相关 traceback（`Failed to load ...` 或 `Star sdk_bridge... handle error`）
4. 复现步骤（发了什么指令/点了什么）

## 11. 测试清单（建议）

- [ ] hello 示例插件：`/hello` 正常回复
- [ ] 1~2 个常用旧插件切隔离模式：指令、LLM 对话、tool 调用正常
- [ ] 插件禁用 → 重启 → 仍是禁用
- [ ] 插件卡片显示 icon / 描述 / tool 列表 / 配置页
- [ ] 有 views 的插件（如 meme_manager）：页面能打开、资源加载正常
- [ ] 插件带 `requirements.txt`：自动建 venv，依赖生效
- [ ] 隔离插件崩溃（如手动制造异常）：Core 与其他插件不受影响
