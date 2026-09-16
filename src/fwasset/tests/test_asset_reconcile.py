"""REVIEW-20260728 R3 / gate #3：reconcile_subtree 对账冷接口集成测试。

统一纪律：断言磁盘与库一致，且分层断言——目标子树字段级一致、兄弟子树
逐行不变、整库与全工作区扫描字段级对拍。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import fwasset.core.asset_reconcile as asset_reconcile
from fwasset.core.asset_index import (
    AssetIndexError,
    hide_item,
    load_assets,
    load_hidden_items,
    load_scan_meta,
    save_assets,
)
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.file_scan import scan_firmware_assets, scan_firmware_subtree


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_tree(root: Path) -> Path:
    _write(root / "L36程序" / "通用" / "主板程序A" / "ITE_NOR_L36_v1.0.0.bin")
    _write(root / "L36程序" / "手控UI" / "YJ-L36-3122MG_UI_118.3.10.ROM")
    _write(root / "L36程序" / "手控UI" / "firmware.pkg")
    _write(root / "L50程序" / "通用" / "主板程序C" / "ITE_NOR_L50_v3.0.0.bin")
    return root / "L36程序"


@pytest.fixture()
def seeded_ws(tmp_path: Path) -> tuple[Path, Path, Path]:
    """完整工作区 + 全量播种的索引库。"""
    ws = tmp_path / "工作区"
    ws.mkdir()
    subtree = make_tree(ws)
    db_path = tmp_path / "fwasset.db"
    assets, errors = scan_firmware_assets(str(ws))
    assert errors == []
    save_assets(assets, str(ws), db_path, scanned_at=1000.0)
    return ws, subtree, db_path


def test_reconcile_updates_target_subtree_only(seeded_ws) -> None:
    ws, subtree, db_path = seeded_ws
    sibling_before = [a for a in load_assets(db_path) if "L50" in a["path"]]

    # 磁盘改动：子树内新增目录 + 重命名既有目录
    _write(ws / "L36程序" / "通用" / "主板程序D" / "ITE_NOR_L36_v4.0.0.bin")
    old_dir = ws / "L36程序" / "手控UI"
    old_dir.rename(ws / "L36程序" / "手控UI改名")

    reconcile_subtree(str(ws), str(subtree), path=db_path)

    # 1) 目标子树：库记录 == 子树扫描结果（字段级）
    db_sub = [a for a in load_assets(db_path) if a["path"].startswith(str(subtree))]
    scanned_sub, errors = scan_firmware_subtree(str(ws), str(subtree))
    assert errors == []
    assert sorted(db_sub, key=lambda a: (a["path"], a["firmware_type"])) == sorted(
        scanned_sub, key=lambda a: (a["path"], a["firmware_type"])
    )
    # 2) 兄弟子树逐行不变
    assert [a for a in load_assets(db_path) if "L50" in a["path"]] == sibling_before
    # 3) 整库 == 全工作区扫描（字段级）
    full_scan, full_errors = scan_firmware_assets(str(ws))
    assert full_errors == []
    assert sorted(load_assets(db_path), key=lambda a: (a["path"], a["firmware_type"])) == sorted(
        full_scan, key=lambda a: (a["path"], a["firmware_type"])
    )
    # 4) scan_meta 未被改动
    meta = load_scan_meta(db_path)
    assert len(meta) == 1 and meta[0]["root_dir"] == str(ws)


def test_reconcile_deleted_subtree_clears_rows_and_boundary_hidden(seeded_ws) -> None:
    ws, subtree, db_path = seeded_ws
    hidden_target = [
        a["path"] for a in load_assets(db_path) if a["path"].startswith(str(subtree))
    ][0]
    hide_item(hidden_target, "asset", path=db_path)
    shutil.rmtree(subtree)

    reconcile_subtree(str(ws), str(subtree), path=db_path)

    remaining = load_assets(db_path)
    assert all(not a["path"].startswith(str(subtree)) for a in remaining)
    assert len(remaining) == 1  # 兄弟 L50 子树保留
    hidden = load_hidden_items(db_path)
    assert hidden_target not in hidden
    assert all(p.startswith(str(ws / "L50程序")) for p in hidden) or not hidden


def test_reconcile_scan_errors_do_not_write(seeded_ws, monkeypatch) -> None:
    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)

    def broken_scan(*args, **kwargs):
        return [], [
            {"severity": "error", "message": f"{args[0]}: boom", "path": ""}
        ]

    monkeypatch.setattr(asset_reconcile, "scan_firmware_subtree", broken_scan)
    with pytest.raises(AssetIndexError, match="禁止对账写库"):
        reconcile_subtree(str(ws), str(subtree), path=db_path)
    assert load_assets(db_path) == before


def test_reconcile_scan_warnings_allow_write(seeded_ws, monkeypatch) -> None:
    """warning 级 issue（如 chassis 配置损坏）放行对账写库（TASK-20260916）。"""
    ws, subtree, db_path = seeded_ws

    def warned_scan(*args, **kwargs):
        assets, _issues = real_scan_firmware_subtree(*args, **kwargs)
        return assets, [
            {
                "severity": "warning",
                "message": "平台配置读取失败（parse_error），机芯类型留空",
                "path": str(subtree / "平台配置.toml"),
            }
        ]

    real_scan_firmware_subtree = asset_reconcile.scan_firmware_subtree
    monkeypatch.setattr(asset_reconcile, "scan_firmware_subtree", warned_scan)
    reconcile_subtree(str(ws), str(subtree), path=db_path)  # 不因 warning 阻止
    scanned_sub, _ = scan_firmware_subtree(str(ws), str(subtree))
    db_sub = [a for a in load_assets(db_path) if a["path"].startswith(str(subtree))]
    assert sorted(db_sub, key=lambda a: (a["path"], a["firmware_type"])) == sorted(
        scanned_sub, key=lambda a: (a["path"], a["firmware_type"])
    )


def test_reconcile_cancelled_does_not_write(seeded_ws, monkeypatch) -> None:
    import threading

    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)
    cancel_event = threading.Event()
    cancel_event.set()

    def cancelled_scan(*args, **kwargs):
        return [], [
            {"severity": "error", "message": "扫描已被用户取消", "path": ""}
        ]

    monkeypatch.setattr(asset_reconcile, "scan_firmware_subtree", cancelled_scan)
    with pytest.raises(AssetIndexError, match="未写库"):
        reconcile_subtree(str(ws), str(subtree), path=db_path, cancel_event=cancel_event)
    assert load_assets(db_path) == before


def test_reconcile_rejects_workspace_mismatch(seeded_ws) -> None:
    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)
    other = ws / "其他工作区"
    other.mkdir()

    with pytest.raises(AssetIndexError, match="工作区不一致"):
        reconcile_subtree(str(other), str(other / "sub"), path=db_path)
    assert load_assets(db_path) == before


def test_reconcile_rejects_subtree_not_directory(seeded_ws) -> None:
    ws, _subtree, db_path = seeded_ws
    before = load_assets(db_path)
    a_file = ws / "说明.txt"
    a_file.write_text("x", encoding="utf-8")

    with pytest.raises(AssetIndexError, match="不是目录"):
        reconcile_subtree(str(ws), str(a_file), path=db_path)
    assert load_assets(db_path) == before


def test_reconcile_missing_workspace_root_does_not_write(seeded_ws) -> None:
    """P0 回归：工作区根被重命名/卸载 ≠ 子树已删除，必须报错不改库。"""
    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)
    hidden_before = load_hidden_items(db_path)

    ws.rename(ws.parent / "工作区搬家")
    with pytest.raises(AssetIndexError, match="工作区根目录不存在"):
        reconcile_subtree(str(ws), str(subtree), path=db_path)
    assert load_assets(db_path) == before
    assert load_hidden_items(db_path) == hidden_before


def test_reconcile_preset_cancel_blocks_deleted_subtree_clear(seeded_ws) -> None:
    """P0 回归：已删子树的空快照写库也必须先过取消检查。"""
    import threading

    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)
    cancel_event = threading.Event()
    cancel_event.set()
    shutil.rmtree(subtree)

    with pytest.raises(AssetIndexError, match="未写库"):
        reconcile_subtree(str(ws), str(subtree), path=db_path, cancel_event=cancel_event)
    assert load_assets(db_path) == before


def test_reconcile_real_scan_error_does_not_write(seeded_ws, monkeypatch) -> None:
    """P2：真实 os.walk onerror 产生的扫描错误必须阻止对账写库。"""
    import os as os_mod

    import fwasset.core.file_scan as file_scan_mod

    ws, subtree, db_path = seeded_ws
    before = load_assets(db_path)
    real_walk = os_mod.walk

    def failing_walk(start, *args, **kwargs):
        kwargs["onerror"](OSError(13, "Permission denied", str(start)))
        yield from real_walk(start, *args, **kwargs)

    monkeypatch.setattr(file_scan_mod.os, "walk", failing_walk)
    with pytest.raises(AssetIndexError, match="禁止对账写库"):
        reconcile_subtree(str(ws), str(subtree), path=db_path)
    assert load_assets(db_path) == before


def test_reconcile_rejects_unconfigured_workspace(tmp_path: Path) -> None:
    with pytest.raises(AssetIndexError, match="未配置"):
        reconcile_subtree("", str(tmp_path / "sub"), path=tmp_path / "fwasset.db")
