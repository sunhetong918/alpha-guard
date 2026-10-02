# YouTube 真实读取与转写验收

检查日期：2026-10-02。使用应用专用 Chrome 的正常页面及产品采集代码，所有浏览器操作在跨进程锁中串行执行，只创建并关闭本次测试页面。没有导出 Cookie、构造平台签名、重放认证请求或绕过登录和播放器限制。

## 已通过：完整技术链路

真实公开讲话 [Me at the zoo，jNQXAC9IVRw](https://www.youtube.com/watch?v=jNQXAC9IVRw)，作者 `jawed`，通过以下实际调用：

`youtube_transcript → media_handler → capture_audio → transcribe_media → 本机 Whisper base → ResearchStore`

| 项目 | 实际结果 |
| --- | --- |
| 视频身份 | 页面视频 ID、原生播放器视频 ID、作者均匹配；捕获前后没有广告，仍是同一视频。 |
| 完整音频 | 时长 19.021 秒，从 0 秒正常原速播放到原生 `ended`；观测播放时长 19.021 秒，实际经过 19.102 秒。 |
| 本地文件 | 154,471 字节 WebM/Opus，只包含该页面正常播放的音轨；未访问麦克风或录制系统音频。 |
| 实际识别 | 本机 Whisper base 用时 3.655 秒，输出 4 个时间片段、191 字符，片段范围 0–19 秒。 |
| 入库 | `content_kind=transcript`、`completeness=full`；正文哈希与全部时间片段读回一致。 |
| 去重与恢复 | 重复写入保持相同证据 ID，记录数为 1；重新打开 SQLite 后身份及内容哈希一致。 |

该样本的证据内容哈希为 `1da6632a8ae3dd8af41fbd6e85ee6407d4ced9ec84ced7403951069f6cd29622`。保留 `machine_transcription_unverified`、`recording_time_unknown` 和来源时间未知等质量标记；没有把技术成功转换为投资建议。

**这个视频不是财经访谈。** 它证明正常浏览器音频、完整性检查、本地识别与证据保存这条技术链路，不代表 YouTube 财经长片已经完成全文验收。实际财经材料的全文与多周期研究验证应另看 [B站验收](research-bilibili-validation.md)。

## 2026-10-02 16:00 订阅端到端复验：通过

同日第二轮复验中，`connectors.discover → 官方 feed 发现 → youtube_transcript → VideoResearch` 一次通过：

- 官方频道 feed 本轮直接成功，发现 `jNQXAC9IVRw / Me at the zoo / jawed`，发布时间 2005-04-24。
- 字幕不可用后走媒体回退：完整录音 1 次（19 秒原速播放），本地无词表 turbo 转写 4 段、193 字符，立即持久化到独立 SQLite；第一轮全程 54.9 秒。
- 第二轮重新执行完整订阅发现：0.12 秒完成，零录音、零转写、缓存命中 1 次；证据 ID、来源身份、内容哈希与第一轮完全一致。

回执与独立库：`build/research-preview/20261002/youtube-subscription-e2e-r3.json`。早间两轮 `media_unavailable` 未复现，其诊断与失败回执保留在本文件下节；本结果不改变"单轮通过不等于长期稳定"的边界，也不含 CNBC 长片的字幕/录音验收（下节限制依旧）。机器转写未核验标记照常保留。

## 同作者订阅发现：已通过；早间自动全文重建受限记录

新增作者更新回退路径并实际运行 `discover → discover_youtube → youtube_transcript → VideoResearch`。测试订阅为 [jawed 的频道](https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A)，不是用户提供的投资作者清单。

- 官方频道 feed 的一次请求失败后，产品正常打开该作者 `/videos` 页面，核验页面频道 ID、当前作者、选中的 Videos 标签及最新排序，真实发现 `jNQXAC9IVRw / Me at the zoo / jawed`，与上面已完成录音和本地识别的作品身份相同。没有从首页推荐或置顶区补造条目。
- 作者页面提供更新元信息，不能据此标注字幕完整；日期未知仍保留未知。明确无作品的已核验作者返回成功空列表，页面未加载或身份不符返回具体错误。
- 后续实际 `VideoResearch` 首轮遇到 `media_unavailable`：没有执行 ASR，没有写入全文。一次有诊断的复核仍取得相同作品身份，但视频保持 `duration=null`、`readyState=0`、`networkState=2`、`ended=false`，元素连接且可见、没有广告；12 秒的媒体就绪等待未改变这个状态。
- 再做一次不录音的普通页面检查，页面显示 `0:00 / 0:19`，没有登录、同意、验证或播放结束提示，媒体仍未就绪。因此这次不是自然播完后需要 Replay，也没有证据支持放宽完整性检查。

**订阅发现已实际通过，本轮“一次自动采集→完整新转写→重开数据库复用缓存”尚未取得成功证据。** 先前完整音轨、本地识别、数据库读回和重复写入的成功记录仍保留，但不能替代该项端到端验收。本轮在已有明确媒体加载限制后停止重复探测；失败库 `youtube-subscription.db` 保持没有全文记录。

## CNBC 财经材料：仍有真实外部限制

测试对象来自此前实际发现的 CNBC 频道更新，不是随意替换出来的无关材料：

| 视频 | 实际结果 |
| --- | --- |
| [xrg6QCGzQGc](https://www.youtube.com/watch?v=xrg6QCGzQGc)，钻石行业报道 | 页面实际作者为 CNBC，时长 265.961 秒。原生字幕请求曾返回 HTTP 200 但正文为空；正常展开文字稿也未取得完整片段。音频捕获到 47.051 秒时媒体状态变化，完整性门禁拒收，未保存半段音频或证据全文。 |
| [P9BTn-kxMcc，Why Gen Z is driving the box office](https://www.youtube.com/watch?v=P9BTn-kxMcc) | 作者 CNBC，时长 62.801 秒；捕获到 48.929 秒时播放器时长变成非有限值，完整性门禁拒收，未进入语音识别。 |
| [RcLImKRagfg，Why Warner Records is investing in South Asian artists](https://www.youtube.com/watch?v=RcLImKRagfg) | 只读元信息确认作者 CNBC、时长 79 秒、可播放状态；没有重复完整播放或录音。 |

为区分录音实现与网站本身的问题，对 P9BTn-kxMcc 另做了一次**完全不调用 `captureStream` 或 `MediaRecorder`** 的普通播放对照：

- 0–44 秒正常播放，页面始终前台可见。
- 约 48.14 秒，视频 ID 与页面 ID 未变、没有广告、原视频元素仍连接、媒体地址未变，但 `duration` 变成非有限值、播放时间变回 0、播放器暂停，`readyState=0`、`networkState=0`。
- 网站播放器显示播放错误；错误持续到最后一次 64.19 秒检查。DOM 标准媒体错误码为空，不能由此进一步断言是某个具体网络或浏览器原因。

这证明 **该次 P9 失败也发生在没有录音的普通播放中**。本轮没有放宽完整性标准、把前半段当全文、循环重试同一失败长片，也没有声称所有 YouTube 视频都受相同影响。

## 实现边界与回归

- 先尝试正常字幕和文字稿。错误字幕体不会挡住其他轨道或后续本地转写；无法确认完整性的 DOM 摘录保持部分内容。
- 只有原视频身份再次匹配、播放器没有广告时才允许音频转写。广告期间仅使用网站提供的可见普通跳过按钮，或在原页面时限内等待结束；失败则明确拒收。
- 捕获阶段必须从开头原速播放到原生结束。保护媒体、来源变化、媒体错误、跳播、时长超限和未播完都不能标为完整。时长与大小预算由产品配置控制。
- 机器字幕和机器识别仍需核验。没有核实的发布日期、录制日期或说话人不会补成看似精确的事实。
- 原视频身份、广告、坏字幕轨道继续回退、B站分 P 部分内容及完整性拒收均有离线回归；离线测试不能替代上面的真实结果。
- B站多 P 视频只得到第一 P 时保持部分内容，并且在自动音频 handler 执行前拒绝该作品的完整录音回退，避免 handler 的即时持久化留下错误的全文缓存。

## 本机验收材料

材料位于忽略目录 `build/research-preview/20261002/`：

- `youtube-media-jNQXAC9IVRw.json`：成功技术样本的身份、完整播放收据、实际识别与数据库验证。
- `youtube.db`：实际识别正文、时间片段及质量标记；没有写入用户日常研究库。
- `youtube-media-validation.json`、`youtube-media-P9BTn-kxMcc.json`：财经样本的真实未完成收据。
- `youtube-normal-playback-control.json`：不录音的普通播放对照，媒体地址只记录是否变化，不保存地址值。
- `youtube-short-metadata.json`：另一 CNBC 样本的实际标题、作者和时长。
- `youtube-product-subscription-validation.json`：真实作者 Videos 页回退发现成功及后续媒体未就绪的产品调用结果。
- `youtube-product-subscription-diagnostic.json`：同作品身份、完整性拒收及 capture 前后的媒体状态；没有执行识别或持久化全文。
- `youtube-loading-diagnostic.json`：普通页面无录音检查的可见文本和媒体加载状态；仅保存公共页面文字及控件描述，不保存媒体地址。

验收 JSON 不包含带签名媒体地址、认证参数或 Cookie。Chrome 登录资料和用户已有标签页均保留。
