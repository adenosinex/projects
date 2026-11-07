# Qwen3-ASR 本地音视频转文字

面向 Apple Silicon Mac，以完整输出文本信息为主。Flask + SQLite + 单 GPU Worker，支持上传音视频、任务排队、刷新后找回、取消、批量清理历史、失败重试、复制和下载。默认监听 `0.0.0.0:5840`，无账号密码，仅用于可信局域网。

## 两种识别模式

| 模式 | 默认 MLX 模型 | 用途 |
| --- | --- | --- |
| 日常（默认） | `mlx-community/Qwen3-ASR-0.6B-8bit` | 日常转录，兼顾速度、内存与效果 |
| 高准确率 | `mlx-community/Qwen3-ASR-1.7B-8bit` | 重要或较难音频，使用更大的模型 |

上传前在网页选择模式。选择和具体模型路径写入 SQLite，排队、刷新及重启恢复后保持一致。没有选择记录的旧排队任务使用当前默认 Qwen3-ASR 模式；历史已完成文本保持原样。

识别仅使用 Qwen3-ASR。没有其他识别器回退、专名词典、跨块文字提示或自动复听。两种模式都使用 8-bit MLX 权重；1.7B 是更大的备选，不代表所有音频上的字词都一定正确。

## 部署

在 Apple Silicon Mac 上：

```bash
brew install python@3.12 ffmpeg
cd ~/Downloads/mlx-flask-transcriber
chmod +x start.sh
./start.sh
```

本机访问 `http://127.0.0.1:5840`，局域网访问 `http://Mac-mini-IP:5840`。首次使用每个模型会从 Hugging Face 下载，之后复用本地缓存。推理在本机进行，不上传音频到云端。

依赖固定为 `mlx-audio[stt]==0.5.8`，调用 `mlx_audio.stt.load` 与模型的 `generate` 接口。可复用原 `.venv` 安装新依赖；发生依赖冲突时另建 Python 3.12 虚拟环境。升级前停止旧进程、备份 `data/`，覆盖源码时保留 `data/` 和 `.venv/`。SQLite 表结构兼容，无需手动迁移。

## 文本与性能

- **完整保存模型文本**：每块使用返回的完整 `text`，不以时间戳子片段重建或截断文字，不按字典替换专名，不摘要改写，也不删除口头重复。
- **一次识别一块**：默认在约 45 秒附近优先寻找静音分界，块长最短为目标的 0.6 倍（27 秒，不会切得过短）、最长为目标的 1.33 倍（60 秒，不会因过长出错）。静音分两档：先找低于 -32dB 的真静音，找不到再找低于 -20dB 的停顿（呼吸、换人、音乐间隙），在嘈杂 / 有音乐的音频里也能找到切点；两档都没有才在 45 秒处硬切。末尾不足 10 秒的短尾会并入上一块，不单独成块。各块连续覆盖全部采样，没有重叠、音频丢弃或文本去重。没有静音时必须按长度切分，衔接处仍可能识别不准，可调大块长减少切点。
- **控制内存与磁盘开销**：单 Worker 顺序推理，只驻留一个模型；切换模式先释放旧模型。同模式后续任务复用模型。临时块文件一次只生成一个。
- **保留已有标点**：模型负责识别文字与标点。阅读稿只在已有句末标点、空白和块边界调整换行，保留相同的非空白字符。排版检查失败时退回完整原稿。无标点的中文长句不会被凭空加标点，段落上限是软上限。
- **不假装提供精细字幕**：默认没有加载 ForcedAligner。SRT 和时间轴中的时间是实际输入音频块的范围，通常一块约 45 秒；不是逐句、逐字或逐词时间，适合定位与回听。
- **生成上限可见**：每块默认最多生成 2048 tokens。达到上限或未输出文字时标记需核对，保留全部现有输出；不隐藏为“准确完成”，也不自动改写。可缩短块长或提高上限后重新提交。

任务可以取消，当前模型加载或音频块推理完成后停止；刚完成的文字也会保存。失败或取消时保留源文件及部分文本，成功后清理上传的源文件和临时 WAV。已完成或取消的任务需要换模型重转时，重新上传原文件；失败任务可直接加载重试。

## 历史清理与失败重试

- 勾选已完成、失败或已取消的记录后，点击“删除所选”批量删除；表头复选框全选当前显示的可删除记录。
- “清空历史”会在确认后删除全部已结束的记录及其原始文件、输出文件，包括列表最近 200 条之外的历史。排队、处理和取消中的任务会保留。
- 点击“加载最近失败”，会查找全部历史中的最近一条失败记录，将保留的原始文件和识别模式加载到提交区域；也可通过行内“加载重试”或详情中的“加载此失败记录”指定记录。
- 可调整识别模式，再点击“重试转录任务”。无需重新上传，后台复制原文件、创建新任务并从头识别，原失败记录及部分文本保留。原文件已丢失时会提示重新上传。点击“取消加载”或选择新文件可退出重试模式。

对应接口：`DELETE /api/jobs` 接收 `{"ids":["任务ID"]}` 或 `{"all":true}`；`GET /api/jobs/latest-failed` 加载最近失败详情；`POST /api/jobs/<id>/retry` 接收可选的 `model_profile`，返回新的排队任务 ID。

## 参数

```bash
ASR_PROFILE=daily ./start.sh             # 默认日常模式
ASR_PROFILE=accurate ./start.sh          # 默认高准确率模式，网页仍可选择
ASR_LANGUAGE=auto ./start.sh             # 默认由模型识别语言
ASR_LANGUAGE=Chinese ./start.sh          # 明确指定语言，使用 Qwen 的语言名称
ASR_CHUNK_SECONDS=45 ASR_MAX_TOKENS=2048 ./start.sh
PARAGRAPH_CHARS=220 ./start.sh
HOST=0.0.0.0 PORT=5840 ./start.sh
```

`ASR_CHUNK_SECONDS` 范围 10–120 秒；`ASR_MAX_TOKENS` 范围 256–8192。块长增加时应相应留足 token 上限，内存和取消等待时间也可能增加。

已下载到指定目录的同系列模型可通过以下变量配置，提交时保存对应路径：

```bash
QWEN_DAILY_MODEL=/path/to/Qwen3-ASR-0.6B-8bit ./start.sh
QWEN_ACCURATE_MODEL=/path/to/Qwen3-ASR-1.7B-8bit ./start.sh
```

`DATA_DIR` 可指定数据目录，默认是项目内 `data/`。旧识别器相关环境变量不再生效。

## 输出

- `result.txt`：阅读稿，主输出。
- `raw.txt`：按输入块顺序保存的识别原稿，各块用换行分隔。
- `asr_chunks.json`：每块完整文字、真实音频起止时间、语言、token 数和生成上限标记。即使任务中途取消或失败也可下载已有记录。
- `result.srt`、`captions.txt`：粗略音频块时间轴。
- `stats.json`：所用模型、加载/预处理/推理/排版耗时、识别倍率、字数和需核对片段。识别倍率不含排队、模型下载加载和排版时间，不代表识别准确率。

文本框编辑只在当前页面临时保存，下载仍是后台生成的文件。后台进程重启会从保留的原音频重新识别，不是断点续传。必须单进程启动，不使用 Flask reloader 或多 WSGI workers。

## 验证

```bash
python -m unittest discover -s tests -v
python -m py_compile app.py asr.py worker.py formatter.py jobs.py
node --check static/app.js
node --test tests/history_ui.test.js
```

测试覆盖批量清理的状态保护、超出列表范围的历史、最近失败加载、源文件缺失、重试文件复制与模式切换、清空和重试的并发处理，以及前端选择、轮询和普通上传流程。无需下载模型权重。真实模型的速度、内存与字错率需要在 Apple Silicon Mac 上使用原音频验证。

HTTP 测试需安装 Flask；缺少时会明确跳过。前端测试使用 Node.js 内置测试工具。限制创建子进程的环境可使用 `node --test --test-isolation=none tests/history_ui.test.js`。

接口依据：[MLX Audio v0.5.8 Qwen3-ASR 实现](https://github.com/Blaizzy/mlx-audio/blob/v0.5.8/mlx_audio/stt/models/qwen3_asr/qwen3_asr.py)、[0.6B 8-bit 模型](https://huggingface.co/mlx-community/Qwen3-ASR-0.6B-8bit)、[1.7B 8-bit 模型](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-8bit)。
