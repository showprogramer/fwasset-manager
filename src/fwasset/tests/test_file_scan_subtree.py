"""REVIEW-20260728 R3 / gate #3：scan_firmware_subtree 子树扫描测试。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import fwasset.core.file_scan as file_scan
from fwasset.core.file_scan import scan_firmware_assets, scan_firmware_subtree
from fwasset.core.path_guard import PathGuardError


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_tree(root: Path) -> Path:
    """构造多型号工作区：L36 子树（通用 + 双机芯平台 + 手控UI）与 L50 兄弟子树。"""
    _write(root / "L36程序" / "通用" / "主板程序A" / "ITE_NOR_L36_v1.0.0.bin")
    _write(root / "L36程序" / "通用" / "双机芯-上3D-下2D" / "主板程序B" / "ITE_NOR_L36_v2.0.0.bin")
    _write(root / "L36程序" / "手控UI" / "YJ-L36-3122MG_UI_118.3.10.ROM")
    _write(root / "L36程序" / "手控UI" / "firmware.pkg")
    _write(root / "L50程序" / "通用" / "主板程序C" / "ITE_NOR_L50_v3.0.0.bin")
    return root / "L36程序"


def test_subtree_scan_field_level_matches_filtered_full_scan(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    ws = str(tmp_path)

    full, full_errors = scan_firmware_assets(ws)
    sub, sub_errors = scan_firmware_subtree(ws, str(root))

    assert sub_errors == []
    assert full_errors == []
    boundary = str(root)
    expected = [
        a for a in full if a["path"] == boundary or a["path"].startswith(boundary + os.sep)
    ]
    assert sub == expected  # 字段级相等（含 category/platform），不只是 path 集合
    assert sub  # fixture 自身必须产出资产
    platforms = {a["platform"] for a in sub}
    assert "双机芯-上3D-下2D" in platforms


def test_subtree_scan_only_walks_subtree(tmp_path: Path, monkeypatch) -> None:
    """防止「全根扫描后过滤」的假实现：walk 起点必须是子树且不越界。"""
    root = make_tree(tmp_path)
    visited: list[str] = []
    starts: list[str] = []
    real_walk = os.walk

    def spy_walk(start: str, *args, **kwargs):
        starts.append(start)

        def record(dirpath, dirnames, filenames):
            visited.append(dirpath)
            return dirpath, dirnames, filenames

        kwargs = dict(kwargs)
        kwargs.pop("onerror", None)
        for dirpath, dirnames, filenames in real_walk(start, *args, **kwargs):
            yield record(dirpath, dirnames, filenames)

    monkeypatch.setattr(file_scan.os, "walk", spy_walk)
    sub, errors = scan_firmware_subtree(str(tmp_path), str(root))

    assert errors == []
    assert [str(s) for s in starts] == [str(root)]
    assert sub
    assert visited
    assert all(p.startswith(str(root)) for p in visited)


def test_subtree_scan_equal_to_full_scan_when_subtree_is_workspace(
    tmp_path: Path,
) -> None:
    make_tree(tmp_path)
    full, _ = scan_firmware_assets(str(tmp_path))
    sub, _ = scan_firmware_subtree(str(tmp_path), str(tmp_path))
    assert sub == full


def test_subtree_scan_rejects_outside_subtree(tmp_path: Path) -> None:
    make_tree(tmp_path)
    outside = tmp_path.parent / "外部子树"
    with pytest.raises(PathGuardError):
        scan_firmware_subtree(str(tmp_path), str(outside))


def test_subtree_scan_missing_subtree_returns_empty_snapshot(tmp_path: Path) -> None:
    make_tree(tmp_path)
    gone = tmp_path / "L36程序" / "已删除型号"
    sub, errors = scan_firmware_subtree(str(tmp_path), str(gone))
    assert sub == []
    assert errors == []


def test_subtree_scan_rejects_subtree_not_directory(tmp_path: Path) -> None:
    make_tree(tmp_path)
    a_file = tmp_path / "说明.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        scan_firmware_subtree(str(tmp_path), str(a_file))


def test_subtree_scan_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        scan_firmware_subtree(str(tmp_path / "不存在"), str(tmp_path / "不存在" / "子树"))


def test_subtree_scan_respects_cancel_event(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()
    sub, issues = scan_firmware_subtree(
        str(tmp_path), str(root), cancel_event=cancel_event
    )
    assert issues == [
        {"severity": "error", "message": "扫描已被用户取消", "path": ""}
    ]
    assert sub == []


def test_subtree_scan_excludes_configured_keywords(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    _write(root / "CH341SER" / "主板程序X" / "ITE_NOR_L36_v9.bin")
    sub, errors = scan_firmware_subtree(str(tmp_path), str(root))
    assert errors == []
    assert all("CH341SER" not in a["path"] for a in sub)


def test_subtree_scan_passes_custom_catalog(tmp_path: Path) -> None:
    """自定义 catalog_path 必须透传：类型全部不匹配时返回空结果。"""
    root = make_tree(tmp_path)
    catalog = tmp_path / "custom_catalog.toml"
    catalog.write_text(
        "\n".join(
            [
                "[[firmware_types]]",
                'key = "custom"',
                'label = "自定义"',
                'dir_keywords = ["无匹配关键词"]',
                'file_extensions = [".zzz"]',
                'flash_mode = "tool_launch"',
                'tool_name = ""',
                'tool_path = ""',
                "enabled = true",
            ]
        ),
        encoding="utf-8",
    )
    sub, errors = scan_firmware_subtree(
        str(tmp_path), str(root), catalog_path=catalog
    )
    assert errors == []
    assert sub == []
    # 同一子树用默认 catalog 仍有资产，证明 catalog 确实生效
    sub_default, _ = scan_firmware_subtree(str(tmp_path), str(root))
    assert sub_default


def test_subtree_scan_reports_walk_errors(tmp_path: Path, monkeypatch) -> None:
    """目录读取失败经 onerror 汇入 error 级 issue 返回，不抛异常。"""
    root = make_tree(tmp_path)
    real_walk = os.walk

    def failing_walk(start, *args, **kwargs):
        kwargs["onerror"](OSError(13, "Permission denied", str(root / "受保护目录")))
        yield from real_walk(start, *args, **kwargs)

    monkeypatch.setattr(file_scan.os, "walk", failing_walk)
    sub, issues = scan_firmware_subtree(str(tmp_path), str(root))
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert "Permission denied" in issues[0]["message"]
    assert "受保护目录" in issues[0]["message"]
    assert issues[0]["path"] == str(root / "受保护目录")
    assert isinstance(sub, list)
