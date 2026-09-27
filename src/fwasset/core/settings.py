from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import tomli_w

from fwasset.core.config_io import atomic_write_text
from fwasset.core.types import ServiceResult


def _find_project_root(start: Path) -> Path:
    probe = Path(start).resolve()
    for parent in [probe, *probe.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return probe


def _find_app_root(start: Path | None = None) -> Path:
    """返回应用程序根目录（配置文件和 exe 所在目录）。

    开发模式：pyproject.toml 所在目录。
    打包模式（PyInstaller）：exe 所在目录。
    """
    if start is not None:
        return _find_project_root(start)
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后运行
        return Path(sys.executable).resolve().parent
    # 开发模式：向上查找 pyproject.toml
    return _find_project_root(Path(__file__).resolve())


APP_ROOT = _find_app_root()
"""应用程序根目录。配置文件和可执行文件同目录。"""


def _config_path() -> Path:
    """配置文件路径。

    - 打包模式（frozen exe）：%%APPDATA%%/fwasset/config.toml
    - 开发模式：项目根目录 / config.toml（维持原行为）
    """
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        config_dir = Path(appdata) / "fwasset"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "config.toml"
    return APP_ROOT / "config.toml"


CONFIG_PATH = _config_path()


def _default_runtime_dir(app_root: Path | None = None) -> Path:
    root = Path(app_root or APP_ROOT)
    if getattr(sys, "frozen", False):
        return root / "runtime"
    return root / ".runtime"


def _resolve_runtime_dir(
    app_root: Path | None = None, env_value: str | None = None
) -> Path:
    root = Path(app_root or APP_ROOT)
    raw = os.environ.get("FWASSET_RUNTIME_DIR") if env_value is None else env_value
    if raw and str(raw).strip():
        path = Path(str(raw).strip())
        if path.is_absolute():
            return path
        return (root / path).resolve()
    return _default_runtime_dir(root).resolve()


def ensure_runtime_dir(path: str | Path | None = None) -> Path:
    target = Path(path) if path is not None else _resolve_runtime_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"运行时目录创建失败: {target} ({exc})") from exc
    return target


RUNTIME_DIR = ensure_runtime_dir()
ASSET_INDEX_PATH = RUNTIME_DIR / "fwasset.db"
LOG_DIR = RUNTIME_DIR / "logs"
APP_LOG_PATH = LOG_DIR / "app.log"

_DEFAULTS = {
    "paths": {
        "root_dir": "",
        "tool_root": "",
    },
}


def load_toml_config(path: Path) -> tuple[dict, str, str]:
    if not path.exists():
        return {}, "missing", ""

    toml_loader = None
    try:
        import tomllib  # type: ignore

        toml_loader = tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore

            toml_loader = tomllib
        except ModuleNotFoundError:
            return (
                {},
                "parser_missing",
                "Python<3.11 需要安装 tomli 才能读取 config.toml",
            )

    try:
        with open(path, "rb") as file_obj:
            data = toml_loader.load(file_obj)
        if isinstance(data, dict):
            return data, "ok", ""
        return {}, "parse_error", "config.toml 顶层结构必须是 TOML 表（table）"
    except Exception as exc:
        return {}, "parse_error", str(exc)


def _cfg_get(cfg: dict, section: str, key: str, default: object) -> object:
    node = cfg.get(section, {})
    if not isinstance(node, dict):
        return default
    return node.get(key, default)


def _resolve_path(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return str(default or "")
    path = Path(text)
    if not path.is_absolute():
        return str((APP_ROOT / path).resolve())
    return str(path)


_cfg, CONFIG_LOAD_STATUS, CONFIG_LOAD_ERROR = load_toml_config(CONFIG_PATH)
CONFIG_LOAD_SOURCE = str(CONFIG_PATH)

# 归属厂商候选名单缺省值（D6.1）：设置页可追加，config.toml 顶层 `vendors` 覆盖。
DEFAULT_VENDORS: list[str] = ["摩众", "国瑞", "亿微", "明锐"]

DEFAULT_ROOT = str(_cfg_get(_cfg, "paths", "root_dir", _DEFAULTS["paths"]["root_dir"]))
TOOL_ROOT = _resolve_path(
    _cfg_get(_cfg, "paths", "tool_root", _DEFAULTS["paths"]["tool_root"]),
    _DEFAULTS["paths"]["tool_root"],
)


def normalize_vendor_list(values: list[str]) -> list[str]:
    """厂商名单规范化（D6.1）：trim 后非空 + casefold 去重保留首次展示大小写。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def load_vendor_candidates() -> list[str]:
    """读取 config.toml 顶层 ``vendors`` 名单（D6.1）。

    - 键缺失 / 非列表 / 含非字符串项 / 配置损坏 → 回退缺省名单，**不写盘**；
    - 显式空列表按原样返回空（用户可清空候选）；
    - 名单增删写入口归设置页（子任务 8），本函数只读。
    """
    if CONFIG_LOAD_STATUS != "ok":
        return list(DEFAULT_VENDORS)
    raw = _cfg.get("vendors")
    if not isinstance(raw, list):
        return list(DEFAULT_VENDORS)
    if any(not isinstance(item, str) for item in raw):
        return list(DEFAULT_VENDORS)
    return normalize_vendor_list(raw)


def _result(
    ok: bool, code: str, message: str, payload: dict[str, Any] | None = None
) -> ServiceResult:
    return {"ok": ok, "code": code, "message": message, "payload": payload or {}}


def _same_config_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _dump_config(document: dict[str, Any]) -> str:
    return tomli_w.dumps(document)


def _sync_process_cache(
    document: dict[str, Any], *, created: bool, sync_paths: bool
) -> None:
    """成功写入当前 CONFIG_PATH 后更新 load 所读的缓存。

    状态已是 ok 且本次只保存厂商时，只替换 vendors。
    """
    global _cfg, CONFIG_LOAD_STATUS, CONFIG_LOAD_ERROR
    vendors = document.get("vendors")
    stored = list(vendors) if isinstance(vendors, list) else None
    paths = document.get("paths")
    if created or CONFIG_LOAD_STATUS != "ok":
        copied: dict[str, Any] = dict(document)
        if stored is not None:
            copied["vendors"] = stored
        if isinstance(paths, dict):
            copied["paths"] = dict(paths)
        _cfg = copied
        CONFIG_LOAD_STATUS = "ok"
        CONFIG_LOAD_ERROR = ""
        return
    if stored is not None:
        _cfg["vendors"] = stored
    if not sync_paths or not isinstance(paths, dict):
        return
    current = _cfg.get("paths")
    if not isinstance(current, dict):
        current = {}
        _cfg["paths"] = current
    current.update(paths)


def _write_config_document(
    target: Path,
    document: dict[str, Any],
    *,
    created: bool,
    sync_paths: bool,
) -> ServiceResult:
    process = _same_config_path(target, CONFIG_PATH)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, _dump_config(document))
    except OSError as exc:
        return _result(False, "write_failed", f"配置保存失败：{exc}")
    if process:
        _sync_process_cache(document, created=created, sync_paths=sync_paths)
    return _result(True, "ok", "厂商名单已保存")


def save_vendor_candidates(
    values: list[str], *, config_path: Path | None = None
) -> ServiceResult:
    """把规范化后的厂商名单写入 config.toml 顶层 ``vendors``。

    成功写入当前 ``CONFIG_PATH`` 时同步进程缓存。其他路径不改缓存。
    """
    normalized = normalize_vendor_list(list(values))
    target = CONFIG_PATH if config_path is None else Path(config_path)
    process = _same_config_path(target, CONFIG_PATH)
    if not target.exists():
        return _write_config_document(
            target, {"vendors": list(normalized)}, created=True, sync_paths=False
        )

    data, status, _detail = load_toml_config(target)
    if status != "ok":
        return _result(False, "config_corrupt", "配置文件已损坏，未修改厂商名单")
    if "vendors" in data:
        raw = data.get("vendors")
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            return _result(
                False, "config_corrupt", "配置文件里的厂商名单无法读取，未写入"
            )
        on_disk = normalize_vendor_list(raw)
    else:
        on_disk = list(DEFAULT_VENDORS)
    current = load_vendor_candidates() if process else on_disk
    if current == normalized:
        return _result(True, "unchanged", "厂商名单未变化")
    data["vendors"] = list(normalized)
    return _write_config_document(target, data, created=False, sync_paths=False)


def save_path_settings(
    root_dir: str,
    tool_root: str,
    *,
    config_path: Path | None = None,
) -> ServiceResult:
    """只改 ``[paths]``，保留其他键。损坏的配置零写盘。"""
    target = CONFIG_PATH if config_path is None else Path(config_path)
    process = _same_config_path(target, CONFIG_PATH)
    root = str(root_dir or "").replace("\\", "/")
    tool = str(tool_root or "").replace("\\", "/")
    if not target.exists():
        vendors = load_vendor_candidates() if process else list(DEFAULT_VENDORS)
        document: dict[str, Any] = {
            "paths": {"root_dir": root, "tool_root": tool},
            "vendors": list(vendors),
        }
        result = _write_config_document(
            target, document, created=True, sync_paths=True
        )
        if result["ok"]:
            result["message"] = "配置已保存"
        return result

    data, status, _detail = load_toml_config(target)
    if status != "ok":
        return _result(False, "config_corrupt", "配置文件已损坏，未修改路径")
    raw = data.get("vendors")
    if "vendors" in data and (
        not isinstance(raw, list) or any(not isinstance(item, str) for item in raw)
    ):
        return _result(
            False, "config_corrupt", "配置文件里的厂商名单无法读取，未修改路径"
        )
    paths = data.get("paths")
    if not isinstance(paths, dict):
        paths = {}
        data["paths"] = paths
    paths["root_dir"] = root
    paths["tool_root"] = tool
    result = _write_config_document(target, data, created=False, sync_paths=True)
    if result["ok"]:
        result["message"] = "配置已保存"
    return result


# USB 扫描常量（硬编码，不再从用户配置读取）
SCAN_ROM_EXTENSIONS: list[str] = [".rom"]
SCAN_PKG_EXTENSIONS: list[str] = [".pkg"]
# 泛化的 "旧" 已按 D4.3③ 退役：它按子串匹配，会连带排除 旧款L36 这类真实型号
# 目录。``旧版本/`` 备用副本仍由 managed_paths 的**精确目录段**比较排除。
SCAN_EXCLUDE_DIR_KEYWORDS: list[str] = [
    "CH341SER",
    "接线图",
    "新建文件夹",
    "照片",
]
SCAN_MODEL_PATTERNS: list[str] = [r"(?:^|[_\-])((L\d+[A-Za-z]*))(?![A-Za-z0-9])"]
SCAN_VERSION_PATTERNS: list[str] = [
    r"[Vv](\d+\.\d+(?:\.\d+)?(?:_\d+)?)",
    r"_(\d+\.\d+(?:\.\d+)?(?:_\d+)?)$",
    r"_UI_(\d+\.\d+(?:\.\d+)?(?:_\d+)?)",
    r"_(\d+\.\d+\.\d+(?:_\d+))$",
    r"_(\d+\.\d+\.\d+(?:_\d+)?)$",
    r"_(\d+_\d+(?:_\d+)?)$",
    r"(?<![0-9A-Za-z])(\d+\.\d+\.\d+(?:_\d+)?)(?![0-9A-Za-z])",
]
SCAN_PATH_MODEL_PATTERNS: list[str] = [r"(L\d+[A-Za-z]*(?:max|pro|s)?)(?![A-Za-z0-9])"]
SCAN_PATH_VERSION_PATTERNS: list[str] = [
    r"(?:^|[_\-])([Vv]\d+\.\d+(?:\.\d+)?(?:_\d+)?)(?:[_\-]|$)",
    r"\b(\d+\.\d+(?:\.\d+)?(?:_\d+))\b",
    r"\b(\d+\.\d+\.\d+(?:_\d+))\b",
    r"\b(\d+\.\d+\.\d+)\b",
]
