"""工作区写事务的锁、状态与恢复基础（TASK-20260915）。"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from fwasset.core.managed_paths import (
    MANAGED_OWNER_MARKER,
    assert_managed_root_not_redirected,
    is_managed_root_owned,
    managed_root,
)
from fwasset.core.path_guard import PathGuardError, normalize_workspace_path
from fwasset.core.types import ManagedPathReason, WorkspaceState

_STATE_FILENAME = "workspace-state.json"
_GENERATION_FILENAME = "generation.json"
_OPERATION_FILENAME = "operation.json"
_INITIALIZED_KINDS: tuple[ManagedPathReason, ...] = (
    "workspace_state",
    "staging",
    "incomplete_candidate",
    "quarantine",
)
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102
_SHARING_RETRY_BUDGET_SECONDS: float = 2.0
_SHARING_RETRY_DELAY_SECONDS: float = 0.01
_held_mutexes: set[str] = set()


class WorkspaceTransactionError(RuntimeError):
    """工作区事务基础拒绝继续时抛出。"""


class WorkspaceBusyError(WorkspaceTransactionError):
    """另一写操作正持有同一工作区的排他锁。"""


class WorkspaceRecoveryRequiredError(WorkspaceTransactionError):
    """工作区保留了中断现场，必须先恢复或人工确认。"""


class ManagedRootOccupiedError(WorkspaceTransactionError):
    """目标受管根由外部预先占用，不能安全接管。"""


@dataclass(frozen=True)
class WorkspaceStatus:
    """持久化工作区现场的只读快照。"""

    state: WorkspaceState
    generation: int
    operation: str | None


def _state_root(workspace_root: str | Path) -> Path:
    return managed_root(workspace_root, "workspace_state")


_T = TypeVar("_T")


def _retry_sharing_conflicts(operation: Callable[[], _T]) -> _T:
    """Windows 瞬态共享冲突的有界重试；预算耗尽后原样抛出。

    无锁预览读者与写侧的 ``os.replace`` / ``os.unlink`` 会互相触发
    PermissionError（句柄未带 FILE_SHARE_DELETE），窗口只有一次
    open/replace 的时长；按预算重试即可穿越，不改变任何写入顺序。
    """
    deadline = time.monotonic() + _SHARING_RETRY_BUDGET_SECONDS
    while True:
        try:
            return operation()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_SHARING_RETRY_DELAY_SECONDS)


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded = _retry_sharing_conflicts(lambda: _read_json_file(path))
    except FileNotFoundError:
        return default
    except PermissionError as exc:
        raise WorkspaceRecoveryRequiredError(
            f"工作区状态文件持续无法读取（可能被其他程序占用）：{path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceRecoveryRequiredError(f"工作区状态文件无法读取：{path}") from exc
    if not isinstance(loaded, dict):
        raise WorkspaceRecoveryRequiredError(f"工作区状态文件格式无效：{path}")
    return loaded


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


def _remove_file(path: Path) -> None:
    def unlink_once() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    _retry_sharing_conflicts(unlink_once)


def _write_state(workspace_root: str | Path, state: WorkspaceState) -> None:
    _write_json_atomically(_state_root(workspace_root) / _STATE_FILENAME, {"state": state})


def _write_generation(workspace_root: str | Path, generation: int) -> None:
    _write_json_atomically(
        _state_root(workspace_root) / _GENERATION_FILENAME,
        {"generation": generation},
    )


def _write_operation(workspace_root: str | Path, operation: str, phase: str) -> None:
    _write_json_atomically(
        _state_root(workspace_root) / _OPERATION_FILENAME,
        {"operation": operation, "phase": phase, "started_at": time.time()},
    )


def _owned_root_or_raise(workspace_root: str | Path, kind: ManagedPathReason) -> Path:
    """在持锁时建立受管根并认领所有权（零覆盖、零删除）。

    任何丢失竞争或重定向都统一报告 ``ManagedRootOccupiedError``：目录用
    ``exist_ok=False``、标记用 ``O_EXCL`` 创建（都不可能覆盖既有内容）；
    复核发现替换时只撤回本应用刚以 ``O_EXCL`` 新建的标记文件，绝不删除
    目录或外部内容。``mkdir`` 会跟随 junction，因此认领前后各复核一次
    重定向，把「写标记进外部目标」的窗口收敛为可检测、可撤回。
    """
    try:
        root = assert_managed_root_not_redirected(workspace_root, kind)
    except (PathGuardError, ValueError) as exc:
        raise ManagedRootOccupiedError(str(exc)) from exc
    if root.exists():
        if not root.is_dir() or not is_managed_root_owned(workspace_root, kind):
            raise ManagedRootOccupiedError(f"受管目录「{kind}」已被外部内容占用：{root}")
        return root

    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ManagedRootOccupiedError(f"受管目录「{kind}」已被外部内容占用：{root}") from exc
    _assert_root_not_redirected(workspace_root, kind, root)

    marker = root / MANAGED_OWNER_MARKER
    try:
        _write_owner_marker(marker)
    except FileExistsError as exc:
        raise ManagedRootOccupiedError(f"受管目录「{kind}」已被外部内容占用：{root}") from exc
    _verify_owner_claim(workspace_root, kind, root, marker)
    return root


def _assert_root_not_redirected(
    workspace_root: str | Path, kind: ManagedPathReason, root: Path
) -> None:
    """mkdir 之后、写标记之前的重定向复核；发现即报告 occupied。"""
    try:
        assert_managed_root_not_redirected(workspace_root, kind)
    except (PathGuardError, ValueError) as exc:
        raise ManagedRootOccupiedError(
            f"受管目录「{kind}」创建后被外部重定向，已拒绝接管：{root}"
        ) from exc


def _write_owner_marker(marker: Path) -> None:
    """以 O_EXCL 创建所有权标记；写入失败只撤回自建文件。

    ``os.open`` 失败时标记属于他人（O_EXCL 拒绝打开既有文件），绝不能
    清理；只有打开成功后的失败，标记才确定是本应用新建的。
    """
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("fwasset-managed-root-v1\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _remove_file(marker)
        raise


def _verify_owner_claim(
    workspace_root: str | Path, kind: ManagedPathReason, root: Path, marker: Path
) -> None:
    """标记落盘后的最终复核：根身份与标记在位，任一失守即撤回自建标记。"""
    try:
        assert_managed_root_not_redirected(workspace_root, kind)
        if not marker.is_file():
            raise PathGuardError(f"所有权标记未落在受管目录内：{marker}")
    except (PathGuardError, ValueError) as exc:
        _remove_file(marker)
        raise ManagedRootOccupiedError(
            f"受管目录「{kind}」标记写入后被外部替换或重定向，已拒绝接管：{root}"
        ) from exc


def _initialize_managed_roots(workspace_root: str | Path) -> None:
    """在已持有工作区锁时建立受管根及初始状态文件。"""
    for kind in _INITIALIZED_KINDS:
        _owned_root_or_raise(workspace_root, kind)
    root = _state_root(workspace_root)
    if not (root / _STATE_FILENAME).exists():
        _write_state(workspace_root, "clean")
    if not (root / _GENERATION_FILENAME).exists():
        _write_generation(workspace_root, 0)


def load_workspace_status(workspace_root: str | Path) -> WorkspaceStatus:
    """读取当前持久化状态；调用前应先初始化受管根。"""
    root = _state_root(workspace_root)
    state_value = _load_json(root / _STATE_FILENAME, {"state": "clean"}).get("state")
    if state_value not in {"clean", "operation_in_progress", "recovery_required"}:
        raise WorkspaceRecoveryRequiredError("工作区状态值无效")
    generation = _load_json(root / _GENERATION_FILENAME, {"generation": 0}).get("generation")
    if not isinstance(generation, int) or generation < 0:
        raise WorkspaceRecoveryRequiredError("工作区 generation 无效")
    operation = _load_json(root / _OPERATION_FILENAME, {}).get("operation")
    if operation is not None and not isinstance(operation, str):
        raise WorkspaceRecoveryRequiredError("工作区操作日志无效")
    return WorkspaceStatus(state=state_value, generation=generation, operation=operation)


@dataclass(frozen=True)
class WorkspacePreviewToken:
    """无锁预览开始时观察到的 generation。"""

    generation: int


def capture_workspace_preview(workspace_root: str | Path) -> WorkspacePreviewToken:
    """记录预览起点；调用方在发布结果前再调用校验函数。"""
    return WorkspacePreviewToken(load_workspace_status(workspace_root).generation)


def preview_token_is_current(
    workspace_root: str | Path, token: WorkspacePreviewToken
) -> bool:
    """仅相同且偶数 generation 的预览结果可发布。"""
    current = load_workspace_status(workspace_root).generation
    return current == token.generation and current % 2 == 0


def recover_interrupted_workspace(workspace_root: str | Path) -> WorkspaceStatus:
    """将异常退出遗留的现场收敛为可诊断、但禁止继续写入的状态。"""
    with WorkspaceLock(workspace_root) as lock:
        _initialize_managed_roots(workspace_root)
        status = load_workspace_status(workspace_root)
        if (
            not lock.was_abandoned
            and status.state == "clean"
            and status.generation % 2 == 0
            and status.operation is None
        ):
            return status
        generation = status.generation + (status.generation % 2)
        if generation != status.generation:
            _write_generation(workspace_root, generation)
        _write_state(workspace_root, "recovery_required")
        return WorkspaceStatus("recovery_required", generation, status.operation)


def _kernel32() -> Any:
    """配置本模块使用的 Win32 函数签名，避免 64 位句柄被截断。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


def _mutex_name(workspace_root: str | Path) -> str:
    normalized = normalize_workspace_path(workspace_root)
    if not normalized:
        raise PathGuardError("工作区根目录未配置，请先在设置中配置程序文件夹")
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"Local\\fwasset-workspace-{key}"


class WorkspaceLock:
    """Windows 命名互斥体；进程异常退出后由系统自动释放。"""

    def __init__(self, workspace_root: str | Path, *, timeout_seconds: float = 2.0) -> None:
        self._name = _mutex_name(workspace_root)
        self._timeout_milliseconds = max(0, round(timeout_seconds * 1000))
        self._handle: int | None = None
        self.was_abandoned = False

    def __enter__(self) -> WorkspaceLock:
        if os.name != "nt":
            raise OSError("工作区跨进程锁目前仅支持 Windows")
        if self._name in _held_mutexes:
            raise WorkspaceBusyError("当前进程已持有该工作区锁，禁止重入")
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建工作区锁")
        result = kernel32.WaitForSingleObject(handle, self._timeout_milliseconds)
        if result == _WAIT_TIMEOUT:
            kernel32.CloseHandle(handle)
            raise WorkspaceBusyError("工作区正被另一操作占用，请稍后重试")
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "无法获取工作区锁")
        self._handle = int(handle)
        self.was_abandoned = result == _WAIT_ABANDONED
        _held_mutexes.add(self._name)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        if self._handle is not None:
            kernel32 = _kernel32()
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None
            _held_mutexes.discard(self._name)
        return False


class WorkspaceTransaction:
    """一次待接入 CRUD 的写事务骨架。"""

    def __init__(self, workspace_root: str | Path, *, operation: str) -> None:
        if not operation.strip():
            raise ValueError("操作名称不能为空")
        self._workspace_root = Path(workspace_root)
        self._operation = operation
        self._lock = WorkspaceLock(workspace_root)
        self.status = WorkspaceStatus("clean", 0, None)
        self._committed = False
        self._entered = False

    def __enter__(self) -> WorkspaceTransaction:
        self._lock.__enter__()
        try:
            _initialize_managed_roots(self._workspace_root)
            self.status = load_workspace_status(self._workspace_root)
            if (
                self._lock.was_abandoned
                or self.status.state != "clean"
                or self.status.generation % 2
                or self.status.operation is not None
            ):
                raise WorkspaceRecoveryRequiredError("工作区存在待恢复的中断操作")
            _write_state(self._workspace_root, "operation_in_progress")
            _write_operation(self._workspace_root, self._operation, "prepared")
            self.status = WorkspaceStatus(
                "operation_in_progress", self.status.generation, self._operation
            )
            self._entered = True
            return self
        except Exception:
            self._lock.__exit__(None, None, None)
            raise

    def begin_product_write(self) -> None:
        """在调用方首次改动产品数据前持久化 odd generation。"""
        if not self._entered or self._committed:
            raise WorkspaceTransactionError("事务尚未开始或已经完成")
        if self.status.generation % 2:
            return
        _write_operation(self._workspace_root, self._operation, "writing")
        generation = self.status.generation + 1
        _write_generation(self._workspace_root, generation)
        self.status = WorkspaceStatus("operation_in_progress", generation, self._operation)

    def commit(self) -> None:
        """结束 seqlock，并仅在成功路径清除操作日志。"""
        if not self._entered or self._committed:
            raise WorkspaceTransactionError("事务尚未开始或已经完成")
        generation = self.status.generation
        if generation % 2:
            generation += 1
            _write_generation(self._workspace_root, generation)
        _write_state(self._workspace_root, "clean")
        _remove_file(_state_root(self._workspace_root) / _OPERATION_FILENAME)
        self.status = WorkspaceStatus("clean", generation, None)
        self._committed = True

    def _mark_recovery_required(self) -> None:
        generation = self.status.generation
        if generation % 2:
            generation += 1
            _write_generation(self._workspace_root, generation)
        _write_state(self._workspace_root, "recovery_required")
        self.status = WorkspaceStatus("recovery_required", generation, self._operation)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        try:
            if self._entered and not self._committed:
                self._mark_recovery_required()
        finally:
            self._lock.__exit__(exc_type, exc, traceback)
        return False