from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fwasset.core.config_io import atomic_write_text

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


PlatformConfigStatus = Literal[
    "ok",
    "missing",
    "parse_error",
    "parser_missing",
]

PLATFORM_CONFIG_FILENAME = "平台配置.toml"
PLATFORM_CONFIG_HEADER = "# 本文件由 fwasset 管理。用户无需手写。"

_MODULE_KEY_ALIASES = {
    "快捷键": "快捷键程序",
    "快捷按键": "快捷键程序",
    "快捷键-旋钮": "快捷键程序",
}


def canonical_module_dir(name: str) -> str:
    """模块目录/配置键规范名。

    磁盘与历史 TOML 中的目录短名、别名与「机芯版」笔误均归一到 catalog 标签。
    """
    normalized = str(name or "").strip().replace("机芯版", "机芯板")
    return _MODULE_KEY_ALIASES.get(normalized, normalized)


@dataclass
class PlatformDefaults:
    """单个平台的默认模块变体声明。"""

    platform_name: str
    defaults: dict[str, str] = field(
        default_factory=dict
    )  # module_dir_name -> variant_name


def load_platform_config_with_status(
    model_root: Path,
) -> tuple[list[PlatformDefaults], PlatformConfigStatus, str]:
    """严格读取型号根下的 ``平台配置.toml``。

    返回 ``(platforms, status, error)``：
    - ``ok``：可解析且结构合法（允许 0 个 platform 块）
    - ``missing``：文件不存在
    - ``parse_error``：语法或结构不符合契约
    - ``parser_missing``：tomllib/tomli 均不可用
    """
    toml_path = Path(model_root) / "平台配置.toml"
    if not toml_path.exists():
        return [], "missing", ""
    if tomllib is None:
        return [], "parser_missing", "TOML 解析组件不可用（需要 tomllib 或 tomli）"

    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001
        return [], "parse_error", f"平台配置 TOML 解析失败: {exc}"

    if not isinstance(data, dict):
        return [], "parse_error", "平台配置顶层必须是 table"

    if "platform" not in data:
        return [], "ok", ""

    platform_raw = data.get("platform")
    if not isinstance(platform_raw, list):
        return [], "parse_error", "platform 必须是 array of tables"

    results: list[PlatformDefaults] = []
    for entry in platform_raw:
        if not isinstance(entry, dict):
            return [], "parse_error", "每个 [[platform]] 条目必须是 table"
        defaults_raw = entry.get("defaults", {})
        if defaults_raw is None:
            defaults_raw = {}
        if not isinstance(defaults_raw, dict):
            return [], "parse_error", "platform.defaults 必须是 table"
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        defaults = {str(k): str(v) for k, v in defaults_raw.items()}
        results.append(PlatformDefaults(platform_name=name, defaults=defaults))
    return results, "ok", ""


def load_platform_config_strict(
    model_root: Path,
) -> tuple[list[PlatformDefaults], PlatformConfigStatus, str]:
    """R8 反查/级联专用严格读取：无 name 的 platform 块按 parse_error 处理。

    与 :func:`load_platform_config_with_status` 的区别：后者容忍无名块
    （静默跳过），严格版把结构异常显式暴露给删除预检与级联，防止少查。
    """
    platforms, status, error = load_platform_config_with_status(model_root)
    if status != "ok":
        return platforms, status, error
    toml_path = Path(model_root) / PLATFORM_CONFIG_FILENAME
    if not toml_path.exists():
        return platforms, status, error
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001
        return [], "parse_error", f"平台配置 TOML 解析失败: {exc}"
    platform_raw = data.get("platform")
    if platform_raw is None:
        return platforms, status, error
    for entry in platform_raw:
        if isinstance(entry, dict) and not str(entry.get("name", "") or "").strip():
            return [], "parse_error", "platform 块缺少 name"
    return platforms, status, error


def load_platform_config(model_root: Path) -> list[PlatformDefaults]:
    """
    读取型号根目录下的 `平台配置.toml`。
    文件不存在或无法解析时静默返回空列表，保证只读路径向后兼容。

    会写盘的服务必须改用 :func:`load_platform_config_with_status`，
    禁止再用 ``[]`` 推断「文件不存在」。
    """
    platforms, status, _error = load_platform_config_with_status(model_root)
    return platforms if status == "ok" else []


def default_variant_for(
    platforms: list[PlatformDefaults],
    platform_name: str,
    module_dir: str,
) -> str:
    """
    根据平台名和模块目录名，查找该模块的默认变体子目录名。
    找不到时返回空字符串。

    键查找容忍历史笔误「机芯版」↔ 规范「机芯板」（catalog / 磁盘目录）。
    """
    want = canonical_module_dir(module_dir)
    if not want:
        return ""
    for p in platforms:
        if p.platform_name != platform_name:
            continue
        if module_dir in p.defaults:
            return p.defaults[module_dir]
        if want in p.defaults:
            return p.defaults[want]
        for key, value in p.defaults.items():
            if canonical_module_dir(key) == want:
                return value
        return ""
    return ""


def _toml_str(value: str) -> str:
    """Serialize a string as a TOML basic string (quotes/backslash escaped)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def serialize_platform_config(platforms: list[PlatformDefaults]) -> str:
    """平台配置规范格式序列化（R8 级联改写计划用）：只输出头注释 + 平台块。"""
    lines: list[str] = [PLATFORM_CONFIG_HEADER]
    for p in platforms:
        lines.append("")
        lines.append("[[platform]]")
        lines.append(f"name = {_toml_str(p.platform_name)}")
        lines.append("[platform.defaults]")
        for module_dir, variant_name in p.defaults.items():
            lines.append(f"{_toml_str(module_dir)} = {_toml_str(variant_name)}")
    return "\n".join(lines) + "\n"


def save_platform_config(model_root: Path, platforms: list[PlatformDefaults]) -> Path:
    """把平台默认配置写回型号根目录下的 `平台配置.toml`。

    以规范格式整体重写（应用托管该文件）；经同目录临时文件 + os.replace 原子落盘。
    写入失败向上抛异常，由服务层包装为 ServiceResult。返回写入的文件路径。
    """
    toml_path = Path(model_root) / "平台配置.toml"
    atomic_write_text(toml_path, serialize_platform_config(platforms))
    return toml_path
