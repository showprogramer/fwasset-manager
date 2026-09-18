"""待补齐候选期元数据读写测试（TASK-20260918 子任务 5，父规格 D7.5）。

覆盖 ``import_state`` / ``intended_firmware_type`` 的写入、读取与补齐后清除。
``vendor`` 相关行为见 test_vendor_metadata.py；这里只测候选期新增的两个键
以及它们与既有键、未知键、注释的共存。
"""

from __future__ import annotations

from pathlib import Path

from fwasset.core.asset_info import (
    clear_candidate_metadata,
    import_state_from_asset_info,
    intended_firmware_type_from_asset_info,
    load_asset_info_with_status,
    save_candidate_metadata,
    save_vendor,
    vendor_from_asset_info,
)
from fwasset.core.managed_paths import ASSET_METADATA_FILENAME


def _read(asset_dir: Path) -> dict:
    data, status, error = load_asset_info_with_status(asset_dir)
    assert status == "ok", error
    return data


def test_save_candidate_metadata_writes_three_keys(tmp_path: Path) -> None:
    status, error = save_candidate_metadata(
        tmp_path, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )

    assert (status, error) == ("ok", "")
    data = _read(tmp_path)
    assert vendor_from_asset_info(data) == "摩众"
    assert import_state_from_asset_info(data) == "incomplete"
    assert intended_firmware_type_from_asset_info(data) == "handcontrol_ui"


def test_empty_intended_type_is_not_written(tmp_path: Path) -> None:
    """来源无法判断意图类型是允许的，候选仍靠 import_state 被识别。"""
    save_candidate_metadata(tmp_path, vendor="摩众", intended_firmware_type="")

    data = _read(tmp_path)
    assert import_state_from_asset_info(data) == "incomplete"
    assert "intended_firmware_type" not in data


def test_clear_candidate_metadata_removes_keys_and_keeps_vendor(tmp_path: Path) -> None:
    """Q1 裁决：补齐后删除候选键，不置 complete；vendor 保留。"""
    save_candidate_metadata(
        tmp_path, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )

    status, error = clear_candidate_metadata(tmp_path)

    assert (status, error) == ("ok", "")
    data = _read(tmp_path)
    assert vendor_from_asset_info(data) == "摩众"
    assert "import_state" not in data
    assert "intended_firmware_type" not in data
    assert import_state_from_asset_info(data) == ""


def test_clear_on_plain_asset_info_is_noop(tmp_path: Path) -> None:
    """普通程序信息（从没有候选键）清除是空操作，不损坏文件。"""
    save_vendor(tmp_path, "国瑞")

    status, _error = clear_candidate_metadata(tmp_path)

    assert status == "ok"
    assert vendor_from_asset_info(_read(tmp_path)) == "国瑞"


def test_candidate_keys_preserve_unknown_keys_and_comments(tmp_path: Path) -> None:
    """D6.2「解析保留未知键」在候选键写入与清除两侧都成立。"""
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    toml_path.write_text(
        '# 顶部注释\nvendor = "旧厂商"\nnote = "自定义键"\n\n[extra]\ninner = 1\n',
        encoding="utf-8",
    )

    save_candidate_metadata(
        tmp_path, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )
    after_write = toml_path.read_text(encoding="utf-8")
    assert "# 顶部注释" in after_write
    assert 'note = "自定义键"' in after_write
    assert "[extra]" in after_write

    clear_candidate_metadata(tmp_path)
    after_clear = toml_path.read_text(encoding="utf-8")
    assert "# 顶部注释" in after_clear
    assert 'note = "自定义键"' in after_clear
    assert "[extra]" in after_clear
    assert "import_state" not in after_clear
    data = _read(tmp_path)
    assert data["extra"] == {"inner": 1}
    assert vendor_from_asset_info(data) == "摩众"


def test_nested_same_named_key_untouched(tmp_path: Path) -> None:
    """只动顶层：table 内的同名键属未知键，写入与清除都不得碰。"""
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    toml_path.write_text(
        '[nested]\nimport_state = "incomplete"\n',
        encoding="utf-8",
    )

    save_candidate_metadata(tmp_path, vendor="摩众", intended_firmware_type="")
    clear_candidate_metadata(tmp_path)

    data = _read(tmp_path)
    assert data["nested"]["import_state"] == "incomplete"
    assert "import_state" not in data


def test_invalid_import_state_value_is_returned_verbatim(tmp_path: Path) -> None:
    """值级宽松但不静默归一：非法值原样返回，由候选扫描产出诊断。"""
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    toml_path.write_text('import_state = "不认识的值"\n', encoding="utf-8")

    assert import_state_from_asset_info(_read(tmp_path)) == "不认识的值"


def test_non_string_values_read_as_empty(tmp_path: Path) -> None:
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    toml_path.write_text(
        "import_state = 123\nintended_firmware_type = true\n", encoding="utf-8"
    )

    data = _read(tmp_path)
    assert import_state_from_asset_info(data) == ""
    assert intended_firmware_type_from_asset_info(data) == ""


def test_parse_error_refuses_candidate_write_and_keeps_file(tmp_path: Path) -> None:
    """解析损坏不静默覆盖（D6.2），原文件逐字节保留。"""
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    broken = 'vendor = "未闭合\n'
    toml_path.write_text(broken, encoding="utf-8")

    status, error = save_candidate_metadata(
        tmp_path, vendor="摩众", intended_firmware_type="handcontrol_ui"
    )

    assert status == "parse_error"
    assert error
    assert toml_path.read_text(encoding="utf-8") == broken


def test_parse_error_refuses_clear_and_keeps_file(tmp_path: Path) -> None:
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    broken = "import_state = [未闭合\n"
    toml_path.write_text(broken, encoding="utf-8")

    status, _error = clear_candidate_metadata(tmp_path)

    assert status == "parse_error"
    assert toml_path.read_text(encoding="utf-8") == broken


def test_clearing_only_key_leaves_empty_file_readable(tmp_path: Path) -> None:
    """删掉唯一的键后文件为空，仍应可读为空文档（不留孤零零的换行）。"""
    toml_path = tmp_path / ASSET_METADATA_FILENAME
    toml_path.write_text('import_state = "incomplete"\n', encoding="utf-8")

    clear_candidate_metadata(tmp_path)

    assert toml_path.read_text(encoding="utf-8") == ""
    data, status, _error = load_asset_info_with_status(tmp_path)
    assert status == "ok"
    assert data == {}
