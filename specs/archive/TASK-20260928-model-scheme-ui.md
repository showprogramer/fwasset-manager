# TASK-20260928-model-scheme-ui：型号与方案工作台入口

状态：已完成

## 目标与规则

工作台复用已有的型号、方案服务完成新增、重命名、删除；重命名就是界面上的修改，型号机芯类型建成后不可改。删除进入回收站，跨型号借用由服务返回影响后再确认。规则与服务见 [型号方案 CRUD](../active/TASK-20260918-model-scheme-crud.md)。

## 验收清单

- [x] 当前型号可重命名、删除；新建型号入口保留。型号变化后选择器与列表刷新，删除最后一个型号仍可新建。
- [x] 当前型号的方案可新建、重命名、删除；空方案也显示在侧栏，范围下拉沿用已有列表。
- [x] 删除前提示全部内容，跨型号借用显示影响并二次确认；回收站可还原型号与方案。
- [x] 人工操作确认菜单可见、弹窗可读、选择与列表在增改删还原后正确。

## 验证

- `uv run ruff check src scripts`：通过。
- `uv run mypy`：通过。
- `uv run python -m pytest -q`：1317 passed，1 skipped，覆盖率 92.74%，进程正常退出；现有 2 项依赖/环境警告。
- 人工验证：用户确认通过（2026-09-28）。

用户可见变化记于 [CHANGELOG](../../docs/CHANGELOG.md)。审查记录见 [REVIEW-20260928-model-scheme-ui](../../docs/code-review/archive/REVIEW-20260928-model-scheme-ui.md)。
