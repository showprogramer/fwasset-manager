from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fwasset.core import workspace_transaction
from fwasset.core.managed_paths import (
    MANAGED_OWNER_MARKER,
    is_managed_root_owned,
    managed_root,
)
from fwasset.core.workspace_transaction import (
    ManagedRootOccupiedError,
    WorkspaceBusyError,
    WorkspaceLock,
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
    capture_workspace_preview,
    load_workspace_status,
    preview_token_is_current,
    recover_interrupted_workspace,
)


def test_committed_transaction_initializes_managed_roots_and_returns_to_clean(
    tmp_path,
) -> None:
    with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
        assert transaction.status.state == "operation_in_progress"
        assert transaction.status.generation == 0

        transaction.begin_product_write()
        assert transaction.status.generation == 1
        transaction.commit()

    status = load_workspace_status(tmp_path)
    assert status.state == "clean"
    assert status.generation == 2
    assert status.operation is None
    for kind in ("staging", "incomplete_candidate", "workspace_state", "quarantine"):
        assert is_managed_root_owned(tmp_path, kind)
        assert managed_root(tmp_path, kind).is_dir()


def test_recovery_normalizes_interrupted_odd_generation_and_blocks_new_write(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
        transaction.begin_product_write()
        state_root = managed_root(tmp_path, "workspace_state")
        (state_root / "workspace-state.json").write_text(
            '{"state":"operation_in_progress"}\n', encoding="utf-8"
        )
        (state_root / "generation.json").write_text('{"generation":1}\n', encoding="utf-8")
        transaction._committed = True

    recovered = recover_interrupted_workspace(tmp_path)

    assert recovered.state == "recovery_required"
    assert recovered.generation == 2
    assert recovered.operation == "create_asset"

def test_preview_token_is_invalid_during_or_after_a_product_write(tmp_path) -> None:
    with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
        transaction.begin_product_write()
        transaction.commit()

    token = capture_workspace_preview(tmp_path)
    assert preview_token_is_current(tmp_path, token)

    with WorkspaceTransaction(tmp_path, operation="rename_asset") as transaction:
        transaction.begin_product_write()
        assert not preview_token_is_current(tmp_path, token)
        transaction.commit()

    assert not preview_token_is_current(tmp_path, token)
    assert preview_token_is_current(tmp_path, capture_workspace_preview(tmp_path))

def test_lock_rejects_reentrant_writer_then_releases_workspace(tmp_path) -> None:
    with WorkspaceLock(tmp_path, timeout_seconds=0):
        with pytest.raises(WorkspaceBusyError):
            with WorkspaceLock(tmp_path, timeout_seconds=0):
                pass

    with WorkspaceLock(tmp_path, timeout_seconds=0):
        pass


def test_preexisting_unowned_root_is_not_claimed_or_overwritten(tmp_path) -> None:
    occupied = managed_root(tmp_path, "staging")
    occupied.mkdir(parents=True)
    personal_file = occupied / "私人文件.txt"
    personal_file.write_text("保留", encoding="utf-8")

    with pytest.raises(ManagedRootOccupiedError):
        with WorkspaceTransaction(tmp_path, operation="create_asset"):
            pass

    assert personal_file.read_text(encoding="utf-8") == "保留"


def test_failed_transaction_retains_log_and_blocks_the_next_writer(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="模拟故障"):
        with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
            transaction.begin_product_write()
            raise RuntimeError("模拟故障")

    status = load_workspace_status(tmp_path)
    assert status.state == "recovery_required"
    assert status.generation == 2
    assert status.operation == "create_asset"

    with pytest.raises(WorkspaceRecoveryRequiredError):
        with WorkspaceTransaction(tmp_path, operation="rename_asset"):
            pass

def test_crashed_writer_is_recovered_from_persisted_state(tmp_path) -> None:
    script = "\n".join(
        (
            "import os",
            "from fwasset.core.workspace_transaction import WorkspaceTransaction",
            f"transaction = WorkspaceTransaction({str(tmp_path)!r}, operation='create_asset')",
            "transaction.__enter__()",
            "transaction.begin_product_write()",
            "os._exit(0)",
        )
    )
    subprocess.run([sys.executable, "-c", script], check=True)

    recovered = recover_interrupted_workspace(tmp_path)
    assert recovered.state == "recovery_required"
    assert recovered.generation == 2
    assert recovered.operation == "create_asset"


def test_atomic_replace_retries_while_preview_reader_holds_state_file(tmp_path) -> None:
    """写侧竞争：预览读者短暂持有状态文件时，原子替换须重试成功而非失败。"""
    dest = tmp_path / "workspace-state.json"
    dest.write_text('{"state":"clean"}', encoding="utf-8")
    held = threading.Event()

    def hold_state_file_open() -> None:
        with dest.open("r", encoding="utf-8"):
            held.set()
            time.sleep(0.3)

    reader = threading.Thread(target=hold_state_file_open, daemon=True)
    reader.start()
    assert held.wait(timeout=5)

    workspace_transaction._write_json_atomically(dest, {"state": "recovery_required"})
    reader.join(timeout=5)

    assert json.loads(dest.read_text(encoding="utf-8"))["state"] == "recovery_required"


def test_remove_file_retries_while_preview_reader_holds_log_open(tmp_path) -> None:
    """写侧竞争：删除操作日志时读者持句柄，同样须有界重试完成删除。"""
    dest = tmp_path / "operation.json"
    dest.write_text('{"operation":"create_asset"}', encoding="utf-8")
    held = threading.Event()

    def hold_log_open() -> None:
        with dest.open("r", encoding="utf-8"):
            held.set()
            time.sleep(0.3)

    reader = threading.Thread(target=hold_log_open, daemon=True)
    reader.start()
    assert held.wait(timeout=5)

    workspace_transaction._remove_file(dest)
    reader.join(timeout=5)

    assert not dest.exists()


def test_load_status_retries_through_transient_sharing_violation(
    tmp_path, monkeypatch
) -> None:
    """读侧竞争：替换瞬间的共享冲突须重试读取，不得误报 recovery_required。"""
    with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
        transaction.begin_product_write()
        transaction.commit()

    state_file = managed_root(tmp_path, "workspace_state") / "workspace-state.json"
    real_open = Path.open
    failures: list[str] = []

    def flaky_open(self, *args, **kwargs):
        if self == state_file and len(failures) < 2:
            failures.append("模拟原子替换瞬间的共享冲突")
            raise PermissionError(5, "模拟原子替换瞬间的共享冲突")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)

    status = load_workspace_status(tmp_path)

    assert status.state == "clean"
    assert status.generation == 2
    assert status.operation is None
    assert len(failures) == 2


def test_atomic_replace_gives_up_bounded_when_state_file_stays_locked(
    tmp_path, monkeypatch
) -> None:
    """有界性：持续占用超过重试预算后按原语义抛出，不得无限阻塞。"""
    monkeypatch.setattr(workspace_transaction, "_SHARING_RETRY_BUDGET_SECONDS", 0.05)
    dest = tmp_path / "workspace-state.json"
    dest.write_text('{"state":"clean"}', encoding="utf-8")
    held = threading.Event()
    release = threading.Event()

    def hold_until_released() -> None:
        with dest.open("r", encoding="utf-8"):
            held.set()
            release.wait(timeout=10)

    reader = threading.Thread(target=hold_until_released, daemon=True)
    reader.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(PermissionError):
            workspace_transaction._write_json_atomically(dest, {"state": "clean"})
    finally:
        release.set()
        reader.join(timeout=5)


def test_load_status_reports_recovery_required_after_persistent_lock(
    tmp_path, monkeypatch
) -> None:
    """有界性：读取侧被持续占用超过预算后，才按 recovery_required 语义报告。"""
    monkeypatch.setattr(workspace_transaction, "_SHARING_RETRY_BUDGET_SECONDS", 0.05)
    with WorkspaceTransaction(tmp_path, operation="create_asset") as transaction:
        transaction.begin_product_write()
        transaction.commit()

    state_file = managed_root(tmp_path, "workspace_state") / "workspace-state.json"
    real_open = Path.open

    def always_denied(self, *args, **kwargs):
        if self == state_file:
            raise PermissionError(5, "持续占用状态文件")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", always_denied)

    with pytest.raises(WorkspaceRecoveryRequiredError):
        load_workspace_status(tmp_path)


def _try_make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _skip_if_junction_unavailable(tmp_path: Path) -> None:
    probe_target = tmp_path / "junction-probe-target"
    probe_target.mkdir()
    if not _try_make_junction(tmp_path / "junction-probe-link", probe_target):
        pytest.skip("当前环境无法创建 junction")


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_marker_never_lands_in_external_redirect_target(tmp_path, monkeypatch) -> None:
    """mkdir 与写标记之间根被换成指向外部的 junction（复审阻断项）。

    修复契约：统一报告 ManagedRootOccupiedError；标记不得留在外部目标
    （只撤回 O_EXCL 自建文件）；不删除被搬走的真实根，也不删除 junction。
    """
    _skip_if_junction_unavailable(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    external_staging = external / "staging"
    external_staging.mkdir()

    staging_root = managed_root(workspace, "staging")
    staging_marker = staging_root / MANAGED_OWNER_MARKER
    real_open = os.open

    def open_that_swaps_root(path, flags, *args, **kwargs):
        if os.fspath(path) == str(staging_marker):
            staging_root.rename(staging_root.with_name("staging-moved"))
            assert _try_make_junction(staging_root, external_staging)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_that_swaps_root)

    with pytest.raises(ManagedRootOccupiedError):
        with WorkspaceTransaction(workspace, operation="create_asset"):
            pass

    assert not (external_staging / MANAGED_OWNER_MARKER).exists()
    assert staging_root.with_name("staging-moved").is_dir()
    assert staging_root.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_redirect_after_mkdir_reports_occupied_without_deletion(
    tmp_path, monkeypatch
) -> None:
    """mkdir 刚完成即被换成 junction：统一 occupied，且零删除、零外部写入。"""
    _skip_if_junction_unavailable(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    external_staging = external / "staging"
    external_staging.mkdir()

    staging_root = managed_root(workspace, "staging")
    real_mkdir = Path.mkdir

    def mkdir_that_swaps_root(self, *args, **kwargs):
        result = real_mkdir(self, *args, **kwargs)
        if self == staging_root:
            self.rename(self.with_name("staging-moved"))
            assert _try_make_junction(self, external_staging)
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_that_swaps_root)

    with pytest.raises(ManagedRootOccupiedError):
        with WorkspaceTransaction(workspace, operation="create_asset"):
            pass

    assert not (external_staging / MANAGED_OWNER_MARKER).exists()
    assert staging_root.with_name("staging-moved").is_dir()
    assert staging_root.exists()


def test_marker_write_failure_rolls_back_own_marker_only(
    tmp_path, monkeypatch
) -> None:
    """非竞争故障：标记写失败只撤回自建文件；绝不 rmdir 已创建的目录。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real_fsync = os.fsync
    failed: list[bool] = []

    def fsync_failing_once(fd):
        if not failed:
            failed.append(True)
            raise OSError("模拟写标记时故障")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_failing_once)

    with pytest.raises(OSError, match="模拟写标记时故障"):
        with WorkspaceTransaction(workspace, operation="create_asset"):
            pass

    state_root = managed_root(workspace, "workspace_state")
    assert not (state_root / MANAGED_OWNER_MARKER).exists()
    assert state_root.is_dir()