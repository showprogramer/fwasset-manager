# Repository Guidelines

这是一个 Python 固件资产管理桌面应用。产品见 [README.md](README.md)，架构见 [docs/architecture.md](docs/architecture.md)，流程见 [docs/agent-workflow.md](docs/agent-workflow.md)。按任务需要读取。

## 工作方式

- 明确的任务直接执行；只有缺失信息会影响正确性、范围或授权时才询问。已有决定不反复确认。
- 回复先说结果，只补充必要验证、阻塞和下一步；不复述需求、工具操作或逐步思考。
- 小改动不建 Task；跨模块、多阶段或需后续接手的任务才在 `specs/active/` 留当前规格。
- 多 Agent / Herdr 协作先读流程中的“多 Agent 协作”，按角色和候选版本交接。
- 同一事实只保留一处，其他文档引用；不保存会话流水账、废弃方案、重复验证或占位章节。
- 修改前检查工作区，保留用户已有改动；不默认读取 `config.toml`、`.env`、缓存、构建产物或 Agent 本地状态。配置参考 `config.example.toml`。

## 代码约束

- `src/fwasset/core/` 放扫描、索引、配置和服务；`ui_common/` 放框架无关 ViewModel；`ui_qt/` 放 PySide6 + QFluentWidgets 界面；测试在 `src/fwasset/tests/`。
- 新源文件使用 `from __future__ import annotations`；新 helper 和公共方法补类型标注。导入顺序：标准库、第三方、`fwasset.*`。
- `core/types.py` 是 TypedDict / Literal 真源，字段变化同步生产者、消费者和测试。
- `core/services/` 返回 `ServiceResult`，用户消息使用中文，不以裸异常代替服务错误码。
- `firmware_catalog.toml` 条目顺序影响匹配；`handcontrol_ui` 必须同时有 `.rom` 和 `.pkg`。
- `asset_index.py` 采用单工作区语义；目录树和 TOML 是真源，SQLite 是搜索缓存。
- UI 只用 Qt：registry + `PanelHost`、组合式 ViewModel、后台线程与结果队列；取消使用 `threading.Event`。

## 验证与提交

仅在 Windows `.venv` 下验证，不使用 WSL。Python 代码改动须通过：

```powershell
uv run ruff check src scripts
uv run mypy
uv run python -m pytest -q
```

纯文档改动检查差异、链接和规则一致性，无需运行 Python 全套检查。安装开发依赖用 `uv sync --extra dev`，启动应用用 `uv run fwasset`。检查范围与覆盖率以 `pyproject.toml` 为准，覆盖率门槛为 80%。

UI、核心公共 API、类型/服务契约、schema、扫描索引、USB、配置、性能边界、重构和新功能仍须在提交前完成人工验证；无 UI 的隔离场景验证方式见流程文档。需要验证时明确回复“实现完成，等待人工验证”，用户确认后更新记录。

用户已持续授权按流程自动提交，无需再次询问提交许可；人工验证、检查与提交时机统一见 [流程](docs/agent-workflow.md#验证与提交)。按 [提交格式](docs/COMMIT_TEMPLATE.md) 提交。不为回填 hash 或重复状态记录追加提交。Review、CHANGELOG、迁移说明按触发条件维护，规则统一见流程文档。
