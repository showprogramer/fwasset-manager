from __future__ import annotations

from fwasset.ui_common.workbench_helpers import (
    flash_mode_label,
    model_chip_values,
    set_default_action_label,
    shared_conflict_prompt_message,
    shared_register_action_label,
    shared_replace_action_label,
    shared_source_picker_caption,
    shared_unregister_action_label,
)


def test_framework_neutral_helpers_keep_operator_facing_labels() -> None:
    assert flash_mode_label("tool_launch") == "工具烧录"
    assert model_chip_values(["L36", "L50S"], selected="L50S") == (["L36", "L50S"], [])
    assert set_default_action_label("L36", "蓝牙程序") == "设为「L36」蓝牙程序默认版本"
    assert shared_register_action_label("快捷键程序") == "为「快捷键程序」登记借用…"
    assert shared_replace_action_label("快捷键程序") == "更换「快捷键程序」的借用…"
    assert shared_unregister_action_label("快捷键程序") == "解除「快捷键程序」的借用"
    assert "已有借用登记" in shared_conflict_prompt_message("快捷键程序")
    assert shared_source_picker_caption("快捷键程序") == (
        "从其它型号选择一个「快捷键程序」程序作为借用"
    )
