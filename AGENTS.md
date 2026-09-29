# Repository Guidelines

fwasset 是 Python 固件资产管理桌面应用（PySide6 + QFluentWidgets）。产品见 [README.md](README.md)，架构与领域规则见 [docs/architecture.md](docs/architecture.md)，协作、留档与提交细节见 [docs/agent-workflow.md](docs/agent-workflow.md)，按任务需要读取。

## 命令

只在 Windows `.venv` 下运行，不用 WSL。

```powershell
uv sync --extra dev             # 安装开发依赖
uv run fwasset                  # 启动应用
uv run ruff check src scripts   # 以下三条是 Python 改动的必需检查
uv run mypy
uv run python -m pytest -q      # 覆盖率门槛 80%，范围以 pyproject.toml 为准
```

## 工作方式

- 任务明确就直接做；只在缺失信息会影响正确性、范围或授权时询问，已定的事不反复确认。
- 用中文回复，先说结果，再补必要的验证、阻塞和下一步。
- 修改前检查工作区，保留用户已有改动。不读 `config.toml`、`.env`、`.runtime/` 和 Agent 本地状态；配置参考 `config.example.toml`。场景脚本与人工验证用测试副本，不碰真实用户数据。
- 小改动直接做；跨模块、多阶段或需跨会话接手的任务才在 `specs/active/` 建 Task。
- 高风险改动（写语义、schema、扫描索引、USB、可能丢数据）提交前请独立 Codex 只读审查，用法见[独立审查](docs/agent-workflow.md#独立审查)。
- 同一事实只写一处，其他地方引用；不写会话流水账、废弃方案或重复验证记录。

## 代码约束

- `core/` 放扫描、索引、配置和服务；`ui_common/` 放框架无关 ViewModel；`ui_qt/` 放 Qt 界面；测试在 `src/fwasset/tests/`。
- 新源文件用 `from __future__ import annotations`，新 helper 和公共方法补类型标注；导入顺序为标准库、第三方、`fwasset.*`。
- `core/types.py` 是 TypedDict / Literal 真源，字段变化同步生产者、消费者和测试。
- `core/services/` 返回 `ServiceResult`，用户消息用中文，不以裸异常代替服务错误码。
- `firmware_catalog.toml` 条目顺序影响匹配。手控 UI 的识别、版本来源和 U 盘复制规则见 [architecture.md](docs/architecture.md#固件目录与分类)。
- `asset_index.py` 采用单工作区语义；目录树和 TOML 是真源，SQLite 只是搜索缓存。
- UI 只用 Qt：registry + `PanelHost`、组合式 ViewModel、后台线程与结果队列；取消用 `threading.Event`。

## 验证与提交

- Python 改动须通过上面三条检查；纯文档改动只核对差异、链接和规则一致性。
- 验收点能被测试或一次性场景脚本完全断言的，检查通过即可提交。UI 交互或观感、真实设备、无法自动断言的性能边界、待定产品语义须人工验证：停在已暂存，回复「实现完成，等待人工验证」，附最短验证步骤；拿不准也按此处理。
- 用户已持续授权按流程提交，不必再问。按 [提交格式](docs/COMMIT_TEMPLATE.md) 只提交本任务内容，不 push，不为回填 hash 追加提交。
