"""新路径准入校验（TASK-20260917，父规格 D3 + D0.2）。

任何操作要让一个新业务身份占用最终可扫描路径，落盘前必须通过
:func:`validate_new_path` 的全套校验；锁外预览与锁内原子提升前复验
（D3.7）共用同一入口。不提供强制覆盖——原身份延续例外（备用副本
恢复、删除撤销、legacy 布局归一）由各调用方在自己的 plan 范围内
窄化，本模块没有旁路开关。
"""

from __future__ import annotations

import os
from pathlib import Path

from fwasset.core.file_scan import _is_excluded_dir
from fwasset.core.managed_paths import _has_model_marker, detect_workspace_layout
from fwasset.core.path_guard import PathGuardError, assert_within_workspace
from fwasset.core.reference_lookup import (
    ReferenceLookupResult,
    find_dangling_anchors,
    is_blocking_issue,
)
from fwasset.core.types import AdmissionKind

_COMMON_DIR = "通用"
_CUSTOM_DIR = "定制"

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')


class AdmissionError(ValueError):
    """准入拒绝；``code`` 对应 Task 规格检查表，``payload`` 携带定位信息。"""

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


def validate_new_path(
    target: str | Path,
    *,
    kind: AdmissionKind,
    configured_root: str | Path,
    workspace_root: str | Path,
) -> Path:
    """D3 全套准入校验（按规格顺序 1–6），通过返回 resolved 目标路径。"""
    try:
        resolved = assert_within_workspace(target, workspace_root)
    except PathGuardError as exc:
        raise AdmissionError("out_of_workspace", str(exc)) from exc

    _assert_target_absent(resolved)
    for segment in _validate_domain(resolved, kind, workspace_root):
        reason = _invalid_name_reason(segment)
        if reason is not None:
            raise AdmissionError(
                "invalid_name",
                f"名称「{segment}」不能用作目录名：{reason}",
                {"segment": segment},
            )
    _assert_not_excluded(resolved, configured_root, workspace_root)
    _assert_no_anchor_conflict(resolved, configured_root, workspace_root)
    return resolved


def _assert_target_absent(resolved: Path) -> None:
    """检查 2：目标不存在（词法 normcase 与 resolved 身份双重比较）。"""
    if resolved.exists() or resolved.is_symlink():
        raise AdmissionError(
            "path_exists",
            f"目标路径已存在：{resolved}",
            {"target": str(resolved)},
        )
    target_name = os.path.normcase(resolved.name)
    try:
        siblings = list(resolved.parent.iterdir())
    except OSError:
        return
    for entry in siblings:
        if os.path.normcase(entry.name) == target_name:
            raise AdmissionError(
                "path_exists",
                f"目标路径已存在（大小写等价）：{entry}",
                {"target": str(entry)},
            )


def _validate_domain(
    resolved: Path, kind: AdmissionKind, workspace_root: str | Path
) -> list[str]:
    """检查 3：父目录领域归属（D0.2）；返回待做文件名校验的新建段。"""
    if kind == "model":
        return _validate_model_domain(resolved, workspace_root)
    if kind == "scheme":
        return _validate_scheme_domain(resolved)
    return _validate_asset_domain(resolved)


def _validate_model_domain(resolved: Path, workspace_root: str | Path) -> list[str]:
    root = Path(str(workspace_root)).resolve()
    if resolved.parent != root:
        raise AdmissionError(
            "domain_violation",
            f"型号目录必须直接位于工作区根之下：{resolved}",
        )
    layout = detect_workspace_layout(workspace_root)
    if layout == "single_model":
        raise AdmissionError(
            "migration_required",
            "当前工作区是旧单型号布局，请先完成工作区布局迁移再新增型号",
        )
    if layout == "invalid":
        raise AdmissionError(
            "layout_invalid",
            "工作区布局无法识别（存在无法归类的内容），禁止新增型号",
        )
    return [resolved.name]


def _validate_scheme_domain(resolved: Path) -> list[str]:
    parent = resolved.parent
    if parent.name != _CUSTOM_DIR:
        raise AdmissionError(
            "domain_violation",
            f"方案目录必须直接位于「{_CUSTOM_DIR}」之下：{resolved}",
        )
    owner = parent.parent
    if not _has_model_marker(owner):
        raise AdmissionError(
            "domain_violation",
            f"方案目录的上层不是型号根：{owner}",
        )
    return [resolved.name]


def _validate_asset_domain(resolved: Path) -> list[str]:
    owner = _find_model_root(resolved.parent)
    if owner is None:
        raise AdmissionError(
            "domain_violation",
            f"目标不在任何型号根的通用/定制结构之内：{resolved}",
        )
    parts = resolved.relative_to(owner).parts
    if parts[0] == _COMMON_DIR and len(parts) == 3:
        return [parts[1], parts[2]]
    if parts[0] == _CUSTOM_DIR and len(parts) == 4:
        scheme_dir = owner / _CUSTOM_DIR / parts[1]
        if not scheme_dir.is_dir():
            raise AdmissionError(
                "domain_violation",
                f"方案目录不存在，请先创建方案：{scheme_dir}",
            )
        return [parts[2], parts[3]]
    raise AdmissionError(
        "domain_violation",
        "新增程序必须位于 通用/<模块>/<程序名> 或 "
        f"定制/<方案>/<模块>/<程序名>：{resolved}",
    )


def _find_model_root(start: Path) -> Path | None:
    """向上回溯具型号标志的根（与 managed_paths._has_model_marker 同规则）。"""
    current = start
    for _ in range(6):
        if _has_model_marker(current):
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def _invalid_name_reason(name: str) -> str | None:
    """检查 4：Windows 文件名；合法返回 None。"""
    if not name or name in {".", ".."}:
        return "名称为空或是相对段"
    if any(char in _INVALID_NAME_CHARS for char in name):
        return "包含非法字符 <>:\"/\\|?*"
    if any(ord(char) < 0x20 for char in name):
        return "包含控制字符"
    if name.split(".", 1)[0].upper() in _RESERVED_NAMES:
        return "是 Windows 保留设备名"
    if name != name.rstrip(". "):
        return "以点或空格结尾"
    return None


def _assert_not_excluded(
    resolved: Path, configured_root: str | Path, workspace_root: str | Path
) -> None:
    """检查 5：与 scanner 完全相同的排除判定，作用于最终完整绝对路径。"""
    root = Path(str(configured_root))
    if _is_excluded_dir(str(root), Path(str(workspace_root))):
        raise AdmissionError(
            "workspace_excluded",
            f"工作区根路径命中扫描排除规则，该工作区不可用于写入：{root}",
        )
    if _is_excluded_dir(str(resolved), Path(str(workspace_root))):
        raise AdmissionError(
            "path_excluded",
            f"目标路径会被扫描排除，不能作为业务路径：{resolved}",
        )


def _assert_no_anchor_conflict(
    resolved: Path, configured_root: str | Path, workspace_root: str | Path
) -> None:
    """检查 6：悬空锚点命中或阻断级 issue 均拒绝（path_identity_conflict）。"""
    result = find_dangling_anchors(configured_root, workspace_root, resolved)
    if not result["ok"]:
        raise AdmissionError(result["code"], result["message"])
    lookup: ReferenceLookupResult | None = result["payload"].get("result")
    if lookup is None:
        return
    blocking = [issue for issue in lookup.issues if is_blocking_issue(issue)]
    if not lookup.hits and not blocking:
        return
    raise AdmissionError(
        "path_identity_conflict",
        "目标路径命中现存关联或默认值锚点"
        f"（命中 {len(lookup.hits)} 项，阻断级问题 {len(blocking)} 项）",
        {
            "hits": [
                {
                    "kind": hit.kind,
                    "owner_root": hit.owner_root,
                    "module_key": hit.module_key,
                    "raw_key": hit.raw_key,
                }
                for hit in lookup.hits
            ],
            "blocking_issues": [issue.category for issue in blocking],
        },
    )
