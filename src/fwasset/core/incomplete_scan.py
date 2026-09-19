"""待补齐候选区扫描（TASK-20260918-asset-crud-incomplete，父规格 D7.5）。

与 :mod:`fwasset.core.file_scan` 并列、不混入普通 scanner——候选区是应用
内部受管区域，`should_exclude_managed_path` 已让普通 scanner 完全跳过它
（双盲区的一半）；本模块补上「候选 scanner 认」的另一半。

**可发现性不变量**（D7.5 明文）：候选目录在任何中间状态下崩溃后都必须能
被本入口发现。实现只依赖磁盘上的目录存在 + 元数据文件，不依赖任何内存态
或事务日志；元数据写失败时候选目录也要留下（带诊断 issue 报出），不静默
跳过。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from fwasset.core.asset_info import (
    IMPORT_STATE_INCOMPLETE,
    import_state_from_asset_info,
    intended_firmware_type_from_asset_info,
    load_asset_info_with_status,
    vendor_from_asset_info,
)
from fwasset.core.file_scan import classify_staged_content
from fwasset.core.managed_paths import ASSET_METADATA_FILENAME, managed_root
from fwasset.core.types import ScanIssue

__all__ = ["IncompleteCandidate", "scan_incomplete_imports"]


@dataclass(frozen=True)
class IncompleteCandidate:
    """一条待补齐候选项快照（严格读取产物，供 UI 「软件修复」列表使用）。

    ``ready_to_promote``：候选内容已重新匹配到某个 catalog 类型（用户手工
    塞了缺失文件），但仍需用户显式选择新增还是更新——不自动提升。
    """

    candidate_id: str
    path: str
    vendor: str
    intended_firmware_type: str
    ready_to_promote: bool


def scan_incomplete_imports(
    workspace_root: str | Path,
) -> tuple[list[IncompleteCandidate], list[ScanIssue]]:
    """遍历受管候选区，按元数据识别待补齐项，诊断分级不静默跳过。"""
    root = managed_root(workspace_root, "incomplete_candidate")
    candidates: list[IncompleteCandidate] = []
    issues: list[ScanIssue] = []

    if not root.is_dir():
        return candidates, issues

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        issues.append(
            {"severity": "error", "message": f"候选区无法读取：{exc}", "path": str(root)}
        )
        return candidates, issues

    for entry in entries:
        if not entry.is_dir():
            continue
        candidates_result, entry_issues = _scan_one_candidate(entry)
        if candidates_result is not None:
            candidates.append(candidates_result)
        issues.extend(entry_issues)

    return candidates, issues


def _candidate_content_files(candidate_dir: Path) -> list[str]:
    """候选目录内容文件清单（相对路径，正斜杠归一）。

    与 create 分流（``asset_service._session_filenames``）同一口径：递归收集
    全部文件的相对路径；**根层**的受管元数据（``程序信息.toml``）不计入内容
    ——它是候选期元数据，不是固件文件，把元数据当内容会让「仅元数据」的空
    候选逃过「目录空」诊断，也会让完整性判定虚高。
    """
    names: list[str] = []
    for entry in sorted(candidate_dir.rglob("*")):
        if entry.is_file() and not (
            entry.parent == candidate_dir
            and os.path.normcase(entry.name) == os.path.normcase(ASSET_METADATA_FILENAME)
        ):
            names.append(str(entry.relative_to(candidate_dir)).replace("\\", "/"))
    return names


def _scan_one_candidate(
    candidate_dir: Path,
) -> tuple[IncompleteCandidate | None, list[ScanIssue]]:
    issues: list[ScanIssue] = []
    candidate_id = candidate_dir.name

    # ACI-007：空目录诊断与完整性判定使用同一份内容清单——排除受管元数据、
    # 递归收集相对路径，与 create 分流的 staging 清单口径一致（同一内容在
    # 两个入口不得得出不同结论）。
    try:
        content_files = _candidate_content_files(candidate_dir)
    except OSError as exc:
        issues.append(
            {
                "severity": "error",
                "message": f"候选目录无法读取：{exc}",
                "path": str(candidate_dir),
            }
        )
        return None, issues
    if not content_files:
        issues.append(
            {
                "severity": "warning",
                "message": "候选目录为空（可能是崩溃残留），建议删除后重新导入",
                "path": str(candidate_dir),
            }
        )
        return None, issues

    data, status, error = load_asset_info_with_status(candidate_dir)
    if status == "missing":
        issues.append(
            {
                "severity": "warning",
                "message": "候选目录缺少元数据文件，可能是崩溃残留",
                "path": str(candidate_dir / ASSET_METADATA_FILENAME),
            }
        )
        return None, issues
    if status in ("parse_error", "parser_missing"):
        issues.append(
            {
                "severity": "warning",
                "message": f"候选元数据读取失败（{status}）：{error}",
                "path": str(candidate_dir / ASSET_METADATA_FILENAME),
            }
        )
        return None, issues

    import_state = import_state_from_asset_info(data)
    if import_state == "":
        issues.append(
            {
                "severity": "warning",
                "message": "候选目录元数据缺少 import_state，可能已补齐但未清理，或元数据损坏",
                "path": str(candidate_dir / ASSET_METADATA_FILENAME),
            }
        )
        return None, issues
    if import_state != IMPORT_STATE_INCOMPLETE:
        issues.append(
            {
                "severity": "warning",
                "message": f"候选目录 import_state 值非法：{import_state!r}",
                "path": str(candidate_dir / ASSET_METADATA_FILENAME),
            }
        )
        return None, issues

    vendor = vendor_from_asset_info(data)
    intended_firmware_type = intended_firmware_type_from_asset_info(data)

    matched = classify_staged_content(candidate_dir, content_files)
    ready_to_promote = matched is not None

    return (
        IncompleteCandidate(
            candidate_id=candidate_id,
            path=str(candidate_dir),
            vendor=vendor,
            intended_firmware_type=intended_firmware_type,
            ready_to_promote=ready_to_promote,
        ),
        issues,
    )
