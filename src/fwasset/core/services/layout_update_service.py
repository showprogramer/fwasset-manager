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

import hashlib
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fwasset.core.admission import AdmissionError, validate_new_path
from fwasset.core.asset_index import AssetIndexError
from fwasset.core.asset_info import (
    INTENDED_FIRMWARE_TYPE_KEY,
    _save_keys,
    _toml_str,
    load_asset_info_with_status,
    save_candidate_metadata,
    vendor_from_asset_info,
)
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.file_scan import scan_firmware_subtree
from fwasset.core.import_io import (
    AssetImportError,
    promote_import,
    stage_import_directory,
)
from fwasset.core.managed_paths import (
    RETIRED_METADATA_FILENAME,
    RETIRED_VERSIONS_DIRNAME,
    managed_root,
    should_exclude_managed_path,
)
from fwasset.core.manifest import (
    ManifestError,
    directory_manifest_hash,
    manifest_hash,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    is_within_boundary,
    normalize_workspace_path,
    same_path_identity,
)
from fwasset.core.quarantine import QuarantineError, register_retire
from fwasset.core.reference_lookup import (
    _validate_target_kind,
    check_reference_gate,
    find_references_to,
)
from fwasset.core.services.reference_service import (
    RewritePlan,
    _derive_semantics,
    apply_rewrite_plan,
    build_clear_defaults_plan,
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
    ClearDefaultsKind,
    FirmwareAsset,
    ReferenceSemantics,
    RetiredBy,
    RetiredVersionMetadata,
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
    "RetireBackupError",
    "change_asset_semantics",
    "normalize_module_leaf",
    "restore_retired_version",
    "resume_change_asset_semantics",
    "resume_restore_retired_version",
    "retire_asset_to_backup",
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
            retired = {
                "backup": str(
                    retire_asset_to_backup(
                        transaction,
                        ws,
                        old,
                        target,
                        retired_by="update_asset",
                    )
                )
            }
    except (QuarantineError, ManifestError, OSError, RetireBackupError) as exc:
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


class RetireBackupError(Exception):
    """D4.2 退位失败；``payload`` 可携带 temp 路径等恢复材料。"""

    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_tree_bytes(source: Path) -> int:
    total = 0
    for path in source.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _insufficient_space(workspace: Path, needed: int) -> bool:
    try:
        return shutil.disk_usage(workspace).free < needed
    except OSError:
        return False


def _resolve_vendor(vendor: str, old: Path) -> str:
    if vendor.strip():
        return vendor.strip()
    data, status, _error = load_asset_info_with_status(old)
    if status == "ok":
        return vendor_from_asset_info(data)
    return ""


def _expected_change_kind(old: Path, new_path: Path, ws: Path) -> ClearDefaultsKind | None:
    old_sem = _derive_semantics(old, ws)
    new_sem = _derive_semantics(new_path, ws)
    if old_sem is None or new_sem is None:
        return None
    if old_sem["model_id"] != new_sem["model_id"]:
        return None
    if old_sem["module_key"] != new_sem["module_key"]:
        return "change_type"
    old_custom = bool(old_sem["scheme_name"])
    new_custom = bool(new_sem["scheme_name"])
    if not old_custom and new_custom:
        return "general_to_custom"
    if old_custom and not new_custom:
        return "custom_to_general"
    if old_custom and new_custom and old_sem["scheme_name"] != new_sem["scheme_name"]:
        return "custom_scheme_move"
    return None


def _looks_like_handcontrol(filenames: list[str]) -> bool:
    from fwasset.core.settings import SCAN_PKG_EXTENSIONS, SCAN_ROM_EXTENSIONS

    lower = [name.lower() for name in filenames]
    has_rom = any(name.endswith(tuple(SCAN_ROM_EXTENSIONS)) for name in lower)
    has_pkg = any(name.endswith(tuple(SCAN_PKG_EXTENSIONS)) for name in lower)
    return has_rom or has_pkg


def _write_retired_metadata(directory: Path, meta: RetiredVersionMetadata) -> None:
    text = (
        f"retired_from = {_toml_str(meta['retired_from'])}\n"
        f"content_hash = {_toml_str(meta['content_hash'])}\n"
        f"retired_at = {_toml_str(meta['retired_at'])}\n"
        f"retired_by = {_toml_str(meta['retired_by'])}\n"
    )
    (directory / RETIRED_METADATA_FILENAME).write_text(text, encoding="utf-8")


def _load_retired_toml(directory: Path) -> dict[str, Any] | None:
    path = directory / RETIRED_METADATA_FILENAME
    if not path.is_file():
        return None
    try:
        import tomllib

        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _retired_from_identity(data: dict[str, Any]) -> str:
    value = data.get("retired_from")
    if isinstance(value, str) and value.strip():
        return value.strip()
    alias = data.get("original_path")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    return ""


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


def retire_asset_to_backup(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    old_path: str | Path,
    current_asset_path: str | Path,
    *,
    retired_by: RetiredBy,
) -> Path:
    """D4.2 唯一 backup 写入口。返回正式副本目录。须已持写锁且已 begin_product_write。"""
    if not transaction.is_active:
        raise RetireBackupError("事务尚未开始或已经完成，无法退位到备用副本")
    if transaction.status.generation % 2 == 0:
        raise RetireBackupError("尚未 begin_product_write，禁止写入备用副本")
    ws = Path(workspace_root)
    # 6B-IMP-005：backup 是唯一写入口，先做完整边界守卫再动盘——old/current
    # 必须落在本事务工作区内、current 是现存合法活动 asset，否则拒绝写入。
    if normalize_workspace_path(ws) != normalize_workspace_path(transaction.workspace_root):
        raise RetireBackupError("backup 写入口的工作区与事务所属工作区不一致，已拒绝")
    try:
        old = assert_within_workspace(Path(old_path), ws)
        current = assert_within_workspace(Path(current_asset_path), ws)
    except PathGuardError as exc:
        raise RetireBackupError(f"退位路径越界：{exc}") from exc
    if should_exclude_managed_path(old, is_dir=True, workspace_root=ws):
        raise RetireBackupError(f"旧程序位于受管排除区，已拒绝退位：{old}")
    if should_exclude_managed_path(current, is_dir=True, workspace_root=ws):
        raise RetireBackupError(f"当前程序位于受管排除区，已拒绝退位：{current}")
    if not old.is_dir():
        raise RetireBackupError(f"旧程序目录不存在：{old}")
    if not current.is_dir():
        raise RetireBackupError(f"当前程序目录不存在：{current}")
    kind_detail = _validate_target_kind(current, "asset")
    if kind_detail is not None:
        raise RetireBackupError(f"当前程序不是合法活动资产：{kind_detail}")
    if same_path_identity(old, current) or is_within_boundary(current, old):
        raise RetireBackupError("不能把旧程序放进它自己的旧版本目录")

    identity = str(old)
    staging = allocate_staging_area(ws, transaction)
    # 6B-IMP-001：在任何搬动**之前**持久化预期 temp 身份与阶段；进程若死在
    # ``os.replace(old, staging)`` 与 ``set_phase`` 之间，resume 仍能发现 temp。
    transaction.set_phase(
        "retire_temp",
        details={"retire_temp": str(staging), "retire_old_path": identity},
    )
    moved = False
    try:
        staging.rmdir()
        os.replace(old, staging)
        moved = True
        content_hash = directory_manifest_hash(
            staging, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
        )
        meta: RetiredVersionMetadata = {
            "retired_from": identity,
            "content_hash": content_hash,
            "retired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "retired_by": retired_by,
        }
        _write_retired_metadata(staging, meta)
        loaded = _load_retired_toml(staging)
        if (
            loaded is None
            or _retired_from_identity(loaded) != identity
            or str(loaded.get("content_hash") or "") != content_hash
        ):
            raise RetireBackupError("退位元数据校验失败")
        transaction.set_phase(
            "retire_metadata",
            details={"retire_temp": str(staging)},
        )
        destination = _allocate_backup_destination(current, old.name)
        # 推进前持久化预期正式副本路径；死在 replace 与 return 之间时
        # resume 据 destination 判定已完成，不会重复推进或误归位。
        transaction.set_phase(
            "retire_official",
            details={"retire_destination": str(destination)},
        )
        os.replace(staging, destination)
        return destination
    except Exception as exc:
        if moved and staging.exists() and not old.exists():
            try:
                os.replace(staging, old)
                transaction.set_phase(
                    "retire_restored",
                    details={"retire_temp": str(staging)},
                )
            except OSError:
                raise RetireBackupError(
                    f"退位失败且无法把临时目录移回原处：{exc}",
                    payload={"temp": str(staging)},
                ) from exc
        if isinstance(exc, RetireBackupError):
            raise
        raise RetireBackupError(str(exc)) from exc


def _plan_freeze(plan: RewritePlan) -> list[dict[str, str]]:
    frozen: list[dict[str, str]] = []
    for item in plan.files:
        original = "" if item.original_bytes is None else item.original_bytes.decode(
            "utf-8", errors="replace"
        )
        frozen.append(
            {
                "path": str(item.pre_path),
                "original_sha256": item.original_sha256,
                "original_text": original,
                "new_sha256": item.new_sha256,
            }
        )
    return frozen


def _cas_restore_defaults(frozen: list[dict[str, str]]) -> bool:
    """仅当当前字节仍等于 postimage 时写回 preimage。冲突返回 False。"""
    for item in frozen:
        path = Path(item["path"])
        if not path.exists():
            return False
        current = path.read_bytes()
        if _sha256_bytes(current) != item["new_sha256"]:
            return False
        path.write_text(item["original_text"], encoding="utf-8")
    return True


def _rmdir_empty(path: Path) -> bool:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            return True
    except OSError:
        return False
    return False


def _promote_incomplete_candidate(
    transaction: WorkspaceTransaction,
    ws: Path,
    session: Path,
    *,
    vendor: str,
    intended_firmware_type: str,
) -> ServiceResult:
    from fwasset.core.services.asset_service import _promote_staging_to_candidate

    candidate_id = f"{uuid.uuid4().hex[:8]}-{session.name[:8]}"
    candidate_path = managed_root(ws, "incomplete_candidate") / candidate_id
    try:
        _promote_staging_to_candidate(transaction, ws, session, candidate_path)
    except StagingError as exc:
        if session.is_dir():
            cleanup_staging_area(ws, session)
            transaction.commit()
            return _error("promote_failed", str(exc))
        return _keep_scene(
            transaction,
            "promote_failed",
            f"候选区提升失败且内容已移动，需人工恢复：{exc}",
            {"candidate": str(candidate_path)},
        )

    # 6B-IMP-004：内容已落候选区，先持久化身份与元数据计划；崩溃 resume
    # 据此完成元数据写入或报 recovery_required，不能静默 clean。
    transaction.set_phase(
        "candidate_metadata",
        details={
            "candidate": str(candidate_path),
            "candidate_vendor": str(vendor),
            "candidate_intended": str(intended_firmware_type),
        },
    )
    status, error = save_candidate_metadata(
        candidate_path,
        vendor=vendor,
        intended_firmware_type=intended_firmware_type,
    )
    if status != "ok":
        return _keep_scene(
            transaction,
            "promote_failed",
            f"内容已存入候选区但元数据写入失败：{error}",
            {"candidate": str(candidate_path)},
        )
    # 6B-IMP-004：save_candidate_metadata 对空 intended 省略该键；规格 6B-006
    # 要求「无法判断时写空串」。补写与元数据写入同属一个崩溃窗口，先持久化
    # 阶段再落盘，resume 才能幂等收尾。
    data, load_status, _load_error = load_asset_info_with_status(candidate_path)
    if load_status == "ok" and INTENDED_FIRMWARE_TYPE_KEY not in data:
        transaction.set_phase(
            "candidate_intended_key",
            details={"candidate": str(candidate_path)},
        )
        # 6B-IMP-004：补写结果必须检查；parse_error / parser_missing 等任一
        # 非 ok 都收敛为 recovery_required，不能带着缺键的候选 commit clean。
        key_status, key_error = _save_keys(
            candidate_path, {INTENDED_FIRMWARE_TYPE_KEY: intended_firmware_type}
        )
        if key_status != "ok":
            return _keep_scene(
                transaction,
                "candidate_metadata_failed",
                f"候选区补写 intended_firmware_type 失败：{key_error}",
                {"candidate": str(candidate_path)},
            )
    transaction.set_phase("candidate_done", details={"candidate": str(candidate_path)})
    transaction.commit()
    return _ok(
        "created_incomplete",
        "内容不完整，已存入待补齐候选区",
        {"candidate": str(candidate_path), "candidate_id": candidate_id},
    )


def change_asset_semantics(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_asset: str | Path,
    source: str | Path,
    new_path: str | Path,
    change_kind: ClearDefaultsKind,
    *,
    retire_mode: RetireMode,
    vendor: str = "",
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """改类型 / 改范围：清 defaults、提升 replacement、再按 retire_mode 退位。"""
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    if retire_mode not in ("retire_to_trash", "retire_to_backup"):
        return _error("invalid_args", f"未知的退位方式：{retire_mode}")
    if change_kind not in (
        "change_type",
        "general_to_custom",
        "custom_to_general",
        "custom_scheme_move",
    ):
        return _error("invalid_args", f"未知的语义变化种类：{change_kind}")

    try:
        old = assert_within_workspace(Path(old_asset), ws)
        destination = assert_within_workspace(Path(new_path), ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"路径不在工作区内：{exc}")

    src = Path(source)
    if not src.is_dir():
        return _error("invalid_args", f"来源不是目录：{src}")
    if should_exclude_managed_path(src, is_dir=True, workspace_root=ws):
        return _error("path_excluded", f"来源位于受管排除区：{src}")
    if not old.is_dir():
        return _error("invalid_target", "旧程序目录不存在或不是目录")
    if same_path_identity(old, destination):
        return _error("invalid_args", "新路径与旧程序是同一身份")
    if _is_unnormalized_module_leaf(old):
        return _error(
            "normalize_required",
            "该程序仍是未归一的模块目录，请先执行布局归一再改类型或改范围",
            {"module": str(old)},
        )
    kind_detail = _validate_target_kind(old, "asset")
    if kind_detail is not None:
        return _error("invalid_target", f"旧程序不是合法程序目录：{kind_detail}")

    derived = _expected_change_kind(old, destination, ws)
    if derived != change_kind:
        return _error(
            "change_kind_mismatch",
            "声明的语义变化种类与新旧路径派生结果不符",
            {"expected": derived or "", "got": change_kind},
        )
    if _insufficient_space(ws, _source_tree_bytes(src)):
        return _error("insufficient_space", "工作区所在卷可用空间不足")

    lookup = find_references_to(configured_root, ws, old, "asset")
    if not lookup["ok"]:
        return lookup

    try:
        with WorkspaceTransaction(ws, operation="change_asset_semantics") as transaction:
            return _run_change(
                transaction,
                ws,
                configured_root,
                old,
                src,
                destination,
                change_kind,
                retire_mode,
                vendor,
                log_fn,
            )
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _run_change(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    old: Path,
    src: Path,
    new_path: Path,
    change_kind: ClearDefaultsKind,
    retire_mode: RetireMode,
    vendor: str,
    log_fn: Callable[..., None],
) -> ServiceResult:
    transaction.set_phase("revalidate")
    old_scanned, reason = _scan_single_asset(ws, old)
    if old_scanned is None:
        transaction.commit()
        return _error("invalid_target", f"锁内重验失败：{reason}")
    if _expected_change_kind(old, new_path, ws) != change_kind:
        transaction.commit()
        return _error("change_kind_mismatch", "锁内重验：语义变化种类已与路径不符")
    if _insufficient_space(ws, _source_tree_bytes(src)):
        transaction.commit()
        return _error("insufficient_space", "工作区所在卷可用空间不足")
    lookup = find_references_to(configured_root, ws, old, "asset")
    if not lookup["ok"]:
        transaction.commit()
        return lookup

    transaction.set_phase("freeze_clear_plan", details={
        "change_kind": change_kind,
        "old_path": str(old),
        "new_path": str(new_path),
        "retire_mode": retire_mode,
    })
    plan_result = build_clear_defaults_plan(
        configured_root, ws, old, change_kind, log_fn=log_fn
    )
    if not plan_result["ok"]:
        transaction.commit()
        return plan_result
    plan: RewritePlan = plan_result["payload"]["plan"]
    frozen = _plan_freeze(plan)
    transaction.set_phase(
        "freeze_clear_plan",
        details={"plan_files": frozen},
    )

    resolved_vendor = _resolve_vendor(vendor, old)
    parent_existed = new_path.parent.is_dir()
    transaction.set_phase("stage_import", details={"source": str(src)})
    transaction.begin_product_write()
    try:
        staged = stage_import_directory(transaction, ws, src)
    except (AssetImportError, StagingError, OSError) as exc:
        transaction.commit()
        return _error("incomplete_replacement", f"来源导入失败：{exc}")
    session = Path(staged["session"])
    transaction.set_phase("stage_import", details={"session": str(session), "new_path": str(new_path)})

    if not _staging_is_complete_asset(ws, session, new_path):
        filenames = [f.name for f in session.iterdir() if f.is_file()]
        intended = "handcontrol_ui" if _looks_like_handcontrol(filenames) else ""
        return _promote_incomplete_candidate(
            transaction,
            ws,
            session,
            vendor=resolved_vendor,
            intended_firmware_type=intended,
        )

    transaction.set_phase("promote_replacement", details={"target": str(new_path)})
    try:
        promote_import(transaction, ws, str(configured_root or ""), session, new_path)
    except AdmissionError as exc:
        cleanup_staging_area(ws, session)
        if not parent_existed:
            _rmdir_empty(new_path.parent)
            if new_path.parent.is_dir() and any(new_path.parent.iterdir()):
                return _keep_scene(
                    transaction,
                    "change_inconsistent",
                    "新程序提升失败且模块容器非空，已保留现场",
                    {"target": str(new_path)},
                )
        transaction.commit()
        return _error(exc.code, exc.message, exc.payload)
    except (AssetImportError, StagingError, OSError) as exc:
        if session.is_dir():
            cleanup_staging_area(ws, session)
        if not parent_existed:
            _rmdir_empty(new_path.parent)
            if new_path.parent.is_dir() and any(new_path.parent.iterdir()):
                return _keep_scene(
                    transaction,
                    "change_inconsistent",
                    "新程序提升失败且模块容器非空，已保留现场",
                    {"target": str(new_path)},
                )
        if new_path.exists():
            return _cleanup_product(
                transaction, ws, new_path, "promote_failed", f"新程序提升失败：{exc}"
            )
        transaction.commit()
        return _error("promote_failed", f"新程序提升失败：{exc}")

    # 6B-IMP-006：promote_import 只记录缺失父目录的占位产物，不为 destination
    # 本身记录 manifest；补记提升时 manifest，供续跑按 §9 验证产物完整性。
    try:
        new_manifest = directory_manifest_hash(new_path)
    except (ManifestError, OSError) as exc:
        return _cleanup_product(
            transaction, ws, new_path, "promote_failed", f"新程序 manifest 失败：{exc}"
        )
    transaction.record_product(new_path, new_manifest)

    transaction.set_phase("apply_clear_defaults")
    apply_result = apply_rewrite_plan(plan, configured_root, log_fn)
    if not apply_result["ok"]:
        if apply_result["code"] == "rollback_conflict":
            return _keep_scene(
                transaction,
                "change_inconsistent",
                "清理平台默认失败且回滚存在冲突，已保留现场",
                apply_result["payload"],
            )
        return _keep_scene(
            transaction,
            "config_rewrite_failed",
            f"清理平台默认失败，新程序已在、旧程序未退位：{apply_result['message']}",
            apply_result["payload"],
        )

    # 6B-IMP-003：defaults 清理已成功，持久化独立阶段；崩溃续跑见到它即
    # 跳过重建/应用，直接退位，避免重复改写或误回滚。
    transaction.set_phase("change_apply_done")

    transaction.set_phase("retire_old", details={"retire_mode": retire_mode})
    try:
        if retire_mode == "retire_to_trash":
            record = register_retire(ws, old)
            retired: dict[str, Any] = {"quarantine_id": record["id"]}
        else:
            retired = {
                "backup": str(
                    retire_asset_to_backup(
                        transaction,
                        ws,
                        old,
                        new_path,
                        retired_by="change_asset_semantics",
                    )
                )
            }
    except (QuarantineError, ManifestError, OSError, RetireBackupError) as exc:
        if frozen and not _cas_restore_defaults(frozen):
            return _keep_scene(
                transaction,
                "retire_failed_with_config_conflict",
                f"旧程序退位失败，且平台默认无法安全恢复：{exc}",
                {"old": str(old), "replacement": str(new_path)},
            )
        return _keep_scene(
            transaction,
            "retire_failed",
            f"新程序已上岗，但旧程序退位失败，两份暂时并存：{exc}",
            {"old": str(old), "replacement": str(new_path)},
        )

    transaction.set_phase("reconcile")
    payload = {"old": str(old), "replacement": str(new_path), **retired}
    try:
        ancestor = old.parent if old.parent.exists() else new_path.parent
        if new_path.parent.exists():
            ancestor = new_path.parent
        reconcile_subtree(str(ws), str(ancestor))
    except (AssetIndexError, OSError) as exc:
        transaction.commit()
        log_fn(f"改类型后索引对账失败，需重新读取程序列表：{exc}")
        return _ok(
            "reindex_failed",
            f"已改到「{new_path.name}」，但索引对账失败，请重新扫描程序列表",
            {**payload, "detail": str(exc)},
        )

    transaction.commit()
    log_fn(f"语义变化完成：{old} → {new_path}")
    return _ok("ok", f"已改到「{new_path.name}」", payload)


def _restore_admission(
    configured_root: str | Path | None,
    ws: Path,
    retired_from: Path,
) -> ServiceResult | None:
    try:
        validate_new_path(
            retired_from,
            kind="asset",
            configured_root=str(configured_root or ""),
            workspace_root=ws,
        )
    except AdmissionError as exc:
        if exc.code == "path_identity_conflict":
            return None
        return _error(exc.code, exc.message, exc.payload)
    return None


def restore_retired_version(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    backup_path: str | Path,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """把 ``旧版本/`` 副本恢复到 ``retired_from``，并与当前程序做 R8 交换。"""
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    try:
        backup = assert_within_workspace(Path(backup_path), ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"副本不在工作区内：{exc}")
    if not backup.is_dir():
        return _error("invalid_target", f"备用副本不存在：{backup}")

    data = _load_retired_toml(backup)
    if data is None:
        return _error("retired_metadata_invalid", "退位信息无法解析")
    identity = _retired_from_identity(data)
    content_hash = data.get("content_hash")
    if not identity or not isinstance(content_hash, str) or not content_hash.strip():
        return _error("retired_metadata_invalid", "退位信息缺少身份或内容哈希")
    preview_identity = identity

    try:
        with WorkspaceTransaction(ws, operation="restore_retired_version") as transaction:
            return _run_restore(
                transaction,
                ws,
                configured_root,
                backup,
                preview_identity,
                content_hash.strip(),
                log_fn,
            )
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _run_restore(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    backup: Path,
    preview_identity: str,
    preview_hash: str,
    log_fn: Callable[..., None],
) -> ServiceResult:
    data = _load_retired_toml(backup)
    if data is None:
        transaction.commit()
        return _error("retired_metadata_invalid", "锁内重验：退位信息无法解析")
    identity = _retired_from_identity(data)
    if not identity or identity != preview_identity:
        transaction.commit()
        return _error("retired_metadata_invalid", "锁内重验：retired_from 已变化或为空")
    content_hash = data.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash.strip():
        transaction.commit()
        return _error("retired_metadata_invalid", "锁内重验：缺少 content_hash")
    actual_hash = directory_manifest_hash(
        backup, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
    )
    if actual_hash != preview_hash or actual_hash != content_hash.strip():
        transaction.commit()
        return _error("content_hash_mismatch", "备用副本内容与退位哈希不一致")
    if not backup.is_dir():
        transaction.commit()
        return _error("invalid_target", "备用副本已不在原路径")

    retired_from = Path(identity)
    try:
        retired_from = assert_within_workspace(retired_from, ws)
    except PathGuardError as exc:
        transaction.commit()
        return _error("out_of_workspace", f"恢复目标不在工作区内：{exc}")
    if retired_from.exists() or retired_from.is_symlink():
        transaction.commit()
        return _error("path_exists", f"恢复目标已存在：{retired_from}")
    admission = _restore_admission(configured_root, ws, retired_from)
    if admission is not None:
        transaction.commit()
        return admission

    current = backup.parent.parent
    if not current.is_dir() or same_path_identity(current, retired_from):
        transaction.commit()
        return _error("invalid_target", "无法确定当前程序目录")
    current_asset, reason = _scan_single_asset(ws, current)
    if current_asset is None:
        transaction.commit()
        return _error("invalid_target", f"当前程序不是唯一完整资产：{reason}")
    old_sem = _derive_semantics(current, ws)
    new_sem = _derive_semantics(retired_from, ws)
    if (
        old_sem is None
        or new_sem is None
        or old_sem["model_id"] != new_sem["model_id"]
        or old_sem["module_key"] != new_sem["module_key"]
        or old_sem["scheme_name"] != new_sem["scheme_name"]
    ):
        transaction.commit()
        return _error(
            "unsupported_semantic_change",
            "当前程序与备用副本语义不同，不能经恢复交换改类型",
        )

    transaction.set_phase(
        "promote_restored",
        details={
            "backup_path": str(backup),
            "retired_from": str(retired_from),
            "current_path": str(current),
        },
    )
    transaction.begin_product_write()
    staging = allocate_staging_area(ws, transaction)
    # 6B-IMP-001②：在任何搬动**之前**持久化预期 staging 身份与三路径；进程若死
    # 在两次 ``os.replace`` 之间，resume 仍能定位 temp 并归位/推进。
    transaction.set_phase(
        "restore_staging",
        details={
            "restore_temp": str(staging),
            "backup_path": str(backup),
            "retired_from": str(retired_from),
            "current_path": str(current),
        },
    )
    try:
        staging.rmdir()
        os.replace(backup, staging)
        retired_from.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, retired_from)
    except OSError as exc:
        if staging.exists() and not backup.exists():
            try:
                os.replace(staging, backup)
            except OSError:
                return _keep_scene(
                    transaction,
                    "restore_inconsistent",
                    f"副本提升失败且无法移回旧版本：{exc}",
                    {"temp": str(staging), "backup": str(backup)},
                )
        transaction.commit()
        return _error("promote_failed", f"副本提升失败：{exc}")

    try:
        manifest = directory_manifest_hash(retired_from)
    except (ManifestError, OSError) as exc:
        return _keep_scene(
            transaction,
            "restore_inconsistent",
            f"恢复目录无法计算 manifest：{exc}",
            {"restored": str(retired_from)},
        )
    transaction.record_product(retired_from, manifest)
    transaction.set_phase(
        "restore_promoted",
        details={"restore_temp": str(staging), "backup_path": str(backup)},
    )

    old_semantics = _semantics_of(ws, current)
    new_semantics = _semantics_of(ws, retired_from)
    if old_semantics is None or new_semantics is None:
        moved_back = _move_back_to_backup(retired_from, backup)
        if not moved_back:
            return _keep_scene(
                transaction,
                "restore_inconsistent",
                "无法派生语义且副本无法移回旧版本",
                {"restored": str(retired_from), "backup": str(backup)},
            )
        transaction.commit()
        return _error("plan_build_failed", "无法派生新旧程序归属")

    transaction.set_phase("apply_update")
    plan_result = build_rewrite_plan(
        configured_root,
        ws,
        RewriteRequest(
            operation="update",
            target_kind="asset",
            old_path=str(current),
            replacement_path=str(retired_from),
            old_semantics=old_semantics,
            new_semantics=new_semantics,
        ),
        log_fn,
    )
    if not plan_result["ok"]:
        if not _move_back_to_backup(retired_from, backup):
            return _keep_scene(
                transaction,
                "restore_inconsistent",
                "计划构建失败且副本无法移回旧版本",
                {"restored": str(retired_from), "backup": str(backup)},
            )
        transaction.commit()
        return _error(
            "plan_build_failed",
            f"引用改写计划构建失败：{plan_result['message']}",
            plan_result["payload"],
        )

    apply_result = apply_rewrite_plan(plan_result["payload"]["plan"], configured_root, log_fn)
    if not apply_result["ok"]:
        if apply_result["code"] == "rollback_conflict":
            return _keep_scene(
                transaction,
                "restore_inconsistent",
                "引用改写失败且回滚存在冲突，已保留现场",
                apply_result["payload"],
            )
        if apply_result["code"] == "rolled_back":
            if not _move_back_to_backup(retired_from, backup):
                return _keep_scene(
                    transaction,
                    "restore_inconsistent",
                    "引用改写已回滚但副本无法移回旧版本",
                    {"restored": str(retired_from), "backup": str(backup)},
                )
            transaction.commit()
            return _error(
                "config_rewrite_failed",
                f"引用改写失败，已恢复原状：{apply_result['message']}",
                apply_result["payload"],
            )
        if not _move_back_to_backup(retired_from, backup):
            return _keep_scene(
                transaction,
                "restore_inconsistent",
                "引用改写失败且副本无法移回旧版本",
                {"restored": str(retired_from), "backup": str(backup)},
            )
        transaction.commit()
        return _error(
            apply_result["code"],
            apply_result["message"],
            apply_result["payload"],
        )

    # 6B-IMP-003：apply 已成功，立即把「已完成」持久化为独立阶段；后续崩溃
    # resume 见到该阶段即知引用已指向 restored，绝不能再把副本移回 backup。
    transaction.set_phase("restore_apply_done", details={"backup_path": str(backup)})

    transaction.set_phase("retire_current")
    try:
        retired = retire_asset_to_backup(
            transaction,
            ws,
            current,
            retired_from,
            retired_by="restore_retired_version",
        )
    except (ManifestError, OSError, RetireBackupError) as exc:
        return _keep_scene(
            transaction,
            "retire_failed",
            f"已恢复旧程序，但当前程序退位失败，两份暂时并存：{exc}",
            {"restored": str(retired_from), "current": str(current)},
        )
    # 6B-IMP-013：持久化本操作退位目的地，供 resume 判定是否已退位。
    transaction.set_phase("retire_done", details={"retire_destination": str(retired)})

    transaction.set_phase("reconcile")
    payload = {"restored": str(retired_from), "retired": str(retired)}
    try:
        reconcile_subtree(str(ws), str(retired_from.parent))
    except (AssetIndexError, OSError) as exc:
        transaction.commit()
        log_fn(f"恢复后索引对账失败，需重新读取程序列表：{exc}")
        return _ok(
            "reindex_failed",
            "已恢复备用副本，但索引对账失败，请重新扫描程序列表",
            {**payload, "detail": str(exc)},
        )

    transaction.commit()
    log_fn(f"已恢复备用副本：{retired_from}")
    return _ok("ok", f"已恢复「{retired_from.name}」", payload)


def _move_back_to_backup(restored: Path, backup: Path) -> bool:
    if not restored.exists():
        return backup.exists()
    if backup.exists():
        return False
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        staging_parent = restored.parent
        os.replace(restored, backup)
        _rmdir_empty(staging_parent)
        return True
    except OSError:
        return False


def resume_change_asset_semantics(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """按操作日志续跑 ``change_asset_semantics``。"""
    ws = Path(workspace_root).resolve()
    try:
        with WorkspaceTransaction(
            ws, operation="change_asset_semantics", resume=True
        ) as transaction:
            return _resume_change(transaction, ws, configured_root, log_fn)
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _resume_change(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    log_fn: Callable[..., None],
) -> ServiceResult:
    details = transaction.log.get("details") or {}
    phase = str(transaction.log.get("phase") or "")
    session_raw = details.get("session")

    # 6B-IMP-004：候选区内容已移动但元数据可能未写完。按持久化阶段补写或
    # 收敛，绝不能把「磁盘已改、元数据缺失」的候选当成 clean 收尾。
    candidate_raw = details.get("candidate")
    if isinstance(candidate_raw, str) and candidate_raw.strip():
        candidate_path = Path(candidate_raw)
        if candidate_path.is_dir():
            return _resume_candidate_metadata(
                transaction, candidate_path, details, phase
            )
        # 候选目录已不存在（可能被人工清理）：无半成品可收尾，按 clean 结束。
        transaction.commit()
        return _ok("ok", "候选目录已不存在，续跑结束", {})

    # 6B-IMP-001：retire 临时目录仍持有旧副本（old 已空、backup 未生成）。
    # 这是受管 staging 内的半成品，必须归位或推进，不能静默 clean。
    retire_temp_raw = details.get("retire_temp")
    retire_old_raw = details.get("retire_old_path")
    if (
        isinstance(retire_temp_raw, str)
        and retire_temp_raw.strip()
        and isinstance(retire_old_raw, str)
        and retire_old_raw.strip()
    ):
        retire_temp = Path(retire_temp_raw)
        retire_old = Path(retire_old_raw)
        if retire_temp.is_dir():
            # 原路径已被占用（外部写入）：不能覆盖，保留现场等待人工处理。
            if retire_old.exists():
                return _keep_scene(
                    transaction,
                    "retire_inconsistent",
                    "续跑发现退位临时目录存在但原路径已被占用，已保留现场",
                    {"temp": str(retire_temp), "old": str(retire_old)},
                )
            # retire_official 已持久化正式副本路径：temp 仍在说明 replace 未
            # 完成或失败，按已记录 destination 幂等推进。
            destination_raw = details.get("retire_destination")
            new_path_probe = Path(str(details.get("new_path") or ""))
            if isinstance(destination_raw, str) and destination_raw.strip():
                destination = Path(destination_raw)
                if destination.is_dir():
                    # 已推进成功（可能死在 replace 与 return 之间）：清理 temp。
                    try:
                        if retire_temp.is_dir():
                            cleanup_staging_area(ws, retire_temp)
                    except StagingError:
                        pass
                    # 6B-IMP-007：旧已退位、尚未对账的崩溃点，必须补对账。
                    try:
                        reconcile_subtree(str(ws), str(new_path_probe.parent))
                    except (AssetIndexError, OSError) as exc:
                        transaction.commit()
                        return _ok(
                            "reindex_failed",
                            f"续跑退位后索引对账失败：{exc}",
                            {"replacement": str(new_path_probe), "backup": str(destination)},
                        )
                    transaction.commit()
                    return _ok(
                        "ok",
                        "已续跑完成退位（备用副本）",
                        {
                            "replacement": str(new_path_probe) if new_path_probe.is_dir() else "",
                            "backup": str(destination),
                        },
                    )
                # destination 不在：从 temp 幂等推进。
                transaction.set_phase(
                    "retire_promote",
                    details={
                        "retire_temp": str(retire_temp),
                        "new_path": str(new_path_probe),
                    },
                )
                transaction.begin_product_write()
                try:
                    retire_temp.rename(destination)
                except OSError as exc:
                    return _keep_scene(
                        transaction,
                        "retire_failed",
                        f"续跑推进退位临时目录失败：{exc}",
                        {"temp": str(retire_temp), "replacement": str(new_path_probe)},
                    )
                # 6B-IMP-007：推进成功后必须对账。
                try:
                    reconcile_subtree(str(ws), str(new_path_probe.parent))
                except (AssetIndexError, OSError) as exc:
                    transaction.commit()
                    return _ok(
                        "reindex_failed",
                        f"续跑退位后索引对账失败：{exc}",
                        {"replacement": str(new_path_probe), "backup": str(destination)},
                    )
                transaction.commit()
                return _ok(
                    "ok",
                    "已续跑完成退位（备用副本）",
                    {"replacement": str(new_path_probe), "backup": str(destination)},
                )
            if new_path_probe.is_dir():
                # replacement 在位：把 temp 直接推进为正式 backup（幂等）。
                # 先补写持久化阶段再进产品写入，resume 可重入。
                transaction.set_phase(
                    "retire_promote",
                    details={
                        "retire_temp": str(retire_temp),
                        "new_path": str(new_path_probe),
                    },
                )
                transaction.begin_product_write()
                content_hash = directory_manifest_hash(
                    retire_temp,
                    exclude_names=frozenset({RETIRED_METADATA_FILENAME}),
                )
                meta: RetiredVersionMetadata = {
                    "retired_from": str(retire_old),
                    "content_hash": content_hash,
                    "retired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "retired_by": "change_asset_semantics",
                }
                _write_retired_metadata(retire_temp, meta)
                loaded = _load_retired_toml(retire_temp)
                if (
                    loaded is None
                    or _retired_from_identity(loaded) != str(retire_old)
                    or str(loaded.get("content_hash") or "") != content_hash
                ):
                    return _keep_scene(
                        transaction,
                        "retire_failed",
                        "续跑写退位元数据校验失败",
                        {"temp": str(retire_temp)},
                    )
                destination = _allocate_backup_destination(
                    new_path_probe, retire_old.name
                )
                # 6B-IMP-001：推进前持久化预期正式副本路径。
                transaction.set_phase(
                    "retire_official",
                    details={"retire_destination": str(destination)},
                )
                try:
                    os.replace(retire_temp, destination)
                except OSError as exc:
                    return _keep_scene(
                        transaction,
                        "retire_failed",
                        f"续跑推进退位临时目录失败：{exc}",
                        {"temp": str(retire_temp), "replacement": str(new_path_probe)},
                    )
                # 6B-IMP-007：推进成功正是旧已退位、尚未对账的崩溃点，
                # 必须补对账；失败按 reindex_failed 以 ok 收尾（同 6a 语义）。
                try:
                    reconcile_subtree(str(ws), str(new_path_probe.parent))
                except (AssetIndexError, OSError) as exc:
                    transaction.commit()
                    return _ok(
                        "reindex_failed",
                        f"续跑退位后索引对账失败：{exc}",
                        {"replacement": str(new_path_probe), "backup": str(destination)},
                    )
                transaction.commit()
                return _ok(
                    "ok",
                    "已续跑完成退位（备用副本）",
                    {"replacement": str(new_path_probe), "backup": str(destination)},
                )
            # replacement 不在：未发生产品写入，把 temp 归位到原路径。
            try:
                os.replace(retire_temp, retire_old)
            except OSError as exc:
                transaction.begin_product_write()
                return _keep_scene(
                    transaction,
                    "retire_inconsistent",
                    f"续跑无法把退位临时目录归位：{exc}",
                    {"temp": str(retire_temp), "old": str(retire_old)},
                )
            transaction.commit()
            return _ok("ok", "已把退位临时目录归位，旧程序恢复原状", {})
        # temp 已不在：若已记录正式副本且其在，说明推进成功；否则无半成品。
        destination_raw = details.get("retire_destination")
        if isinstance(destination_raw, str) and destination_raw.strip():
            destination = Path(destination_raw)
            if destination.is_dir():
                # 6B-IMP-007：旧已退位、尚未对账的崩溃点，必须补对账。
                new_path_probe = Path(str(details.get("new_path") or ""))
                reconcile_root = (
                    new_path_probe.parent if new_path_probe.is_dir() else destination.parent
                )
                try:
                    reconcile_subtree(str(ws), str(reconcile_root))
                except (AssetIndexError, OSError) as exc:
                    transaction.commit()
                    return _ok(
                        "reindex_failed",
                        f"续跑退位后索引对账失败：{exc}",
                        {"backup": str(destination)},
                    )
                transaction.commit()
                return _ok(
                    "ok",
                    "已续跑完成退位（备用副本）",
                    {"backup": str(destination)},
                )
        transaction.commit()
        return _ok("ok", "退位临时目录已消失，续跑结束", {})

    if phase in {"revalidate", "freeze_clear_plan", "stage_import", "prepared", "writing"}:
        if isinstance(session_raw, str) and Path(session_raw).is_dir():
            cleanup_staging_area(ws, Path(session_raw))
        transaction.commit()
        return _ok("ok", "未完成的改类型操作已清理", {})

    old = Path(str(details.get("old_path") or ""))
    new_path = Path(str(details.get("new_path") or ""))
    retire_mode = str(details.get("retire_mode") or "retire_to_trash")
    change_kind = str(details.get("change_kind") or "change_type")
    frozen_raw = details.get("plan_files")
    frozen: list[dict[str, str]] = frozen_raw if isinstance(frozen_raw, list) else []

    if not new_path.is_dir():
        transaction.commit()
        return _ok("ok", "新程序不存在，已结束续跑", {})

    # 6B-IMP-006：续跑前按日志 manifest 验证本操作记录的产物；被第三方
    # 改动或与记录不符时保留 recovery_required，绝不继续清 defaults/退位。
    product_error = _verify_recorded_products(transaction)
    if product_error is not None:
        return product_error

    transaction.begin_product_write()

    # 6B-IMP-002/003：只有尚未确认 apply 成功的阶段才允许重建/应用 defaults
    # 计划；「change_apply_done」及之后说明 defaults 已清，跳过避免重复改写。
    defaults_pending = phase in {"promote_replacement", "apply_clear_defaults"}
    if old.is_dir() and defaults_pending:
        plan_result = build_clear_defaults_plan(
            configured_root, ws, old, change_kind, log_fn=log_fn  # type: ignore[arg-type]
        )
        # 6B-IMP-002：第三方改动平台 TOML 或扫描损坏时重建失败——停止退位，
        # 保留现场与恢复材料，不能留下指向已退位程序的 default。
        if not plan_result["ok"]:
            return _keep_scene(
                transaction,
                plan_result["code"],
                f"续跑重建默认清理计划失败：{plan_result['message']}",
                {"old": str(old), "replacement": str(new_path)},
            )
        apply_result = apply_rewrite_plan(
            plan_result["payload"]["plan"], configured_root, log_fn
        )
        if not apply_result["ok"]:
            return _keep_scene(
                transaction,
                "config_rewrite_failed",
                f"续跑应用默认清理失败：{apply_result['message']}",
                {"old": str(old), "replacement": str(new_path)},
            )
        transaction.set_phase("change_apply_done")

    if old.is_dir():
        try:
            if retire_mode == "retire_to_backup":
                retire_asset_to_backup(
                    transaction,
                    ws,
                    old,
                    new_path,
                    retired_by="change_asset_semantics",
                )
            else:
                register_retire(ws, old)
        except (QuarantineError, ManifestError, OSError, RetireBackupError) as exc:
            if frozen and not _cas_restore_defaults(frozen):
                return _keep_scene(
                    transaction,
                    "retire_failed_with_config_conflict",
                    f"续跑退位失败且平台默认无法恢复：{exc}",
                    {"old": str(old), "replacement": str(new_path)},
                )
            return _keep_scene(
                transaction,
                "retire_failed",
                f"续跑退位失败：{exc}",
                {"old": str(old), "replacement": str(new_path)},
            )

    try:
        reconcile_subtree(str(ws), str(new_path.parent))
    except (AssetIndexError, OSError) as exc:
        transaction.commit()
        return _ok("reindex_failed", f"续跑对账失败：{exc}", {"replacement": str(new_path)})
    transaction.commit()
    return _ok("ok", "已续跑完成改类型操作", {"replacement": str(new_path)})


def _resume_candidate_metadata(
    transaction: WorkspaceTransaction,
    candidate_path: Path,
    details: dict[str, Any],
    phase: str,
) -> ServiceResult:
    """6B-IMP-004：候选区续跑——补写缺失的候选期元数据键后 clean 收尾。"""
    if phase in {"candidate_metadata", "candidate_intended_key"}:
        vendor = str(details.get("candidate_vendor") or "")
        intended = str(details.get("candidate_intended") or "")
        data, load_status, load_error = load_asset_info_with_status(candidate_path)
        if load_status not in {"ok", "missing"}:
            return _keep_scene(
                transaction,
                "candidate_metadata_invalid",
                f"候选区续跑读取元数据失败：{load_error}",
                {"candidate": str(candidate_path)},
            )
        missing_vendor = load_status == "missing" or "vendor" not in data
        missing_import = load_status == "missing" or "import_state" not in data
        missing_intended = (
            load_status == "missing" or INTENDED_FIRMWARE_TYPE_KEY not in data
        )
        updates: dict[str, str | None] = {}
        if missing_vendor:
            updates["vendor"] = vendor
        if missing_import:
            updates["import_state"] = "incomplete"
        if missing_intended:
            updates[INTENDED_FIRMWARE_TYPE_KEY] = intended
        if updates:
            status, error = _save_keys(candidate_path, updates)
            if status != "ok":
                return _keep_scene(
                    transaction,
                    "candidate_metadata_failed",
                    f"候选区续跑补写元数据失败：{error}",
                    {"candidate": str(candidate_path)},
                )
    # candidate_done 或更早阶段但元数据已齐全：均可 clean 收尾。
    transaction.set_phase("candidate_done", details={"candidate": str(candidate_path)})
    transaction.commit()
    return _ok(
        "ok",
        "已续跑完成候选区操作",
        {"candidate": str(candidate_path)},
    )


def _verify_recorded_products(
    transaction: WorkspaceTransaction,
) -> ServiceResult | None:
    """6B-IMP-006：按日志 manifest 验证本操作记录的产物。

    规格 §9：产物 manifest 不等于日志即停止。只校验**非空 manifest** 的产物
    （空 manifest 是 promote_import 为缺失父目录创建的占位记录，目录内容
    由后续导入填充，不属于可校验的产物）。

    排除策略：本操作后续合法步骤可能在已记录产物之下新增内容（如 restore
    成功后把当前程序退位到 ``restored/旧版本/``），因此校验时排除产物内
    所有 ``旧版本`` 子目录下的文件——它们属本操作后续产物，不属第三方
    改动。任何其它不符都返回错误并保持 ``recovery_required``。
    """
    empty_manifest = manifest_hash([])
    for product in transaction.products:
        if product["manifest"] == empty_manifest:
            continue  # 占位记录，无可校验内容
        path = Path(product["path"])
        if not path.is_dir():
            return _keep_scene(
                transaction,
                "product_missing",
                f"续跑发现本操作产物已不存在：{path}",
                {"product": str(path)},
            )
        try:
            current_hash = _manifest_excluding_retired_versions(path)
        except (ManifestError, OSError) as exc:
            return _keep_scene(
                transaction,
                "product_unverifiable",
                f"续跑无法校验本操作产物：{path}（{exc}）",
                {"product": str(path)},
            )
        if current_hash != product["manifest"]:
            return _keep_scene(
                transaction,
                "product_modified",
                f"续跑发现本操作产物在崩溃后被改动：{path}",
                {
                    "product": str(path),
                    "expected": product["manifest"],
                    "actual": current_hash,
                },
            )
    return None


def _manifest_excluding_retired_versions(root: Path) -> str:
    """计算目录 manifest，跳过所有 ``旧版本`` 子目录（本操作后续产物）。"""
    from fwasset.core.manifest import directory_manifest, manifest_hash

    base = Path(root)
    if not base.is_dir():
        raise ManifestError(f"manifest 根不是目录：{base}")
    all_entries = directory_manifest(base)
    filtered = [
        entry
        for entry in all_entries
        if RETIRED_VERSIONS_DIRNAME not in Path(entry.relpath).parts
    ]
    return manifest_hash(filtered)


def _current_already_retired(retired_from: Path, current: Path) -> bool:
    """验证 ``current`` 是否已被本操作退位到 ``retired_from/旧版本/``。

    6B-IMP-013：``retired_from`` 路径相等不够。恢复后再更新同一版本名时，
    历史副本可以记录同一路径，而当前新程序仍在线。仍在线的 ``current``
    一律不算已退位（否则会跳过退位、留下两份活动程序）。当前已被搬走时，
    副本必须同时匹配路径身份与 ``content_hash``。
    """
    if current.is_dir() or current.is_symlink():
        return False
    retired_root = retired_from / RETIRED_VERSIONS_DIRNAME
    if not retired_root.is_dir():
        return False
    for child in retired_root.iterdir():
        if not child.is_dir():
            continue
        data = _load_retired_toml(child)
        if data is None:
            continue
        identity = _retired_from_identity(data)
        stored = data.get("content_hash")
        if (
            identity
            and same_path_identity(identity, current)
            and isinstance(stored, str)
            and stored.strip()
        ):
            return True
    return False


def resume_restore_retired_version(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """按操作日志续跑 ``restore_retired_version``。"""
    ws = Path(workspace_root).resolve()
    try:
        with WorkspaceTransaction(
            ws, operation="restore_retired_version", resume=True
        ) as transaction:
            return _resume_restore(transaction, ws, configured_root, log_fn)
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)


def _resume_restore(
    transaction: WorkspaceTransaction,
    ws: Path,
    configured_root: str | Path | None,
    log_fn: Callable[..., None],
) -> ServiceResult:
    details = transaction.log.get("details") or {}
    phase = str(transaction.log.get("phase") or "")
    backup = Path(str(details.get("backup_path") or ""))
    retired_from = Path(str(details.get("retired_from") or ""))
    current = Path(str(details.get("current_path") or ""))

    if phase in {"prepared", "writing"} or not retired_from:
        transaction.commit()
        return _ok("ok", "未搬动的恢复操作已清理", {})

    # 6B-IMP-001②/③/009：restore 在搬动前持久化了预期 staging（restore_temp）。
    # temp 仍持有副本时，先归位 backup，commit 并立即返回——绝不能落入后续
    # 分支把「已移回 backup」误报成「恢复成功」。
    restore_temp_raw = details.get("restore_temp")
    if isinstance(restore_temp_raw, str) and restore_temp_raw.strip():
        restore_temp = Path(restore_temp_raw)
        if restore_temp.is_dir():
            if not backup or not backup.parent.is_dir():
                return _keep_scene(
                    transaction,
                    "restore_inconsistent",
                    "续跑发现恢复临时目录但旧版本路径不可用，已保留现场",
                    {"temp": str(restore_temp)},
                )
            if backup.exists():
                # 同名 backup 已存在（外部写入）：不能覆盖，保留现场。
                return _keep_scene(
                    transaction,
                    "restore_inconsistent",
                    "续跑发现恢复临时目录但旧版本已存在，已保留现场",
                    {"temp": str(restore_temp), "backup": str(backup)},
                )
            try:
                os.replace(restore_temp, backup)
            except OSError as exc:
                transaction.begin_product_write()
                return _keep_scene(
                    transaction,
                    "restore_inconsistent",
                    f"续跑无法把恢复临时目录移回旧版本：{exc}",
                    {"temp": str(restore_temp), "backup": str(backup)},
                )
            transaction.commit()
            return _ok("ok", "已将未完成的恢复副本移回旧版本", {"backup": str(backup)})
        # temp 已不在：无需归位，继续既有分支。

    # 6B-IMP-001③：restore 内部调用 retire_asset_to_backup 后日志阶段会变
    # retire_temp；_resume_restore 必须能消费它，否则 current 被留在 staging。
    retire_temp_raw = details.get("retire_temp")
    retire_old_raw = details.get("retire_old_path")
    if (
        isinstance(retire_temp_raw, str)
        and retire_temp_raw.strip()
        and isinstance(retire_old_raw, str)
        and retire_old_raw.strip()
    ):
        retire_temp = Path(retire_temp_raw)
        retire_old = Path(retire_old_raw)
        if retire_temp.is_dir():
            if retire_old.exists():
                return _keep_scene(
                    transaction,
                    "retire_inconsistent",
                    "续跑发现退位临时目录存在但原路径已被占用，已保留现场",
                    {"temp": str(retire_temp), "old": str(retire_old)},
                )
                destination_raw = details.get("retire_destination")
                if isinstance(destination_raw, str) and destination_raw.strip():
                    destination = Path(destination_raw)
                    if destination.is_dir():
                        # 已推进成功：清理 temp 后按 restore_apply_done 收尾。
                        try:
                            cleanup_staging_area(ws, retire_temp)
                        except StagingError:
                            pass
                        retire_temp_resolved = destination
                    else:
                        transaction.begin_product_write()
                        if not retired_from.is_dir():
                            return _keep_scene(
                                transaction,
                                "retire_inconsistent",
                                "续跑推进退位缺少 restored 目录，已保留现场",
                                {"temp": str(retire_temp)},
                            )
                        try:
                            retire_temp.rename(destination)
                        except OSError as exc:
                            return _keep_scene(
                                transaction,
                                "retire_failed",
                                f"续跑推进退位临时目录失败：{exc}",
                                {"temp": str(retire_temp), "restored": str(retired_from)},
                            )
                        retire_temp_resolved = destination
            elif retired_from.is_dir():
                # 尚无 destination 记录（retire_temp/retire_metadata 阶段）：
                # 写元数据并推进到 <restored>/旧版本/。
                transaction.begin_product_write()
                content_hash = directory_manifest_hash(
                    retire_temp,
                    exclude_names=frozenset({RETIRED_METADATA_FILENAME}),
                )
                meta: RetiredVersionMetadata = {
                    "retired_from": str(retire_old),
                    "content_hash": content_hash,
                    "retired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "retired_by": "restore_retired_version",
                }
                _write_retired_metadata(retire_temp, meta)
                destination = _allocate_backup_destination(
                    retired_from, retire_old.name
                )
                transaction.set_phase(
                    "retire_official",
                    details={"retire_destination": str(destination)},
                )
                try:
                    os.replace(retire_temp, destination)
                except OSError as exc:
                    return _keep_scene(
                        transaction,
                        "retire_failed",
                        f"续跑推进退位临时目录失败：{exc}",
                        {"temp": str(retire_temp), "restored": str(retired_from)},
                    )
                retire_temp_resolved = destination
            else:
                # restored 不在且无法推进：归位 current，保留现场。
                transaction.begin_product_write()
                try:
                    os.replace(retire_temp, retire_old)
                except OSError as exc:
                    return _keep_scene(
                        transaction,
                        "retire_inconsistent",
                        f"续跑无法把退位临时目录归位：{exc}",
                        {"temp": str(retire_temp), "old": str(retire_old)},
                    )
                transaction.commit()
                return _ok("ok", "已把退位临时目录归位，旧程序恢复原状", {})
            # 6B-IMP-007：推进成功后必须对账，失败按 reindex_failed 收尾。
            try:
                reconcile_subtree(str(ws), str(retired_from.parent))
            except (AssetIndexError, OSError) as exc:
                transaction.commit()
                return _ok(
                    "reindex_failed",
                    f"续跑恢复退位后索引对账失败：{exc}",
                    {"restored": str(retired_from), "retired": str(retire_temp_resolved)},
                )

    # 6B-IMP-006：续跑前按日志 manifest 验证本操作记录的产物；被第三方
    # 改动或与记录不符时保留 recovery_required，绝不继续清 defaults/退位。
    product_error = _verify_recorded_products(transaction)
    if product_error is not None:
        return product_error

    transaction.begin_product_write()
    # 6B-IMP-003/008：只有「restore_apply_done」才推进退位；所有 apply 前
    # 已提升状态（含 restore_staging / restore_promoted 等阶段）都必须按
    # 磁盘状态安全移回 backup，不能带着未改写的引用继续。
    if phase == "restore_apply_done":
        pass
    elif retired_from.is_dir() and backup and not backup.exists() and phase in {
        "promote_restored",
        "apply_update",
        "restore_staging",
        "restore_promoted",
    }:
        if not _move_back_to_backup(retired_from, backup):
            return _keep_scene(
                transaction,
                "restore_inconsistent",
                "续跑无法把已提升副本移回旧版本",
                {"restored": str(retired_from), "backup": str(backup)},
            )
        transaction.commit()
        return _ok("ok", "已将未完成的恢复副本移回旧版本", {"backup": str(backup)})
    elif (
        phase in {"promote_restored", "restore_staging"}
        and backup
        and backup.is_dir()
    ):
        # 6B-IMP-010：搬动尚未发生（temp 不在、backup 仍在），不能落入
        # 后续分支把「未执行恢复」误报成恢复成功。
        transaction.commit()
        return _ok("ok", "恢复操作尚未搬动副本，已清理", {"backup": str(backup)})

    if retired_from.is_dir() and current.is_dir():
        # 6B-IMP-013：仍在线的 current 不能凭历史副本的 retired_from 跳过退位。
        if not _current_already_retired(retired_from, current):
            try:
                retire_asset_to_backup(
                    transaction,
                    ws,
                    current,
                    retired_from,
                    retired_by="restore_retired_version",
                )
            except (ManifestError, OSError, RetireBackupError) as exc:
                return _keep_scene(
                    transaction,
                    "retire_failed",
                    f"续跑退位失败：{exc}",
                    {"restored": str(retired_from), "current": str(current)},
                )

    try:
        if retired_from.parent.exists():
            reconcile_subtree(str(ws), str(retired_from.parent))
    except (AssetIndexError, OSError) as exc:
        transaction.commit()
        return _ok("reindex_failed", f"续跑对账失败：{exc}", {"restored": str(retired_from)})
    transaction.commit()
    log_fn("已续跑完成备用副本恢复")
    return _ok("ok", "已续跑完成备用副本恢复", {"restored": str(retired_from)})
