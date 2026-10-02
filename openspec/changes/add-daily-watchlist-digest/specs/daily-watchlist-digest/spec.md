# daily-watchlist-digest Specification（delta）

## ADDED Requirements

### Requirement: Explicit daily schedule
系统 SHALL 提供默认关闭的每日摘要：用户显式开启并设定本地时刻后，Guardian 在该时刻执行一轮有界采集与汇总；这是与连续监控不同的独立已授权计划，显式开启后跨 Guardian 重启保持生效，关闭即删除计划。
#### Scenario: Default off
- **WHEN** 用户未开启每日摘要
- **THEN** 不注册任何摘要任务，读取界面零外部请求
#### Scenario: Enabled across restart
- **WHEN** 摘要已开启且 Guardian 重启
- **THEN** 按设置时刻继续执行下一次摘要；不追补 Guardian 离线期间错过的历史摘要，缺口在摘要记录中如实标注
#### Scenario: Disable
- **WHEN** 用户关闭摘要
- **THEN** 取消计划任务；正在执行的批次有界收尾，不再发起新请求或投递

### Requirement: Watchlist matching
摘要 SHALL 基于用户关注清单匹配已采集材料：证券沿用研究设置的 instruments（代码与名称），主题为用户自定义关键词组；匹配与排序为确定性规则（相关度、新鲜度、跨来源佐证数），不使用后验或未保存数据。
#### Scenario: Deterministic ranking
- **WHEN** 同一批材料重复生成摘要
- **THEN** 相同输入得到相同排序与分组
#### Scenario: Honest coverage
- **WHEN** 某证券或主题在本轮来源中无材料
- **THEN** 该分组显示为空并保留，不虚构条目，不宣称全网热度

### Requirement: Bounded digest generation
每轮摘要 SHALL 有界执行：来源为已配置新闻源与已启用作者订阅（不启动音视频录音或转写）、条数与模型调用受限、复用证据去重与既有采集状态语义；摘要记录持久化后才算完成。
#### Scenario: No audio transcription
- **WHEN** 摘要批次遇到视频订阅作品
- **THEN** 只读已保存文本与元信息，不触发录音、转写或新增媒体预算
#### Scenario: Busy research run
- **WHEN** 摘要时刻研究扫描正在运行
- **THEN** 摘要不并发抢跑，延后有界重试并在记录中说明

### Requirement: Optional model summary with gates
系统 SHALL 提供可选 AI 摘要：每轮最多一次模型调用，仅限已配置且可用的提供者；生成的叙述必须锚定本轮已保存证据（逐条列出对应证据条目），出现数字无对应来源支撑或引用不匹配时整段拒收并回退确定性摘要。
#### Scenario: AI disabled or unavailable
- **WHEN** 未开启 AI 摘要或提供者不可用
- **THEN** 仍生成确定性分组摘要，状态如实说明未使用模型
#### Scenario: Invalid model output
- **WHEN** 模型输出含无来源数字或引用缺失
- **THEN** 拒收该叙述并保留确定性摘要，本轮不追加模型调用

### Requirement: Delivery and history
摘要 SHALL 默认仅在应用内保存与查看；用户显式开启投递后经已配置通知通道发送有界文本；摘要历史可离线分页读取。
#### Scenario: In-app only
- **WHEN** 未开启投递
- **THEN** 摘要保存于本地库，界面与 CLI 可离线查看，不外发任何内容
#### Scenario: Delivery failure
- **WHEN** 投递通道拒绝或失败
- **THEN** 摘要记录标注投递失败原因码，不重发超过一次
