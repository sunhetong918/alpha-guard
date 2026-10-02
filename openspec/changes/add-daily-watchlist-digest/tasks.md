## 1. Specification

- [x] 1.1 写明显式开启、跨重启生效、关闭即取消与缺口诚实语义
- [x] 1.2 写明关注清单匹配、确定性排序、有界生成与 AI 门禁
- [x] 1.3 通过 OpenSpec strict 校验

## 2. Storage and pure logic

- [x] 2.1 实现 `research_digest_settings` 与 `research_digests` 表（沿用建表前备份）与设置校验（时刻、主题数、关键词数、开关）
- [x] 2.2 实现关注清单确定性匹配与排序纯函数（证券/主题/新鲜度/佐证，并列稳定）
- [x] 2.3 实现缺口说明（离线跨时刻、空分组、来源失败）与窗口去重

## 3. Scheduling and generation

- [x] 3.1 实现 `DigestScheduler`：睡眠至设定本地时刻、lease 互斥、有界重试、停止语义
- [x] 3.2 实现摘要批次：新闻源+订阅文本采集（无媒体）、证据入库、从库读当前版本、记录持久化
- [x] 3.3 实现可选 AI 摘要：一次调用、数字锚定与引用校验、拒收回退、无模型时确定性摘要
- [x] 3.4 实现可选投递：≤3500 字符、一次发送、结果码入记录、默认关闭

## 4. Surfaces

- [x] 4.1 Guardian 启动恢复计划；`research.command` 增加 `digest_save/digest_once`；快照含 digest 块
- [x] 4.2 桌面"每日摘要"页：开关/时刻/主题/AI/投递/立即生成/历史查看
- [x] 4.3 CLI：digest-status/digest-configure/digest-run/digest-show/digest-history
- [x] 4.4 同步 README/RESEARCH/DESKTOP 文档

## 5. Verification

- [x] 5.1 离线测试：设置校验、匹配排序确定性、调度时刻计算、lease 冲突、AI 门禁拒收、投递、IPC/CLI/UI
- [x] 5.2 全量测试、ruff、mypy、OpenSpec strict 通过
- [x] 5.3 产品真实验证：真实公开 RSS 订阅 + 主题/证券关注，立即生成一轮真实摘要并入库可读；计划时刻语义与重启恢复在产品路径验证
- [ ] 5.4 通知通道配置后做一次真实投递验收；未配置前不宣称推送可用

## 验收记录（2026-10-02 22:30）

- 离线：新增 10 项摘要测试 + 2 项桌面页测试；全量 1,280 tests passed；ruff、mypy（77 文件）、OpenSpec strict 通过。
- 产品真实链路（`build/research-preview/digest-live.json`）：独立库 + 真实美联储 RSS 订阅，手动一轮 2.2 秒采集 10 条真实新闻稿并入库，"美联储"主题命中 10 条、股票分组空且如实标注；DigestScheduler 在真实墙钟下一分钟触发 trigger=schedule 完整批次；stop 后 restart 恢复运行（重启恢复语义）；快照计划显示次日时刻。CLI `research digest-status/digest-history/digest-show` 在同一真实库上离线读取通过。
- 未验收：真实通知通道投递（5.4）——本机未配置 Telegram/WhatsApp，按边界不宣称推送可用；开启 AI 汇总的真实模型输出质量以用户自己的模型实测为准。
