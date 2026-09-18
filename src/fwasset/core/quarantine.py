"""隔离记录登记、撤销、到期清理与启动恢复（TASK-20260916，父规格 D2.5）。

隔离根本身（``managed_paths.quarantine``、同卷兄弟目录、所有权标记、路径
守卫）已由 TASK-20260905 / TASK-20260915 交付，本模块只负责**记录层**：

- ``undoable_delete``——D10.1 各删除操作，保留 5 秒撤销窗口；
- ``transactional_retire``——update / 改类型的旧程序退位，仅供失败补偿，
  提交成功后立即异步送系统回收站，**不展示撤销**（D10.2）。

清单持久化在 ``workspace_state`` 受管根，读写沿用 ``workspace_transaction``
的原子替换（临时文件 + fsync + ``os.replace``）与有界重试模式，不新造
一套；隔离内容本身的移入/移出走 ``managed_paths`` 的写授权守卫。

**锁归属（2026-09-16 统筹裁决）**：父规格 D2.4 失败补偿「全程持写锁」、
D8.3 要求删除接入同一事务协调器，故本模块的登记 / 撤销 / 清理都发生在
**调用方已持有的写锁内**——公共函数一律不自行获取
:class:`~fwasset.core.workspace_transaction.WorkspaceLock`，只用
:func:`~fwasset.core.workspace_transaction.workspace_lock_is_held` 校验
调用方**当前线程**已持锁，未持锁调用直接拒绝（与
``staging_io.allocate_staging_area`` 同一模式）。``WorkspaceLock`` 同
线程不可重入是既有事务基础的正确语义，本模块不为自身方便放宽；
``workspace_lock_is_held`` 按线程而非按进程判断（2026-09-16 裁决：
``_held_mutexes`` 记录持锁线程，避免同进程另一线程被误判为已持锁）。

**调用链归属（2026-09-16 统筹裁决）**：本模块只交付
:func:`schedule_sweep_expired` 异步入口与 :func:`recover_on_startup`
启动恢复函数，并在各自 docstring 声明调用契约；真实触发点不在本片——
删除操作接入（D2.4 / D2.4a）归子任务 4/5，应用启动时机的恢复调用归
子任务 8 UI 编排，本模块不提前拉入尚未实现的调用方。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fwasset.core.managed_paths import assert_managed_write, managed_root
from fwasset.core.manifest import (
    ManifestError,
    directory_manifest_hash,
    path_is_reparse_point,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    contained_subpath,
    normalize_workspace_path,
)
from fwasset.core.types import QuarantineRecord
from fwasset.core.workspace_transaction import (
    WorkspaceLock,
    WorkspaceTransactionError,
    workspace_lock_is_held,
)

#: 隔离清单文件名（位于 ``workspace_state`` 受管根）。
_MANIFEST_FILENAME = "quarantine-manifest.json"

#: ``undoable_delete`` 的撤销窗口（父规格 D2.5：保留 5 秒撤销）。
UNDO_WINDOW_SECONDS: float = 5.0

_SHARING_RETRY_BUDGET_SECONDS: float = 2.0
_SHARING_RETRY_DELAY_SECONDS: float = 0.01


class QuarantineError(WorkspaceTransactionError):
    """隔离登记、撤销或送出拒绝继续时抛出。"""


class UndoConflictError(QuarantineError):
    """撤销目标已被占用（含大小写 / junction 等价身份）；隔离内容不得删除。"""


class TrashUnavailableError(QuarantineError):
    """无法建立同卷隔离根（首批不做跨卷分支）。"""


def _retry_sharing_conflicts(operation: Any) -> Any:
    """Windows 瞬态共享冲突的有界重试，与 ``workspace_transaction`` 同一模式。"""
    deadline = time.monotonic() + _SHARING_RETRY_BUDGET_SECONDS
    while True:
        try:
            return operation()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_SHARING_RETRY_DELAY_SECONDS)


def _manifest_path(workspace_root: str | Path) -> Path:
    return managed_root(workspace_root, "workspace_state") / _MANIFEST_FILENAME


def _write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _retry_sharing_conflicts(lambda: os.replace(temporary, path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_record(raw: dict[str, Any]) -> QuarantineRecord | None:
    """校验单条记录；旧格式缺省字段可读，损坏条目跳过（不阻断整份清单）。"""
    record_id = raw.get("id")
    kind = raw.get("kind")
    workspace_root = raw.get("workspace_root")
    original_path = raw.get("original_path")
    quarantine_path = raw.get("quarantine_path")
    manifest = raw.get("manifest")
    if (
        not isinstance(record_id, str)
        or kind not in ("undoable_delete", "transactional_retire")
        or not isinstance(workspace_root, str)
        or not isinstance(original_path, str)
        or not isinstance(quarantine_path, str)
        or not isinstance(manifest, str)
    ):
        return None
    status = raw.get("status", "pending")
    if status not in ("moving", "pending", "committed", "sent", "send_failed"):
        status = "pending"
    created_at = raw.get("created_at", 0.0)
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        created_at = 0.0
    expires_at = raw.get("expires_at", 0.0)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        expires_at = 0.0
    # D10.1b：旧记录缺该键读为 []（撤销时不重建容器，与扩字段前行为一致）；
    # 非 list 或含非字符串元素一律降级为 []，与本函数「损坏字段降级、不阻断
    # 整份清单」的既有口径一致。
    raw_containers = raw.get("removed_containers", [])
    if isinstance(raw_containers, list) and all(
        isinstance(item, str) for item in raw_containers
    ):
        removed_containers = [str(item) for item in raw_containers]
    else:
        removed_containers = []
    return {
        "id": record_id,
        "kind": kind,
        "workspace_root": workspace_root,
        "original_path": original_path,
        "quarantine_path": quarantine_path,
        "manifest": manifest,
        "status": status,
        "created_at": float(created_at),
        "expires_at": float(expires_at),
        "removed_containers": removed_containers,
    }


def load_quarantine_manifest(workspace_root: str | Path) -> list[QuarantineRecord]:
    """读取隔离清单；文件缺失返回空列表，损坏条目跳过（旧格式缺省可读）。"""
    path = _manifest_path(workspace_root)
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = _retry_sharing_conflicts(lambda: json.load(handle))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantineError(f"隔离清单无法读取：{path}") from exc
    raw_records = raw.get("records", []) if isinstance(raw, dict) else None
    if not isinstance(raw_records, list):
        raise QuarantineError(f"隔离清单格式无效：{path}")
    records: list[QuarantineRecord] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        parsed = _read_record(item)
        if parsed is not None:
            records.append(parsed)
    return records


def _save_quarantine_manifest(
    workspace_root: str | Path, records: list[QuarantineRecord]
) -> None:
    path = _manifest_path(workspace_root)
    _write_json_atomically(path, {"records": [dict(record) for record in records]})


def _assert_lock_held(workspace_root: str | Path) -> None:
    """公共函数一律要求调用方已持有工作区写锁，本模块不自行获取。

    与 ``staging_io.allocate_staging_area`` 同一模式：只检查
    :func:`~fwasset.core.workspace_transaction.workspace_lock_is_held`
    （**当前线程**是否持有该工作区命名互斥体——同进程另一线程不算数，
    否则线程 A 持锁时线程 B 会被误判为已持锁而并发读改写清单），不静默
    放行、也不代为加锁——隔离登记/撤销/清理是 D2.4 失败补偿链路的一
    部分，必须与调用方的写操作在同一把锁下连续完成。
    """
    if not workspace_lock_is_held(workspace_root):
        raise QuarantineError(
            f"隔离操作要求调用方已持有工作区写锁，已拒绝：{workspace_root}"
        )


def _assert_same_workspace(record: QuarantineRecord, workspace_root: str | Path) -> None:
    """隔离操作绑定事务所属工作区；事务 A 不得操作工作区 B 的隔离记录。"""
    if normalize_workspace_path(record["workspace_root"]) != normalize_workspace_path(
        workspace_root
    ):
        raise QuarantineError(
            f"隔离记录属于其他工作区，已拒绝操作：{record['id']}"
        )


def _assert_quarantine_path_owned(
    record: QuarantineRecord,
    workspace_root: str | Path,
    *,
    other_records: tuple[QuarantineRecord, ...] = (),
) -> None:
    """记录的 ``quarantine_path`` 必须是隔离根下**应用生成的独立记录目录**。

    与 :func:`_assert_same_workspace` 互补：后者只比较记录自报的
    ``workspace_root`` 字段（可被篡改），这里额外核对路径的**实际**归属，
    两者都命中才允许对隔离内容执行送出 / 回收动作。

    按登记约定校验，不做嵌套扫描（2026-09-16 第四轮裁决）：
    :func:`_quarantine_destination` 生成的路径恒为
    ``managed_root(workspace_root, "quarantine") / uuid4().hex``——隔离根
    的**直接子目录**。因此合法路径解析后必须满足 ``parent == 隔离根``，
    这一条同时拒绝「等于隔离根本身」（父目录是根的父目录，不是根）与
    「更深层嵌套目录」（父目录是某个子目录，也不是根），不需要额外分别
    判断。跨记录篡改成**同一目录**则是显式的相同路径冲突（两条记录，
    一条到期一条未到期，指向同一目录时清理会连带毁掉未到期记录的内容），
    与「谁包含谁」无关，单独比较 ``other_records`` 中是否有记录解析到
    相同路径即可，不需要两两判断祖先/后代关系。额外用
    :func:`~fwasset.core.manifest.path_is_reparse_point` 拒绝记录路径
    本身是链接的情况，防止未来改动误换成不 resolve 的比较函数时失去
    这层保护。

    ``other_records`` 在 :func:`sweep_expired` 与 :func:`undo_delete`
    都传入同批记录：撤销把隔离内容 ``move`` 回原路径并删除本条记录，
    若合法记录 A 与被篡改记录 B 指向同一隔离目录，撤销任意一方都会连带
    夺走另一方的隔离内容——撤销同样是破坏性操作，不能因为「表面上是移回
    原路径」就跳过碰撞检查。检查按路径冲突对称生效，两条记录都会被拒绝
    （无法仅从路径判断谁是被篡改的一方）；需要先移除无效记录，碰撞解除
    后合法记录才恢复可撤销/可回收。
    """
    root = managed_root(workspace_root, "quarantine")
    quarantine_path = Path(record["quarantine_path"])
    # 先用存在性判断再查 reparse point：``lstat`` 要求路径确实存在，
    # 一个含 ``..`` 段但折叠后本不存在的记录路径不该在这里被误判成异常。
    if quarantine_path.exists() and path_is_reparse_point(quarantine_path):
        raise QuarantineError(
            f"隔离记录路径是链接或重定向路径，已拒绝操作：{record['id']}"
        )
    resolved = contained_subpath(quarantine_path, root)
    if resolved is None:
        raise QuarantineError(
            f"隔离记录路径不在本工作区隔离根内，已拒绝操作：{record['id']}"
        )
    resolved_root = root.resolve()
    if resolved.parent != resolved_root:
        raise QuarantineError(
            f"隔离记录路径不是隔离根下的直接记录目录，已拒绝操作：{record['id']}"
        )
    for other in other_records:
        if other["id"] == record["id"]:
            continue
        other_resolved = contained_subpath(Path(other["quarantine_path"]), root)
        if other_resolved == resolved:
            raise QuarantineError(
                f"隔离记录路径与另一条记录指向同一目录，已拒绝操作：{record['id']}"
            )


def _quarantine_destination(workspace_root: str | Path) -> Path:
    try:
        root = managed_root(workspace_root, "quarantine")
    except PathGuardError as exc:
        raise TrashUnavailableError(str(exc)) from exc
    return root / uuid.uuid4().hex


def _move_into_quarantine(
    workspace_root: str | Path, source: str | Path, destination: Path
) -> None:
    try:
        assert_within_workspace(source, workspace_root)
    except PathGuardError as exc:
        raise QuarantineError(str(exc)) from exc
    try:
        assert_managed_write(destination, workspace_root, expect="quarantine")
    except PathGuardError as exc:
        raise TrashUnavailableError(str(exc)) from exc
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise QuarantineError(f"移入隔离区失败：{source}") from exc


def _register(
    workspace_root: str | Path,
    source: str | Path,
    *,
    kind: str,
    expires_at: float,
    removed_containers: Sequence[str | Path] = (),
) -> QuarantineRecord:
    """两阶段登记：持续持有调用方的锁，先落盘 ``moving`` 记录，再移动内容，
    最后转正为终态——整个过程是**单一连续锁区间**，不在中途放锁再重新
    获取（2026-09-16 裁决：登记/撤销/清理发生在调用方已持有的写锁内）。

    崩溃在任一点都能靠清单收敛：只落盘未移动 → 恢复时按 ``original_path``
    发现内容仍在原处，丢弃该条 ``moving`` 记录；移动完成但转正前崩溃 →
    恢复时按 ``quarantine_path`` 发现内容已在隔离区，把记录转正即可，不
    会出现「内容已移走但清单找不到」的孤儿现场。
    """
    _assert_lock_held(workspace_root)
    source_path = Path(source)
    try:
        manifest = directory_manifest_hash(source_path)
    except ManifestError as exc:
        raise QuarantineError(str(exc)) from exc

    destination = _quarantine_destination(workspace_root)
    now = time.time()
    record: QuarantineRecord = {
        "id": uuid.uuid4().hex,
        "kind": kind,  # type: ignore[typeddict-item]
        "workspace_root": str(workspace_root),
        "original_path": str(source_path),
        "quarantine_path": str(destination),
        "manifest": manifest,
        "status": "moving",
        "created_at": now,
        "expires_at": expires_at if expires_at == 0.0 else now + expires_at,
        "removed_containers": [str(item) for item in removed_containers],
    }
    records = load_quarantine_manifest(workspace_root)
    records.append(record)
    _save_quarantine_manifest(workspace_root, records)

    _move_into_quarantine(workspace_root, source_path, destination)

    finalized: QuarantineRecord = {**record, "status": "pending"}
    records[-1] = finalized
    _save_quarantine_manifest(workspace_root, records)
    return finalized


def register_delete(
    workspace_root: str | Path,
    source: str | Path,
    *,
    removed_containers: Sequence[str | Path] = (),
) -> QuarantineRecord:
    """登记一条 ``undoable_delete``：先落盘记录，再移入隔离区，保留撤销窗口。

    调用方须已持有工作区写锁（未持锁调用直接拒绝，见模块 docstring）。

    ``removed_containers``（D10.1b）：本次删除**由调用方自动 ``rmdir`` 掉的
    空父容器**，自外向内排列；:func:`undo_delete` 会在移回内容前按需重建。
    调用方只登记自己删掉的目录，不登记用户此前就不存在的路径。
    """
    return _register(
        workspace_root,
        source,
        kind="undoable_delete",
        expires_at=UNDO_WINDOW_SECONDS,
        removed_containers=removed_containers,
    )


def register_retire(
    workspace_root: str | Path, source: str | Path
) -> QuarantineRecord:
    """登记一条 ``transactional_retire``：先落盘记录，再移入隔离区，无撤销窗口。

    调用方须已持有工作区写锁（未持锁调用直接拒绝，见模块 docstring）。
    """
    return _register(
        workspace_root, source, kind="transactional_retire", expires_at=0.0
    )


def mark_retire_committed(workspace_root: str | Path, record_id: str) -> QuarantineRecord:
    """标记一条 ``transactional_retire`` 已提交成功，转入待送出回收站。

    调用方须已持有工作区写锁（未持锁调用直接拒绝，见模块 docstring）。
    """
    _assert_lock_held(workspace_root)
    records = load_quarantine_manifest(workspace_root)
    for index, record in enumerate(records):
        if record["id"] != record_id:
            continue
        if record["kind"] != "transactional_retire":
            raise QuarantineError(f"记录不是 transactional_retire：{record_id}")
        _assert_same_workspace(record, workspace_root)
        updated: QuarantineRecord = {**record, "status": "committed"}
        records[index] = updated
        _save_quarantine_manifest(workspace_root, records)
        return updated
    raise QuarantineError(f"隔离记录不存在：{record_id}")


def _target_occupied(destination: Path) -> bool:
    """还原目标是否已被占用（含大小写等价；同名 junction/symlink 视为占用）。

    与 ``staging_io._assert_promotable_target`` 同一模式：父目录逐项比较
    normcase 名称，命中即占用——不区分目标是普通目录还是 reparse point，
    足以满足 D3「目标不存在」判定，不需要额外跟随目标身份。
    """
    if destination.exists():
        return True
    parent = destination.parent
    if not parent.is_dir():
        return False
    target_name = os.path.normcase(destination.name)
    try:
        entries = list(parent.iterdir())
    except OSError:
        return False
    return any(os.path.normcase(entry.name) == target_name for entry in entries)


def _rebuild_removed_containers(
    record: QuarantineRecord, workspace_root: str | Path
) -> None:
    """撤销前重建本次删除时自动 ``rmdir`` 掉的空父容器（D10.1b）。

    ``removed_containers`` 自外向内排列，按序 ``mkdir`` 即可保证先祖先于
    后代。每个路径都要过工作区守卫——记录里的路径是自报内容，不能直接拿来
    建目录。已存在（用户或并发操作又建了回来）视为满足前置，不报错；被同名
    **文件**占住则无法继续，报 :class:`UndoConflictError` 并保留隔离内容。
    """
    for raw in record.get("removed_containers", ()):
        try:
            container = assert_within_workspace(raw, workspace_root)
        except PathGuardError as exc:
            raise QuarantineError(f"待重建容器不在工作区内：{raw}") from exc
        if container.is_dir():
            continue
        if container.exists():
            raise UndoConflictError(
                f"待重建的模块容器被同名文件占用，隔离内容已保留：{container}"
            )
        try:
            container.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QuarantineError(f"重建模块容器失败：{container}") from exc


def undo_delete(workspace_root: str | Path, record_id: str) -> QuarantineRecord:
    """在窗口内撤销一条 ``undoable_delete``，还原到原路径。

    目标已被占用（含大小写 / junction 等价）→ :class:`UndoConflictError`，
    隔离内容保留不删；窗口已过 → :class:`QuarantineError`。调用方须已持有
    工作区写锁（未持锁调用直接拒绝，见模块 docstring）。

    记录带 ``removed_containers`` 时（D10.1b：删除最后一个变体会自动
    ``rmdir`` 空模块容器），先按 :func:`_rebuild_removed_containers` 重建
    父容器再移回内容——否则 ``os.replace`` 的目标父目录不存在。重建发生在
    占用检查**之后**、移动**之前**：占用冲突时不该留下凭空建出的空目录。

    与 :func:`sweep_expired` 一样，用 :func:`_assert_quarantine_path_owned`
    的 ``other_records`` 校验清单中是否有另一条记录解析到同一隔离目录
    （第六轮修正：撤销把内容 ``move`` 回原路径并删除本记录，若另一条
    记录也指向这个目录，撤销会连带夺走它的内容，使其永久无法撤销——
    这与 sweep 送系统回收站同样是破坏性操作，不能因为「表面上是移回
    原路径」而跳过碰撞检查）。

    碰撞发生时**两条记录的撤销都会被拒绝**，不挑一条放行：应用无法从
    记录自报字段分辨哪条是合法记录、哪条是被篡改或损坏的——如果靠
    ``created_at`` 之类字段做 tie-break，篡改者改得动 ``quarantine_path``
    就同样改得动那个字段，等于用记录自报内容当身份凭证，与
    :func:`_assert_same_workspace` 只信自报 ``workspace_root`` 是同一类
    错误。对称拒绝下双方隔离内容都原样保留在隔离区，不丢数据；等碰撞
    消除（例如冲突的一方被 :func:`sweep_expired` 送出回收站或从清单移
    除）后，剩下那条记录即可正常撤销。
    """
    _assert_lock_held(workspace_root)
    records = load_quarantine_manifest(workspace_root)
    for index, record in enumerate(records):
        if record["id"] != record_id:
            continue
        if record["kind"] != "undoable_delete":
            raise QuarantineError(f"记录不支持撤销：{record_id}")
        _assert_same_workspace(record, workspace_root)
        _assert_quarantine_path_owned(
            record, workspace_root, other_records=tuple(records)
        )
        if record["status"] != "pending":
            raise QuarantineError(f"记录已不在待撤销状态：{record_id}")
        if time.time() >= record["expires_at"]:
            raise QuarantineError(f"撤销窗口已过：{record_id}")

        original = Path(record["original_path"])
        quarantine_path = Path(record["quarantine_path"])
        try:
            assert_within_workspace(original, workspace_root)
        except PathGuardError as exc:
            raise QuarantineError(str(exc)) from exc
        if _target_occupied(original):
            raise UndoConflictError(
                f"撤销目标已被占用，隔离内容已保留：{original}"
            )
        _rebuild_removed_containers(record, workspace_root)
        try:
            os.replace(quarantine_path, original)
        except OSError as exc:
            raise QuarantineError(f"撤销还原失败：{original}") from exc

        records.pop(index)
        _save_quarantine_manifest(workspace_root, records)
        return {**record, "status": "sent"}
    raise QuarantineError(f"隔离记录不存在：{record_id}")


def list_records(workspace_root: str | Path) -> list[QuarantineRecord]:
    """按创建时间返回当前工作区的全部隔离记录（不跨工作区）。"""
    records = load_quarantine_manifest(workspace_root)
    normalized_root = normalize_workspace_path(workspace_root)
    own = [
        record
        for record in records
        if normalize_workspace_path(record["workspace_root"]) == normalized_root
    ]
    return sorted(own, key=lambda record: record["created_at"])


def _send_to_system_recycle_bin(path: Path) -> None:
    """经系统回收站送出（Windows Shell ``SHFileOperationW`` + ``FOF_ALLOWUNDO``）。

    与隔离区本身是两套机制：隔离区在撤销窗口内保留原始句柄，这里只在
    「已提交成功」之后把内容彻底移交系统回收站，不追求可控撤销。
    """
    if os.name != "nt":
        raise OSError("送出系统回收站目前仅支持 Windows")
    import ctypes

    class _SHFileOpStruct(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    _FO_DELETE = 3
    _FOF_ALLOWUNDO = 0x0040
    _FOF_NOCONFIRMATION = 0x0010
    _FOF_NOERRORUI = 0x0400
    _FOF_SILENT = 0x0004

    operation = _SHFileOpStruct(
        hwnd=None,
        wFunc=_FO_DELETE,
        pFrom=f"{path}\0\0",
        pTo=None,
        fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI | _FOF_SILENT,
        fAnyOperationsAborted=0,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    result = shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"送出系统回收站失败（code={result}）：{path}")


def _reconcile_moving_record(record: QuarantineRecord) -> QuarantineRecord | None:
    """收敛崩溃在两阶段登记之间遗留的 ``moving`` 记录。

    内容仍在原路径（未移动）→ 丢弃该记录，原内容未受影响；内容已在隔离
    区（移动完成但转正前崩溃）→ 转正为 ``pending``，正常参与后续撤销 /
    送出流程。两处路径都不存在是不可达状态（两阶段登记不会产生），按
    丢弃处理不留死记录。
    """
    if Path(record["original_path"]).exists():
        return None
    if Path(record["quarantine_path"]).exists():
        return {**record, "status": "pending"}
    return None


def sweep_expired(workspace_root: str | Path) -> list[QuarantineRecord]:
    """清理到期的 ``undoable_delete`` 与已提交的 ``transactional_retire``。

    撤销窗口届满或 retire 已提交 → 同步送系统回收站并从清单移除；送出
    失败保留清单（``send_failed``）供下次启动恢复重试，**不重新承诺
    update 可撤销**。跨工作区混入或路径被篡改指向隔离根之外的记录一律
    跳过、原样保留在清单中，不参与本次回收（QR-003）。

    调用方须已持有工作区写锁（未持锁调用直接拒绝，见模块 docstring）。
    真实触发点不在本片：:func:`schedule_sweep_expired` 提供异步入口，
    :func:`recover_on_startup` 提供启动恢复入口；两者的调用方各自负责
    先取得锁再调用本函数——具体接入归子任务 4/5（删除入口）与子任务 8
    （启动恢复的应用编排），本模块不提前拉入。
    """
    _assert_lock_held(workspace_root)
    records = load_quarantine_manifest(workspace_root)
    now = time.time()
    processed: list[QuarantineRecord] = []
    remaining: list[QuarantineRecord] = []
    for record in records:
        if record["status"] == "moving":
            reconciled = _reconcile_moving_record(record)
            if reconciled is not None:
                remaining.append(reconciled)
            continue
        try:
            _assert_same_workspace(record, workspace_root)
            _assert_quarantine_path_owned(
                record, workspace_root, other_records=tuple(records)
            )
        except QuarantineError:
            # 不属于本工作区或路径被篡改越界：原样保留，不回收、不报告。
            remaining.append(record)
            continue
        due = (
            record["kind"] == "undoable_delete"
            and record["status"] == "pending"
            and now >= record["expires_at"]
        ) or (
            record["kind"] == "transactional_retire"
            and record["status"] in ("committed", "send_failed")
        )
        if not due:
            remaining.append(record)
            continue
        quarantine_path = Path(record["quarantine_path"])
        try:
            if quarantine_path.exists():
                _send_to_system_recycle_bin(quarantine_path)
            sent: QuarantineRecord = {**record, "status": "sent"}
            processed.append(sent)
        except OSError:
            failed: QuarantineRecord = {**record, "status": "send_failed"}
            processed.append(failed)
            remaining.append(failed)
    _save_quarantine_manifest(workspace_root, remaining)
    return processed


class SweepThread(threading.Thread):
    """:func:`schedule_sweep_expired` 返回的线程，携带执行结果供调用方检查。

    后台线程的异常默认无处可去；这里既不吞掉也不能跨线程抛给调用方，
    只能存起来给 ``join()`` 之后的检查——``error`` 非空即表示本轮清理
    失败（含 :class:`~fwasset.core.workspace_transaction.WorkspaceBusyError`
    等本模块之外的异常），调用方（未来子任务 4/5 的删除入口编排）据此
    决定是否重试或记日志，不能假设「线程跑完就等于成功」。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.error: BaseException | None = None
        self.result: list[QuarantineRecord] = []


def schedule_sweep_expired(workspace_root: str | Path) -> SweepThread:
    """在后台线程异步获取工作区锁并执行 :func:`sweep_expired`，不阻塞调用方。

    父规格 D2.5：撤销窗口届满后异步送系统回收站，不等应用退出。本函数是
    维护性入口，不依附于某个删除事务，因此自行获取
    :class:`~fwasset.core.workspace_transaction.WorkspaceLock`
    后调用 :func:`sweep_expired`——这与「登记/撤销/清理发生在调用方已持
    有的写锁内」的裁决不冲突：那条裁决约束的是嵌在 D2.4 删除流程里的
    记录层调用，本函数是该流程之外独立触发的维护调用，必须自己建立锁
    作用域。真实调用时机（UI 定时器 / 删除完成回调）不在本片，归子任务
    4/5；线程异常一律收集到返回对象的 ``error``，不静默丢弃。
    """
    holder: list[SweepThread] = []

    def _run() -> None:
        thread = holder[0]
        try:
            with WorkspaceLock(workspace_root):
                thread.result = sweep_expired(workspace_root)
        except BaseException as exc:  # noqa: BLE001 - 全部收集，不静默丢弃
            thread.error = exc

    thread = SweepThread(target=_run, name="fwasset-quarantine-sweep", daemon=True)
    holder.append(thread)
    thread.start()
    return thread


def recover_on_startup(workspace_root: str | Path) -> list[QuarantineRecord]:
    """崩溃后下次启动按持久化清单恢复：到期/已提交记录清理，未决记录保留。

    同步执行——启动恢复必须在应用可用前完成收敛，不通过
    :func:`schedule_sweep_expired` 的后台线程调用，否则恢复状态与「是否
    已完成」无法确定性观察。本函数是维护性入口而非嵌入某个删除事务，
    自行获取工作区锁后调用 :func:`sweep_expired`；真实调用时机（应用
    启动流程）不在本片，归子任务 8 的 UI 编排负责触发。
    """
    with WorkspaceLock(workspace_root):
        return sweep_expired(workspace_root)
