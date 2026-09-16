"""TASK-20260916 chassis_type 生产链与扫描诊断分级测试（父规格 D0.1 / D0.1a）。

覆盖：单块枚举写入、多块 / 非枚举 name / missing 留空无 issue、
parse_error / parser_missing 警告、单型号与多型号布局归属、
全量与子树字段级一致、同一型号根单次扫描只读一次盘。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import fwasset.core.file_scan as file_scan
import fwasset.core.platform_config as platform_config
from fwasset.core.file_scan import scan_firmware_assets, scan_firmware_subtree
from fwasset.core.types import FirmwareAsset

VALID_TOML = '[[platform]]\nname = "单3D"\n'


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_multi_model_tree(root: Path, *, l36_toml: str | None = VALID_TOML) -> Path:
    """多型号布局：L36程序（可带 平台配置.toml）+ 无标志的 L50程序。"""
    _write(root / "L36程序" / "通用" / "主板程序A" / "ITE_NOR_L36_v1.0.0.bin")
    _write(root / "L36程序" / "通用" / "主板程序B" / "ITE_NOR_L36_v2.0.0.bin")
    if l36_toml is not None:
        _write(root / "L36程序" / "平台配置.toml", l36_toml)
    _write(root / "L50程序" / "通用" / "主板程序C" / "ITE_NOR_L50_v3.0.0.bin")
    return root / "L36程序"


def _chassis_by_path(assets: list[FirmwareAsset]) -> dict[str, str]:
    return {a["path"]: str(a["chassis_type"]) for a in assets}


# --- 单块枚举 / 多块 / 非枚举 name / missing ------------------------------


def test_single_enum_block_writes_chassis_type(tmp_path: Path) -> None:
    make_multi_model_tree(tmp_path)
    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    chassis = _chassis_by_path(assets)
    a_path = str(tmp_path / "L36程序" / "通用" / "主板程序A")
    c_path = str(tmp_path / "L50程序" / "通用" / "主板程序C")
    assert chassis[a_path] == "单3D"
    assert chassis[str(tmp_path / "L36程序" / "通用" / "主板程序B")] == "单3D"
    # L50程序 是型号根但无 平台配置.toml（missing）→ "" 且无 issue
    assert chassis[c_path] == ""


def test_single_model_layout_root_is_model_root(tmp_path: Path) -> None:
    """单型号布局（根即型号）：归属到工作区根，读取根上的 平台配置.toml。"""
    _write(tmp_path / "通用" / "主板程序A" / "ITE_NOR_L36_v1.0.0.bin")
    _write(tmp_path / "平台配置.toml", '[[platform]]\nname = "上3D下2D"\n')

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert len(assets) == 1
    assert assets[0]["chassis_type"] == "上3D下2D"


def test_missing_config_yields_empty_without_issue(tmp_path: Path) -> None:
    """型号根存在但无 平台配置.toml → "" 且无 issue。"""
    make_multi_model_tree(tmp_path, l36_toml=None)

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert set(_chassis_by_path(assets).values()) == {""}


def test_multi_block_config_yields_empty_without_issue(tmp_path: Path) -> None:
    make_multi_model_tree(
        tmp_path,
        l36_toml='[[platform]]\nname = "单3D"\n[[platform]]\nname = "单2D"\n',
    )

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert set(_chassis_by_path(assets).values()) == {""}


def test_non_enum_name_yields_empty_without_issue(tmp_path: Path) -> None:
    """单块但 name 不属于 ChassisType 枚举（legacy 平台名）→ "" 且无 issue。"""
    make_multi_model_tree(tmp_path, l36_toml='[[platform]]\nname = "双机芯-上3D-下2D"\n')

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert set(_chassis_by_path(assets).values()) == {""}


def test_config_without_platform_table_yields_empty_without_issue(
    tmp_path: Path,
) -> None:
    make_multi_model_tree(tmp_path, l36_toml="# 仅注释，无 platform 块\n")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert set(_chassis_by_path(assets).values()) == {""}


# --- parse_error / parser_missing → warning -------------------------------


def test_parse_error_yields_warning_issue_once_per_model_root(
    tmp_path: Path,
) -> None:
    """损坏 TOML → "" + warning 级 issue；同一型号根两个资产只告警一次。"""
    make_multi_model_tree(tmp_path, l36_toml="platform = [ 损坏")

    assets, issues = scan_firmware_assets(str(tmp_path))

    # 资产仍全部扫出，机芯留空
    assert len(assets) == 3
    assert set(_chassis_by_path(assets).values()) == {""}
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "warning"
    assert "parse_error" in issue["message"]
    assert issue["path"] == str(tmp_path / "L36程序" / "平台配置.toml")


def test_parser_missing_yields_warning_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_multi_model_tree(tmp_path)
    monkeypatch.setattr(platform_config, "tomllib", None)

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert set(_chassis_by_path(assets).values()) == {""}
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert "parser_missing" in issues[0]["message"]
    assert issues[0]["path"] == str(tmp_path / "L36程序" / "平台配置.toml")


# --- 只读一次盘（缓存）-----------------------------------------------------


def test_chassis_config_read_once_per_model_root_per_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一型号根单次扫描只读一次盘；两个型号根共两次调用。"""
    make_multi_model_tree(tmp_path)
    real_load = file_scan.load_platform_config_strict
    calls: list[Path] = []

    def counting_load(model_root: Path):
        calls.append(Path(model_root))
        return real_load(model_root)

    monkeypatch.setattr(file_scan, "load_platform_config_strict", counting_load)

    _assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    # L36 根 2 个资产只触发一次配置读取（无缓存会是 3 次）；L50 根 1 次
    assert sorted(p.name for p in calls) == ["L36程序", "L50程序"]


def test_asset_without_any_model_root_reads_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯 legacy 布局（无任何型号根）：chassis 为 ""，不读任何 平台配置.toml。"""
    _write(tmp_path / "L36主板程序" / "主板程序_v1" / "ITE_NOR_L36_v1.0.0.bin")
    real_load = file_scan.load_platform_config_strict
    calls: list[Path] = []

    def counting_load(model_root: Path):
        calls.append(Path(model_root))
        return real_load(model_root)

    monkeypatch.setattr(file_scan, "load_platform_config_strict", counting_load)

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert issues == []
    assert len(assets) == 1
    assert assets[0]["chassis_type"] == ""
    assert calls == []


# --- 全量与子树字段级一致（含 chassis）-------------------------------------


def test_full_and_subtree_field_level_consistent_with_chassis(
    tmp_path: Path,
) -> None:
    """L36 配置合法 + L50 配置损坏：全量与子树对同一路径字段级一致。"""
    make_multi_model_tree(tmp_path)
    _write(tmp_path / "L50程序" / "平台配置.toml", "platform = [ 损坏")

    full, full_issues = scan_firmware_assets(str(tmp_path))
    sub, sub_issues = scan_firmware_subtree(str(tmp_path), str(tmp_path / "L36程序"))

    boundary = str(tmp_path / "L36程序")
    expected = [
        a
        for a in full
        if a["path"] == boundary or a["path"].startswith(boundary + os.sep)
    ]
    assert sub == expected
    assert sub
    assert {a["chassis_type"] for a in sub} == {"单3D"}
    # 子树诊断只含本子树：L36 无 issue；L50 的 warning 不属于该子树
    assert sub_issues == []
    assert [i["path"] for i in full_issues] == [
        str(tmp_path / "L50程序" / "平台配置.toml")
    ]


def test_subtree_scan_single_model_layout_chassis(tmp_path: Path) -> None:
    """单型号布局子树扫描：归属到工作区根，chassis 与全量一致。"""
    _write(tmp_path / "通用" / "主板程序A" / "ITE_NOR_L36_v1.0.0.bin")
    _write(tmp_path / "平台配置.toml", VALID_TOML)

    full, _ = scan_firmware_assets(str(tmp_path))
    sub, _ = scan_firmware_subtree(str(tmp_path), str(tmp_path))

    assert sub == full
    assert {a["chassis_type"] for a in sub} == {"单3D"}
