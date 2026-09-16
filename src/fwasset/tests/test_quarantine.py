from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from fwasset.core.managed_paths import managed_root, should_exclude_managed_path
from fwasset.core.quarantine import (
    UNDO_WINDOW_SECONDS,
    QuarantineError,
    TrashUnavailableError,
    UndoConflictError,
    _save_quarantine_manifest,
    list_records,
    load_quarantine_manifest,
    mark_retire_committed,
    recover_on_startup,
    register_delete,
    register_retire,
    schedule_sweep_expired,
    sweep_expired,
    undo_delete,
)
from fwasset.core.workspace_transaction import WorkspaceLock, WorkspaceTransaction


def _init_workspace(workspace_root: Path) -> None:
    with WorkspaceTransaction(workspace_root, operation="noop") as transaction:
        transaction.begin_product_write()
        transaction.commit()


def _make_asset(workspace_root: Path, name: str = "asset") -> Path:
    asset = workspace_root / name
    asset.mkdir()
    (asset / "file.bin").write_bytes(b"payload")
    return asset


def _locked_register_delete(workspace_root: Path, source: Path):
    with WorkspaceLock(workspace_root):
        return register_delete(workspace_root, source)


def _locked_register_retire(workspace_root: Path, source: Path):
    with WorkspaceLock(workspace_root):
        return register_retire(workspace_root, source)


def _locked_mark_retire_committed(workspace_root: Path, record_id: str):
    with WorkspaceLock(workspace_root):
        return mark_retire_committed(workspace_root, record_id)


def _locked_undo_delete(workspace_root: Path, record_id: str):
    with WorkspaceLock(workspace_root):
        return undo_delete(workspace_root, record_id)


def _locked_sweep_expired(workspace_root: Path):
    with WorkspaceLock(workspace_root):
        return sweep_expired(workspace_root)


def test_register_delete_persists_manifest_with_original_path_hash_status_time(
    tmp_path,
) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)

    record = _locked_register_delete(tmp_path, asset)

    assert record["kind"] == "undoable_delete"
    assert record["status"] == "pending"
    assert record["original_path"] == str(asset)
    assert record["manifest"]
    assert record["created_at"] > 0
    assert not asset.exists()

    records = load_quarantine_manifest(tmp_path)
    assert len(records) == 1
    assert records[0]["id"] == record["id"]


def test_old_format_manifest_without_optional_fields_is_readable(tmp_path) -> None:
    _init_workspace(tmp_path)
    manifest_path = managed_root(tmp_path, "workspace_state") / "quarantine-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "legacy-1",
                        "kind": "undoable_delete",
                        "workspace_root": str(tmp_path),
                        "original_path": str(tmp_path / "old"),
                        "quarantine_path": str(tmp_path / "old-q"),
                        "manifest": "deadbeef",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    records = load_quarantine_manifest(tmp_path)

    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert records[0]["created_at"] == 0.0
    assert records[0]["expires_at"] == 0.0


def test_undoable_delete_can_be_restored_within_window_with_matching_hash(tmp_path) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_delete(tmp_path, asset)

    restored = _locked_undo_delete(tmp_path, record["id"])

    assert restored["manifest"] == record["manifest"]
    assert asset.is_dir()
    assert (asset / "file.bin").read_bytes() == b"payload"
    assert load_quarantine_manifest(tmp_path) == []


def test_undo_conflict_when_target_occupied_keeps_quarantine_content(tmp_path) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_delete(tmp_path, asset)
    asset.mkdir()  # 原路径已被重新占用

    with pytest.raises(UndoConflictError):
        _locked_undo_delete(tmp_path, record["id"])

    quarantine_path = Path(record["quarantine_path"])
    assert quarantine_path.exists()
    records = load_quarantine_manifest(tmp_path)
    assert records[0]["status"] == "pending"


def test_undo_conflict_on_case_variant_target(tmp_path) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path, "Asset")
    record = _locked_register_delete(tmp_path, asset)
    (tmp_path / "asset").mkdir()  # 大小写等价占用

    with pytest.raises(UndoConflictError):
        _locked_undo_delete(tmp_path, record["id"])

    assert Path(record["quarantine_path"]).exists()


def test_transactional_retire_has_no_undo_entry(tmp_path) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)

    assert record["kind"] == "transactional_retire"
    assert record["expires_at"] == 0.0
    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, record["id"])


def test_retire_committed_and_sent_to_recycle_bin_on_sweep(tmp_path, monkeypatch) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)
    _locked_mark_retire_committed(tmp_path, record["id"])

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert len(processed) == 1
    assert processed[0]["status"] == "sent"
    assert sent_paths == [Path(record["quarantine_path"])]
    assert load_quarantine_manifest(tmp_path) == []


def test_retire_send_failure_keeps_manifest_for_startup_recovery(tmp_path, monkeypatch) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)
    _locked_mark_retire_committed(tmp_path, record["id"])

    def _boom(path: Path) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr("fwasset.core.quarantine._send_to_system_recycle_bin", _boom)

    processed = _locked_sweep_expired(tmp_path)

    assert processed[0]["status"] == "send_failed"
    remaining = load_quarantine_manifest(tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["status"] == "send_failed"

    # 启动恢复不得因送出失败重新承诺 update 可撤销。
    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, record["id"])


def test_trash_unavailable_when_workspace_is_volume_root(tmp_path, monkeypatch) -> None:
    volume_root = tmp_path.anchor
    asset = tmp_path / "asset"
    asset.mkdir()

    with pytest.raises(TrashUnavailableError):
        _locked_register_delete(volume_root, asset)


def test_recover_on_startup_clears_expired_and_committed_but_keeps_pending(
    tmp_path, monkeypatch
) -> None:
    _init_workspace(tmp_path)
    expired_asset = _make_asset(tmp_path, "expired")
    pending_asset = _make_asset(tmp_path, "pending")
    retire_asset = _make_asset(tmp_path, "retire")

    expired_record = _locked_register_delete(tmp_path, expired_asset)
    pending_record = _locked_register_delete(tmp_path, pending_asset)
    retire_record = _locked_register_retire(tmp_path, retire_asset)
    _locked_mark_retire_committed(tmp_path, retire_record["id"])

    records = load_quarantine_manifest(tmp_path)
    records = [
        {**record, "expires_at": time.time() - 1}
        if record["id"] == expired_record["id"]
        else record
        for record in records
    ]
    _save_quarantine_manifest(tmp_path, records)

    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin", lambda path: None
    )

    # recover_on_startup 是维护性入口，自行获取锁，调用方不得预先持锁
    # （否则同进程重入会被 WorkspaceLock 拒绝）。
    recover_on_startup(tmp_path)

    remaining_ids = {record["id"] for record in load_quarantine_manifest(tmp_path)}
    assert remaining_ids == {pending_record["id"]}


def test_quarantine_root_excluded_from_scan_and_usb_copy(tmp_path) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_delete(tmp_path, asset)

    quarantine_path = Path(record["quarantine_path"])
    assert should_exclude_managed_path(
        quarantine_path, is_dir=True, workspace_root=tmp_path
    )
    assert should_exclude_managed_path(
        managed_root(tmp_path, "quarantine"), is_dir=True, workspace_root=tmp_path
    )


def test_quarantine_operations_are_bound_to_owning_workspace(tmp_path) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _init_workspace(workspace_a)
    _init_workspace(workspace_b)

    asset = _make_asset(workspace_a)
    record = _locked_register_delete(workspace_a, asset)

    assert list_records(workspace_b) == []
    # 记录物理上按工作区分文件存放；即便有条目被误混入另一工作区的清单
    # （例如手工合并现场），事务 A 也不得凭 ID 操作工作区 B 的记录。
    _save_quarantine_manifest(workspace_b, [record])
    with pytest.raises(QuarantineError):
        _locked_undo_delete(workspace_b, record["id"])


def test_list_records_excludes_other_workspaces(tmp_path) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _init_workspace(workspace_a)
    _init_workspace(workspace_b)

    asset_a = _make_asset(workspace_a)
    asset_b = _make_asset(workspace_b)
    record_a = _locked_register_delete(workspace_a, asset_a)
    _locked_register_delete(workspace_b, asset_b)

    records = list_records(workspace_a)
    assert [record["id"] for record in records] == [record_a["id"]]


def test_crash_between_persist_and_move_is_discarded_on_recovery(tmp_path) -> None:
    """只落盘 moving 记录、内容尚未移动就崩溃——原内容未受影响。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    now = time.time()
    stuck_record = {
        "id": "stuck-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(asset),
        "quarantine_path": str(managed_root(tmp_path, "quarantine") / "stuck-1"),
        "manifest": "deadbeef",
        "status": "moving",
        "created_at": now,
        "expires_at": now + 5.0,
    }
    _save_quarantine_manifest(tmp_path, [stuck_record])

    recover_on_startup(tmp_path)

    assert asset.is_dir()
    assert load_quarantine_manifest(tmp_path) == []


def test_crash_after_move_before_finalize_is_recovered_as_pending(tmp_path) -> None:
    """内容已移入隔离区、转正前崩溃——恢复后记录可正常参与撤销。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    quarantine_path = managed_root(tmp_path, "quarantine") / "stuck-2"
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    asset.rename(quarantine_path)
    now = time.time()
    stuck_record = {
        "id": "stuck-2",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(asset),
        "quarantine_path": str(quarantine_path),
        "manifest": "deadbeef",
        "status": "moving",
        "created_at": now,
        "expires_at": now + 5.0,
    }
    _save_quarantine_manifest(tmp_path, [stuck_record])

    recover_on_startup(tmp_path)

    records = load_quarantine_manifest(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "pending"

    restored = _locked_undo_delete(tmp_path, "stuck-2")
    assert restored["manifest"] == "deadbeef"
    assert asset.is_dir()


def test_schedule_sweep_expired_runs_asynchronously_and_acquires_its_own_lock(
    tmp_path, monkeypatch
) -> None:
    """异步入口不阻塞调用方，自行获取锁后完成实际清理。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)
    _locked_mark_retire_committed(tmp_path, record["id"])

    release = threading.Event()
    sent_paths: list[Path] = []

    def _blocking_send(path: Path) -> None:
        release.wait(timeout=2.0)
        sent_paths.append(path)

    monkeypatch.setattr("fwasset.core.quarantine._send_to_system_recycle_bin", _blocking_send)

    thread = schedule_sweep_expired(tmp_path)
    assert thread.is_alive()
    # 调用立即返回：此刻清单仍未收敛，证明确实是异步执行。
    assert len(load_quarantine_manifest(tmp_path)) == 1

    release.set()
    thread.join(timeout=2.0)

    assert thread.error is None
    assert sent_paths == [Path(record["quarantine_path"])]
    assert load_quarantine_manifest(tmp_path) == []


def test_schedule_sweep_expired_surfaces_failure_instead_of_swallowing(tmp_path) -> None:
    """后台线程异常必须收集到 thread.error，不能被静默吞掉。"""
    _init_workspace(tmp_path)

    # 提前占住工作区锁，让 schedule_sweep_expired 内部获取锁失败。
    with WorkspaceLock(tmp_path):
        thread = schedule_sweep_expired(tmp_path)
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert thread.error is not None


def test_public_functions_reject_calls_without_held_lock(tmp_path) -> None:
    """公共函数一律拒绝未持锁调用，不静默放行（裁决一）。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)

    with pytest.raises(QuarantineError):
        register_delete(tmp_path, asset)
    assert asset.is_dir()  # 拒绝发生在移动之前，原内容未受影响。

    record = _locked_register_delete(tmp_path, asset)

    with pytest.raises(QuarantineError):
        undo_delete(tmp_path, record["id"])
    with pytest.raises(QuarantineError):
        sweep_expired(tmp_path)

    retire_record = _locked_register_retire(tmp_path, _make_asset(tmp_path, "retire"))
    with pytest.raises(QuarantineError):
        mark_retire_committed(tmp_path, retire_record["id"])


def test_sweep_expired_skips_record_mixed_in_from_another_workspace(tmp_path, monkeypatch) -> None:
    """跨工作区混入的记录不参与回收，原样保留在清单中。"""
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _init_workspace(workspace_a)
    _init_workspace(workspace_b)

    asset = _make_asset(workspace_a)
    foreign_record = _locked_register_delete(workspace_a, asset)
    expired_foreign = {**foreign_record, "expires_at": time.time() - 1}
    _save_quarantine_manifest(workspace_b, [expired_foreign])

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(workspace_b)

    assert processed == []
    assert sent_paths == []
    remaining = load_quarantine_manifest(workspace_b)
    assert len(remaining) == 1
    assert remaining[0]["id"] == foreign_record["id"]
    assert Path(foreign_record["quarantine_path"]).exists()


def test_sweep_expired_skips_record_with_tampered_quarantine_path(tmp_path, monkeypatch) -> None:
    """quarantine_path 被篡改指向隔离根之外（词法上仍可能是子路径）时不得回收。"""
    _init_workspace(tmp_path)
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    (outside_target / "keepme.txt").write_text("do not delete", encoding="utf-8")

    quarantine_root = managed_root(tmp_path, "quarantine")
    # 词法上是隔离根的子路径（含 `..` 段），但折叠后其实指向根外的普通目录。
    escaping_path = quarantine_root / ".." / ".." / outside_target.name
    now = time.time()
    tampered_record = {
        "id": "tampered-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        "quarantine_path": str(escaping_path),
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now - 1,
    }
    _save_quarantine_manifest(tmp_path, [tampered_record])

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert processed == []
    assert sent_paths == []
    assert outside_target.is_dir()
    assert (outside_target / "keepme.txt").exists()
    remaining = load_quarantine_manifest(tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["id"] == "tampered-1"


@pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="junction 仅 Windows 可创建"
)
def test_sweep_expired_skips_record_pointing_at_junction_out_of_quarantine_root(
    tmp_path, monkeypatch
) -> None:
    """隔离根内一个指向外部的 junction 不得被当作隔离内容回收（QR-003）。"""
    import subprocess

    _init_workspace(tmp_path)
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    (outside_target / "keepme.txt").write_text("do not delete", encoding="utf-8")

    quarantine_root = managed_root(tmp_path, "quarantine")
    junction_path = quarantine_root / "alias"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_path), str(outside_target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("当前环境无法创建 junction")

    now = time.time()
    tampered_record = {
        "id": "junction-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        "quarantine_path": str(junction_path),
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now - 1,
    }
    _save_quarantine_manifest(tmp_path, [tampered_record])

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert processed == []
    assert sent_paths == []
    assert outside_target.is_dir()
    assert (outside_target / "keepme.txt").exists()
    remaining = load_quarantine_manifest(tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["id"] == "junction-1"


def test_register_delete_is_single_continuous_lock_span(tmp_path) -> None:
    """两阶段登记的状态转换在单一连续锁区间内完成，不分段放锁重取（QR-004）。

    通过 hook ``_move_into_quarantine``（两阶段之间发生的动作）验证此时
    进程仍持有工作区锁——若实现退化为「加锁写 moving → 放锁 → 加锁转正」，
    这一断言会在放锁窗口内失败。
    """
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)

    from fwasset.core import quarantine as quarantine_module
    from fwasset.core.workspace_transaction import workspace_lock_is_held

    original_move = quarantine_module._move_into_quarantine
    lock_held_during_move: list[bool] = []

    def _spy_move(workspace_root, source, destination):
        lock_held_during_move.append(workspace_lock_is_held(workspace_root))
        return original_move(workspace_root, source, destination)

    quarantine_module._move_into_quarantine = _spy_move
    try:
        record = _locked_register_delete(tmp_path, asset)
    finally:
        quarantine_module._move_into_quarantine = original_move

    assert lock_held_during_move == [True]
    assert record["status"] == "pending"


def test_sweep_expired_rejects_record_pointing_at_quarantine_root_itself(
    tmp_path, monkeypatch
) -> None:
    """quarantine_path 被篡改成隔离根本身：拒绝，不得把整根送进回收站（QR-003）。"""
    _init_workspace(tmp_path)
    quarantine_root = managed_root(tmp_path, "quarantine")
    other_asset = _make_asset(tmp_path, "other")
    other_record = _locked_register_delete(tmp_path, other_asset)

    now = time.time()
    root_record = {
        "id": "root-itself-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        "quarantine_path": str(quarantine_root),
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now - 1,
    }
    records = load_quarantine_manifest(tmp_path)
    records.append(root_record)
    _save_quarantine_manifest(tmp_path, records)

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert processed == []
    assert sent_paths == []
    # 隔离根及其他记录的内容必须原封不动。
    assert quarantine_root.is_dir()
    assert Path(other_record["quarantine_path"]).exists()
    remaining_ids = {record["id"] for record in load_quarantine_manifest(tmp_path)}
    assert remaining_ids == {other_record["id"], "root-itself-1"}


def test_sweep_expired_rejects_record_pointing_at_nested_directory_under_root(
    tmp_path, monkeypatch
) -> None:
    """quarantine_path 被篡改成隔离根下更深层的嵌套目录：拒绝（QR-003）。"""
    _init_workspace(tmp_path)
    quarantine_root = managed_root(tmp_path, "quarantine")

    intermediate = quarantine_root / "intermediate-parent"
    nested = intermediate / "nested-leaf"
    nested.mkdir(parents=True)
    (nested / "keepme.txt").write_text("do not delete", encoding="utf-8")

    now = time.time()
    tampered_record = {
        "id": "nested-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        "quarantine_path": str(nested),
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now - 1,
    }
    _save_quarantine_manifest(tmp_path, [tampered_record])

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert processed == []
    assert sent_paths == []
    assert nested.is_dir()
    assert (nested / "keepme.txt").exists()
    remaining_ids = {record["id"] for record in load_quarantine_manifest(tmp_path)}
    assert remaining_ids == {"nested-1"}


def test_sweep_expired_rejects_tampered_record_pointing_at_another_records_directory(
    tmp_path, monkeypatch
) -> None:
    """到期的篡改记录指向未到期记录的目录：拒绝，未到期记录内容仍在、仍可撤销（QR-003）。

    第四轮点名场景：`other_resolved == resolved` 时若直接放行（旧实现的
    O(n^2) 扫描曾经这样做），清理 B 会连带把 A 的隔离内容一并送进回收站，
    A 的清单条目虽还在但已无法撤销回原路径。

    第六轮起 `undo_delete` 也做同样的碰撞校验（对称保护，见
    `test_undo_delete_rejects_tampered_record_pointing_at_another_records_directory`），
    因此 B 仍留在清单中时 A 的撤销同样会被拒绝；移除 B 后 A 才恢复可撤销。
    """
    _init_workspace(tmp_path)
    live_asset = _make_asset(tmp_path, "live")
    live_record = _locked_register_delete(tmp_path, live_asset)

    now = time.time()
    tampered_record = {
        "id": "collision-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        # 指向 live_record 真实所在的目录，而非自己的独立 UUID 目录。
        "quarantine_path": live_record["quarantine_path"],
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now - 1,
    }
    records = load_quarantine_manifest(tmp_path)
    records.append(tampered_record)
    _save_quarantine_manifest(tmp_path, records)

    sent_paths: list[Path] = []
    monkeypatch.setattr(
        "fwasset.core.quarantine._send_to_system_recycle_bin",
        lambda path: sent_paths.append(path),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert processed == []
    assert sent_paths == []
    remaining_ids = {record["id"] for record in load_quarantine_manifest(tmp_path)}
    assert remaining_ids == {live_record["id"], "collision-1"}

    # 未到期记录的隔离内容必须原封不动；B 仍在清单中时撤销 A 会因同一路径
    # 碰撞被对称拒绝，这是预期行为，不是回归。
    quarantine_path = Path(live_record["quarantine_path"])
    assert quarantine_path.exists()
    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, live_record["id"])
    assert quarantine_path.exists()

    # 移除无效的篡改记录 B 后，A 恢复可正常撤销回原路径。
    records = load_quarantine_manifest(tmp_path)
    records = [record for record in records if record["id"] != "collision-1"]
    _save_quarantine_manifest(tmp_path, records)

    restored = _locked_undo_delete(tmp_path, live_record["id"])
    assert restored["manifest"] == live_record["manifest"]
    assert live_asset.is_dir()
    assert (live_asset / "file.bin").read_bytes() == b"payload"


def test_undo_delete_rejects_tampered_record_pointing_at_another_records_directory(
    tmp_path,
) -> None:
    """未到期的篡改记录 B 与合法未到期记录 A 指向同一隔离目录：撤销 B 被拒绝，
    A 的隔离内容原封不动（第六轮回归：undo_delete 之前不比较
    ``other_records``，撤销 B 会把 A 的隔离内容 move 走并删除 B 的记录，导致
    A 的清单条目永久无法撤销）。碰撞检查是对称的——B 仍在清单中时 A 的撤销
    也会因同一条路径冲突被拒绝，此为预期行为（两条记录指向同一目录本身就是
    无法安全区分谁合法的损坏状态）；先移除 B 这条无效记录后，A 才恢复可正常
    撤销。
    """
    _init_workspace(tmp_path)
    live_asset = _make_asset(tmp_path, "live")
    live_record = _locked_register_delete(tmp_path, live_asset)

    now = time.time()
    tampered_record = {
        "id": "collision-undo-1",
        "kind": "undoable_delete",
        "workspace_root": str(tmp_path),
        "original_path": str(tmp_path / "was-here"),
        # 指向 live_record 真实所在的目录，而非自己的独立 UUID 目录。
        "quarantine_path": live_record["quarantine_path"],
        "manifest": "deadbeef",
        "status": "pending",
        "created_at": now,
        "expires_at": now + UNDO_WINDOW_SECONDS,
    }
    records = load_quarantine_manifest(tmp_path)
    records.append(tampered_record)
    _save_quarantine_manifest(tmp_path, records)

    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, "collision-undo-1")

    # A 的隔离内容必须原封不动。
    quarantine_path = Path(live_record["quarantine_path"])
    assert quarantine_path.exists()
    remaining_ids = {record["id"] for record in load_quarantine_manifest(tmp_path)}
    assert remaining_ids == {live_record["id"], "collision-undo-1"}

    # 撤销 A 此刻同样会因与 B 的路径碰撞被拒绝——对称保护，不误删任何一方。
    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, live_record["id"])
    assert quarantine_path.exists()

    # 移除无效的篡改记录 B 后，A 恢复可正常撤销回原路径。
    records = load_quarantine_manifest(tmp_path)
    records = [record for record in records if record["id"] != "collision-undo-1"]
    _save_quarantine_manifest(tmp_path, records)

    restored = _locked_undo_delete(tmp_path, live_record["id"])
    assert restored["manifest"] == live_record["manifest"]
    assert live_asset.is_dir()
    assert (live_asset / "file.bin").read_bytes() == b"payload"


def test_public_functions_reject_calls_from_a_different_thread_holding_no_lock(
    tmp_path,
) -> None:
    """线程 A 持锁、线程 B 调用登记必须被拒绝，不能被误判为已持锁（QR-004）。

    ``_held_mutexes`` 若只记录锁名不记录持锁线程，线程 B 会看到
    「本进程有人持有」就通过校验，从而在线程 A 仍持锁写清单期间并发
    读改写——本用例复现这一场景并断言线程 B 必须收到拒绝。
    """
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)

    thread_a_locked = threading.Event()
    release_thread_a = threading.Event()
    thread_b_error: list[BaseException | None] = [None]
    thread_b_done = threading.Event()

    def _thread_a() -> None:
        with WorkspaceLock(tmp_path):
            thread_a_locked.set()
            release_thread_a.wait(timeout=5.0)

    def _thread_b() -> None:
        thread_a_locked.wait(timeout=5.0)
        try:
            register_delete(tmp_path, asset)
        except BaseException as exc:  # noqa: BLE001 - 收集到主线程断言
            thread_b_error[0] = exc
        finally:
            thread_b_done.set()

    worker_a = threading.Thread(target=_thread_a)
    worker_b = threading.Thread(target=_thread_b)
    worker_a.start()
    worker_b.start()
    try:
        assert thread_b_done.wait(timeout=5.0)
    finally:
        release_thread_a.set()
        worker_a.join(timeout=5.0)
        worker_b.join(timeout=5.0)

    assert isinstance(thread_b_error[0], QuarantineError)
    # 线程 B 被拒绝，资产原地未动、未落盘任何记录。
    assert asset.is_dir()
    assert load_quarantine_manifest(tmp_path) == []


def test_workspace_lock_allows_a_different_thread_to_wait_and_acquire(tmp_path) -> None:
    """线程 A 释放锁后，线程 B 应能真正获取到锁（验证不是误伤跨线程串行化）。"""
    from fwasset.core.workspace_transaction import workspace_lock_is_held

    thread_a_locked = threading.Event()
    release_thread_a = threading.Event()
    thread_b_acquired = threading.Event()
    thread_b_saw_lock_from_inside = [False]

    def _thread_a() -> None:
        with WorkspaceLock(tmp_path):
            thread_a_locked.set()
            release_thread_a.wait(timeout=5.0)

    def _thread_b() -> None:
        thread_a_locked.wait(timeout=5.0)
        with WorkspaceLock(tmp_path, timeout_seconds=5.0):
            thread_b_saw_lock_from_inside[0] = workspace_lock_is_held(tmp_path)
            thread_b_acquired.set()

    worker_a = threading.Thread(target=_thread_a)
    worker_b = threading.Thread(target=_thread_b)
    worker_a.start()
    worker_b.start()

    assert thread_a_locked.wait(timeout=5.0)
    # 线程 A 仍持锁时，线程 B 不应该已经拿到锁（还在等待）。
    assert not thread_b_acquired.is_set()

    release_thread_a.set()
    worker_a.join(timeout=5.0)
    assert thread_b_acquired.wait(timeout=5.0)
    worker_b.join(timeout=5.0)

    assert thread_b_saw_lock_from_inside[0] is True
    assert not workspace_lock_is_held(tmp_path)
