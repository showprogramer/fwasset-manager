"""导入成形与边界防护（TASK-20260917，父规格 D7.1–D7.4）。

厂商可信来源的轻量导入原语：来源最小防呆（重叠 / 受管 / 元数据
文件跳过）、zip 解压与「最多剥一层」成形；持锁事务内由
:func:`promote_import` 完成准入复验、容器创建与原子提升。stage 阶段
不触碰产品数据（不调 ``begin_product_write``）；失败即清理 staging
会话，不留半成品。catalog 完整性分流与候选区归子任务 5。
"""

from __future__ import annotations

import os
import shutil
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path

from fwasset.core.admission import validate_new_path
from fwasset.core.managed_paths import ASSET_METADATA_FILENAME, managed_path_reason
from fwasset.core.manifest import manifest_hash
from fwasset.core.path_guard import is_within_boundary
from fwasset.core.staging_io import (
    StagingError,
    _assert_session_directory,
    _assert_transaction_workspace,
    allocate_staging_area,
    cleanup_staging_area,
    promote_staging,
)
from fwasset.core.types import StagedImport
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    workspace_lock_is_held,
)


class AssetImportError(RuntimeError):
    """导入拒绝或失败；``code`` 见 Task 规格，``payload`` 携带定位信息。"""

    def __init__(
        self,
        code: str,
        message: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.payload: dict[str, object] = payload or {}


def stage_import_files(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    files: Sequence[str | Path],
) -> StagedImport:
    """散选文件导入：全部放入会话根（即未来资产目录内容）。"""
    session = allocate_staging_area(workspace_root, transaction)
    try:
        paths = [Path(item) for item in files]
        if not paths:
            raise AssetImportError("empty_source", "未选择任何来源文件")
        seen: dict[str, str] = {}
        for path in paths:
            name_key = os.path.normcase(path.name)
            if name_key in seen:
                raise AssetImportError(
                    "duplicate_name",
                    f"来源文件名重复（大小写等价）：{seen[name_key]} 与 {path.name}",
                    {"conflicts": [seen[name_key], path.name]},
                )
            seen[name_key] = path.name
            if not path.is_file():
                raise AssetImportError("source_unreadable", f"来源不是文件：{path}")
            _assert_source_not_managed(
                path, is_dir=False, workspace_root=workspace_root
            )
        skipped: list[str] = []
        for path in paths:
            if _is_metadata_name(path.name):
                skipped.append(str(path))
                continue
            try:
                shutil.copy2(path, session / path.name)
            except OSError as exc:
                raise AssetImportError(
                    "archive_extract_failed",
                    f"复制来源文件失败：{path}（{exc}）",
                ) from exc
        if len(skipped) == len(paths):
            raise AssetImportError(
                "empty_source",
                "来源全部是应用元数据文件（程序信息.toml），没有可导入内容",
                {"skipped_metadata": skipped},
            )
        return {"session": str(session), "skipped_metadata": skipped}
    except BaseException:
        _cleanup_quietly(workspace_root, session)
        raise


def stage_import_directory(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    source: str | Path,
    *,
    session: str | Path | None = None,
) -> StagedImport:
    """文件夹导入：会话根替代来源目录这一层，内部结构原样保留。

    调用方若已分配并持久化 ``session``，传入后本函数不再另行分配；失败时
    也不自动清理该会话，以便崩溃续跑按日志回收。
    """
    if session is None:
        owned = True
        area = allocate_staging_area(workspace_root, transaction)
    else:
        owned = False
        area = Path(session)
        if not transaction.is_active or not workspace_lock_is_held(workspace_root):
            raise StagingError("传入的 staging 会话只能在持锁且未提交的事务内使用")
        _assert_transaction_workspace(transaction, workspace_root)
        _assert_session_directory(area, workspace_root)
        if not area.is_dir():
            raise StagingError(f"传入的 staging 会话不存在或不是目录：{area}")
        try:
            leftover = any(area.iterdir())
        except OSError as exc:
            raise StagingError(f"无法读取传入的 staging 会话：{area}") from exc
        if leftover:
            raise StagingError(f"传入的 staging 会话必须为空：{area}")
    try:
        src = Path(source)
        if not src.is_dir():
            raise AssetImportError("source_unreadable", f"来源不是目录：{src}")
        _assert_source_not_managed(src, is_dir=True, workspace_root=workspace_root)
        if is_within_boundary(area, src):
            raise AssetImportError(
                "source_overlap",
                f"来源包含暂存区，导入会产生自嵌套：{src}",
            )
        skipped = _copy_tree_contents(src, area)
        if _count_files(area) == 0:
            raise AssetImportError(
                "empty_source",
                "来源目录没有任何可导入文件",
                {"skipped_metadata": skipped},
            )
        return {"session": str(area), "skipped_metadata": skipped}
    except BaseException:
        if owned:
            _cleanup_quietly(workspace_root, area)
        raise


def stage_import_archive(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    archive: str | Path,
) -> StagedImport:
    """zip 压缩包导入：解压进会话内临时子目录，最多剥掉一层包装目录。"""
    session = allocate_staging_area(workspace_root, transaction)
    try:
        src = Path(archive)
        if not src.is_file():
            raise AssetImportError("source_unreadable", f"来源不是文件：{src}")
        _assert_source_not_managed(
            src, is_dir=False, workspace_root=workspace_root
        )
        if src.suffix.lower() != ".zip":
            raise AssetImportError(
                "archive_unsupported",
                f"仅支持 zip 压缩包：{src.name}",
            )
        extract_root = session / f".extract-{uuid.uuid4().hex[:8]}"
        try:
            extract_root.mkdir()
            with zipfile.ZipFile(src) as archive_file:
                archive_file.extractall(extract_root)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError) as exc:
            raise AssetImportError(
                "archive_extract_failed",
                f"压缩包解压失败：{src.name}（{exc}）",
            ) from exc
        entries = list(extract_root.iterdir())
        content_root = (
            entries[0]
            if len(entries) == 1 and entries[0].is_dir()
            else extract_root
        )
        skipped = _move_extracted_content(content_root, session)
        shutil.rmtree(extract_root, ignore_errors=True)
        if _count_files(session) == 0:
            raise AssetImportError(
                "empty_source",
                "压缩包内没有任何文件",
                {"skipped_metadata": skipped},
            )
        return {"session": str(session), "skipped_metadata": skipped}
    except BaseException:
        _cleanup_quietly(workspace_root, session)
        raise


def promote_import(
    transaction: WorkspaceTransaction,
    workspace_root: str | Path,
    configured_root: str | Path,
    staged_session: str | Path,
    target: str | Path,
) -> Path:
    """锁内：准入复验（D3.7）→ 产品写入 → 容器创建 → 原子提升。

    ``AdmissionError`` / ``WorkspaceTransactionError`` / ``StagingError`` /
    ``OSError`` 原样向上传播，由调用方的事务与 staging 清理逻辑收尾。
    """
    session = Path(staged_session)
    if not session.is_dir():
        raise AssetImportError("source_unreadable", f"staging 会话不存在：{session}")
    destination = validate_new_path(
        target,
        kind="asset",
        configured_root=configured_root,
        workspace_root=workspace_root,
    )
    transaction.begin_product_write()
    _create_missing_containers(transaction, destination)
    promote_staging(transaction, workspace_root, session, destination)
    return destination


def _is_metadata_name(name: str) -> bool:
    return os.path.normcase(name) == os.path.normcase(ASSET_METADATA_FILENAME)


def _assert_source_not_managed(
    source: Path, *, is_dir: bool, workspace_root: str | Path
) -> None:
    reason = managed_path_reason(source, is_dir=is_dir, workspace_root=workspace_root)
    if reason is not None and reason != "asset_metadata":
        raise AssetImportError(
            "source_managed",
            f"来源位于应用受管区域（{reason}），不能作为导入来源：{source}",
            {"reason": reason},
        )


def _copy_tree_contents(source: Path, session: Path) -> list[str]:
    """复制来源目录内容到会话根；只跳过来源根层的元数据文件。"""
    skipped: list[str] = []
    for current, _dirnames, filenames in os.walk(source):
        rel = Path(current).relative_to(source)
        destination_dir = session / rel
        destination_dir.mkdir(parents=True, exist_ok=True)
        skip_metadata = rel == Path(".")
        for name in filenames:
            source_file = Path(current) / name
            if skip_metadata and _is_metadata_name(name):
                skipped.append(str(source_file))
                continue
            try:
                shutil.copy2(source_file, destination_dir / name)
            except OSError as exc:
                raise AssetImportError(
                    "archive_extract_failed",
                    f"复制来源文件失败：{source_file}（{exc}）",
                ) from exc
    return skipped


def _move_extracted_content(content_root: Path, session: Path) -> list[str]:
    """把解压内容整体移入会话根；只跳过根层的元数据文件。"""
    skipped: list[str] = []
    for entry in sorted(content_root.iterdir()):
        if entry.is_file() and _is_metadata_name(entry.name):
            skipped.append(str(entry))
            continue
        shutil.move(str(entry), str(session / entry.name))
    return skipped


def _count_files(root: Path) -> int:
    try:
        return sum(1 for entry in root.rglob("*") if entry.is_file())
    except OSError:
        return 0


def _create_missing_containers(
    transaction: WorkspaceTransaction, destination: Path
) -> None:
    """创建缺失的容器目录段；先 record_product 后 mkdir（与提升原语同序）。"""
    missing: list[Path] = []
    current = destination.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    empty_manifest = manifest_hash([])
    for directory in reversed(missing):
        transaction.record_product(str(directory), empty_manifest)
        directory.mkdir(parents=False)


def _cleanup_quietly(workspace_root: str | Path, session: Path) -> None:
    try:
        cleanup_staging_area(workspace_root, session)
    except (StagingError, OSError):
        pass
