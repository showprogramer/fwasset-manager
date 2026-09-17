from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, cast

from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    contained_subpath,
    is_same_or_under,
    normalize_workspace_path,
    same_path_identity,
)
from fwasset.core.settings import ASSET_INDEX_PATH
from fwasset.core.sort_config import SortKey, apply_sort
from fwasset.core.types import ChassisType, FirmwareAsset

SCHEMA_VERSION = 4
HiddenItemType = Literal["model_directory", "firmware_type", "asset"]

# FirmwareAsset 全键集合：行级写入前校验调用方传入完整资产（TypedDict 在
# 运行时不校验，缺键会被 _asset_to_row 静默填空值，必须在入口拦住）。
FIRMWARE_ASSET_KEYS: frozenset[str] = frozenset(FirmwareAsset.__annotations__)

# 行级写与全量替换共用的列清单（保持与 assets 表结构一致）。
_ASSET_COLUMNS = (
    "path, series, model, model_directory_name, model_directory_path, "
    "firmware_type, firmware_label, flash_mode, usb_flow, version, directory_name, "
    "files_json, modified_time, scanned_at, tool_name, tool_path, tool_dir, label, "
    "category, platform, scheme_name, scheme_path, chassis_type, vendor"
)
_ASSET_BINDINGS = (
    ":path, :series, :model, :model_directory_name, :model_directory_path, "
    ":firmware_type, :firmware_label, :flash_mode, :usb_flow, :version, :directory_name, "
    ":files_json, :modified_time, :scanned_at, :tool_name, :tool_path, :tool_dir, :label, "
    ":category, :platform, :scheme_name, :scheme_path, :chassis_type, :vendor"
)
_INSERT_ASSET_SQL = (
    f"INSERT INTO assets ({_ASSET_COLUMNS}) VALUES ({_ASSET_BINDINGS})"
)

# 连接 busy 等待秒数。扫描写库（DELETE+INSERT）可能较长；默认 5s 易在
# 读路径（query/缓存加载）上提前抛 OperationalError。
CONNECT_TIMEOUT_SEC = 30.0

# ---------------------------------------------------------------------------
# 单工作区语义（非多根并存）
#
# 本索引一次只服务一个固件根目录（工作区）。真相源是整理后的目录树 + TOML；
# SQLite 是搜索/浏览缓存。全量扫描会用当前根的快照整库替换 assets，
# scan_meta 也只保留当前根一行。换根扫描 = 切换工作区，旧根资产不保留。
#
# 并发模型（单写者）：
# - 预期：同一库文件由本应用单进程使用；写主要在扫描（及未来 CRUD）线程，
#   读在 UI 查询路径。勿多开多个 fwasset 实例同时写同一 fwasset.db。
# - 连接 timeout 只缓解短暂锁等待，不替代单写者纪律。
#
# 未来应用内 CRUD 落地后，日常走行级写；全量 save_assets 退化为
# 首次导入 / 索引修复 / 强制对账的冷路径，语义仍是「当前工作区快照」。
# ---------------------------------------------------------------------------


class AssetIndexError(RuntimeError):
    """Raised when the local SQLite index cannot be opened or migrated."""


def default_index_path() -> Path:
    return ASSET_INDEX_PATH


def _db_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else default_index_path()


def connect_asset_index(path: str | Path | None = None) -> sqlite3.Connection:
    """打开本地资产索引连接。

    使用 :data:`CONNECT_TIMEOUT_SEC` 作为 busy timeout，减轻扫描写事务与
    并发读之间的短暂争用。仍假定单进程单写者（见模块顶部说明）。
    """
    db_path = _db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT_SEC)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"本地资产索引无法打开，请重新扫描生成：{exc}") from exc


def init_asset_index(path: str | Path | None = None) -> None:
    try:
        with connect_asset_index(path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    path TEXT PRIMARY KEY,
                    series TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_directory_name TEXT NOT NULL,
                    model_directory_path TEXT NOT NULL,
                    firmware_type TEXT NOT NULL,
                    firmware_label TEXT NOT NULL,
                    flash_mode TEXT NOT NULL,
                    usb_flow TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL,
                    directory_name TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    modified_time REAL NOT NULL,
                    scanned_at REAL NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_path TEXT NOT NULL,
                    tool_dir TEXT NOT NULL,
                    label TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    scheme_name TEXT NOT NULL DEFAULT '',
                    scheme_path TEXT NOT NULL DEFAULT '',
                    chassis_type TEXT NOT NULL DEFAULT '',
                    vendor TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(firmware_type);
                CREATE INDEX IF NOT EXISTS idx_assets_model ON assets(model);
                CREATE INDEX IF NOT EXISTS idx_assets_directory ON assets(model_directory_path);
                CREATE TABLE IF NOT EXISTS hidden_items (
                    path TEXT PRIMARY KEY,
                    hide_type TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_meta (
                    root_dir TEXT PRIMARY KEY,
                    last_scan_at REAL NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                """
            )
            current = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_platform ON assets(platform)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_scheme ON assets(scheme_name)"
                )
                return
            current_version = int(current["value"])
            if current_version == 1:
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN usb_flow TEXT NOT NULL DEFAULT ''"
                )
                current_version = 2
                conn.execute(
                    "UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'"
                )
            if current_version == 2:
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN platform TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN scheme_name TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "ALTER TABLE assets ADD COLUMN scheme_path TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_platform ON assets(platform)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_scheme ON assets(scheme_name)"
                )
                current_version = 3
                conn.execute(
                    "UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'"
                )
            if current_version != SCHEMA_VERSION:
                raise AssetIndexError(
                    f"本地资产索引版本不兼容：当前 {current['value']}，需要 {SCHEMA_VERSION}。请重新扫描生成。"
                )
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"本地资产索引损坏，请重新扫描生成：{exc}") from exc


def schema_version(path: str | Path | None = None) -> int:
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"])


def _rebuild_incompatible_index(path: str | Path | None) -> None:
    # 版本不兼容的全量重扫：真源是目录树 + TOML，缓存可整体重建。
    # 只重建 assets / scan_meta / schema_meta；hidden_items 是用户隐藏状态，原样保留。
    with connect_asset_index(path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS assets;
            DROP TABLE IF EXISTS scan_meta;
            DROP TABLE IF EXISTS schema_meta;
            """
        )
    init_asset_index(path)


def save_assets(
    assets: Iterable[FirmwareAsset],
    root_dir: str,
    path: str | Path | None = None,
    scanned_at: float | None = None,
) -> None:
    """用当前工作区根目录的全量快照重建索引（单根语义）。

    - ``DELETE FROM assets`` 后写入 ``assets``：整库替换，不是按根增量合并。
    - ``scan_meta`` 先清空再写入当前 ``root_dir`` 一行：不保留历史根，避免
      「多行 scan_meta + 单表 assets」被误读成多根并存。
    - 换根再次扫描会丢弃上一工作区的资产与 meta，这是切换工作区，不是 bug。
    """
    try:
        init_asset_index(path)
    except AssetIndexError:
        _rebuild_incompatible_index(path)
    now = float(scanned_at if scanned_at is not None else time.time())
    asset_rows = [_asset_to_row(asset, now) for asset in assets]
    with connect_asset_index(path) as conn:
        # 单工作区：当前根的快照覆盖整库（见模块顶部注释）。
        conn.execute("DELETE FROM assets")
        conn.executemany(_INSERT_ASSET_SQL, asset_rows)
        # 只保留当前工作区一行，清掉历史 root_dir（旧库可能多行）。
        conn.execute("DELETE FROM scan_meta")
        conn.execute(
            """
            INSERT INTO scan_meta(root_dir, last_scan_at, schema_version)
            VALUES(?, ?, ?)
            """,
            (str(root_dir), now, SCHEMA_VERSION),
        )
        prune_missing_hidden_items(conn=conn)


def load_assets(path: str | Path | None = None) -> list[FirmwareAsset]:
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        rows = conn.execute(
            "SELECT * FROM assets ORDER BY path, firmware_type"
        ).fetchall()
        return [_row_to_asset(row) for row in rows]


def count_assets(path: str | Path | None = None) -> int:
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM assets").fetchone()
        return int(row["count"])


def query_assets(
    keyword: str = "",
    firmware_types: Iterable[str] | None = None,
    path: str | Path | None = None,
    sort_key: SortKey | str = SortKey.PATH,
    ascending: bool = True,
    category: str = "",
    scheme_name: str = "",
    platform: str = "",
) -> list[FirmwareAsset]:
    init_asset_index(path)
    clauses: list[str] = []
    params: list[str] = []
    selected_types = [str(item) for item in (firmware_types or []) if str(item).strip()]
    if selected_types:
        placeholders = ", ".join("?" for _ in selected_types)
        clauses.append(f"firmware_type IN ({placeholders})")
        params.extend(selected_types)
    if category.strip():
        clauses.append("category = ?")
        params.append(category.strip())
    if platform.strip():
        clauses.append("platform = ?")
        params.append(platform.strip())
    if scheme_name.strip():
        clauses.append("lower(scheme_name) LIKE ?")
        params.append(f"%{scheme_name.strip().lower()}%")
    # 空格分词：每个词都要命中（任意字段 OR），词间 AND。
    # 这样 "手控 V13" = (任意字段含"手控") AND (任意字段含"V13")，
    # 解决"既想搜模块、又想搜版本号"。单个词时退化为原来的跨字段模糊。
    keyword_fields = (
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
    for token in keyword.split():
        pattern = f"%{token.lower()}%"
        ors = " OR ".join(f"lower({field}) LIKE ?" for field in keyword_fields)
        clauses.append(f"({ors})")
        params.extend([pattern] * len(keyword_fields))
    sql = "SELECT * FROM assets"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY path, firmware_type"
    with connect_asset_index(path) as conn:
        assets = [_row_to_asset(row) for row in conn.execute(sql, params).fetchall()]
    try:
        selected_sort_key = SortKey(sort_key)
    except ValueError:
        selected_sort_key = SortKey.PATH
    return apply_sort(assets, sort_key=selected_sort_key, ascending=ascending)


def delete_missing_assets(
    existing_paths: Iterable[str], path: str | Path | None = None
) -> int:
    init_asset_index(path)
    keep = {str(item) for item in existing_paths}
    with connect_asset_index(path) as conn:
        rows = conn.execute("SELECT path FROM assets").fetchall()
        removed = 0
        for row in rows:
            asset_path = str(row["path"])
            if asset_path in keep:
                continue
            conn.execute("DELETE FROM assets WHERE path = ?", (asset_path,))
            removed += 1
        prune_missing_hidden_items(conn=conn)
        return removed


# ---------------------------------------------------------------------------
# 行级写 API（REVIEW-20260728 R3 / gate #3）
#
# 供未来 CRUD 动作使用的定点写能力。纪律：
# - SQLite 事务不包含文件系统动作；主行写与 hidden_items 同步同事务。
# - 不引入 UUID asset ID：保留 path 主键，路径变化必须整行替换
#   （replace_asset），禁止调用方只 UPDATE path。
# - bulk/reconcile 强校验「库内 scan_meta 根 == 传入 workspace_root」，
#   防止用工作区 B 的参数改工作区 A 的数据库。
# ---------------------------------------------------------------------------


def _validate_asset_payload(asset: FirmwareAsset) -> None:
    """行级写入前的运行时校验：全键存在、path 非空且为绝对路径。"""
    missing = sorted(FIRMWARE_ASSET_KEYS - set(asset.keys()))
    if missing:
        raise AssetIndexError(f"资产缺少必需字段，已拒绝写入：{'、'.join(missing)}")
    asset_path = str(asset.get("path") or "").strip()
    if not asset_path:
        raise AssetIndexError("资产 path 不能为空，已拒绝写入")
    if not Path(asset_path).is_absolute():
        raise AssetIndexError(f"资产 path 必须为绝对路径，已拒绝写入：{asset_path}")


def _find_equivalent_asset_path(conn: sqlite3.Connection, target: str) -> str | None:
    """定位与 ``target`` 物理等价的现存资产 path 主键。

    先精确匹配，再按统一身份判定匹配（R8 起复用 ``path_guard.same_path_identity``：
    Windows 大小写 / 分隔符 / junction 折叠，防止同一路径两种写法产生重复行或漏删目标）。
    """
    row = conn.execute(
        "SELECT path FROM assets WHERE path = ? LIMIT 1", (target,)
    ).fetchone()
    if row is not None:
        return str(row["path"])
    for row in conn.execute("SELECT path FROM assets").fetchall():
        stored = str(row["path"])
        if same_path_identity(stored, target):
            return stored
    return None


def _within_boundary(stored: str, boundary: str) -> bool:
    """存量路径是否位于边界之内：词法（normcase）或 resolved 归属任一命中。"""
    if is_same_or_under(stored, boundary):
        return True
    return contained_subpath(stored, boundary) is not None


def _affected_by_boundary(stored: str, boundary: str) -> bool:
    """隐藏项是否受子树边界影响：子树内（后代）或子树祖先（边界在其之下）。

    祖先方向用于「对账叶子子树清空后，位于父型号/类型目录的隐藏项失活」；
    兄弟边界的隐藏项不受影响，由 :func:`_hidden_item_exists` 判活决定。
    两个方向都用词法（normcase）+ resolved 双重归属，junction/真实路径混用
    也能命中。
    """
    return _within_boundary(stored, boundary) or _within_boundary(boundary, stored)


def _has_asset_column_identity(
    conn: sqlite3.Connection, column: str, item_path: str
) -> bool:
    """assets 表 ``column`` 列是否存在与 ``item_path`` 物理等价的值。

    精确匹配优先，其次 normcase+resolve 归一身份（junction / 大小写 /
    分隔符变体视为同一路径）。``column`` 仅接受内部常量，无注入面。
    """
    row = conn.execute(
        f"SELECT 1 FROM assets WHERE {column} = ? LIMIT 1", (item_path,)
    ).fetchone()
    if row is not None:
        return True
    item_norm = normalize_workspace_path(item_path)
    if not item_norm:
        return False
    rows = conn.execute(f"SELECT {column} AS val FROM assets").fetchall()
    return any(
        normalize_workspace_path(str(r["val"])) == item_norm for r in rows
    )


def _require_index_workspace(conn: sqlite3.Connection, workspace_root: str) -> None:
    """断言库内 scan_meta 根与传入工作区根一致（单工作区语义）。"""
    meta_row = conn.execute(
        "SELECT root_dir FROM scan_meta ORDER BY last_scan_at DESC LIMIT 1"
    ).fetchone()
    db_root = str(meta_row["root_dir"]).strip() if meta_row is not None else ""
    if not db_root or normalize_workspace_path(db_root) != normalize_workspace_path(
        workspace_root
    ):
        raise AssetIndexError(
            "索引工作区与目标工作区不一致（或无扫描记录），拒绝子树写入；请先重新扫描"
        )


def upsert_asset(
    asset: FirmwareAsset,
    *,
    path: str | Path | None = None,
    scanned_at: float | None = None,
) -> None:
    """新增/覆盖单行（新建、编辑后重扫单目录）。

    同一路径的大小写/分隔符变体视为同一行（normcase 身份），不会产生重复行。
    """
    _validate_asset_payload(asset)
    now = float(scanned_at if scanned_at is not None else time.time())
    row = _asset_to_row(asset, now)
    init_asset_index(path)
    conn = connect_asset_index(path)
    try:
        with conn:
            existing = _find_equivalent_asset_path(conn, str(asset["path"]))
            if existing is not None:
                conn.execute("DELETE FROM assets WHERE path = ?", (existing,))
            conn.execute(_INSERT_ASSET_SQL, row)
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"资产写入失败，可重扫该子树恢复：{exc}") from exc
    finally:
        conn.close()


def replace_asset(
    old_asset_path: str,
    new_asset: FirmwareAsset,
    *,
    path: str | Path | None = None,
    scanned_at: float | None = None,
) -> None:
    """单事务整行替换：删旧行 → 写完整新行 → 精确迁移 asset 级隐藏行。

    路径变化时 ``directory_name``/``version``/``label`` 等字段都会变，调用方
    必须传入保留工作区上下文重扫生成的完整资产，不允许只 UPDATE ``path``。
    旧行不存在或新路径被其他行占用时抛 :class:`AssetIndexError`。
    """
    old_path = str(old_asset_path).strip()
    if not old_path:
        raise AssetIndexError("旧资产路径不能为空，已拒绝替换")
    _validate_asset_payload(new_asset)
    new_path = str(new_asset["path"])
    now = float(scanned_at if scanned_at is not None else time.time())
    row = _asset_to_row(new_asset, now)
    init_asset_index(path)
    conn = connect_asset_index(path)
    try:
        with conn:
            # 身份按 normcase 归一：大小写/分隔符变体视为同一资产
            old_key = _find_equivalent_asset_path(conn, old_path)
            if old_key is None:
                raise AssetIndexError(f"旧资产不存在，无法替换：{old_path}")
            new_key = _find_equivalent_asset_path(conn, new_path)
            if new_key is not None and new_key != old_key:
                raise AssetIndexError(f"新路径已被其他资产占用，无法替换：{new_path}")
            # 精确 + 归一匹配的 asset 级隐藏行迁移到新路径
            old_norm = normalize_workspace_path(old_key)
            hidden_rows = conn.execute(
                "SELECT path FROM hidden_items WHERE hide_type = 'asset'"
            ).fetchall()
            for hidden_row in hidden_rows:
                hidden_path = str(hidden_row["path"])
                if hidden_path == old_key or (
                    old_norm
                    and normalize_workspace_path(hidden_path) == old_norm
                ):
                    conn.execute(
                        "UPDATE hidden_items SET path = ? WHERE path = ?",
                        (new_path, hidden_path),
                    )
            conn.execute("DELETE FROM assets WHERE path = ?", (old_key,))
            conn.execute(_INSERT_ASSET_SQL, row)
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"资产替换失败，可重扫该子树恢复：{exc}") from exc
    finally:
        conn.close()


def delete_asset(
    asset_path: str, *, path: str | Path | None = None
) -> bool:
    """删单行 + 关联 asset 级隐藏行；型号/类型级隐藏项按边界清理。

    行不存在时返回 ``False``（幂等），不抛错。
    """
    target = str(asset_path).strip()
    if not target:
        raise AssetIndexError("资产路径不能为空，已拒绝删除")
    init_asset_index(path)
    conn = connect_asset_index(path)
    try:
        with conn:
            # 身份按 normcase 归一：大小写/分隔符变体视为同一资产
            key = _find_equivalent_asset_path(conn, target)
            if key is None:
                return False
            exists = conn.execute(
                "SELECT model_directory_path FROM assets WHERE path = ? LIMIT 1",
                (key,),
            ).fetchone()
            model_dir = str(exists["model_directory_path"])
            conn.execute("DELETE FROM assets WHERE path = ?", (key,))
            # 精确 + 归一匹配的 asset 级隐藏行一并删除
            hidden_rows = conn.execute(
                "SELECT path FROM hidden_items WHERE hide_type = 'asset'"
            ).fetchall()
            key_norm = normalize_workspace_path(key)
            for hidden_row in hidden_rows:
                hidden_path = str(hidden_row["path"])
                if hidden_path == key or (
                    key_norm and normalize_workspace_path(hidden_path) == key_norm
                ):
                    conn.execute(
                        "DELETE FROM hidden_items WHERE path = ?", (hidden_path,)
                    )
            # 只清理受该资产影响的型号/类型级隐藏项：型号目录与资产的
            # model_directory_path 物理等价，或类型路径边界包含被删资产；
            # 不做全局清理。
            model_dir_norm = normalize_workspace_path(model_dir)
            hidden_rows = conn.execute(
                "SELECT path, hide_type FROM hidden_items"
            ).fetchall()
            for row in hidden_rows:
                item_path = str(row["path"])
                hide_type = str(row["hide_type"])
                if hide_type == "asset":
                    continue
                if hide_type == "model_directory":
                    same_model = item_path == model_dir or (
                        model_dir_norm
                        and normalize_workspace_path(item_path) == model_dir_norm
                    )
                    if not same_model:
                        continue
                if hide_type == "firmware_type" and not _within_boundary(
                    key, item_path
                ):
                    continue
                if not _hidden_item_exists(conn, item_path, hide_type):
                    conn.execute(
                        "DELETE FROM hidden_items WHERE path = ?", (item_path,)
                    )
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"资产删除失败，可重扫该子树恢复：{exc}") from exc
    finally:
        conn.close()
    return True


def bulk_reindex_subtree(
    workspace_root: str,
    subtree_root: str,
    assets: list[FirmwareAsset],
    *,
    path: str | Path | None = None,
    scanned_at: float | None = None,
) -> None:
    """子树整批重建：替换 ``subtree_root`` 边界内的全部资产行（R3）。

    - 同时保留「工作区根」与「遍历子树」两个参数；库内 scan_meta 根必须与
      ``workspace_root`` 一致，否则拒绝且不改库（单工作区语义）。
    - 整批原子：写入前一次性校验全部资产（全键、绝对路径、均位于子树内、
      路径无重复），任一失败整批拒绝。
    - 隐藏项只处理子树边界内的行：失活依据消失的清理，边界外一律保留。
    - ``assets`` 传空列表表示该子树已清空（删除恢复场景），只删不插。
    """
    ws_norm = normalize_workspace_path(workspace_root)
    if not ws_norm:
        raise AssetIndexError("工作区根目录未配置，拒绝子树重建")
    try:
        subtree_resolved = assert_within_workspace(subtree_root, workspace_root)
    except PathGuardError as exc:
        raise AssetIndexError(f"子树不在工作区内，已拒绝重建：{exc}") from exc
    boundary = str(subtree_resolved)

    now = float(scanned_at if scanned_at is not None else time.time())
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for asset in assets:
        _validate_asset_payload(asset)
        asset_path = str(asset["path"])
        if contained_subpath(asset_path, boundary) is None:
            raise AssetIndexError(
                f"资产不在子树内，已拒绝整批重建：{asset_path}（子树：{boundary}）"
            )
        dup_key = normalize_workspace_path(asset_path)
        if dup_key in seen:
            raise AssetIndexError(f"批量资产路径重复，已拒绝整批重建：{asset_path}")
        seen.add(dup_key)
        rows.append(_asset_to_row(asset, now))

    init_asset_index(path)
    conn = connect_asset_index(path)
    try:
        with conn:
            _require_index_workspace(conn, workspace_root)
            stale_paths = [
                str(row["path"])
                for row in conn.execute("SELECT path FROM assets").fetchall()
                if _within_boundary(str(row["path"]), boundary)
            ]
            conn.executemany(
                "DELETE FROM assets WHERE path = ?", [(p,) for p in stale_paths]
            )
            conn.executemany(_INSERT_ASSET_SQL, rows)
            _prune_hidden_items(conn, boundary=boundary)
    except sqlite3.DatabaseError as exc:
        raise AssetIndexError(f"子树重建失败，可重扫该子树恢复：{exc}") from exc
    finally:
        conn.close()


def hide_item(
    item_path: str,
    hide_type: HiddenItemType = "asset",
    path: str | Path | None = None,
    created_at: float | None = None,
) -> None:
    init_asset_index(path)
    now = float(created_at if created_at is not None else time.time())
    with connect_asset_index(path) as conn:
        conn.execute(
            """
            INSERT INTO hidden_items(path, hide_type, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hide_type = excluded.hide_type,
                created_at = excluded.created_at
            """,
            (str(item_path), hide_type, now),
        )


def unhide_item(item_path: str, path: str | Path | None = None) -> None:
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        conn.execute("DELETE FROM hidden_items WHERE path = ?", (str(item_path),))


def load_hidden_items(path: str | Path | None = None) -> dict[str, str]:
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        rows = conn.execute(
            "SELECT path, hide_type FROM hidden_items ORDER BY path"
        ).fetchall()
        return {str(row["path"]): str(row["hide_type"]) for row in rows}


def _hidden_item_exists(
    conn: sqlite3.Connection, item_path: str, hide_type: str
) -> bool:
    """判断隐藏项是否仍有存活依据（R3：路径边界 + 归一身份，不裸 ``startswith``）。

    - ``asset``：与资产 ``path`` 物理等价（精确或 normcase/resolved 身份）；
    - ``model_directory``：与资产 ``model_directory_path`` 物理等价；
    - ``firmware_type``：任一资产路径在该隐藏路径边界之内（词法 +
      resolved 双重归属，避免 ``...\\A`` 与 ``...\\AB`` 前缀混淆）。
    """
    if hide_type == "asset":
        return _has_asset_column_identity(conn, "path", item_path)
    if hide_type == "model_directory":
        return _has_asset_column_identity(conn, "model_directory_path", item_path)
    asset_paths = [
        str(row["path"]) for row in conn.execute("SELECT path FROM assets").fetchall()
    ]
    return any(_within_boundary(asset_path, item_path) for asset_path in asset_paths)


def _prune_hidden_items(
    conn: sqlite3.Connection, boundary: str | None = None
) -> int:
    """按存活依据清理隐藏项；``boundary`` 给定时只处理受该边界影响的行
    （子树内 + 子树祖先），边界外的兄弟隐藏行一律保留。"""
    hidden_rows = conn.execute(
        "SELECT path, hide_type FROM hidden_items"
    ).fetchall()
    removed = 0
    for row in hidden_rows:
        item_path = str(row["path"])
        hide_type = str(row["hide_type"])
        if boundary is not None and not _affected_by_boundary(item_path, boundary):
            continue
        if _hidden_item_exists(conn, item_path, hide_type):
            continue
        conn.execute("DELETE FROM hidden_items WHERE path = ?", (item_path,))
        removed += 1
    return removed


def prune_missing_hidden_items(
    path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    close_conn = False
    if conn is None:
        init_asset_index(path)
        conn = connect_asset_index(path)
        close_conn = True
    try:
        removed = _prune_hidden_items(conn)
        if close_conn:
            conn.commit()
        return removed
    finally:
        if close_conn:
            conn.close()


def load_scan_meta(
    path: str | Path | None = None,
) -> list[dict[str, float | int | str]]:
    """返回工作区扫描元数据。

    正常经 :func:`save_assets` 写入后至多一行（当前工作区根）。
    列表形式保留兼容；调用方取 ``[0]`` 即当前根。
    未清理的旧库可能短暂多行，按 ``last_scan_at DESC`` 排序，首条为最近一次。
    """
    init_asset_index(path)
    with connect_asset_index(path) as conn:
        rows = conn.execute(
            "SELECT root_dir, last_scan_at, schema_version FROM scan_meta ORDER BY last_scan_at DESC"
        ).fetchall()
        return [
            {
                "root_dir": str(row["root_dir"]),
                "last_scan_at": float(row["last_scan_at"]),
                "schema_version": int(row["schema_version"]),
            }
            for row in rows
        ]


def active_workspace_root(path: str | Path | None = None) -> str | None:
    """当前工作区根目录；无扫描记录时返回 ``None``。"""
    meta = load_scan_meta(path)
    if not meta:
        return None
    root = str(meta[0].get("root_dir", "")).strip()
    return root or None


def _asset_to_row(asset: FirmwareAsset, scanned_at: float) -> dict[str, object]:
    return {
        "path": str(asset.get("path", "")),
        "series": str(asset.get("series", "")),
        "model": str(asset.get("model", "")),
        "model_directory_name": str(asset.get("model_directory_name", "")),
        "model_directory_path": str(asset.get("model_directory_path", "")),
        "firmware_type": str(asset.get("firmware_type", "")),
        "firmware_label": str(asset.get("firmware_label", "")),
        "flash_mode": str(asset.get("flash_mode", "")),
        "usb_flow": str(asset.get("usb_flow", "")),
        "version": str(asset.get("version", "")),
        "directory_name": str(asset.get("directory_name", "")),
        "files_json": json.dumps(list(asset.get("files", [])), ensure_ascii=False),
        "modified_time": float(asset.get("modified_time", 0) or 0),
        "scanned_at": scanned_at,
        "tool_name": str(asset.get("tool_name", "")),
        "tool_path": str(asset.get("tool_path", "")),
        "tool_dir": str(asset.get("tool_dir", "")),
        "label": str(asset.get("label", "")),
        "category": str(asset.get("category", "")),
        "platform": str(asset.get("platform", "")),
        "scheme_name": str(asset.get("scheme_name", "")),
        "scheme_path": str(asset.get("scheme_path", "")),
        "chassis_type": str(asset.get("chassis_type", "")),
        "vendor": str(asset.get("vendor", "")),
    }


def _row_to_asset(row: sqlite3.Row) -> FirmwareAsset:
    try:
        files = json.loads(str(row["files_json"]))
    except json.JSONDecodeError:
        files = []
    if not isinstance(files, list):
        files = []
    keys = row.keys()
    return {
        "series": str(row["series"]),
        "firmware_type": str(row["firmware_type"]),  # type: ignore[typeddict-item]
        "firmware_label": str(row["firmware_label"]),
        "flash_mode": str(row["flash_mode"]),  # type: ignore[typeddict-item]
        "usb_flow": str(row["usb_flow"]),  # type: ignore[typeddict-item]
        "model": str(row["model"]),
        "version": str(row["version"]),
        "model_directory_name": str(row["model_directory_name"]),
        "model_directory_path": str(row["model_directory_path"]),
        "path": str(row["path"]),
        "directory_name": str(row["directory_name"]),
        "files": [str(item) for item in files],
        "modified_time": float(row["modified_time"]),
        "tool_name": str(row["tool_name"]),
        "tool_path": str(row["tool_path"]),
        "tool_dir": str(row["tool_dir"]),
        "label": str(row["label"]),
        "category": cast(
            Literal["common", "custom", ""],
            str(row["category"]) if "category" in keys else "",
        ),
        "platform": str(row["platform"]) if "platform" in keys else "",
        "scheme_name": str(row["scheme_name"]) if "scheme_name" in keys else "",
        "scheme_path": str(row["scheme_path"]) if "scheme_path" in keys else "",
        # chassis_type / vendor（D0.1 / D6，schema v4 起进索引列）直接读列。
        "chassis_type": cast(
            "ChassisType | Literal['']",
            str(row["chassis_type"]),
        ),
        "vendor": str(row["vendor"]),
    }
