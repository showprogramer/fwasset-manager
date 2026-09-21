"""导入原语测试：来源防呆、成形规则、zip 解压与提升流程。"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from fwasset.core.admission import AdmissionError
from fwasset.core.import_io import (
    AssetImportError,
    promote_import,
    stage_import_archive,
    stage_import_directory,
    stage_import_files,
)
from fwasset.core.managed_paths import MANAGED_OWNER_MARKER, managed_root
from fwasset.core.model_config import save_model_id
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.staging_io import StagingError, delete_recorded_product
from fwasset.core.workspace_transaction import WorkspaceTransaction


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    l36 = root / "L36程序"
    l36.mkdir(parents=True)
    save_model_id(l36, "l36")
    save_platform_config(l36, [PlatformDefaults("标准单机芯3D", {"主板程序": "v1"})])
    v1 = l36 / "通用" / "主板程序" / "v1"
    v1.mkdir(parents=True)
    (v1 / "rom.bin").write_bytes(b"ROM")
    return root


def _zip_file(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _staging_sessions(ws: Path) -> list[Path]:
    root = managed_root(ws, "staging")
    if not root.is_dir():
        return []
    return sorted(
        child for child in root.iterdir() if child.name != MANAGED_OWNER_MARKER
    )


# ---------------------------------------------------------------------------
# stage_import_files
# ---------------------------------------------------------------------------


def test_stage_files_copies_and_reports_metadata(ws: Path, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rom").write_bytes(b"ROM")
    (src / "说明.txt").write_text("说明", encoding="utf-8")
    (src / "程序信息.toml").write_text('vendor = "x"', encoding="utf-8")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_files(
            tx, ws, [src / "main.rom", src / "说明.txt", src / "程序信息.toml"]
        )
        session = Path(staged["session"])
        assert (session / "main.rom").read_bytes() == b"ROM"
        assert (session / "说明.txt").is_file()
        assert not (session / "程序信息.toml").exists()
        assert staged["skipped_metadata"] == [str(src / "程序信息.toml")]


def test_stage_files_duplicate_name_rejected(ws: Path, tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "Data.bin").write_bytes(b"1")
    (b / "data.bin").write_bytes(b"2")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_files(tx, ws, [a / "Data.bin", b / "data.bin"])
        assert exc_info.value.code == "duplicate_name"
        assert _staging_sessions(ws) == []


def test_stage_files_empty_rejected(ws: Path):
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_files(tx, ws, [])
        assert exc_info.value.code == "empty_source"
        assert _staging_sessions(ws) == []


def test_stage_files_missing_file_rejected(ws: Path, tmp_path: Path):
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_files(tx, ws, [tmp_path / "不存在.bin"])
        assert exc_info.value.code == "source_unreadable"
        assert _staging_sessions(ws) == []


def test_stage_files_managed_source_rejected(ws: Path):
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        outside = managed_root(ws, "staging") / "外来" / "a.bin"
        outside.parent.mkdir()
        outside.write_bytes(b"X")
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_files(tx, ws, [outside])
        assert exc_info.value.code == "source_managed"


def test_stage_requires_active_transaction(ws: Path, tmp_path: Path):
    idle = WorkspaceTransaction(ws, operation="import_asset")
    src = tmp_path / "a.bin"
    src.write_bytes(b"A")
    with pytest.raises(StagingError):
        stage_import_files(idle, ws, [src])


# ---------------------------------------------------------------------------
# stage_import_directory
# ---------------------------------------------------------------------------


def test_stage_directory_replaces_source_layer(ws: Path, tmp_path: Path):
    src = tmp_path / "厂商包"
    (src / "子目录").mkdir(parents=True)
    (src / "main.rom").write_bytes(b"ROM")
    (src / "程序信息.toml").write_text("vendor = 'x'", encoding="utf-8")
    (src / "子目录" / "程序信息.toml").write_text("vendor = 'y'", encoding="utf-8")
    (src / "子目录" / "data.pkg").write_bytes(b"PKG")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_directory(tx, ws, src)
        session = Path(staged["session"])
        assert (session / "main.rom").is_file()
        assert (session / "子目录" / "data.pkg").is_file()
        assert (session / "子目录" / "程序信息.toml").is_file()
        assert not (session / "程序信息.toml").exists()
        assert staged["skipped_metadata"] == [str(src / "程序信息.toml")]


def test_stage_directory_empty_rejected(ws: Path, tmp_path: Path):
    src = tmp_path / "空目录"
    src.mkdir()
    (src / "程序信息.toml").write_text("vendor = 'x'", encoding="utf-8")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_directory(tx, ws, src)
        assert exc_info.value.code == "empty_source"
        assert _staging_sessions(ws) == []


def test_stage_directory_workspace_overlap_rejected(ws: Path):
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_directory(tx, ws, ws)
        assert exc_info.value.code == "source_overlap"
        assert _staging_sessions(ws) == []


def test_stage_directory_rejects_session_outside_staging(ws: Path, tmp_path: Path):
    """6A-IMP-008：传入 session 必须是本事务 staging 会话目录。"""
    src = tmp_path / "厂商包"
    src.mkdir()
    (src / "main.rom").write_bytes(b"ROM")
    outsider = tmp_path / "outside-session"
    outsider.mkdir()
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(StagingError):
            stage_import_directory(tx, ws, src, session=outsider)
        assert (outsider / "main.rom").exists() is False


def test_stage_directory_rejects_nonempty_session(ws: Path, tmp_path: Path):
    """6A-IMP-008：传入的 staging 会话在复制前必须为空。"""
    from fwasset.core.staging_io import allocate_staging_area

    src = tmp_path / "厂商包"
    src.mkdir()
    (src / "main.rom").write_bytes(b"ROM")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        session = allocate_staging_area(ws, tx)
        (session / "leftover.bin").write_bytes(b"x")
        with pytest.raises(StagingError):
            stage_import_directory(tx, ws, src, session=session)


def test_stage_directory_managed_source_rejected(ws: Path, tmp_path: Path):
    src = tmp_path / "旧版本"
    src.mkdir()
    (src / "a.bin").write_bytes(b"A")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_directory(tx, ws, src)
        assert exc_info.value.code == "source_managed"
        assert _staging_sessions(ws) == []


# ---------------------------------------------------------------------------
# stage_import_archive
# ---------------------------------------------------------------------------


def test_stage_archive_strips_single_wrapper(ws: Path, tmp_path: Path):
    archive = _zip_file(
        tmp_path / "包.zip",
        {"厂商包/main.rom": b"ROM", "厂商包/子/data.pkg": b"PKG"},
    )
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_archive(tx, ws, archive)
        session = Path(staged["session"])
        assert (session / "main.rom").is_file()
        assert (session / "子" / "data.pkg").is_file()
        assert staged["skipped_metadata"] == []


def test_stage_archive_keeps_root_when_files_present(ws: Path, tmp_path: Path):
    archive = _zip_file(tmp_path / "混合.zip", {"a.bin": b"A", "目录/b.bin": b"B"})
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_archive(tx, ws, archive)
        session = Path(staged["session"])
        assert (session / "a.bin").is_file()
        assert (session / "目录" / "b.bin").is_file()


def test_stage_archive_no_strip_when_wrapper_has_sibling_file(
    ws: Path, tmp_path: Path
):
    archive = _zip_file(
        tmp_path / "带文件.zip", {"厂商包/main.rom": b"R", "说明.txt": b"T"}
    )
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_archive(tx, ws, archive)
        session = Path(staged["session"])
        assert (session / "厂商包" / "main.rom").is_file()
        assert (session / "说明.txt").is_file()


def test_stage_archive_metadata_skipped(ws: Path, tmp_path: Path):
    archive = _zip_file(
        tmp_path / "元数据.zip",
        {"程序信息.toml": b"vendor='x'", "main.rom": b"R"},
    )
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_archive(tx, ws, archive)
        session = Path(staged["session"])
        assert (session / "main.rom").is_file()
        assert not (session / "程序信息.toml").exists()
        assert len(staged["skipped_metadata"]) == 1
        assert staged["skipped_metadata"][0].endswith("程序信息.toml")


def test_stage_archive_empty_rejected(ws: Path, tmp_path: Path):
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_archive(tx, ws, _zip_file(tmp_path / "空.zip", {}))
        assert exc_info.value.code == "empty_source"
        assert _staging_sessions(ws) == []

        with pytest.raises(AssetImportError) as exc_info:
            stage_import_archive(
                tx, ws, _zip_file(tmp_path / "空目录.zip", {"空目录/": b""})
            )
        assert exc_info.value.code == "empty_source"
        assert _staging_sessions(ws) == []


def test_stage_archive_corrupt_rejected(ws: Path, tmp_path: Path):
    archive = tmp_path / "坏.zip"
    archive.write_bytes(b"not a zip")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_archive(tx, ws, archive)
        assert exc_info.value.code == "archive_extract_failed"
        assert _staging_sessions(ws) == []


def test_stage_archive_unsupported_suffix_rejected(ws: Path, tmp_path: Path):
    archive = tmp_path / "包.7z"
    archive.write_bytes(b"whatever")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        with pytest.raises(AssetImportError) as exc_info:
            stage_import_archive(tx, ws, archive)
        assert exc_info.value.code == "archive_unsupported"
        assert _staging_sessions(ws) == []


# ---------------------------------------------------------------------------
# promote_import
# ---------------------------------------------------------------------------


def test_promote_full_flow_with_container_creation(ws: Path, tmp_path: Path):
    src = tmp_path / "main.rom"
    src.write_bytes(b"ROM")
    target = ws / "L36程序" / "通用" / "新手柄" / "v1"
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_files(tx, ws, [src])
        result = promote_import(tx, ws, ws, staged["session"], target)
        assert result == target.resolve()
        assert (target / "main.rom").read_bytes() == b"ROM"
        assert not Path(staged["session"]).exists()
        product_paths = {
            os.path.normcase(product["path"]) for product in tx.products
        }
        assert os.path.normcase(str(target.parent)) in product_paths
        assert os.path.normcase(str(target)) in product_paths
        tx.commit()


def test_promote_container_cleanup_via_recorded_product(ws: Path, tmp_path: Path):
    src = tmp_path / "main.rom"
    src.write_bytes(b"ROM")
    target = ws / "L36程序" / "通用" / "新模块" / "v1"
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_files(tx, ws, [src])
        promote_import(tx, ws, ws, staged["session"], target)
        delete_recorded_product(tx, ws, target)
        delete_recorded_product(tx, ws, target.parent)
        assert not target.parent.exists()
        tx.commit()


def test_promote_admission_failure_keeps_session(ws: Path, tmp_path: Path):
    src = tmp_path / "main.rom"
    src.write_bytes(b"ROM")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_files(tx, ws, [src])
        bad = ws / "L36程序" / "通用" / "叶子"
        with pytest.raises(AdmissionError):
            promote_import(tx, ws, ws, staged["session"], bad)
        assert Path(staged["session"]).is_dir()

        good = ws / "L36程序" / "通用" / "叶子" / "v1"
        promote_import(tx, ws, ws, staged["session"], good)
        assert (good / "main.rom").read_bytes() == b"ROM"
        tx.commit()


def test_promote_existing_target_rejected(ws: Path, tmp_path: Path):
    src = tmp_path / "main.rom"
    src.write_bytes(b"ROM")
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_files(tx, ws, [src])
        occupied = ws / "L36程序" / "通用" / "主板程序" / "v1"
        with pytest.raises(AdmissionError) as exc_info:
            promote_import(tx, ws, ws, staged["session"], occupied)
        assert exc_info.value.code == "path_exists"
        assert Path(staged["session"]).is_dir()


def test_promote_to_custom_scheme(ws: Path, tmp_path: Path):
    scheme = ws / "L36程序" / "定制" / "酒店版"
    scheme.mkdir(parents=True)
    archive = _zip_file(tmp_path / "包.zip", {"厂商包/main.rom": b"ROM"})
    target = scheme / "主板程序" / "v1"
    with WorkspaceTransaction(ws, operation="import_asset") as tx:
        staged = stage_import_archive(tx, ws, archive)
        promote_import(tx, ws, ws, staged["session"], target)
        assert (target / "main.rom").is_file()
        tx.commit()
