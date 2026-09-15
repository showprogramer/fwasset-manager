# TASK-20260915-workspace-transaction-foundation：工作区事务状态基础

状态：已完成

## 目标与规则

本任务是 [CRUD 写语义](TASK-20260903-crud-write-semantics.md) 子任务 1b 的首个可独立验收切片：为后续 CRUD、权威扫描与索引写入提供跨进程排他锁、受管根初始化、持久化 generation / `WorkspaceState` / 操作日志，以及中断现场的阻写判定。它不接入尚未实现的 CRUD 入口，也不实现隔离、候选区提升或具体业务补偿。

- 锁键由已规范化的 configured root 计算；Windows 采用命名互斥体。获取超时返回 `workspace_busy`；同一进程的重复获取拒绝；系统报告 abandoned mutex 时视为中断现场。
- `.fwasset/staging`、`.fwasset/incomplete`、`.fwasset/state` 与同卷兄弟隔离根只能在持锁时初始化。目标已存在但没有应用标记时不改动任何内容，返回 `managed_root_occupied`；创建过程中失去竞争或任何时点检测到重定向都统一返回 `managed_root_occupied`：目录用 `exist_ok=False`、标记用 `O_EXCL` 创建（不可能覆盖他人内容），失败只撤回本应用新建的标记文件，绝不删除目录或外部内容。
- 状态文件位于已授权的 `workspace_state` 根，采用同目录临时文件 + `os.replace` 原子替换。初始值为 generation `0`、状态 `clean`、无操作日志。
- 状态文件的原子替换、日志删除与无锁预览读取会与并发读者发生 Windows 瞬态共享冲突：读写两侧均有界重试（预算 2 秒），瞬时冲突不误报 `recovery_required`；持续外部占用才按既有错误语义报告。
- 事务开始先把状态写为 `operation_in_progress` 并持久化操作日志；首次产品数据变更前 generation 递增为奇数。结束时 generation 必须为偶数：成功清除日志并回到 `clean`；失败或中断现场保留日志并进入 `recovery_required`。
- 发现奇数 generation、`operation_in_progress` 或未清理日志时，恢复入口必须将现场收敛为 `recovery_required` 且 generation 偶数；恢复完成或人工确认前，后续写入不得继续。
- 纯预览可用起止 generation 都相同且为偶数来判定可发布；`recovery_required` 下预览仍可读，但调用方必须携带警告。当前任务只提供判定与状态 API，不改扫描 UI。
- 新增 `WorkspaceState` 等公共类型时以 `core/types.py` 为唯一真源。内部模块抛出的领域异常由后续 service 映射为中文 `ServiceResult`，不让 UI 依赖底层异常文本。

## 验收清单

- [x] 未初始化工作区可在取得排他锁后建立四个受管根及所有权标记；占用或重定向根零覆盖、零删除。
- [x] 同一工作区第二个持锁者在超时后得到 busy；锁释放或异常退出后可重新获取。
- [x] 状态初始化、写入开始、成功结束与失败结束均满足状态—generation 顺序；预览 token 仅在相同偶数 generation 时有效。
- [x] 原子替换、日志删除与无锁读取在瞬态共享冲突下有界重试成功；预算耗尽分别按原始异常与 recovery_required 语义报告。
- [x] mkdir 与写标记之间的替换/重定向被认领后复核捕获：标记不留在外部目标（只撤回 O_EXCL 自建文件）、不删除被搬走的根或 junction，统一报告 `managed_root_occupied`。
- [x] 检测到中断现场后进入 `recovery_required` 并结束奇数 generation；该状态阻止新的写事务。
- [x] 操作日志只在成功完成后删除；失败和中断现场保留供后续具体恢复器使用。
- [x] 新增核心代码、类型与测试通过 ruff、mypy 和完整 pytest。
- [x] 隔离临时工作区场景验证通过（无 UI 入口可人工操作，按无 UI 等效规则以脚本关闭）。

## 验证

- 自动化：`uv run python -m pytest src\fwasset\tests\test_workspace_transaction.py -q --no-cov`（15 passed）；`uv run ruff check src scripts`、`uv run mypy`、`uv run python -m pytest -q`（均通过）。
- 隔离场景：`uv run python scripts\verify_workspace_transaction.py` 五场景断言全部通过——新增事务提交（generation 偶数、日志清除）、真实子进程写入期硬退出（保留状态与操作记录）、中断现场与恢复后两次阻写、`recover_interrupted_workspace` 收敛为 `recovery_required`；在临时目录执行，不触碰真实工作区。
- 文档：[REVIEW-20260915-workspace-transaction-foundation](../../docs/code-review/REVIEW-20260915-workspace-transaction-foundation.md) 已关闭；本切片不改变用户可见行为，CHANGELOG 与迁移说明不适用。

## 待决事项

- 具体 CRUD 操作的日志阶段、补偿与启动恢复器接入，依赖本基础并留在后续事务子切片定稿。
