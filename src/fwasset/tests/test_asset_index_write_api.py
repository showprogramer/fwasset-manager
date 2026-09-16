"""REVIEW-20260728 R3 / gate #3：asset_index 行级写 API 测试。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from fwasset.core.asset_index import (
    AssetIndexError,
    bulk_reindex_subtree,
    connect_asset_index,
    count_assets,
    delete_asset,
    hide_item,
    load_assets,
    load_hidden_items,
    load_scan_meta,
    prune_missing_hidden_items,
    replace_asset,
    save_assets,
    upsert_asset,
)
from fwasset.core.types import FirmwareAsset


def make_asset(
    base: Path,
    *,
    model: str = "L36",
    directory_name: str = "主板程序_v1",
    version: str = "V1.0.0",
    label: str = "",
) -> FirmwareAsset:
    model_dir = base / f"{model}主板程序"
    asset_dir = model_dir / directory_name
    return {
        "series": model,
        "firmware_type": "mainboard",
        "firmware_label": "主板程序",
        "flash_mode": "tool_launch",
        "usb_flow": "",
        "model": model,
        "version": version,
        "model_directory_name": model_dir.name,
        "model_directory_path": str(model_dir),
        "path": str(asset_dir),
        "directory_name": directory_name,
        "files": ["firmware.bin"],
        "modified_time": 123.0,
        "tool_name": "主板烧录工具",
        "tool_path": "",
        "tool_dir": "主板程序",
        "label": label or f"{model} {version} [{directory_name}]",
        "category": "",
        "platform": "",
        "scheme_name": "",
        "scheme_path": "",
        "chassis_type": "",
    }


@pytest.fixture()
def seeded_db(tmp_path: Path) -> tuple[Path, Path, FirmwareAsset, FirmwareAsset]:
    """两个子树（两个型号目录）各一个资产；db 由 save_assets 全量播种。"""
    db_path = tmp_path / "fwasset.db"
    asset_a = make_asset(tmp_path, model="L36", directory_name="主板程序_v1")
    asset_b = make_asset(tmp_path, model="L50", directory_name="主板程序_v2")
    save_assets([asset_a, asset_b], str(tmp_path), db_path, scanned_at=1000.0)
    return db_path, tmp_path, asset_a, asset_b


def _snapshot(db_path: Path) -> tuple[list[FirmwareAsset], dict[str, str], list]:
    return (
        load_assets(db_path),
        load_hidden_items(db_path),
        load_scan_meta(db_path),
    )


# --- upsert_asset -----------------------------------------------------------


def test_upsert_asset_insert_and_overwrite(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, _ = seeded_db
    new_asset = make_asset(ws, model="L36", directory_name="主板程序_v9", version="V9.0")

    meta_before = load_scan_meta(db_path)
    upsert_asset(new_asset, path=db_path, scanned_at=2000.0)

    loaded = {a["path"]: a for a in load_assets(db_path)}
    assert loaded[new_asset["path"]] == new_asset
    assert count_assets(db_path) == 3
    # scan_meta 不受行级写影响
    assert load_scan_meta(db_path) == meta_before

    # 同 path 覆盖
    updated = dict(new_asset, label="覆盖后的标签")
    upsert_asset(updated, path=db_path, scanned_at=2001.0)
    loaded = {a["path"]: a for a in load_assets(db_path)}
    assert loaded[new_asset["path"]]["label"] == "覆盖后的标签"
    assert count_assets(db_path) == 3


def test_upsert_asset_rejects_incomplete_payload(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, _, _ = seeded_db
    before = _snapshot(db_path)

    broken = dict(make_asset(ws, model="L66"))
    del broken["series"]  # type: ignore[typeddict-item]
    with pytest.raises(AssetIndexError, match="缺少必需字段"):
        upsert_asset(broken, path=db_path)  # type: ignore[arg-type]

    empty_path = make_asset(ws, model="L66", directory_name="x")
    empty_path["path"] = ""
    with pytest.raises(AssetIndexError, match="path 不能为空"):
        upsert_asset(empty_path, path=db_path)

    relative = make_asset(ws, model="L66", directory_name="x")
    relative["path"] = "relative/dir"
    with pytest.raises(AssetIndexError, match="绝对路径"):
        upsert_asset(relative, path=db_path)

    assert _snapshot(db_path) == before


# --- replace_asset ----------------------------------------------------------


def test_replace_asset_replaces_full_row_not_only_path(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, _ = seeded_db
    # 重命名目录 → path / directory_name / version / label 全变
    renamed = dict(asset_a)
    renamed["path"] = str(ws / f"{asset_a['model']}主板程序" / "主板程序_v1_改名")
    renamed["directory_name"] = "主板程序_v1_改名"
    renamed["version"] = "V2.0.0"
    renamed["label"] = "L36 V2.0.0 [主板程序_v1_改名]"

    replace_asset(asset_a["path"], renamed, path=db_path, scanned_at=2000.0)

    loaded = load_assets(db_path)
    assert renamed in loaded
    assert asset_a["path"] not in {a["path"] for a in loaded}
    assert count_assets(db_path) == 2


def test_replace_asset_rejects_missing_old_row(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, _ = seeded_db
    before = _snapshot(db_path)
    with pytest.raises(AssetIndexError, match="旧资产不存在"):
        replace_asset(str(ws / "不存在"), asset_a, path=db_path)
    assert _snapshot(db_path) == before


def test_replace_asset_rejects_new_path_clash(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, asset_a, asset_b = seeded_db
    before = _snapshot(db_path)
    clash = dict(asset_a, path=asset_b["path"])
    with pytest.raises(AssetIndexError, match="已被其他资产占用"):
        replace_asset(asset_a["path"], clash, path=db_path)
    assert _snapshot(db_path) == before


def test_replace_asset_migrates_exact_asset_hidden_row(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, _ = seeded_db
    renamed = dict(asset_a)
    renamed["path"] = str(ws / f"{asset_a['model']}主板程序" / "改名目录")
    hide_item(asset_a["path"], "asset", path=db_path)

    replace_asset(asset_a["path"], renamed, path=db_path)

    assert load_hidden_items(db_path) == {renamed["path"]: "asset"}


# --- delete_asset -----------------------------------------------------------


def test_delete_asset_removes_row_and_asset_hidden(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, asset_a, _ = seeded_db
    hide_item(asset_a["path"], "asset", path=db_path)

    assert delete_asset(asset_a["path"], path=db_path) is True
    assert asset_a["path"] not in {a["path"] for a in load_assets(db_path)}
    assert load_hidden_items(db_path) == {}


def test_delete_asset_is_idempotent_when_missing(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, _asset_a, _ = seeded_db
    assert delete_asset(str(Path(" nowhere ") / "x"), path=db_path) is False


def test_delete_asset_cleans_model_hidden_only_when_model_empty(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    # L36 型号目录下再加一个资产；隐藏两个型号目录
    asset_a2 = make_asset(ws, model="L36", directory_name="主板程序_v1b")
    upsert_asset(asset_a2, path=db_path)
    hide_item(asset_a["model_directory_path"], "model_directory", path=db_path)
    hide_item(asset_b["model_directory_path"], "model_directory", path=db_path)

    delete_asset(asset_a["path"], path=db_path)
    # L36 型号目录仍有 asset_a2 → 型号隐藏保留；L50 未受影响
    assert load_hidden_items(db_path) == {
        asset_a["model_directory_path"]: "model_directory",
        asset_b["model_directory_path"]: "model_directory",
    }

    delete_asset(asset_a2["path"], path=db_path)
    # L36 型号目录已空 → 型号隐藏清理；L50 保留
    assert load_hidden_items(db_path) == {
        asset_b["model_directory_path"]: "model_directory"
    }


def test_hidden_boundary_survival_a_vs_ab(tmp_path: Path) -> None:
    """回归：firmware_type 隐藏项存活判断用路径边界，不用裸 startswith。

    旧实现 asset_path.startswith(item_path) 会把 ``.../手控UIB_v1`` 误判为
    ``.../手控UI`` 边界内的存活依据，导致失活的类型隐藏项无法清理。
    """
    db_path = tmp_path / "fwasset.db"
    base = tmp_path / "L36主板程序"
    asset_in = make_asset(tmp_path, model="L36", directory_name="手控UI/v1")
    asset_ab = make_asset(tmp_path, model="L36", directory_name="手控UIB_v1")
    boundary = str(base / "手控UI")
    hide_item(boundary, "firmware_type", path=db_path)
    save_assets([asset_ab], str(tmp_path), db_path, scanned_at=1000.0)

    # 只有 .../手控UIB_v1 存活：边界 .../手控UI 内无资产 → 隐藏项应被清理
    prune_missing_hidden_items(path=db_path)
    assert load_hidden_items(db_path) == {}

    # 边界内确有资产时保留
    hide_item(boundary, "firmware_type", path=db_path)
    save_assets([asset_in, asset_ab], str(tmp_path), db_path, scanned_at=1001.0)
    prune_missing_hidden_items(path=db_path)
    assert load_hidden_items(db_path) == {boundary: "firmware_type"}


# --- bulk_reindex_subtree ---------------------------------------------------


def test_bulk_reindex_replaces_subtree_and_keeps_sibling(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    subtree = asset_a["model_directory_path"]
    new_a1 = make_asset(ws, model="L36", directory_name="主板程序_new1")
    new_a2 = make_asset(ws, model="L36", directory_name="主板程序_new2")

    # 隐藏：子树内旧资产（将失活）+ 子树外资产（必须保留）
    hide_item(asset_a["path"], "asset", path=db_path)
    hide_item(asset_b["path"], "asset", path=db_path)
    sibling_before = _snapshot(db_path)[0]

    bulk_reindex_subtree(str(ws), subtree, [new_a1, new_a2], path=db_path, scanned_at=2000.0)

    loaded = {a["path"]: a for a in load_assets(db_path)}
    # 子树内：旧行被替换为完整新行（字段级）
    assert loaded[new_a1["path"]] == new_a1
    assert loaded[new_a2["path"]] == new_a2
    assert asset_a["path"] not in loaded
    # 子树外：兄弟子树逐行不变
    assert loaded[asset_b["path"]] == sibling_before[
        [a["path"] for a in sibling_before].index(asset_b["path"])
    ]
    # 隐藏项：子树内失活清理，子树外保留
    assert load_hidden_items(db_path) == {asset_b["path"]: "asset"}


def test_bulk_reindex_empty_assets_clears_subtree(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    hide_item(asset_a["path"], "asset", path=db_path)

    bulk_reindex_subtree(str(ws), asset_a["model_directory_path"], [], path=db_path)

    loaded = {a["path"] for a in load_assets(db_path)}
    assert loaded == {asset_b["path"]}
    assert load_hidden_items(db_path) == {}


@pytest.mark.parametrize(
    "mutate",
    ["outside_subtree", "duplicate_paths", "missing_key"],
)
def test_bulk_reindex_validation_rejects_whole_batch(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
    mutate: str,
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    before = _snapshot(db_path)
    subtree = asset_a["model_directory_path"]

    if mutate == "outside_subtree":
        outside = make_asset(ws, model="L50", directory_name="越界目录")
        batch = [make_asset(ws, model="L36", directory_name="正常"), outside]
    elif mutate == "duplicate_paths":
        dup = make_asset(ws, model="L36", directory_name="重复")
        batch = [dup, dict(dup)]
    else:
        broken = make_asset(ws, model="L36", directory_name="缺键")
        del broken["label"]  # type: ignore[typeddict-item]
        batch = [broken]

    with pytest.raises(AssetIndexError):
        bulk_reindex_subtree(str(ws), subtree, batch, path=db_path)
    assert _snapshot(db_path) == before


def test_bulk_reindex_rejects_workspace_mismatch(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, _ = seeded_db
    before = _snapshot(db_path)
    other_root = ws / "其他工作区"
    other_root.mkdir()

    # 空批次：输入校验通过，走到库内工作区一致性校验才拒绝
    with pytest.raises(AssetIndexError, match="工作区不一致"):
        bulk_reindex_subtree(str(other_root), str(other_root / "sub"), [], path=db_path)
    assert _snapshot(db_path) == before


def test_bulk_reindex_rejects_when_scan_meta_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "fwasset.db"
    asset = make_asset(tmp_path, model="L36")
    from fwasset.core.asset_index import init_asset_index

    init_asset_index(db_path)
    with pytest.raises(AssetIndexError, match="工作区不一致"):
        bulk_reindex_subtree(
            str(tmp_path), asset["model_directory_path"], [asset], path=db_path
        )


def test_bulk_reindex_rejects_subtree_outside_workspace(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, _asset_a, _ = seeded_db
    before = _snapshot(db_path)
    outside = ws.parent / "外部子树"
    with pytest.raises(AssetIndexError, match="不在工作区内"):
        bulk_reindex_subtree(str(ws), str(outside), [], path=db_path)
    assert _snapshot(db_path) == before


# --- 路径身份归一（Windows 大小写 / 分隔符变体视为同一资产） -----------------


def test_replace_asset_case_variant_keeps_single_row(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, asset_a, asset_b = seeded_db
    # 仅大小写/分隔符变化：SQLite TEXT 主键区分大小写，不做身份归一会产生重复行
    renamed = dict(asset_a)
    renamed["path"] = asset_a["path"].lower().replace("\\", "/")

    replace_asset(asset_a["path"], renamed, path=db_path)

    loaded = load_assets(db_path)
    assert len(loaded) == 2
    assert {a["path"] for a in loaded} == {renamed["path"], asset_b["path"]}


def test_upsert_asset_case_variant_no_duplicate(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, asset_a, asset_b = seeded_db
    variant = dict(asset_a, label="变体写法")
    variant["path"] = asset_a["path"].lower().replace("\\", "/")

    upsert_asset(variant, path=db_path)

    loaded = load_assets(db_path)
    assert len(loaded) == 2
    assert {a["path"] for a in loaded} == {variant["path"], asset_b["path"]}


def test_delete_asset_case_variant_matches(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, _ws, asset_a, asset_b = seeded_db
    assert delete_asset(asset_a["path"].lower().replace("\\", "/"), path=db_path) is True
    assert [a["path"] for a in load_assets(db_path)] == [asset_b["path"]]


# --- 祖先隐藏项清理（对账叶子子树清空后，父型号/类型隐藏项失活） -------------


def test_bulk_reindex_prunes_ancestor_hidden_but_keeps_sibling(tmp_path: Path) -> None:
    db_path = tmp_path / "fwasset.db"
    asset_in = make_asset(tmp_path, model="L36", directory_name="手控UI/v1")
    asset_out = make_asset(tmp_path, model="L36", directory_name="主板程序_v2")
    sibling = make_asset(tmp_path, model="L50", directory_name="主板程序_v1")
    model_dir = asset_in["model_directory_path"]
    save_assets([asset_in, asset_out, sibling], str(tmp_path), db_path, scanned_at=1000.0)
    hide_item(str(Path(model_dir) / "手控UI"), "firmware_type", path=db_path)
    hide_item(model_dir, "model_directory", path=db_path)
    hide_item(sibling["model_directory_path"], "model_directory", path=db_path)

    # 清空叶子子树 M/手控UI → 类型隐藏项失活清理；型号隐藏项因仍有 asset_out 保留
    bulk_reindex_subtree(str(tmp_path), str(Path(model_dir) / "手控UI"), [], path=db_path)
    assert load_hidden_items(db_path) == {
        model_dir: "model_directory",
        sibling["model_directory_path"]: "model_directory",
    }

    # 清空整个型号目录 → 型号隐藏项失活清理；兄弟型号的隐藏项保留
    bulk_reindex_subtree(str(tmp_path), model_dir, [], path=db_path)
    assert load_hidden_items(db_path) == {
        sibling["model_directory_path"]: "model_directory"
    }


# --- junction / 真实路径混用的身份与归属（Windows） ---------------------------


def _try_make_junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可创建")
def test_hidden_survival_with_junction_path_identity(tmp_path: Path) -> None:
    """junction 路径登记的隐藏项 vs 真实路径 bulk：判活与影响面必须贯穿。

    junction 经 resolve 折叠到真实路径；词法比较永远不命中，只有
    normcase/resolved 双重身份与归属判断能正确判活/清理。
    """
    real_ws = tmp_path / "real_ws"
    real_ws.mkdir()
    asset_in = make_asset(real_ws, model="L36", directory_name="手控UI/v1")
    asset_out = make_asset(real_ws, model="L36", directory_name="主板程序_v2")
    db_path = tmp_path / "fwasset.db"
    save_assets([asset_in, asset_out], str(real_ws), db_path, scanned_at=1000.0)

    junction = tmp_path / "link_ws"
    if not _try_make_junction(junction, real_ws):
        pytest.skip("当前环境无法创建 junction")

    # junction 形式的祖先 model hidden；真实路径 bulk 清空叶子子树 → 仍存活
    hide_item(str(junction / "L36主板程序"), "model_directory", path=db_path)
    bulk_reindex_subtree(
        str(real_ws), str(real_ws / "L36主板程序" / "手控UI"), [], path=db_path
    )
    assert load_hidden_items(db_path) == {
        str(junction / "L36主板程序"): "model_directory"
    }

    # 再清空整个型号目录（junction 路径作边界）→ junction 形式 hidden 失活清理
    bulk_reindex_subtree(str(real_ws), str(junction / "L36主板程序"), [], path=db_path)
    assert load_hidden_items(db_path) == {}


# --- 事务回滚注入（真实 SQL 触发器，DELETE 已执行后 INSERT 失败） ------------


def _add_fail_trigger(db_path: Path, fail_path: str) -> None:
    # SQLite 触发器不能使用绑定参数，改为转义后的字符串字面量
    literal = fail_path.replace("'", "''")
    conn = connect_asset_index(db_path)
    try:
        conn.execute(
            f"""
            CREATE TRIGGER fail_insert BEFORE INSERT ON assets
            WHEN NEW.path = '{literal}' BEGIN SELECT RAISE(ABORT, 'boom'); END
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_replace_asset_rollback_keeps_old_row_and_hidden(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    renamed = dict(asset_a)
    renamed["path"] = str(ws / f"{asset_a['model']}主板程序" / "炸点目录")
    _add_fail_trigger(db_path, renamed["path"])
    hide_item(asset_a["path"], "asset", path=db_path)

    with pytest.raises(AssetIndexError) as excinfo:
        replace_asset(asset_a["path"], renamed, path=db_path)
    # sqlite3.DatabaseError 链式转换为 AssetIndexError
    assert isinstance(excinfo.value.__cause__, sqlite3.DatabaseError)

    # 回滚完整：旧行（含兄弟子树）+ 旧 asset 隐藏行原样保留
    assert set(a["path"] for a in load_assets(db_path)) == {
        asset_a["path"],
        asset_b["path"],
    }
    assert load_hidden_items(db_path) == {asset_a["path"]: "asset"}


def test_bulk_reindex_rollback_keeps_old_subtree_rows(
    seeded_db: tuple[Path, Path, FirmwareAsset, FirmwareAsset],
) -> None:
    db_path, ws, asset_a, asset_b = seeded_db
    new_a = make_asset(ws, model="L36", directory_name="炸点目录")
    _add_fail_trigger(db_path, new_a["path"])

    with pytest.raises(AssetIndexError):
        bulk_reindex_subtree(
            str(ws), asset_a["model_directory_path"], [new_a], path=db_path
        )

    # 子树旧行未被删除（事务整体回滚）
    assert set(a["path"] for a in load_assets(db_path)) == {
        asset_a["path"],
        asset_b["path"],
    }
