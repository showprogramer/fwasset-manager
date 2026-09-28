# Agent 工作流

补充 `AGENTS.md` 的协作、留档与提交细节，按需读取。

## 跨工具审查

写语义、schema、扫描索引、USB 或可能丢数据的改动，由 Claude Code 实现后请 Codex 只读审查（`/codex-review`）；其他改动靠必需检查和人工验证，不审。

```bash
uv run python scripts/cross_review.py
```

可加 `--spec <规格>`、`--focus "<重点>"`、`--base <基线>`、`--model <模型>`。审查指令在 [review-prompt.md](review-prompt.md)，结果写入系统临时目录。审查者只报问题；实现者逐条核实，修复成立的问题，并向用户说明不成立的理由。同类问题连续两轮出现，就停下来请用户裁决。

## 子 Agent 协作

默认一名 Agent 完成实现、文档、验证和提交，产品语义由用户决定。需要派子 Agent 时：

- 派单只给目标、修改范围、必要输入（规格章节、代码路径、候选差异）和验收条件，不转发完整对话。
- 同一工作区同时只有一个写入者（含测试、格式化和自动修复）；并行实现用独立 worktree，并指定集成负责人。每个任务只有一个提交负责人。
- 用环境提供的等待机制等子 Agent，不轮询、不读其他 Agent 会话。以产物和验证结果验收，不以状态标签为准；输出过长时让对方写到临时 Markdown，只返回路径。
- 返回约 20 行：结论、改动摘要、验证结果、未决问题、下一步。长日志写到临时文件，只回报退出码、关键统计和失败摘要。

## 跨会话交接

用户要求 handoff 时，在系统临时目录写交接文档并把路径交给用户，由用户选接手 Agent。内容：目标、现状、工作区路径与分支、基线 commit、未提交差异（存成 diff 文件）、最新验证、待办。已有规格和差异只引用，不复制。

接手方先核对路径、分支、HEAD 和已有差异；相关代码或输入变了，就重新验证，不沿用旧结论。

## 什么时候留档

| 内容 | 触发条件 | 保留内容 |
| --- | --- | --- |
| Task：`specs/active/` | 跨模块、多阶段、需跨会话接手或有待决规则 | 目标、规则、验收、最新验证及阻塞 |
| Review：`docs/code-review/` | 重大重构、公共 API、schema、扫描索引、USB、服务契约或 UI 入口改动 | 有证据的问题、处理结论及剩余风险 |
| CHANGELOG：`docs/CHANGELOG.md` | 用户可感知功能、重要修复、兼容性或安全变化 | 用户能观察到的结果 |
| 迁移说明：`docs/migrations/` | 路径、公共入口、配置/runtime、schema 或兼容层变化需要迁移 | 影响、迁移步骤及必要恢复方式 |
| 架构决策：`specs/decisions/` | 形成长期有效的架构决策 | 决策、理由与约束 |

验证只保留最新结果，不用旧的通过记录掩盖失败或未执行项。Task 完成后移到 `specs/archive/`；Review 问题关闭且改动合入后移到 `docs/code-review/archive/`，并在其 `INDEX.md` 补一行。运行截图放 `docs/ui-reference/screenshots/`，设计草图放 `specs/design/sketches/`。

### Task 格式

```markdown
# TASK-YYYYMMDD-short-name：任务名称

状态：进行中 / 实现完成，等待人工验证 / 已完成

## 目标与规则

当前行为、约束及范围；已有规格用链接引用。

## 验收清单

- [ ] 可观察、可验证的结果

## 验证

必要命令、场景及最新结果；待人工验证时写最短操作步骤。
```

有阻塞才加「待决事项」；状态须与验收清单一致。

### Review 与 CHANGELOG

- Review 首轮看完整任务差异，一次汇总有效问题；每条带稳定 ID、严重度、定位、触发条件、影响和处理结论。后续轮次只复核新差异、未关闭问题和受影响调用链。
- 连续两轮出现同类问题，先裁决规则或架构分歧，不为凑格式制造问题。
- CHANGELOG 的 Unreleased 按 Added / Changed / Fixed / Removed / Deprecated / Security 分类，同一功能合成一条；内部重构、测试和流程调整不记录。

## 验证与提交

1. 实现阶段先跑相关测试；候选稳定、审查阻断问题解决后，由提交负责人统一跑 `AGENTS.md` 的必需检查。之后代码或测试再变就重跑。
2. 是否需要人工验证，看验收点能否被自动断言，不看改动属于哪类模块（判据见 `AGENTS.md`）。
3. 提交前更新验收清单和适用文档，核对暂存范围只含本任务内容。检查失败、审查阻断或人工验证未通过时不得提交。

## 本地环境

- 用 PowerShell 的 Agent 以不加载 profile 的方式运行（如 `login=false`），统一 UTF-8。
- USB 路径和序列号用占位符；个人草稿放 `.local/ai-prompts/`。
- `.codexignore` 与 `.cursorignore` 内容保持同步；Claude Code 的读取限制在 `.claude/settings.json` 的 `permissions.deny`。
