# DSH monorepo source mirror

This directory preserves the browser source for integration into a matching DeepSeek Harness monorepo. Its package/build layout differs from the independently buildable plugin.

Use the [standalone plugin](https://github.com/HenryVarro666/dsh-voice-ai-girlfriend-plugin) for product goals, behavior, tests and the normal build. Read the [main setup](../docs/SETUP.md) for monorepo integration; do not run the standalone npm build against this layout.

`src/` must match the standalone source. From the main repository, `python scripts/sync_plugin.py --check` verifies the mirror when the two checkouts are adjacent.
