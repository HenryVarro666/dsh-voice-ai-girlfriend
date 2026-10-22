# 开发与验证

主仓库负责bridge、架构和实验资料；独立插件仓库负责浏览器构建。主仓库 `dsh-plugin/src` 是插件的源码镜像，不要同时在两份源码上独立修改。

## 本地CPU检查

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s bridge/tests -v
python scripts/check_docs.py
python scripts/summarize_turn_eval.py
python -m ruff check --select E4,E7,E9,F,I bridge/voice_bridge.py bridge/audio_codec.py bridge/turn_detection.py bridge/stream_tail.py bridge/tests scripts
python -m ruff format --check bridge/audio_codec.py bridge/turn_detection.py bridge/stream_tail.py bridge/tests scripts
```

单元测试检查PCM/WAV、重采样、音频窗口、聊天模板结束符、无模型加载构造，以及HTTP协议。测试中的ASR/TTS输出是替身，不是GPU性能或音色质量测试。FastAPI 的 on_event 弃用提示仍存在；本地较新 Starlette 还会提示 TestClient 的 httpx 后端弃用。迁移 lifespan 或测试后端需另测后台任务清理。

独立插件仓库运行：

```bash
npm ci
npm run check
```

`check`包含11项回归、语音核心TypeScript检查和构建。`tsconfig.json`只覆盖不依赖DSH注入类型的模块；React slots与完整host类型兼容需在DSH中另外验证，不冒称全插件类型检查通过。

## 同步与部署

两个仓库相邻时，从主仓库运行：

```bash
python scripts/sync_plugin.py --check
python scripts/sync_plugin.py --apply
```

`--source`可指定独立插件的src路径。脚本仅同步src，不复制其package.json或tsconfig，也不会自动删除镜像多出来的文件。提交前检查两边diff。CI无法读取另一个仓库的未发布工作副本，因此同步检查是本地职责。

打包CSS类名基于仓库内相对路径生成，避免不同电脑构建出不同hash。重新部署仍须遵循 [SETUP](docs/SETUP.md) 的备份与pnpm硬链接规则。代码整理之后的build不等于已经部署，也不继承旧构建的GPU性能验证。

## 修改约束

- 音频协议固定为16kHz、mono、PCM16 little-endian；更改时同时改服务端头、客户端解码和测试。
- 句子拆分对流式前缀保持稳定；保留已提交的换行边界。
- 恢复说话、切换会话和stop之后的异步结果不得重放旧文本或音频。
- GPU生产线程退出前不释放推理锁；客户端取消要传递到生产任务。
- 新模型使用显式路径与惰性导入；CPU测试不下载权重或隐式启动CUDA。
- 运行配置、日志、模型、参考音频不纳入提交。公开截图不含token，也不复制个人语音或未经确认可分发的媒体。

## 记录结果

改动行为时补充针对该边界的测试；性能改动另做真实链路测量。把日期、硬件、模型、构建、输入和计时起止写进实验记录，保留原始数据。README只引用有证据的结论，四轮样本不能变成p95承诺。

断轮表由归档数值计算，不下载语料；更新数据后运行 `python scripts/summarize_turn_eval.py > docs/experiments/2026-10-19-turn/RESULTS.md`。CI 对比生成表与存档，防止代码、分数和结果表不一致。图表另由 `scripts/render_figures.py` 生成。

本地工作区另有PROJECT_STATUS和EVOLUTION供多助手续接；本仓库的README、docs与实验资料不依赖那些仓库外文件，在GitHub上也应能完整阅读。

## 双语与设计路线

README.md 为英文入口，README.zh-CN.md 为中文入口，顶部互链。独立插件和源码镜像使用同样约定。修改功能、默认参数、命令或测量数字时，同一轮同步两种版本；图片和原始数据共用一份，不各自维护统计。详细指南当前为中文，英文 README 标明语言。

下一阶段的阶段依赖、设计取舍、实验协议与验收条件见 [设计与改进路线](docs/DESIGN_ROADMAP.md)。开始实施前先查看当前部署状态；路线中的目标不代表已实现。阶段完成后更新实验证据及对应路线决定，不把候选的 CPU 测试写成 GPU 部署完成。
