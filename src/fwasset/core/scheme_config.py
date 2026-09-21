from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fwasset.core.config_io import atomic_write_text

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

_CUSTOM_DIR_NAME = "定制"
_SCHEME_CONFIG_FILENAME = "方案配置.toml"
SCHEME_CONFIG_FILENAME = _SCHEME_CONFIG_FILENAME

_SCHEME_CONFIG_HEADER = "# 本文件由 fwasset 管理（方案名）。用户无需手写。\n"


def _toml_str(value: str) -> str:
    """Serialize a string as a TOML basic string (quotes/backslash escaped)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def serialize_scheme_config(name: str) -> str:
    """方案配置规范格式序列化：固定文件头 + ``name`` 字段（R8 级联改写计划用）。

    风格对齐 ``model_config._serialize_model_config``：固定文件头注释 +
    单行内容。不写 ``platform`` 字段（D2.2/D5.3）。
    """
    return f"{_SCHEME_CONFIG_HEADER}\nname = {_toml_str(name)}\n"


def save_scheme_config(scheme_root: Path, name: str) -> Path:
    """把方案配置整体重写为 ``name = "..."``（应用托管该文件）。

    经同目录临时文件 + ``os.replace`` 原子落盘；供 D2.2 新建方案与
    D2.3 方案改名的 R8 计划落地复用。返回写入的文件路径。
    """
    toml_path = Path(scheme_root) / _SCHEME_CONFIG_FILENAME
    atomic_write_text(toml_path, serialize_scheme_config(name))
    return toml_path


@dataclass
class SchemeConfig:
    """单个定制方案的元数据。"""

    name: str  # 方案名，如 "以色列-Royal-Z9"
    platform: str  # 所属平台，如 "标准单机芯3D"
    path: Path  # 方案目录的绝对路径


def discover_schemes(model_root: Path) -> list[SchemeConfig]:
    """
    扫描根目录及其子目录下 `定制/` 子目录中的所有定制方案。

    支持两种目录层级：
    - model_root/定制/...   （型号根目录就是结构化根目录）
    - model_root/型号名/定制/...  （型号根目录包含多个型号子目录）

    每个含有 `方案配置.toml` 的子目录视为一个方案。
    子目录不含 TOML 时，也识别为方案但元数据留默认值（向后兼容）。
    `定制/单模块变体/` 目录跳过（不是整机方案）。
    """
    results: list[SchemeConfig] = []
    seen_paths: set[str] = set()

    # 收集所有包含 定制 子目录的路径
    custom_dirs: list[Path] = []

    # 直接子目录：model_root/定制
    direct = model_root / _CUSTOM_DIR_NAME
    if direct.exists() and direct.is_dir():
        custom_dirs.append(direct)

    # 间接子目录：model_root/*/定制 （型号目录定性结构）
    try:
        for child in model_root.iterdir():
            if child.is_dir() and not _is_excluded_dir(child.name):
                candidate = child / _CUSTOM_DIR_NAME
                if candidate.exists() and candidate.is_dir():
                    custom_dirs.append(candidate)
    except OSError:
        pass

    for custom_root in custom_dirs:
        for scheme_dir in sorted(custom_root.iterdir()):
            if not scheme_dir.is_dir():
                continue
            # 跳过单模块变体汇总目录
            if scheme_dir.name in ("单模块变体",):
                continue

            # 去重：避免扫描根目录包含多个型号时重复
            scheme_key = str(scheme_dir.resolve())
            if scheme_key in seen_paths:
                continue
            seen_paths.add(scheme_key)

            toml_path = scheme_dir / _SCHEME_CONFIG_FILENAME
            name = scheme_dir.name
            platform = ""

            if toml_path.exists() and tomllib is not None:
                try:
                    with open(toml_path, "rb") as f:
                        data = tomllib.load(f)
                    name = (
                        str(data.get("name", scheme_dir.name)).strip()
                        or scheme_dir.name
                    )
                    platform = str(data.get("platform", "")).strip()
                except Exception:
                    pass

            results.append(SchemeConfig(name=name, platform=platform, path=scheme_dir))
    return results


def _is_excluded_dir(name: str) -> bool:
    """判断目录名是否应被跳过（临时、备份等）。

    这里的关键词表与 ``settings.SCAN_EXCLUDE_DIR_KEYWORDS`` 各自独立。泛化的
    ``"旧"`` 已按 D4.3③ 一并退役：它在**型号级子目录**那一层生效，留着会让
    ``旧款L36`` 的全部定制方案整片消失。其余四项不会命中中文业务目录名。
    """
    lower = name.lower()
    return any(kw in lower for kw in ("backup", "-back", "temp", "tmp"))


def scheme_for_path(
    schemes: list[SchemeConfig], asset_path: Path
) -> SchemeConfig | None:
    """根据资产路径找到所属定制方案（若有）。

    前缀匹配；多候选时取**路径最深**（最长前缀），避免嵌套/重叠方案绑错。
    """
    resolved = asset_path.resolve()
    matches: list[SchemeConfig] = []
    for scheme in schemes:
        try:
            resolved.relative_to(scheme.path.resolve())
            matches.append(scheme)
        except ValueError:
            continue
    if not matches:
        return None
    # 最深路径优先（嵌套方案时绑到内层）
    return max(matches, key=lambda s: len(str(s.path.resolve())))
