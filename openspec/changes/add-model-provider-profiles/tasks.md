## 1. Specification

- [x] 1.1 写明 profile 存储、密钥保护、激活唯一与回退语义
- [x] 1.2 写明模型端点网络边界、私网显式许可、缓存隔离与真实测试边界
- [x] 1.3 通过 OpenSpec strict 校验

## 2. Storage and secrets

- [x] 2.1 实现 `research_model_profiles` 表（沿用建表前备份）与 profile 校验（协议/URL/模型/超时/私网开关/启用）
- [x] 2.2 实现钥匙串密钥存储与 0600 文件后备、删除同步清理；任何读取路径不返回明文
- [x] 2.3 实现研究设置 `model_profile_id` 激活指针与运行中禁止修改约束

## 3. Provider integration

- [x] 3.1 实现有界模型 HTTP 客户端：单次 POST、无重定向、DNS 钉扎、默认仅公网、显式私网许可、限量与超时
- [x] 3.2 实现 openai/anthropic 兼容协议解析与低基数错误映射；密钥不进日志/数据库/错误
- [x] 3.3 扩展 `model_identity`/`model_capabilities`；分析缓存按 profile 隔离；预算/取消/校验不变
- [x] 3.4 实现一次性真实连接测试命令（极小提示词、不产证据）

## 4. Surfaces

- [x] 4.1 Guardian IPC：`model_save/model_delete/model_activate/model_test`；快照含 profile 列表与生效提供者
- [x] 4.2 桌面"模型"管理页：列表/编辑/删除/激活/测试（费用确认），密钥只显示已配置状态
- [x] 4.3 CLI：model-list/model-save/model-activate/model-delete/model-test
- [x] 4.4 同步 README/RESEARCH/DESKTOP 文档

## 5. Verification

- [x] 5.1 离线测试：校验、密钥后备、HTTP 边界（私网默认拒绝/许可后允许）、协议解析、缓存隔离、IPC/UI/CLI
- [x] 5.2 全量测试、ruff、mypy、OpenSpec strict 通过
- [x] 5.3 产品真实验证：本机 OpenAI 兼容端点经真实 profile 激活完成一次真实分析入库与缓存复用、重复分析零调用；删除 profile 后回退环境变量提供者
- [ ] 5.4 取得用户 key 后对真实第三方供应商做连接与分析验收；未完成前不宣称该供应商可用

## 验收记录（2026-10-02 22:30）

- 离线：新增 13 项模型 profile 测试（端点校验/密钥双后端/HTTP 边界与错误映射/缓存隔离/IPC/生命周期），全量 1,280 tests passed；ruff、mypy（77 文件）、OpenSpec strict 通过。
- 产品真实链路（`build/research-preview/model-profiles-e2e.json`）：真实钥匙串存取密钥（keyring 后端）；本机 OpenAI 兼容端点 profile 激活后，一次真实扫描经该端点完成 1 次模型调用（Bearer 头正确），三周期报告引用逐字匹配证据；第二次扫描 0 次调用、0 次端点请求（缓存复用）；删除 profile 后钥匙串密钥同步清除，回退环境变量提供者。连接测试经真实 HTTP 成功。
- 边界保持：确定性校验未放宽；本机端点的"模型"为验证桩，真实第三方供应商未验收（5.4 待用户提供 key），不宣称已接通。
