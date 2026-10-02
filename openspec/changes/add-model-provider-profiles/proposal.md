## Why

研究分析目前只支持内置 `anthropic` API 与本机 `claude_cli`/`codex_cli` 客户端。用户已有其他供应商（OpenAI 兼容或 Anthropic 兼容服务，含本机推理）的 API key 时无法接入，只能改环境变量重启。用户于 2026-10-02 要求提供 CC Switch 式的模型管理：填 endpoint、API key、选模型，用自己的 key 即可使用。

## What Changes

- 新增模型 profile：名称、协议（`openai_compatible` / `anthropic_compatible`）、base URL、模型 ID、超时、是否允许本机/私网端点；API key 仅存操作系统钥匙串（后备为 0600 本地文件），数据库与 IPC 只保存"已配置"布尔与更新时间。
- 同一时刻最多一个激活 profile；未激活时沿用环境变量提供者。激活 profile 与环境变量提供者共用现有 AI 开关、调用预算与取消语义。
- 分析缓存键在 profile 模式下包含协议与 base URL 指纹，不同供应商同名模型不共享缓存。
- 模型 HTTP 请求走独立有界客户端：单次 POST、禁重定向、响应限量、超时与取消；默认仅公网 HTTPS/HTTP 端点，显式开启 `allow_private` 后才允许环回/私网（Ollama/LM Studio 等本机推理）。密钥只进入请求头，不进入日志、数据库或错误信息。
- 新增"测试连接"命令：对激活或指定 profile 发起一次极小的真实调用并如实返回结果，界面明确提示该操作产生一次真实请求费用。
- 桌面研究页新增"模型"管理区；CLI 新增同域命令；Guardian IPC 扩展对应命令。弱模型在现有确定性校验（引用逐字匹配、输出上限）下只会如实失败，不会降低报告质量门槛。

## Capabilities

### New Capabilities

- `model-provider-profiles`: 多供应商模型 profile 的存储、激活、网络边界与密钥保护。

### Modified Capabilities

- `research-workspace`: 模型 profile 的桌面/CLI/IPC 管理入口与真实状态展示。

## Impact

影响 `research/model_provider.py`（拆出 profile 解析与 HTTP 客户端）、`research/store.py`（新表 `research_model_profiles`，沿用建表前备份）、`research/ipc.py`、`research/cli.py`、`research/service.py`（快照与激活）、`desktop/ui/research_page.py`、`guardian/` 协议不需新增方法（复用 `research.command`）。不改变既有提供者行为、分析校验与默认关闭语义。真实第三方模型的可用性以用户自己的 key 实测为准；无 key 时不得宣称已接通。
