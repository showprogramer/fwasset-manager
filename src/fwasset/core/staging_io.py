"""staging 分配、原子提升与「删除本次产物」原语（D7.1 / D1.4c）。

TASK-20260915-atomic-dir-primitives（父规格子任务 1 收尾）。前置依赖：
TASK-20260905 受管路径守卫、TASK-20260915 事务状态基础。三条硬边界：

- staging 只能在**持锁的事务内**分配；提升目标必须已通过
  ``begin_product_write``（D8 不变量①：产品数据变更前先进入非 ``clean``）；
- 提升顺序是**先记录产物后 ``os.replace``**——崩溃夹在中间只会留下
  「日志多于现场」，反向顺序会留下无日志的孤儿产物；
- 「删除本次产物」先过身份 / 日志状态 / manifest 三重校验，删除动作只
  碰 manifest 记录过的文件，任何失守都保留现场（D1.4c，禁止递归盲删）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Literal

from fwasset.core.managed_paths import (
    MANAGED_ROOT_DIRNAME,
    assert_managed_write,
    managed_path_reason,
    managed_root,
)
from fwasset.core.manifest import (
    FileEntry,
    ManifestError,
    directory_manifest,
    manifest_hash,
    path_is_reparse_point,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    normalize_workspace_path,
)
from fwasset.core.types import OperationProduct
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    WorkspaceTransactionError,
    workspace_lock_is_held,
)

CleanupConflictReason = Literal["identity", "log_state", "manifest", "delete_failed"]


class StagingError(WorkspaceTransactionError):
    """staging 分配、提升或清理拒绝继续时抛出。"""


class ProductCleanupConflict(StagingError):
    """「删除本次产物」三重校验或删除动作受阻（D1.4c）。

    ``reason`` 区分失守种类；调用方（后续 service 层）应把冲突映射为
    ``update_inconsistent`` 并附恢复材料，本层不做任何静默清理。
    """

    def __init__(
        self,
        message: str,
        *,
        reason: CleanupConflictReason,
        target: Path,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.target = target
        self.expected = expected
        self.actual = actual


def _assert_transaction_workspace(
    transaction: WorkspaceTransaction, workspace_root: str | Path
) -> None:
    """staging 操作必须落在事务所属的工作区（状态与产物记录才能一致）。"""
    if (
        normalize_workspace_path(transaction.workspace_root)
        != normalize_workspace_path(workspace_root)
    ):
        raise StagingError(
            "staging 操作的工作区与事务所属工作区不一致，已拒绝："
            f"{workspace_root}"
        )


def _assert_session_directory(
    staging_area: Path, workspace_root: str | Path
) -> Path:
    """会话目录必须是 staging 根的直接子目录；staging 根本身拒绝操作。"""
    staging_root = managed_root(workspace_root, "staging")
    if normalize_workspace_path(staging_area.parent) != normalize_workspace_path(
        staging_root
    ):
        raise StagingError(
            f"目标不是 staging 会话目录（staging 根及其嵌套路径均拒绝）：{staging_area}"
        )
    return staging_root


def allocate_staging_area(
    workspace_root: str | Path, transaction: WorkspaceTransaction
) -> Path:
    """在持锁事务内分配一个 staging 会话目录（应用生成，不接受用户输入）。"""
    if not workspace_lock_is_held(workspace_root) or not transaction.is_active:
        raise StagingError("staging 只能在持锁且未提交的事务内分配")
    _assert_transaction_workspace(transaction, workspace_root)
    area = managed_root(workspace_root, "staging") / uuid.uuid4().hex
    try:
        assert_managed_write(area, workspace_root, expect="staging")
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    try:
        area.mkdir(parents=False)
    except OSError as exc:
        raise StagingError(f"staging 会话目录创建失败：{area}") from exc
    return area


def promote_staging(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    staging_area: str | Path,
    target: str | Path,
) -> None:
    """把 staging 会话目录原子提升为最终业务路径，并记录产物。"""
    if not transaction.is_active:
        raise StagingError("事务尚未开始或已经完成，无法提升")
    _assert_transaction_workspace(transaction, workspace_root)
    if transaction.status.generation % 2 == 0:
        raise StagingError(
            "产品数据写入尚未开始（须先调用 begin_product_write），拒绝提升"
        )
    area = Path(staging_area)
    destination = Path(target)
    if not area.is_dir():
        raise StagingError(f"staging 会话目录不存在或不是目录：{area}")
    _assert_session_directory(area, workspace_root)
    try:
        assert_managed_write(area, workspace_root, expect="staging")
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    try:
        assert_within_workspace(destination, workspace_root)
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    _assert_promotable_target(workspace_root, destination)

    try:
        manifest = manifest_hash(directory_manifest(area))
    except ManifestError as exc:
        raise StagingError(str(exc)) from exc
    transaction.record_product(destination, manifest)
    try:
        os.replace(area, destination)
    except OSError as exc:
        raise StagingError(
            f"提升失败（目标可能被外部抢占，staging 已保留）：{destination}"
        ) from exc


def _assert_promotable_target(
    workspace_root: str | Path, destination: Path
) -> None:
    if os.path.normcase(MANAGED_ROOT_DIRNAME) in {
        os.path.normcase(part) for part in destination.parts
    }:
        raise StagingError(f"目标落在应用内部目录，拒绝提升：{destination}")
    if (
        managed_path_reason(
            destination, is_dir=True, workspace_root=workspace_root
        )
        is not None
    ):
        raise StagingError(f"目标落在受管区域，拒绝提升：{destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise StagingError(f"目标父目录不存在：{parent}")
    target_name = os.path.normcase(destination.name)
    try:
        existing = list(parent.iterdir())
    except OSError as exc:
        raise StagingError(f"无法检查目标是否已存在：{parent}") from exc
    for entry in existing:
        if os.path.normcase(entry.name) == target_name:
            raise StagingError(
                f"目标已存在（含大小写等价），拒绝提升：{destination}"
            )


def delete_recorded_product(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    target: str | Path,
) -> None:
    """按 D1.4c 删除本操作记录的产物；任何失守都保留现场并报告冲突。"""
    destination = Path(target)
    if not transaction.is_active:
        raise ProductCleanupConflict(
            "事务已结束，操作日志状态不允许清理产物",
            reason="log_state",
            target=destination,
        )
    _assert_transaction_workspace(transaction, workspace_root)
    try:
        assert_within_workspace(destination, workspace_root)
    except PathGuardError as exc:
        raise ProductCleanupConflict(
            f"产物路径不在工作区内，已拒绝清理：{destination}",
            reason="identity",
            target=destination,
        ) from exc
    normalized = os.path.normcase(str(destination))
    recorded: OperationProduct | None = None
    for product in transaction.products:
        if os.path.normcase(product["path"]) == normalized:
            recorded = product
    if recorded is None:
        raise ProductCleanupConflict(
            f"路径不是本操作记录的产物，已拒绝清理：{destination}",
            reason="identity",
            target=destination,
        )
    if not destination.exists():
        return
    if not destination.is_dir():
        raise ProductCleanupConflict(
            f"产物不是目录，内容与记录不一致，已保留现场：{destination}",
            reason="manifest",
            target=destination,
            expected=recorded["manifest"],
        )
    try:
        entries = directory_manifest(destination)
    except ManifestError as exc:
        raise ProductCleanupConflict(
            f"产物内容无法校验（{exc}），已保留现场：{destination}",
            reason="manifest",
            target=destination,
            expected=recorded["manifest"],
        ) from exc
    current_hash = manifest_hash(entries)
    if current_hash != recorded["manifest"]:
        raise ProductCleanupConflict(
            f"产物内容与记录不一致，已保留现场：{destination}",
            reason="manifest",
            target=destination,
            expected=recorded["manifest"],
            actual=current_hash,
        )
    _delete_recorded_files(destination, entries)
    _remove_directories_bottom_up(destination)


def _resolve_plain_path(root: Path, relpath: str) -> Path:
    """逐段复核路径链：任一段在校验后被替换为链接即中止。

    不跟随 reparse point——把校验与 ``unlink`` 之间的竞争窗口收敛为
    「复核到系统调用」的纳秒级，与受管根认领的复核策略一致。
    """
    if path_is_reparse_point(root):
        raise ProductCleanupConflict(
            f"产物根在校验后被替换为链接，已保留剩余现场：{root}",
            reason="delete_failed",
            target=root,
        )
    current = root
    for part in relpath.split("/"):
        current = current / part
        if path_is_reparse_point(current):
            raise ProductCleanupConflict(
                f"产物路径在校验后被替换为链接，已保留剩余现场：{current}",
                reason="delete_failed",
                target=root,
            )
    return current


def _delete_recorded_files(
    destination: Path, entries: list[FileEntry]
) -> None:
    """只删 manifest 记录过的文件；任何失守立即中止并保留剩余现场。"""
    for entry in entries:
        file_path = _resolve_plain_path(destination, entry.relpath)
        try:
            file_path.unlink()
        except OSError as exc:
            raise ProductCleanupConflict(
                f"产物文件删除受阻（内容可能已被外部改动），已保留剩余现场："
                f"{file_path}",
                reason="delete_failed",
                target=destination,
            ) from exc


def _remove_directories_bottom_up(root: Path) -> None:
    """自底向上清空目录树；非空目录（计划外内容）导致中止而非递归强删。"""
    directories = [root]
    stack = [root]
    while stack:
        current = stack.pop()
        if path_is_reparse_point(current):
            raise ProductCleanupConflict(
                f"产物目录在遍历期间被替换为链接，已保留剩余现场：{current}",
                reason="delete_failed",
                target=root,
            )
        try:
            children = list(current.iterdir())
        except OSError as exc:
            raise ProductCleanupConflict(
                f"无法枚举产物目录（可能存在计划外内容），已保留剩余现场：{current}",
                reason="delete_failed",
                target=root,
            ) from exc
        for child in children:
            if path_is_reparse_point(child):
                directories.append(child)
                continue
            if child.is_dir():
                directories.append(child)
                stack.append(child)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError as exc:
            raise ProductCleanupConflict(
                f"目录非空或被占用（可能存在计划外内容），已保留剩余现场：{directory}",
                reason="delete_failed",
                target=root,
            ) from exc


def cleanup_staging_area(workspace_root: str | Path, staging_area: str | Path) -> None:
    """删除整个 staging 会话目录；不要求事务存活，供失败与恢复路径共用。"""
    area = Path(staging_area)
    _assert_session_directory(area, workspace_root)
    try:
        assert_managed_write(area, workspace_root, expect="staging")
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    if not area.exists():
        return
    if not area.is_dir():
        raise StagingError(f"staging 会话不是目录：{area}")
    if path_is_reparse_point(area):
        raise StagingError(
            f"staging 会话目录是链接或重定向路径，已拒绝清理：{area}"
        )
    try:
        _unlink_staging_files(area)
        _rmdir_staging_bottom_up(area)
    except OSError as exc:
        raise StagingError(f"staging 清理中止，已保留剩余现场：{area}") from exc


def _unlink_staging_files(root: Path) -> None:
    """删除 staging 树内全部普通文件；reparse point 只移除链接本身。"""
    if path_is_reparse_point(root):
        raise OSError(f"staging 会话在清理前被替换为链接：{root}")
    stack = [root]
    while stack:
        current = stack.pop()
        if path_is_reparse_point(current):
            raise OSError(f"staging 目录在遍历期间被替换为链接：{current}")
        for child in current.iterdir():
            if path_is_reparse_point(child):
                if child.is_dir():
                    os.rmdir(child)
                else:
                    child.unlink()
                continue
            if child.is_dir():
                stack.append(child)
            else:
                child.unlink()


def _rmdir_staging_bottom_up(root: Path) -> None:
    directories = [root]
    stack = [root]
    while stack:
        current = stack.pop()
        if path_is_reparse_point(current):
            raise OSError(f"staging 目录在遍历期间被替换为链接：{current}")
        for child in current.iterdir():
            if child.is_dir():
                directories.append(child)
                stack.append(child)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        directory.rmdir()
