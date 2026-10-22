# 安装与运行

这份步骤面向当前改进版。历史 Windows/WSL/Docker 启动器仍保留在 `bridge/`，其中有上游机器路径，不能直接用于任意主机。语音模式不要求 Docker；真实数字人是独立的可选部署。

## 已测环境与依赖

| 部件 | 复现环境 |
|---|---|
| 系统与硬件 | Linux，RTX 5080 16 GB，Ryzen 7 9800X3D |
| Python / Node | Python 3.12 / Node 24 |
| PyTorch / torchaudio | 2.11.0+cu130 / 2.11.0+cu130 |
| speech-to-speech | 0.2.10 |
| faster-qwen3-tts | 0.5.4 |
| transformers / FunASR | 5.20.1 / 1.4.16 |
| DSH | 0.1.3-alpha.2 |

这是已运行主机的版本快照，不是完整依赖锁，也不是在干净主机上重跑过的一键安装器。安装时选择适合自己 GPU 的 PyTorch CUDA 轮子；Blackwell 的编译与 CUDA 版本不能照搬旧显卡方案。

从包含本次修改的主仓库和插件仓库开始。两者相邻放置可运行源码同步检查。上游 `beiyege-01` 插件没有这里尚未发布的补丁；以后发布 fork 时再使用其真实地址，本文不虚构下载地址。

## 1 准备 bridge

在主仓库根目录：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
# 复现主机使用的 CUDA 组合；其他 GPU 请按 PyTorch 支持选择
python -m pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu130
python -m pip install -r bridge/requirements.txt
cp bridge/bridge-config.example.json bridge/bridge-config.json
```

首次加载仍可能下载 TTS 及 tokenizer 资源；离线运行需要预先完成缓存。不要把本机 `bridge-config.json` 或参考音频提交到 Git。

## 2 准备模型和参考音频

FunASR 与 Silero：

```bash
python -c "from modelscope import snapshot_download; snapshot_download('iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch', local_dir='models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch')"
mkdir -p bridge/models/silero-vad
curl -fL https://raw.githubusercontent.com/snakers4/silero-vad/v4.0/files/silero_vad.jit \
  -o bridge/models/silero-vad/silero_vad_v4.jit
mkdir -p voices/my-voice
```

将你有权使用的参考音频放到 `voices/my-voice/ref_audio.wav`，把准确转写放到同目录 `ref_text.txt`。不加 `omnivoice.txt` 标记即走 Qwen3 克隆路径。示例配置同时填了 `tts.ref_audio`、`tts.ref_text`，请填成自己的内容；默认 voice 目录名需与 `persona.default_voice` 一致。

默认 TTS 模型 ID 为 `Qwen/Qwen3-TTS-12Hz-0.6B-Base`，handler 负责首次加载。需要自定义下载缓存时设置 `HF_HOME`，后续所有进程保持相同路径。

先保持 `turn.enabled=false`，通过 STT/TTS 验证基础链路。要尝试文本断轮：

```bash
hf download Qwen/Qwen3-0.6B --local-dir bridge/models/qwen3-0.6b
```

然后在本地配置中开启 `turn.enabled`、选择 `mode=text`。`turn.text_model` 相对 bridge 目录解析，默认 CPU bf16；实验阈值 0.2。音频 Smart Turn 模式需另装 onnxruntime 并准备 ONNX 权重，中文实验不支持把它作为默认推荐。[判定实验](EXPERIMENTS.md)

## 3 启动与单独验证语音

```bash
python -m uvicorn voice_bridge:app --app-dir bridge --host 127.0.0.1 --port 8765
```

另一个终端运行：

```bash
curl http://127.0.0.1:8765/api/health
python bridge/smoke_stt.py --file voices/my-voice/ref_audio.wav --format wav
python bridge/smoke_tts.py --text '你好，我们开始吧。' --out tts_out.wav
```

初次 `health` 的 stt/tts 可能为 false，表示尚未加载；error 字段用于区分失败。默认 Qwen3 音色会后台预热。模型完整加载后再做热运行测量。

路径规则：`voices_dir` 相对主仓库根目录；turn 模型路径相对 `bridge/`；ASR 和媒体相对进程 cwd，因此从主仓库根目录启动。需要外部配置可设置 `VOICE_BRIDGE_CONFIG=/absolute/path/config.json`，模型路径仍按上述规则处理。

## 4 DSH 与本地 LLM

安装已测 DSH，并准备本地 ollama 模型：

```bash
npm install -g @deepseek-ai/dsh@0.1.3-alpha.2
ollama pull qwen3.5:9b
export DSH_HOME="$HOME/.dsh-voice"
export OLLAMA_API_KEY=ollama
```

9B 的视觉投影器、量化与上下文长度会改变显存。实验使用了去掉视觉部分的本地别名；`ollama pull` 得到的原模型不保证同样占用。先用 `ollama ps` 检查是否完整驻留 GPU，不要把本机改过的模型别名当作可下载模型。

后续插件注册和网页启动保持同一个 `DSH_HOME`。`OLLAMA_API_KEY=ollama` 是本地兼容客户端的占位值，不是远程服务凭据。

在 DSH 的 `$DSH_HOME/profiles/web/cordis.patch.yml` 中合并 OpenAI-compatible ollama provider，`baseURL=http://127.0.0.1:11434/v1`，模型 ID 填实际本地名称。[示例 patch](examples/cordis.ollama.patch.yml) 给出已测字段结构，合并时保留插件注册产生的其他配置。关闭思考使用 wire 值 `reasoning_effort=none`；OpenAI 兼容路由里的 `think:false` 不等价。

语音人设放在 DSH 自定义 agent preset 中，并在网页选择该预设。preset 内的人设可能覆盖全局 personaPrefix；长工具提示也会增加首轮等待。主仓库不提供可直接安装的 DSH 人设包。

## 5 安装本次修改的插件

在独立插件仓库：

```bash
npm ci
npm run check
```

先让 DSH profile 识别独立插件包。若使用上游 `dsh plugin --profile web add github:beiyege-01/dsh-voice-ai-girlfriend-plugin` 建立 profile，它只安装上游版本；随后必须将这里构建的 `lib/client.js` 部署到该 profile 实际包目录。检查 profile 的包依赖，不能假设它在当前项目的 node_modules 中。

Linux 的部署形式（把路径替换成自己的实际位置）：

```bash
PLUGIN_BUILD=/path/to/dsh-voice-ai-girlfriend-plugin/lib/client.js
PROFILE_PACKAGE=/path/to/dsh-home/profiles/web/node_modules/@beiyege-01/dsh-voice-ai-girlfriend
cp "$PROFILE_PACKAGE/lib/client.js" /path/to/backup/client.js
cp --remove-destination "$PLUGIN_BUILD" "$PROFILE_PACKAGE/lib/client.js"
```

`--remove-destination` 防止写入 pnpm 共享硬链接；这是 GNU cp 参数。macOS 使用先将目标移到备份、再复制的方式。首次注册插件后重启 DSH；仅更新客户端 bundle 则硬刷新页面。重新安装 profile 依赖可能覆盖本地补丁。

## 6 浏览器与常见问题

插件注册和 profile 配置完成后，在保留上述环境变量的终端启动网页：

```bash
dsh web --host 127.0.0.1 --port 3080 --no-open
```

如果通过 SSH 进入主机启动 DSH，`DSH_HOME` 和 `OLLAMA_API_KEY` 也设置在主机端。复制终端打印的带 token 地址到浏览器，不把 token 保存进项目文件。

在 DSH 页面开启麦克风，数字人保持关闭。远程主机通过 README 的 SSH 转发访问 localhost；不把麦克风页面直接开在普通 HTTP 的远程 IP 上。

| 问题 | 检查 |
|---|---|
| 看见文字却没声音 | voice 总开关、默认音色、bridge 日志；确认未切到已停用的 OmniVoice 后端 |
| 重复朗读 | 是否同时打开多个 DSH 标签页 |
| 首轮特别慢 | LLM 是否已加载、提示长度、TTS 预热状态；分别测冷/热 |
| 断句抢话 | 先用耳机测试；文本断轮是实验功能，可关闭恢复固定等待 |
| `/api/balance` 503 | 本地 ollama 场景未配置 DeepSeek key 时的可选余额功能，不代表 TTS 失败 |
| 数字人没画面 | 本项目的真实 DUIX 路径未验证；语音模式不需要 Docker |

运行仅绑定 loopback，DSH token 不公开。不要重用上游脚本内的机器路径或凭据。
