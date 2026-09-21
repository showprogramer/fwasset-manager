"""R8 引用反查（TASK-20260901-r8-reference-integrity）。

冷读盘上 TOML（目录树 + TOML 是真源），不依赖 SQLite 索引。三个公共入口共用
统一配置根 gate 与严格加载：任何阻断级 issue 都进入 ``issues``，不当空数据
静默降级（否则「零命中」与「查不全」无法区分，级联会留下断链）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from fwasset.core.file_scan import _is_excluded_dir
from fwasset.core.firmware_catalog import FirmwareTypeConfig
from fwasset.core.managed_paths import (
    RETIRED_VERSIONS_DIRNAME,
    managed_path_reason,
)
from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    load_model_config,
    load_shared_modules_strict,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    is_within_boundary,
    normalize_workspace_path,
    same_path_identity,
)
from fwasset.core.platform_config import (
    PLATFORM_CONFIG_FILENAME,
    PlatformDefaults,
    canonical_module_dir,
    load_platform_config_strict,
)
from fwasset.core.shared_module_resolver import resolve_shared_module
from fwasset.core.types import ReferenceTargetKind, ServiceResult

__all__ = [
    "LookupIssue",
    "ReferenceHit",
    "ReferenceLookupResult",
    "check_reference_gate",
    "enumerate_model_roots",
    "find_dangling_anchors",
    "find_references_to",
    "is_blocking_issue",
    "owner_model_root_for",
]

BLOCKING_ISSUE_CATEGORIES = frozenset(
    {
        "model_config_parse_error",
        "platform_parse_error",
        "parser_missing",
        "invalid_shared_entry",
        "missing_model_id",
        "duplicate_model_id",
    }
)


def is_blocking_issue(issue: LookupIssue) -> bool:
    """阻断级 issue：计划构建与删除预检遇到即拒绝。

    ``canonical_conflict`` / ``canonical_duplicate`` 属数据完整性提示：
    删除零 TOML 改写不受其影响；级联改写按**命中范围**单独限定阻止
    （见 reference_service._check_canonical_conflicts）。
    """
    return issue.category in BLOCKING_ISSUE_CATEGORIES


@dataclass
class LookupIssue:
    """严格读取问题：配置文件路径 + owner 型号根 + 类别。"""

    category: str
    config_path: str
    owner_root: str
    detail: str = ""
    anchor_path: str = ""
    source_root: str = ""
    raw_key: str = ""


@dataclass
class ReferenceHit:
    """一条会因目标失效或需要改写的引用。"""

    kind: str  # shared_static / shared_follow_default / shared_follow_asset / platform_default
    owner_root: str
    owner_model_id: str
    config_path: str
    module_key: str = ""  # shared: raw module_key；defaults: canonical key
    raw_key: str = ""  # shared: 原始条目键；defaults: 原始键（别名原样）
    canonical_key: str = ""
    raw_value: str = ""  # defaults: 原始变体名
    platform_name: str = ""  # defaults: 块名
    block_index: int = -1  # defaults: [[platform]] 块序号
    mode: str = ""  # shared: 引用 mode
    source_model_id: str = ""  # shared
    source_relative_path: str = ""  # shared
    current_target: str = ""  # 当前指向（尽力还原，悬空可为不存在路径）


@dataclass
class ReferenceLookupResult:
    """反查结果：命中 + issue（issue 非空 = 命中清单可能不完整）。"""

    hits: list[ReferenceHit] = field(default_factory=list)
    issues: list[LookupIssue] = field(default_factory=list)
    model_roots: list[str] = field(default_factory=list)


@dataclass
class _ModelEntry:
    """工作区扫描出的单个型号快照（严格读取产物）。"""

    root: Path
    model_id: str
    shared_pairs: list[tuple[str, SharedModuleRef]]  # (raw_key, ref)
    platforms: list[PlatformDefaults]


def _within_boundary(path: Path, boundary: Path) -> bool:
    """祖先/后代判定：委托 path_guard 公共 helper（词法+resolved，审查 P1-8）。"""
    return is_within_boundary(path, boundary)


def _ref_source_root(ref: SharedModuleRef, ws: Path) -> Path | None:
    """shared 引用的来源型号根（单/多布局统一归属规则）。"""
    rel = str(ref.source_relative_path or "").strip().replace("\\", "/")
    if not rel:
        return None
    first = Path(rel).parts[0] if Path(rel).parts else ""
    if first in ("通用", "定制"):
        return ws
    if not first:
        return None
    return ws / first


def _has_model_marker(root: Path) -> bool:
    """型号根领域标志：通用/定制 目录或两份 TOML 任一存在。

    配置按 ``is_file()`` 判定——同名**目录**不是有效标志（后续配置读取用不了
    它）。与 ``managed_paths._has_model_marker`` 保持同一语义，两处不一致会让
    布局判定与引用反查对同一目录给出不同归属。
    """
    return (
        (root / "通用").is_dir()
        or (root / "定制").is_dir()
        or (root / MODEL_CONFIG_FILENAME).is_file()
        or (root / PLATFORM_CONFIG_FILENAME).is_file()
    )


def enumerate_model_roots(workspace_root: Path) -> list[Path]:
    """枚举工作区内的型号根（规则 1 识别规则，写死）。

    工作区根本身带领域标志 → 单型号布局；否则枚举带标志的一级子目录。
    无任何标志的普通目录忽略、不产生 issue。返回 resolved 路径。
    """
    root = Path(workspace_root).resolve()
    if _has_model_marker(root):
        return [root]
    out: list[Path] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        if _is_excluded_dir(str(child), root):
            continue
        if _has_model_marker(child):
            out.append(child)
    return out


def check_reference_gate(
    configured_root: str | Path | None,
    workspace_root: str | Path,
) -> ServiceResult | None:
    """统一配置根 gate：空 → not_configured；resolved/normcase 不一致 → root_changed。"""
    configured = normalize_workspace_path(configured_root)
    if not configured:
        return {
            "ok": False,
            "code": "not_configured",
            "message": "请先在设置中配置程序文件夹",
            "payload": {},
        }
    ws = normalize_workspace_path(workspace_root)
    if ws != configured:
        return {
            "ok": False,
            "code": "root_changed",
            "message": "程序文件夹已变更，请重新读取程序列表后再操作",
            "payload": {"configured_root": configured, "workspace_root": ws},
        }
    return None


def _scan_workspace(
    workspace_root: Path,
) -> tuple[list[_ModelEntry], list[LookupIssue], dict[str, list[Path]]]:
    """严格扫描全部型号：shared 条目（含 raw key）+ 平台块 + id。

    返回 (entries, issues, model_id → roots)。任何阻断级问题进 issues。
    """
    ws = Path(workspace_root).resolve()
    entries: list[_ModelEntry] = []
    issues: list[LookupIssue] = []
    id_roots: dict[str, list[Path]] = {}

    for model_root in enumerate_model_roots(ws):
        mid, mid_status, mid_error = load_model_config(model_root)
        if mid_status == "parse_error":
            issues.append(
                LookupIssue(
                    category="model_config_parse_error",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=mid_error,
                )
            )
            continue
        if mid_status == "parser_missing":
            issues.append(
                LookupIssue(
                    category="parser_missing",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=mid_error,
                )
            )
            continue
        if mid_status != "ok" or not mid:
            issues.append(
                LookupIssue(
                    category="missing_model_id",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail="型号缺少合法 model_id",
                )
            )
            continue

        id_roots.setdefault(mid, []).append(model_root)
        pairs, invalid_keys, status, error = load_shared_modules_strict(model_root)
        if status == "parse_error":
            issues.append(
                LookupIssue(
                    category="model_config_parse_error",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=error,
                )
            )
            continue
        if status == "parser_missing":
            issues.append(
                LookupIssue(
                    category="parser_missing",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=error,
                )
            )
            continue
        for raw_key in invalid_keys:
            issues.append(
                LookupIssue(
                    category="invalid_shared_entry",
                    config_path=str(model_root / MODEL_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=f"共享引用条目结构非法或缺字段: {raw_key}",
                )
            )

        # 锚点边界校验：resolved 后越过工作区/来源型号根 → 结构非法（P1-8）
        for raw_key, ref in pairs:
            rel = str(ref.source_relative_path or "").strip().replace("\\", "/")
            if not rel:
                continue
            anchor = (ws / rel).resolve()
            source_root = _ref_source_root(ref, ws)
            if not _within_boundary(anchor, ws) or (
                source_root is not None and not _within_boundary(anchor, source_root)
            ):
                issues.append(
                    LookupIssue(
                        category="invalid_shared_entry",
                        config_path=str(model_root / MODEL_CONFIG_FILENAME),
                        owner_root=str(model_root),
                        detail=f"共享锚点越界或指向来源型号之外: {raw_key}",
                    )
                )

        platforms, plt_status, plt_error = load_platform_config_strict(model_root)
        if plt_status == "parse_error":
            issues.append(
                LookupIssue(
                    category="platform_parse_error",
                    config_path=str(model_root / PLATFORM_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=plt_error,
                )
            )
            platforms = []
        elif plt_status == "parser_missing":
            issues.append(
                LookupIssue(
                    category="parser_missing",
                    config_path=str(model_root / PLATFORM_CONFIG_FILENAME),
                    owner_root=str(model_root),
                    detail=plt_error,
                )
            )
            platforms = []

        # canonical 表级完整性（非阻断提示，级联按命中范围另行限定，P1-5）
        for block in platforms:
            groups: dict[str, set[str]] = {}
            for raw_key, raw_value in block.defaults.items():
                groups.setdefault(canonical_module_dir(raw_key), set()).add(raw_value)
            for canon, values in groups.items():
                if len(values) > 1:
                    issues.append(
                        LookupIssue(
                            category="canonical_conflict",
                            config_path=str(model_root / PLATFORM_CONFIG_FILENAME),
                            owner_root=str(model_root),
                            detail=f"defaults 模块「{canon}」存在多个别名键且默认值不同",
                        )
                    )
                elif len(
                    {k for k in block.defaults if canonical_module_dir(k) == canon}
                ) > 1:
                    issues.append(
                        LookupIssue(
                            category="canonical_duplicate",
                            config_path=str(model_root / PLATFORM_CONFIG_FILENAME),
                            owner_root=str(model_root),
                            detail=f"defaults 模块「{canon}」存在同值别名键",
                        )
                    )
        # shared 表同 canonical 组（审查第二轮 P1-6）
        shared_groups: dict[str, list[SharedModuleRef]] = {}
        for _raw_key, ref in pairs:
            shared_groups.setdefault(ref.module_key, []).append(ref)
        for canon, refs in shared_groups.items():
            if len(refs) < 2:
                continue
            signatures = {
                (
                    ref.mode,
                    ref.source_model_id,
                    ref.source_group,
                    ref.source_module,
                    os.path.normcase(str(ref.source_relative_path).replace("/", "\\")),
                    ref.source_platform,
                )
                for ref in refs
            }
            if len(signatures) > 1:
                issues.append(
                    LookupIssue(
                        category="canonical_conflict",
                        config_path=str(model_root / MODEL_CONFIG_FILENAME),
                        owner_root=str(model_root),
                        detail=f"共享模块「{canon}」存在多个别名条目且内容不同",
                    )
                )
            else:
                issues.append(
                    LookupIssue(
                        category="canonical_duplicate",
                        config_path=str(model_root / MODEL_CONFIG_FILENAME),
                        owner_root=str(model_root),
                        detail=f"共享模块「{canon}」存在同值别名条目",
                    )
                )

        entries.append(_ModelEntry(model_root, mid, pairs, platforms))

    for mid, roots in id_roots.items():
        if len(roots) > 1:
            for model_root in roots:
                issues.append(
                    LookupIssue(
                        category="duplicate_model_id",
                        config_path=str(model_root / MODEL_CONFIG_FILENAME),
                        owner_root=str(model_root),
                        detail=f"model_id「{mid}」被多个型号持有",
                    )
                )
    return entries, issues, id_roots


def _default_program_dir(model_root: Path, module_key: str, raw_value: str) -> Path:
    """defaults 条目 → 当前指向（尽力还原）：通用区模块目录 + 变体。

    先按 ``canonical_module_dir(子目录名) == module_key`` 命中；找不到时回退到
    catalog ``dir_keywords``（复用 scanner 的 ``_match_catalog_type``）。catalog
    不可用则保持合成 canonical 路径，不把关键词目录静默当命中。
    """
    common_dir = model_root / "通用"
    module_dir: Path | None = None
    if common_dir.is_dir():
        try:
            children = sorted(common_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and canonical_module_dir(child.name) == module_key:
                module_dir = child
                break
        if module_dir is None:
            module_dir = _module_dir_by_catalog_keywords(children, module_key, raw_value)
    if module_dir is None:
        module_dir = common_dir / module_key
    return module_dir / raw_value if raw_value else module_dir


def _module_dir_by_catalog_keywords(
    children: list[Path], module_key: str, raw_value: str
) -> Path | None:
    """canonical 名未命中时，按 catalog dir_keywords 回退匹配模块目录。

    「唯一」指通用区子目录中满足「命中条目 label 的 canonical == module_key」
    的目录个数为 1；0 个或 ≥2 个都不把任一关键词目录静默当命中。
    """
    context = _catalog_context()
    if context is None:
        return None
    _labels, types = context
    matches: list[Path] = []
    for child in children:
        if not child.is_dir():
            continue
        candidate = child / raw_value if raw_value else child
        matched = _is_catalog_asset_dir(candidate, types)
        if matched is None:
            continue
        if canonical_module_dir(str(matched.get("label", ""))) == module_key:
            matches.append(child)
    if len(matches) == 1:
        return matches[0]
    return None


def _shared_hit(
    kind: str,
    entry: _ModelEntry,
    raw_key: str,
    ref: SharedModuleRef,
    current_target: str,
) -> ReferenceHit:
    """组装 shared 类 ReferenceHit。"""
    return ReferenceHit(
        kind=kind,
        owner_root=str(entry.root),
        owner_model_id=entry.model_id,
        config_path=str(entry.root / MODEL_CONFIG_FILENAME),
        module_key=raw_key,
        raw_key=raw_key,
        canonical_key=ref.module_key,
        mode=ref.mode,
        source_model_id=ref.source_model_id,
        source_relative_path=ref.source_relative_path,
        current_target=current_target,
    )


def _collect_shared_hits(
    entries: list[_ModelEntry],
    ws: Path,
    target: Path,
    target_kind: str,
) -> list[ReferenceHit]:
    """shared 引用命中（规则 1 矩阵，owner 限定 + 联合身份，审查 P1-1）。

    - static/follow_asset：锚定路径位于 target 子树内；model 级另校验
      「来源型号根 + model_id + 锚点首段归属」联合身份；
    - follow_default：asset 级按 resolved 身份；module/model 级按
      「来源型号根 + canonical 模块」语义命中（默认缺失也保留 hit）。
    """
    target_owner = _owner_root_for(target, entries)
    target_owner_id: str | None = None
    if target_owner is not None:
        target_owner_id = target_owner.model_id
    target_canon_module = (
        canonical_module_dir(target.name) if target_kind in ("module",) else None
    )
    hits: list[ReferenceHit] = []
    for entry in entries:
        for raw_key, ref in entry.shared_pairs:
            if ref.mode in ("static", "follow_asset"):
                # 保留未 resolve 的词法锚点（junction 词法关系不能丢，审查第三轮 P1-1）；
                # is_within_boundary 内部会再做 resolved 判定
                anchor = ws / Path(str(ref.source_relative_path).replace("\\", "/"))
                if not (
                    same_path_identity(anchor, target)
                    or _within_boundary(anchor, target)
                ):
                    continue
                if target_kind == "model" and target_owner is not None:
                    # 联合身份：来源根指向目标型号 + id 一致，防重复 id 误命中
                    source_root = _ref_source_root(ref, ws)
                    if source_root is None or not same_path_identity(
                        source_root, target_owner.root
                    ):
                        continue
                    if ref.source_model_id != target_owner_id:
                        continue
                hits.append(
                    _shared_hit(
                        f"shared_{ref.mode}", entry, raw_key, ref, str(anchor)
                    )
                )
            elif ref.mode == "follow_default":
                if target_kind == "asset":
                    resolution = resolve_shared_module(ref, ws)
                    if resolution.status != "hit" or resolution.resolved_path is None:
                        continue
                    resolved = resolution.resolved_path
                    if not _within_boundary(resolved, target):
                        continue
                    hits.append(
                        _shared_hit(
                            "shared_follow_default",
                            entry,
                            raw_key,
                            ref,
                            str(resolved),
                        )
                    )
                elif target_kind in ("module", "model"):
                    # 语义命中：来源根指向目标 + canonical 模块匹配（默认可缺失）。
                    # 联合身份含 source_model_id 校验（审查第二轮 P1-3）。
                    source_root = _ref_source_root(ref, ws)
                    if target_owner is None or source_root is None:
                        continue
                    if not same_path_identity(source_root, target_owner.root):
                        continue
                    if ref.source_model_id != target_owner.model_id:
                        continue
                    if target_kind == "module":
                        if canonical_module_dir(ref.source_module) != target_canon_module:
                            continue
                    hits.append(
                        _shared_hit(
                            "shared_follow_default",
                            entry,
                            raw_key,
                            ref,
                            str(target),
                        )
                    )
    return hits


def _collect_defaults_hits(
    entries: list[_ModelEntry],
    target: Path,
    target_kind: str,
) -> list[ReferenceHit]:
    """平台默认命中（owner 限定：只查目标所属型号的 defaults，P1-1）。

    型号级删除不算断链（自身 defaults 随型号删除）。
    """
    hits: list[ReferenceHit] = []
    target_owner = _owner_root_for(target, entries)
    for entry in entries:
        if target_kind == "model" and target_owner is not None and same_path_identity(
            entry.root, target_owner.root
        ):
            continue
        if target_owner is not None and not same_path_identity(entry.root, target_owner.root):
            # defaults 只会指向本型号通用区，跨型号 defaults 不存在
            continue
        for block_index, block in enumerate(entry.platforms):
            for raw_key, raw_value in block.defaults.items():
                canon = canonical_module_dir(raw_key)
                if target_kind == "module":
                    if canon != canonical_module_dir(target.name):
                        continue
                else:
                    default_dir = _default_program_dir(entry.root, canon, raw_value)
                    if not _within_boundary(default_dir, target):
                        continue
                hits.append(
                    ReferenceHit(
                        kind="platform_default",
                        owner_root=str(entry.root),
                        owner_model_id=entry.model_id,
                        config_path=str(entry.root / PLATFORM_CONFIG_FILENAME),
                        module_key=canon,
                        raw_key=raw_key,
                        canonical_key=canon,
                        raw_value=raw_value,
                        platform_name=block.platform_name,
                        block_index=block_index,
                        current_target=str(
                            _default_program_dir(entry.root, canon, raw_value)
                        ),
                    )
                )
    return hits


def _validate_target_kind(
    target: Path,
    target_kind: ReferenceTargetKind,
) -> str | None:
    """target_kind 领域身份验证（现存路径，结构规则写死；审查 P1-2）。

    - model：领域标志（通用/定制 目录或两份 TOML）；
    - scheme：直接位于「定制」之下的目录；
    - module：「通用」的直接子目录或方案（「定制」的直接子目录）的直接子目录；
    - asset：通用/定制结构下的叶子层级——通用区 2~3 段、定制区 3~4 段
      （相对型号根），且不是型号根/方案根/模块容器歧义目录。
    """
    if target_kind == "model":
        if not _has_model_marker(target):
            return "目标目录缺少型号领域标志（通用/定制/型号配置.toml/平台配置.toml）"
        return None
    if not target.is_dir():
        return "目标路径不存在或不是目录"
    if target_kind == "scheme":
        if target.parent.name != "定制":
            return "方案目录必须直接位于「定制」之下"
        return None
    if target_kind == "module":
        if target.parent.name == "通用" or target.parent.parent.name == "定制":
            return None
        return "模块目录必须位于「通用」或方案（「定制」的直接子目录）之下"
    # asset：层级结构 + 变体容器 + catalog 上下文校验（审查第三轮 P1-2）
    if _has_model_marker(target):
        return "目标目录是型号根，不是程序目录"
    if target.parent.name == "定制":
        return "目标目录是方案目录，不是程序目录"
    structural, at_module_leaf = _structural_asset_depth(target)
    if not structural:
        return "目录层级不符合程序（asset）结构：通用区须为 模块[/变体]，定制区须为 方案/模块[/变体]"
    if at_module_leaf and _has_variant_children(target):
        return "目标目录是含变体的模块容器，不是程序目录"
    context = _catalog_context()
    if context is None:
        # fail-closed：catalog 不可用时不猜测 asset 身份
        return "程序类型目录不可用，无法确认 asset 身份"
    _labels, types = context
    module_dir = target if at_module_leaf else target.parent
    matched = _is_catalog_asset_dir(target, types)
    if matched is None:
        return "目录内没有可识别的程序文件，不是程序目录"
    # 模块类型一致性：matched cfg 的 label/dir_keywords 须与模块目录对应。
    # 全部比较统一 casefold（与 scanner 的小写关键词匹配一致，审查第六轮 P1-1）。
    module_canon = canonical_module_dir(module_dir.name).casefold()
    label_canon = canonical_module_dir(str(matched.get("label", ""))).casefold()
    keywords = [
        canonical_module_dir(str(k).strip().lower()).casefold()
        for k in matched.get("dir_keywords", [])
        if str(k).strip()
    ]
    if module_canon != label_canon and not any(
        kw and kw in module_canon for kw in keywords
    ):
        return (
            f"模块「{module_dir.name}」与匹配到的程序类型"
            f"「{matched.get('label', '')}」不一致，不能作为程序目录"
        )
    return None


def _catalog_context() -> tuple[set[str], list[FirmwareTypeConfig]] | None:
    """catalog 上下文：(canonical labels, enabled types)。不可用返回 None（fail-closed）。"""
    try:
        from fwasset.core.firmware_catalog import enabled_firmware_types

        types: list[FirmwareTypeConfig] = list(enabled_firmware_types())
    except Exception:  # noqa: BLE001 — catalog 缺失/损坏时不得静默降级（审查第三轮 P1-2）
        return None
    labels = {canonical_module_dir(str(item.get("label", ""))) for item in types}
    return labels, types


def _is_catalog_asset_dir(
    target: Path, types: list[FirmwareTypeConfig]
) -> FirmwareTypeConfig | None:
    """目录是否为 scanner 语义下可识别的资产目录（审查第四轮 P1-2）。

    复用 ``file_scan._match_catalog_type``：保留 catalog 顺序、``dir_keywords``
    与 ``handcontrol_ui`` 的 ``.rom + .pkg`` 双文件联合规则，与正式扫描的资产
    身份语义完全一致。
    """
    from fwasset.core.file_scan import _match_catalog_type

    try:
        filenames = [f.name for f in target.iterdir() if f.is_file()]
    except OSError:
        return None
    return _match_catalog_type(str(target), filenames, types)


def _structural_asset_depth(target: Path) -> tuple[bool, bool]:
    """asset 结构校验：返回 (结构合法, 处于模块叶子层)。

    找到所属型号根后按 通用/定制 层级判定：通用区 2~3 段、定制区 3~4 段
    （相对型号根）。
    """
    root = target
    for _ in range(6):  # 型号根最多向上回溯 6 层
        if _has_model_marker(root):
            break
        if root.parent == root:
            return False, False
        root = root.parent
    else:
        return False, False
    try:
        rel = target.resolve().relative_to(root.resolve())
    except ValueError:
        return False, False
    parts = rel.parts
    if not parts:
        return False, False
    offset = 0
    if parts[0] not in ("通用", "定制"):
        offset = 1  # 跳过型号目录层
        if len(parts) <= offset:
            return False, False
    category = parts[offset]
    rest = len(parts) - offset - 1
    if category == "通用":
        return rest in (1, 2), rest == 1  # 模块叶子 / 模块/变体
    if category == "定制":
        return rest in (2, 3), rest == 2  # 方案/模块叶子 / 方案/模块/变体
    return False, False


def _has_variant_children(target: Path) -> bool:
    """目录下是否存在非排除的子目录（变体容器判定，审查第二轮 P1-2）。"""
    try:
        for child in target.iterdir():
            if child.is_dir() and not _is_excluded_dir(str(child)):
                return True
    except OSError:
        return False
    return False


def _error_result(code: str, message: str, payload: dict | None = None) -> ServiceResult:
    return {
        "ok": False,
        "code": code,
        "message": message,
        "payload": payload or {},
    }


def _retired_copy_root(path: Path) -> Path | None:
    """路径若含 ``旧版本`` 段，返回该副本目录（``旧版本`` 的直接子目录）。"""
    retired = os.path.normcase(RETIRED_VERSIONS_DIRNAME)
    parts = path.parts
    for index, part in enumerate(parts):
        if os.path.normcase(part) != retired:
            continue
        if index + 1 < len(parts):
            return Path(*parts[: index + 2])
        return Path(*parts[: index + 1])
    return None


def _retired_anchor_issue_for(
    *,
    config_path: str,
    owner_root: str,
    raw_key: str,
    anchor: Path,
) -> LookupIssue | None:
    if managed_path_reason(anchor, is_dir=True) != "retired_versions":
        return None
    copy_root = _retired_copy_root(anchor)
    return LookupIssue(
        category="retired_anchor",
        config_path=config_path,
        owner_root=owner_root,
        detail=f"借用锚点落在旧版本：{anchor}",
        anchor_path=str(anchor),
        source_root=str(copy_root) if copy_root is not None else str(anchor),
        raw_key=raw_key,
    )


def _collect_retired_anchor_issues(
    entries: list[_ModelEntry], ws: Path
) -> list[LookupIssue]:
    """扫描全部 shared 引用，锚点落在 ``旧版本/`` 时发出 issue（与本次查询目标无关）。"""
    issues: list[LookupIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        config_path = str(entry.root / MODEL_CONFIG_FILENAME)
        for raw_key, ref in entry.shared_pairs:
            anchors: list[Path] = []
            if ref.mode in ("static", "follow_asset"):
                anchors.append(ws / Path(str(ref.source_relative_path).replace("\\", "/")))
            elif ref.mode == "follow_default":
                resolution = resolve_shared_module(ref, ws)
                if resolution.resolved_path is not None:
                    anchors.append(resolution.resolved_path)
            for anchor in anchors:
                issue = _retired_anchor_issue_for(
                    config_path=config_path,
                    owner_root=str(entry.root),
                    raw_key=raw_key,
                    anchor=anchor,
                )
                if issue is None:
                    continue
                key = (issue.config_path, issue.raw_key, issue.anchor_path)
                if key in seen:
                    continue
                seen.add(key)
                issues.append(issue)
    return issues


def _retired_anchor_related(target: Path, issue: LookupIssue) -> bool:
    """查询目标与 issue 的锚点/来源是否同一身份或祖先-后代。"""
    for related in (issue.anchor_path, issue.source_root):
        if not related:
            continue
        other = Path(related)
        if same_path_identity(target, other):
            return True
        if is_within_boundary(target, other) or is_within_boundary(other, target):
            return True
    return False


def find_references_to(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    target_path: str | Path,
    target_kind: ReferenceTargetKind,
) -> ServiceResult:
    """反查会因 ``target_path`` 失效或需要改写的 TOML 引用（规则 1）。

    返回 ``ok=True`` 时 payload 携带 ``ReferenceLookupResult``；``issues`` 非空
    表示命中清单可能不完整，删除预检/计划构建必须按阻止处理。
    ``retired_anchor`` 不进全局阻断集合：仅当查询目标与该锚点有边界关系时
    本次返回 ``ok=False``。
    """
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    try:
        target = assert_within_workspace(target_path, workspace_root)
    except PathGuardError as exc:
        return _error_result("out_of_workspace", f"目标不在工作区内：{exc}")
    if not target.exists():
        return _error_result("invalid_target", f"目标路径不存在：{target}")

    kind_detail = _validate_target_kind(target, target_kind)
    if kind_detail is not None:
        return _error_result("invalid_target", f"目标类型不匹配：{kind_detail}")

    ws = Path(workspace_root).resolve()
    entries, issues, _id_roots = _scan_workspace(ws)
    hits = _collect_shared_hits(entries, ws, target, target_kind)
    hits.extend(_collect_defaults_hits(entries, target, target_kind))
    issues.extend(_collect_retired_anchor_issues(entries, ws))
    hits.sort(key=lambda h: (h.owner_root, h.kind, h.module_key, h.raw_key))
    result = ReferenceLookupResult(
        hits=hits,
        issues=issues,
        model_roots=[str(e.root) for e in entries],
    )
    related = next(
        (
            issue
            for issue in issues
            if issue.category == "retired_anchor" and _retired_anchor_related(target, issue)
        ),
        None,
    )
    if related is not None:
        return _error_result(
            "retired_anchor",
            "目标与旧版本中的借用锚点存在路径关系，已阻止",
            {"result": result},
        )
    return {
        "ok": True,
        "code": "ok",
        "message": f"反查完成：{len(hits)} 条命中，{len(issues)} 条 issue",
        "payload": {"result": result},
    }


def find_dangling_anchors(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    path: str | Path,
) -> ServiceResult:
    """路径身份复用检查（规则 7）：``path`` 上的现存/悬空锚点。

    允许 ``path`` 不存在。两类锚点：
    ① static/follow_asset——由 ``source_relative_path`` 还原；
    ② platform_default——候选路径驱动：从 ``path`` 结构派生 owner/模块/变体
    （模块叶子 variant=""），按 canonical key + raw value 匹配 defaults；
    实际目录存在仅作交叉验证，非必要条件（模块已删 + alias 场景仍命中）。
    兼容期 follow_default 不作固定路径锚点。
    """
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    try:
        candidate = assert_within_workspace(path, workspace_root)
    except PathGuardError as exc:
        return _error_result("out_of_workspace", f"目标不在工作区内：{exc}")

    ws = Path(workspace_root).resolve()
    entries, issues, _id_roots = _scan_workspace(ws)
    hits: list[ReferenceHit] = []

    # ① shared 锚点：锚定路径与候选等价或双向边界重叠（词法+resolved，P1-8）
    for entry in entries:
        for raw_key, ref in entry.shared_pairs:
            if ref.mode not in ("static", "follow_asset"):
                continue
            # 保留未 resolve 的词法锚点（审查第三轮 P1-1）
            anchor = ws / Path(str(ref.source_relative_path).replace("\\", "/"))
            if (
                same_path_identity(anchor, candidate)
                or _within_boundary(candidate, anchor)
                or _within_boundary(anchor, candidate)
            ):
                hits.append(
                    _shared_hit(f"shared_{ref.mode}", entry, raw_key, ref, str(anchor))
                )

    # ② platform_default 锚点：候选路径驱动匹配
    rel_parts = _relative_parts(candidate, ws)
    if rel_parts is not None:
        generic_idx = next(
            (i for i, p in enumerate(rel_parts) if p == "通用"), None
        )
        if generic_idx is not None and generic_idx + 1 < len(rel_parts):
            module_name = rel_parts[generic_idx + 1]
            variant = rel_parts[generic_idx + 2] if generic_idx + 2 < len(rel_parts) else ""
            owner = _owner_root_for(candidate, entries)
            if owner is not None:
                canon = canonical_module_dir(module_name)
                for block_index, block in enumerate(owner.platforms):
                    for raw_key, raw_value in block.defaults.items():
                        if canonical_module_dir(raw_key) != canon:
                            continue
                        value_matches = (
                            raw_value == "" and variant == ""
                        ) or _norm_value(raw_value) == _norm_value(variant)
                        if not value_matches:
                            continue
                        hits.append(
                            ReferenceHit(
                                kind="platform_default",
                                owner_root=str(owner.root),
                                owner_model_id=owner.model_id,
                                config_path=str(
                                    owner.root / PLATFORM_CONFIG_FILENAME
                                ),
                                module_key=canon,
                                raw_key=raw_key,
                                canonical_key=canon,
                                raw_value=raw_value,
                                platform_name=block.platform_name,
                                block_index=block_index,
                                current_target=str(candidate),
                            )
                        )

    hits.sort(key=lambda h: (h.owner_root, h.kind, h.module_key))
    result = ReferenceLookupResult(
        hits=hits,
        issues=issues,
        model_roots=[str(e.root) for e in entries],
    )
    return {
        "ok": True,
        "code": "ok",
        "message": f"锚点检查完成：{len(hits)} 条命中，{len(issues)} 条 issue",
        "payload": {"result": result},
    }


def _norm_value(value: str) -> str:
    """defaults 变体名比较：normcase 归一（大小写/分隔符差异不误判）。"""
    return os.path.normcase(str(value or "").strip().replace("\\", "/"))


def _relative_parts(candidate: Path, ws: Path) -> tuple[str, ...] | None:
    """候选路径相对工作区的 parts；越界返回 None。"""
    try:
        return candidate.resolve().relative_to(ws).parts
    except ValueError:
        return None


def owner_model_root_for(
    candidate: Path, model_roots: Iterable[Path]
) -> Path | None:
    """候选路径所属型号根（规则 1 最深匹配语义，归属规则单一真源）。

    在 ``model_roots``（一般来自 :func:`enumerate_model_roots`）中返回作为
    ``candidate`` 最深祖先的型号根；不属于任何型号根返回 None。
    供本模块反查与扫描链（file_scan 的 ``chassis_type`` 归属）共同复用，
    归属规则不得在调用方复制第二套。
    """
    resolved = candidate.resolve()
    best: Path | None = None
    best_rel_len: int | None = None
    for root in model_roots:
        try:
            rel = resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        # 最深匹配 = rel.parts 最少（型号根是 candidate 的最近祖先）；
        # 平局保持首个出现的根（真实布局下型号根互不嵌套，不会出现平局）。
        if best_rel_len is None or len(rel.parts) < best_rel_len:
            best = Path(root)
            best_rel_len = len(rel.parts)
    return best


def _owner_root_for(candidate: Path, entries: list[_ModelEntry]) -> _ModelEntry | None:
    """候选路径所属型号根（最深匹配），委托 :func:`owner_model_root_for`。"""
    best_root = owner_model_root_for(candidate, (entry.root for entry in entries))
    if best_root is None:
        return None
    for entry in entries:
        if same_path_identity(Path(entry.root), best_root):
            return entry
    return None
