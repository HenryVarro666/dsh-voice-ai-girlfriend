# DSH monorepo 源码镜像

此目录保留接入匹配版本 DeepSeek Harness monorepo 的浏览器源码；包与构建布局不同于可独立构建的插件。

产品目标、行为、测试和常规构建见[独立插件](https://github.com/HenryVarro666/dsh-voice-ai-girlfriend-plugin)。monorepo 接入见[主安装说明](../docs/SETUP.md)，不要在本布局直接运行独立插件 npm 构建。

`src/` 必须与独立插件一致。两个 checkout 相邻时，在主仓库运行 `python scripts/sync_plugin.py --check` 验证镜像。
