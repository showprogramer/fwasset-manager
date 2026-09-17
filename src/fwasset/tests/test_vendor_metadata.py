"""TASK-20260917 vendor 元数据生产链测试（父规格 D6.1–D6.3）。

覆盖：程序信息.toml 五态扫描行为、save_vendor 原子合并写（保留未知键 /
损坏拒绝）、全量与子树字段级一致、厂商名单读取分支。
schema v4 部分见 test_asset_index.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fwasset.core.asset_info as asset_info
import fwasset.core.settings as settings
from fwasset.core.asset_info import (
    load_asset_info_with_status,
    save_vendor,
    vendor_from_asset_info,
)
from fwasset.core.file_scan import scan_firmware_assets, scan_firmware_subtree
from fwasset.core.managed_paths import ASSET_METADATA_FILENAME
from fwasset.core.settings import (
    DEFAULT_VENDORS,
    load_vendor_candidates,
    normalize_vendor_list,
)
from fwasset.core.types import FirmwareAsset


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_asset_dir(root: Path, name: str) -> Path:
    asset_dir = root / "L36程序" / "通用" / name
    _write(asset_dir / "ITE_NOR_L36_v1.0.0.bin", "x")
    return asset_dir


def _vendor_by_path(assets: list[FirmwareAsset]) -> dict[str, str]:
    return {a["path"]: str(a["vendor"]) for a in assets}


# --- asset_info：load / vendor_from_asset_info -----------------------------


def test_load_asset_info_missing(tmp_path: Path) -> None:
    data, status, error = load_asset_info_with_status(tmp_path)

    assert data == {}
    assert status == "missing"
    assert error == ""


def test_load_asset_info_ok_with_vendor(tmp_path: Path) -> None:
    _write(tmp_path / ASSET_METADATA_FILENAME, 'vendor = "摩众"\n')

    data, status, error = load_asset_info_with_status(tmp_path)

    assert status == "ok"
    assert error == ""
    assert data == {"vendor": "摩众"}


def test_load_asset_info_parse_error(tmp_path: Path) -> None:
    _write(tmp_path / ASSET_METADATA_FILENAME, 'vendor = \n"broken')

    data, status, error = load_asset_info_with_status(tmp_path)

    assert data == {}
    assert status == "parse_error"
    assert error


def test_load_asset_info_parser_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / ASSET_METADATA_FILENAME, 'vendor = "摩众"\n')
    monkeypatch.setattr(asset_info, "tomllib", None)

    data, status, error = load_asset_info_with_status(tmp_path)

    assert data == {}
    assert status == "parser_missing"
    assert "tomli" in error


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"vendor": "摩众"}, "摩众"),
        ({}, ""),
        ({"vendor": ""}, ""),
        ({"vendor": 123}, ""),
        ({"vendor": ["摩众"]}, ""),
        ({"other": "x"}, ""),
    ],
)
def test_vendor_from_asset_info_value_level_leniency(
    data: dict[str, Any], expected: str
) -> None:
    assert vendor_from_asset_info(data) == expected


# --- asset_info：save_vendor ------------------------------------------------


def test_save_vendor_starts_from_missing_file(tmp_path: Path) -> None:
    status, error = save_vendor(tmp_path, "摩众")

    assert status == "ok"
    assert error == ""
    data, load_status, _ = load_asset_info_with_status(tmp_path)
    assert load_status == "ok"
    assert data == {"vendor": "摩众"}


def test_save_vendor_replaces_existing_top_level_vendor(tmp_path: Path) -> None:
    info = tmp_path / ASSET_METADATA_FILENAME
    _write(info, '# 备注\nvendor = "摩众"\nowner = "张三"\n')

    status, _error = save_vendor(tmp_path, "国瑞")

    assert status == "ok"
    text = info.read_text(encoding="utf-8")
    assert 'vendor = "国瑞"' in text
    assert 'owner = "张三"' in text
    assert "# 备注" in text
    data, load_status, _ = load_asset_info_with_status(tmp_path)
    assert load_status == "ok"
    assert data["vendor"] == "国瑞"
    assert data["owner"] == "张三"


def test_save_vendor_preserves_unknown_keys_and_tables(tmp_path: Path) -> None:
    """未知键、注释与嵌套 table 原样保留；vendor 插入顶层范围。"""
    info = tmp_path / ASSET_METADATA_FILENAME
    _write(info, '# 应用注释\nowner = "张三"\n[extra]\nkey = "value"\n')

    status, _error = save_vendor(tmp_path, "亿微")

    assert status == "ok"
    data, load_status, _ = load_asset_info_with_status(tmp_path)
    assert load_status == "ok"
    assert data == {"owner": "张三", "vendor": "亿微", "extra": {"key": "value"}}


def test_save_vendor_escapes_toml_special_chars(tmp_path: Path) -> None:
    status, _error = save_vendor(tmp_path, '明"锐\\A')

    assert status == "ok"
    data, load_status, _ = load_asset_info_with_status(tmp_path)
    assert load_status == "ok"
    assert data["vendor"] == '明"锐\\A'


def test_save_vendor_roundtrips_control_chars(tmp_path: Path) -> None:
    status, _error = save_vendor(tmp_path, "摩\n众\r亿\t微")

    assert status == "ok"
    data, load_status, _ = load_asset_info_with_status(tmp_path)
    assert load_status == "ok"
    assert data["vendor"] == "摩\n众\r亿\t微"


def test_save_vendor_rejects_corrupt_file_and_keeps_original(
    tmp_path: Path,
) -> None:
    info = tmp_path / ASSET_METADATA_FILENAME
    original = 'vendor = \n"broken'
    _write(info, original)

    status, error = save_vendor(tmp_path, "摩众")

    assert status == "parse_error"
    assert error
    assert info.read_text(encoding="utf-8") == original


def test_save_vendor_rejects_when_parser_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / ASSET_METADATA_FILENAME
    _write(info, 'vendor = "摩众"\n')
    monkeypatch.setattr(asset_info, "tomllib", None)

    status, error = save_vendor(tmp_path, "国瑞")

    assert status == "parser_missing"
    assert "tomli" in error
    assert info.read_text(encoding="utf-8") == 'vendor = "摩众"\n'


# --- file_scan：五态扫描行为 ------------------------------------------------


def test_scan_vendor_five_states(tmp_path: Path) -> None:
    valid = _make_asset_dir(tmp_path, "主板程序A")
    _write(valid / ASSET_METADATA_FILENAME, 'vendor = "摩众"\n')
    no_file = _make_asset_dir(tmp_path, "主板程序B")
    no_key = _make_asset_dir(tmp_path, "主板程序C")
    _write(no_key / ASSET_METADATA_FILENAME, 'owner = "张三"\n')
    non_string = _make_asset_dir(tmp_path, "主板程序D")
    _write(non_string / ASSET_METADATA_FILENAME, "vendor = 123\n")
    corrupt = _make_asset_dir(tmp_path, "主板程序E")
    _write(corrupt / ASSET_METADATA_FILENAME, 'vendor = \n"broken')

    assets, issues = scan_firmware_assets(str(tmp_path))

    vendor = _vendor_by_path(assets)
    assert len(assets) == 5
    assert vendor[str(valid)] == "摩众"
    assert vendor[str(no_file)] == ""
    assert vendor[str(no_key)] == ""
    assert vendor[str(non_string)] == ""
    assert vendor[str(corrupt)] == ""
    # 仅损坏文件产生 warning，path 为该文件完整路径
    assert len(issues) == 1
    issue = issues[0]
    assert issue["severity"] == "warning"
    assert issue["path"] == str(corrupt / ASSET_METADATA_FILENAME)
    assert "厂商" in issue["message"]


def test_scan_vendor_parser_missing_yields_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_dir = _make_asset_dir(tmp_path, "主板程序A")
    _write(asset_dir / ASSET_METADATA_FILENAME, 'vendor = "摩众"\n')
    monkeypatch.setattr(asset_info, "tomllib", None)

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert _vendor_by_path(assets)[str(asset_dir)] == ""
    assert len(issues) == 1
    assert issues[0]["severity"] == "warning"
    assert issues[0]["path"] == str(asset_dir / ASSET_METADATA_FILENAME)


def test_scan_vendor_full_and_subtree_field_level_consistent(
    tmp_path: Path,
) -> None:
    a = _make_asset_dir(tmp_path, "主板程序A")
    _write(a / ASSET_METADATA_FILENAME, 'vendor = "摩众"\n')
    _make_asset_dir(tmp_path, "主板程序B")
    model_root = tmp_path / "L36程序"

    full, _full_issues = scan_firmware_assets(str(tmp_path))
    sub, _sub_issues = scan_firmware_subtree(str(tmp_path), str(model_root))

    assert _vendor_by_path(full) == _vendor_by_path(sub)
    assert _vendor_by_path(sub)[str(a)] == "摩众"


def test_scan_vendor_issue_at_most_once_per_asset(tmp_path: Path) -> None:
    asset_dir = _make_asset_dir(tmp_path, "主板程序A")
    _write(asset_dir / ASSET_METADATA_FILENAME, "not = [valid\n")

    assets, issues = scan_firmware_assets(str(tmp_path))

    assert len(assets) == 1
    vendor_issues = [
        i for i in issues if i["path"] == str(asset_dir / ASSET_METADATA_FILENAME)
    ]
    assert len(vendor_issues) == 1


# --- settings：厂商名单读取（D6.1）------------------------------------------


def _set_config(
    monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any], status: str = "ok"
) -> None:
    monkeypatch.setattr(settings, "CONFIG_LOAD_STATUS", status)
    monkeypatch.setattr(settings, "_cfg", cfg)


def test_normalize_vendor_list_trims_and_dedupes_casefold() -> None:
    assert normalize_vendor_list(
        [" 摩众 ", "摩众", "", "  ", "MZ", "mz ", "国瑞"]
    ) == ["摩众", "MZ", "国瑞"]


def test_load_vendor_candidates_defaults_when_key_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(monkeypatch, {})

    assert load_vendor_candidates() == DEFAULT_VENDORS == [
        "摩众",
        "国瑞",
        "亿微",
        "明锐",
    ]


@pytest.mark.parametrize(
    "bad_value",
    ["摩众", {"vendor": "摩众"}, ["摩众", 123], ["摩众", None]],
)
def test_load_vendor_candidates_falls_back_on_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: Any
) -> None:
    _set_config(monkeypatch, {"vendors": bad_value})

    assert load_vendor_candidates() == DEFAULT_VENDORS


def test_load_vendor_candidates_falls_back_on_broken_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(monkeypatch, {"vendors": ["摩众"]}, status="parse_error")

    assert load_vendor_candidates() == DEFAULT_VENDORS


def test_load_vendor_candidates_returns_empty_list_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(monkeypatch, {"vendors": []})

    assert load_vendor_candidates() == []


def test_load_vendor_candidates_normalizes_configured_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(monkeypatch, {"vendors": [" 摩众 ", "摩众", "国瑞"]})

    assert load_vendor_candidates() == ["摩众", "国瑞"]
