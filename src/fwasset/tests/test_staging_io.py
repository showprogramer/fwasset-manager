from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fwasset.core import staging_io
from fwasset.core.managed_paths import MANAGED_OWNER_MARKER, managed_root
from fwasset.core.manifest import directory_manifest
from fwasset.core.staging_io import (
    ProductCleanupConflict,
    StagingError,
    allocate_staging_area,
    cleanup_staging_area,
    delete_recorded_product,
    promote_staging,
)
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_operation_log,
)


def _staged_content(area: Path) -> None:
    (area / "main.rom").write_bytes(b"ROM")
    (area / "子").mkdir()
    (area / "子" / "extra.pkg").write_bytes(b"PKG")


def _promoted_target(workspace: Path) -> Path:
    return workspace / "通用" / "主板" / "demo"


def test_allocate_staging_area_requires_active_transaction(tmp_path) -> None:
    idle = WorkspaceTransaction(tmp_path, operation="import_asset")

    with pytest.raises(StagingError):
        allocate_staging_area(tmp_path, idle)


def test_promote_moves_content_records_product_and_cleanup_deletes(
    tmp_path,
) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        assert area.is_dir()
        assert managed_root(tmp_path, "staging") in area.parents

        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)
        transaction.begin_product_write()
        promote_staging(transaction, tmp_path, area, target)

        assert (target / "main.rom").read_bytes() == b"ROM"
        assert (target / "子" / "extra.pkg").read_bytes() == b"PKG"
        assert not area.exists()

        log = load_operation_log(tmp_path)
        assert log is not None
        assert log["operation"] == "import_asset"
        assert log["phase"] == "writing"
        recorded = [
            product
            for product in log["products"]
            if os.path.normcase(product["path"]) == os.path.normcase(str(target))
        ]
        assert len(recorded) == 1
        assert recorded[0]["manifest"]
        assert os.path.normcase(transaction.products[0]["path"]) == os.path.normcase(
            str(target)
        )

        delete_recorded_product(transaction, tmp_path, target)
        assert not target.exists()

        transaction.commit()

    assert load_operation_log(tmp_path) is None


def test_promote_requires_product_write_generation(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)

        with pytest.raises(StagingError, match="begin_product_write"):
            promote_staging(transaction, tmp_path, area, target)

        assert area.is_dir()
        assert (area / "main.rom").is_file()


def test_promote_rejects_existing_target_and_case_variants(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        parent = tmp_path / "通用"
        parent.mkdir()
        (parent / "Demo").mkdir()
        target = parent / "demo"

        transaction.begin_product_write()
        with pytest.raises(StagingError, match="已存在"):
            promote_staging(transaction, tmp_path, area, target)

        assert area.is_dir()
        assert (parent / "Demo").is_dir()
        assert transaction.products == ()


def test_promote_rejects_out_of_workspace_and_managed_targets(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with WorkspaceTransaction(workspace, operation="import_asset") as transaction:
        area = allocate_staging_area(workspace, transaction)
        transaction.begin_product_write()

        with pytest.raises(StagingError):
            promote_staging(transaction, workspace, area, outside / "demo")
        with pytest.raises(StagingError):
            promote_staging(transaction, workspace, area, workspace / ".fwasset" / "x")
        with pytest.raises(StagingError):
            promote_staging(
                transaction,
                workspace,
                area,
                managed_root(workspace, "staging") / "demo",
            )

        assert area.is_dir()


def test_promote_rejects_missing_parent(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        transaction.begin_product_write()

        with pytest.raises(StagingError, match="父目录"):
            promote_staging(transaction, tmp_path, area, tmp_path / "不存在" / "demo")


def test_delete_recorded_product_conflict_reasons(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)
        transaction.begin_product_write()
        promote_staging(transaction, tmp_path, area, target)

        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, tmp_path / "通用" / "other")
        assert caught.value.reason == "identity"

        (target / "子" / "extra.pkg").write_bytes(b"CHANGED")
        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, target)
        assert caught.value.reason == "manifest"
        assert caught.value.expected is not None
        assert caught.value.actual != caught.value.expected
        assert (target / "子" / "extra.pkg").read_bytes() == b"CHANGED"

        (target / "子" / "extra.pkg").write_bytes(b"PKG")
        delete_recorded_product(transaction, tmp_path, target)
        assert not target.exists()

        delete_recorded_product(transaction, tmp_path, target)

        transaction.commit()
        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, target)
        assert caught.value.reason == "log_state"


def test_delete_recorded_product_keeps_unexpected_file(tmp_path) -> None:
    """内容被计划外文件改变 → manifest 冲突，现场完整保留。"""
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)
        transaction.begin_product_write()
        promote_staging(transaction, tmp_path, area, target)

        foreign = target / "外来文件.txt"
        foreign.write_text("外部放入", encoding="utf-8")

        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, target)

        assert caught.value.reason == "manifest"
        assert foreign.is_file()
        assert (target / "main.rom").is_file()


def test_delete_recorded_product_aborts_when_race_adds_file_after_check(
    tmp_path, monkeypatch
) -> None:
    """校验通过后、删除期间出现计划外文件：中止并保留剩余现场。"""
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)
        transaction.begin_product_write()
        promote_staging(transaction, tmp_path, area, target)

        foreign = target / "外来文件.txt"
        foreign.write_text("外部放入", encoding="utf-8")

        real_manifest = directory_manifest

        def stale_manifest(root: Path, **kwargs):
            entries = real_manifest(root, **kwargs)
            return [entry for entry in entries if entry.relpath != "外来文件.txt"]

        monkeypatch.setattr(staging_io, "directory_manifest", stale_manifest)

        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, target)

        assert caught.value.reason == "delete_failed"
        assert foreign.is_file()
        assert target.is_dir()


def test_cleanup_staging_area_removes_tree_and_rejects_outside(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)

        cleanup_staging_area(tmp_path, area)
        assert not area.exists()

        cleanup_staging_area(tmp_path, area)

    with pytest.raises(StagingError):
        cleanup_staging_area(tmp_path, tmp_path / "not-staging")


def test_cleanup_staging_area_removes_read_only_files(tmp_path) -> None:
    """copy2 进 staging 的厂商固件保留只读属性，清理不能因此中止。"""
    import stat

    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        os.chmod(area / "main.rom", stat.S_IREAD)
        os.chmod(area / "子" / "extra.pkg", stat.S_IREAD)

        cleanup_staging_area(tmp_path, area)

        assert not area.exists()


def test_staging_root_itself_is_rejected_as_session(tmp_path) -> None:
    """staging 根（含所有权标记与其他会话）不得被当作会话目录操作。"""
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        staging_root = managed_root(tmp_path, "staging")
        leftover = staging_root / "游离文件.txt"
        leftover.write_text("遗留", encoding="utf-8")
        transaction.begin_product_write()
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)

        with pytest.raises(StagingError):
            cleanup_staging_area(tmp_path, staging_root)
        with pytest.raises(StagingError):
            promote_staging(transaction, tmp_path, staging_root, target)

        assert leftover.is_file()


def test_staging_actions_are_bound_to_the_transaction_workspace(tmp_path) -> None:
    """事务 A 不得对工作区 B 的 staging/产物执行操作（状态会记错工作区）。"""
    workspace_a = tmp_path / "ws-a"
    workspace_b = tmp_path / "ws-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    with (
        WorkspaceTransaction(workspace_a, operation="op-a") as tx_a,
        WorkspaceTransaction(workspace_b, operation="op-b") as tx_b,
    ):
        with pytest.raises(StagingError):
            allocate_staging_area(workspace_b, tx_a)

        area = allocate_staging_area(workspace_b, tx_b)
        (area / "x.bin").write_bytes(b"X")
        (workspace_b / "通用").mkdir()
        tx_a.begin_product_write()

        with pytest.raises(StagingError):
            promote_staging(tx_a, workspace_b, area, workspace_b / "通用" / "demo")
        with pytest.raises(StagingError):
            delete_recorded_product(tx_a, workspace_b, workspace_b / "通用")

        assert area.is_dir()


def _try_make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_cleanup_rejects_session_replaced_by_junction(tmp_path) -> None:
    """会话目录被换成指向 staging 根的 junction：拒绝清理，根内容零删除。"""
    def _skip_if_junction_unavailable() -> None:
        probe_target = tmp_path / "junction-probe-target"
        probe_target.mkdir()
        if not _try_make_junction(tmp_path / "junction-probe-link", probe_target):
            pytest.skip("当前环境无法创建 junction")

    _skip_if_junction_unavailable()
    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        victim = allocate_staging_area(tmp_path, transaction)
        other = allocate_staging_area(tmp_path, transaction)
        (other / "其他会话.txt").write_text("内容", encoding="utf-8")
        staging_root = managed_root(tmp_path, "staging")

        victim.rmdir()
        assert _try_make_junction(victim, staging_root)

        with pytest.raises(StagingError):
            cleanup_staging_area(tmp_path, victim)

        assert (other / "其他会话.txt").is_file()
        assert (staging_root / MANAGED_OWNER_MARKER).is_file()


def test_delete_recorded_product_rejects_out_of_workspace_target(
    tmp_path, monkeypatch
) -> None:
    """被伪造/损坏的日志指向工作区外：拒绝清理，外部内容零触碰。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("外部内容", encoding="utf-8")

    forged: tuple[dict[str, str], ...] = ({"path": str(outside), "manifest": "x"},)
    monkeypatch.setattr(WorkspaceTransaction, "products", property(lambda self: forged))

    with WorkspaceTransaction(workspace, operation="forged") as transaction:
        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, workspace, outside)

    assert caught.value.reason == "identity"
    assert (outside / "victim.txt").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_delete_aborts_when_child_dir_becomes_junction_after_check(
    tmp_path, monkeypatch
) -> None:
    """校验后子目录被换成 junction：unlink 前逐段复核必须中止，外部零删除。"""
    probe = tmp_path / "junction-probe-target"
    probe.mkdir()
    if not _try_make_junction(tmp_path / "junction-probe-link", probe):
        pytest.skip("当前环境无法创建 junction")

    with WorkspaceTransaction(tmp_path, operation="import_asset") as transaction:
        area = allocate_staging_area(tmp_path, transaction)
        _staged_content(area)
        target = _promoted_target(tmp_path)
        target.parent.mkdir(parents=True)
        transaction.begin_product_write()
        promote_staging(transaction, tmp_path, area, target)

        external = tmp_path / "external"
        external.mkdir()
        (external / "extra.pkg").write_bytes(b"external-same-name")
        (external / "precious.txt").write_bytes(b"external-important")

        real_manifest = directory_manifest
        stale = real_manifest(target)
        monkeypatch.setattr(staging_io, "directory_manifest", lambda root, **kw: stale)

        shutil.rmtree(target / "子")
        assert _try_make_junction(target / "子", external)

        with pytest.raises(ProductCleanupConflict) as caught:
            delete_recorded_product(transaction, tmp_path, target)

        assert caught.value.reason == "delete_failed"
        assert (external / "extra.pkg").is_file()
        assert (external / "precious.txt").is_file()
