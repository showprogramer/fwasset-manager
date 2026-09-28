@AGENTS.md

## Claude Code

- 读取限制、常用命令授权和 hook 在 `.claude/settings.json`。编辑 `.py` 后 hook 会自动跑 `ruff check`，报错会回传，直接修掉即可。
- 需要人工验证或独立审查的多阶段任务，先用 plan mode 定方案再动手。
