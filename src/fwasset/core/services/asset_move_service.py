"""沿用原文件修改程序身份：受管移动目录并同步引用。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from fwasset.core.admission import AdmissionError, validate_new_path
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    same_path_identity,
)
from fwasset.core.reference_lookup import check_reference_gate, find_references_to
from fwasset.core.services.layout_update_service import (
    _expected_change_kind,
    _scan_single_asset,
)
from fwasset.core.services.reference_service import (
    apply_rewrite_plan,
    build_clear_defaults_plan,
    build_rewrite_plan,
)
from fwasset.core.types import ClearDefaultsKind, RewriteRequest, ServiceResult
from fwasset.core.workspace_transaction import (
    WorkspaceBusyError,
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
)


def _error(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def move_asset_in_place(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    old_path: str | Path,
    new_path: str | Path,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """移动现有程序目录；不复制固件，不退位旧目录。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws = Path(workspace_root).resolve()
    try:
        old = assert_within_workspace(old_path, ws)
        target = assert_within_workspace(new_path, ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"路径不在当前工作区内：{exc}")
    if same_path_identity(old, target):
        return _error("invalid_args", "程序位置未变化")
    scanned, reason = _scan_single_asset(ws, old)
    if scanned is None:
        return _error("invalid_target", f"原程序无法读取：{reason}")
    model_root = Path(str(scanned.get("model_directory_path") or ""))
    if not model_root.is_dir() or not target.is_relative_to(model_root):
        return _error("domain_violation", "新位置必须属于原型号")

    kind = _expected_change_kind(old, target, ws)
    lookup = find_references_to(configured_root, ws, old, "asset")
    if not lookup["ok"]:
        return lookup
    hits = list(getattr(lookup["payload"].get("result"), "hits", []) or [])
    if kind is not None and any(hit.kind.startswith("shared_") for hit in hits):
        return _error("reference_conflict", "程序存在其他型号的关联，暂不能直接改类型或范围")

    try:
        validate_new_path(
            target, kind="asset", configured_root=str(configured_root or ""), workspace_root=ws
        )
    except AdmissionError as exc:
        return _error(exc.code, exc.message, exc.payload)

    try:
        return _move_locked(
            configured_root, ws, old, target, kind, model_root, log_fn
        )
    except (WorkspaceBusyError, WorkspaceRecoveryRequiredError) as exc:
        if isinstance(exc, WorkspaceBusyError):
            return _error("workspace_busy", "工作区正在执行另一项写操作，请稍后重试")
        return _error("recovery_required", "工作区存在待恢复的中断操作，暂不能写入")


def _move_locked(
    configured_root: str | Path | None,
    ws: Path,
    old: Path,
    target: Path,
    kind: str | None,
    model_root: Path,
    log_fn: Callable[[str], None],
) -> ServiceResult:
    with WorkspaceTransaction(ws, operation="move_asset_in_place") as transaction:
        try:
            validate_new_path(
                target, kind="asset", configured_root=str(configured_root or ""), workspace_root=ws
            )
        except AdmissionError as exc:
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)
        fresh, reason = _scan_single_asset(ws, old)
        if fresh is None:
            transaction.commit()
            return _error("invalid_target", f"原程序已变化：{reason}")
        if Path(str(fresh.get("model_directory_path") or "")) != model_root:
            transaction.commit()
            return _error("stale_plan", "程序所属型号已变化，请重新读取后再试")
        if _expected_change_kind(old, target, ws) != kind:
            transaction.commit()
            return _error("stale_plan", "程序目标类型或范围已变化，请重新打开更新窗口")
        locked_lookup = find_references_to(configured_root, ws, old, "asset")
        if not locked_lookup["ok"]:
            transaction.commit()
            return locked_lookup
        locked_hits = list(getattr(locked_lookup["payload"].get("result"), "hits", []) or [])
        if kind is not None and any(hit.kind.startswith("shared_") for hit in locked_hits):
            transaction.commit()
            return _error("reference_conflict", "程序存在其他型号的关联，暂不能直接改类型或范围")

        if kind is None:
            planned = build_rewrite_plan(
                configured_root,
                ws,
                RewriteRequest(
                    operation="rename",
                    target_kind="asset",
                    old_path=str(old),
                    new_path=str(target),
                ),
                log_fn,
            )
        else:
            planned = build_clear_defaults_plan(
                configured_root, ws, old, cast(ClearDefaultsKind, kind), log_fn=log_fn
            )
        if not planned["ok"]:
            transaction.commit()
            return planned
        plan = planned["payload"]["plan"]

        parent_created = not target.parent.exists()
        transaction.begin_product_write()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, target)
        except OSError as exc:
            if target.exists():
                return _error("move_inconsistent", f"移动未完成，需人工检查目标目录：{exc}")
            if parent_created and target.parent.is_dir() and not any(target.parent.iterdir()):
                target.parent.rmdir()
            transaction.commit()
            return _error("move_failed", f"移动程序失败：{exc}")

        moved, reason = _scan_single_asset(ws, target)
        if moved is None:
            try:
                os.rename(target, old)
                if parent_created and not any(target.parent.iterdir()):
                    target.parent.rmdir()
            except OSError as exc:
                return _error("move_inconsistent", f"程序无法识别且回移失败，需人工恢复：{exc}")
            transaction.commit()
            return _error("invalid_asset", f"目标位置无法识别原程序：{reason}")

        applied = apply_rewrite_plan(plan, configured_root, log_fn)
        if not applied["ok"]:
            if applied["code"] == "rolled_back":
                try:
                    os.rename(target, old)
                    if parent_created and not any(target.parent.iterdir()):
                        target.parent.rmdir()
                except OSError as exc:
                    return _error("move_inconsistent", f"引用回滚后程序回移失败，需人工恢复：{exc}")
                transaction.commit()
                return _error("rewrite_failed", "引用更新失败，程序已移回原位置")
            return _error("move_inconsistent", "引用更新未完整回滚，需人工恢复", applied["payload"])

        try:
            reconcile_subtree(str(ws), str(old.parent))
            if old.parent != target.parent:
                reconcile_subtree(str(ws), str(target.parent))
        except Exception as exc:  # noqa: BLE001
            transaction.commit()
            return {
                "ok": True,
                "code": "index_pending",
                "message": "程序已移动，但列表索引未同步，请重新读取程序列表",
                "payload": {"old": str(old), "replacement": str(target), "detail": str(exc)},
            }
        transaction.commit()
        message = f"已修改程序：{target.name}"
        log_fn(message)
        return {
            "ok": True,
            "code": "ok",
            "message": message,
            "payload": {"old": str(old), "replacement": str(target)},
        }
