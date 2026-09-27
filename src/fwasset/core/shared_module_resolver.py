"""共享引用解析器：一套路径覆盖命中 / 缺失（Phase B1/C1）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fwasset.core.model_config import SharedModuleRef, load_model_config
from fwasset.core.path_guard import PathGuardError, assert_within_workspace
from fwasset.core.platform_config import (
    PlatformDefaults,
    canonical_module_dir,
    load_platform_config_with_status,
)
from fwasset.core.scheme_config import _is_excluded_dir


@dataclass
class SharedModuleResolution:
    ref: SharedModuleRef
    status: Literal["hit", "missing"]
    reason: str = ""
    resolved_path: Path | None = None
    variants: list[Path] = field(default_factory=list)


def _is_under_workspace(path: Path, workspace_root: Path) -> bool:
    """复用统一守卫：目标在根之下（允许相等）。"""
    try:
        assert_within_workspace(path, workspace_root)
        return True
    except PathGuardError:
        return False


def _list_variant_dirs(abs_path: Path) -> list[Path]:
    """过滤后的直接子目录；空则视为叶子 → [abs_path]。"""
    if not abs_path.is_dir():
        return [abs_path]
    children: list[Path] = []
    try:
        for child in abs_path.iterdir():
            if not child.is_dir():
                continue
            if _is_excluded_dir(child.name):
                continue
            children.append(child)
    except OSError:
        return [abs_path]
    children.sort(key=lambda p: p.name)
    return children if children else [abs_path]


def resolve_shared_module(
    ref: SharedModuleRef,
    workspace_root: Path,
    root_for_model_id: Callable[[str], Path | None] | None = None,
) -> SharedModuleResolution:
    """解析一条共享引用 → hit 或 missing（同一路径，无第二套逻辑）。

    ``root_for_model_id`` 为兼容保留（B0 bind 映射注入点）；B1 解析以
    ``source_dir`` 盘上 ``model_id`` 为权威，不依赖该映射（审查 #3）。
    """
    ws = Path(workspace_root).resolve()
    rel = str(ref.source_relative_path or "").strip().replace("\\", "/")
    want_id = str(ref.source_model_id or "").strip()
    if not rel:
        return SharedModuleResolution(
            ref=ref, status="missing", reason="path_not_found"
        )

    # 拒绝相对段中的 . / ..，防止「首段型号 id 校验」与 resolve 后真实路径脱节
    # 例：L36程序/../L50程序/... 会解析到 L50，但若只校验 L36 会误 hit
    parts = Path(rel).parts
    if not parts or any(p in (".", "..") for p in parts):
        return SharedModuleResolution(
            ref=ref, status="missing", reason="out_of_workspace"
        )

    abs_path = (ws / rel).resolve()
    if not _is_under_workspace(abs_path, ws):
        return SharedModuleResolution(
            ref=ref, status="missing", reason="out_of_workspace"
        )

    first = parts[0]
    # R8：支持单型号布局——首段为 通用/定制 时型号根即工作区根（与
    # reference_lookup/_source_model_root 同一归属规则），否则首段为型号目录。
    if first in ("通用", "定制"):
        source_dir = ws
    else:
        source_dir = (ws / first).resolve()

    if not source_dir.is_dir():
        # id 映射若存在但路径段不在工作区 → 仍按源未导入（路径指向的型号根不存在）
        return SharedModuleResolution(
            ref=ref, status="missing", reason="source_not_imported"
        )

    mid, status, _ = load_model_config(source_dir)
    if status != "ok" or not mid:
        # 目录在但无合法 model_id：无法完成 id 校验 → 视为源未就绪
        return SharedModuleResolution(
            ref=ref, status="missing", reason="source_not_imported"
        )
    if mid != want_id:
        return SharedModuleResolution(ref=ref, status="missing", reason="id_mismatch")

    # 权威来源 = source_dir 磁盘上的 model_id（上面已校验）。
    # 不再与 bind 内存映射交叉比对：该映射可能滞后于盘上文件，或因大小写/
    # 规范化差异把同一根解析成不同 Path，导致误判 id_mismatch（审查 #3）。
    # .. 越界由前置段校验与下方 source_dir 归属双保险覆盖。

    # 双保险：resolve 后的目标必须仍落在已校验的 source_dir 下
    # （即使将来放宽对 .. 的拒绝，也不能跨到其它型号根）
    if not _is_under_workspace(abs_path, source_dir):
        return SharedModuleResolution(
            ref=ref, status="missing", reason="out_of_workspace"
        )

    # --- 按 mode 分支 ---
    if ref.mode == "follow_default":
        return _resolve_follow_default(ref, source_dir, abs_path)

    if ref.mode == "follow_asset":
        # R8：跟随来源具体程序——锚定路径存在即命中；resolved_path 即锚点，
        # variants 不展开（固定指向该程序，不做子目录展开）。
        if not abs_path.exists():
            return SharedModuleResolution(
                ref=ref, status="missing", reason="path_not_found"
            )
        return SharedModuleResolution(
            ref=ref,
            status="hit",
            reason="",
            resolved_path=abs_path,
            variants=[abs_path],
        )

    # static（默认）：固定版本，Phase B 行为
    if not abs_path.exists():
        return SharedModuleResolution(
            ref=ref, status="missing", reason="path_not_found"
        )
    variants = _list_variant_dirs(abs_path)
    return SharedModuleResolution(
        ref=ref,
        status="hit",
        reason="",
        resolved_path=abs_path,
        variants=variants,
    )


# ---------------------------------------------------------------------------
# Phase C1 辅助：follow_default 解析
# ---------------------------------------------------------------------------


def _find_default_key(block: PlatformDefaults, module: str) -> str:
    """规范化匹配：在 platform 块的 defaults 中找模块键。

    容忍 catalog label（「快捷键程序」）与目录名（「快捷键」）不一致。
    返回匹配到的原始 key，未找到返回空串。
    """
    want = canonical_module_dir(module)
    if want in block.defaults:
        return want
    want_short = want[:-2] if want.endswith("程序") else want
    for key in block.defaults:
        k = canonical_module_dir(key)
        k_short = k[:-2] if k.endswith("程序") else k
        if want == k or want_short == k_short:
            return key
    return ""


def _resolve_follow_default(
    ref: SharedModuleRef,
    source_dir: Path,
    module_dir: Path,
) -> SharedModuleResolution:
    """follow_default 模式：读源 平台配置.toml 动态查找默认变体。

    ``module_dir`` 是共同前置校验后的 abs_path；
    C2 service 注册时已保证它指向模块目录而非变体目录。

    默认只认显式写入的变体目录名。键缺失或值为空串都报 ``no_source_default``，
    不回落到模块目录下的唯一变体。
    """
    if not module_dir.is_dir():
        return SharedModuleResolution(
            ref=ref, status="missing", reason="path_not_found"
        )

    platforms, plt_status, _ = load_platform_config_with_status(source_dir)
    if plt_status != "ok" or not platforms:
        return SharedModuleResolution(
            ref=ref, status="missing", reason="no_source_platform"
        )

    if ref.source_platform:
        # 显式 platform：必须找到对应块
        block = next(
            (p for p in platforms if p.platform_name == ref.source_platform), None
        )
        if block is None:
            return SharedModuleResolution(
                ref=ref, status="missing", reason="no_source_platform"
            )
        matched_key = _find_default_key(block, ref.source_module)
        if not matched_key:
            return SharedModuleResolution(
                ref=ref, status="missing", reason="no_source_default"
            )
        variant_name = str(block.defaults[matched_key] or "").strip()
    else:
        # 自动检测：依次试所有 platform，取第一个含该模块默认的
        variant_name = ""
        for p in platforms:
            matched_key = _find_default_key(p, ref.source_module)
            if matched_key:
                variant_name = str(p.defaults[matched_key] or "").strip()
                break

    if not variant_name:
        # 源型号该模块未设默认（无键或空串），不猜唯一变体
        return SharedModuleResolution(
            ref=ref, status="missing", reason="no_source_default"
        )

    resolved_path = module_dir / variant_name

    if not resolved_path.exists():
        return SharedModuleResolution(
            ref=ref, status="missing", reason="path_not_found"
        )

    return SharedModuleResolution(
        ref=ref,
        status="hit",
        reason="",
        resolved_path=resolved_path,
        variants=[resolved_path],  # follow_default 已确定具体变体，不再展开
    )
