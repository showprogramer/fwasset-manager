from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fwasset.core.asset_index import query_assets
from fwasset.core.model_config import (
    SharedModuleRef,
    load_model_config,
    load_shared_modules,
)
from fwasset.core.platform_config import (
    PlatformDefaults,
    load_platform_config,
    load_platform_config_with_status,
)
from fwasset.core.scheme_config import discover_schemes
from fwasset.core.services.model_id_service import ensure_model_ids
from fwasset.core.services.platform_default_service import (
    canonical_module_dir,
)
from fwasset.core.services.platform_default_service import (
    set_module_default_for_model as _set_module_default_for_model,
)
from fwasset.core.services.shared_module_service import (
    clear_shared_module as _clear_shared_module_core,
)
from fwasset.core.services.shared_module_service import (
    set_shared_module as _set_shared_module_core,
)
from fwasset.core.shared_module_resolver import (
    SharedModuleResolution,
)
from fwasset.core.shared_module_resolver import (
    resolve_shared_module as resolve_shared_module_core,
)
from fwasset.core.types import FirmwareAsset

_log = logging.getLogger(__name__)


def _strip_model_suffix(name: str) -> str:
    """目录名 → 型号名（'L36程序' → 'L36'）。"""
    for suffix in ("程序", "目录"):
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name.strip()


def _module_matches(asset: FirmwareAsset, module_key: str) -> bool:
    """判断资产是否属于平台配置里的某个模块键。

    规范名统一为「机芯板」（catalog）；磁盘/toml 历史笔误「机芯版」仍可读。
    除标签/规范名相等外，检查 module_key 或规范名是否命中路径任一段。
    """
    key = str(module_key or "").strip()
    if not key:
        return False
    canon = canonical_module_dir(key)
    label = str(asset.get("firmware_label", "")).strip()
    if label == key or label == canon or canonical_module_dir(label) == canon:
        return True
    parts = Path(str(asset.get("path", ""))).parts
    if key in parts or canon in parts:
        return True
    return any(canonical_module_dir(p) == canon for p in parts)


@dataclass
class WorkbenchSelection:
    """表示当前工作台选中的节点状态"""

    model_name: str = ""  # 选中的型号名（顶级切换，如 "L36"）
    node_type: str = ""  # 选中的节点类型 ("common_type" 或 "custom_scheme")
    common_type: str = (
        ""  # 如果是 common_type，这是选中的 firmware_label (如 "主板程序")
    )
    scheme_name: str = (
        ""  # 如果是 custom_scheme，这是选中的方案名 (如 "以色列-Royal-Z9")
    )


@dataclass
class ModuleCardData:
    """右侧单张卡片的数据"""

    asset: FirmwareAsset
    source_type: str  # "common_default" | "custom_exclusive" | "common_fallback" | "common_variant"
    source_label: str  # 用于显示的来源标签文本
    is_fallback: bool  # 是否是回源模块
    source_kind: str = (
        ""  # "common" | "custom" — coarse bucket for the row-level 定制专属/通用 标签
    )
    default_badge: str = ""  # 平台默认徽章文案（如 "★默认"），非默认为空串
    shared_state: str = "local"  # local / shared_hit / shared_missing
    shared_source_label: str = ""  # 来源型号显示名
    shared_reason: str = ""  # 缺失原因
    effective_asset: FirmwareAsset | None = None  # 命中时实际打开/烧录的来源资产


# 整机标准模块的固定展示顺序（按烧录习惯，大部分机型包含这些模块）
STANDARD_MODULE_ORDER = [
    "主板程序",
    "手控UI",
    "蓝牙程序",
    "语音程序",
    "快捷键程序",
    "3D机芯板程序",
    "2D机芯板程序",
    "腿部程序",
]


@dataclass
class ModuleVariant:
    """模块下的单个具体变体（可直接烧录的资产）。"""

    asset: FirmwareAsset
    name: str  # 变体显示名（目录名）
    version: str
    source_kind: str  # "custom"（定制专属）/ "common"（通用）
    source_label: str  # 用户可读来源文案，绝不含"回源"
    default_badge: str = ""  # 平台默认徽章文案（如 "★默认"），非默认为空串
    shared_state: str = "local"  # local / shared_hit / shared_missing
    shared_source_label: str = ""  # 来源型号显示名
    shared_reason: str = ""  # 缺失原因
    effective_asset: FirmwareAsset | None = None  # 命中时实际打开/烧录的来源资产


@dataclass
class ModuleRow:
    """整机模块固定层级中的一行（一个模块类型）。"""

    label: str  # 模块中文名，如 "手控UI"
    source_kind: str  # 该模块整体来源：custom / common
    source_label: str  # 行级来源文案
    variants: list[ModuleVariant]  # 该模块下的所有变体（单变体则长度为 1）


class SchemeWorkbenchModel:
    def __init__(self):
        self.db_path: Path | None = None
        self.root_dir: Path | None = None
        # R8 规则 6：设置中的权威配置根；仅 scan_meta 兜底恢复出的根不得当作
        # 配置根（此时保持 None，型号 id 只读、不自动创建）。
        self.configured_root: Path | None = None
        self._platforms: list[PlatformDefaults] = []
        self._model_root_paths: dict[str, str] = {}  # model_name → model_directory_path
        # 布局模式：True = 扫描根本身是一个型号目录（含 通用/定制）；
        # False = 扫描根是父文件夹，一级子目录各是一个型号（多型号根）。
        self._single_model_root: bool = True
        self._multi_model_dirs: dict[str, str] = {}  # model_name → 一级子目录名
        self._platforms_by_model: dict[str, list[PlatformDefaults]] = {}
        # 型号持久 id 映射（B0）；UI 选型键仍为 display_name
        self._model_id_by_dir: dict[str, str] = {}  # dir_name → model_id
        self._model_id_by_display: dict[str, str] = {}  # display_name → model_id
        self._root_by_model_id: dict[str, Path] = {}  # model_id → 型号根 Path
        # Single-shot in-memory asset cache. Populated by bind() so the hot
        # path (sidebar rebuild on every search keystroke) does not re-query
        # SQLite. Methods that previously called query_assets() now read from
        # this list and filter in Python. The cache is invalidated by bind();
        # see test_cache_invalidates_on_rebind.
        self._all_assets: list[FirmwareAsset] = []

    def _cache_loaded(self) -> bool:
        """True once bind() has populated the asset cache."""
        return bool(self._all_assets)

    def bind(
        self,
        db_path: Path | None,
        root_dir: Path,
        configured_root: Path | str | None = None,
    ) -> None:
        self.db_path = db_path
        self.root_dir = root_dir
        self.configured_root = Path(configured_root) if configured_root else None
        # Load once. Any view method on the hot path reads from this list.
        self._all_assets = list(query_assets(path=self.db_path))
        self._single_model_root = self._detect_single_model_root()
        self._load_multi_model_dirs()
        self._load_model_root_paths()
        self._load_model_ids()
        self._load_platforms_for_all_models()

    def _detect_single_model_root(self) -> bool:
        """扫描根直接含 通用/定制 → 单型号根；否则视为多型号父文件夹。

        根目录不存在（如仅有索引的测试场景）时保持单型号语义，向后兼容。
        """
        if self.root_dir is None or not self.root_dir.is_dir():
            return True
        return (self.root_dir / "通用").is_dir() or (self.root_dir / "定制").is_dir()

    def _load_multi_model_dirs(self):
        """多型号根：从资产路径的一级子目录枚举型号（'L36程序' → 'L36'）。

        以"该子目录下扫到过资产"为准，未整理的型号目录（还没有 通用/定制）
        也会出现在型号列表里。
        """
        self._multi_model_dirs.clear()
        if self.root_dir is None or self._single_model_root:
            return
        for a in self._all_assets:
            try:
                rel = Path(str(a.get("path", ""))).relative_to(self.root_dir)
            except ValueError:
                continue
            if not rel.parts:
                continue
            top = rel.parts[0]
            model = _strip_model_suffix(top)
            if model and model not in self._multi_model_dirs:
                self._multi_model_dirs[model] = top

    def _load_model_root_paths(self):
        """从数据库中提取每个型号的根目录路径 (model_directory_path)。"""
        self._model_root_paths.clear()
        # Use the cache when available (avoid re-hitting SQLite for a derived
        # lookup that runs at every bind). Falls back to a one-shot query if
        # bind() was bypassed somehow.
        assets = (
            self._all_assets
            if self._cache_loaded()
            else query_assets(path=self.db_path)
        )
        for a in assets:
            model = str(a.get("model", ""))
            mdp = str(a.get("model_directory_path", ""))
            if model and mdp and model not in self._model_root_paths:
                self._model_root_paths[model] = mdp

    def _enumerate_model_roots(self) -> list[Path]:
        """型号根目录列表（不落在 ``通用/``）。

        单型号根 → 扫描根自身；多型号父根 → ``root_dir / dir_name``。
        """
        if self.root_dir is None or not self.root_dir.is_dir():
            return []
        if self._single_model_root:
            return [self.root_dir]
        roots: list[Path] = []
        for dir_name in self._multi_model_dirs.values():
            p = self.root_dir / dir_name
            if p.is_dir():
                roots.append(p)
        return roots

    def _load_model_ids(self) -> None:
        """ensure_model_ids 后建立 id 映射；单根损坏不阻断。"""
        self._model_id_by_dir.clear()
        self._model_id_by_display.clear()
        self._root_by_model_id.clear()
        roots = self._enumerate_model_roots()
        if not roots:
            return
        try:
            result = ensure_model_ids(
                roots,
                self.configured_root,
                log_fn=lambda _m: None,
            )
        except Exception:  # noqa: BLE001
            # 不因 id 服务异常阻断工作台
            result = None
        if result is not None and not result["ok"]:
            code = str(result.get("code", ""))
            if code in ("out_of_workspace", "invalid_root"):
                # 门闩拒绝：不进入成功流程（也不建立 id 映射）
                return
            # not_configured / parse_error：只读降级，继续读取盘上已有 id
        for root in roots:
            try:
                mid, status, _err = load_model_config(root)
            except Exception:  # noqa: BLE001
                continue
            if status != "ok" or not mid:
                continue
            dir_name = root.name
            display = _strip_model_suffix(dir_name)
            self._model_id_by_dir[dir_name] = mid
            if display:
                self._model_id_by_display[display] = mid
            self._root_by_model_id[mid] = root

    def ensure_model_id(self, model_root: str | Path) -> str:
        """返回型号根持久 id；缺失时触发写入后返回（失败只读降级返回空）。"""
        root = Path(model_root)
        mid, status, _ = load_model_config(root)
        if status == "ok" and mid:
            return mid
        result = ensure_model_ids([root], self.configured_root, log_fn=lambda _m: None)
        if not result["ok"] and result["code"] not in ("not_configured",):
            # 写入门闩拒绝（越界/非法根等）：不进入成功流程
            return ""
        mid, status, _ = load_model_config(root)
        if status == "ok" and mid:
            dir_name = root.name
            display = _strip_model_suffix(dir_name)
            self._model_id_by_dir[dir_name] = mid
            if display:
                self._model_id_by_display[display] = mid
            self._root_by_model_id[mid] = root
            return mid
        return ""

    def resolve_model_id(self, name: str) -> str | None:
        """display_name 或 dir_name → model_id。"""
        key = str(name or "").strip()
        if not key:
            return None
        if key in self._model_id_by_dir:
            return self._model_id_by_dir[key]
        if key in self._model_id_by_display:
            return self._model_id_by_display[key]
        return None

    def model_root_for_id(self, model_id: str) -> Path | None:
        """model_id → 型号根 Path；工作区内无此 id → None。"""
        mid = str(model_id or "").strip()
        if not mid:
            return None
        return self._root_by_model_id.get(mid)

    def _model_root_path_for_name(self, model_name: str) -> Path | None:
        """display_name → 型号根（不落 通用/）。"""
        name = str(model_name or "").strip()
        if not name or self.root_dir is None:
            return None
        if self._single_model_root:
            # 接受结构化型号名、目录名、或去尾缀后与目录名相等的传入
            if (
                name == self._structural_model()
                or name == self.root_dir.name
                or _strip_model_suffix(self.root_dir.name) == name
            ):
                return self.root_dir
            return None
        dir_name = self._multi_model_dirs.get(name, "")
        if dir_name:
            p = self.root_dir / dir_name
            return p if p.is_dir() else None
        # 直接当 dir_name
        p = self.root_dir / name
        return p if p.is_dir() else None

    def get_shared_modules(self, model_name: str) -> list[SharedModuleRef]:
        """读取该型号根 ``型号配置.toml`` 的共享引用；损坏/缺失 → []。"""
        root = self._model_root_path_for_name(model_name)
        if root is None:
            return []
        try:
            return load_shared_modules(root)
        except Exception:  # noqa: BLE001
            return []

    def resolve_shared_module(
        self, model_name: str, module_key: str
    ) -> SharedModuleResolution | None:
        """解析该型号某模块的共享引用；无引用 → None。"""
        key = canonical_module_dir(module_key)
        if not key or self.root_dir is None:
            return None
        refs = self.get_shared_modules(model_name)
        ref = next((r for r in refs if r.module_key == key), None)
        if ref is None:
            return None
        return resolve_shared_module_core(
            ref,
            workspace_root=self.root_dir,
            root_for_model_id=self.model_root_for_id,
        )

    # --- 共享登记入口（Phase B2）---
    def register_shared_module(
        self,
        target_model_name: str,
        source_asset: FirmwareAsset,
        module_key: str = "",
        overwrite: bool = False,
        mode: str = "static",
        source_platform: str = "",
        log_fn: Callable[..., None] = print,
    ) -> dict:
        """手动登记一条共享引用到目标型号根的 `型号配置.toml`。

        工作区根取 ``self.root_dir``；目标型号根经 ``_model_root_path_for_name``
        反查（不落 ``通用/``）。仅接受工作区内已扫描到的真实来源资产——
        分层纪律见 TASK-20260720。
        """
        if self.root_dir is None:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": "登记共享来源失败：工作区未绑定",
                "payload": {},
            }
        target_root = self._model_root_path_for_name(target_model_name)
        if target_root is None:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": f"登记共享来源失败：目标型号不存在 ({target_model_name})",
                "payload": {},
            }
        return _set_shared_module_core(
            target_model_root=target_root,
            source_asset=source_asset,
            workspace_root=self.root_dir,
            module_key=module_key,
            overwrite=overwrite,
            mode=mode,
            source_platform=source_platform,
            log_fn=log_fn,
        )

    def unregister_shared_module(
        self,
        target_model_name: str,
        module_key: str,
        log_fn: Callable[..., None] = print,
    ) -> dict:
        """取消登记：删除目标模块的共享引用条目（只删 toml 不删文件，B4b）。"""
        if self.root_dir is None:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": "取消共享失败：工作区未绑定",
                "payload": {},
            }
        target_root = self._model_root_path_for_name(target_model_name)
        if target_root is None:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": f"取消共享失败：目标型号不存在 ({target_model_name})",
                "payload": {},
            }
        return _clear_shared_module_core(
            target_root, module_key, workspace_root=self.root_dir, log_fn=log_fn
        )

    # 与 query_assets / _filter_assets 共用的跨字段分词搜索字段
    _KEYWORD_FIELDS = (
        "series",
        "model",
        "version",
        "firmware_label",
        "directory_name",
        "model_directory_name",
        "path",
        "flash_mode",
        "scheme_name",
        "platform",
    )

    def _asset_matches_keyword(self, asset: FirmwareAsset, keyword: str) -> bool:
        """空格分词 AND：每个 token 在 _KEYWORD_FIELDS 任一字段命中（OR）。"""
        tokens = keyword.split() if keyword else []
        if not tokens:
            return True
        for token in tokens:
            needle = token.lower()
            hit = any(
                needle in str(asset.get(field, "")).lower()
                for field in self._KEYWORD_FIELDS
            )
            if not hit:
                return False
        return True

    def _filter_assets(
        self,
        *,
        keyword: str = "",
        category: str = "",
        scheme_name: str = "",
    ) -> list[FirmwareAsset]:
        """Read assets from the cache, applying the same filters query_assets would.

        Falls back to a one-shot query_assets call if bind() has not populated
        the cache yet — keeps the model correct for callers that skip bind
        (and lets the existing 6 fixture tests keep passing).
        """
        if not self._cache_loaded():
            return query_assets(
                keyword=keyword,
                path=self.db_path,
                category=category,
                scheme_name=scheme_name,
            )
        out: list[FirmwareAsset] = []
        # scheme_name uses a substring LIKE in query_assets; mirror that.
        scheme_needle = scheme_name.strip().lower() if scheme_name else ""
        for a in self._all_assets:
            if category and str(a.get("category", "")) != category:
                continue
            if scheme_needle:
                sn = str(a.get("scheme_name", "")).lower()
                if scheme_needle not in sn:
                    continue
            if not self._asset_matches_keyword(a, keyword):
                continue
            out.append(a)
        return out

    def _load_platforms_for_all_models(self):
        """加载平台配置（平台配置.toml），并按型号隔离。

        - 多型号根：每个型号只读自己目录下的 `平台配置.toml`（型号间绝不混用，
          否则同名平台/同名模块的默认会跨型号串扰）。
        - 单型号根：配置位于扫描根（如 L36程序/），优先从 root_dir 读取；再尝试
          每个 model_directory_path 及其上层目录，结果去重合并。
        `self._platforms` 保留为全量合并列表，供无型号上下文的旧调用使用。
        """
        self._platforms.clear()
        self._platforms_by_model.clear()

        def _merge(platforms: list[PlatformDefaults]) -> None:
            for plat in platforms:
                if plat.platform_name not in {p.platform_name for p in self._platforms}:
                    self._platforms.append(plat)

        if not self._single_model_root and self.root_dir is not None:
            for model, dir_name in self._multi_model_dirs.items():
                platforms = load_platform_config(self.root_dir / dir_name)
                self._platforms_by_model[model] = platforms
                _merge(platforms)
            return

        seen_dirs: set[str] = set()
        candidate_dirs: list[Path] = []
        if self.root_dir is not None:
            candidate_dirs.append(self.root_dir)
        # 兼容：型号目录可能各自带平台配置，从 model_directory_path 向上回溯
        for model_path in self._model_root_paths.values():
            p = Path(model_path)
            candidate_dirs.append(p)
            candidate_dirs.append(p.parent)

        for model_dir in candidate_dirs:
            key = str(model_dir)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            _merge(load_platform_config(model_dir))

        structural = self._structural_model()
        if structural:
            self._platforms_by_model[structural] = list(self._platforms)

    def _platforms_for(self, model_name: str) -> list[PlatformDefaults]:
        """该型号可用的平台列表；无型号上下文时退化为全量合并列表。"""
        return self._platforms_by_model.get(model_name, self._platforms)

    def _model_of_asset(self, asset: FirmwareAsset) -> str:
        """资产所属型号：多型号根按一级子目录推导，单型号根即结构化型号。"""
        if self._single_model_root or self.root_dir is None:
            return self._structural_model()
        try:
            rel = Path(str(asset.get("path", ""))).relative_to(self.root_dir)
        except ValueError:
            return ""
        return _strip_model_suffix(rel.parts[0]) if rel.parts else ""

    # --- 平台默认（设默认功能） ---
    def platform_names(self, model_name: str = "") -> list[str]:
        """可选平台名列表：优先取该型号的平台配置，缺失时退化为资产上出现过的平台。"""
        platforms = self._platforms_for(model_name) if model_name else self._platforms
        names = [p.platform_name for p in platforms]
        if names:
            return names
        seen: list[str] = []
        for a in self._all_assets:
            if model_name and not self._belongs_to_model(a, model_name):
                continue
            pn = str(a.get("platform", ""))
            if pn and pn not in seen:
                seen.append(pn)
        return seen

    def _common_module_parts(self, asset: FirmwareAsset) -> tuple[str, str]:
        """解析通用区资产的 (模块目录名, 变体目录名)。

        路径形如 …/通用/主板程序/量产_默认：模块 = 通用后的第一段（跳过双机芯
        等平台子目录），变体 = 资产目录名。资产目录直接位于模块层（该模块唯一
        一份）时变体返回空串——与 平台配置.toml 中空值的语义一致。
        非通用区或无法解析时返回 ("", "")。
        """
        parts = Path(str(asset.get("path", ""))).parts
        if "通用" not in parts:
            return "", ""
        rest = list(parts[parts.index("通用") + 1 :])
        platform = str(asset.get("platform", ""))
        if rest and platform and rest[0] == platform:
            rest = rest[1:]
        if not rest:
            return "", ""
        if len(rest) == 1:
            return rest[0], ""
        return rest[0], rest[-1]

    def _common_assets_matching_module(
        self, model_name: str, module_key: str
    ) -> list[FirmwareAsset]:
        """当前型号下、匹配平台配置模块键的全部通用资产。"""
        assets = (
            self._all_assets
            if self._cache_loaded()
            else query_assets(path=self.db_path)
        )
        out: list[FirmwareAsset] = []
        for a in assets:
            if str(a.get("category", "")) != "common":
                continue
            if not self._belongs_to_model(a, model_name):
                continue
            if _module_matches(a, module_key):
                out.append(a)
        return out

    def _module_key_for_asset(self, asset: FirmwareAsset) -> str:
        """模块键：catalog label 优先，否则路径段；并归一机芯板。"""
        label = str(asset.get("firmware_label", "")).strip()
        if label:
            return canonical_module_dir(label)
        module_dir, _ = self._common_module_parts(asset)
        return canonical_module_dir(module_dir)

    def _module_has_defaults_key(
        self, platforms: list[PlatformDefaults], module_key: str
    ) -> bool:
        """任一配置块是否已有该模块（含版/板同义）的 defaults 键。"""
        canon = canonical_module_dir(module_key)
        if not canon:
            return False
        for p in platforms:
            for key in p.defaults:
                if canonical_module_dir(key) == canon:
                    return True
        return False

    @staticmethod
    def _preferred_default_items(defaults: dict[str, str]) -> list[tuple[str, str]]:
        """按规范模块键去重 defaults，规范键优先于同义历史键。"""
        selected: dict[str, tuple[str, str]] = {}
        for key, value in defaults.items():
            canon = canonical_module_dir(key)
            if not canon:
                continue
            current = selected.get(canon)
            if current is None or key == canon:
                selected[canon] = (key, value)
        return list(selected.values())

    def default_platforms_for(self, asset: FirmwareAsset) -> list[str]:
        """返回把该通用变体配置为默认程序的平台名列表（只看资产所属型号的配置）。

        平台配置约定：
        - ``defaults[模块] = "量产_默认"``：变体目录名精确匹配；
        - ``defaults[模块] = ""``：该模块**唯一**通用变体即默认（可嵌套在
          ``通用/语音程序/中文唯一版/``，不要求文件直接落在模块目录下）。
          若匹配该模块的通用资产不止一份，视为配置异常，不标默认。
        - **A4 无键推断**：配置块存在但该模块无键、且通用区仅一份变体 → 视为
          各块隐式默认（不猜多变体）。
        """
        if str(asset.get("category", "")) != "common":
            return []
        dir_name = str(asset.get("directory_name", ""))
        if not dir_name:
            return []
        model_name = self._model_of_asset(asset)
        asset_path = str(asset.get("path", ""))
        platforms = self._platforms_for(model_name)
        names: list[str] = []
        for p in platforms:
            for module_key, variant_name in self._preferred_default_items(p.defaults):
                if not _module_matches(asset, module_key):
                    continue
                configured = str(variant_name or "").strip()
                if configured == "":
                    peers = self._common_assets_matching_module(model_name, module_key)
                    if len(peers) == 1 and str(peers[0].get("path", "")) == asset_path:
                        names.append(p.platform_name)
                        break
                elif configured == dir_name:
                    names.append(p.platform_name)
                    break
        if names:
            return names
        # A4：无键且唯一变体 → 隐式默认（有配置块时标全部块名；无块时标空列表由徽章处理）
        module_key = self._module_key_for_asset(asset)
        if not module_key or self._module_has_defaults_key(platforms, module_key):
            return []
        peers = self._common_assets_matching_module(model_name, module_key)
        if len(peers) == 1 and str(peers[0].get("path", "")) == asset_path:
            if platforms:
                return [p.platform_name for p in platforms]
            return ["*"]
        return []

    def default_badge(self, asset: FirmwareAsset) -> str:
        """默认徽章文案：该型号单平台只标 ★默认，多平台附上平台名。"""
        names = self.default_platforms_for(asset)
        if not names:
            return ""
        platforms = self._platforms_for(self._model_of_asset(asset))
        # A4 无配置块时的隐式唯一默认：只标 ★默认，不展示内部名
        if not platforms or names == ["*"] or len(platforms) <= 1:
            return "★默认"
        real = [n for n in names if n != "*"]
        if len(real) <= 1:
            return "★默认"
        return "★默认·" + "/".join(real)

    def _platform_config_root(self, model_name: str) -> str:
        """平台配置.toml 的落盘目录。

        不能直接用 _get_model_root：扫描器的 model_directory_path 可能指向
        通用/ 子目录，把配置写到那里后加载器会先读到扫描根下的旧文件（按平台
        名去重，旧值获胜）。因此按加载器同样的候选顺序找现存配置文件所在目录；
        都不存在时落在扫描根（配置的规范位置）。
        """
        # 多型号根：配置的规范位置就是各型号自己的目录
        if not self._single_model_root:
            return self._get_model_root(model_name)

        candidates: list[Path] = []
        if self.root_dir is not None:
            candidates.append(self.root_dir)
        model_path = self._model_root_paths.get(model_name, "")
        if model_path:
            p = Path(model_path)
            candidates.extend([p, p.parent])
        for d in candidates:
            if (d / "平台配置.toml").exists():
                return str(d)
        return str(candidates[0]) if candidates else ""

    def set_default_variant(
        self,
        model_name: str,
        asset: FirmwareAsset,
        log_fn: Callable[..., None] = print,
        platform_name: str = "",  # 兼容旧调用；已忽略，默认按型号+模块写入全部配置块
    ) -> dict:
        """把选中的通用变体设为该型号下该模块的默认版本（写入 平台配置.toml）。

         业务维度是「型号 + 模块」（如 L36 蓝牙），不是 toml 里的 name
        （标准单机芯3D 等仅为方案回源分组）。成功后同步该型号根下所有
         [[platform]] 的同名模块键，并就地重载配置。
        """
        del platform_name  # 显式忽略：避免再按「平台」分叉写默认
        module_dir, variant_name = self._common_module_parts(asset)
        if not module_dir:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": "设置默认失败：该程序不在通用区，无法设为模块默认版本",
                "payload": {},
            }
        if self.root_dir is None:
            return {
                "ok": False,
                "code": "invalid_args",
                "message": "设置默认失败：工作区未绑定",
                "payload": {},
            }
        # 规范模块名：优先 catalog label（板），否则路径段归一「版」→「板」
        label = str(asset.get("firmware_label", "")).strip()
        module_dir = canonical_module_dir(label or module_dir)
        result = _set_module_default_for_model(
            self._platform_config_root(model_name),
            module_dir,
            variant_name,
            workspace_root=self.root_dir,
            log_fn=log_fn,
            model_name=model_name,
        )
        if result["ok"]:
            self._load_platforms_for_all_models()
        return result

    def is_model_module_default(self, asset: FirmwareAsset) -> bool:
        """该通用变体是否已是本型号下该模块的默认（所有配置块一致指向它）。

        仅依据已落盘的 ``平台配置.toml``（``_platforms_for``），不用资产上的
        platform 字段冒充「已有配置」。无落盘配置时返回 False，以便 UI 仍可
        点「设为默认」触发 A2 建文件（A4 隐式唯一默认只影响徽章/回源，不锁菜单）。
        """
        if str(asset.get("category", "")) != "common":
            return False
        model_name = self._model_of_asset(asset)
        platforms = self._platforms_for(model_name)
        if not platforms:
            return False
        current = set(self.default_platforms_for(asset))
        return set(p.platform_name for p in platforms) <= current

    def _structural_model(self) -> str:
        """从扫描根目录名推导结构化型号（如 'L36程序' → 'L36'）。

        固件目录是单型号根（整个 root_dir 即一个型号），因此型号应以目录结构为准，
        而不是来自单个文件名的解析（文件名里出现 'L50S' 等会污染型号列表）。
        """
        if self.root_dir is None:
            return ""
        return _strip_model_suffix(self.root_dir.name)

    def _belongs_to_model(self, asset: FirmwareAsset, model_name: str) -> bool:
        """资产是否属于该型号。

        多型号根：以"路径的一级子目录 == 该型号目录"为准，不看解析 model
        （文件名里的 L50S 等噪声会跨型号误判）。
        单型号根：资产的解析 model 可能因文件名噪声而异（L50/L50S...），
        但它们物理上都在 root_dir 内，因此以"路径在 root_dir 下"为准。
        兼容旧多型号结构：解析 model 精确相等也算。
        """
        if not self._single_model_root:
            dir_name = self._multi_model_dirs.get(model_name, "")
            if not dir_name or self.root_dir is None:
                return False
            try:
                rel = Path(str(asset.get("path", ""))).relative_to(self.root_dir)
            except ValueError:
                return False
            return bool(rel.parts) and rel.parts[0] == dir_name

        if str(asset.get("model", "")) == model_name:
            return True
        if self.root_dir is None or model_name != self._structural_model():
            return False
        try:
            Path(str(asset.get("path", ""))).relative_to(self.root_dir)
            return True
        except ValueError:
            return False

    def _get_model_root(self, model_name: str) -> str:
        """获取给定型号的根目录路径。"""
        if not self._single_model_root:
            dir_name = self._multi_model_dirs.get(model_name, "")
            if dir_name and self.root_dir is not None:
                return str(self.root_dir / dir_name)
            return ""
        root = self._model_root_paths.get(model_name, "")
        if root:
            return root
        # 结构化型号（单型号根）→ 直接用扫描根目录
        if self.root_dir is not None and model_name == self._structural_model():
            return str(self.root_dir)
        return ""

    def load_all_models(self) -> list[str]:
        """获取型号列表，用于下拉框。

        多型号根：枚举一级子目录推导的型号；单型号根：以扫描根目录名为准。
        两者都不从文件名解析型号，避免 L50/L50S 等噪声产生假型号。
        """
        if not self._single_model_root and self._multi_model_dirs:
            return sorted(self._multi_model_dirs)
        structural = self._structural_model()
        if structural:
            return [structural]
        # 退化：无 root_dir 时回退到解析 model（兼容旧调用）
        assets = (
            self._all_assets
            if self._cache_loaded()
            else query_assets(path=self.db_path)
        )
        models = {str(a.get("model", "")) for a in assets if str(a.get("model", ""))}
        return sorted(models)

    def build_sidebar_tree(self, model_name: str) -> dict:
        """
        构建左侧树的数据结构。
        格式:
        {
            "common": {"主板程序": 6, "手控UI": 17, ...},
            "custom": ["以色列-Royal-Z9", "葡萄牙-小明", ...]
        }
        """
        if not model_name:
            return {"common": {}, "custom": []}

        assets = (
            self._all_assets
            if self._cache_loaded()
            else query_assets(path=self.db_path)
        )
        # 过滤出该型号的资产
        model_assets = [a for a in assets if self._belongs_to_model(a, model_name)]

        common_counts: dict[str, int] = {}
        custom_schemes: set[str] = set()

        for a in model_assets:
            cat = str(a.get("category", ""))
            if cat == "common":
                label = str(a.get("firmware_label", a.get("firmware_type", "")))
                common_counts[label] = common_counts.get(label, 0) + 1
            elif cat == "custom":
                scheme = str(a.get("scheme_name", ""))
                if scheme:
                    custom_schemes.add(scheme)

        return {"common": common_counts, "custom": sorted(list(custom_schemes))}

    def _shared_source_assets(
        self, resolution: SharedModuleResolution
    ) -> list[FirmwareAsset]:
        """按解析器给出的来源变体目录，映射回索引中的真实资产。"""
        wanted = [p.resolve() for p in resolution.variants]
        if not wanted:
            return []
        assets = (
            self._all_assets
            if self._cache_loaded()
            else query_assets(path=self.db_path)
        )
        matched: list[FirmwareAsset] = []
        for asset in assets:
            try:
                asset_path = Path(str(asset.get("path", ""))).resolve()
            except (OSError, RuntimeError):
                continue
            if any(asset_path == path or path in asset_path.parents for path in wanted):
                matched.append(asset)
        matched.sort(key=lambda a: str(a.get("path", "")))
        return matched

    def _decorate_shared_cards(
        self, model_name: str, cards: list[ModuleCardData]
    ) -> list[ModuleCardData]:
        """给现有模块卡附加共享状态；不创建独立共享列表。"""
        for card in cards:
            if card.source_kind != "common":
                continue
            module_key = canonical_module_dir(str(card.asset.get("firmware_label", "")))
            resolution = self.resolve_shared_module(model_name, module_key)
            if resolution is None:
                continue
            card.shared_reason = resolution.reason
            if resolution.status != "hit":
                _log.info(
                    "共享模块解析失败：%s/%s → %s",
                    model_name,
                    module_key,
                    resolution.reason,
                )
                card.shared_state = "shared_missing"
                continue
            source_assets = self._shared_source_assets(resolution)
            if not source_assets:
                _log.info(
                    "共享模块路径不存在：%s/%s → %s",
                    model_name,
                    module_key,
                    resolution.resolved_path,
                )
                card.shared_state = "shared_missing"
                card.shared_reason = "path_not_found"
                continue
            card.shared_state = "shared_hit"
            card.effective_asset = source_assets[0]
            source_root = self.model_root_for_id(resolution.ref.source_model_id)
            card.shared_source_label = self._shared_label_for_ref(
                resolution.ref, source_root
            )
        return cards

    def _shared_label_for_ref(
        self, ref: SharedModuleRef, source_root: Path | None
    ) -> str:
        """根据 mode 生成模块列表展示文案。"""
        model_name = (
            _strip_model_suffix(source_root.name)
            if source_root is not None
            else ref.source_model_id
        )
        if ref.mode == "follow_default":
            return f"同{model_name}"
        return f"来自{model_name}"

    def get_source_platforms_for_asset(self, source_asset: FirmwareAsset) -> list[str]:
        """From a source asset, return the platform names in its model root's 平台配置.toml."""
        if self.root_dir is None:
            return []
        asset_path = Path(str(source_asset.get("path", "") or ""))
        if not asset_path.is_absolute():
            return []
        # 源型号根：工作区单型号根 → root_dir；多型号 → 第一段
        try:
            rel = asset_path.resolve().relative_to(self.root_dir.resolve())
        except ValueError:
            return []
        parts = rel.parts
        if not parts:
            return []
        first = parts[0]
        if first in ("通用", "定制"):
            src_root = self.root_dir
        else:
            src_root = self.root_dir / first
        platforms, status, _ = load_platform_config_with_status(src_root)
        if status != "ok":
            return []
        return [p.platform_name for p in platforms]

    def get_common_modules(
        self, model_name: str, firmware_label: str, keyword: str = ""
    ) -> list[ModuleCardData]:
        """点击通用模块时，返回该类型下的所有变体"""
        assets = self._filter_assets(keyword=keyword, category="common")
        results: list[ModuleCardData] = []

        for a in assets:
            if not self._belongs_to_model(a, model_name):
                continue
            if str(a.get("category", "")) != "common":
                continue
            if (
                str(a.get("firmware_label", "")) != firmware_label
                and str(a.get("firmware_type", "")) != firmware_label
            ):
                continue

            # 判断是否是默认变体
            dir_name = str(a.get("directory_name", ""))
            is_default = "_默认" in dir_name

            source_type = "common_default" if is_default else "common_variant"
            # 用户可见归属：仅「通用」（禁内部路径/回源字样）
            source_label = "通用"

            results.append(
                ModuleCardData(
                    asset=a,
                    source_type=source_type,
                    source_label=source_label,
                    is_fallback=False,
                    source_kind="common",
                    default_badge=self.default_badge(a),
                )
            )

        return self._decorate_shared_cards(model_name, results)

    def get_scheme_modules(
        self, model_name: str, scheme_name: str, keyword: str = ""
    ) -> list[ModuleCardData]:
        """点击定制方案时，返回完整模块清单（含回源）。

        keyword 过滤与 ``_filter_assets`` / ``query_assets`` 一致（空格分词 + 多字段）。
        UI 层进入方案时应清空搜索框，使默认展示满树（Issue 19-A）。
        """
        # 1. 本方案定制模块（keyword 走统一分词，不再只扫 label/directory_name）
        custom_assets = self._filter_assets(
            category="custom",
            scheme_name=scheme_name,
            keyword=keyword,
        )
        # 回源覆盖判定必须基于「未按 keyword 缩小」的完整方案定制集，
        # 否则搜「手控」时会把未命中的主板当成「未覆盖」而错误回源一份通用主板。
        all_scheme_custom = self._filter_assets(
            category="custom", scheme_name=scheme_name
        )

        model_root = self._get_model_root(model_name)
        # 方案 platform：优先 方案配置.toml / discover_schemes；资产字段仅旧索引兼容回退
        platform_name = self._resolve_scheme_platform(
            model_name, scheme_name, all_scheme_custom
        )

        results: list[ModuleCardData] = []
        # 平台配置 defaults 的键是模块目录名（如 主板程序 / 3D机芯板程序）。
        # 磁盘与历史 toml 可能误写「机芯版」；_module_matches 统一为「机芯板」再比对。
        # 覆盖判定：标签/规范名相等，或键命中资产路径任一段。
        covered_assets = list(all_scheme_custom)

        for a in custom_assets:
            results.append(
                ModuleCardData(
                    asset=a,
                    source_type="custom_exclusive",
                    source_label="定制专属",
                    is_fallback=False,
                    source_kind="custom",
                )
            )

        # 2. 从 platform_config 中寻找缺失的通用模块（回源）
        # 平台按型号隔离；方案声明了 platform 则必须匹配配置块（A5）。
        # 回源作用域 = 本型号根内通用 + 本型号默认/A4 推断；禁止跨型号静默补。
        # B4b：已登记 shared_modules 的键（hit/missing 皆然）不得把保留本地副本当回源。
        model_platforms = self._platforms_for(model_name)
        if platform_name:
            relevant_platforms = [
                p for p in model_platforms if p.platform_name == platform_name
            ]
            # 方案声明了 platform 但 TOML 无对应块 → 通用回源失败（含禁止 A4）
            if not relevant_platforms:
                return results
        else:
            # 未声明 platform：退化用该型号全部配置块
            relevant_platforms = model_platforms

        if model_root:
            common_assets = self._filter_assets(category="common")
            model_common = [
                a for a in common_assets if self._belongs_to_model(a, model_name)
            ]
            shared_keys = {
                canonical_module_dir(ref.module_key)
                for ref in self.get_shared_modules(model_name)
                if canonical_module_dir(ref.module_key)
            }

            def _module_is_shared(module_key: str) -> bool:
                key = canonical_module_dir(module_key)
                return bool(key) and key in shared_keys

            def _append_fallback(fallback_asset: FirmwareAsset) -> None:
                if not self._asset_matches_keyword(fallback_asset, keyword):
                    return
                covered_assets.append(fallback_asset)
                results.append(
                    ModuleCardData(
                        asset=fallback_asset,
                        source_type="common_fallback",
                        source_label="通用",
                        is_fallback=True,
                        source_kind="common",
                        default_badge=self.default_badge(fallback_asset),
                    )
                )

            def _pick_fallback(
                module_key: str, default_dir: str
            ) -> FirmwareAsset | None:
                """按 defaults 值选回源变体，与 default_platforms_for 契约一致。

                - 非空：按 directory_name 精确匹配；
                - 空串 ``""``：仅当该模块通用区**唯一**变体时返回，多变体视为配置异常不猜。
                """
                candidates = [
                    ca for ca in model_common if _module_matches(ca, module_key)
                ]
                if not candidates:
                    return None
                if default_dir:
                    for ca in candidates:
                        if str(ca.get("directory_name", "")) == default_dir:
                            return ca
                    return None
                if len(candidates) == 1:
                    return candidates[0]
                return None

            for p in relevant_platforms:
                for module_key, default_dir in self._preferred_default_items(
                    p.defaults
                ):
                    if _module_is_shared(module_key):
                        continue
                    if any(_module_matches(a, module_key) for a in covered_assets):
                        continue
                    fallback_asset = _pick_fallback(module_key, str(default_dir or ""))
                    if fallback_asset:
                        _append_fallback(fallback_asset)

            # A4：配置无该模块键时，唯一通用变体可作回源。
            # 注意：方案已声明 platform 且块匹配失败时已在上方 return，不会走到这里。
            seen_module_keys: set[str] = set()
            for ca in model_common:
                mkey = self._module_key_for_asset(ca)
                if not mkey or mkey in seen_module_keys:
                    continue
                seen_module_keys.add(mkey)
                if _module_is_shared(mkey):
                    continue
                if any(_module_matches(a, mkey) for a in covered_assets):
                    continue
                if self._module_has_defaults_key(relevant_platforms, mkey):
                    continue
                peers = self._common_assets_matching_module(model_name, mkey)
                if len(peers) == 1:
                    _append_fallback(peers[0])

        return results

    def _scheme_discovery_roots(self, model_name: str) -> list[Path]:
        """用于 ``discover_schemes`` 的候选型号根（有序去重）。

        ``_get_model_root`` 可能落在 ``通用/``（scanner 的 model_directory_path），
        其下没有 ``定制/``，故还需扫描根、平台配置所在目录及其父级。
        """
        candidates: list[Path] = []
        cfg = self._platform_config_root(model_name)
        if cfg:
            candidates.append(Path(cfg))
        if not self._single_model_root and self.root_dir is not None:
            dir_name = self._multi_model_dirs.get(model_name, "")
            if dir_name:
                candidates.append(self.root_dir / dir_name)
        if self.root_dir is not None:
            candidates.append(self.root_dir)
        model_root = self._get_model_root(model_name)
        if model_root:
            p = Path(model_root)
            candidates.append(p)
            if p.name == "通用":
                candidates.append(p.parent)
        seen: set[str] = set()
        out: list[Path] = []
        for d in candidates:
            try:
                key = str(d.resolve()) if d.exists() else str(d)
            except OSError:
                key = str(d)
            if key in seen:
                continue
            seen.add(key)
            if d.is_dir():
                out.append(d)
        return out

    def _resolve_scheme_platform(
        self,
        model_name: str,
        scheme_name: str,
        scheme_custom_assets: list[FirmwareAsset],
    ) -> str:
        """解析方案所属 platform 名。

        优先 ``discover_schemes`` / 方案配置.toml（即使方案无任何定制固件资产）；
        资产上的 ``platform`` 字段仅作旧索引兼容回退。
        """
        want = str(scheme_name or "").strip()
        if want:
            for root in self._scheme_discovery_roots(model_name):
                for scheme in discover_schemes(root):
                    if scheme.name == want or scheme.path.name == want:
                        platform = str(scheme.platform or "").strip()
                        if platform:
                            return platform
        for a in scheme_custom_assets:
            pn = str(a.get("platform", "")).strip()
            if pn:
                return pn
        return ""

    def get_scheme_module_tree(
        self, model_name: str, scheme_name: str, keyword: str = ""
    ) -> list[ModuleRow]:
        """返回整机"模块固定层级"。

        把 get_scheme_modules 的扁平卡片按模块类型（firmware_label）归组：
        - 每个模块一行（ModuleRow），多变体（如手控UI 3 份）收在该行的 variants 下，
          不在顶层铺平。
        - 行级来源：只要该模块有任一"定制专属"变体即视为 custom，否则 common。
        - 用户文案只用「定制专属」/「通用」，绝不出现"回源"。
        - 行顺序按 STANDARD_MODULE_ORDER；不在标准列表里的模块（如接线图）排在最后。
        - 大部分机型包含 7 个标准模块，但非完整，缺失的模块不显示。
        """
        cards = self.get_scheme_modules(model_name, scheme_name, keyword)

        grouped: dict[str, list[ModuleVariant]] = {}
        for c in cards:
            label = str(c.asset.get("firmware_label", "")) or str(
                c.asset.get("firmware_type", "")
            )
            # source_kind is set by the producer (get_scheme_modules etc.). Fall
            # back to the legacy is_fallback inference for safety.
            kind = c.source_kind or ("common" if c.is_fallback else "custom")
            # 卡片层 source_label 已是用户文案；树行仍统一 定制专属/通用
            display = (
                c.source_label
                if c.source_label and "回源" not in c.source_label
                else ("通用" if kind == "common" else "定制专属")
            )
            variant = ModuleVariant(
                asset=c.asset,
                name=str(c.asset.get("directory_name", "")),
                version=str(c.asset.get("version", "")),
                source_kind=kind,
                source_label=display,
                default_badge=c.default_badge,
            )
            grouped.setdefault(label, []).append(variant)

        def _order_index(label: str) -> int:
            return (
                STANDARD_MODULE_ORDER.index(label)
                if label in STANDARD_MODULE_ORDER
                else len(STANDARD_MODULE_ORDER)
            )

        rows: list[ModuleRow] = []
        for label in sorted(grouped, key=lambda key: (_order_index(key), key)):
            variants = grouped[label]
            row_kind = (
                "custom"
                if any(v.source_kind == "custom" for v in variants)
                else "common"
            )
            rows.append(
                ModuleRow(
                    label=label,
                    source_kind=row_kind,
                    source_label="定制专属" if row_kind == "custom" else "通用",
                    variants=variants,
                )
            )
        return rows

    def get_all_modules(
        self, model_name: str, keyword: str = ""
    ) -> list[ModuleCardData]:
        """获取指定型号下的所有模块（不回源，仅展示物理存在的模块）。

        归属文案：``通用`` 或 ``定制专属 · {scheme_name}``。
        程序名称由 UI 使用 ``directory_name`` 展示，本方法不折叠。
        """
        assets = self._filter_assets(keyword=keyword)
        results: list[ModuleCardData] = []
        for a in assets:
            if not self._belongs_to_model(a, model_name):
                continue
            cat = str(a.get("category", ""))
            dir_name = str(a.get("directory_name", ""))

            if cat == "common":
                is_default = "_默认" in dir_name
                source_type = "common_default" if is_default else "common_variant"
                source_label = "通用"
            elif cat == "custom":
                scheme = str(a.get("scheme_name", "")).strip()
                source_type = "custom_exclusive"
                source_label = f"定制专属 · {scheme}" if scheme else "定制专属"
            else:
                source_type = "unknown"
                source_label = "未知来源"

            results.append(
                ModuleCardData(
                    asset=a,
                    source_type=source_type,
                    source_label=source_label,
                    is_fallback=False,
                    source_kind="common" if cat == "common" else "custom",
                    default_badge=self.default_badge(a) if cat == "common" else "",
                )
            )
        return self._decorate_shared_cards(model_name, results)
