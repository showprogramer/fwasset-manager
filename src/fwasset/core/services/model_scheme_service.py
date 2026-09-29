"""型号 / 方案 CRUD 服务（TASK-20260918-model-scheme-crud，父规格 D2.1-D2.4/D9）。

`create_model` / `create_scheme` / `rename_model` / `rename_scheme` /
`delete_model` / `delete_scheme` / `undo_model_scheme_delete` 七个公共入口，
均返回 :class:`fwasset.core.types.ServiceResult`。仅支持 ``multi_model``
布局的型号级操作；方案级操作不受此前置限制（沿用 ``admission.py`` 既有的
方案域校验）。

**MSC-001 划线（规格 r2 修订，三轮审查收口）**：所有无产品写入的校验
（gate、布局、``validate_new_path``、反查、用户确认）都在进入
``WorkspaceTransaction`` 之前完成；锁内保留的「一次重验」失败时先
``transaction.commit()``（此时未 ``begin_product_write()``，是安全的空
提交）再返回错误码，不让普通的校验失败坠入 ``recovery_required``。只有
``begin_product_write()`` 之后磁盘可能已发生变化的分支才允许落
``recovery_required``（`__exit__` 自动完成，不在本模块显式调用）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from fwasset.core.admission import AdmissionError, validate_new_path
from fwasset.core.asset_index import bulk_reindex_subtree
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.managed_paths import _has_model_marker, detect_workspace_layout
from fwasset.core.manifest import ManifestError, directory_manifest, manifest_hash
from fwasset.core.model_config import load_model_config, save_model_id, slugify_model_id
from fwasset.core.path_guard import same_path_identity
from fwasset.core.platform_config import (
    PlatformDefaults,
    load_platform_config_with_status,
    save_platform_config,
)
from fwasset.core.quarantine import (
    QuarantineError,
    UndoConflictError,
    load_quarantine_manifest,
    register_delete,
    undo_delete,
)
from fwasset.core.reference_lookup import (
    check_reference_gate,
    find_references_to,
    is_blocking_issue,
)
from fwasset.core.scheme_config import save_scheme_config
from fwasset.core.services.reference_service import (
    apply_rewrite_plan,
    build_rewrite_plan,
)
from fwasset.core.staging_io import (
    StagingError,
    allocate_staging_area,
    cleanup_staging_area,
    promote_staging,
)
from fwasset.core.types import ChassisType, RewriteRequest, ServiceResult
from fwasset.core.workspace_transaction import WorkspaceTransaction

__all__ = [
    "create_model",
    "change_chassis_type",
    "create_scheme",
    "rename_model",
    "rename_scheme",
    "delete_model",
    "delete_scheme",
    "undo_model_scheme_delete",
]

_CHASSIS_TYPES: tuple[ChassisType, ...] = ("单3D", "单2D", "双2D", "上3D下2D")


def _error(code: str, message: str, payload: dict | None = None) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def _ok(code: str, message: str, payload: dict | None = None) -> ServiceResult:
    return {"ok": True, "code": code, "message": message, "payload": payload or {}}


def _move_directory(source: Path, destination: Path) -> None:
    """重命名目录本体（``os.replace`` 的薄包装，供测试针对性 mock）。"""
    os.replace(source, destination)


def _allocate_model_id(workspace_root: Path, base: str) -> str:
    """按既有型号 id 去重（同款 ``model_id_service._allocate_id`` 逻辑，内联实现）。"""
    occupied: set[str] = set()
    try:
        children = sorted(workspace_root.iterdir(), key=lambda p: p.name)
    except OSError:
        children = []

    for child in children:
        if not child.is_dir():
            continue
        mid, status, _error_msg = load_model_config(child)
        if status == "ok" and mid:
            occupied.add(mid)
    candidate = base
    n = 2
    while candidate in occupied:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# D1 create_model
# ---------------------------------------------------------------------------


def create_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_name: str,
    chassis_type: ChassisType,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """新建型号：分配 model_id、写单块机芯类型、staging 原子提升（D2.1/D5.1-5.2）。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    layout = detect_workspace_layout(ws)
    if layout == "single_model":
        return _error(
            "migration_required", "当前工作区是旧单型号布局，请先完成工作区布局迁移再新增型号"
        )
    if layout == "invalid":
        return _error("layout_invalid", "工作区布局无法识别（存在无法归类的内容），禁止新增型号")

    if chassis_type not in _CHASSIS_TYPES:
        return _error("invalid_chassis_type", f"非法的机芯类型：{chassis_type!r}")

    target = ws / model_name

    with WorkspaceTransaction(ws, operation="create_model") as transaction:
        try:
            validate_new_path(
                target, kind="model", configured_root=str(configured_root), workspace_root=ws
            )
        except AdmissionError as exc:
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)

        transaction.begin_product_write()

        try:
            staging = allocate_staging_area(ws, transaction)
        except StagingError as exc:
            return _error("staging_unavailable", str(exc))

        model_id = _allocate_model_id(ws, slugify_model_id(model_name))
        try:
            (staging / "通用").mkdir()
            (staging / "定制").mkdir()
            save_model_id(staging, model_id)
            save_platform_config(staging, [PlatformDefaults(chassis_type, {})])
        except OSError as exc:
            cleanup_staging_area(ws, staging)
            return _error("promote_failed", f"staging 骨架搭建失败：{exc}")

        try:
            promote_staging(transaction, ws, staging, target)
        except StagingError as exc:
            cleanup_staging_area(ws, staging)
            return _error("promote_failed", str(exc))

        transaction.set_phase("indexed", details={"target": str(target)})

        code = "ok"
        message = f"型号「{model_name}」已创建"
        try:
            bulk_reindex_subtree(str(ws), str(target), [])
        except Exception as exc:  # noqa: BLE001
            log_fn(f"新型号索引写入失败，需重扫：{exc}")
            code = "index_pending"
            message = f"型号「{model_name}」已创建，但索引未同步，请重新读取程序列表"

        transaction.commit()
        return _ok(
            code,
            message,
            {"model_root": str(target), "model_id": model_id, "chassis_type": chassis_type},
        )


def change_chassis_type(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    chassis_type: ChassisType,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """修改型号的机芯类型：只改 `平台配置.toml` 的单块名，保留模块默认。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    if chassis_type not in _CHASSIS_TYPES:
        return _error("invalid_chassis_type", f"非法的机芯类型：{chassis_type!r}")

    ws = Path(workspace_root)
    root = Path(model_root)
    if not _has_model_marker(root) or same_path_identity(root, ws):
        return _error("invalid_target", f"目标不是型号根：{root}")

    with WorkspaceTransaction(ws, operation="change_chassis_type") as transaction:
        platforms, status, detail = load_platform_config_with_status(root)
        if status in ("parse_error", "parser_missing"):
            transaction.commit()
            return _error("config_parse_error", "平台配置读取失败，已停止修改", {"detail": detail})
        if len(platforms) > 1:
            transaction.commit()
            return _error("platform_not_normalized", "该型号有多个平台配置块，无法直接修改机芯类型")

        previous = platforms[0].platform_name if platforms else ""
        if previous == chassis_type:
            transaction.commit()
            return _ok("unchanged", f"机芯类型已经是「{chassis_type}」", {"previous": previous})

        transaction.begin_product_write()
        block = platforms[0] if platforms else PlatformDefaults(chassis_type, {})
        block.platform_name = chassis_type
        try:
            save_platform_config(root, [block])
        except OSError as exc:
            transaction.commit()
            return _error("write_failed", f"写入平台配置失败：{exc}")

        try:
            reconcile_subtree(str(ws), str(root))
        except Exception as exc:  # noqa: BLE001
            log_fn(f"机芯类型修改后索引对账失败，需重新读取程序列表：{exc}")

        transaction.commit()
        return _ok(
            "ok",
            f"「{root.name}」的机芯类型已改为「{chassis_type}」",
            {"model_root": str(root), "previous": previous, "chassis_type": chassis_type},
        )


# ---------------------------------------------------------------------------
# D2 create_scheme
# ---------------------------------------------------------------------------


def create_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    scheme_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """新建方案：写 ``方案配置.toml``（不含 platform 字段），staging 原子提升（D2.2）。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    model_root_path = Path(model_root)
    if not _has_model_marker(model_root_path):
        return _error("invalid_target", f"目标不是型号根：{model_root_path}")

    target = model_root_path / "定制" / scheme_name

    with WorkspaceTransaction(ws, operation="create_scheme") as transaction:
        try:
            validate_new_path(
                target, kind="scheme", configured_root=str(configured_root), workspace_root=ws
            )
        except AdmissionError as exc:
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)

        transaction.begin_product_write()

        try:
            staging = allocate_staging_area(ws, transaction)
        except StagingError as exc:
            return _error("staging_unavailable", str(exc))

        try:
            save_scheme_config(staging, scheme_name)
        except OSError as exc:
            cleanup_staging_area(ws, staging)
            return _error("promote_failed", f"staging 骨架搭建失败：{exc}")

        try:
            promote_staging(transaction, ws, staging, target)
        except StagingError as exc:
            cleanup_staging_area(ws, staging)
            return _error("promote_failed", str(exc))

        transaction.set_phase("indexed", details={"target": str(target)})

        code = "ok"
        message = f"方案「{scheme_name}」已创建"
        try:
            bulk_reindex_subtree(str(ws), str(target), [])
        except Exception as exc:  # noqa: BLE001
            log_fn(f"新方案索引写入失败，需重扫：{exc}")
            code = "index_pending"
            message = f"方案「{scheme_name}」已创建，但索引未同步，请重新读取程序列表"

        transaction.commit()
        return _ok(code, message, {"scheme_root": str(target), "scheme_name": scheme_name})


# ---------------------------------------------------------------------------
# D3 rename_model / rename_scheme
# ---------------------------------------------------------------------------


def _rename_target(
    *,
    target_kind: str,
    configured_root: str | Path | None,
    workspace_root: str | Path,
    target: str | Path,
    new_name: str,
    log_fn: Callable[..., None],
) -> ServiceResult:
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    target_path = Path(target)

    if target_kind == "model":
        layout = detect_workspace_layout(ws)
        if layout == "single_model":
            return _error(
                "migration_required",
                "当前工作区是旧单型号布局，请先完成工作区布局迁移再重命名型号",
            )
        if layout == "invalid":
            return _error(
                "layout_invalid", "工作区布局无法识别（存在无法归类的内容），禁止重命名型号"
            )

    new_path = target_path.parent / new_name
    try:
        validate_new_path(
            new_path, kind=target_kind, configured_root=configured_root, workspace_root=ws  # type: ignore[arg-type]
        )
    except AdmissionError as exc:
        return _error(exc.code, exc.message, exc.payload)

    plan_result = build_rewrite_plan(
        configured_root,
        ws,
        RewriteRequest(
            operation="rename",
            target_kind=target_kind,  # type: ignore[arg-type]
            old_path=str(target_path),
            new_path=str(new_path),
        ),
        log_fn,
    )
    if not plan_result["ok"]:
        return plan_result
    plan = plan_result["payload"]["plan"]

    with WorkspaceTransaction(ws, operation=f"rename_{target_kind}") as transaction:
        try:
            validate_new_path(
                new_path, kind=target_kind, configured_root=configured_root, workspace_root=ws  # type: ignore[arg-type]
            )
        except AdmissionError as exc:
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)

        transaction.begin_product_write()

        try:
            _move_directory(target_path, new_path)
        except OSError as exc:
            transaction.commit()
            return _error("rename_failed", f"重命名失败：{exc}")

        apply_result = apply_rewrite_plan(plan, configured_root, log_fn)
        if not apply_result["ok"]:
            code = apply_result["code"]
            if code == "rolled_back":
                try:
                    _move_directory(new_path, target_path)
                except OSError as exc:
                    # 回移本身失败：目录停在新路径，磁盘为真实半成品态，
                    # 不提交——让 __exit__ 自动落 recovery_required（MSC-001
                    # 划线之外的预期行为，须交给启动恢复流程人工核实）。
                    return _error(
                        "rename_inconsistent",
                        f"改写回滚成功但目录移回失败，需人工恢复：{exc}",
                        apply_result["payload"],
                    )
                transaction.commit()
                return _error(
                    "rewrite_failed", "级联改写失败，已完整回滚并移回原目录", apply_result["payload"]
                )
            # rollback_conflict 或其它未完整回滚的失败：磁盘已在新路径且部分
            # 写入，属真实半成品态，不提交——__exit__ 自动落 recovery_required。
            return _error(
                "rename_inconsistent", "级联改写失败且回滚存在冲突，需人工恢复", apply_result["payload"]
            )

        if target_kind == "model":
            reconcile_root = ws
        else:
            reconcile_root = target_path.parent  # 定制/ 目录

        try:
            reconcile_subtree(str(ws), str(reconcile_root))
        except Exception as exc:  # noqa: BLE001
            log_fn(f"重命名后索引对账失败，需重新读取程序列表：{exc}")

        transaction.commit()
        return _ok(
            "ok",
            f"「{target_path.name}」已重命名为「{new_name}」",
            {
                "old_path": str(target_path),
                "new_path": str(new_path),
                "applied_files": apply_result["payload"].get("applied", []),
            },
        )


def rename_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    new_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """重命名型号（D2.3，仅支持 ``multi_model`` 布局）。"""
    return _rename_target(
        target_kind="model",
        configured_root=configured_root,
        workspace_root=workspace_root,
        target=model_root,
        new_name=new_name,
        log_fn=log_fn,
    )


def rename_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    scheme_root: str | Path,
    new_name: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """重命名方案（D2.3）。"""
    return _rename_target(
        target_kind="scheme",
        configured_root=configured_root,
        workspace_root=workspace_root,
        target=scheme_root,
        new_name=new_name,
        log_fn=log_fn,
    )


# ---------------------------------------------------------------------------
# D4 delete_model / delete_scheme
# ---------------------------------------------------------------------------


def _cross_owner_hits(hits: list, owner_root: Path) -> list:
    """筛出跨 owner 的借用命中。

    MSC-010：``hit.owner_root`` 恒为型号根（``reference_lookup`` 各处
    赋值均是 ``str(model_root)``）。型号删除时 ``owner_root`` 参数即
    型号根本身，比较成立；方案删除时必须传入方案所属的**型号根**
    （``scheme_root.parent.parent``），不能传方案目录本身——否则方案
    目录永远不等于任何 ``hit.owner_root``，同型号内的引用会被误判为
    跨 owner，凭空要求用户勾选确认。
    """
    owner_norm = str(owner_root)
    return [h for h in hits if h.owner_root != owner_norm]


def _delete_target(
    *,
    target_kind: str,
    configured_root: str | Path | None,
    workspace_root: str | Path,
    target: str | Path,
    confirm_shared: bool,
    log_fn: Callable[..., None],
) -> ServiceResult:
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    target_path = Path(target)

    if target_kind == "model":
        if not _has_model_marker(target_path):
            return _error("invalid_target", f"目标不是型号根：{target_path}")
        if same_path_identity(target_path, ws):
            return _error(
                "migration_required",
                "旧单型号布局下型号根即工作区根，请先完成工作区布局迁移再删除型号",
            )
        # MSC-011：D9 布局前置矩阵要求 delete_model 遇 invalid 布局同样拒绝
        # （只对 target_kind == "model" 加；方案级入口按规格不受此限制）。
        layout = detect_workspace_layout(ws)
        if layout == "single_model":
            return _error(
                "migration_required",
                "当前工作区是旧单型号布局，请先完成工作区布局迁移再删除型号",
            )
        if layout == "invalid":
            return _error(
                "layout_invalid", "工作区布局无法识别（存在无法归类的内容），禁止删除型号"
            )
        owner_root = target_path
    else:
        if target_path.parent.name != "定制":
            return _error("invalid_target", f"目标不是方案目录：{target_path}")
        # MSC-010：方案删除的 owner 比较对象是型号根（型号根/定制/方案名），
        # 不是方案目录自身。
        owner_root = target_path.parent.parent

    try:
        with WorkspaceTransaction(ws, operation=f"delete_{target_kind}") as transaction:
            lookup_result = find_references_to(configured_root, ws, target_path, target_kind)  # type: ignore[arg-type]
            if not lookup_result["ok"]:
                transaction.commit()
                return lookup_result
            lookup = lookup_result["payload"]["result"]

            blocking = [issue for issue in lookup.issues if is_blocking_issue(issue)]
            if blocking:
                transaction.commit()
                return _error(
                    "lookup_blocked",
                    "存在配置损坏或身份异常，反查清单不完整，已阻止删除",
                    {"issues": [i.__dict__ for i in blocking]},
                )

            cross_owner_hits = _cross_owner_hits(lookup.hits, owner_root)
            if cross_owner_hits and not confirm_shared:
                transaction.commit()
                return _error(
                    "confirmation_required",
                    f"存在 {len(cross_owner_hits)} 条跨型号关联命中，需确认后再删除",
                    {"hits": [h.__dict__ for h in cross_owner_hits]},
                )

            transaction.begin_product_write()

            try:
                record = register_delete(ws, target_path)
            except QuarantineError:
                # MSC-014：``register_delete``（``quarantine._register``）先落盘
                # "moving" 清单记录、再 ``os.replace`` 实际移动、最后再写一次
                # 清单转正为 "pending"（quarantine.py:333-341）。三步里只有
                # **第一步之前**的失败（manifest 计算失败、
                # assert_within_workspace/assert_managed_write 拒绝）是真零
                # 产物；``os.replace`` 成功之后（含第三步「转正」失败）目标
                # 已经不在原路径，属非零产物半成品——与 MSC-009（undo 路径的
                # 镜像问题）同一类错误，判据同样是「原路径是否仍然存在」，
                # 不能无条件 commit()。
                #
                # 零产物（原路径仍在）→ commit() 收敛为 clean 再返回错误码，
                # 可重试。非零产物（原路径已消失，内容已在隔离区或介于两者
                # 之间）→ 不 commit，让 with 块以未提交状态退出，__exit__
                # 自动落 recovery_required，交外层 except 转 ServiceResult。
                if not target_path.exists():
                    raise
                transaction.commit()
                return _error("quarantine_failed", "隔离登记失败，目标未删除，可重试")

            try:
                bulk_reindex_subtree(str(ws), str(target_path), [])
            except Exception as exc:  # noqa: BLE001
                log_fn(f"删除后索引写入失败，需重新读取程序列表：{exc}")

            transaction.commit()
            return _ok(
                "ok",
                f"「{target_path.name}」已删除，可在回收站还原",
                {"quarantine_record_id": record["id"], "original_path": str(target_path)},
            )
    except QuarantineError as exc:
        # MSC-014：源目录已被移动（进入隔离区甚至更晚的转正阶段）但登记未
        # 完整收尾，工作区已落 recovery_required（__exit__ 已完成），须人工
        # 恢复；不让裸异常穿透到 service 边界（AGENTS.md 要求）。
        return _error(
            "quarantine_failed",
            f"隔离登记时目标已被移动但收尾失败，需人工恢复：{exc}",
            {"recovery_required": True, "target": str(target_path)},
        )


def delete_model(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    *,
    confirm_shared: bool,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """删除型号（D2.4，零 TOML 改写，可撤销）。"""
    return _delete_target(
        target_kind="model",
        configured_root=configured_root,
        workspace_root=workspace_root,
        target=model_root,
        confirm_shared=confirm_shared,
        log_fn=log_fn,
    )


def delete_scheme(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    scheme_root: str | Path,
    *,
    confirm_shared: bool,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """删除方案（D2.4，零 TOML 改写，可撤销）。"""
    return _delete_target(
        target_kind="scheme",
        configured_root=configured_root,
        workspace_root=workspace_root,
        target=scheme_root,
        confirm_shared=confirm_shared,
        log_fn=log_fn,
    )


# ---------------------------------------------------------------------------
# undo_model_scheme_delete
# ---------------------------------------------------------------------------


def undo_model_scheme_delete(
    workspace_root: str | Path,
    record_id: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """撤销一次型号/方案删除（MSC-005/MSC-008/MSC-009：seqlock 覆盖 quarantine 移动）。

    MSC-009：``QuarantineError``（非 ``UndoConflictError``）在「移动已发生」
    时不得被 service 边界捕获后静默 commit——``with`` 块以未提交状态退出，
    让 ``__exit__`` 落 ``recovery_required``；本函数在**外层**捕获该异常
    并转成 ``ServiceResult``（``undo_failed`` + ``recovery_required: True``
    payload），不让裸异常穿透到调用方。

    MSC-015：外层 ``try/except QuarantineError`` 只应捕获「已确认移动之
    后」的异常。``load_quarantine_manifest`` 是纯读取，其 ``QuarantineError``
    （清单文件损坏/无法解析）与移动无关、且发生在 ``begin_product_write()``
    之前——必须在事务内单独捕获并空提交为 ``clean``，不能让外层 ``except``
    把它误报成「隔离内容已移动但收尾失败」。
    """
    ws = Path(workspace_root)

    try:
        with WorkspaceTransaction(ws, operation="undo_model_scheme_delete") as transaction:
            try:
                manifest_records = load_quarantine_manifest(ws)
            except QuarantineError as exc:
                # MSC-015：纯读失败，未调用 begin_product_write()，零产物，
                # 空提交收敛为 clean 再返回 undo_failed，消息不含「已移动」。
                transaction.commit()
                return _error("undo_failed", f"隔离清单读取失败，可重试撤销：{exc}")

            record = None
            for item in manifest_records:
                if item["id"] == record_id:
                    record = item
                    break
            if record is None:
                transaction.commit()
                return _error("undo_failed", f"隔离记录不存在：{record_id}")

            quarantine_path = Path(record["quarantine_path"])
            original_path = Path(record["original_path"])

            transaction.begin_product_write()

            try:
                manifest = manifest_hash(directory_manifest(quarantine_path))
            except ManifestError as exc:
                # MSC-008：零产物分支（隔离内容尚未移动、清单未改写），须先收束
                # 为 clean 再返回错误，不得留 recovery_required。
                transaction.commit()
                return _error("undo_failed", f"隔离内容校验失败，可重试撤销：{exc}")

            transaction.record_product(original_path, manifest)

            try:
                undo_delete(ws, record_id)
            except UndoConflictError as exc:
                # MSC-005：确认零产物（undo_delete 内部占用检查早于 os.replace），
                # commit() 对奇偶两种起始 generation 都能正确收敛。
                transaction.commit()
                return _error("undo_conflict", f"撤销目标已被占用，隔离内容已保留：{exc}")
            except QuarantineError:
                # MSC-009：QuarantineError 覆盖两类分支，不能一律当零产物。
                # ``undo_delete`` 的 os.replace 失败（quarantine.py:460-461）与
                # 清单改写失败（:464）都发生在移动**之后**，磁盘可能已变；只有
                # 移动前的前置校验失败（状态非 pending、窗口已过、路径越界）
                # 才是零产物。
                #
                # 判据：``quarantine_path`` 是否仍然存在——``os.replace``
                # 成功后隔离侧内容已搬空，``quarantine_path`` 必然不再存在，
                # 无论后续 ``_save_quarantine_manifest``（:464）是否失败，
                # 都能正确反映「移动已发生」。``original_path.exists()`` 不
                # 足以证明本次移动发生：外部程序可能在原路径新建同名目录，
                # 导致误判；``quarantine_path`` 是本次操作独占的内部状态，
                # 不受外部程序影响，是更准的判据。
                #
                # 移动未发生（quarantine_path 仍在）→ 零产物，commit() 收敛
                # 为 clean 再返回 undo_failed，可重试。移动已发生
                # （quarantine_path 已消失）→ 不 commit，让 with 块以未提交
                # 状态退出，__exit__ 自动落 recovery_required，交外层捕获。
                if not quarantine_path.exists():
                    raise
                transaction.commit()
                return _error("undo_failed", "撤销失败，隔离内容已保留，可重试")

            if original_path.parent == ws:
                reconcile_root = ws
            else:
                reconcile_root = original_path.parent

            try:
                reconcile_subtree(str(ws), str(reconcile_root))
            except Exception as exc:  # noqa: BLE001
                log_fn(f"撤销后索引对账失败，需重新读取程序列表：{exc}")

            transaction.commit()
            return _ok(
                "ok", f"已撤销删除：{original_path.name}", {"original_path": str(original_path)}
            )
    except QuarantineError as exc:
        # MSC-009：移动已发生但收尾失败——目录已在原路径，隔离清单可能未
        # 改写，工作区已落 recovery_required（__exit__ 已完成），须人工恢复。
        # MSC-015：``load_quarantine_manifest`` 的纯读失败已在事务内单独
        # 捕获并提前返回，不会落到这里——本分支只覆盖「已确认移动之后」
        # 的异常（``undo_delete`` 内 os.replace/清单改写失败）。
        return _error(
            "undo_failed",
            f"撤销时隔离内容已移动但收尾失败，需人工恢复：{exc}",
            {"recovery_required": True, "record_id": record_id},
        )
