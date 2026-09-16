from pathlib import Path

import pytest

from fwasset.core.asset_index import (
    CONNECT_TIMEOUT_SEC,
    SCHEMA_VERSION,
    AssetIndexError,
    active_workspace_root,
    connect_asset_index,
    count_assets,
    delete_missing_assets,
    hide_item,
    init_asset_index,
    load_assets,
    load_hidden_items,
    load_scan_meta,
    prune_missing_hidden_items,
    query_assets,
    save_assets,
    schema_version,
    unhide_item,
)
from fwasset.core.sort_config import SortKey
from fwasset.core.types import FirmwareAsset


def make_asset(
    base: Path,
    *,
    firmware_type: str = "mainboard",
    model: str = "L36",
    directory_name: str = "mainboard_v1",
    category: str = "",
    platform: str = "",
    scheme_name: str = "",
    scheme_path: str = "",
) -> FirmwareAsset:
    model_dir = base / f"{model} test"
    asset_dir = model_dir / directory_name
    return {
        "series": "L36",
        "firmware_type": firmware_type,  # type: ignore[typeddict-item]
        "firmware_label": "主板程序" if firmware_type == "mainboard" else "手控UI",
        "flash_mode": "tool_launch",
        "usb_flow": "",
        "model": model,
        "version": "V1.0.0",
        "model_directory_name": model_dir.name,
        "model_directory_path": str(model_dir),
        "path": str(asset_dir),
        "directory_name": directory_name,
        "files": ["firmware.bin"],
        "modified_time": 123.0,
        "tool_name": "writer",
        "tool_path": "D:/tools/writer.exe",
        "tool_dir": "writer",
        "label": f"{model} V1.0.0 [{directory_name}]",
        "category": category,
        "platform": platform,
        "scheme_name": scheme_name,
        "scheme_path": scheme_path,
        "chassis_type": "",
    }


def test_init_asset_index_creates_schema_and_version(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"

    init_asset_index(db_path)

    assert db_path.exists()
    assert schema_version(db_path) == SCHEMA_VERSION


def test_connect_asset_index_uses_extended_busy_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 18：连接须使用 CONNECT_TIMEOUT_SEC，而非 SQLite 默认 5s。"""
    import sqlite3

    import fwasset.core.asset_index as mod

    captured: dict[str, float] = {}
    real_connect = sqlite3.connect

    def wrap(database, timeout=5.0, **kwargs):
        captured["timeout"] = float(timeout)
        return real_connect(database, timeout=timeout, **kwargs)

    monkeypatch.setattr(mod.sqlite3, "connect", wrap)
    conn = connect_asset_index(tmp_path / "timeout.db")
    try:
        assert captured["timeout"] == CONNECT_TIMEOUT_SEC
        assert CONNECT_TIMEOUT_SEC >= 30.0
    finally:
        conn.close()


def test_save_and_load_assets_roundtrip(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    asset = make_asset(tmp_path)

    save_assets([asset], str(tmp_path), db_path, scanned_at=1000.0)

    loaded = load_assets(db_path)
    assert loaded == [asset]
    assert count_assets(db_path) == 1
    assert load_scan_meta(db_path) == [
        {
            "root_dir": str(tmp_path),
            "last_scan_at": 1000.0,
            "schema_version": SCHEMA_VERSION,
        }
    ]
    assert active_workspace_root(db_path) == str(tmp_path)


def test_save_assets_replaces_index_when_workspace_root_changes(tmp_path: Path):
    """单工作区：换根扫描整库替换，不保留上一根资产，scan_meta 仅一行。"""
    db_path = tmp_path / "fwasset.db"
    root_a = tmp_path / "workspace_a"
    root_b = tmp_path / "workspace_b"
    root_a.mkdir()
    root_b.mkdir()

    asset_a = make_asset(root_a, model="LA", directory_name="mod_a")
    asset_b = make_asset(root_b, model="LB", directory_name="mod_b")

    save_assets([asset_a], str(root_a), db_path, scanned_at=1000.0)
    assert [item["model"] for item in load_assets(db_path)] == ["LA"]
    assert load_scan_meta(db_path) == [
        {
            "root_dir": str(root_a),
            "last_scan_at": 1000.0,
            "schema_version": SCHEMA_VERSION,
        }
    ]

    # 切换工作区：B 覆盖 A（intentional，非多根合并）
    save_assets([asset_b], str(root_b), db_path, scanned_at=2000.0)

    loaded = load_assets(db_path)
    assert [item["model"] for item in loaded] == ["LB"]
    assert asset_a["path"] not in {item["path"] for item in loaded}
    assert load_scan_meta(db_path) == [
        {
            "root_dir": str(root_b),
            "last_scan_at": 2000.0,
            "schema_version": SCHEMA_VERSION,
        }
    ]
    assert active_workspace_root(db_path) == str(root_b)


def test_save_assets_clears_stale_scan_meta_rows(tmp_path: Path):
    """旧库若残留多行 scan_meta，再次 save 后应收口为当前根一行。"""
    import sqlite3

    db_path = tmp_path / "fwasset.db"
    asset = make_asset(tmp_path, model="L36", directory_name="only")
    save_assets([asset], str(tmp_path), db_path, scanned_at=3000.0)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_meta(root_dir, last_scan_at, schema_version) VALUES(?, ?, ?)",
            (str(tmp_path / "stale_root"), 100.0, SCHEMA_VERSION),
        )
        conn.commit()

    assert len(load_scan_meta(db_path)) == 2

    save_assets([asset], str(tmp_path), db_path, scanned_at=4000.0)

    meta = load_scan_meta(db_path)
    assert len(meta) == 1
    assert meta[0]["root_dir"] == str(tmp_path)
    assert meta[0]["last_scan_at"] == 4000.0
    assert active_workspace_root(db_path) == str(tmp_path)


def test_query_assets_by_keyword_and_type(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    mainboard = make_asset(
        tmp_path, firmware_type="mainboard", model="L36", directory_name="mainboard_v1"
    )
    handcontrol = make_asset(
        tmp_path,
        firmware_type="handcontrol_ui",
        model="L39",
        directory_name="handcontrol_v2",
    )
    save_assets([mainboard, handcontrol], str(tmp_path), db_path)

    assert [item["model"] for item in query_assets("L39", path=db_path)] == ["L39"]
    assert [
        item["firmware_type"]
        for item in query_assets(firmware_types=["mainboard"], path=db_path)
    ] == ["mainboard"]
    assert query_assets("no-match", firmware_types=["mainboard"], path=db_path) == []


def test_query_assets_keyword_is_space_tokenized_AND(tmp_path: Path):
    """多词搜索：空格分词，每个词都要命中（任意字段），词间 AND。

    解决"既想搜手控、又想搜版本号"——把'手控 V13'当两个独立约束。
    """
    db_path = tmp_path / "fwasset.db"
    # 同型号下两个手控变体，版本不同
    hc_v12 = make_asset(
        tmp_path, firmware_type="handcontrol_ui", model="L36", directory_name="手控UI-A"
    )
    hc_v12["version"] = "V12"
    hc_v12["firmware_label"] = "手控UI"
    hc_v13 = make_asset(
        tmp_path, firmware_type="handcontrol_ui", model="L36", directory_name="手控UI-B"
    )
    hc_v13["version"] = "V13"
    hc_v13["firmware_label"] = "手控UI"
    mainboard = make_asset(
        tmp_path, firmware_type="mainboard", model="L36", directory_name="主板程序"
    )
    mainboard["version"] = "V13"  # 同样含 V13，但不是手控
    save_assets([hc_v12, hc_v13, mainboard], str(tmp_path), db_path)

    # "手控 V13" → 只命中 手控UI-B（手控 AND V13），不含 V12 手控、不含 V13 主板
    got = query_assets("手控 V13", path=db_path)
    assert [a["directory_name"] for a in got] == ["手控UI-B"]

    # 词序无关："V13 手控" 同结果
    got2 = query_assets("V13 手控", path=db_path)
    assert [a["directory_name"] for a in got2] == ["手控UI-B"]

    # 单词仍跨字段模糊匹配（向后兼容）
    assert {a["directory_name"] for a in query_assets("手控", path=db_path)} == {
        "手控UI-A",
        "手控UI-B",
    }

    # 多个空格 / 首尾空格不影响
    assert [
        a["directory_name"] for a in query_assets("  手控   V13  ", path=db_path)
    ] == ["手控UI-B"]


def test_query_assets_supports_sort_key_and_direction(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    assets = [
        make_asset(tmp_path, model="L100", directory_name="mainboard_v2"),
        make_asset(tmp_path, model="L20", directory_name="mainboard_v10"),
        make_asset(tmp_path, model="L3", directory_name="mainboard_v1"),
    ]
    assets[0]["version"] = "V2.0.0"
    assets[1]["version"] = "V10.0.0"
    assets[2]["version"] = "V1.0.0"
    save_assets(assets, str(tmp_path), db_path)

    assert [
        item["model"] for item in query_assets(path=db_path, sort_key=SortKey.MODEL)
    ] == ["L3", "L20", "L100"]
    assert [
        item["version"] for item in query_assets(path=db_path, sort_key=SortKey.VERSION)
    ] == [
        "V1.0.0",
        "V2.0.0",
        "V10.0.0",
    ]
    assert [
        item["model"]
        for item in query_assets(path=db_path, sort_key=SortKey.MODEL, ascending=False)
    ] == [
        "L100",
        "L20",
        "L3",
    ]


def test_delete_missing_assets_prunes_hidden_items(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    keep_asset = make_asset(tmp_path, model="L36", directory_name="keep")
    removed_asset = make_asset(tmp_path, model="L39", directory_name="removed")
    save_assets([keep_asset, removed_asset], str(tmp_path), db_path)
    hide_item(removed_asset["path"], "asset", db_path)

    removed = delete_missing_assets([keep_asset["path"]], db_path)

    assert removed == 1
    assert [item["path"] for item in load_assets(db_path)] == [keep_asset["path"]]
    assert load_hidden_items(db_path) == {}


def test_hidden_items_persist_and_can_be_pruned(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    asset = make_asset(tmp_path)
    save_assets([asset], str(tmp_path), db_path)

    hide_item(
        asset["model_directory_path"], "model_directory", db_path, created_at=100.0
    )
    assert load_hidden_items(db_path) == {
        asset["model_directory_path"]: "model_directory"
    }

    unhide_item(asset["model_directory_path"], db_path)
    assert load_hidden_items(db_path) == {}

    hide_item(asset["path"], "asset", db_path)
    save_assets([], str(tmp_path), db_path)
    assert prune_missing_hidden_items(db_path) == 0
    assert load_hidden_items(db_path) == {}


def test_schema_version_mismatch_raises_chinese_recovery_message(tmp_path: Path):
    db_path = tmp_path / "fwasset.db"
    init_asset_index(db_path)
    with pytest.raises(AssetIndexError, match="重新扫描生成"):
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'"
            )
        init_asset_index(db_path)


def test_schema_version_one_migrates_to_v3(tmp_path: Path):
    """v1 database (no usb_flow, no category/platform/scheme) should auto-migrate to v3."""
    db_path = tmp_path / "fwasset.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE assets (
                path TEXT PRIMARY KEY,
                series TEXT NOT NULL,
                model TEXT NOT NULL,
                model_directory_name TEXT NOT NULL,
                model_directory_path TEXT NOT NULL,
                firmware_type TEXT NOT NULL,
                firmware_label TEXT NOT NULL,
                flash_mode TEXT NOT NULL,
                version TEXT NOT NULL,
                directory_name TEXT NOT NULL,
                files_json TEXT NOT NULL,
                modified_time REAL NOT NULL,
                scanned_at REAL NOT NULL,
                tool_name TEXT NOT NULL,
                tool_path TEXT NOT NULL,
                tool_dir TEXT NOT NULL,
                label TEXT NOT NULL
            );
            """
        )

    init_asset_index(db_path)

    assert schema_version(db_path) == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(assets)").fetchall()
        ]
    assert "usb_flow" in columns
    assert "category" in columns
    assert "platform" in columns
    assert "scheme_name" in columns
    assert "scheme_path" in columns


def test_schema_version_two_migrates_to_v3(tmp_path: Path):
    """v2 database (has usb_flow but no category/platform/scheme) should auto-migrate to v3."""
    db_path = tmp_path / "fwasset.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '2');
            CREATE TABLE assets (
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
                label TEXT NOT NULL
            );
            """
        )

    init_asset_index(db_path)

    assert schema_version(db_path) == SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(assets)").fetchall()
        ]
    assert "category" in columns
    assert "platform" in columns
    assert "scheme_name" in columns
    assert "scheme_path" in columns


def test_query_assets_by_category_and_scheme(tmp_path: Path):
    """query_assets should support filtering by category, platform and scheme_name."""
    db_path = tmp_path / "fwasset.db"
    common_asset = make_asset(
        tmp_path, model="L36", directory_name="common_main", category="common"
    )
    custom_asset = make_asset(
        tmp_path,
        model="L36",
        directory_name="custom_main",
        category="custom",
        scheme_name="以色列-Royal-Z9",
        scheme_path="/some/path/以色列-Royal-Z9",
    )
    save_assets([common_asset, custom_asset], str(tmp_path), db_path)

    common_results = query_assets(path=db_path, category="common")
    assert len(common_results) == 1
    assert common_results[0]["directory_name"] == "common_main"

    custom_results = query_assets(path=db_path, category="custom")
    assert len(custom_results) == 1
    assert custom_results[0]["scheme_name"] == "以色列-Royal-Z9"

    scheme_results = query_assets(path=db_path, scheme_name="以色列")
    assert len(scheme_results) == 1
    assert scheme_results[0]["category"] == "custom"
