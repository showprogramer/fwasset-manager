"""D0.3 legacy 布局归一与 D1.4a 普通 update 事务（子任务 6a）。

两个入口都是 D8.3 协调器清单内的复合写：共用持久化操作日志、阶段标记与
启动恢复，产品数据首次变更前 ``begin_product_write()`` 把 generation 推成
奇数，收尾再推成偶数。

锁边界总则（父规格 D1.1 / D1.4a / D8）：用户选择一律发生在**锁外**，锁内
只做重验。锁内尚未发生产品写入的拒绝分支必须 ``commit()``，以 clean 偶数
generation 收束（子任务 4 定稿、5/5a 沿用）；已落盘半成品的失败路径保持
``recovery_required``，不 commit 成 clean。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwasset.core.admission import AdmissionError, validate_new_path
from fwasset.core.asset_index import AssetIndexError
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.file_scan import scan_firmware_subtree
from fwasset.core.import_io import (
    AssetImportError,
    stage_import_directory,
)
from fwasset.core.managed_paths import RETIRED_VERSIONS_DIRNAME
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    same_path_identity,
)
from fwasset.core.quarantine import QuarantineError, register_retire
from fwasset.core.reference_lookup import (
    _validate_target_kind,
    check_reference_gate,
    find_references_to,
)
from fwasset.core.services.reference_service import (
    _derive_semantics,
    apply_rewrite_plan,
    build_rewrite_plan,
)
from fwasset.core.staging_io import (
    ProductCleanupConflict,
    StagingError,
    allocate_staging_area,
    cleanup_staging_area,
    delete_recorded_product,
    promote_staging,
)
from fwasset.core.types import (
    FirmwareAsset,
    ReferenceSemantics,
    RetireMode,
    RewriteRequest,
    ServiceResult,
)
from fwasset.core.workspace_transaction import (
    WorkspaceBusyError,
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
)

__all__ = [
    "normalize_module_leaf",
    "update_asset",
]


def _error(
    code: str, message: str, payload: dict[str, Any] | None = None
) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def _ok(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": True, "code": code, "message": message, "payload": payload or {}}


def _transaction_error(exc: Exception) -> ServiceResult:
    if isinstance(exc, WorkspaceBusyError):
        return _error("workspace_busy", "工作区正在执行另一项写操作，请稍后重试")
    if isinstance(exc, WorkspaceRecoveryRequiredError):
        return _error("recovery_required", "工作区存在待恢复的中断操作，暂不能写入")
    return _error("write_failed", f"写入失败：{exc}")


def _scan_single_asset(
    workspace_root: Path, target: Path
) -> tuple[FirmwareAsset | None, str]:
    """冷扫子树并要求恰好一个 path 身份相同的完整合法资产。

    扫描入口与全根扫描共享同一推导实现，category / model_directory 等上下文
    字段与全根扫描字段级一致（见 ``file_scan.scan_firmware_subtree``）。
    """
    try:
        assets, issues = scan_firmware_subtree(str(workspace_root), str(target))
    except Exception as exc:  # noqa: BLE001
        return None, f"无法读取程序目录：{exc}"
    if any(str(issue.get("severity")) == "error" for issue in issues):
        return None, "程序目录扫描存在错误"
    matches = [
        item
        for item in assets
        if same_path_identity(str(item["path"]), str(target))
    ]
    if len(matches) != 1:
        return None, "目标不构成唯一的完整合法程序"
    return matches[0], ""


def _has_child_directories(target: Path) -> bool:
    try:
        return any(child.is_dir() for child in target.iterdir())
    except OSError:
        return False


def _semantics_of(workspace_root: Path, target: Path) -> ReferenceSemantics | None:
    """派生 R8 语义快照：复用 R8 自己的权威派生，不在本服务另起一套。

    ``build_rewrite_plan`` 会把请求里的快照与它内部 ``_derive_semantics``
    的结果逐字段比对，手工拼 ``FirmwareAsset`` 字段必然对不上（模块键要
    canonical、source_group 取型号 id）。直接复用同一函数即可保证一致。
    """
    derived = _derive_semantics(target, workspace_root)
    if derived is None:
        return None
    return ReferenceSemantics(
        model_id=derived["model_id"],
        module_key=derived["module_key"],
        source_group=derived["source_group"],
        scheme_name=derived["scheme_name"],
    )


# ---------------------------------------------------------------------------
# D0.3 legacy 模块叶子归一
# ---------------------------------------------------------------------------


def normalize_module_leaf(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    module_path: str | Path,
    asset_name: str,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """把 legacy 模块叶子 ``M`` 归一为容器 + 变体 ``M/V``（D0.3）。

    ``asset_name`` **必须由调用方（最终是用户）传入**并经 D3 名称校验。实现
    不得从版本号、固件文件名或类型名派生或兜底猜测——legacy 模块叶子的目录
    名通常就是类型名，没有独立程序名可派生，猜错会把类型名写成程序名。

    归一属**身份延续**而非新身份占用：目标路径命中的历史锚点（指向 ``M``
    的 default 与借用）是预期结果，走 D3 窄化例外；仅当发现本次计划之外的
    重叠锚点才返回 ``path_identity_conflict``。

    状态机借道 staging（不能把目录 ``M`` 直接移进自身后代 ``M/V``）：
    ``M`` → staging → 新建容器 ``M`` → staging → ``M/V`` → 引用同步 → 对账。
    归一不可撤销（D10.2），操作前需用户确认。
    """
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    name = str(asset_name or "").strip()
    if not name:
        return _error(
            "invalid_asset_name",
            "请输入新的程序名称：归一必须由用户指定程序名，不能从类型名推断",
        )
    # 程序名是**单个目录段**：带分隔符会让 module/name 变成多级路径，
    # 准入会先报 domain_violation，掩盖「名称非法」这个真实原因。
    if len(Path(name).parts) != 1 or name in (".", ".."):
        return _error(
            "invalid_asset_name",
            f"程序名称「{name}」不能包含路径分隔符",
            {"asset_name": name},
        )

    try:
        module = assert_within_workspace(Path(module_path), ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"模块目录不在工作区内：{exc}")

    if not module.is_dir():
        return _error("not_module_leaf", "模块目录不存在或不是目录")
    if _has_child_directories(module):
        return _error(
            "not_module_leaf",
            "该模块目录下已有变体子目录，无需归一",
            {"module": str(module)},
        )

    asset, reason = _scan_single_asset(ws, module)
    if asset is None:
        return _error("not_single_asset", f"模块目录无法作为单个程序归一：{reason}")

    target = module / name
    try:
        validate_new_path(
            target,
            kind="asset",
            configured_root=str(configured_root or ""),
            workspace_root=ws,
        )
    except AdmissionError as exc:
        if exc.code == "path_identity_conflict":
            # 窄化例外（D3）：归一是**身份延续**而非新身份占用——指向 M 的
            # 锚点在归一后随计划一起改写到 M/V，仍指向同一个程序，是预期
            # 结果而非冲突。只有本次计划改写不到的锚点才是真冲突。
            extra = _anchor_hits_outside_plan(
                configured_root, ws, exc, module, target
            )
            if extra:
                return _error(
                    "path_identity_conflict",
                    "目标路径命中本次归一计划之外的锚点，已阻止",
                    {"hits": extra},
                )
        elif exc.code in ("invalid_name", "path_exists"):
            return _error("invalid_asset_name", exc.message, exc.payload)
        else:
            return _error(exc.code, exc.message, exc.payload)

    try:
        with WorkspaceTransaction(ws, operation="normalize_module_leaf") as transaction:
            return _run_normalize(
                transaction, ws, configured_root, module, target, log_fn
            )
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _anchor_hits_outside_plan(
    configured_root: str | Path | None,
    ws: Path,
    exc: AdmissionError,
    module: Path,
    target: Path,
) -> list[dict[str, Any]]:
    """筛出**本次归一计划改写不到**的锚点。

    准入检查（``find_dangling_anchors``）会把「指向 M/V 的悬空锚点」一律报
    成冲突，但归一恰恰要把指向 M 的引用改写到 M/V——这些锚点由本次 R8
    计划覆盖，属身份延续。判据是反查 M 得到的命中集：凡在其中的 ``(owner,
    raw_key)`` 都会被计划改写，其余才是真冲突。

    反查本身失败（配置损坏等）时**不做窄化**，原样报冲突（fail-closed）。
    """
    hits = exc.payload.get("hits")
    if not isinstance(hits, list) or not hits:
        return []
    covered: set[tuple[str, str]] = set()
    lookup = find_references_to(configured_root, ws, module, "asset")
    if lookup["ok"]:
        result = lookup["payload"].get("result")
        if result is not None:
            covered = {
                (os.path.normcase(hit.owner_root), hit.raw_key)
                for hit in result.hits
            }
    outside: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        key = (
            os.path.normcase(str(hit.get("owner_root", ""))),
            str(hit.get("raw_key", "")),
        )
        if key in covered:
            continue
        outside.append(hit)
    return outside


def _run_normalize(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    module: Path,
    target: Path,
    log_fn: Callable[..., None],
) -> ServiceResult:
    """D0.3 六步状态机；每步进入前 ``set_phase``，失败按补偿表收敛。"""
    # 步骤 1：build 在文件动作**之前**采集 preimage（R8 契约）。
    asset, reason = _scan_single_asset(ws, module)
    if asset is None:
        transaction.commit()  # 锁内重验失败，尚未写盘 → 收成 clean
        return _error("not_single_asset", f"锁内重验失败：{reason}")
    semantics = _semantics_of(ws, module)
    if semantics is None:
        transaction.commit()
        return _error("not_single_asset", "无法派生程序归属（型号 id 缺失或路径异常）")
    transaction.set_phase("build_plan", details={"module": str(module)})
    plan_result = build_rewrite_plan(
        configured_root,
        ws,
        RewriteRequest(
            operation="rename",
            target_kind="asset",
            old_path=str(module),
            new_path=str(target),
            old_semantics=semantics,
            new_semantics=semantics,
        ),
        log_fn,
    )
    if not plan_result["ok"]:
        transaction.commit()  # 未写盘
        return _error(
            plan_result["code"],
            f"归一计划构建失败：{plan_result['message']}",
            plan_result["payload"],
        )
    plan = plan_result["payload"]["plan"]

    transaction.begin_product_write()

    # 步骤 2：M → staging（不能把 M 直接移进自身后代 M/V）
    transaction.set_phase("move_to_staging")
    try:
        staging = allocate_staging_area(ws, transaction)
        os.rmdir(staging)  # os.replace 要求目标不存在
        os.replace(module, staging)
    except (OSError, StagingError) as exc:
        transaction.commit()  # 原状不变
        return _error("normalize_failed", f"模块目录暂存失败，已保持原状：{exc}")

    # 步骤 3：创建新的模块容器 M
    transaction.set_phase("create_container")
    try:
        module.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return _restore_from_staging(
            transaction, ws, staging, module, f"模块容器创建失败：{exc}"
        )

    # 步骤 4：staging → M/V
    transaction.set_phase("promote_variant", details={"target": str(target)})
    try:
        promote_staging(transaction, ws, staging, target)
    except (OSError, StagingError) as exc:
        # 硬条件：只有新 M 仍为空才 rmdir 回退。非空说明有外部写入，
        # **禁止递归清理未知内容**（应用锁挡不住资源管理器）。
        if _directory_is_empty(module):
            try:
                module.rmdir()
            except OSError:
                return _keep_scene(
                    transaction,
                    "layout_inconsistent",
                    f"归一中断且无法清理空容器，已保留现场：{exc}",
                    {"staging": str(staging), "module": str(module)},
                )
            return _restore_from_staging(
                transaction, ws, staging, module, f"变体提升失败：{exc}"
            )
        return _keep_scene(
            transaction,
            "layout_inconsistent",
            f"归一中断且模块目录已被外部写入，已保留现场（未删除任何内容）：{exc}",
            {"staging": str(staging), "module": str(module)},
        )

    # 步骤 5：引用同步（static / follow_asset / defaults）
    transaction.set_phase("apply_plan")
    apply_result = apply_rewrite_plan(plan, configured_root, log_fn)
    if not apply_result["ok"]:
        if apply_result["code"] == "rollback_conflict":
            # 回滚存在冲突 → **停止自动移动**，保留现场
            return _keep_scene(
                transaction,
                "layout_inconsistent",
                "引用改写失败且回滚存在冲突，已保留现场，请按恢复材料人工处理",
                apply_result["payload"],
            )
        return _rollback_normalize(transaction, ws, module, target, apply_result)

    # 步骤 6：对账（磁盘为准，不回滚）
    transaction.set_phase("reconcile")
    try:
        reconcile_subtree(str(ws), str(module.parent))
    except (AssetIndexError, OSError) as exc:
        # 磁盘是真源、SQLite 只是搜索缓存：对账失败不回滚、也不置
        # recovery_required（阻写代价过高），重扫即可恢复。归一本身已成功，
        # 故 ok=True + 独立 code，让 UI 能提示「请重新扫描」。
        transaction.commit()
        log_fn(f"归一后索引对账失败，需重新读取程序列表：{exc}")
        return _ok(
            "reindex_failed",
            f"已归一为「{module.name}/{target.name}」，但索引对账失败，请重新扫描程序列表",
            {"module": str(module), "asset": str(target), "detail": str(exc)},
        )

    transaction.commit()
    log_fn(f"归一完成：{module} → {target}")
    return _ok(
        "ok",
        f"已归一为「{module.name}/{target.name}」",
        {"module": str(module), "asset": str(target)},
    )


def _directory_is_empty(target: Path) -> bool:
    try:
        return not any(target.iterdir())
    except OSError:
        return False


def _restore_from_staging(
    transaction: WorkspaceTransaction,
    ws: Path,
    staging: Path,
    module: Path,
    detail: str,
) -> ServiceResult:
    """补偿：staging → M，恢复原状；恢复失败则保留现场。"""
    try:
        os.replace(staging, module)
    except OSError as exc:
        return _keep_scene(
            transaction,
            "layout_inconsistent",
            f"{detail}；且原状恢复失败（{exc}），已保留现场",
            {"staging": str(staging), "module": str(module)},
        )
    transaction.commit()  # 磁盘已回到原状
    return _error("normalize_failed", f"{detail}，已恢复原状")


def _rollback_normalize(
    transaction: WorkspaceTransaction,
    ws: Path,
    module: Path,
    target: Path,
    apply_result: ServiceResult,
) -> ServiceResult:
    """apply 完整回滚成功后，反向恢复目录结构（M/V → M）。"""
    staging_back = module.parent / f".normalize-rollback-{os.getpid()}"
    try:
        os.replace(target, staging_back)
        module.rmdir()
        os.replace(staging_back, module)
    except OSError as exc:
        return _keep_scene(
            transaction,
            "layout_inconsistent",
            f"引用改写失败且目录恢复中断（{exc}），已保留现场",
            {"module": str(module), "target": str(target)},
        )
    transaction.commit()
    return _error(
        "normalize_failed",
        f"引用改写失败，已恢复原状：{apply_result['message']}",
        apply_result["payload"],
    )


def _keep_scene(
    transaction: WorkspaceTransaction,
    code: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> ServiceResult:
    """已落盘半成品的失败路径：**不 commit 成 clean**。

    事务 ``__exit__`` 会把 generation 推成偶数以结束 seqlock，同时把
    ``WorkspaceState`` 留在 ``recovery_required``——所有新写操作一律阻止，
    预览只读放行但带警告（D8.4）。不一致现场不得重新开放写入。
    """
    return _error(code, message, payload)


# ---------------------------------------------------------------------------
# D1.4a 普通 update 事务
# ---------------------------------------------------------------------------


def update_asset(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_asset: str | Path,
    source: str | Path,
    *,
    retire_mode: RetireMode,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """用 ``source`` 的内容替换 ``old_asset``，旧程序按 ``retire_mode`` 退位。

    落点矩阵（D1.4）：模块叶子唯一程序须**先按 D0.3 归一**（本入口遇未归一
    叶子直接返回 ``normalize_required``，不隐式归一）；模块下变体之一则把
    staging 提升为同模块下的**兄弟变体目录**——不得原位覆盖，不得把旧资产
    变成 replacement 的父容器。

    来源选择、影响预览与退位方式选择都在**锁外**完成，用户确认后才取写锁。

    **禁止出现「旧已退位但新程序不可用」的结果**：退位一律排在引用改写成功
    之后；退位失败返回 ``retire_failed`` 并保留两份。
    """
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    if retire_mode not in ("retire_to_trash", "retire_to_backup"):
        return _error("invalid_args", f"未知的退位方式：{retire_mode}")

    try:
        old = assert_within_workspace(Path(old_asset), ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"旧程序不在工作区内：{exc}")

    src = Path(source)
    if not src.is_dir():
        return _error("invalid_args", f"来源不是目录：{src}")

    if not old.is_dir():
        return _error("invalid_target", "旧程序目录不存在或不是目录")
    # 未归一的模块叶子不允许直接 update：必须先走 D0.3，避免把 replacement
    # 变成旧资产的兄弟文件而不是兄弟变体。
    if _is_unnormalized_module_leaf(old):
        return _error(
            "normalize_required",
            "该程序仍是未归一的模块目录，请先执行布局归一再更新",
            {"module": str(old)},
        )

    kind_detail = _validate_target_kind(old, "asset")
    if kind_detail is not None:
        return _error("invalid_target", f"旧程序不是合法程序目录：{kind_detail}")

    target = old.parent / src.name
    if same_path_identity(target, old):
        return _error(
            "invalid_args", "新程序名与旧程序同名，请先重命名来源目录"
        )

    try:
        with WorkspaceTransaction(ws, operation="update_asset") as transaction:
            return _run_update(
                transaction,
                ws,
                configured_root,
                old,
                src,
                target,
                retire_mode,
                log_fn,
            )
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _is_unnormalized_module_leaf(target: Path) -> bool:
    """目标是「模块目录直接含固件文件」的 legacy 叶子（父目录是 通用/方案）。"""
    parent = target.parent
    if parent.name != "通用" and parent.parent.name != "定制":
        return False
    if _has_child_directories(target):
        return False
    try:
        return any(child.is_file() for child in target.iterdir())
    except OSError:
        return False


def _run_update(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    old: Path,
    src: Path,
    target: Path,
    retire_mode: RetireMode,
    log_fn: Callable[..., None],
) -> ServiceResult:
    """D1.4a 七步事务；失败按契约表收敛。"""
    # 步骤 1：锁内重验旧程序仍是唯一完整合法资产（尚未写盘）
    old_scanned, reason = _scan_single_asset(ws, old)
    if old_scanned is None:
        transaction.commit()
        return _error("invalid_target", f"锁内重验失败：{reason}")

    # 步骤 2：staging 导入并验证完整性（D1.5）
    transaction.set_phase("stage_import", details={"source": str(src)})
    transaction.begin_product_write()
    try:
        staged = stage_import_directory(transaction, ws, src)
    except (AssetImportError, StagingError, OSError) as exc:
        transaction.commit()  # staging 由导入侧自行清理，产品区零改动
        return _error("incomplete_replacement", f"来源导入失败：{exc}")

    session = Path(staged["session"])
    if not _staging_is_complete_asset(ws, session, target):
        cleanup_staging_area(ws, session)
        transaction.commit()
        return _error(
            "incomplete_replacement",
            "来源内容不是完整合法的程序，已取消更新",
            {"source": str(src)},
        )

    # 步骤 3：提升 replacement 到最终路径，record_product 记录提升时 manifest
    transaction.set_phase("promote_replacement", details={"target": str(target)})
    try:
        validate_new_path(
            target,
            kind="asset",
            configured_root=str(configured_root or ""),
            workspace_root=ws,
        )
        promote_staging(transaction, ws, session, target)
    except (AdmissionError, StagingError, OSError) as exc:
        cleanup_staging_area(ws, session)
        transaction.commit()  # 产品区尚未改动
        code = exc.code if isinstance(exc, AdmissionError) else "incomplete_replacement"
        return _error(code, f"新程序提升失败：{exc}")

    # 步骤 4：此时 old 与 replacement 同为现存合法资产 → build
    transaction.set_phase("build_plan")
    new_scanned, reason = _scan_single_asset(ws, target)
    if new_scanned is None:
        return _cleanup_product(
            transaction, ws, target, "plan_build_failed", f"新程序校验失败：{reason}"
        )
    old_semantics = _semantics_of(ws, old)
    new_semantics = _semantics_of(ws, target)
    if old_semantics is None or new_semantics is None:
        return _cleanup_product(
            transaction,
            ws,
            target,
            "plan_build_failed",
            "无法派生新旧程序归属（型号 id 缺失或路径异常）",
        )
    plan_result = build_rewrite_plan(
        configured_root,
        ws,
        RewriteRequest(
            operation="update",
            target_kind="asset",
            old_path=str(old),
            replacement_path=str(target),
            old_semantics=old_semantics,
            new_semantics=new_semantics,
        ),
        log_fn,
    )
    if not plan_result["ok"]:
        return _cleanup_product(
            transaction,
            ws,
            target,
            "plan_build_failed",
            f"引用改写计划构建失败：{plan_result['message']}",
            plan_result["payload"],
        )

    # 步骤 5：apply
    transaction.set_phase("apply_plan")
    apply_result = apply_rewrite_plan(plan_result["payload"]["plan"], configured_root, log_fn)
    if not apply_result["ok"]:
        if apply_result["code"] == "rollback_conflict":
            # 回滚冲突 → 保留现场，**不自动删除 replacement**
            return _keep_scene(
                transaction,
                "update_inconsistent",
                "引用改写失败且回滚存在冲突，已保留现场，请按恢复材料人工处理",
                apply_result["payload"],
            )
        return _cleanup_product(
            transaction,
            ws,
            target,
            "config_rewrite_failed",
            f"引用改写失败，已恢复原状：{apply_result['message']}",
            apply_result["payload"],
        )

    # 步骤 6：旧程序退位（两条独立分支，落点与生命周期完全不同）
    transaction.set_phase("retire_old", details={"retire_mode": retire_mode})
    try:
        if retire_mode == "retire_to_trash":
            record = register_retire(ws, old)
            retired: dict[str, Any] = {"quarantine_id": record["id"]}
        else:
            retired = {"backup": str(_retire_to_backup(old, target))}
    except (QuarantineError, OSError) as exc:
        # 引用与 defaults 已指向新程序，新程序可用；保留两份等待人工处理
        return _keep_scene(
            transaction,
            "retire_failed",
            f"新程序已上岗，但旧程序退位失败，两份暂时并存：{exc}",
            {"old": str(old), "replacement": str(target)},
        )

    # 步骤 7：对共同祖先对账（磁盘为准，不回滚）
    transaction.set_phase("reconcile")
    try:
        reconcile_subtree(str(ws), str(old.parent))
    except (AssetIndexError, OSError) as exc:
        # 同 D0.3：磁盘为准，缓存失配不阻写（见 _run_normalize 的说明）
        transaction.commit()
        log_fn(f"更新后索引对账失败，需重新读取程序列表：{exc}")
        return _ok(
            "reindex_failed",
            f"已更新为「{target.name}」，但索引对账失败，请重新扫描程序列表",
            {
                "old": str(old),
                "replacement": str(target),
                "detail": str(exc),
                **retired,
            },
        )

    transaction.commit()
    log_fn(f"更新完成：{old} → {target}")
    return _ok(
        "ok",
        f"已更新为「{target.name}」",
        {"old": str(old), "replacement": str(target), **retired},
    )


def _staging_is_complete_asset(ws: Path, session: Path, target: Path) -> bool:
    """D1.5：staging 内容按**最终落点**的模块上下文判定是否完整合法资产。

    直接扫 staging 目录拿不到 category / 模块归属（它在受管区内），因此按
    目标路径的模块目录名做 catalog 匹配——与提升后的扫描语义一致。
    """
    from fwasset.core.file_scan import _match_catalog_type
    from fwasset.core.firmware_catalog import enabled_firmware_types

    try:
        filenames = [f.name for f in session.iterdir() if f.is_file()]
    except OSError:
        return False
    if not filenames:
        return False
    try:
        types = list(enabled_firmware_types())
    except Exception:  # noqa: BLE001 — catalog 不可用时 fail-closed
        return False
    return _match_catalog_type(str(target), filenames, types) is not None


def _cleanup_product(
    transaction: WorkspaceTransaction,
    ws: Path,
    target: Path,
    code: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> ServiceResult:
    """D1.4c「删除本次产物」：三条件全满足才删，任一不满足保留现场。

    条件由 ``delete_recorded_product`` 统一校验：① 路径仍是本操作创建的
    身份；② 当前 manifest hash 等于提升时 ``record_product`` 的值；③ 操作
    日志状态允许清理。任一失守 → ``update_inconsistent`` + 恢复材料，
    **禁止递归删除**（应用锁挡不住资源管理器往 replacement 里加文件）。
    """
    try:
        delete_recorded_product(transaction, ws, target)
    except ProductCleanupConflict as exc:
        return _keep_scene(
            transaction,
            "update_inconsistent",
            f"{message}；本次产物无法安全清理，已保留现场：{exc}",
            {"target": str(target), "reason": exc.reason},
        )
    transaction.commit()  # 磁盘已回到原状
    return _error(code, message, payload)


def _allocate_backup_destination(current: Path, old_name: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_root = current / RETIRED_VERSIONS_DIRNAME
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / f"{old_name}-{stamp}"
    suffix = 2
    while destination.exists():
        destination = backup_root / f"{old_name}-{stamp}-{suffix}"
        suffix += 1
    return destination


def _retire_to_backup(old: Path, current: Path) -> Path:
    """6a 最小 backup：把旧程序整目录搬到 ``replacement/旧版本/<旧名>-<时间戳>/``。

    四字段 ``退位信息.toml`` 由 6b ``retire_asset_to_backup`` 补齐。
    """
    destination = _allocate_backup_destination(current, old.name)
    os.replace(old, destination)
    return destination


