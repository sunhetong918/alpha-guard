# 本机音视频转写

Alpha Guard 的本地转写器使用官方 OpenAI Whisper，音视频与模型推理都留在本机。用户可以显式导入音视频并启动转写；也可以开启视频研究，在一次用户发起的扫描中取得完整音轨后转写。启动应用或保存配置不会启动转写，也不会开启新闻监控。

## 安装与配置

模型与 Python 推理依赖放在应用外的独立目录。当前 macOS Apple Silicon 安装目录为：

```text
~/Library/Application Support/alpha-guard/transcription/
  venv/                 独立 Python 3.12 环境
  local_whisper.py       research/local_whisper.py 的已安装副本
  models/large-v3-turbo.pt  当前默认官方多语言模型，不启用词表提示
  models/base.pt         保留的 base 回退模型
  models/small.pt        保留的质量对照模型，未作为默认启用
  models/tiny.pt         保留的 tiny 模型（比较与低资源使用）
  verification/         合成录音、验收 JSON 与独立 SQLite
```

可重复的安装方式（下载依赖和模型需要联网，转写不需要）：

```bash
runtime_dir="$HOME/Library/Application Support/alpha-guard/transcription"
uv venv --python 3.12 "$runtime_dir/venv"
uv pip install --python "$runtime_dir/venv/bin/python" 'openai-whisper==20250625' 'torch==2.14.0'
mkdir -p "$runtime_dir/models"
curl -fL -C - -o "$runtime_dir/models/large-v3-turbo.pt" 'https://openaipublic.azureedge.net/main/whisper/models/aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a/large-v3-turbo.pt'
shasum -a 256 "$runtime_dir/models/large-v3-turbo.pt"
cp research/local_whisper.py "$runtime_dir/local_whisper.py"
```

安装前应已有 FFmpeg。large-v3-turbo 必须为 1,617,941,637 字节，SHA-256 为 `aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a`。保留的 base 校验值为 `ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e`，模型为 145,262,807 字节。保留的 tiny 模型为 75,572,083 字节，SHA-256 为 `65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9`。模型地址与校验值来自 [Whisper 官方源码](https://github.com/openai/whisper/blob/main/whisper/__init__.py)，引擎用法来自 [Whisper 官方项目](https://github.com/openai/whisper)。不要用来源不明的 PyTorch 模型文件替代它。

`ALPHA_GUARD_TRANSCRIPTION_COMMAND` 是可信本机配置中的 JSON 参数数组，不能来自新闻正文或博主内容。此机器的值为：

```json
["/Users/cvte-data/Library/Application Support/alpha-guard/transcription/venv/bin/python","/Users/cvte-data/Library/Application Support/alpha-guard/transcription/local_whisper.py","{input}","--model-file","/Users/cvte-data/Library/Application Support/alpha-guard/transcription/models/large-v3-turbo.pt","--ffmpeg","/opt/homebrew/bin/ffmpeg","--output-dir","{output_dir}","--threads","4"]
```

路径应按实际安装位置调整，尤其是 Intel Mac 或其他用户目录。使用绝对 FFmpeg 路径，避免桌面启动器的 PATH 与终端不同。服务把 `{input}` 和 `{output_dir}` 替换为本次文件及临时目录，通过参数数组启动子进程，不经过 shell。模型文件缺失时直接失败，不在转写期间联网补下模型。最长音频为 3 小时；超过时长直接拒绝，不把截取部分标为完整材料。默认任务超时 15 分钟，可通过停止按钮取消整个转写进程组。

单独运行已安装转写器：

```bash
runtime_dir="$HOME/Library/Application Support/alpha-guard/transcription"
"$runtime_dir/venv/bin/python" "$runtime_dir/local_whisper.py" '/绝对路径/访谈.mp4' \
  --model-file "$runtime_dir/models/large-v3-turbo.pt" --ffmpeg /opt/homebrew/bin/ffmpeg \
  --output-dir '/绝对路径/转写输出' --threads 4
```

输出 `transcript.json` 包含识别文字及每段起止秒数。自动识别语种，也可以显式加 `--language zh` 或 `--language en`。当前不做说话人分离，无法确认的说话人保留为空。入库会保留 `machine_transcription_unverified`、`recording_time_unknown`；缺少真实来源链接时还会标记 `source_url_missing`，不能因为转写成功就生成方向建议。

2026-10-02 起，包装器显式使用 `temperature=0.0`、`condition_on_previous_text=False`，关闭低置信片段的随机采样回退，保留未经人工改写的识别文本。这个设置提高可重复性，不代表能自动纠正公司名、数字或否定词。安装副本更新前已备份。

## 社交视频完整音轨

`research/media_transcription.py` 只接受已经取得的本地文件和来源绑定，不自行下载或登录。录制适配器必须给出从头播放至自然结束的完整记录；转写前再用 FFmpeg 解码计数，核对音轨时长与播放器时长，拒绝截取、未完成及超预算音频。当前界面每轮默认最多 2 条、每条最多 600 秒，均需显式开启视频研究。超过预算的长访谈可以通过完整本地音视频或字幕导入，不会截取一段后声称取得全文。

转写输出必须包含有效时间片段，片段超过实际音轨时长会失败。保留作品、作者、来源链接及原发布时间，另标记录时间未知和机器转写未核验。相同音轨可以在当轮复用缓存；已保存的同源完整转写可以跨运行复用，保留原取得时间。观察到新标题、发布时间或较新的未完整材料时，旧全文不能作为新版本复用。

## 真实本机技术验收

从仓库运行以下命令，用 macOS 的 Samantha 和 Tingting 声音生成中英文合成录音，再调用真实 Whisper 子进程：

```bash
.venv/bin/python -m examples.verify_local_transcription \
  --runtime-dir "$HOME/Library/Application Support/alpha-guard/transcription" \
  --synthesize
```

验收走 `research.ipc → ResearchService → transcribe_media → Whisper → ResearchStore`。它使用独立 SQLite，不读取应用 `.env`，不使用用户媒体，不启动外部新闻来源。验证识别文字与时间片段保存、运行后回到 STOPPED、停止操作使实际子进程退出、取消任务不新增证据，以及重新构造服务后仍为 STOPPED。这个合成语音复验脚本默认使用 base，不改变应用当前的 turbo 配置；安装 tiny 后加 `--model tiny` 可以复测。结果分别保存在 `verification/acceptance-base.json` 和 `verification/acceptance-tiny.json`，最近一次也写入 `verification/acceptance.json`。

2026-09-30 本机实际结果：Python 3.12.13、openai-whisper 20250625、Torch 2.14.0、NumPy 2.5.3，Apple Silicon CPU，4 个推理线程。

| 模型与样本 | 音频时长 | 整条服务流程耗时 | 已保存时间片段 | 结果 |
| --- | ---: | ---: | ---: | --- |
| tiny 英文 Samantha | 17.191 秒 | 16.215 秒（首次加载） | 5 | 文字主体正确，Alpha Guard 合写为 AlphaGuard |
| tiny 中文 Tingting | 15.777 秒 | 2.498 秒 | 3 | 有明显同音字与否定词错误 |
| base 英文 Samantha（相同 WAV） | 17.191 秒 | 3.686 秒 | 5 | 文字与 tiny 相同 |
| base 中文 Tingting（相同 WAV） | 15.777 秒 | 3.233 秒 | 1 | 竞争优势、核对财务数据等词改善，但仍有否定词错误 |
| 取消实际子进程（tiny / base） | — | 0.071 / 0.100 秒 | 0 条新增证据 | cancelled → STOPPED，子进程退出 |

tiny 把“竞争优势”识别为“進徵優勢”，更关键的是“不包含真实的投资建议”识别成“補包含真實的投資建議”。base 修正了部分词语，但最后一句仍识别为“部包含真實的投資建議”。9 月 30 日当时选择总体改善的 base，同时保留机器转写未核验门禁，不能直接成为买入建议。两种模型的证据都保存了真实识别输出和质量标记；没有杜撰来源链接或录音发布时间，`research_ready` 均为 false。重启后的状态为 STOPPED，未启动新闻来源。这里的单次耗时受缓存与首次依赖加载影响，不能当作模型速度对比基准。

本次样本是干净的合成语音，只能验证技术链路。实际访谈的背景声、方言、公司名、财务数字和多人交谈可能明显降低识别质量；tiny 模型尤其需要人工核对。这里不把合成样本结果当成真实访谈准确率、投资建议有效性或自动辨认事实的验收。

## 2026-10-02 真实中文材料质量复验

本机另下载官方多语言 small 模型，483,617,219 字节，SHA-256 为 `9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794`。模型已校验并保留，尚未作为默认模型激活。以下均用原始完整音轨、4 个 CPU 线程、固定温度 0，未人工修正输出。

| 来源与模型 | 解码音轨时长 | 转写及核验耗时 | 时间片段 | 质量结论 |
| --- | ---: | ---: | ---: | --- |
| 抖音中国网财经，何小鹏现场讲话，small | 31.680 秒 | 11.516 秒 | 21 | 能通过技术完整性核验，但专有名词和整句明显误识，不能视为准确逐字稿 |
| 小红书第一财经有看投，市场走势讲话，small | 292.680 秒 | 70.233 秒 | 143 | 主要论述可读，金融术语与金额片段仍有明显错误，不能据此核验数字 |

抖音样本第一帧烧录字幕为“（小鹏智驾）我觉得在大路的效果 跟FSD一致”；small 在 0–2 秒识别为“我觉得大陆的小伙伴们”，2–4 秒识别为“跟第一次在小伙伴”。截图只能为该句提供独立对照，页面编辑标题不作为整段讲话的参考逐字稿。音轨左右声道相关系数 0.887，左右 RMS 分别为 0.1057、0.1037，合成单声道 RMS 为 0.1017；没有发现明显左右反相抵消，原始音轨也没有静音或大范围削波。

小红书 small 原文在 20.74–23.30 秒输出“上准指數已經跌破了不齡線的下軌”，77.64–80.04 秒输出“然後從整個成交金額一點塑胃液”，154.08–157.20 秒输出“林長的很小的是北正武林”。这些原样保留为诊断输出，不用推测出的术语或金额回填原文。目前没有完整人工参考转录，不计算准确率。

两次结果都只进入各自的验收 SQLite；原发布时间、来源链接与时间片段均保存，重复音轨未再次运行模型，持久缓存未刷新原取得时间。验收文件为 `build/research-preview/douyin-speaking-small-validation.json` 和 `build/research-preview/xhs-speaking-small-validation.json`，其中保留原始输出及质量标记。该技术链路成功不等于中文质量合格；模型默认配置未因此切换。

随后完成官方 large-v3-turbo 的同音轨对照，全部在本机 CPU 推理，未继续尝试更大的模型。该机为 16 GB 内存、10 核 CPU，推理仍限制为 4 线程。

| 模型设置与来源 | 耗时 | 片段 / 字符 | 实际结论 |
| --- | ---: | ---: | --- |
| turbo，无词表，抖音同一现场讲话 | 17.613 秒 | 19 / 155 | 0–2 秒仍误识为“我觉得在大陆的消费”，整段质量不合格 |
| turbo，无词表，小红书同一财经讲话 | 74.606 秒 | 150 / 1,714 | 主要论述可读，金额片段由 small 的不成句改善为“1.4万亿”，但金额仍未人工核对；指数名和游资等仍有同音误识 |
| turbo，通用词表诊断，抖音 | 17.172 秒 | 原输出见验收 JSON | 小鹏等词有所改善，整句仍错，未解决现场样本质量问题 |
| turbo，通用词表并逐窗携带，小红书 | 68.880 秒 | 15 / 1,318 | 部分指数名改善，但出现 10 个 U+FFFD 替换字符，片段从 60 秒跳到 120 秒；该设置不启用 |

词表只包含通用财经与汽车名词，没有样本答案、完整原句或财务金额；未将原始输出替换为猜测的正确词语。词表诊断出现的乱码已推动一个明确校验：自动转写的顶层文字、时间片段、当轮缓存和已保存本机转写缓存都拒绝 U+FFFD 破损输出，返回 `invalid_transcription_output`。用户明确导入的正文或字幕原文仍保留。先前诊断文件的原始结果没有改写，相关坏结果只在独立验收库；不会作为可复用研究证据进入用户库。

当前本机默认选择**无词表 large-v3-turbo**，固定温度 0，保留 base/tiny/small 回退和全部机器转写未核验门禁。选择依据是清晰财经材料中部分句子与金额片段有所改善；这不是“中文准确度已合格”的结论。抖音现场样本保持质量不合格，清晰讲话中的证券名、金额、否定词也仍需核对。没有完整人工参考逐字稿，本次不计算准确率。

源码目录的 `.env` 与 `/Applications/alpha-guard-guardian/.env` 的转写命令已在备份后切换，备份目录为 `~/Library/Application Support/alpha-guard/backups/transcription-turbo-20261002T035854Z`。当前安装记录是 `transcription/runtime-installation.json`，各模型记录分别保留，未开启监控。原始对照结果位于 `build/research-preview/{douyin,xhs}-speaking-turbo-validation.json` 和对应的 `*-turbo-glossary-validation.json`。普通转写可按本文命令复现；完整采集验收脚本为 `build/research-preview/verify_captured_asr.py`，只处理显式提供的本地完整音轨及来源回执，使用独立 SQLite。
