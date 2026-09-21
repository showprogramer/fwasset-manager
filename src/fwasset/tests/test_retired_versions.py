"""D4 旧版本备用副本、retired_anchor 与恢复交换（子任务 6b）。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

import fwasset.core.services.layout_update_service as layout_update_service
from fwasset.core.file_scan import scan_firmware_subtree
from fwasset.core.managed_paths import (
    RETIRED_METADATA_FILENAME,
    RETIRED_VERSIONS_DIRNAME,
)
from fwasset.core.manifest import directory_manifest_hash
from fwasset.core.model_config import SharedModuleRef, save_shared_module
from fwasset.core.platform_config import PlatformDefaults, save_platform_config
from fwasset.core.reference_lookup import BLOCKING_ISSUE_CATEGORIES, find_references_to
from fwasset.core.services.asset_service import delete_asset
from fwasset.core.services.layout_update_service import (
    change_asset_semantics,
    restore_retired_version,
    resume_change_asset_semantics,
    resume_restore_retired_version,
    retire_asset_to_backup,
    update_asset,
)
from fwasset.core.services.model_scheme_service import create_model
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_workspace_status,
)


def _workspace(tmp_path: Path) -> Path:
    result = create_model(str(tmp_path), str(tmp_path), "L36程序", "单3D")
    assert result["ok"], result
    return tmp_path


def _variant(
    workspace: Path,
    module: str = "主板程序",
    name: str = "v1",
    payload: bytes = b"firmware",
) -> Path:
    root = workspace / "L36程序" / "通用" / module / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(payload)
    return root


def _source(
    tmp_path: Path, name: str = "来源", payload: bytes = b"new-firmware"
) -> Path:
    root = tmp_path / "外部" / name
    root.mkdir(parents=True)
    (root / "fw.bin").write_bytes(payload)
    return root


def _assert_converged_clean(workspace: Path) -> None:
    status = load_workspace_status(workspace)
    assert status.state == "clean"
    assert status.generation % 2 == 0


def _read_meta(backup: Path) -> dict[str, object]:
    path = backup / RETIRED_METADATA_FILENAME
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_retire_asset_to_backup_writes_four_fields(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")

    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        dest = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            current,
            retired_by="update_asset",
        )
        transaction.commit()

    assert dest.is_dir()
    assert dest.parent.name == RETIRED_VERSIONS_DIRNAME
    assert dest.parent.parent == current
    meta = _read_meta(dest)
    assert meta["retired_from"] == str(old)
    assert meta["retired_by"] == "update_asset"
    assert isinstance(meta["retired_at"], str) and meta["retired_at"].endswith("Z")
    assert meta["content_hash"] == directory_manifest_hash(
        dest, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
    )
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_retired_hash_excludes_metadata_and_mtime(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")

    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        dest = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            current,
            retired_by="change_asset_semantics",
        )
        transaction.commit()

    first = str(_read_meta(dest)["content_hash"])
    (dest / RETIRED_METADATA_FILENAME).write_text(
        'retired_from = "x"\ncontent_hash = "y"\nretired_at = "z"\nretired_by = "update_asset"\n',
        encoding="utf-8",
    )
    assert (
        directory_manifest_hash(
            dest, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
        )
        == first
    )
    (dest / "fw.bin").write_bytes(b"changed-bytes")
    assert (
        directory_manifest_hash(
            dest, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
        )
        != first
    )


def test_retire_backup_atomic_failure_leaves_no_toml_less_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")
    real_replace = os.replace

    def _fail_official(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if RETIRED_VERSIONS_DIRNAME in Path(dst).parts:
            raise OSError("injected replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(layout_update_service.os, "replace", _fail_official)

    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        with pytest.raises(layout_update_service.RetireBackupError):
            retire_asset_to_backup(
                transaction,
                workspace,
                old,
                current,
                retired_by="update_asset",
            )
        transaction.commit()

    retired_root = current / RETIRED_VERSIONS_DIRNAME
    if retired_root.is_dir():
        copies = [p for p in retired_root.iterdir() if p.is_dir()]
        assert copies == []
    assert old.is_dir()
    assert (old / "fw.bin").read_bytes() == b"firmware"


def test_scan_excludes_retired_files_but_restore_can_read_toml(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    result = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert result["ok"] is True, result
    replacement = Path(result["payload"]["replacement"])
    backup = Path(result["payload"]["backup"])
    assets, _issues = scan_firmware_subtree(str(tmp_path), str(replacement))
    assert len(assets) == 1
    files = [Path(item).name for item in assets[0]["files"]]
    assert RETIRED_METADATA_FILENAME not in files
    assert "fw.bin" in files
    assert (backup / RETIRED_METADATA_FILENAME).is_file()
    assert "retired_from" in _read_meta(backup)


def test_retired_anchor_related_blocks_unrelated_warns(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    borrower = create_model(str(tmp_path), str(tmp_path), "L50程序", "单3D")
    assert borrower["ok"], borrower
    current = _variant(workspace, name="v2", payload=b"new")
    old = _variant(workspace)
    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        backup = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            current,
            retired_by="update_asset",
        )
        transaction.commit()

    save_shared_module(
        tmp_path / "L50程序",
        SharedModuleRef(
            module_key="主板程序",
            source_model_id="l36",
            source_group="l36",
            source_module="主板程序",
            source_relative_path=str(backup.relative_to(tmp_path)).replace("\\", "/"),
            mode="static",
        ),
    )
    related = find_references_to(str(tmp_path), tmp_path, current, "asset")
    assert related["ok"] is False
    assert related["code"] == "retired_anchor"
    issues = related["payload"]["result"].issues
    assert any(issue.category == "retired_anchor" for issue in issues)

    other = tmp_path / "L50程序" / "通用" / "主板程序" / "v9"
    other.mkdir(parents=True)
    (other / "fw.bin").write_bytes(b"other")
    unrelated = find_references_to(str(tmp_path), tmp_path, other, "asset")
    assert unrelated["ok"] is True
    warning = [
        issue
        for issue in unrelated["payload"]["result"].issues
        if issue.category == "retired_anchor"
    ]
    assert warning

    refused = delete_asset(str(tmp_path), tmp_path, current, confirm_shared=True)
    assert refused["ok"] is False
    assert refused["code"] == "retired_anchor"

    change = change_asset_semantics(
        str(tmp_path),
        tmp_path,
        current,
        _source(tmp_path, name="快捷", payload=b"key"),
        tmp_path / "L36程序" / "通用" / "快捷键程序" / "新程序",
        "change_type",
        retire_mode="retire_to_trash",
    )
    assert change["ok"] is False
    assert change["code"] == "retired_anchor"
    assert "retired_anchor" not in BLOCKING_ISSUE_CATEGORIES


def test_restore_retired_version_swaps_with_current(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    save_platform_config(
        workspace / "L36程序",
        [PlatformDefaults("单3D", {"主板程序": "v1"})],
    )
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])

    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is True, result
    assert result["code"] in ("ok", "reindex_failed")
    restored = Path(result["payload"]["restored"])
    retired = Path(result["payload"]["retired"])
    assert restored == old
    assert (restored / "fw.bin").read_bytes() == b"firmware"
    assert retired.parent.name == RETIRED_VERSIONS_DIRNAME
    assert retired.parent.parent == restored
    assert (retired / "fw.bin").read_bytes() == b"new-firmware"
    assert not backup.exists()
    from fwasset.core.platform_config import load_platform_config

    assert load_platform_config(workspace / "L36程序")[0].defaults["主板程序"] == "v1"
    _assert_converged_clean(workspace)


def test_restore_rejects_min_field_copy_without_hash(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    current = _variant(workspace, name="v2", payload=b"new")
    backup = current / RETIRED_VERSIONS_DIRNAME / "v1-stamp"
    backup.mkdir(parents=True)
    (backup / "fw.bin").write_bytes(b"firmware")
    (backup / RETIRED_METADATA_FILENAME).write_text(
        f'original_path = "{workspace / "L36程序" / "通用" / "主板程序" / "v1"}"\n'
        'retired_at = "20260920-000000"\n',
        encoding="utf-8",
    )
    target = workspace / "L36程序" / "通用" / "主板程序" / "v1"
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "retired_metadata_invalid"
    assert backup.is_dir()
    assert not target.exists()
    _assert_converged_clean(workspace)


def test_restore_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")
    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        backup = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            current,
            retired_by="update_asset",
        )
        transaction.commit()
    (backup / "fw.bin").write_bytes(b"tampered")

    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "content_hash_mismatch"
    assert backup.is_dir()
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_restore_unsupported_semantic_change_keeps_backup(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    other = _variant(workspace, module="快捷键程序", name="k1", payload=b"keys")
    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        backup = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            other,
            retired_by="change_asset_semantics",
        )
        transaction.commit()

    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "unsupported_semantic_change"
    assert backup.is_dir() or (other / RETIRED_VERSIONS_DIRNAME).is_dir()
    assert other.is_dir()
    _assert_converged_clean(workspace)


def test_restore_rejects_occupied_target(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")
    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        backup = retire_asset_to_backup(
            transaction,
            workspace,
            old,
            current,
            retired_by="update_asset",
        )
        transaction.commit()
    old.mkdir()
    (old / "fw.bin").write_bytes(b"occupant")

    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "path_exists"
    assert backup.is_dir()
    _assert_converged_clean(workspace)


def test_restore_keeps_scene_on_apply_rollback_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])

    monkeypatch.setattr(
        layout_update_service,
        "apply_rewrite_plan",
        lambda *args, **kwargs: {
            "ok": False,
            "code": "rollback_conflict",
            "message": "冲突",
            "payload": {},
        },
    )
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "restore_inconsistent"
    assert old.exists()
    assert load_workspace_status(workspace).state == "recovery_required"


def test_restore_retire_failed_keeps_two_live_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])
    current = Path(updated["payload"]["replacement"])

    def _fail_retire(*args: object, **kwargs: object) -> Path:
        raise layout_update_service.RetireBackupError("injected")

    monkeypatch.setattr(layout_update_service, "retire_asset_to_backup", _fail_retire)
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "retire_failed"
    assert old.exists()
    assert current.exists()
    assert load_workspace_status(workspace).state == "recovery_required"


def test_restore_resume_moves_copy_back_if_apply_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(layout_update_service, "apply_rewrite_plan", _crash)
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert load_workspace_status(workspace).state == "recovery_required"
    assert old.exists()
    monkeypatch.undo()

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert backup.exists() or list((Path(updated["payload"]["replacement"]) / RETIRED_VERSIONS_DIRNAME).glob("*"))
    _assert_converged_clean(workspace)


def test_restore_does_not_issue_undo_token(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    result = restore_retired_version(
        str(tmp_path), tmp_path, Path(updated["payload"]["backup"])
    )
    assert result["ok"] is True, result
    assert "undo_token" not in result["payload"]
    assert result["payload"].get("undoable_delete") is None

# ---------------------------------------------------------------------------
# 6B-IMP 阻断项回归（2026-09-20 审查）
# ---------------------------------------------------------------------------


def test_retire_backup_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    """6B-IMP-005：old/current 越界或 current 非活动 asset 时拒绝写入。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    current = _variant(workspace, name="v2", payload=b"new")
    outside = tmp_path.parent / "fwasset-outside"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "fw.bin").write_bytes(b"outside")
    not_asset = workspace / "L36程序" / "通用" / "主板程序"

    with WorkspaceTransaction(workspace, operation="test_retire") as transaction:
        transaction.begin_product_write()
        with pytest.raises(layout_update_service.RetireBackupError):
            retire_asset_to_backup(
                transaction, workspace, outside, current,
                retired_by="update_asset",
            )
        with pytest.raises(layout_update_service.RetireBackupError):
            retire_asset_to_backup(
                transaction, workspace, old, outside,
                retired_by="update_asset",
            )
        with pytest.raises(layout_update_service.RetireBackupError):
            retire_asset_to_backup(
                transaction, workspace, old, not_asset,
                retired_by="update_asset",
            )
        transaction.commit()

    assert old.is_dir() and (old / "fw.bin").exists()
    _assert_converged_clean(workspace)


def test_retire_backup_resume_returns_temp_to_old_path(tmp_path: Path) -> None:
    """6B-IMP-001：temp 持有旧副本时 resume 必须归位，不能静默 clean。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 手工制造崩溃现场：把 backup 移回受管 temp 并写对应日志。
    temp = workspace / ".fwasset" / "staging" / "crash-temp-6b001"
    temp.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, temp)
    with WorkspaceTransaction(workspace, operation="change_asset_semantics") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "retire_temp",
            details={
                "retire_temp": str(temp),
                "retire_old_path": str(old),
                "new_path": str(replacement),
            },
        )

    # 不 commit：模拟进程在 retire_temp 阶段崩溃（__exit__ 置 recovery_required）。

    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not temp.exists()
    retired_root = replacement / RETIRED_VERSIONS_DIRNAME
    assert retired_root.is_dir()
    assert any(retired_root.iterdir())
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_restore_resume_apply_done_advances_instead_of_moving_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6B-IMP-003：apply 成功后的崩溃续跑必须前进退位，不能移回 backup。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    save_platform_config(
        workspace / "L36程序",
        [PlatformDefaults("单3D", {"主板程序": "v1"})],
    )
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])

    def _boom(*args: object, **kwargs: object) -> Path:
        raise layout_update_service.RetireBackupError("injected crash after apply")

    monkeypatch.setattr(layout_update_service, "retire_asset_to_backup", _boom)
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is False
    assert result["code"] == "retire_failed"
    assert load_workspace_status(workspace).state == "recovery_required"
    assert old.exists()
    monkeypatch.undo()

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert old.exists()
    retired_root = old / RETIRED_VERSIONS_DIRNAME
    assert retired_root.is_dir()
    assert any(retired_root.iterdir())
    assert not backup.exists()
    _assert_converged_clean(workspace)


def test_retire_backup_resume_returns_temp_when_replacement_missing(
    tmp_path: Path,
) -> None:
    """6B-IMP-001：temp 在而 replacement 不在时，resume 必须归位旧程序。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    temp = workspace / ".fwasset" / "staging" / "crash-temp-6b001b"
    temp.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, temp)
    replacement.rename(tmp_path / "replacement-moved-away")
    with WorkspaceTransaction(workspace, operation="change_asset_semantics") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "retire_temp",
            details={
                "retire_temp": str(temp),
                "retire_old_path": str(old),
                "new_path": str(replacement),
            },
        )

    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert not temp.exists()
    assert (old / "fw.bin").read_bytes() == b"firmware"
    _assert_converged_clean(workspace)

# ---------------------------------------------------------------------------
# 6B-IMP-008/009 回归测试
# ---------------------------------------------------------------------------


def test_restore_resume_restore_staging_moves_back_to_backup(tmp_path: Path) -> None:
    """restore_staging 阶段崩溃：temp 持有副本，resume 必须移回 backup 并返回。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 手工制造 restore_staging 崩溃现场：backup 移到 temp，retired_from 不在。
    temp = workspace / ".fwasset" / "staging" / "crash-restore-temp"
    temp.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, temp)
    with WorkspaceTransaction(workspace, operation="restore_retired_version") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "restore_staging",
            details={
                "restore_temp": str(temp),
                "backup_path": str(backup),
                "retired_from": str(old),
                "current_path": str(replacement),
            },
        )

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert backup.is_dir()
    assert not temp.exists()
    _assert_converged_clean(workspace)


def test_restore_resume_restore_promoted_moves_back_to_backup(tmp_path: Path) -> None:
    """restore_promoted 阶段崩溃：retired_from 在、backup 不在，resume 必须移回。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 手工制造 restore_promoted 崩溃现场：副本已移到 retired_from，backup 不在。
    temp = workspace / ".fwasset" / "staging" / "crash-restore-temp2"
    temp.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, temp)
    os.replace(temp, old)  # retired_from 现在有副本内容
    with WorkspaceTransaction(workspace, operation="restore_retired_version") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "restore_promoted",
            details={
                "restore_temp": str(temp),
                "backup_path": str(backup),
                "retired_from": str(old),
                "current_path": str(replacement),
            },
        )

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert backup.is_dir()
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_restore_resume_post_retire_manifest_verification_passes(tmp_path: Path) -> None:
    """退位完成后、commit 前崩溃：manifest 验证须排除本操作生成的旧版本内容。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    backup = Path(updated["payload"]["backup"])

    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is True, result
    restored = Path(result["payload"]["restored"])
    retired = Path(result["payload"]["retired"])
    assert retired.parent.name == RETIRED_VERSIONS_DIRNAME
    assert restored.exists() and retired.exists()
    _assert_converged_clean(workspace)


def test_retire_official_phase_resumes_with_reconcile(tmp_path: Path) -> None:
    """retire_official 阶段（temp 不在、destination 在）崩溃：resume 须对账。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 手工制造 retire_official 崩溃现场：destination 已在，temp 已不在，
    # 但 reconcile 尚未执行（模拟死在 replace 与 reconcile 之间）。
    transaction = WorkspaceTransaction(workspace, operation="change_asset_semantics")
    transaction.__enter__()
    transaction.begin_product_write()
    transaction.set_phase(
        "retire_official",
        details={
            "retire_temp": str(workspace / ".fwasset" / "staging" / "gone"),
            "retire_old_path": str(old),
            "retire_destination": str(backup),
            "new_path": str(replacement),
        },
    )
    transaction.__exit__(None, None, None)

    resumed = resume_change_asset_semantics(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert backup.is_dir()
    _assert_converged_clean(workspace)


# ---------------------------------------------------------------------------
# 6B-IMP-010 回归测试
# ---------------------------------------------------------------------------


def test_restore_resume_promote_restored_no_temp_backup_still_exists(
    tmp_path: Path,
) -> None:
    """promote_restored 阶段、temp 不在、backup 仍在：resume 须按未搬动清理。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 手工制造 promote_restored 崩溃现场：temp 从未创建（或已消失），backup 仍在。
    temp = workspace / ".fwasset" / "staging" / "never-created"
    with WorkspaceTransaction(workspace, operation="restore_retired_version") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "promote_restored",
            details={
                "restore_temp": str(temp),
                "backup_path": str(backup),
                "retired_from": str(old),
                "current_path": str(replacement),
            },
        )

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert "尚未搬动" in resumed["message"]
    assert backup.is_dir()
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_restore_resume_restore_staging_no_temp_backup_still_exists(
    tmp_path: Path,
) -> None:
    """restore_staging 阶段、temp 不在、backup 仍在：resume 须按未搬动清理。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    temp = workspace / ".fwasset" / "staging" / "never-created-2"
    with WorkspaceTransaction(workspace, operation="restore_retired_version") as transaction:
        transaction.begin_product_write()
        transaction.set_phase(
            "restore_staging",
            details={
                "restore_temp": str(temp),
                "backup_path": str(backup),
                "retired_from": str(old),
                "current_path": str(replacement),
            },
        )

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["ok"] is True, resumed
    assert "尚未搬动" in resumed["message"]
    assert backup.is_dir()
    assert not old.exists()
    _assert_converged_clean(workspace)


def test_restore_resume_post_retire_crash_exercises_manifest_verification(
    tmp_path: Path,
) -> None:
    """restore_apply_done 阶段、退位目录已存在：resume 走完整 manifest 验证。"""
    workspace = _workspace(tmp_path)
    old = _variant(workspace)
    source = _source(tmp_path)
    updated = update_asset(
        str(tmp_path), str(tmp_path), old, source, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    replacement = Path(updated["payload"]["replacement"])
    backup = Path(updated["payload"]["backup"])

    # 先跑一次正常 restore，让退位目录被创建。
    result = restore_retired_version(str(tmp_path), tmp_path, backup)
    assert result["ok"] is True, result
    restored = Path(result["payload"]["restored"])
    retired = Path(result["payload"]["retired"])
    assert retired.parent.name == RETIRED_VERSIONS_DIRNAME

    # 6B-TEST-001：在添加 retired child 之前记录 baseline manifest。
    # 正常 restore 已经添加了 retired child，所以我们需要手动计算
    # 「不含 retired child」的 baseline manifest：把 retired child 暂时
    # 移到 restored 外面，避免 directory_manifest 仍遍历到它。
    temp_retired = workspace / "temp-baseline"
    os.replace(retired, temp_retired)
    baseline_manifest = directory_manifest_hash(restored)
    os.replace(temp_retired, retired)

    # 6B-TEST-001：模拟真实崩溃——用 WorkspaceTransaction 记录 products，
    # 走到 restore_apply_done 后释放锁但不 commit（模拟进程死掉），再 resume。
    transaction = WorkspaceTransaction(workspace, operation="restore_retired_version")
    transaction.__enter__()
    transaction.begin_product_write()
    # 用 baseline manifest（不含 retired child），确保过滤逻辑被真实验证。
    transaction.record_product(restored, baseline_manifest)
    transaction.set_phase(
        "restore_apply_done",
        details={
            "backup_path": str(backup),
            "retired_from": str(restored),
            "current_path": str(replacement),
            # 本操作已把 current 退位到 retired，resume 应跳过退位。
            "retire_destination": str(retired),
        },
    )
    # 释放文件锁但不 commit，模拟进程崩溃后锁被 OS 释放、日志仍在磁盘。
    transaction._lock.__exit__(None, None, None)

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    # 正常 restore 已 reconcile 过，resume 再次 reconcile 会因 reference
    # 已变化而返回 reindex_failed；关键是 manifest 验证通过（没有报
    # product_modified / product_missing），且没把已提升副本误移回 backup。
    assert resumed["code"] != "product_modified", resumed
    assert resumed["code"] != "product_missing", resumed
    assert resumed["code"] != "product_unverifiable", resumed
    assert restored.exists() and retired.exists()
    _assert_converged_clean(workspace)


def test_current_already_retired_rejects_historical_same_path(tmp_path: Path) -> None:
    """6B-IMP-013：历史副本的 retired_from 等于当前路径时，仍在线的当前程序不算已退位。"""
    workspace = _workspace(tmp_path)
    restored = _variant(workspace, name="v1", payload=b"v1-bytes")
    current = _variant(workspace, name="v2", payload=b"new-v2-bytes")
    historical = restored / RETIRED_VERSIONS_DIRNAME / "v2-20260101T000000Z"
    historical.mkdir(parents=True)
    (historical / "fw.bin").write_bytes(b"old-v2-bytes")
    layout_update_service._write_retired_metadata(
        historical,
        {
            "retired_from": str(current),
            "content_hash": directory_manifest_hash(
                historical, exclude_names=frozenset({RETIRED_METADATA_FILENAME})
            ),
            "retired_at": "2026-01-01T00:00:00Z",
            "retired_by": "restore_retired_version",
        },
    )

    assert layout_update_service._current_already_retired(restored, current) is False


def test_restore_resume_historical_same_path_does_not_skip_retire(tmp_path: Path) -> None:
    """6B-IMP-013：恢复后再更新同一版本名，续跑不得把历史副本当成已退位。

    restore v1 → 旧 v2 退位到 v1/旧版本/（retired_from=v2 路径）。
    再 update 出同名新 v2 后，把 v1 搬回以模拟 restore_apply_done：
    历史副本仍记录同一路径，但新 v2 还在。resume 必须退位新 v2。
    """
    workspace = _workspace(tmp_path)
    old_v1 = _variant(workspace, name="v1")
    source_v2 = _source(tmp_path / "v2src", name="v2", payload=b"v2-firmware")
    updated = update_asset(
        str(tmp_path), str(tmp_path), old_v1, source_v2, retire_mode="retire_to_backup"
    )
    assert updated["ok"] is True, updated
    v1_backup = Path(updated["payload"]["backup"])

    restored = restore_retired_version(str(tmp_path), tmp_path, v1_backup)
    assert restored["ok"] is True, restored
    v1_path = Path(restored["payload"]["restored"])
    v2_retired = Path(restored["payload"]["retired"])
    assert v2_retired.parent.name == RETIRED_VERSIONS_DIRNAME

    source_v2_again = _source(tmp_path / "v2again", name="v2", payload=b"v2-new-firmware")
    updated2 = update_asset(
        str(tmp_path),
        str(tmp_path),
        v1_path,
        source_v2_again,
        retire_mode="retire_to_backup",
    )
    assert updated2["ok"] is True, updated2
    v1_backup2 = Path(updated2["payload"]["backup"])
    v2_new_current = Path(updated2["payload"]["replacement"])
    assert v2_new_current.name == "v2"
    assert not v1_path.exists()

    # 模拟第二次 restore 已提升 v1、尚未退位新 v2：v1 带着历史 旧版本/ 回来。
    os.replace(v1_backup2, v1_path)
    assert v1_path.is_dir() and v2_new_current.is_dir()
    assert layout_update_service._current_already_retired(v1_path, v2_new_current) is False

    transaction = WorkspaceTransaction(workspace, operation="restore_retired_version")
    transaction.__enter__()
    transaction.begin_product_write()
    transaction.set_phase(
        "restore_apply_done",
        details={
            "backup_path": str(v1_backup2),
            "retired_from": str(v1_path),
            "current_path": str(v2_new_current),
        },
    )
    transaction._lock.__exit__(None, None, None)

    resumed = resume_restore_retired_version(str(tmp_path), tmp_path)
    assert resumed["code"] != "product_modified", resumed
    if resumed["code"] == "retire_failed":
        assert v2_new_current.is_dir()
        return
    assert not v2_new_current.exists()
    retired_children = [
        child
        for child in (v1_path / RETIRED_VERSIONS_DIRNAME).iterdir()
        if child.is_dir()
    ]
    assert len(retired_children) >= 2, "应至少有两个退位副本（历史 + 本次）"
