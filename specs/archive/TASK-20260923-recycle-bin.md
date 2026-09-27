# 内置回收站：1 小时保留，到期送系统回收站

- 状态：已完成
- 日期：2026-09-23
- 来源：用户决策——「内置一个回收站，一段时间清理，这样不会和系统的回收站冲突」；**修订**：清空/彻底删除/到期一律**真删，全程不进电脑回收站**，以杜绝从电脑回收站撤回
- 关联：specs/archive/TASK-20260923-delete-asset-ui.md（删除入口，本文替换它的 5 秒撤销条）
- 影响面：`core/quarantine.py`（保留窗口常量）、`ui_qt/`（回收站页面、启动清理接入）、测试
- **不新写隔离机制**：`register_delete` / `undo_delete` / `sweep_expired` / `_send_to_system_recycle_bin` / `list_records` 全部已存在，本轮只调参 + 接线 + 加页面

## 1. 现状：机制已备好，只差接线

`core/quarantine.py` 早就是一个完整的内置回收站：

| 能力 | 函数 | 状态 |
| --- | --- | --- |
| 删除移入隔离区并登记 manifest（含内容哈希、原路径、被删空容器） | `register_delete:370` | 已实现，`delete_asset` 在用 |
| 从隔离区精确还原（重建容器 + 移回 + 对账索引） | `undo_delete:509` | 已实现，`undo_asset_delete` 在用 |
| 到期条目清理并出清单 | `sweep_expired:664` | 已实现，**无人调用**；本轮改为真删 |
| Windows 系统回收站（`SHFileOperationW` + `FOF_ALLOWUNDO`） | `_send_to_system_recycle_bin:605` | 已实现，**本轮删除链路不再调用** |
| 列出当前工作区的隔离记录 | `list_records:593` | 已实现，仅 `layout_update_service` 用 |

`sweep_expired` 的 docstring 明确写着接入归「子任务 8（启动恢复的应用编排）」。

**唯一的实质缺口**：`UNDO_WINDOW_SECONDS = 5.0`（`quarantine.py:66`），撤销窗口只有 5 秒。

## 2. 决策

1. 保留窗口 5 秒 → **1 小时**。
2. 删除后**不再弹 5 秒撤销条**，改为页面里的回收站。
3. **出回收站即永久删除。** 到期自动清理、手动「彻底删除」、「清空回收站」三条路径一律 `shutil.rmtree` 真删，**全程不调用系统回收站**。内置回收站是唯一的后悔入口。
4. 到期清理**在启动时做**。

### 2.1 为什么不进系统回收站

用户目的就是**杜绝从电脑回收站撤回**。绕开系统回收站同时解决了几个问题：

- 系统回收站不保留还原所需信息：不重建空模块容器，不恢复 `平台配置.toml` 的默认与 `型号配置.toml` 的借用。从那里还原会得到一个索引里没有、配置也不匹配的目录。
- 系统回收站可被**部分还原**（按项），会得到内容残缺的程序目录，且软件全程不知情。
- 工作区在移动硬盘 / 网络盘时系统回收站本就可能直接永久删除，行为不可依赖。

因此 `_send_to_system_recycle_bin`（`quarantine.py:605`）**本轮不再被任何路径调用**。函数保留（`transactional_retire` 的既有语义不在本轮范围），但删除链路不走它。

### 2.2 关闭期间的时间照常计入

`expires_at` 基于 `time.time()`（墙钟）并持久化在 manifest JSON 里（`quarantine.py:345-355`），**不是进程内单调时钟**。所以软件关着的那段时间照常流逝，重启后 `now >= expires_at` 直接成立。语义就是真实的 1 小时挂钟时间。

只启动时清理的唯一副作用：到期的**瞬间**不会自动消失，要等下次启动。例如 10:00 删、11:00 到期、14:00 才开软件，则 11:00–14:00 期间它仍在回收站里且**仍可还原**——这是宽容，不是数据损失。本轮不加后台定时器。

## 3. 改动项

### 3.1 保留窗口

`quarantine.py:66` 的 `UNDO_WINDOW_SECONDS` 改为 `3600.0`，并更名为 `RETENTION_SECONDS`（原名已名实不符——它不再是「撤销条窗口」而是「回收站保留期」）。`register_delete:388` 同步。

`transactional_retire`（换版本退位）**不受影响**：它的 `expires_at=0.0`，按 `committed` 状态清理，与时长无关。

### 3.2 删除入口

`entry_flows.open_delete_asset`：成功后不再调 `show_undo_bar`。改为 `toast`，文案指向回收站。

确认框补一句：删除后进入软件的回收站，1 小时内可在那里还原；超时或手动清理后**永久删除，电脑回收站里也没有**，届时需要手动重新新增。

`delete_undo_token` 保留（回收站页面还原时要用同一个 `quarantine_record_id`）。

### 3.3 回收站页面

新增 `ui_qt/recycle_interface.py`，挂到主窗口，标题「回收站」。

- 列表来自 `list_records(workspace_root)`，只列 `kind == "undoable_delete"` 且 `status == "pending"` 的记录。`transactional_retire` 不属于用户概念里的「删除」，不列。
- 每行：原路径的程序名、完整原路径、删除时间（`created_at` 转本地时间）、剩余保留时间。
- 行内动作：
  - **还原** → `undo_asset_delete(workspace_root, record_id)`。成功后刷新列表与网格。
  - **彻底删除** → 立即永久删除（见 3.4）。二次确认，文案写明电脑回收站里也不会有。
- 页脚：**清空回收站** —— 对当前列出的全部条目逐个执行彻底删除。逐个失败互不影响，最后汇总报告。同样二次确认。
- 空列表：「回收站是空的」。
- 页面顶部一句说明：出了这个回收站就找不回来了，电脑回收站里也没有。
- 页面打开时刷新一次，不做轮询。

### 3.4 永久删除

新增内部函数 `_purge(path)`：`shutil.rmtree`（文件则 `unlink`），不调系统回收站。

`sweep_expired` 的到期分支改用 `_purge`，不再调 `_send_to_system_recycle_bin`。清理失败仍标 `send_failed` 留在清单，下次启动重试（状态名保持不变，避免动 manifest schema 与既有恢复逻辑）。

`sweep_expired` 只处理**到期**条目，不能用于「立即清理」。新增：

```python
def discard_now(workspace_root: str | Path, record_ids: Sequence[str]) -> list[QuarantineRecord]: ...
```

- 与 `sweep_expired` 同一套安全检查：`_assert_lock_held`、`_assert_same_workspace`、`_assert_quarantine_path_owned`。越界或不属于本工作区的记录跳过、原样保留。
- 只接受 `kind == "undoable_delete"` 且 `status == "pending"` 的记录；其他 id 忽略。
- 未到期也能清（这正是「彻底删除」的用途）。
- 成功 → 出清单；失败 → `send_failed` 留在清单。
- 同样走 `_purge`。

UI 调用前必须取得工作区写锁（与其他写入口一致，走 `host.run_write`）。

### 3.5 启动清理

接进子任务 8 第 6 节的启动恢复序列，在既有三步**之后**追加一步：

```text
recover_interrupted_workspace → recover_on_startup → cleanup_staging_area（逐会话） → sweep_expired
```

- `sweep_expired` 要求已持锁（`_assert_lock_held`），必须在这条持锁序列里，不能在界面别处随手调。
- 抛错不阻断启动：记下异常文本，继续。与 staging 清理同样的容错。
- 返回的记录数写进日志，不弹窗。

## 4. 本轮不做

- 后台定时清理。到期条目等下次启动再清；在此之前仍可还原，不是损失。
- 回收站里的搜索、排序、分页。条目量级很小。
- 型号 / 方案删除进回收站。它们的删除入口本身还没做（见 TASK-20260923-delete-asset-ui 第 2 节）。做的时候复用同一页面。
- 保留期做成可配置项。写死 1 小时。
- 改 `transactional_retire`（换版本退位）的既有清理语义。它的 `expires_at=0.0`、按 `committed` 状态清理，与保留期无关；本轮只改它走 `_purge` 而非系统回收站，其余不动。

## 5. 错误码 → 界面动作

| 入口 | code | 动作 |
| --- | --- | --- |
| 还原 | `ok` | 行消失，刷新网格，`toast` |
| 还原 | `undo_failed` / `undo_conflict` | `alert`，行保留（内容仍在隔离区） |
| 彻底删除 | 成功 | 行消失，`toast` |
| 彻底删除 | 清理失败 | `alert`，行保留并标注「清理失败，下次启动重试」 |
| 启动清理 | 任意异常 | 仅日志，不弹窗，不阻断启动 |

## 6. 测试

`core`（不建 Qt）：

- `RETENTION_SECONDS == 3600.0`；`register_delete` 写入的 `expires_at` 约等于 `now + 3600`。
- **关闭期间计时**：`expires_at` 取自 `time.time()` 且落盘；构造一条 `expires_at` 在过去的记录，`sweep_expired` 立即清理它——证明不依赖进程存活。
- `sweep_expired`：未到期不动；到期清理并出清单；`transactional_retire` 仍按 `committed` 清理，不受时长影响。
- **不进系统回收站**：monkeypatch `_send_to_system_recycle_bin` 使其一旦被调用就失败，`sweep_expired` 与 `discard_now` 仍成功且目标路径真实消失。
- `discard_now`：只清指定 id；未到期也能清；`pending` 之外的记录忽略；跨工作区 / 越界记录跳过且保留；失败留 `send_failed`。
- 未持锁调用 `discard_now` 直接拒绝。

`ui_common` / Qt：

- 删除成功后**不**产生撤销条动作。
- 回收站行过滤：只列 `undoable_delete` + `pending`；`transactional_retire` 不出现；空列表有空状态。

不降低覆盖率门槛（80%）。

## 7. 验收清单

- [x] 保留期 1 小时；删除后不再有 5 秒撤销条
- [x] 关着软件的时间照常计入（`expires_at` 落盘、基于墙钟）
- [x] 回收站页面能列出、还原、彻底删除、清空
- [x] 到期 / 彻底删除 / 清空三条路径都是真删，**不进电脑回收站**
- [x] 启动时清理到期条目，异常不阻断启动
- [x] `discard_now` 未持锁被拒；越界记录不被清
- [x] 确认框与页面都写明「出回收站即永久删除，电脑回收站里也没有」
- [x] 不改 `register_delete` / `undo_delete` 的既有行为
- [x] ruff、mypy、pytest 通过，覆盖率不低于 80%

## 10. 实现记录（2026-09-23）

- `quarantine.py`：`UNDO_WINDOW_SECONDS` 5.0 → `RETENTION_SECONDS` 3600.0（旧名已名实不符，直接改名，三处调用方同步）。新增 `_purge`（`shutil.rmtree` / `unlink`）与 `discard_now`。`sweep_expired` 的到期分支改走 `_purge`，**删除链路不再调用 `_send_to_system_recycle_bin`**。模块 docstring 同步。
- `workspace_actions.py`：新增 `recycle_rows` + `_remaining_text`；`run_startup_recovery` 增加可选第 4 步 `sweep`，异常只记进 `cleanup_errors` 不阻断；`StartupRecovery` 加 `swept`。删掉已无生产调用方的 `delete_undo_token`。
- `entry_flows.open_delete_asset`：去掉 `show_undo_bar`，确认框文案改为回收站语义。
- 新建 `ui_qt/recycle_interface.py`，挂进主窗口导航（页顺序：工作台 / 回收站 / 软件修复 / 设置）。`_purge` 路径在 UI 侧自取 `WorkspaceLock` 后调 `discard_now`。
- 测试：`test_quarantine.py` 加 5 条（保留期与落盘、sweep 真删且不碰系统回收站、`discard_now` 未到期可清 / 需持锁 / 忽略越界），并把 3 条既有用例从 patch `_send_to_system_recycle_bin` 改为 patch `_purge`；`test_ui_orchestration.py` 加 `recycle_rows` 与启动第 4 步；`test_qt_smoke.py` 加回收站页空状态与行过滤。
- 验证：ruff、mypy 通过；pytest 1253 passed、1 skipped，覆盖率 92.73%。离屏构造主窗口确认 `recycleInterface` 已挂载。

## 8. 人工验证

1. 删一个程序 → 网格消失 → 回收站里有它，显示剩余时间。
2. 在回收站点还原 → 程序回到网格。
3. 再删一个 → 点彻底删除 → 出回收站列表 → **去电脑回收站确认里面没有它**（这是本轮的关键验收点）。
4. 关闭软件，把某条记录的 `expires_at` 改成过去时间，重启 → 该条目自动清理、出列表、电脑回收站里同样没有；其他未到期条目还在。
5. 再删一个，直接关软件等一会儿再开 → 剩余时间按真实挂钟减少，证明关闭期间照常计时。
