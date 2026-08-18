# Alpha Guard 官网设计 Spec（三方向 subagent 共同输入）

## 产品事实（来自仓库 README，已核实）
- **产品名**：Alpha Guard — 可信沉默（Trusted Silence）
- **定位**：面向港股与美股个人投资者的**自托管、只读、证据优先**决策守门员。不是荐股软件、不是自动交易机器人。
- **核心卖点**：
  1. 证据优先：配置先验证、缺失返回 UNKNOWN 而非猜测、结论可解释、冲突返回 CONFLICT
  2. 可信沉默（Silence Plane）：沉默也需要证据——扫描按时完成、范围覆盖、字段新鲜、送达链路可用，沉默才可信
  3. 安全默认值：克隆后默认离线、只读、不联网不通知，能力逐层显式启用
  4. 有记忆：SQLite 保存信号状态、证据指纹、新闻指纹，重启去重
  5. 三形态：桌面 App（只读值班台）+ 后台 Guardian（独占调度）+ Telegram/WhatsApp 移动提醒
  6. 市场感知：美股纽约时区、港股香港时区、交易所日历过滤休市
- **命令**：`validate` / `doctor` / `dry-run`，`uv sync --frozen` 安装，可选 extras: notifications/desktop/ai/cn-data
- **仓库**：github.com/sunhetong918/alpha-guard，Python 3.11/3.12 + uv

## 受众与场景
- 个人投资者（港股/美股）、注重数据自主权的开发者型用户
- 场景：GitHub 落地页 / 产品介绍官网，桌面优先但需响应式

## 核心信息（页面板块）
1. 导航（logo + GitHub 链接）
2. Hero：产品一句话 + 快速开始命令
3. 「为什么证据优先」特性区（UNKNOWN / CONFLICT / 指纹去重 / 新鲜度 / 时区日历等）
4. 「可信沉默」理念区（产品最大差异化，必须单独呈现）
5. 三形态架构（桌面 / Guardian / 移动通知）
6. 快速开始 / 安装命令
7. Footer（免责声明：不构成投资建议、不连接券商）

## 情感基调
克制、可信、工程感、诚实（「沉默也有证据」的冷静气质）。避免浮夸营销腔。

## 输出格式
- 单文件 HTML/CSS，纯静态，无构建。桌面 1440px 为主，需响应式
- 存 `alpha-guard/website/design-demos/[逻辑名].html`
- Logo 资产：`/Users/cvte-data/alpha-guard/desktop/assets/alpha-guard-icon-master.png`（用 base64 内嵌，见下）
- 色彩可参考图标主色提取，但允许各方向自行诠释

## 视觉母题（form 种子）
「沉默/守夜/证据」：雷达扫描、值班台、信号灯（绿=可信沉默）、账本、指纹。各方向从中自选一个母题贯穿。

## 用户参考图（三方向共同语境，不豁免差异）
深色 SaaS 落地页：顶导航 + 居中大标题 + 功能卡片网格。三版都要**尊重深色工程感的语境**，但布局骨架必须互异，各自在深色语境内做差异化诠释。
