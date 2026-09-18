"""R8 级联改写与借用语义迁移服务（TASK-20260901-r8-reference-integrity）。

提供三个公共入口（均返回 :class:`fwasset.core.types.ServiceResult`）：

- ``build_rewrite_plan``：纯函数，按操作矩阵产出 ``RewritePlan``（preimage 字节 +
  双 SHA-256 + pre/post 双路径），不改盘；
- ``apply_rewrite_plan``：先全量校验（统一 gate + preimage hash），再逐文件原子替换；
  任一文件失败按 preimage 做 CAS 回滚（当前字节 == 本 plan 新字节才恢复，否则
  ``rollback_conflict`` 保留现状并给出恢复信息）；
- ``migrate_follow_default_refs``：存量 ``follow_default`` → ``follow_asset`` 幂等迁移。

删除动作**不改写**任何 TOML（规则 5）：借用条目保留、解析自然 missing、回收站
撤销即恢复；删除前用 ``reference_lookup.find_references_to`` 做二次确认清单。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tomllib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fwasset.core.config_io import atomic_write_text
from fwasset.core.model_config import (
    MODEL_CONFIG_FILENAME,
    SharedModuleRef,
    load_model_config,
    serialize_model_config,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    is_same_or_under,
    is_within_boundary,
    normalize_workspace_path,
    same_path_identity,
)
from fwasset.core.platform_config import (
    PLATFORM_CONFIG_FILENAME,
    canonical_module_dir,
    serialize_platform_config,
)
from fwasset.core.reference_lookup import (
    LookupIssue,
    ReferenceHit,
    _error_result,
    _ModelEntry,
    _scan_workspace,
    _validate_target_kind,
    check_reference_gate,
    is_blocking_issue,
)
from fwasset.core.scheme_config import (
    SCHEME_CONFIG_FILENAME,
    serialize_scheme_config,
)
from fwasset.core.shared_module_resolver import resolve_shared_module
from fwasset.core.types import RewriteRequest, ServiceResult

__all__ = [
    "FileRewrite",
    "RewritePlan",
    "apply_rewrite_plan",
    "build_rewrite_plan",
    "migrate_follow_default_refs",
]


@dataclass
class FileRewrite:
    """单个 TOML 文件的改写计划条目。"""

    kind: str  # model_config / platform_config
    pre_path: Path  # 文件动作前的配置路径
    post_path: Path  # 文件动作后的配置路径（型号自身配置随目录移动时不同）
    owner_root: str
    original_bytes: bytes | None  # None = 原文件不存在
    original_sha256: str
    new_content: str
    new_sha256: str
    changes: list[str] = field(default_factory=list)


@dataclass
class RewritePlan:
    """级联改写计划（规则 4）：固化校验基准，apply 前重验。

    ``token`` 是 build 时的全内容指纹（含进程内随机盐）：apply 重算并比对，
    伪造/手工拼接的 plan 无法通过（审查第二轮 P1-1）。
    """

    configured_root: str
    workspace_root: str
    request: RewriteRequest
    files: list[FileRewrite] = field(default_factory=list)
    hits: list[ReferenceHit] = field(default_factory=list)
    token: str = field(default="", repr=False)


_PLAN_SALT = secrets.token_hex(16)

#: 计划条目类型 → 期望文件名（D3 修订：新增 scheme_config，由 dict 查表
#: 取代原先的二选一三元表达式，_validate_plan_for_apply 与 build 侧共用）。
_PLAN_ENTRY_FILENAMES: dict[str, str] = {
    "model_config": MODEL_CONFIG_FILENAME,
    "platform_config": PLATFORM_CONFIG_FILENAME,
    "scheme_config": SCHEME_CONFIG_FILENAME,
}


def _plan_token(plan: RewritePlan) -> str:
    """计划全内容指纹：任何字段篡改都会导致 token 失配。"""
    payload = {
        "salt": _PLAN_SALT,
        "configured_root": plan.configured_root,
        "workspace_root": plan.workspace_root,
        "request": asdict(plan.request),
        "files": [
            [
                f.kind,
                str(f.pre_path),
                str(f.post_path),
                f.original_sha256,
                "" if f.original_bytes is None else _sha256(f.original_bytes),
                f.new_content,
                f.new_sha256,
            ]
            for f in plan.files
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_to_ws(path: Path, ws: Path) -> str | None:
    """绝对路径 → 工作区相对路径（统一 ``/``）；越界返回 None。"""
    try:
        resolved = Path(path).resolve()
        return "/".join(resolved.relative_to(ws).parts)
    except (OSError, ValueError):
        return None


def _source_model_root(path: Path, ws: Path) -> Path | None:
    """路径所属型号根：首段为 通用/定制 → 单型号布局（工作区根）；否则 首段目录。"""
    rel = _relative_to_ws(path, ws)
    if rel is None:
        return None
    parts = rel.split("/")
    if parts and parts[0] in ("通用", "定制"):
        return ws
    if parts:
        return ws / parts[0]
    return None


def _category_and_scheme(rel_parts: tuple[str, ...]) -> tuple[str, str, int]:
    """从相对 parts 提取 (category, scheme_name, module 部分起始下标)。

    多型号布局：``[<model>, 通用|定制, (方案), 模块, ...]``；
    单型号布局：``[通用|定制, (方案), 模块, ...]``。
    """
    offset = 0
    if rel_parts and rel_parts[0] not in ("通用", "定制"):
        offset = 1
    category = rel_parts[offset] if len(rel_parts) > offset else ""
    scheme = ""
    module_idx = offset + 1
    if category == "定制":
        if len(rel_parts) > offset + 1:
            scheme = rel_parts[offset + 1]
            module_idx = offset + 2
    return category, scheme, module_idx


def _derive_semantics(path: Path, ws: Path) -> dict[str, str] | None:
    """从盘上路径与型号配置派生权威语义快照；无法定位返回 None。"""
    model_root = _source_model_root(path, ws)
    if model_root is None:
        return None
    mid, status, _err = load_model_config(model_root)
    if status != "ok" or not mid:
        return None
    rel = _relative_to_ws(path, ws)
    if rel is None:
        return None
    parts = tuple(rel.split("/"))
    _category, scheme, module_idx = _category_and_scheme(parts)
    module_name = parts[module_idx] if len(parts) > module_idx else ""
    return {
        "model_id": mid,
        "module_key": canonical_module_dir(module_name),
        "source_group": mid,
        "scheme_name": scheme,
    }


def _semantics_equal(a: dict[str, str], b: dict[str, str]) -> bool:
    keys = ("model_id", "module_key", "source_group", "scheme_name")
    return all(str(a.get(k, "")).strip() == str(b.get(k, "")).strip() for k in keys)


def _module_variant_value(new_path: Path, ws: Path) -> str | None:
    """defaults value 派生（规则 4）：模块叶子 → ""；变体 → 变体目录名；其它 None。"""
    rel = _relative_to_ws(new_path, ws)
    if rel is None:
        return None
    parts = tuple(rel.split("/"))
    _cat, _scheme, module_idx = _category_and_scheme(parts)
    if len(parts) == module_idx + 1:
        return ""  # 模块目录本身就是唯一程序
    if len(parts) == module_idx + 2:
        return parts[module_idx + 1]
    return None  # 更深层级不属于 defaults 可表达范围


def _collect_rewrite_hits(
    ws: Path,
    request: RewriteRequest,
) -> tuple[list[ReferenceHit], list[LookupIssue], list[_ModelEntry]]:
    """按 target_kind 聚合反查（复用 lookup 的扫描与命中判定）。"""
    from fwasset.core.reference_lookup import (
        _collect_defaults_hits,
        _collect_shared_hits,
    )

    old_path = Path(request.old_path).resolve()
    entries, issues, _id_roots = _scan_workspace(ws)
    hits = _collect_shared_hits(entries, ws, old_path, request.target_kind)
    hits.extend(_collect_defaults_hits(entries, old_path, request.target_kind))
    return hits, issues, entries


def _check_canonical_conflicts(
    entries: list[_ModelEntry],
    hits: list[ReferenceHit],
) -> LookupIssue | None:
    """canonical 碰撞检查（P1-5）：**只查本次命中的表/键**。

    - platform：命中条目所在的 (配置文件, 块, canonical key) 组内异值 → 冲突；
    - shared：命中条目所在的 (配置文件, canonical key) 组内规范化签名不同 → 冲突；
      签名比较对路径做 normcase/分隔符归一（物理同值不误报）。
    同值重复不在阻止范围（同值命中组由计划合并；未命中组由扫描以
    ``canonical_duplicate`` 非阻断提示，见 reference_lookup._scan_workspace）。
    """
    scoped_defaults: dict[tuple[str, int, str], set[str]] = {}
    scoped_shared: dict[tuple[str, str], list[SharedModuleRef]] = {}
    for hit in hits:
        if hit.kind == "platform_default":
            key = (hit.config_path, hit.block_index, hit.canonical_key)
            scoped_defaults.setdefault(key, set()).add(hit.raw_value)
        elif hit.kind.startswith("shared_"):
            shared_key = (hit.config_path, hit.canonical_key)
            scoped_shared.setdefault(shared_key, [])
    # 回收命中组所在配置内同 canonical 的**完整别名组**（含别名条目），
    # 供签名/取值比较（审查第二轮 P1-6：只收集 hit 自身会漏掉同组异值别名）
    entries_by_cfg: dict[str, _ModelEntry] = {}
    for entry in entries:
        entries_by_cfg[str(entry.root / MODEL_CONFIG_FILENAME)] = entry
        entries_by_cfg.setdefault(str(entry.root / PLATFORM_CONFIG_FILENAME), entry)
    for (path, block_index, canon), _values in list(scoped_defaults.items()):
        owner = entries_by_cfg.get(path)
        if owner is None or block_index >= len(owner.platforms):
            continue
        for raw_key, raw_value in owner.platforms[block_index].defaults.items():
            if canonical_module_dir(raw_key) == canon:
                scoped_defaults[(path, block_index, canon)].add(raw_value)
    for entry in entries:
        cfg = str(entry.root / MODEL_CONFIG_FILENAME)
        for (path, canon), refs in scoped_shared.items():
            if path != cfg:
                continue
            for raw_key, ref in entry.shared_pairs:
                if ref.module_key == canon:
                    refs.append(ref)
    for (path, block_index, canon), values in scoped_defaults.items():
        if len({str(v) for v in values}) > 1:
            return LookupIssue(
                category="canonical_conflict",
                config_path=path,
                owner_root=str(Path(path).parent),
                detail=f"defaults 模块「{canon}」（块 {block_index}）存在多个别名键且默认值不同",
            )
    for (path, canon), refs in scoped_shared.items():
        signatures = {
            (
                ref.mode,
                ref.source_model_id,
                ref.source_group,
                ref.source_module,
                os.path.normcase(
                    str(ref.source_relative_path).replace("/", "\\")
                ),
                ref.source_platform,
            )
            for ref in refs
        }
        if len(signatures) > 1:
            return LookupIssue(
                category="canonical_conflict",
                config_path=path,
                owner_root=str(Path(path).parent),
                detail=f"共享模块「{canon}」存在多个别名条目且内容不同",
            )
    return None


def build_rewrite_plan(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    request: RewriteRequest,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """按操作矩阵产出改写计划（纯函数，不改盘；规则 2/4）。"""
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    old_path = Path(request.old_path)
    try:
        old_path = assert_within_workspace(old_path, ws)
    except PathGuardError as exc:
        return _error_result("out_of_workspace", f"目标不在工作区内：{exc}")

    kind_detail = _validate_target_kind(old_path, request.target_kind)
    if kind_detail is not None:
        return _error_result("invalid_target", f"目标类型不匹配：{kind_detail}")

    if request.operation == "rename":
        # 单型号布局下工作区根本身改名：new_path 必然在根外，先给稳定阻止码
        if request.target_kind == "model" and same_path_identity(old_path, ws):
            return _error_result(
                "root_rename_unsupported",
                "单型号布局下工作区根改名属配置根迁移，须在设置中完成",
            )
        new_path = Path(str(request.new_path or "").strip())
        if not new_path.name:
            return _error_result("invalid_request", "rename 缺少 new_path")
        try:
            new_path = assert_within_workspace(new_path, ws)
        except PathGuardError as exc:
            return _error_result("out_of_workspace", f"新路径不在工作区内：{exc}")
        if same_path_identity(old_path, new_path):
            return _error_result(
                "invalid_operation", "新路径与旧路径为同一身份，不构成改名"
            )
        # 跨 canonical 模块的 module 改名属语义变化（P1-4）
        if (
            request.target_kind == "module"
            and canonical_module_dir(old_path.name)
            != canonical_module_dir(new_path.name)
        ):
            return _error_result(
                "unsupported_semantic_change",
                "改名改变了模块类型（canonical 键变化），须先按 CRUD 写语义 gate 定稿",
                payload={"old": canonical_module_dir(old_path.name),
                         "new": canonical_module_dir(new_path.name)},
            )
        replacement: Path | None = None
    elif request.operation == "update":
        replacement = Path(str(request.replacement_path or "").strip())
        if not replacement.name:
            return _error_result("invalid_request", "update 缺少 replacement_path")
        try:
            replacement = assert_within_workspace(replacement, ws)
        except PathGuardError as exc:
            return _error_result("out_of_workspace", f"新程序路径不在工作区内：{exc}")
        new_path = replacement
        if request.target_kind != "asset":
            return _error_result(
                "invalid_target", "update 仅支持程序（asset）级目标"
            )
        # replacement 必须已落盘且为合法 asset（P1-4）
        if not replacement.exists():
            return _error_result(
                "invalid_operation", f"新程序目录不存在：{replacement}"
            )
        kind_detail_repl = _validate_target_kind(replacement, "asset")
        if kind_detail_repl is not None:
            return _error_result(
                "invalid_operation", f"新程序目录结构不合法：{kind_detail_repl}"
            )
        if request.old_semantics is None or request.new_semantics is None:
            return _error_result(
                "invalid_request", "update 缺少新旧语义快照"
            )
        old_authoritative = _derive_semantics(old_path, ws)
        if old_authoritative is None:
            return _error_result(
                "invalid_request", "无法从盘上派生旧程序语义（型号 id 缺失或归属异常）"
            )
        if not _semantics_equal(
            old_authoritative,
            {k: str(v) for k, v in asdict(request.old_semantics).items()},
        ):
            return _error_result(
                "invalid_request",
                "请求中的旧程序语义快照与盘上真相不符",
                payload={"authoritative": old_authoritative},
            )
        new_authoritative = _derive_semantics(replacement, ws)
        if new_authoritative is None:
            return _error_result(
                "invalid_request", "无法从盘上派生新程序归属（型号 id 缺失或路径越界）"
            )
        # canonical module 以正式 CRUD 明确选择的类型为权威（new_semantics.module_key）
        new_checked = {
            "model_id": new_authoritative["model_id"],
            "module_key": str(request.new_semantics.module_key or "").strip(),
            "source_group": new_authoritative["source_group"],
            "scheme_name": new_authoritative["scheme_name"],
        }
        if not _semantics_equal(
            new_checked,
            {k: str(v) for k, v in asdict(request.new_semantics).items()},
        ):
            return _error_result(
                "invalid_request",
                "请求中的新程序语义快照与 replacement 路径归属不符",
                payload={"authoritative": new_checked},
            )
        if not _semantics_equal(old_authoritative, new_checked):
            return _error_result(
                "unsupported_semantic_change",
                "更新改变了程序类型或范围，须先按 CRUD 写语义 gate 定稿迁移规则",
                payload={
                    "old": old_authoritative,
                    "new": new_checked,
                },
            )
    else:
        return _error_result("invalid_request", f"未知操作类型：{request.operation}")

    hits, issues, entries = _collect_rewrite_hits(ws, request)
    blocking = [i for i in issues if is_blocking_issue(i)]
    if blocking:
        return _error_result(
            "lookup_blocked",
            "存在配置损坏或身份异常，反查清单不完整，已阻止改写",
            payload={"issues": [i.__dict__ for i in blocking]},
        )

    # update：命中的 static 锚点与 replacement 双向父子重叠 → 静默重绑 → 阻止
    # （词法 + resolved 双重判定，junction 也能拦截；审查第二轮 P1-5）
    if request.operation == "update" and replacement is not None:
        for hit in hits:
            if hit.kind != "shared_static" or not hit.current_target:
                continue
            anchor = Path(hit.current_target)
            if (
                same_path_identity(anchor, replacement)
                or is_within_boundary(replacement, anchor)
                or is_within_boundary(anchor, replacement)
            ):
                return _error_result(
                    "invalid_operation",
                    "replacement 与固定借用锚点重叠，会造成固定借用静默指向新程序",
                    payload={"anchor": hit.current_target},
                )

    conflict = _check_canonical_conflicts(entries, hits)
    if conflict is not None:
        return _error_result(
            "canonical_conflict",
            f"配置内模块别名键冲突，需先人工处理：{conflict.detail}",
            payload={"issue": conflict.__dict__},
        )

    # rename：全部 shared 命中必须能归位到新路径，禁止成功计划里静默保留旧锚点
    # （审查第四轮 P1-1）
    if request.operation == "rename":
        for hit in hits:
            if not hit.kind.startswith("shared_"):
                continue
            if (
                _rewritten_ref_rel(
                    hit.source_relative_path, ws, old_path, new_path
                )
                is None
            ):
                return _error_result(
                    "invalid_request",
                    "存在无法归位到新路径的借用引用，已阻止改写",
                    payload={
                        "raw_key": hit.raw_key,
                        "source_relative_path": hit.source_relative_path,
                    },
                )

    # P1-4：命中 platform_default 但新路径层级无法表达 defaults value → 阻止
    if (
        request.target_kind in ("asset",)
        and any(h.kind == "platform_default" for h in hits)
        and _module_variant_value(new_path, ws) is None
    ):
        return _error_result(
            "invalid_operation",
            "新路径层级超出 defaults 可表达范围（模块叶子或变体）",
            payload={"new_path": str(new_path)},
        )

    plan = _assemble_plan(ws, Path(configured_root or ""), request, old_path, new_path, hits, entries)  # type: ignore[arg-type]
    log_fn(f"级联改写计划：{len(plan.files)} 个文件，{len(plan.hits)} 条命中")
    return {
        "ok": True,
        "code": "ok",
        "message": f"改写计划就绪：{len(plan.files)} 个文件待改写",
        "payload": {"plan": plan},
    }


def _rewritten_ref_rel(
    ref_rel: str, ws: Path, old_path: Path, new_path: Path
) -> str | None:
    """把引用相对路径从 old_path 迁到 new_path 之下（审查第三/四轮 P1-1）。

    词法包含时**优先**按 workspace-relative 词法路径取尾段（junction 名保留）；
    词法不含时才按 resolved 包含关系取物理尾段（junction 指向 old 之内的场景）。
    两条路径统一 normcase/分隔符比较（``is_same_or_under`` / Windows Path
    语义）；无法归位返回 None（build 预检 → invalid_request，不得静默原样
    写回留下断链）。
    """
    ref_norm = Path(str(ref_rel).replace("\\", "/"))
    anchor_lexical = ws / ref_norm

    tail: Path | None = None
    # ① 词法：锚点词法位于 old_path 之下（normcase 比较，junction 名保留）
    if is_same_or_under(anchor_lexical, old_path):
        tail = Path(*anchor_lexical.relative_to(old_path).parts)
    else:
        # ② resolved：词法不在 old 之下，但物理指向 old 之内
        try:
            tail = Path(
                *anchor_lexical.resolve().relative_to(old_path.resolve()).parts
            )
        except (OSError, ValueError):
            return None
    new_abs = new_path / tail
    return _relative_to_ws(new_abs, ws)


def _assemble_plan(
    ws: Path,
    configured_root: Path,
    request: RewriteRequest,
    old_path: Path,
    new_path: Path,
    hits: list[ReferenceHit],
    entries: list[_ModelEntry],
) -> RewritePlan:
    """把命中落到逐文件改动（规则 2 操作矩阵 + 规则 4 字段矩阵）。"""
    is_rename = request.operation == "rename"
    # 型号自身配置随目录移动
    own_model_root_old = _source_model_root(old_path, ws)
    own_moved = is_rename and request.target_kind == "model"
    own_model_root_new = _source_model_root(new_path, ws) if own_moved else None

    # defaults value 派生：仅 asset 级操作改值；module 改名只改键，值保持不变
    defaults_value: str | None = None
    defaults_key_canon: str | None = None
    if request.target_kind == "asset":
        defaults_value = _module_variant_value(new_path, ws)
    elif request.target_kind == "module" and is_rename:
        defaults_key_canon = canonical_module_dir(new_path.name)

    entries_by_root = {str(e.root): e for e in entries}
    model_cfg_changes: dict[str, dict] = {}  # post_path → {entry, data, changes}
    platform_changes: dict[str, dict] = {}

    for hit in hits:
        owner_root = Path(hit.owner_root)
        entry = entries_by_root.get(hit.owner_root)
        if entry is None:
            continue
        if hit.kind in ("shared_static", "shared_follow_asset"):
            # update 只迁移 follow_asset；rename 两者都迁移（规则 2 矩阵）
            if not is_rename and hit.kind != "shared_follow_asset":
                continue
            if is_rename:
                new_ref_rel = _rewritten_ref_rel(
                    hit.source_relative_path, ws, old_path, new_path
                )
                # build 预检已保证可归位；此处 None 属不变式破坏，直接失败
                if new_ref_rel is None:
                    raise RuntimeError(
                        f"引用无法归位到新路径: {hit.source_relative_path}"
                    )
            else:
                new_ref_rel = _relative_to_ws(new_path, ws) or hit.source_relative_path
            bucket = model_cfg_changes.setdefault(
                str(owner_root / MODEL_CONFIG_FILENAME),
                {"entry": entry, "changes": [], "dedupe": {hit.canonical_key}},
            )
            bucket["dedupe"].add(hit.canonical_key)  # 同值 alias 合并（P1-5）
            bucket["changes"].append(
                (
                    hit.raw_key,
                    new_ref_rel,
                    f"shared[{hit.raw_key}]: {hit.source_relative_path} → {new_ref_rel}",
                )
            )
        elif hit.kind == "platform_default" and (
            request.target_kind in ("asset", "module")
        ):
            bucket = platform_changes.setdefault(
                str(owner_root / PLATFORM_CONFIG_FILENAME),
                {"entry": entry, "changes": [], "dedupe": set()},
            )
            # 去重身份限定 (block_index, canonical)：未命中块零改动（第三轮 P1-3）
            bucket["dedupe"].add((hit.block_index, hit.canonical_key))
            bucket["changes"].append(
                (
                    hit.platform_name,
                    hit.block_index,
                    hit.raw_key,
                    defaults_key_canon,
                    defaults_value if defaults_value is not None else hit.raw_value,
                    f"defaults[{hit.raw_key}]: {hit.raw_value!r} → "
                    f"{defaults_value if defaults_value is not None else hit.raw_value!r}",
                )
            )

    plan = RewritePlan(
        configured_root=str(configured_root),
        workspace_root=str(ws),
        request=request,
        hits=hits,
    )

    for post_cfg, bucket in model_cfg_changes.items():
        owner_entry: _ModelEntry = bucket["entry"]
        pre_root = owner_entry.root
        pre_path = pre_root / MODEL_CONFIG_FILENAME
        # 型号自身配置在 rename-model 时 pre/post 路径不同
        if own_moved and own_model_root_old is not None and same_path_identity(
            pre_root, own_model_root_old
        ):
            pre_path = own_model_root_old / MODEL_CONFIG_FILENAME
            post_path = (own_model_root_new or Path(post_cfg).parent) / MODEL_CONFIG_FILENAME
        else:
            post_path = Path(post_cfg)
        original_bytes = pre_path.read_bytes() if pre_path.exists() else None
        new_data = self_mutate_model_config(pre_path, bucket["changes"], bucket["dedupe"])
        new_content = serialize_model_config(new_data)
        plan.files.append(
            FileRewrite(
                kind="model_config",
                pre_path=pre_path,
                post_path=post_path,
                owner_root=str(pre_root),
                original_bytes=original_bytes,
                original_sha256=_sha256(original_bytes) if original_bytes is not None else "",
                new_content=new_content,
                new_sha256=_sha256(new_content.encode("utf-8")),
                changes=[c[2] for c in bucket["changes"]],
            )
        )

    for post_cfg, bucket in platform_changes.items():
        owner_entry = bucket["entry"]
        pre_path = owner_entry.root / PLATFORM_CONFIG_FILENAME
        post_path = Path(post_cfg)
        original_bytes = pre_path.read_bytes() if pre_path.exists() else None
        platforms = deepcopy(owner_entry.platforms)
        for _platform_name, block_index, raw_key, new_key_canon, new_value, _desc in bucket[
            "changes"
        ]:
            block = platforms[block_index]
            del block.defaults[raw_key]
            # 值不变的改写保留原始键；模块改名才写回归范名（规则 4 字段矩阵）
            target_key = new_key_canon if new_key_canon else raw_key
            block.defaults[target_key] = new_value
        # 同值别名组合并：只作用于命中的块，未命中块零改动（审查第三轮 P1-3）
        for block_index, canon in bucket.get("dedupe", set()):
            if block_index >= len(platforms):
                continue
            block = platforms[block_index]
            keys = [k for k in block.defaults if canonical_module_dir(k) == canon]
            if len(keys) < 2:
                continue
            value = block.defaults[keys[0]]
            for k in keys:
                del block.defaults[k]
            block.defaults[canon] = value
        new_content = serialize_platform_config(platforms)
        plan.files.append(
            FileRewrite(
                kind="platform_config",
                pre_path=pre_path,
                post_path=post_path,
                owner_root=str(owner_entry.root),
                original_bytes=original_bytes,
                original_sha256=_sha256(original_bytes) if original_bytes is not None else "",
                new_content=new_content,
                new_sha256=_sha256(new_content.encode("utf-8")),
                changes=[c[5] for c in bucket["changes"]],
            )
        )

    # 方案自身 方案配置.toml 的 name 字段随改名同步（统筹裁决：R8 内部扩展，
    # 见规格「对 reference_service 的改动」）——只在方案改名时生成，型号
    # 改名没有独立于目录名之外的「显示名」字段（own_moved 已覆盖型号侧）。
    if is_rename and request.target_kind == "scheme":
        pre_path = old_path / SCHEME_CONFIG_FILENAME
        post_path = new_path / SCHEME_CONFIG_FILENAME
        original_bytes = pre_path.read_bytes() if pre_path.exists() else None
        new_content = serialize_scheme_config(new_path.name)
        plan.files.append(
            FileRewrite(
                kind="scheme_config",
                pre_path=pre_path,
                post_path=post_path,
                owner_root=str(old_path),
                original_bytes=original_bytes,
                original_sha256=_sha256(original_bytes) if original_bytes is not None else "",
                new_content=new_content,
                new_sha256=_sha256(new_content.encode("utf-8")),
                changes=[f"name: {old_path.name} → {new_path.name}"],
            )
        )

    plan.token = _plan_token(plan)
    return plan


def self_mutate_model_config(
    pre_path: Path,
    changes: list[tuple[str, str, str]],
    dedupe: set[str],
) -> dict:
    """读取型号配置全量 dict 并应用 shared 条目改写（供计划序列化）。

    ``dedupe``：命中组 canonical 键集合——组内存在多个同值别名条目时合并为
    一条（优先 canonical 键），其余删除（P1-5）；组内异值已被阻止条件拦截。
    禁止当空 dict 覆盖：parse_error/parser_missing 直接抛（build 阶段已被
    阻止条件拦截，这里兜底）。
    """
    path = Path(pre_path)
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    shared = data.setdefault("shared_modules", {})
    if not isinstance(shared, dict):
        shared = {}
        data["shared_modules"] = shared
    for raw_key, new_rel, _desc in changes:
        entry = shared.get(raw_key)
        if not isinstance(entry, dict):
            continue
        entry["source_relative_path"] = new_rel
    for canon in dedupe:
        keys = [k for k in shared if canonical_module_dir(str(k)) == canon]
        if len(keys) < 2:
            continue
        # 统一写回唯一 canonical 键：优先已更新的条目内容
        source_key = next((c for c in changes if c[0] in keys), None)
        source = keys[0] if source_key is None else source_key[0]
        merged = dict(shared[source]) if isinstance(shared[source], dict) else {}
        for k in keys:
            del shared[k]
        shared[canon] = merged
    return data


def _validate_plan_for_apply(plan: RewritePlan) -> ServiceResult | None:
    """计划自校验（P0-1/P1-1）：token、路径归属、kind/文件名、内部一致性。"""
    if not plan.token or plan.token != _plan_token(plan):
        return _error_result(
            "invalid_plan", "计划校验失败：token 与内容不符或计划未经 build 生成"
        )
    ws = normalize_workspace_path(plan.workspace_root)
    if not ws:
        return _error_result("invalid_plan", "计划缺少工作区根")
    for item in plan.files:
        expected_name = _PLAN_ENTRY_FILENAMES.get(item.kind)
        if expected_name is None:
            return _error_result(
                "invalid_plan", f"未知计划条目类型：{item.kind}",
                payload={"path": str(item.pre_path)},
            )
        for label, p in (("pre", item.pre_path), ("post", item.post_path)):
            if p.name != expected_name:
                return _error_result(
                    "invalid_plan",
                    f"计划路径与类型不符：{label}={p}（应为 {expected_name}）",
                )
            try:
                assert_within_workspace(p, plan.workspace_root)
            except PathGuardError as exc:
                return _error_result(
                    "invalid_plan", f"计划路径越界（{label}）：{exc}",
                    payload={"path": str(p)},
                )
        # 内部一致性：preimage 状态与 SHA、新内容与 SHA
        if item.original_bytes is None:
            if item.original_sha256 != "":
                return _error_result(
                    "invalid_plan", "absent 条目不得携带 preimage hash",
                    payload={"path": str(item.pre_path)},
                )
        elif _sha256(item.original_bytes) != item.original_sha256:
            return _error_result(
                "invalid_plan", "preimage 字节与 hash 不一致",
                payload={"path": str(item.pre_path)},
            )
        if _sha256(item.new_content.encode("utf-8")) != item.new_sha256:
            return _error_result(
                "invalid_plan", "新内容与 hash 不一致",
                payload={"path": str(item.post_path)},
            )
    return None


def apply_rewrite_plan(
    plan: RewritePlan,
    configured_root: str | Path | None,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """应用改写计划：计划自校验 → 统一 gate → preimage 校验 → 原子替换 + CAS 回滚。"""
    plan_error = _validate_plan_for_apply(plan)
    if plan_error is not None:
        return plan_error
    gate = check_reference_gate(configured_root, plan.workspace_root)
    if gate is not None:
        return gate
    if normalize_workspace_path(plan.configured_root) != normalize_workspace_path(
        str(configured_root or "")
    ):
        return _error_result(
            "root_changed", "配置根在计划生成后发生变更，已阻止应用", payload={}
        )

    # 全量 preimage 校验（任一不一致 → stale_plan 零写入）。
    # 型号自身配置随目录移动（pre/post 双路径）：文件动作后 pre_path 已消失，
    # 改在 post_path 校验（移动不改字节，hash 应与 preimage 一致）。
    for item in plan.files:
        check_path = item.pre_path
        if (
            item.original_bytes is not None
            and not check_path.exists()
            and item.post_path != item.pre_path
            and item.post_path.exists()
        ):
            check_path = item.post_path
        if item.original_bytes is None:
            if item.pre_path.exists() or item.post_path.exists():
                return _error_result(
                    "stale_plan",
                    f"计划生成后出现了新文件，计划已过期：{item.pre_path}",
                    payload={"path": str(item.pre_path)},
                )
            continue
        if not check_path.exists():
            return _error_result(
                "stale_plan",
                f"计划依赖的文件已消失，计划已过期：{item.pre_path}",
                payload={"path": str(item.pre_path)},
            )
        current = check_path.read_bytes()
        if _sha256(current) != item.original_sha256:
            return _error_result(
                "stale_plan",
                f"文件在计划生成后被修改，计划已过期：{item.pre_path}",
                payload={"path": str(item.pre_path)},
            )

    applied: list[FileRewrite] = []
    failed: list[dict] = []
    for item in plan.files:
        try:
            atomic_write_text(item.post_path, item.new_content)
            applied.append(item)
        except Exception as exc:  # noqa: BLE001
            failed.append({"path": str(item.post_path), "error": str(exc)})
            log_fn(f"写入失败：{item.post_path} ({exc})")
            break

    if not failed:
        return {
            "ok": True,
            "code": "ok",
            "message": f"改写完成：{len(applied)} 个文件",
            "payload": {
                "applied": [str(f.post_path) for f in applied],
                "rolled_back": [],
                "rollback_conflict": [],
                "failed": [],
            },
        }

    # CAS 回滚：当前字节 == 本 plan 新字节才恢复 preimage，否则保留现状。
    # 恢复目标是**当前物理位置**（post_path）：型号改名时目录移动由上游 CRUD
    # 编排，R8 不回滚目录动作——把 preimage 写回 pre_path 或删除 post_path
    # 会造成 TOML 与目录树拆开（审查 P0-2）。
    rolled_back: list[str] = []
    conflicts: list[dict] = []
    for item in applied:
        preimage = item.original_bytes
        try:
            if not item.post_path.exists():
                conflicts.append(
                    {"path": str(item.post_path), "detail": "回滚时文件已消失"}
                )
                continue
            current = item.post_path.read_bytes()
            if _sha256(current) != item.new_sha256:
                conflicts.append(
                    {
                        "path": str(item.post_path),
                        "detail": "文件在失败后被其它入口修改，已保留现状",
                        "preimage": (preimage or b"").decode("utf-8", errors="replace"),
                        "preimage_path": str(item.post_path),
                    }
                )
                continue
            if preimage is None:
                item.post_path.unlink()
            else:
                atomic_write_text(item.post_path, preimage.decode("utf-8"))
            rolled_back.append(str(item.post_path))
        except Exception as exc:  # noqa: BLE001
            conflicts.append(
                {
                    "path": str(item.post_path),
                    "detail": f"回滚失败：{exc}",
                    "preimage": (preimage or b"").decode("utf-8", errors="replace"),
                    "preimage_path": str(item.post_path),
                }
            )

    ok = False  # 写入失败即整批失败；回滚成败只影响 code 与恢复信息
    code = "rolled_back" if not conflicts else "rollback_conflict"
    message = (
        f"写入失败，已恢复 {len(rolled_back)} 个文件原状（无残留）"
        if not conflicts
        else "写入失败且回滚存在冲突，请按 payload 中的 preimage 人工恢复"
    )
    log_fn(message)
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "payload": {
            "applied": [],
            "rolled_back": rolled_back,
            "rollback_conflict": conflicts,
            "failed": failed,
        },
    }


def migrate_follow_default_refs(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    log_fn: Callable[..., None] = print,
) -> ServiceResult:
    """存量 follow_default → follow_asset 幂等迁移（规则 3）。

    按借入方配置文件聚合、一个文件只做一次合并写；解析失败的条目保持原样
    并列入报告；阻断级 issue 的文件整文件 failed；写失败即停、可重入收敛。
    """
    ws = Path(workspace_root).resolve()
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate

    entries, issues, _id_roots = _scan_workspace(ws)
    blocking_roots: list[tuple[Path, list[LookupIssue]]] = []
    for issue in issues:
        if not is_blocking_issue(issue):
            continue
        try:
            resolved_owner = Path(issue.owner_root).resolve()
        except OSError:
            continue
        for path, items in blocking_roots:
            if same_path_identity(path, resolved_owner):
                items.append(issue)
                break
        else:
            blocking_roots.append((resolved_owner, [issue]))

    def _blocking_for(root: Path) -> list[LookupIssue] | None:
        """按身份（normcase/junction）关联来源根的阻断 issue（审查第二轮 P1-7）。"""
        for path, items in blocking_roots:
            if same_path_identity(path, root):
                return items
        return None

    entries_by_root = {str(e.root): e for e in entries}
    changed: list[str] = []
    unchanged: list[str] = []
    failed: list[dict] = []
    unresolved: list[dict] = []
    converted = 0
    kept = 0

    from fwasset.core.reference_lookup import (
        _ref_source_root,
        enumerate_model_roots,
    )

    for model_root in enumerate_model_roots(ws):
        root_str = str(model_root)
        own_blocking = _blocking_for(model_root)
        if own_blocking:
            failed.append(
                {
                    "path": str(model_root / MODEL_CONFIG_FILENAME),
                    "reason": "；".join(
                        f"{i.category}: {i.detail}" for i in own_blocking
                    ),
                }
            )
            continue
        entry = entries_by_root.get(root_str)
        if entry is None:
            # 无 issue 也无 entry：无 shared 可迁移
            unchanged.append(root_str)
            continue
        migrations: list[tuple[str, str]] = []
        source_blocked = False
        for raw_key, ref in entry.shared_pairs:
            if ref.mode != "follow_default":
                continue
            # 来源侧阻断 issue 传播到借入文件（P1-6）：来源根损坏/缺 id 时整文件零写
            source_root = _ref_source_root(ref, ws)
            source_issues = (
                _blocking_for(source_root) if source_root is not None else None
            )
            if source_issues:
                failed.append(
                    {
                        "path": str(model_root / MODEL_CONFIG_FILENAME),
                        "reason": "；".join(
                            f"{i.category}: {i.detail}" for i in source_issues
                        ),
                    }
                )
                source_blocked = True
                break
            resolution = resolve_shared_module(ref, ws)
            if (
                resolution.status == "hit"
                and resolution.resolved_path is not None
            ):
                new_rel = _relative_to_ws(resolution.resolved_path, ws)
                if new_rel:
                    migrations.append((raw_key, new_rel))
                    continue
            kept += 1
            unresolved.append(
                {
                    "borrower_config": str(model_root / MODEL_CONFIG_FILENAME),
                    "raw_key": raw_key,
                    "reason": resolution.reason or "unresolved",
                }
            )
        if source_blocked:
            # 只跳过当前借入方，后续无关文件继续预检/迁移（审查第二轮 P1-7）
            continue
        if not migrations:
            unchanged.append(root_str)
            continue

        def _mut(data: dict, _migrations: list[tuple[str, str]] = migrations) -> None:
            shared = data.get("shared_modules")
            if not isinstance(shared, dict):
                return
            for raw_key, new_rel in _migrations:
                item = shared.get(raw_key)
                if not isinstance(item, dict):
                    continue
                item["mode"] = "follow_asset"
                item["source_relative_path"] = new_rel
                item.pop("source_platform", None)

        try:
            from fwasset.core.model_config import _merge_write_model_config

            _merge_write_model_config(entry.root, _mut)
            changed.append(root_str)
            converted += len(migrations)
            log_fn(f"已迁移 {len(migrations)} 条借用（{root_str}）")
        except Exception as exc:  # noqa: BLE001
            failed.append(
                {"path": str(entry.root / MODEL_CONFIG_FILENAME), "reason": str(exc)}
            )
            log_fn(f"迁移写入失败，停止后续文件：{entry.root} ({exc})")
            break

    ok = not failed
    message = (
        f"迁移完成：{converted} 条转为跟随指定程序，{kept} 条保持原样"
        if ok
        else "迁移未完成，可重新运行继续（幂等）"
    )
    return {
        "ok": ok,
        "code": "ok" if ok else "migrate_failed",
        "message": message,
        "payload": {
            "changed": changed,
            "unchanged": unchanged,
            "failed": failed,
            "unresolved": unresolved,
            "converted": converted,
            "kept": kept,
        },
    }
