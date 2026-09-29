from __future__ import annotations

from fwasset.ui_common.workbench_helpers import (
    breadcrumb_text,
    flash_mode_label,
    relative_time_text,
    set_default_action_label,
    shared_conflict_prompt_message,
    shared_register_action_label,
    shared_replace_action_label,
    shared_source_picker_caption,
    shared_unregister_action_label,
    source_tag,
    usb_copy_notice,
)


def test_framework_neutral_helpers_keep_operator_facing_labels() -> None:
    assert flash_mode_label("tool_launch") == "工具烧录"
    assert set_default_action_label("L36", "蓝牙程序") == "设为「L36」蓝牙程序默认版本"
    assert shared_register_action_label("快捷键程序") == "为「快捷键程序」关联其他型号的程序…"
    assert shared_replace_action_label("快捷键程序") == "更换「快捷键程序」的关联…"
    assert shared_unregister_action_label("快捷键程序") == "解除「快捷键程序」的关联"
    assert "已有关联" in shared_conflict_prompt_message("快捷键程序")
    assert shared_source_picker_caption("快捷键程序") == (
        "从其他型号选择一个「快捷键程序」程序进行关联"
    )


def test_source_tag_marks_borrowed_and_missing_rows() -> None:
    assert source_tag("通用默认", "common") == ("通用", "accent")
    assert source_tag("", "custom") == ("定制专属", "accent")
    assert source_tag("客户A", "custom") == ("客户A", "accent")
    assert source_tag("通用", "common", "shared_hit", "自动更新·L50S") == (
        "来自 L50S",
        "warning",
    )
    assert source_tag("通用", "common", "shared_hit", "来自L50S") == ("来自 L50S", "warning")
    assert source_tag("通用", "common", "shared_missing") == ("关联缺失", "error")


def test_relative_time_and_breadcrumb_texts() -> None:
    assert relative_time_text(-5) == "刚刚"
    assert relative_time_text(59) == "刚刚"
    assert relative_time_text(125) == "2 分钟前"
    assert relative_time_text(7200) == "2 小时前"
    assert relative_time_text(3 * 86400) == "3 天前"
    assert breadcrumb_text("all") == "全部程序"
    assert breadcrumb_text("common_type", common_type="手控UI") == "通用程序 › 手控UI"
    assert breadcrumb_text("custom_scheme", scheme_name="客户A") == "定制程序 › 客户A"
    assert breadcrumb_text("") == ""


def test_usb_copy_notice_reports_eject_outcome() -> None:
    assert usb_copy_notice({"format_ok": True}) is None
    assert usb_copy_notice({}) is None
    assert usb_copy_notice({"ejected": True}) == (
        "已复制到 U 盘",
        "U 盘已安全弹出，可以拔出",
        "success",
    )
    title, content, level = usb_copy_notice({"ejected": False})  # type: ignore[misc]
    assert level == "warning" and "手动" not in title and "安全弹出" in content
    _, content, level = usb_copy_notice(  # type: ignore[misc]
        {"ejected": False, "eject_requested": False}
    )
    assert level == "info" and "未自动弹出" in content
