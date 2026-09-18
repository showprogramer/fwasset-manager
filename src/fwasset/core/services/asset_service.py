"""程序（asset）层新增 / 删除 / 待补齐候选区服务（TASK-20260918-asset-crud-incomplete，
父规格 D0.2、D2.4a、D2.5、D3、D7.1-D7.5、D10.1c）。

`create_asset` / `delete_asset` / `supplement_candidate` / `delete_candidate` /
`undo_asset_delete` 五个公共入口，均返回 :class:`fwasset.core.types.ServiceResult`。

事务收敛语义完全沿用子任务 4（``model_scheme_service``）已收口的判据，不重新
发明：零产物失败 → ``commit()`` 收敛为 ``clean``，可重试；已落盘的半成品 →
不 ``commit()``，``__exit__`` 落 ``recovery_required``；判据一律是**原路径是否
仍在原位**，不是「有没有抛异常」。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from fwasset.core.admission import AdmissionError
from fwasset.core.asset_index import bulk_reindex_subtree
from fwasset.core.asset_info import (
    IMPORT_STATE_INCOMPLETE,
    clear_candidate_metadata,
    import_state_from_asset_info,
    load_asset_info_with_status,
    save_candidate_metadata,
    save_vendor,
)
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.file_scan import classify_staged_content
from fwasset.core.import_io import (
    AssetImportError,
    promote_import,
    stage_import_archive,
    stage_import_directory,
    stage_import_files,
)
from fwasset.core.managed_paths import (
    RETIRED_VERSIONS_DIRNAME,
    assert_managed_write,
    managed_path_reason,
    managed_root,
)
from fwasset.core.manifest import (
    ManifestError,
    directory_manifest,
    directory_manifest_hash,
    manifest_hash,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    contained_subpath,
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
from fwasset.core.staging_io import (
    StagingError,
    _assert_session_directory,
    allocate_staging_area,
    cleanup_staging_area,
)
from fwasset.core.types import ServiceResult
from fwasset.core.workspace_transaction import WorkspaceTransaction

__all__ = [
    "create_asset",
    "delete_asset",
    "supplement_candidate",
    "delete_candidate",
    "undo_asset_delete",
]

_COMMON_DIR = "通用"
_CUSTOM_DIR = "定制"


def _error(code: str, message: str, payload: dict | None = None) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def _ok(code: str, message: str, payload: dict | None = None) -> ServiceResult:
    return {"ok": True, "code": code, "message": message, "payload": payload or {}}


def _session_filenames(session: Path) -> list[str]:
    """staging 会话内全部文件的相对路径（正斜杠归一，供 catalog 判定）。"""
    names: list[str] = []
    for entry in sorted(session.rglob("*")):
        if entry.is_file():
            names.append(str(entry.relative_to(session)).replace("\\", "/"))
    return names


# ---------------------------------------------------------------------------
# A2/A3 create_asset
# ---------------------------------------------------------------------------


def create_asset(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    *,
    source: str | Sequence[str | Path],
    source_kind: str,
    model_root: str | Path,
    scope: str,
    scheme_name: str = "",
    module_name: str,
    asset_name: str,
    vendor: str = "",
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """新增程序：catalog 完整 → 正式提升；不完整 → 存入待补齐候选区（D0.2/D7.5）。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    model_root_path = Path(model_root)

    if source_kind not in ("files", "directory", "archive"):
        return _error("invalid_scope", f"非法的来源类型：{source_kind!r}")
    if scope not in (_COMMON_DIR, _CUSTOM_DIR):
        return _error("invalid_scope", f"非法的 scope：{scope!r}，必须是「通用」或「定制」")
    if scope == _CUSTOM_DIR and not str(scheme_name).strip():
        return _error("invalid_scope", "scope 为「定制」时 scheme_name 必填")
    if not str(module_name).strip():
        return _error("invalid_name", "模块名不能为空")
    if not str(asset_name).strip():
        return _error("invalid_name", "程序名不能为空")

    from fwasset.core.managed_paths import _has_model_marker

    if not _has_model_marker(model_root_path):
        return _error("invalid_target", f"目标不是型号根：{model_root_path}")

    if scope == _COMMON_DIR:
        target = model_root_path / _COMMON_DIR / module_name / asset_name
    else:
        scheme_root = model_root_path / _CUSTOM_DIR / scheme_name
        if not scheme_root.is_dir():
            return _error("invalid_target", f"方案不存在：{scheme_root}")
        target = scheme_root / module_name / asset_name

    with WorkspaceTransaction(ws, operation="create_asset") as transaction:
        try:
            session = _stage_source(transaction, ws, source_kind, source)
        except AssetImportError as exc:
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)

        filenames = _session_filenames(session)
        matched = classify_staged_content(target, filenames)

        if matched is None:
            return _create_incomplete_candidate(
                transaction, ws, session, vendor=vendor, log_fn=log_fn
            )

        try:
            # gate 已确认 configured_root 非空且与 workspace_root 一致。
            destination = promote_import(
                transaction, ws, str(configured_root), session, target
            )
        except AdmissionError as exc:
            # validate_new_path 在 begin_product_write 之前（import_io.py:192-198），
            # 故准入失败仍是零产物：清理 staging 后空提交为 clean。
            cleanup_staging_area(ws, session)
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)
        except AssetImportError as exc:
            # source_unreadable（staging 会话消失）发生在 begin_product_write 之前，
            # 仍是零产物。
            cleanup_staging_area(ws, session)
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)
        except StagingError as exc:
            # begin_product_write 之后的失败（容器创建 / 原子提升）：目标路径是否
            # 已存在决定收敛方式，判据同子任务 4（原路径/目标路径是否已落盘）。
            if target.exists():
                return _error("promote_failed", str(exc))
            cleanup_staging_area(ws, session)
            transaction.commit()
            return _error("promote_failed", str(exc))

        status, error_msg = save_vendor(destination, vendor)
        if status not in ("ok",):
            log_fn(f"新程序 vendor 写入失败（{status}）：{error_msg}")

        transaction.set_phase("indexed", details={"target": str(destination)})

        code = "ok"
        message = f"程序「{asset_name}」已创建"
        try:
            bulk_reindex_subtree(str(ws), str(destination), [])
        except Exception as exc:  # noqa: BLE001
            log_fn(f"新程序索引写入失败，需重扫：{exc}")
            code = "index_pending"
            message = f"程序「{asset_name}」已创建，但索引未同步，请重新读取程序列表"

        transaction.commit()
        return _ok(code, message, {"asset_path": str(destination)})


def _stage_source(
    transaction: WorkspaceTransaction,
    ws: Path,
    source_kind: str,
    source: str | Sequence[str | Path],
) -> Path:
    if source_kind == "files":
        files: list[str | Path] = [source] if isinstance(source, str) else list(source)
        staged = stage_import_files(transaction, ws, files)
    elif source_kind == "directory":
        staged = stage_import_directory(transaction, ws, str(source))
    else:
        staged = stage_import_archive(transaction, ws, str(source))
    return Path(staged["session"])


def _create_incomplete_candidate(
    transaction: WorkspaceTransaction,
    ws: Path,
    session: Path,
    *,
    vendor: str,
    log_fn: Callable[..., None],
) -> ServiceResult:
    """A3：staging 内容不完整，原子移入受管候选区，返回 ``created_incomplete``。"""
    filenames = _session_filenames(session)
    intended_firmware_type = "handcontrol_ui" if _looks_like_handcontrol(filenames) else ""

    candidate_id = f"{uuid.uuid4().hex[:8]}-{_slug(session.name)}"
    candidate_root = managed_root(ws, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id

    try:
        assert_within_workspace(candidate_path, ws)
    except PathGuardError as exc:
        cleanup_staging_area(ws, session)
        transaction.commit()
        return _error("staging_unavailable", f"候选目录路径非法：{exc}")

    transaction.begin_product_write()

    try:
        _promote_staging_to_candidate(transaction, ws, session, candidate_path)
    except StagingError as exc:
        # MSC-014 同判据：看**内容是否已经离开 staging 会话**，不是「有没有抛
        # 异常」。移动前失败（会话身份校验、写授权、manifest 计算）→ 会话仍在
        # 原处，真零产物，清理会话后 commit() 收敛为 clean，可重试；os.replace
        # 之后失败 → 会话已消失、候选目录已存在，非零产物半成品，不 commit()，
        # 由 __exit__ 落 recovery_required。
        if session.is_dir():
            cleanup_staging_area(ws, session)
            transaction.commit()
            return _error("promote_failed", str(exc))
        return _error(
            "promote_failed",
            f"候选区提升失败且内容已移动，需人工恢复：{exc}",
            {"recovery_required": True, "candidate_path": str(candidate_path)},
        )

    status, error_msg = save_candidate_metadata(
        candidate_path, vendor=vendor, intended_firmware_type=intended_firmware_type
    )
    if status != "ok":
        log_fn(f"候选元数据写入失败（{status}）：{error_msg}")

    transaction.set_phase("indexed", details={"candidate": str(candidate_path)})
    transaction.commit()

    missing = "缺少 .pkg" if intended_firmware_type == "handcontrol_ui" else "未匹配到任何已知程序类型"
    return _ok(
        "created_incomplete",
        "内容不完整，已存入待补齐候选区",
        {
            "candidate_path": str(candidate_path),
            "candidate_id": candidate_id,
            "missing": missing,
        },
    )


def _looks_like_handcontrol(filenames: list[str]) -> bool:
    from fwasset.core.settings import SCAN_PKG_EXTENSIONS, SCAN_ROM_EXTENSIONS

    lower = [name.lower() for name in filenames]
    has_rom = any(name.endswith(tuple(SCAN_ROM_EXTENSIONS)) for name in lower)
    has_pkg = any(name.endswith(tuple(SCAN_PKG_EXTENSIONS)) for name in lower)
    return has_rom or has_pkg


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value)[:16] or "candidate"


def _promote_staging_to_candidate(
    transaction: WorkspaceTransaction,
    workspace_root: Path,
    session: Path,
    candidate_path: Path,
) -> None:
    """把 staging 会话原子移入受管候选区（不是业务路径，不能走 ``promote_staging``）。

    ``promote_staging`` 的 ``_assert_promotable_target`` 明确拒绝任何落在
    ``.fwasset`` 内部目录的目标——候选区恰恰在那里。这里改用
    ``assert_managed_write(..., expect="incomplete_candidate")`` 做写授权，
    其余步骤（会话身份校验、manifest 记录、``os.replace``）与
    ``promote_staging`` 同构。
    """
    if not transaction.is_active:
        raise StagingError("事务尚未开始或已经完成，无法提升")
    if not session.is_dir():
        raise StagingError(f"staging 会话目录不存在或不是目录：{session}")
    _assert_session_directory(session, workspace_root)
    try:
        assert_managed_write(session, workspace_root, expect="staging")
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    try:
        destination = assert_managed_write(
            candidate_path, workspace_root, expect="incomplete_candidate"
        )
    except PathGuardError as exc:
        raise StagingError(str(exc)) from exc
    if destination.exists():
        raise StagingError(f"候选目录已存在，拒绝提升：{destination}")

    try:
        manifest = manifest_hash(directory_manifest(session))
    except (ManifestError, OSError) as exc:
        # 与 promote_staging 同样收束为 StagingError，让调用方的零产物判据
        # 接管；裸异常穿透 service 边界会绕过事务收敛（AGENTS.md：不以裸异常
        # 代替服务错误码）。
        raise StagingError(str(exc)) from exc
    transaction.record_product(destination, manifest)
    try:
        os.replace(session, destination)
    except OSError as exc:
        raise StagingError(
            f"提升失败（目标可能被外部抢占，staging 已保留）：{destination}"
        ) from exc


# ---------------------------------------------------------------------------
# A5 supplement_candidate
# ---------------------------------------------------------------------------


def supplement_candidate(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    candidate_id: str,
    *,
    files: Sequence[str | Path],
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """补充候选目录文件：禁止覆盖、staging 合并、CAS 提交、原子更新 import_state。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    candidate_path, error = _resolve_candidate(ws, candidate_id)
    if error is not None:
        return error

    data, status, err_msg = load_asset_info_with_status(candidate_path)
    if status != "ok" or import_state_from_asset_info(data) != IMPORT_STATE_INCOMPLETE:
        return _error("invalid_candidate", f"候选项元数据无效或已不是待补齐状态：{err_msg or status}")

    try:
        preimage = directory_manifest_hash(candidate_path)
    except ManifestError as exc:
        return _error("invalid_candidate", f"候选目录内容无法校验：{exc}")

    existing_names = {p.name.lower() for p in candidate_path.iterdir() if p.is_file()}
    for item in files:
        name = Path(item).name
        if name.lower() in existing_names:
            return _error("file_exists", f"补充文件与候选现有文件同名，禁止覆盖：{name}")

    with WorkspaceTransaction(ws, operation="supplement_candidate") as transaction:
        try:
            staging = allocate_staging_area(ws, transaction)
        except StagingError as exc:
            return _error("staging_unavailable", str(exc))

        try:
            import shutil

            for item in files:
                src = Path(item)
                if not src.is_file():
                    raise AssetImportError("source_unreadable", f"来源不是文件：{src}")
                shutil.copy2(src, staging / src.name)
        except AssetImportError as exc:
            cleanup_staging_area(ws, staging)
            transaction.commit()
            return _error(exc.code, exc.message, exc.payload)
        except OSError as exc:
            cleanup_staging_area(ws, staging)
            transaction.commit()
            return _error("source_unreadable", f"复制补充文件失败：{exc}")

        merged_filenames = sorted(
            {p.name for p in candidate_path.iterdir() if p.is_file()}
            | {p.name for p in staging.iterdir() if p.is_file()}
        )
        matched = classify_staged_content(candidate_path, merged_filenames)

        transaction.begin_product_write()

        try:
            current_hash = directory_manifest_hash(candidate_path)
        except ManifestError as exc:
            cleanup_staging_area(ws, staging)
            return _error("invalid_candidate", f"候选目录内容无法校验：{exc}")
        if current_hash != preimage:
            cleanup_staging_area(ws, staging)
            transaction.commit()
            return _error("stale_candidate", "候选目录已被并发修改，提交已拒绝")

        try:
            _merge_staging_into(staging, candidate_path)
        except OSError as exc:
            return _error("promote_failed", f"补充文件合并失败：{exc}")

        try:
            cleanup_staging_area(ws, staging)
        except StagingError:
            pass

        if matched is not None:
            status, err_msg = clear_candidate_metadata(candidate_path)
            if status != "ok":
                log_fn(f"候选元数据清除失败（{status}）：{err_msg}")

        transaction.set_phase("indexed", details={"candidate": str(candidate_path)})
        transaction.commit()

        if matched is not None:
            return _ok(
                "ok",
                "补齐完成，请选择放到哪里",
                {"candidate_path": str(candidate_path), "complete": True},
            )
        return _ok(
            "ok",
            "已补充文件，仍不完整，可继续补充",
            {"candidate_path": str(candidate_path), "complete": False},
        )


def _merge_staging_into(staging: Path, candidate_path: Path) -> None:
    import shutil

    for entry in staging.iterdir():
        destination = candidate_path / entry.name
        shutil.move(str(entry), str(destination))


def _resolve_candidate(
    ws: Path, candidate_id: str
) -> tuple[Path, ServiceResult | None]:
    if not str(candidate_id).strip():
        return Path(), _error("invalid_candidate", "candidate_id 不能为空")
    candidate_root = managed_root(ws, "incomplete_candidate")
    candidate_path = candidate_root / candidate_id
    resolved = contained_subpath(candidate_path, candidate_root)
    if resolved is None or resolved.parent != candidate_root.resolve():
        return Path(), _error("invalid_candidate", f"候选项不存在：{candidate_id}")
    if not candidate_path.is_dir():
        return Path(), _error("invalid_candidate", f"候选项不存在：{candidate_id}")
    return candidate_path, None


# ---------------------------------------------------------------------------
# A7 delete_candidate
# ---------------------------------------------------------------------------


def delete_candidate(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    candidate_id: str,
    *,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """删除待补齐候选项（D10.1c）：不做 R8 asset 反查，仍过路径守卫与状态 CAS。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    candidate_path, error = _resolve_candidate(ws, candidate_id)
    if error is not None:
        return error

    data, status, err_msg = load_asset_info_with_status(candidate_path)
    if status != "ok" or import_state_from_asset_info(data) != IMPORT_STATE_INCOMPLETE:
        return _error("invalid_candidate", f"候选项元数据无效或已不是待补齐状态：{err_msg or status}")

    try:
        preimage = directory_manifest_hash(candidate_path)
    except ManifestError as exc:
        return _error("invalid_candidate", f"候选目录内容无法校验：{exc}")

    try:
        with WorkspaceTransaction(ws, operation="delete_candidate") as transaction:
            try:
                current_hash = directory_manifest_hash(candidate_path)
            except ManifestError as exc:
                transaction.commit()
                return _error("invalid_candidate", f"候选目录内容无法校验：{exc}")
            if current_hash != preimage:
                transaction.commit()
                return _error("stale_candidate", "候选目录已被并发修改，删除已拒绝")

            transaction.begin_product_write()

            try:
                record = register_delete(ws, candidate_path)
            except QuarantineError:
                if not candidate_path.exists():
                    raise
                transaction.commit()
                return _error("quarantine_failed", "隔离登记失败，目标未删除，可重试")

            transaction.commit()
            return _ok(
                "ok",
                "待补齐项已删除，可在 5 秒内撤销",
                {"quarantine_record_id": record["id"], "original_path": str(candidate_path)},
            )
    except QuarantineError as exc:
        return _error(
            "quarantine_failed",
            f"隔离登记时目标已被移动但收尾失败，需人工恢复：{exc}",
            {"recovery_required": True, "target": str(candidate_path)},
        )


# ---------------------------------------------------------------------------
# A6 delete_asset
# ---------------------------------------------------------------------------


def delete_asset(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    asset_path: str | Path,
    *,
    confirm_shared: bool,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """删除程序（D2.4a 八步）：反查、隔离、空模块容器 rmdir、两级索引对账。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    ws = Path(workspace_root)
    target_path = Path(asset_path)

    try:
        with WorkspaceTransaction(ws, operation="delete_asset") as transaction:
            lookup_result = find_references_to(configured_root, ws, target_path, "asset")
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

            owner_root = _owner_model_root(target_path)
            cross_owner_hits = [
                h for h in lookup.hits if owner_root is None or h.owner_root != str(owner_root)
            ]
            if cross_owner_hits and not confirm_shared:
                transaction.commit()
                return _error(
                    "confirmation_required",
                    f"存在 {len(cross_owner_hits)} 条跨型号借用命中，需确认后再删除",
                    {
                        "hits": [h.__dict__ for h in cross_owner_hits],
                        "retired_copies": _count_retired_copies(target_path),
                    },
                )

            retired_copies = _count_retired_copies(target_path)

            transaction.begin_product_write()

            module_dir = target_path.parent
            removed_containers = _plan_removed_containers(module_dir, target_path)

            try:
                record = register_delete(
                    ws, target_path, removed_containers=removed_containers
                )
            except QuarantineError:
                if not target_path.exists():
                    raise
                transaction.commit()
                return _error("quarantine_failed", "隔离登记失败，目标未删除，可重试")

            try:
                bulk_reindex_subtree(str(ws), str(target_path), [])
            except Exception as exc:  # noqa: BLE001
                log_fn(f"删除后索引写入失败，需重新读取程序列表：{exc}")

            container_removed = False
            for container in removed_containers:
                container_path = Path(container)
                try:
                    if container_path.is_dir() and not any(container_path.iterdir()):
                        container_path.rmdir()
                        container_removed = True
                except OSError as exc:
                    log_fn(f"空模块容器删除失败，已保留：{container}（{exc}）")

            if container_removed:
                try:
                    reconcile_subtree(str(ws), str(module_dir.parent))
                except Exception as exc:  # noqa: BLE001
                    log_fn(f"删除后对模块父级对账失败，需重新读取程序列表：{exc}")

            transaction.commit()
            return _ok(
                "ok",
                f"「{target_path.name}」已删除，可在 5 秒内撤销",
                {
                    "quarantine_record_id": record["id"],
                    "original_path": str(target_path),
                    "retired_copies": retired_copies,
                },
            )
    except QuarantineError as exc:
        return _error(
            "quarantine_failed",
            f"隔离登记时目标已被移动但收尾失败，需人工恢复：{exc}",
            {"recovery_required": True, "target": str(target_path)},
        )


def _owner_model_root(target_path: Path) -> Path | None:
    """asset 路径所属的型号根（向上回溯具型号标志的祖先，同 admission._find_model_root）。

    MSC-010 同口径：``ReferenceHit.owner_root`` 恒为型号根字符串，跨 owner
    比较对象必须是型号根本身，不是方案或模块目录。
    """
    from fwasset.core.managed_paths import _has_model_marker

    node = target_path.parent
    for _ in range(6):
        if _has_model_marker(node):
            return node
        if node.parent == node:
            return None
        node = node.parent
    return None


def _count_retired_copies(asset_path: Path) -> int:
    retired_dir = asset_path / RETIRED_VERSIONS_DIRNAME
    if not retired_dir.is_dir():
        return 0
    try:
        return sum(1 for entry in retired_dir.iterdir() if entry.is_dir())
    except OSError:
        return 0


def _plan_removed_containers(module_dir: Path, asset_path: Path) -> list[str]:
    """判断删除 ``asset_path`` 后 ``module_dir`` 是否会变空，规划自动 rmdir 的容器。

    只登记**本次删除会清空**的模块容器——判据是当前模块目录下除
    ``asset_path`` 外没有其它子项；含 ``旧版本/`` 或 ``程序信息.toml`` 等其它
    内容一律不登记（D2.4a 步骤 7：遇任何内容一律保留）。仅一层（模块容器），
    不递归判定祖先——D0.2 层级下型号根/通用/定制不会因程序删除变空。
    """
    try:
        siblings = list(module_dir.iterdir())
    except OSError:
        return []
    remaining = [p for p in siblings if p.resolve() != asset_path.resolve()]
    if remaining:
        return []
    return [str(module_dir)]


# ---------------------------------------------------------------------------
# A8 undo_asset_delete
# ---------------------------------------------------------------------------


def undo_asset_delete(
    workspace_root: str | Path,
    record_id: str,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """撤销一次 asset / 候选项删除；覆盖 A6 与 A7 两类隔离记录。

    重建模块容器（D10.1b）：记录带 ``removed_containers`` 时 ``undo_delete``
    内部会先按序重建再移回内容。对账分流：asset 记录撤销后对原边界
    ``reconcile_subtree``；候选记录（位于 ``incomplete_candidate`` 受管根内）
    从未进过索引，不对账。
    """
    ws = Path(workspace_root)

    try:
        with WorkspaceTransaction(ws, operation="undo_asset_delete") as transaction:
            try:
                manifest_records = load_quarantine_manifest(ws)
            except QuarantineError as exc:
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
            is_candidate = (
                managed_path_reason(original_path, is_dir=True, workspace_root=ws)
                == "incomplete_candidate"
            )

            transaction.begin_product_write()

            try:
                manifest = directory_manifest_hash(quarantine_path)
            except ManifestError as exc:
                transaction.commit()
                return _error("undo_failed", f"隔离内容校验失败，可重试撤销：{exc}")

            transaction.record_product(original_path, manifest)

            try:
                undo_delete(ws, record_id)
            except UndoConflictError as exc:
                transaction.commit()
                return _error("undo_conflict", f"撤销目标已被占用，隔离内容已保留：{exc}")
            except QuarantineError:
                if not quarantine_path.exists():
                    raise
                transaction.commit()
                return _error("undo_failed", "撤销失败，隔离内容已保留，可重试")

            if not is_candidate:
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
        return _error(
            "undo_failed",
            f"撤销时隔离内容已移动但收尾失败，需人工恢复：{exc}",
            {"recovery_required": True, "record_id": record_id},
        )
