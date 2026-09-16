import os
import re
import threading
from pathlib import Path
from typing import Literal, cast, get_args

from fwasset.core.firmware_catalog import (
    DEFAULT_FIRMWARE_CATALOG_PATH,
    FirmwareTypeConfig,
    enabled_firmware_types,
)
from fwasset.core.managed_paths import should_exclude_managed_path
from fwasset.core.path_guard import assert_within_workspace
from fwasset.core.platform_config import (
    PLATFORM_CONFIG_FILENAME,
    load_platform_config_strict,
)
from fwasset.core.scheme_config import discover_schemes, scheme_for_path
from fwasset.core.settings import (
    SCAN_EXCLUDE_DIR_KEYWORDS,
    SCAN_MODEL_PATTERNS,
    SCAN_PATH_MODEL_PATTERNS,
    SCAN_PATH_VERSION_PATTERNS,
    SCAN_PKG_EXTENSIONS,
    SCAN_ROM_EXTENSIONS,
    SCAN_VERSION_PATTERNS,
)
from fwasset.core.types import ChassisType, FirmwareAsset, HandcontrolFolder, ScanIssue

# 通用/定制 的一级目录名
_COMMON_DIR = "通用"
_CUSTOM_DIR = "定制"
# 双机芯平台的专属子目录名（在通用区内部）
_DUAL_CORE_DIR = "双机芯-上3D-下2D"

# 型号机芯类型枚举值集合（D0.1）：`平台配置.toml` 单块 name 命中才写入
_CHASSIS_TYPE_VALUES: frozenset[str] = frozenset(get_args(ChassisType))


def _match_first_group(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        try:
            match = re.search(pattern, text, re.IGNORECASE)
        except re.error:
            continue
        if match:
            return match.group(1)
    return ""


def _normalize_version(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.upper() if text[:1].upper() == "V" else f"V{text}"


def _has_allowed_extension(filename: str, extensions: list[str]) -> bool:
    lower_name = filename.lower()
    return any(lower_name.endswith(ext) for ext in extensions)


def _is_excluded_dir(dirpath: str, workspace_root: Path | None = None) -> bool:
    """目录是否跳过：受管路径（D4.3）+ legacy 关键词。

    受管判定走公共 helper，不在此复制字符串规则。``workspace_root`` 必须传入，
    否则 staging / 候选区 / 隔离区等**内部受管根无法被识别**，其中的固件会被
    扫描成正式资产。legacy ``SCAN_EXCLUDE_DIR_KEYWORDS`` 的泛化子串匹配作为
    短期兼容保留（含泛化的 ``"旧"``，会连带排除 ``旧款L36`` 这类真实型号
    目录——既存隐患，退役见父规格 D4.3③）。
    """
    if should_exclude_managed_path(
        dirpath, is_dir=True, workspace_root=workspace_root
    ):
        return True
    lower_path = str(dirpath or "").lower()
    return any(
        keyword and str(keyword).lower() in lower_path
        for keyword in SCAN_EXCLUDE_DIR_KEYWORDS
    )


def _visible_asset_files(
    dirpath: str, filenames: list[str], workspace_root: Path | None = None
) -> list[str]:
    """过滤掉受管元数据文件，返回排序后的程序文件名。

    ``程序信息.toml`` 是应用内部元数据，不得作为程序文件出现在详情里（D4.3）。
    """
    base = Path(dirpath)
    return sorted(
        name
        for name in filenames
        if not should_exclude_managed_path(
            base / name, is_dir=False, workspace_root=workspace_root
        )
    )


def _files_match_extensions(filenames: list[str], extensions: list[str]) -> bool:
    normalized_exts = [ext.lower() for ext in extensions if str(ext).strip()]
    if not normalized_exts:
        return False
    for name in filenames:
        lower_name = name.lower()
        if any(lower_name.endswith(ext) for ext in normalized_exts):
            return True
    return False


def _match_catalog_type(
    dirpath: str, filenames: list[str], type_configs: list[FirmwareTypeConfig]
) -> FirmwareTypeConfig | None:
    lower_parts = [part.lower() for part in Path(dirpath).parts]
    for cfg in type_configs:
        keywords = [
            str(item).strip().lower()
            for item in cfg.get("dir_keywords", [])
            if str(item).strip()
        ]
        if cfg.get("key") == "handcontrol_ui":
            has_rom = _files_match_extensions(filenames, SCAN_ROM_EXTENSIONS)
            has_pkg = _files_match_extensions(filenames, SCAN_PKG_EXTENSIONS)
            if has_rom and has_pkg:
                return cfg
            # 仓库硬约束：handcontrol_ui 必须同时有 .rom 与 .pkg；缺任一不得
            # 落入通用扩展名分支误认（R8 第五轮审查 P1-1）
            continue
        if keywords and not any(
            keyword in part for keyword in keywords for part in lower_parts
        ):
            continue
        if not _files_match_extensions(filenames, cfg.get("file_extensions", [])):
            continue
        return cfg
    return None


def _extract_model_version(dirpath: str, filenames: list[str]) -> tuple[str, str]:
    for name in filenames:
        model, version = parse_rom_filename(name)
        if model or version:
            return model or guess_model_from_path(
                dirpath
            ), version or guess_version_from_path(dirpath)
    return guess_model_from_path(dirpath), guess_version_from_path(dirpath)


def guess_series_from_model_or_path(model: str, dirpath: str) -> str:
    """Infer series from model first, then path segments, using the [A-Z]+[0-9]+ prefix rule."""
    candidates = [str(model or "")]
    candidates.extend(reversed([str(part) for part in Path(dirpath).parts]))
    for candidate in candidates:
        match = re.search(r"([A-Za-z]+\d+)", candidate)
        if match:
            return match.group(1).upper()
    return "未知系列"


def _model_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _is_known_model(model: str) -> bool:
    token = _model_token(model)
    return bool(re.search(r"(?:[a-z]+\d|\d+[a-z])", token))


def _part_contains_model(part: str, model: str) -> bool:
    model_token = _model_token(model)
    if not model_token:
        return False
    return model_token in _model_token(part)


def _model_directory_for_asset(
    root_path: Path, folder_path: Path, model: str = "", firmware_type: str = ""
) -> Path:
    try:
        relative_parts = folder_path.relative_to(root_path).parts
        base_path = root_path
    except ValueError:
        relative_parts = folder_path.parts
        base_path = Path(folder_path.anchor)
    if not relative_parts:
        return folder_path
    if _is_known_model(model):
        candidate = base_path
        matches: list[Path] = []
        for part in relative_parts:
            candidate = Path(candidate) / part
            if _part_contains_model(part, model):
                matches.append(candidate)
        if matches:
            if firmware_type == "handcontrol_ui":
                return matches[-1]
            return matches[0]
        if firmware_type == "handcontrol_ui":
            return folder_path
        return root_path / relative_parts[0]
    return root_path / relative_parts[0]


def _prefer_handcontrol_directory_model(folder_path: Path, model: str) -> str:
    path_model = guess_model_from_path(str(folder_path))
    if not _is_known_model(path_model):
        return model
    if not _is_known_model(model) or _part_contains_model(folder_path.name, path_model):
        return path_model
    return model


def _require_scan_root(root_path: Path, label: str) -> None:
    if not root_path.exists():
        raise FileNotFoundError(f"扫描根目录不存在: {label}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"扫描根目录不是目录: {label}")


def _load_scan_context(
    root_path: Path, catalog_path: str | Path | None
) -> tuple[list[FirmwareTypeConfig], list]:
    """加载固件类型目录配置与方案元数据（方案按工作区根发现）。"""
    type_configs = enabled_firmware_types(
        Path(catalog_path) if catalog_path else DEFAULT_FIRMWARE_CATALOG_PATH
    )
    return type_configs, discover_schemes(root_path)


def _chassis_type_for_model_root(
    model_root: Path,
) -> tuple[ChassisType | Literal[""], ScanIssue | None]:
    """型号根 → (机芯类型, warning 级 issue)。严格读取 `平台配置.toml`（D0.1a）。

    - 恰好一个块且 ``name`` 属于 :data:`ChassisType` 枚举 → 写入该 name；
    - ``missing``、多块、非枚举 name → ``""`` 且无 issue；
    - ``parse_error`` / ``parser_missing`` → ``""`` 且产生 warning 级
      :class:`ScanIssue`（不阻断索引对账），``path`` 记 `平台配置.toml`
      完整路径。
    """
    platforms, status, error = load_platform_config_strict(model_root)
    if status in ("parse_error", "parser_missing"):
        detail = error or status
        return "", {
            "severity": "warning",
            "message": f"平台配置读取失败（{status}），机芯类型留空：{detail}",
            "path": str(model_root / PLATFORM_CONFIG_FILENAME),
        }
    if status == "ok" and len(platforms) == 1:
        name = platforms[0].platform_name
        if name in _CHASSIS_TYPE_VALUES:
            return cast(ChassisType, name), None
    return "", None


def scan_firmware_assets(
    root: str,
    catalog_path: str | Path | None = None,
    last_scan_at: float | None = None,
    cancel_event: "threading.Event | None" = None,
) -> tuple[list[FirmwareAsset], list[ScanIssue]]:
    """
    Scan root using catalog-configured directory keywords and file extensions.
    Returns recognized assets plus explicit scan issues (severity-graded).

    If last_scan_at is provided, directories whose mtime is older than
    last_scan_at will be skipped (incremental mode).
    If cancel_event is provided, scanning can be interrupted by setting the event.
    """
    root_path = Path(root)
    _require_scan_root(root_path, root)
    type_configs, schemes = _load_scan_context(root_path, catalog_path)
    return _scan_assets(
        root_path, root_path, type_configs, schemes, last_scan_at, cancel_event
    )


def scan_firmware_subtree(
    workspace_root: str,
    subtree_root: str,
    catalog_path: str | Path | None = None,
    cancel_event: "threading.Event | None" = None,
) -> tuple[list[FirmwareAsset], list[ScanIssue]]:
    """局部子树扫描（REVIEW-20260728 R3）：仅遍历 ``subtree_root``。

    - 归属推导（category/platform/scheme_name/scheme_path/model_directory）
      按 ``workspace_root`` 计算，必须与 :func:`scan_firmware_assets` 的
      全根扫描对同一路径给出字段级一致的结果——本函数与全根扫描共享同一
      推导实现（:func:`_scan_assets`），不得在调用方手写字段映射。
    - ``subtree_root`` 必须位于 ``workspace_root`` 之下（经路径守卫校验）；
      子树不存在返回空快照（支撑删除恢复），存在但不是目录则报错。
    - 不支持 ``last_scan_at`` 增量跳过：局部重建必须拿到该子树的完整快照。
    """
    ws_path = Path(workspace_root)
    _require_scan_root(ws_path, workspace_root)
    sub_path = Path(subtree_root)
    assert_within_workspace(sub_path, ws_path)
    if not sub_path.exists():
        return [], []
    if not sub_path.is_dir():
        raise NotADirectoryError(f"扫描子树不是目录: {subtree_root}")
    type_configs, schemes = _load_scan_context(ws_path, catalog_path)
    return _scan_assets(ws_path, sub_path, type_configs, schemes, None, cancel_event)


def _scan_assets(
    context_root: Path,
    walk_root: Path,
    type_configs: list[FirmwareTypeConfig],
    schemes: list,
    last_scan_at: float | None,
    cancel_event: "threading.Event | None",
) -> tuple[list[FirmwareAsset], list[ScanIssue]]:
    """从 ``walk_root`` 遍历扫描，归属按 ``context_root`` 推导。"""
    # 函数内导入：reference_lookup 顶层依赖本模块（_is_excluded_dir），
    # 顶层互导会成环；归属规则单一真源在 reference_lookup（D0.1a 规则 1）。
    from fwasset.core.reference_lookup import (
        enumerate_model_roots,
        owner_model_root_for,
    )

    root_path = context_root
    results: list[FirmwareAsset] = []
    issues: list[ScanIssue] = []

    def _on_walk_error(exc: OSError) -> None:
        if exc.filename:
            issues.append(
                {
                    "severity": "error",
                    "message": f"{exc.filename}: {exc.strerror}",
                    "path": exc.filename,
                }
            )
        else:
            issues.append({"severity": "error", "message": str(exc), "path": ""})

    # 型号根按 context_root（工作区根）单次枚举；chassis 配置按型号根缓存，
    # 同一型号根单次扫描只读一次盘（D0.1a 规则 4/7）。
    model_roots = enumerate_model_roots(root_path)
    chassis_cache: dict[str, tuple[ChassisType | Literal[""], ScanIssue | None]] = {}
    chassis_issues: dict[str, ScanIssue] = {}

    def _chassis_for_folder(folder: Path) -> ChassisType | Literal[""]:
        owner = owner_model_root_for(folder, model_roots)
        if owner is None:
            return ""
        cache_key = str(owner)
        cached = chassis_cache.get(cache_key)
        if cached is None:
            cached = _chassis_type_for_model_root(owner)
            chassis_cache[cache_key] = cached
            if cached[1] is not None:
                # 同一型号根的损坏配置只告警一次（诊断对象是配置文件本身）
                chassis_issues[cache_key] = cached[1]
        return cached[0]

    for dirpath, dirnames, filenames in os.walk(walk_root, onerror=_on_walk_error):
        if cancel_event is not None and cancel_event.is_set():
            issues.append({"severity": "error", "message": "扫描已被用户取消", "path": ""})
            break

        if last_scan_at is not None and dirpath != str(walk_root):
            try:
                dir_mtime = Path(dirpath).stat().st_mtime
                if dir_mtime < last_scan_at:
                    # 仅跳过本目录的资产匹配，**不**剪枝子树：父目录 mtime 旧
                    # 不代表子孙未更新（改深层文件常不抬祖先 mtime）。
                    continue
            except OSError:
                pass

        if _is_excluded_dir(dirpath, root_path):
            # 排除目录整棵子树可剪（排除语义是整枝不要，与 mtime 无关）
            dirnames[:] = []
            continue
        cfg = _match_catalog_type(dirpath, filenames, type_configs)
        if cfg is None:
            continue

        folder_path = Path(dirpath)
        model, version = _extract_model_version(dirpath, filenames)
        if str(cfg["key"]) == "handcontrol_ui":
            model = _prefer_handcontrol_directory_model(folder_path, model)
        model_directory = _model_directory_for_asset(
            root_path, folder_path, model, str(cfg["key"])
        )
        series = guess_series_from_model_or_path(model, str(model_directory))
        files = _visible_asset_files(dirpath, filenames, root_path)
        label = f"{model}  {version or '-'}  [{folder_path.name}]  {cfg['label']}"
        chassis_type = _chassis_for_folder(folder_path)

        # --- 推断 category / platform / scheme ---
        category, platform, scheme_name, scheme_path = _infer_asset_context(
            root_path, folder_path, schemes
        )

        results.append(
            {
                "series": series,
                "firmware_type": cfg["key"],
                "firmware_label": cfg["label"],
                "flash_mode": cast(
                    Literal["auto_usb", "tool_launch", "manual_doc", "disabled"],
                    cfg["flash_mode"],
                ),
                "usb_flow": cfg.get("usb_flow", ""),
                "model": model,
                "version": version,
                "model_directory_name": model_directory.name,
                "model_directory_path": str(model_directory),
                "path": str(folder_path),
                "directory_name": folder_path.name,
                "files": files,
                "modified_time": folder_path.stat().st_mtime,
                "tool_name": cfg["tool_name"],
                "tool_path": cfg["tool_path"],
                "tool_dir": cfg.get("tool_dir", ""),
                "label": label,
                "category": cast(Literal["common", "custom", ""], category),
                "platform": platform,
                "scheme_name": scheme_name,
                "scheme_path": scheme_path,
                "chassis_type": chassis_type,
            }
        )

    results.sort(key=lambda item: (item["path"], item["firmware_type"]))
    # chassis 警告按型号根去重后统一追加（排序保证全量/子树诊断确定性）
    issues.extend(chassis_issues[key] for key in sorted(chassis_issues))
    return results, issues


def _infer_asset_context(
    root_path: Path,
    folder_path: Path,
    schemes: list,
) -> tuple[str, str, str, str]:
    """
    根据资产路径推断 (category, platform, scheme_name, scheme_path)。

    规则：
    - 路径相对 root 中包含 '通用' 段 → category='common'
    - 路径相对 root 中包含 '定制' 段 → category='custom'，向 scheme_for_path 查询方案
    - 在 '通用/双机芯-上3D-下2D/' 下 → platform='双机芯-上3D-下2D'
    - 其他通用区 → platform=''
    - 旧目录（无法推断）→ 全部留空字符串（向后兼容）
    """
    try:
        rel_parts = folder_path.relative_to(root_path).parts
    except ValueError:
        return "", "", "", ""

    if not rel_parts:
        return "", "", "", ""

    # 查找路径中 通用/定制 段的位置，支持多级嵌套
    # 如根目录/型号目录/通用/... 或 根目录/通用/... 两种
    common_idx = None
    custom_idx = None
    for i, part in enumerate(rel_parts):
        if part == _COMMON_DIR:
            common_idx = i
            break
        if part == _CUSTOM_DIR:
            custom_idx = i
            break

    if common_idx is not None:
        # 在通用区
        category = "common"
        # 检查是否在双机芯专属子目录下
        platform = (
            _DUAL_CORE_DIR
            if (
                len(rel_parts) > common_idx + 1
                and rel_parts[common_idx + 1] == _DUAL_CORE_DIR
            )
            else ""
        )
        return category, platform, "", ""

    if custom_idx is not None:
        # 在定制区，查找所属方案
        category = "custom"
        scheme = scheme_for_path(schemes, folder_path)
        if scheme is not None:
            return category, scheme.platform, scheme.name, str(scheme.path)
        return category, "", "", ""

    # 旧目录结构（既不是 通用 也不是 定制），向后兼容留空
    return "", "", "", ""


def parse_rom_filename(name: str) -> tuple[str, str]:
    """
    Parse model and version from ROM filename using configurable regex rules.
    """
    name_no_ext = Path(name).stem

    model = _match_first_group(name_no_ext, SCAN_MODEL_PATTERNS).upper()
    version = _normalize_version(_match_first_group(name_no_ext, SCAN_VERSION_PATTERNS))

    return model, version


def guess_model_from_path(dirpath: str) -> str:
    """Guess model from path segments using configurable regex rules."""
    for part in reversed(Path(dirpath).parts):
        matched = _match_first_group(part, SCAN_PATH_MODEL_PATTERNS)
        if matched:
            return matched.upper()
    return "未知型号"


def guess_version_from_path(dirpath: str) -> str:
    """Guess version from folder name/path segments when ROM filename has no version."""
    for part in reversed(Path(dirpath).parts):
        matched = _match_first_group(part, SCAN_PATH_VERSION_PATTERNS)
        if matched:
            return _normalize_version(matched)
    return ""


def handcontrol_folders_from_assets(
    assets: list[FirmwareAsset],
) -> list[HandcontrolFolder]:
    """从已扫描资产派生手控文件夹列表（不再二次 os.walk）。"""
    results: list[HandcontrolFolder] = []
    for asset in assets:
        if asset.get("firmware_type") != "handcontrol_ui":
            continue
        rom_files = [
            name
            for name in asset.get("files", [])
            if _has_allowed_extension(name, SCAN_ROM_EXTENSIONS)
        ]
        pkg_files = [
            name
            for name in asset.get("files", [])
            if _has_allowed_extension(name, SCAN_PKG_EXTENSIONS)
        ]
        if not rom_files or not pkg_files:
            continue
        results.append(
            {
                "path": str(asset.get("path", "")),
                "rom_file": rom_files[0],
                "pkg_file": pkg_files[0],
                "model": str(asset.get("model", "")),
                "version": str(asset.get("version", "")),
                "label": (
                    f"{asset.get('model', '')}  {asset.get('version', '')}  "
                    f"[{Path(str(asset.get('path', ''))).name}]"
                ),
            }
        )
    results.sort(key=lambda x: x["path"])
    return results


def find_handcontrol_folders(root: str) -> list[HandcontrolFolder]:
    """
    Recursively scan root and find folders containing both configured ROM and PKG files.
    Priority: ROM filename -> folder/path fallback.
    """
    assets, _issues = scan_firmware_assets(root)
    return handcontrol_folders_from_assets(assets)
