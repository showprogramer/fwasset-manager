"""界面编排：启动顺序、错误动作、修复行、撤销条。不持有工作区锁。"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from fwasset.core.managed_paths import managed_root
from fwasset.core.platform_config import canonical_module_dir
from fwasset.core.reference_lookup import enumerate_model_roots
from fwasset.core.scheme_config import discover_schemes

ResumeFn = Callable[..., Any]
CleanupFn = Callable[[Path, Path], None]


_RESUME_FUNCTIONS = {
    "normalize_module_leaf": "resume_normalize_module_leaf",
    "update_asset": "resume_update_asset",
    "change_asset_semantics": "resume_change_asset_semantics",
    "restore_retired_version": "resume_restore_retired_version",
}

_KEEP_DIALOG = {
    "create_model": {
        "migration_required",
        "layout_invalid",
        "invalid_chassis_type",
        "invalid_name",
        "path_exists",
        "domain_violation",
        "path_excluded",
        "workspace_excluded",
        "path_identity_conflict",
    },
    "create_asset": {
        "invalid_name",
        "invalid_scope",
        "invalid_target",
        "invalid_args",
        "path_exists",
        "domain_violation",
        "path_excluded",
        "workspace_excluded",
        "path_identity_conflict",
        "out_of_workspace",
    },
    "update_asset": {
        "invalid_args",
        "invalid_target",
        "path_exists",
        "path_excluded",
        "out_of_workspace",
        "change_kind_mismatch",
        "insufficient_space",
        "incomplete_replacement",
    },
}

_NORMALIZE_NOTE = "软件修复页可以查看，本轮不在那里执行归一。"
_NORMALIZE_CODES = {
    "platform_not_normalized",
    "canonical_conflict",
    "canonical_duplicate",
}


@dataclass(frozen=True)
class WriteEffect:
    """一次服务返回在界面上的动作。"""

    action: str
    note: str = ""


@dataclass(frozen=True)
class StartupRecovery:
    """启动三步跑完后的界面状态。"""

    writes_enabled: bool
    resume_name: str | None
    banner: str | None
    cleanup_errors: list[tuple[Path, str]]
    #: 本次启动清理掉的回收站条目数（TASK-20260923）。
    swept: int = 0


@dataclass(frozen=True)
class RepairRow:
    """软件修复页的一行。本轮一律不可执行。"""

    text: str
    executable: bool

    def __getitem__(self, key: str) -> str | bool:
        if key == "text":
            return self.text
        if key == "executable":
            return self.executable
        raise KeyError(key)


def compose_model_dir_name(name: str, chassis: str) -> str:
    """型号目录名 = 名称 + 机芯类型，使同型号不同机芯并列时能区分。"""
    base = name.strip()
    if not base or not chassis or base.endswith(chassis):
        return base
    return f"{base} {chassis}"


def retarget_model_dir_name(dir_name: str, old: str, new: str) -> str | None:
    """改机芯类型后同步目录名后缀；目录名不以旧类型结尾则返回 None（不动名字）。"""
    if not old or old == new or not dir_name.endswith(old):
        return None
    stem = dir_name[: -len(old)].rstrip(" -_·")
    return f"{stem} {new}" if stem else None


def resume_function_name(operation: str | None) -> str | None:
    """操作日志名 → 续跑函数名。没有续跑入口的操作返回 None。"""
    if not operation:
        return None
    return _RESUME_FUNCTIONS.get(operation)


def recovery_banner_text(operation: str | None) -> str:
    shown = operation or "未知"
    return f"工作区有未完成的写入（操作：{shown}），完成恢复前不能写入。"


def run_startup_recovery(
    workspace_root: str | Path,
    *,
    recover_interrupted: Callable[[Path], Any],
    recover_on_startup: Callable[[Path], Any],
    cleanup: CleanupFn,
    sweep: Callable[[Path], Any] | None = None,
) -> StartupRecovery:
    """串行执行启动恢复。staging 根本身不会传给 cleanup。

    ``sweep``（TASK-20260923）是第 4 步：清理到期的回收站条目。它与前三步
    共用调用方那把写锁，抛错只记不抛——清理失败不该挡住启动。
    """
    root = Path(workspace_root)
    status = recover_interrupted(root)
    recover_on_startup(root)
    errors: list[tuple[Path, str]] = []
    staging = managed_root(root, "staging")
    if staging.is_dir():
        children = sorted(
            (path for path in staging.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        for session in children:
            try:
                cleanup(root, session)
            except Exception as exc:  # noqa: BLE001
                errors.append((session, str(exc)))
    swept = 0
    if sweep is not None:
        try:
            swept = len(list(sweep(root)))
        except Exception as exc:  # noqa: BLE001
            errors.append((root, f"回收站清理失败：{exc}"))
    state = str(getattr(status, "state", ""))
    operation = getattr(status, "operation", None)
    if state == "recovery_required":
        return StartupRecovery(
            writes_enabled=False,
            resume_name=resume_function_name(
                str(operation) if operation else None
            ),
            banner=recovery_banner_text(
                str(operation) if operation else None
            ),
            cleanup_errors=errors,
            swept=swept,
        )
    enabled = state == "clean" and operation is None
    return StartupRecovery(
        writes_enabled=enabled,
        resume_name=None,
        banner=None,
        cleanup_errors=errors,
        swept=swept,
    )


def effect_for_result(result: dict[str, Any], *, entry: str) -> WriteEffect:
    """按 code 选择界面动作。message 由调用方原样展示。"""
    code = str(result.get("code") or "")
    ok = bool(result.get("ok"))
    if code in {"index_pending", "reindex_failed"}:
        return WriteEffect("rescan_hint")
    if code == "index_update_failed":
        return WriteEffect("rescan_hint")
    if code == "confirmation_required":
        # 删除的确认是「影响对话框」（跨型号命中 + 备用副本份数），
        # 与借用的覆盖确认不是同一件事，动作名不能共用。
        if entry in {"delete_asset", "delete_model", "delete_scheme"}:
            return WriteEffect("delete_confirm")
        return WriteEffect("overwrite_confirm")
    if code in {"recovery_required"} or (
        ok is False and code == "recovery_required"
    ):
        return WriteEffect("recovery_banner")
    if code in {"ok", "unchanged"} and ok:
        return WriteEffect("toast")
    if code in _KEEP_DIALOG.get(entry, set()):
        return WriteEffect("keep_dialog")
    note = _NORMALIZE_NOTE if code in _NORMALIZE_CODES else ""
    return WriteEffect("alert", note)


def repair_rows(
    previews: list[tuple[str, dict[str, Any]]],
    legacy: list[dict[str, Any]],
) -> list[RepairRow]:
    """已归一型号不占行。没有任何输入时返回空列表。"""
    rows: list[RepairRow] = []
    for name, result in previews:
        if result.get("ok"):
            preview = result.get("payload", {}).get("preview", {})
            if preview.get("already_normalized"):
                continue
            rows.append(RepairRow(f"{name} 需要归一", False))
            continue
        rows.append(RepairRow(str(result.get("message") or ""), False))
    for item in legacy:
        text = str(item.get("path") or "")
        if item.get("is_retired_versions"):
            text = f"{text} 备用副本（有意排除）"
        rows.append(RepairRow(text, False))
    return rows


def borrow_resubmit(
    original: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """覆盖确认后原样带回 overwrite_token，其他字段不变。"""
    again = dict(original)
    again["overwrite_token"] = payload["overwrite_token"]
    return again


def _remaining_text(expires_at: float, now: float) -> str:
    """回收站剩余保留时间的展示文案。"""
    left = expires_at - now
    if left <= 0:
        return "已到期，下次启动时清理"
    minutes = int(left // 60)
    if minutes >= 60:
        return f"剩余约 {minutes // 60} 小时 {minutes % 60} 分钟"
    if minutes >= 1:
        return f"剩余约 {minutes} 分钟"
    return "剩余不到 1 分钟"


def recycle_rows(
    records: list[dict[str, Any]], *, now: float
) -> list[dict[str, Any]]:
    """回收站页面的行。

    只列可还原的删除项：``undoable_delete`` + ``pending``。退位记录
    （``transactional_retire``）不是用户概念里的「删除」，清理失败的
    ``send_failed`` 也不再提供还原，都不列。
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") != "undoable_delete":
            continue
        if record.get("status") != "pending":
            continue
        original = str(record.get("original_path") or "")
        rows.append(
            {
                "record_id": str(record.get("id") or ""),
                "name": PureWindowsPath(original).name or original,
                "original_path": original,
                "created_at": float(record.get("created_at") or 0.0),
                "remaining": _remaining_text(
                    float(record.get("expires_at") or 0.0), now
                ),
            }
        )
    return rows


def delete_confirm_resubmit(original: dict[str, Any]) -> dict[str, Any]:
    """影响对话框确认后重提：只把 confirm_shared 置真，其余入参逐字不变。

    不就地改调用方的字典——取消时那份还要保持原样。
    """
    again = dict(original)
    again["confirm_shared"] = True
    return again


def undo_bar_callable(*, started_at: float, now: float, limit: float = 5.0) -> bool:
    """与服务 ``expires_at < monotonic`` 对齐：满 5 秒仍可点，超过则不可。"""
    return now <= started_at + limit


def version_replace_undo_target(
    result: dict[str, Any], *, retire_mode: str
) -> str | None:
    """换版本成功不提供撤销。隔离记录不是 undoable_delete。"""
    del result, retire_mode
    return None


def chip_labels_from_workspace(workspace_root: str | Path) -> list[str]:
    """型号芯片使用型号根目录名，不看索引里有没有资产。"""
    return [path.name for path in enumerate_model_roots(Path(workspace_root))]


def direct_scheme_dir_names(model_root: Path) -> list[str]:
    """该型号「定制」目录下的方案目录名。嵌套型号里的方案不算。"""
    root = Path(model_root)
    custom = root / "定制"
    if not custom.is_dir():
        return []
    try:
        custom_key = os.path.normcase(str(custom.resolve()))
        schemes = discover_schemes(root)
    except OSError:
        return []
    names: list[str] = []
    for scheme in schemes:
        try:
            parent_key = os.path.normcase(str(scheme.path.parent.resolve()))
        except OSError:
            continue
        if parent_key != custom_key:
            continue
        name = scheme.path.name
        if name not in names:
            names.append(name)
    return names


@dataclass(frozen=True)
class ProgramUpdatePlan:
    """更新程序对话框的派生结果：``kind`` 为空表示不能提交，原因在 ``error``。"""

    kind: str  # update / change_type / general_to_custom / custom_to_general / custom_scheme_move
    new_path: Path | None
    error: str = ""


def plan_program_update(
    model_root: Path,
    old_path: Path,
    old_module: str,
    old_scheme: str,
    new_module: str,
    new_scheme: str,
    new_name: str,
) -> ProgramUpdatePlan:
    """按改了哪几项派生操作与新路径（TASK-20260924 §4）。

    ``old_scheme`` / ``new_scheme`` 为空表示通用。类型变了一律是 ``change_type``，
    落点按目标范围拼接——服务按新旧路径派生种类，规则与此一致。
    """
    name = new_name.strip()
    if not name:
        return ProgramUpdatePlan("", None, "请填写程序名称。")
    if "/" in name or "\\" in name:
        return ProgramUpdatePlan("", None, "程序名称不能包含斜杠。")
    module = new_module.strip()
    if not module:
        return ProgramUpdatePlan("", None, "请选择程序类型。")
    old = Path(old_path)
    scheme = new_scheme.strip()
    previous_scheme = old_scheme.strip()
    same_module = canonical_module_dir(old_module) == canonical_module_dir(module)
    if same_module and scheme == previous_scheme:
        if os.path.normcase(name) == os.path.normcase(old.name):
            return ProgramUpdatePlan(
                "", None, "只换程序时，新程序名称不能和旧程序相同。"
            )
        return ProgramUpdatePlan("update", old.parent / name)
    root = Path(model_root)
    target = (
        root / "定制" / scheme / module / name if scheme else root / "通用" / module / name
    )
    if not same_module:
        kind = "change_type"
    elif not previous_scheme:
        kind = "general_to_custom"
    elif not scheme:
        kind = "custom_to_general"
    else:
        kind = "custom_scheme_move"
    return ProgramUpdatePlan(kind, target)


def stage_files_as_named_dir(files: list[str], name: str) -> tuple[Path, Path]:
    """把所选文件复制进系统临时目录下名为 ``name`` 的子目录。

    更新服务只收来源目录，且新程序目录名取来源目录名；以程序名称命名来源
    即可让改名生效，不必改事务服务。返回 ``(临时根, 来源目录)``，调用方用完
    删除临时根。
    """
    temp_root = Path(tempfile.mkdtemp(prefix="fwasset-update-"))
    source = temp_root / name.strip()
    try:
        source.mkdir()
        for item in files:
            path = Path(item)
            shutil.copy2(path, source / path.name)
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return temp_root, source
