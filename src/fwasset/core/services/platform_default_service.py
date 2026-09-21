from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import get_args

from fwasset.core.path_guard import PathGuardError, assert_within_workspace
from fwasset.core.platform_config import (
    PlatformDefaults,
    canonical_module_dir,
    load_platform_config_with_status,
    save_platform_config,
)
from fwasset.core.scheme_config import discover_schemes
from fwasset.core.types import ChassisType

# 再导出，便于 view model / 测试从 service 侧一并导入
__all__ = [
    "canonical_module_dir",
    "bootstrap_platform_blocks",
    "ensure_platform_blocks",
    "set_default_variant",
    "set_module_default_for_model",
]

_INTERNAL_DEFAULT_BLOCK = "默认"

_CHASSIS_TYPES: set[str] = set(get_args(ChassisType))


def _scheme_platform_names(model_root: Path) -> list[str]:
    """本型号根内方案配置中的 platform 名（去重、去空、保序）。"""
    names: list[str] = []
    seen: set[str] = set()
    for scheme in discover_schemes(model_root):
        name = str(scheme.platform or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def bootstrap_platform_blocks(model_root: Path | str) -> list[PlatformDefaults]:
    """无配置时初始化 [[platform]] 块（TASK A2）。

    1. 收集本型号根方案中的 platform 名称；
    2. 有名称 → 每个名称一块；
    3. 无方案/无 platform → 单一内部块 ``默认``（不对用户作主语展示）。
    """
    root = Path(model_root)
    names = _scheme_platform_names(root)
    if names:
        return [PlatformDefaults(platform_name=n, defaults={}) for n in names]
    return [PlatformDefaults(platform_name=_INTERNAL_DEFAULT_BLOCK, defaults={})]


def _is_normalized_blocks(platforms: list[PlatformDefaults]) -> bool:
    """D5.5③：恰好一个块且块名属机芯类型枚举 = 已归一，不得再制造新块。"""
    return len(platforms) == 1 and platforms[0].platform_name in _CHASSIS_TYPES


def ensure_platform_blocks(
    model_root: Path | str, platforms: list[PlatformDefaults]
) -> list[PlatformDefaults]:
    """保证写盘前有配置块：空则 bootstrap；已有则补全方案声明但尚未建块的 platform。

    不删除历史块。已归一型号（单块 + 枚举名）**原样返回**，不再从方案
    ``platform`` 补块，否则归一成果会被这条旧路径重新打散（D5.5③）。
    """
    root = Path(model_root)
    if not platforms:
        return bootstrap_platform_blocks(root)
    if _is_normalized_blocks(platforms):
        return platforms
    existing = {p.platform_name for p in platforms}
    for name in _scheme_platform_names(root):
        if name not in existing:
            platforms.append(PlatformDefaults(platform_name=name, defaults={}))
            existing.add(name)
    return platforms


def _alias_keys(defaults: dict[str, str], module: str) -> list[str]:
    """在 defaults 中找与 module 同义的全部键（含版/板笔误）。"""
    canon = canonical_module_dir(module)
    if not canon:
        return []
    return [key for key in defaults if canonical_module_dir(key) == canon]


def _apply_module_default(target: PlatformDefaults, module: str, variant: str) -> str:
    """写入一块 [[platform]] 的模块默认；返回该块上一次的变体名。

    - 若块内已有同义键（版/板），全部删除后只保留规范键「板」，避免重复。
    - 若无键，用规范模块名新增。
    - 多键并存时 previous 优先取规范键上的值，否则取第一个同义键。
    """
    canon = canonical_module_dir(module)
    aliases = _alias_keys(target.defaults, module)
    previous = ""
    if aliases:
        if canon in target.defaults:
            previous = target.defaults.get(canon, "")
        else:
            previous = target.defaults.get(aliases[0], "")
        for key in aliases:
            del target.defaults[key]
    target.defaults[canon] = str(variant or "")
    return previous


def _load_platforms_for_write(
    root_path: Path, log_fn: Callable[..., None]
) -> tuple[list[PlatformDefaults] | None, dict | None]:
    """严格读取平台配置供写服务使用。

    成功返回 ``(platforms, None)``（``missing`` 时 platforms 为空列表）。
    失败返回 ``(None, ServiceResult)``，且**尚未**做任何 mutate/bootstrap。
    """
    platforms, status, error = load_platform_config_with_status(root_path)
    if status in ("ok", "missing"):
        return platforms, None
    if status == "parser_missing":
        message = "平台配置读取组件不可用，已停止写入"
        log_fn(message)
        return None, {
            "ok": False,
            "code": "parser_missing",
            "message": message,
            "payload": {"detail": error},
        }
    # parse_error
    message = "平台配置读取失败，已停止写入并保留原文件"
    log_fn(message)
    return None, {
        "ok": False,
        "code": "config_parse_error",
        "message": message,
        "payload": {"detail": error},
    }


def set_default_variant(
    model_root: str,
    platform_name: str,
    module_dir: str,
    variant_name: str,
    workspace_root: str | Path,
    log_fn: Callable[..., None] = print,
) -> dict:
    """把某 [[platform]] 条目下某模块的默认变体写入 `平台配置.toml`。

    低层 API（按配置块名写入）。产品「型号+模块默认」请用
    :func:`set_module_default_for_model`。
    """
    root = str(model_root or "").strip()
    platform = str(platform_name or "").strip()
    module = canonical_module_dir(module_dir)
    if not root or not platform or not module:
        return {
            "ok": False,
            "code": "invalid_args",
            "message": "设置默认失败：型号根目录、配置块名与模块名均不能为空",
            "payload": {},
        }

    # 目标写入路径守卫（R5）
    try:
        root_path = assert_within_workspace(root, workspace_root)
    except PathGuardError as exc:
        message = f"设置默认失败：{exc}"
        log_fn(message)
        return {
            "ok": False,
            "code": "out_of_workspace",
            "message": message,
            "payload": {},
        }
    if not root_path.is_dir():
        return {
            "ok": False,
            "code": "invalid_args",
            "message": f"设置默认失败：型号根目录不存在 ({root})",
            "payload": {},
        }

    platforms, err = _load_platforms_for_write(root_path, log_fn)
    if err is not None:
        return err

    assert platforms is not None
    if _is_normalized_blocks(platforms) and platforms[0].platform_name != platform:
        message = "该型号平台配置已归一，不能新建配置块"
        log_fn(message)
        return {
            "ok": False,
            "code": "platform_normalized_locked",
            "message": message,
            "payload": {
                "current": platforms[0].platform_name,
                "requested": platform,
            },
        }

    try:
        target = next((p for p in platforms if p.platform_name == platform), None)
        if target is None:
            target = PlatformDefaults(platform_name=platform)
            platforms.append(target)
        previous = _apply_module_default(target, module, str(variant_name or ""))
        toml_path = save_platform_config(root_path, platforms)
    except Exception as exc:  # noqa: BLE001
        message = f"写入平台配置失败: {exc}"
        log_fn(message)
        return {"ok": False, "code": "write_failed", "message": message, "payload": {}}

    shown = variant_name or module
    message = f"已将「{shown}」设为模块「{module}」默认版本"
    log_fn(message)
    return {
        "ok": True,
        "code": "ok",
        "message": message,
        "payload": {
            "config_path": str(toml_path),
            "platform": platform,
            "module_dir": module,
            "variant_name": str(variant_name or ""),
            "previous_variant": previous,
        },
    }


def set_module_default_for_model(
    model_root: str,
    module_dir: str,
    variant_name: str,
    workspace_root: str | Path,
    log_fn: Callable[..., None] = print,
    *,
    model_name: str = "",
) -> dict:
    """按「型号 + 模块」设默认：同步该型号根下所有 [[platform]]。

    每块用自身已有同义键更新并归一为规范名「机芯板」；无键则新增规范名。
    业务主语是型号与模块（如 L36 蓝牙），toml 的 name（标准单机芯3D）仅作回源分组。
    """
    root = str(model_root or "").strip()
    module = canonical_module_dir(module_dir)
    if not root or not module:
        return {
            "ok": False,
            "code": "invalid_args",
            "message": "设置默认失败：型号根目录与模块名均不能为空",
            "payload": {},
        }

    # 目标写入路径守卫（R5）
    try:
        root_path = assert_within_workspace(root, workspace_root)
    except PathGuardError as exc:
        message = f"设置默认失败：{exc}"
        log_fn(message)
        return {
            "ok": False,
            "code": "out_of_workspace",
            "message": message,
            "payload": {},
        }
    if not root_path.is_dir():
        return {
            "ok": False,
            "code": "invalid_args",
            "message": f"设置默认失败：型号根目录不存在 ({root})",
            "payload": {},
        }

    platforms, err = _load_platforms_for_write(root_path, log_fn)
    if err is not None:
        return err

    variant = str(variant_name or "")
    try:
        assert platforms is not None
        platforms = ensure_platform_blocks(root_path, platforms)

        previous = ""
        updated_blocks: list[str] = []
        for target in platforms:
            previous = _apply_module_default(target, module, variant) or previous
            updated_blocks.append(target.platform_name)

        toml_path = save_platform_config(root_path, platforms)
    except Exception as exc:  # noqa: BLE001
        message = f"写入平台配置失败: {exc}"
        log_fn(message)
        return {"ok": False, "code": "write_failed", "message": message, "payload": {}}

    shown = variant or module
    model_shown = (model_name or "").strip()
    if model_shown:
        message = f"已将「{shown}」设为「{model_shown}」{module}默认版本"
    else:
        message = f"已将「{shown}」设为{module}默认版本"
    log_fn(message)
    return {
        "ok": True,
        "code": "ok",
        "message": message,
        "payload": {
            "config_path": str(toml_path),
            "module_dir": module,
            "variant_name": variant,
            "previous_variant": previous,
            "updated_platforms": updated_blocks,
            "model_name": model_shown,
        },
    }
