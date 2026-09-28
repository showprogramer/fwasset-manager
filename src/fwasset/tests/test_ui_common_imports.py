from __future__ import annotations


def test_qt_shared_models_live_in_framework_neutral_package():
    from fwasset.ui_common.view_models.scheme_workbench_model import (
        SchemeWorkbenchModel,
    )
    from fwasset.ui_common.workbench_helpers import shared_register_action_label

    assert SchemeWorkbenchModel.__module__.startswith("fwasset.ui_common.")
    assert shared_register_action_label("快捷键程序") == "为「快捷键程序」关联其他型号的程序…"
