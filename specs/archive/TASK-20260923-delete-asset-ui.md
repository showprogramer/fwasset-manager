# 删除程序：影响提示、确认、5 秒撤销

- 状态：已完成
- 日期：2026-09-23
- 来源：用户提问「当前是不是删除还没做」——确认未做，本轮补上程序层删除
- 关联：specs/archive/TASK-20260922-ui-orchestration.md（子任务 8 第 1.2 节把删除冻结在切片外，本文解冻其中的**程序删除**）
- 影响面：`ui_qt/workbench_window.py`（右键菜单）、`ui_qt/entry_flows.py`（删除流程）、`ui_common/workspace_actions.py`（撤销条时钟已有，复用）、测试
- **不改核心层**：`delete_asset`、`undo_asset_delete` 的签名、错误码、payload 一律不动

## 1. 现状

服务层齐全，UI 一处未接：

| 服务 | 位置 | 现有 UI |
| --- | --- | --- |
| `delete_asset` | `asset_service.py:838` | 无 |
| `undo_asset_delete` | `asset_service.py:1046` | 无 |
| `delete_model` / `delete_scheme` | `model_scheme_service.py:546` / `:565` | 无 |
| `undo_model_scheme_delete` | `model_scheme_service.py:589` | 无 |
| `rename_model` / `rename_scheme` | `model_scheme_service.py:393` 一带 | 无 |

用户现在删错程序只能去资源管理器手删，`平台配置.toml` 的 `defaults` 和 `型号配置.toml` 的借用记录会留下悬空引用。

## 2. 本轮做 / 不做

**做**：正式程序的删除 + 5 秒撤销，删除前展示引用命中。

**不做**（下一轮，各自影响面更大）：

- 型号删除、方案删除及其撤销。一删一片，影响对话框要列的东西完全不同。
- 型号 / 方案重命名。
- 备用副本（`旧版本/`）的浏览与删除。
- 批量删除。一次只删一份。

## 3. 入口

程序行右键菜单加「删除」，位置在「设为默认」「改厂商」之后，与其他破坏性操作分隔。

- 只对**存在磁盘路径**的行显示。借用来源行（`shared_state == "shared_hit"`）不显示——那份程序不属于本型号，删它要去源型号。
- 点击后先走 `host._write_gate`，与其他写入口一致。

## 4. 流程

删除是两阶段：先锁外反查并展示，再确认写入。**不要**在 UI 里自己判定谁能删。

### 4.1 第一次调用（不确认）

```text
delete_asset(configured_root, workspace_root, asset_path, confirm_shared=False)
```

服务内部已做锁外反查（`asset_service.py:875` 一带调 `find_references_to`），UI 不再自己调一次。

- 返回 `confirmation_required`：payload 带 `hits`（跨型号借用命中）与 `retired_copies`。展示影响对话框，列出每条命中，并写明备用副本份数。用户确认后进第 4.2 步。
- 返回 `ok` / `index_pending`：说明没有跨型号命中，服务已经删完。直接进第 4.3 步的撤销条。**不要**因为"没弹确认框"就再补一个。
- 返回其他失败码：按第 5 节处理，零写入。

### 4.2 确认后调用

同样参数，`confirm_shared=True`。其余入参**逐字不变**，不重新拼路径。

确认对话框必须写明：

- 跨型号借用命中条数与每条的位置（用 payload 里已有的字段，不重新分类）。
- 备用副本份数（`retired_copies`）——它们**不随本次删除一起消失**，仍在磁盘上。
- 删除后可在 5 秒内撤销，超时后只能从隔离区人工恢复。

### 4.3 撤销条

成功 payload 的 `quarantine_record_id` 进 5 秒 `undo_bar`，复用子任务 8 第 5 节的撤销条与既有时钟（`workspace_actions.py` 的 `undo_bar` 判定）。点击调用：

```text
undo_asset_delete(workspace_root, record_id)
```

5 秒从删除成功回到 UI 时起算。超时只隐藏按钮，不写盘、不再调用。撤销 token 只活在当前进程，重启不恢复。

## 5. 错误码 → 界面动作

沿用子任务 8 第 5 节的通用动作表。本入口额外：

| code | 动作 |
| --- | --- |
| `ok` | 刷新网格，`toast` + `undo_bar` |
| `index_pending` | 同上 + `rescan_hint` |
| `confirmation_required` | 影响对话框（第 4.1 节）。取消则零写入 |
| `invalid_target` | `alert`。目标已不在，刷新网格 |
| `layout_invalid` | `alert` |
| `lookup_blocked` | `alert`。配置损坏导致反查不完整，服务已阻止删除。提示先去软件修复页查看 |
| `stale_plan` | `alert`「目标内容已变化，请重新删除」。不自动重试 |
| `quarantine_failed` | `alert`。payload 带 `recovery_required` 时按通用规则进恢复横幅 |
| 撤销 `ok` | 行回到网格，`toast` |
| 撤销 `undo_failed` / `undo_conflict` | `alert`。条已消费就不再显示 |

任意返回后都 `load_workspace_status`；`recovery_required` 进横幅，即使本次 `code` 不是它。

## 6. 测试

`ui_common`（可单测、不建 Qt）：

- `confirmation_required` → 影响对话框动作；取消不产生第二次调用。
- 确认后的第二次调用 `confirm_shared=True`，其余入参与第一次逐字相同。
- 成功结果产生 `undo_bar`，token 取自 `payload["quarantine_record_id"]`。
- 超时后撤销条不可再调用（复用既有时钟断言）。
- `lookup_blocked` / `stale_plan` 是 `alert`，不产生 `undo_bar`。

Qt：

- 右键菜单对普通程序行有「删除」，对借用来源行没有。

不降低覆盖率门槛（80%）。`ui_qt` 仍在 omit 中。

## 7. 验收清单

- [x] 右键有「删除」；借用来源行没有
- [x] 跨型号命中时先出影响对话框，取消则零写入
- [x] 确认后第二次调用 `confirm_shared=True`，入参不变
- [x] 成功后 5 秒撤销可用，超时后不再调用
- [x] `lookup_blocked` / `stale_plan` 不产生撤销条
- [x] 不改 `delete_asset` / `undo_asset_delete` 的签名与错误码
- [x] ruff、mypy、pytest 通过，覆盖率不低于 80%

## 9. 实现记录（2026-09-23）

- `workspace_actions.effect_for_result`：`confirmation_required` 改为按 entry 分流——`delete_asset` 得 `delete_confirm`，借用仍是 `overwrite_confirm`。两者语义不同，动作名不再共用。
- 新增 `delete_confirm_resubmit`（只翻 `confirm_shared`，不就地改调用方字典）与 `delete_undo_token`（只有 `ok` / `index_pending` 才给 token）。
- `entry_flows.open_delete_asset` + `_confirm_delete_impact`：先 `confirm_shared=False` 试删，服务返回 `confirmation_required` 时弹影响对话框（列命中、备用副本份数、撤销窗口），确认后原参 `confirm_shared=True` 重提；成功走既有 `show_undo_bar`。
- `workbench_window`：右键菜单末尾加「删除」，`shared_state == "shared_hit"` 不给。
- 测试：`test_ui_orchestration.py` 加 6 条（动作分流、重提入参、token 取值、超时、确认链路、取消零写入）；`test_qt_smoke.py` 加借用行无删除，并更新两条既有菜单断言（多一项「删除」、分隔线 +1）。
- 验证：ruff、mypy 通过；pytest 1244 passed、1 skipped，覆盖率 92.71%。

## 8. 人工验证

接在既有链之后，同一临时空目录：

1. 建两个程序，删掉其中一个没有被引用的 → 直接删，出现撤销条 → 点撤销 → 程序回来。
2. 再删一次，等 5 秒 → 撤销条消失，程序确实没了。
3. 把某个程序设为默认，再在另一个型号上借用它 → 删它 → **出现影响对话框**并列出命中 → 取消 → 程序还在。
4. 同一个再删一次 → 确认 → 删掉，借用方显示「借用来源缺失」。
