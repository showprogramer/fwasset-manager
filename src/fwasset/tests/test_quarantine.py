from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import fwasset.core.quarantine as quarantine_module
from fwasset.core.managed_paths import managed_root, should_exclude_managed_path
from fwasset.core.quarantine import (
    RETENTION_SECONDS,
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


def test_retire_committed_is_purged_on_sweep(tmp_path, monkeypatch) -> None:
    """已提交的退位在 sweep 时永久删除；不经系统回收站（TASK-20260923）。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)
    _locked_mark_retire_committed(tmp_path, record["id"])

    purged: list[Path] = []
    original_purge = quarantine_module._purge

    def _spy(path: Path) -> None:
        purged.append(path)
        original_purge(path)

    monkeypatch.setattr(quarantine_module, "_purge", _spy)
    monkeypatch.setattr(
        quarantine_module,
        "_send_to_system_recycle_bin",
        lambda _p: (_ for _ in ()).throw(AssertionError("不得送进系统回收站")),
    )

    processed = _locked_sweep_expired(tmp_path)

    assert len(processed) == 1
    assert processed[0]["status"] == "sent"
    assert purged == [Path(record["quarantine_path"])]
    assert not Path(record["quarantine_path"]).exists()
    assert load_quarantine_manifest(tmp_path) == []


def test_retire_send_failure_keeps_manifest_for_startup_recovery(tmp_path, monkeypatch) -> None:
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    record = _locked_register_retire(tmp_path, asset)
    _locked_mark_retire_committed(tmp_path, record["id"])

    def _boom(path: Path) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(quarantine_module, "_purge", _boom)

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
    purged_paths: list[Path] = []
    original_purge = quarantine_module._purge

    def _blocking_purge(path: Path) -> None:
        release.wait(timeout=2.0)
        purged_paths.append(path)
        original_purge(path)

    monkeypatch.setattr(quarantine_module, "_purge", _blocking_purge)

    thread = schedule_sweep_expired(tmp_path)
    assert thread.is_alive()
    # 调用立即返回：此刻清单仍未收敛，证明确实是异步执行。
    assert len(load_quarantine_manifest(tmp_path)) == 1

    release.set()
    thread.join(timeout=2.0)

    assert thread.error is None
    assert purged_paths == [Path(record["quarantine_path"])]
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
        "expires_at": now + RETENTION_SECONDS,
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


# ---------------------------------------------------------------------------
# D10.1b：removed_containers —— 删除最后一个变体后撤销需重建模块容器
# ---------------------------------------------------------------------------


def _locked_register_delete_with_containers(
    workspace_root: Path, source: Path, containers
):
    with WorkspaceLock(workspace_root):
        return register_delete(
            workspace_root, source, removed_containers=containers
        )


def test_removed_containers_rebuilt_before_restore(tmp_path) -> None:
    """D10.1b：登记的空模块容器在撤销时先重建，内容才能移回原位。"""
    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(
        tmp_path, asset, [str(tmp_path / "通用"), str(container)]
    )
    # 模拟 delete_asset 步骤 7：移走资产后自动 rmdir 空容器（自内向外）。
    container.rmdir()
    (tmp_path / "通用").rmdir()
    assert not container.exists()

    restored = _locked_undo_delete(tmp_path, record["id"])

    assert restored["removed_containers"] == [
        str(tmp_path / "通用"),
        str(container),
    ]
    assert container.is_dir()
    assert (asset / "file.bin").read_bytes() == b"payload"


def test_removed_containers_absent_keeps_previous_behaviour(tmp_path) -> None:
    """不带容器的记录撤销行为不变（扩字段前后一致）。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)

    record = _locked_register_delete(tmp_path, asset)
    assert record["removed_containers"] == []

    restored = _locked_undo_delete(tmp_path, record["id"])

    assert restored["removed_containers"] == []
    assert (asset / "file.bin").read_bytes() == b"payload"


def test_existing_container_is_not_recreated_and_restore_succeeds(tmp_path) -> None:
    """容器已被重新建出来（用户或并发操作）→ 视为满足前置，不报错。"""
    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(tmp_path, asset, [str(container)])
    # 容器没有被删掉（调用方登记了但实际保留），撤销仍应成功。
    assert container.is_dir()

    _locked_undo_delete(tmp_path, record["id"])

    assert (asset / "file.bin").read_bytes() == b"payload"


def test_container_blocked_by_file_returns_undo_conflict(tmp_path) -> None:
    """待重建容器被同名文件占住 → undo_conflict，隔离内容保留。"""
    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(tmp_path, asset, [str(container)])
    container.rmdir()
    container.write_text("占位文件", encoding="utf-8")

    with pytest.raises(UndoConflictError):
        _locked_undo_delete(tmp_path, record["id"])

    assert Path(record["quarantine_path"]).is_dir()
    assert len(load_quarantine_manifest(tmp_path)) == 1


def test_removed_containers_outside_workspace_rejected(tmp_path) -> None:
    """记录里的容器路径是自报内容，越界一律拒绝，不在工作区外建目录。"""
    _init_workspace(tmp_path)
    asset = _make_asset(tmp_path)
    outside = tmp_path.parent / "外部容器"

    record = _locked_register_delete_with_containers(tmp_path, asset, [str(outside)])

    with pytest.raises(QuarantineError):
        _locked_undo_delete(tmp_path, record["id"])

    assert not outside.exists()
    assert Path(record["quarantine_path"]).is_dir()


def test_corrupt_removed_containers_field_degrades_to_empty(tmp_path) -> None:
    """removed_containers 字段损坏 → 降级为 []，不阻断整份清单读取。"""
    _init_workspace(tmp_path)
    manifest_path = (
        managed_root(tmp_path, "workspace_state") / "quarantine-manifest.json"
    )
    manifest_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "r1",
                        "kind": "undoable_delete",
                        "workspace_root": str(tmp_path),
                        "original_path": str(tmp_path / "a"),
                        "quarantine_path": str(tmp_path / "q"),
                        "manifest": "h",
                        "removed_containers": "不是列表",
                    },
                    {
                        "id": "r2",
                        "kind": "undoable_delete",
                        "workspace_root": str(tmp_path),
                        "original_path": str(tmp_path / "b"),
                        "quarantine_path": str(tmp_path / "q2"),
                        "manifest": "h",
                        "removed_containers": ["ok", 123],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    records = load_quarantine_manifest(tmp_path)

    assert len(records) == 2
    assert records[0]["removed_containers"] == []
    assert records[1]["removed_containers"] == []


# ---------------------------------------------------------------------------
# ACI-005：撤销重建容器后的失败补偿
# ---------------------------------------------------------------------------


def test_undo_replace_failure_rolls_back_created_containers(
    tmp_path, monkeypatch
) -> None:
    """os.replace 失败 → 逆序移除本次创建的容器，隔离内容保留，可重试。"""
    import os

    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(
        tmp_path, asset, [str(tmp_path / "通用"), str(container)]
    )
    container.rmdir()
    (tmp_path / "通用").rmdir()

    def _boom(src, dst):
        raise OSError("模拟移动失败")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", _boom)
        with pytest.raises(QuarantineError):
            _locked_undo_delete(tmp_path, record["id"])

    # 补偿完整：本次重建的容器已逆序移除，隔离内容仍在，清单保留一条记录。
    assert not (tmp_path / "通用").exists()
    assert not container.exists()
    assert Path(record["quarantine_path"]).is_dir()
    assert len(load_quarantine_manifest(tmp_path)) == 1

    # 失败可重试：解除模拟后同一记录撤销成功。
    restored = _locked_undo_delete(tmp_path, record["id"])
    assert restored["status"] == "sent"
    assert (asset / "file.bin").read_bytes() == b"payload"


def test_undo_partial_multilevel_rebuild_rolled_back_on_failure(tmp_path) -> None:
    """多级容器重建中途失败 → 已建部分被逆序回滚，不残留空容器。"""
    import pathlib as _pathlib

    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(
        tmp_path, asset, [str(tmp_path / "通用"), str(container)]
    )
    container.rmdir()
    (tmp_path / "通用").rmdir()

    # 第一级「通用」创建成功，第二级 mkdir 失败 → 已建部分须被逆序移除。
    orig_mkdir = _pathlib.Path.mkdir

    def _mkdir_fail_on_second(self, *args, **kwargs):
        if self.name == "主板程序":
            raise OSError("模拟第二级 mkdir 失败")
        return orig_mkdir(self, *args, **kwargs)

    _pathlib.Path.mkdir = _mkdir_fail_on_second
    try:
        with pytest.raises(QuarantineError):
            _locked_undo_delete(tmp_path, record["id"])
    finally:
        _pathlib.Path.mkdir = orig_mkdir

    # 回滚完整：中途建出的「通用」已被逆序移除，隔离内容保留。
    assert not (tmp_path / "通用").exists()
    assert Path(record["quarantine_path"]).is_dir()
    assert len(load_quarantine_manifest(tmp_path)) == 1

    # 失败可重试：解除模拟后同一记录撤销成功。
    restored = _locked_undo_delete(tmp_path, record["id"])
    assert restored["status"] == "sent"
    assert (asset / "file.bin").read_bytes() == b"payload"


def test_undo_rollback_rmdir_failure_maps_recovery_required(tmp_path) -> None:
    """回滚（逆序 rmdir）自身失败 → UndoCompensationIncompleteError，
    service 侧返回 undo_failed 且 payload.recovery_required=True。"""
    import pathlib as _pathlib

    from fwasset.core.services.asset_service import undo_asset_delete
    from fwasset.core.workspace_transaction import load_workspace_status

    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(
        tmp_path, asset, [str(tmp_path / "通用"), str(container)]
    )
    container.rmdir()
    (tmp_path / "通用").rmdir()

    orig_mkdir = _pathlib.Path.mkdir
    orig_rmdir = _pathlib.Path.rmdir

    def _mkdir_fail_on_second(self, *args, **kwargs):
        if self.name == "主板程序":
            raise OSError("模拟第二级 mkdir 失败")
        return orig_mkdir(self, *args, **kwargs)

    def _rmdir_fail(self, *args, **kwargs):
        # 回滚时移除「通用」失败 → 补偿不完整。
        raise OSError("模拟回滚 rmdir 失败")

    # 经 service 入口触发（撤销事务内），故障发生在锁内的重建-失败-回滚链路。
    _pathlib.Path.mkdir = _mkdir_fail_on_second
    _pathlib.Path.rmdir = _rmdir_fail
    try:
        result = undo_asset_delete(str(tmp_path), record["id"])
    finally:
        _pathlib.Path.mkdir = orig_mkdir
        _pathlib.Path.rmdir = orig_rmdir

    assert result["ok"] is False
    assert result["code"] == "undo_failed"
    assert result["payload"]["recovery_required"] is True

    # 补偿失败：中途建出的「通用」残留在工作区；事务未 commit。
    assert (tmp_path / "通用").exists()
    assert load_workspace_status(tmp_path).state == "recovery_required"


def test_undo_manifest_save_failure_maps_recovery_required(
    tmp_path, monkeypatch
) -> None:
    """清单收尾失败 → 可区分补偿失败；service 返回 undo_failed +
    recovery_required=True，工作区保持 recovery_required。"""
    from fwasset.core import quarantine as qmod
    from fwasset.core.services.asset_service import undo_asset_delete
    from fwasset.core.workspace_transaction import load_workspace_status

    def _broken_save(workspace_root, records):
        raise OSError("模拟清单写盘失败")

    # ---- 第一部分：undo_delete 直接行为（内容已移动、清单未收敛）----
    _init_workspace(tmp_path)
    container = tmp_path / "通用" / "主板程序"
    container.mkdir(parents=True)
    asset = _make_asset(container, "v1")

    record = _locked_register_delete_with_containers(
        tmp_path, asset, [str(container)]
    )
    container.rmdir()

    with monkeypatch.context() as m:
        m.setattr(qmod, "_save_quarantine_manifest", _broken_save)
        with pytest.raises(qmod.UndoCompensationIncompleteError):
            _locked_undo_delete(tmp_path, record["id"])

    # 内容确实已移动到原路径，但隔离清单没有收敛（记录仍指向已消失的隔离目录）。
    assert (asset / "file.bin").read_bytes() == b"payload"
    assert len(load_quarantine_manifest(tmp_path)) == 1
    assert not Path(record["quarantine_path"]).exists()

    # ---- 第二部分：service 层在干净现场触发同一故障 ----
    # 先 register（真实写清单），再 patch —— 避免登记本身失败。
    ws2 = tmp_path.parent / f"{tmp_path.name}-service"
    ws2.mkdir()
    _init_workspace(ws2)
    container2 = ws2 / "通用" / "主板程序"
    container2.mkdir(parents=True)
    asset2 = _make_asset(container2, "v1")
    with WorkspaceLock(ws2):
        record2 = register_delete(ws2, asset2, removed_containers=[str(container2)])
    container2.rmdir()

    with monkeypatch.context() as m:
        m.setattr(qmod, "_save_quarantine_manifest", _broken_save)
        result = undo_asset_delete(str(ws2), record2["id"])

    assert result["ok"] is False
    assert result["code"] == "undo_failed"
    assert result["payload"]["recovery_required"] is True
    # service 未 commit：工作区保持 recovery_required，不得宣称零产物成功。
    assert load_workspace_status(ws2).state == "recovery_required"


# ---------------------------------------------------------------------------
# 内置回收站（TASK-20260923）：1 小时保留、真删、不进系统回收站
# ---------------------------------------------------------------------------


def _locked_discard_now(workspace_root: Path, record_ids):
    from fwasset.core.quarantine import discard_now

    with WorkspaceLock(workspace_root):
        return discard_now(workspace_root, record_ids)


def test_retention_is_one_hour_and_persisted_as_wall_clock(tmp_path) -> None:
    """保留期 1 小时；expires_at 取墙钟并落盘，关掉软件也照常计时。"""
    from fwasset.core.quarantine import RETENTION_SECONDS

    assert RETENTION_SECONDS == 3600.0

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)

    before = time.time()
    record = _locked_register_delete(workspace_root, asset)
    after = time.time()

    assert before + 3600.0 <= record["expires_at"] <= after + 3600.0
    # 落盘的是绝对墙钟时刻，不依赖进程存活
    raw = json.loads(
        (managed_root(workspace_root, "workspace_state") / "quarantine-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    stored = [r for r in raw["records"] if r["id"] == record["id"]][0]
    assert stored["expires_at"] == record["expires_at"]


def test_sweep_purges_without_touching_system_recycle_bin(tmp_path, monkeypatch) -> None:
    """到期清理是真删：不调系统回收站，目标路径彻底消失。"""
    import fwasset.core.quarantine as quarantine_module

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)
    record = _locked_register_delete(workspace_root, asset)
    quarantine_path = Path(record["quarantine_path"])
    assert quarantine_path.exists()

    def _must_not_be_called(_path: Path) -> None:
        raise AssertionError("删除链路不得送进系统回收站")

    monkeypatch.setattr(
        quarantine_module, "_send_to_system_recycle_bin", _must_not_be_called
    )

    # 手动把这条记录改成已到期（等价于关掉软件过了一小时）
    records = load_quarantine_manifest(workspace_root)
    expired = [{**r, "expires_at": time.time() - 1.0} for r in records]
    with WorkspaceLock(workspace_root):
        _save_quarantine_manifest(workspace_root, expired)

    processed = _locked_sweep_expired(workspace_root)

    assert [r["id"] for r in processed] == [record["id"]]
    assert not quarantine_path.exists(), "内容应被真删"
    assert load_quarantine_manifest(workspace_root) == []


def test_discard_now_purges_before_expiry(tmp_path, monkeypatch) -> None:
    """彻底删除：未到期也能清，且不进系统回收站。"""
    import fwasset.core.quarantine as quarantine_module

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    keep = _make_asset(workspace_root, "keep")
    drop = _make_asset(workspace_root, "drop")
    keep_record = _locked_register_delete(workspace_root, keep)
    drop_record = _locked_register_delete(workspace_root, drop)

    monkeypatch.setattr(
        quarantine_module,
        "_send_to_system_recycle_bin",
        lambda _p: (_ for _ in ()).throw(AssertionError("不得送进系统回收站")),
    )

    processed = _locked_discard_now(workspace_root, [drop_record["id"]])

    assert [r["id"] for r in processed] == [drop_record["id"]]
    assert not Path(drop_record["quarantine_path"]).exists()
    # 未指定的那条原样保留，仍可还原
    remaining = load_quarantine_manifest(workspace_root)
    assert [r["id"] for r in remaining] == [keep_record["id"]]
    assert Path(keep_record["quarantine_path"]).exists()


def test_discard_now_purges_read_only_files(tmp_path) -> None:
    """厂商固件常带只读属性：彻底删除不能因此失败并留在回收站里。"""
    import os
    import stat

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)
    nested = asset / "旧版本" / "v1"
    nested.mkdir(parents=True)
    (nested / "old.bin").write_bytes(b"old")
    os.chmod(asset / "file.bin", stat.S_IREAD)
    os.chmod(nested / "old.bin", stat.S_IREAD)
    record = _locked_register_delete(workspace_root, asset)

    processed = _locked_discard_now(workspace_root, [record["id"]])

    assert [r["status"] for r in processed] == ["sent"]
    assert not Path(record["quarantine_path"]).exists()
    assert load_quarantine_manifest(workspace_root) == []


def test_sweep_retries_failed_discard(tmp_path, monkeypatch) -> None:
    """彻底删除失败留下的 send_failed 记录，下次清理必须真的重试。"""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)
    record = _locked_register_delete(workspace_root, asset)

    def _fail(_path: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(quarantine_module, "_purge", _fail)
    processed = _locked_discard_now(workspace_root, [record["id"]])
    assert [r["status"] for r in processed] == ["send_failed"]
    monkeypatch.undo()

    retried = _locked_sweep_expired(workspace_root)

    assert [r["status"] for r in retried] == ["sent"]
    assert not Path(record["quarantine_path"]).exists()
    assert load_quarantine_manifest(workspace_root) == []


def test_discard_now_requires_lock(tmp_path) -> None:
    from fwasset.core.quarantine import discard_now

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)
    record = _locked_register_delete(workspace_root, asset)

    with pytest.raises(QuarantineError):
        discard_now(workspace_root, [record["id"]])


def test_discard_now_ignores_unknown_and_foreign_ids(tmp_path) -> None:
    """未知 id 忽略；跨工作区记录跳过且原样保留。"""
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    _init_workspace(workspace_root)
    asset = _make_asset(workspace_root)
    record = _locked_register_delete(workspace_root, asset)

    foreign = {**record, "id": "foreign-1", "workspace_root": str(tmp_path / "other")}
    with WorkspaceLock(workspace_root):
        _save_quarantine_manifest(workspace_root, [record, foreign])

    processed = _locked_discard_now(workspace_root, ["nope", "foreign-1"])

    assert processed == []
    kept = {r["id"] for r in load_quarantine_manifest(workspace_root)}
    assert kept == {record["id"], "foreign-1"}
