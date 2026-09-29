"""Qt 工作台 smoke 测试（REVIEW-20260708-pyside6-p1-p2 阻断项 3.2）。

契约级兜底：导入可用、DataGrid 渲染语义（单变体折叠 / 多变体归组 /
★默认徽章 / 归属文案）、运行日志缓冲。用 offscreen 平台跑，
无显示器也能执行，但仍标 ui（依赖 PySide6，qt extra 未装时自动跳过）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fwasset.ui_common.view_models.scheme_workbench_model import (  # noqa: E402
    ModuleCardData,
    ModuleRow,
    ModuleVariant,
)
from fwasset.ui_qt.data_grid import DataGrid  # noqa: E402
from fwasset.ui_qt.log_panel import ActivityLog, LogView  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _asset(
    path: str = "D:/x/主板程序/量产_默认", files: list[str] | None = None
) -> dict:
    return {"path": path, "files": files or ["fw_V40.bin"], "category": "common"}


def _variant(name: str, badge: str = "", kind: str = "common") -> ModuleVariant:
    return ModuleVariant(
        asset=_asset(),
        name=name,
        version="V40",
        source_kind=kind,
        source_label="通用" if kind == "common" else "定制专属",
        default_badge=badge,
    )


def _logs() -> list[str]:
    return []


def test_import_workbench_window_module(qapp) -> None:
    """入口模块可导入（不实例化窗口——那需要扫描环境）。"""
    import fwasset.ui_qt.workbench_window as module

    assert hasattr(module, "QtWorkbenchWindow")
    assert hasattr(module, "main")


def _fake_gate(ok: bool) -> dict:
    """测试替身：跳过真实门闩（TASK-20260806 R1/R10 单测在 test_workbench_helpers）。"""
    if ok:
        return {"ok": True, "code": "ok", "message": "", "payload": {}}
    return {
        "ok": False,
        "code": "not_configured",
        "message": "请先在设置中配置程序文件夹",
        "payload": {},
    }


def _capture_workbench_context_menu(
    qapp,
    monkeypatch,
    *,
    is_default: bool,
    shared: bool,
    gate_ok: bool = True,
    shared_state: str = "local",
    borrowed_only: bool = False,
) -> tuple[list[QAction], int]:
    """构建工作台资产右键菜单，并返回其中的动作列表。"""
    from PySide6.QtCore import QPoint
    from qfluentwidgets import RoundMenu

    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    workbench = WorkbenchInterface()
    workbench.current_selection.model_name = "L36"
    monkeypatch.setattr(
        workbench.workbench_model, "is_model_module_default", lambda _asset: is_default
    )
    monkeypatch.setattr(
        workbench.workbench_model,
        "resolve_shared_module",
        lambda _model, _module: {"source": "L50S"} if shared else None,
    )
    from fwasset.ui_qt import workbench_window as window_module

    monkeypatch.setattr(
        window_module, "write_gate_check", lambda *_a, **_k: _fake_gate(gate_ok)
    )
    captured: dict[str, RoundMenu] = {}
    separator_count = 0
    original_add_separator = RoundMenu.addSeparator

    def _add_separator(menu: RoundMenu) -> None:
        nonlocal separator_count
        separator_count += 1
        original_add_separator(menu)

    monkeypatch.setattr(RoundMenu, "addSeparator", _add_separator)
    monkeypatch.setattr(
        RoundMenu, "exec", lambda menu, _pos: captured.setdefault("menu", menu)
    )
    variant = ModuleVariant(
        asset={
            "category": "common",
            "firmware_label": "快捷键程序",
            "path": "D:/L36/通用/快捷键/量产_默认",
        },
        name="量产_默认",
        version="V1.12",
        source_kind="common",
        source_label="通用",
    )
    variant.shared_state = shared_state
    variant.borrowed_only = borrowed_only

    workbench._on_grid_right_click(variant, QPoint(0, 0))
    return captured["menu"].actions(), separator_count


def test_context_menu_hides_default_status_and_uses_replace_actions(
    qapp, monkeypatch
) -> None:
    """默认状态由表格徽章表达；已共享时菜单只给更换与取消操作。"""
    from qfluentwidgets import FluentIcon

    actions, separator_count = _capture_workbench_context_menu(
        qapp,
        monkeypatch,
        is_default=True,
        shared=True,
    )
    visible = [action for action in actions if not action.isSeparator()]

    assert [action.text() for action in visible] == [
        "更换「快捷键程序」的关联…",
        "解除「快捷键程序」的关联",
        "打开所在目录",
        "复制目录路径",
        "删除",
    ]
    assert [action.fluentIcon for action in visible] == [
        FluentIcon.SYNC,
        FluentIcon.CANCEL,
        FluentIcon.FOLDER,
        FluentIcon.COPY,
        FluentIcon.DELETE,
    ]
    assert all("已是" not in action.text() for action in visible)
    assert separator_count == 2


def test_context_menu_does_not_offer_setting_default(qapp, monkeypatch) -> None:
    actions, _ = _capture_workbench_context_menu(
        qapp, monkeypatch, is_default=False, shared=False
    )

    assert all("默认" not in action.text() for action in actions)


def test_context_menu_borrow_only_row_has_no_default_or_delete(
    qapp, monkeypatch
) -> None:
    """只借用的行 asset 在源型号：只给换借用/解除与目录操作（TASK-20260923）。"""
    for state in ("shared_hit", "shared_missing"):
        actions, _separators = _capture_workbench_context_menu(
            qapp,
            monkeypatch,
            is_default=False,
            shared=True,
            shared_state=state,
            borrowed_only=True,
        )
        texts = [a.text() for a in actions if not a.isSeparator()]
        assert texts == [
            "更换「快捷键程序」的关联…",
            "解除「快捷键程序」的关联",
            "打开所在目录",
            "复制目录路径",
        ]


def test_context_menu_groups_register_and_directory_actions(
    qapp, monkeypatch
) -> None:
    """未登记共享时，只显示借用、目录和删除操作。"""
    from qfluentwidgets import FluentIcon

    actions, separator_count = _capture_workbench_context_menu(
        qapp,
        monkeypatch,
        is_default=False,
        shared=False,
    )
    visible = [action for action in actions if not action.isSeparator()]

    assert [action.text() for action in visible] == [
        "为「快捷键程序」关联其他型号的程序…",
        "打开所在目录",
        "复制目录路径",
        "删除",
    ]
    assert [action.fluentIcon for action in visible] == [
        FluentIcon.LINK,
        FluentIcon.FOLDER,
        FluentIcon.COPY,
        FluentIcon.DELETE,
    ]
    assert separator_count == 2


def test_context_menu_hides_delete_on_borrowed_row(qapp, monkeypatch) -> None:
    """借用来源行不给删除——那份程序不属于本型号（TASK-20260923）。"""
    actions, _separator_count = _capture_workbench_context_menu(
        qapp,
        monkeypatch,
        is_default=False,
        shared=True,
        shared_state="shared_hit",
    )
    visible = [action for action in actions if not action.isSeparator()]
    assert "删除" not in [action.text() for action in visible]


def test_context_menu_hides_write_actions_when_gate_blocks(qapp, monkeypatch) -> None:
    """门闩拒绝（如未配置程序文件夹）时，菜单只保留只读操作。"""
    actions, _separator_count = _capture_workbench_context_menu(
        qapp,
        monkeypatch,
        is_default=False,
        shared=False,
        gate_ok=False,
    )
    visible = [action for action in actions if not action.isSeparator()]

    assert [action.text() for action in visible] == [
        "打开所在目录",
        "复制目录路径",
    ]


def test_shared_source_picker_excludes_other_modules(qapp, monkeypatch) -> None:
    """共享登记入口只传入其它型号的同模块来源。"""
    from fwasset.ui_qt import workbench_window as window_module
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    workbench = WorkbenchInterface()
    workbench.current_selection.model_name = "目标型号"
    monkeypatch.setattr(
        window_module, "write_gate_check", lambda *_a, **_k: _fake_gate(True)
    )
    target_asset = {"model": "目标型号", "firmware_label": "快捷键程序"}
    shortcut_source = {"model": "L36", "firmware_label": "快捷键程序"}
    bluetooth_source = {"model": "L36", "firmware_label": "蓝牙程序"}
    workbench.workbench_model._all_assets = [
        target_asset,
        shortcut_source,
        bluetooth_source,
    ]
    monkeypatch.setattr(
        workbench.workbench_model,
        "_model_of_asset",
        lambda asset: str(asset["model"]),
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        workbench,
        "_show_shared_source_picker",
        lambda *args: captured.update(args=args),
    )

    workbench._register_shared_source(
        SimpleNamespace(asset=target_asset, borrowed_only=False)
    )

    assert captured["args"] == (
        "目标型号",
        "快捷键程序",
        "快捷键程序",
        [shortcut_source],
    )


def test_shared_source_picker_requires_explicit_selection(qapp, monkeypatch) -> None:
    """登记对话框没有来源选择时禁用确认，且不提供跨模块切换。"""
    from PySide6.QtWidgets import QDialog, QListWidget, QPushButton
    from qfluentwidgets import PrimaryPushButton

    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    workbench = WorkbenchInterface()
    captured: dict[str, QDialog] = {}

    def _capture_exec(dialog: QDialog) -> int:
        captured["dialog"] = dialog
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", _capture_exec)
    workbench._show_shared_source_picker(
        "目标型号",
        "快捷键程序",
        "快捷键程序",
        [
            {
                "model": "L36",
                "directory_name": "量产_默认",
                "path": "D:/L36/快捷键/量产_默认",
            }
        ],
    )

    dialog = captured["dialog"]
    list_widget = dialog.findChild(QListWidget)
    assert list_widget is not None
    assert list_widget.count() == 1
    assert list_widget.currentRow() == -1
    confirm_button = next(
        button
        for button in dialog.findChildren(PrimaryPushButton)
        if button.text() == "确认登记"
    )
    assert confirm_button.isEnabled() is False
    assert all(
        "看全部" not in button.text() for button in dialog.findChildren(QPushButton)
    )

    list_widget.setCurrentRow(0)
    qapp.processEvents()
    assert confirm_button.isEnabled() is True
    from PySide6.QtWidgets import QComboBox, QLabel

    labels = [label.text() for label in dialog.findChildren(QLabel)]
    assert not any("更新方式" in text or "固定版本" in text for text in labels)
    assert dialog.findChild(QComboBox) is None
    registered: dict[str, object] = {}

    def _capture_register(*args: object) -> None:
        registered["mode"] = args[4]

    monkeypatch.setattr(workbench, "_do_register_shared", _capture_register)
    confirm_button.click()
    assert registered["mode"] == "follow_asset"


def test_setup_wizard_prefills_validates_and_writes_config(qapp, tmp_path) -> None:
    from fwasset.ui_qt.setup_wizard import SetupWizard

    wizard = SetupWizard(root_dir=str(tmp_path), tool_root="")

    assert wizard.root_dir() == str(tmp_path)
    assert wizard.tool_root() == ""
    assert wizard.validate_paths() == ""
    assert wizard.windowTitle() == "初始配置"

    settings_wizard = SetupWizard(
        root_dir=str(tmp_path), tool_root="", allow_skip=False
    )
    assert settings_wizard.windowTitle() == "程序文件夹设置"

    config_path = tmp_path / "config.toml"
    wizard.write_config(config_path)
    expected_root = str(tmp_path).replace("\\", "/")
    assert f'root_dir = "{expected_root}"' in config_path.read_text(encoding="utf-8")

    wizard._root_edit.setText("")
    assert "请选择固件根目录" in wizard.validate_paths()
    wizard._on_accept()
    assert wizard.result() == 0

    wizard._root_edit.setText(str(tmp_path))
    wizard._tool_edit.setText(str(tmp_path / "missing-tool"))
    assert "工具根目录不存在" in wizard.validate_paths()


def test_settings_wizard_blank_root_keeps_current_config(qapp, tmp_path) -> None:
    """修改场景（allow_skip=False）留空固件根目录时保持原值，不清空配置（方案 A）。"""
    from fwasset.ui_qt.setup_wizard import SetupWizard

    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    wizard = SetupWizard(root_dir=str(tmp_path), tool_root="", allow_skip=False)

    # 只改工具根目录，固件根目录留空
    wizard._root_edit.setText("")
    wizard._tool_edit.setText(str(tool_dir))

    assert wizard.validate_paths() == ""
    assert wizard.resolved_root_dir() == str(tmp_path)
    assert wizard.resolved_tool_root() == str(tool_dir)

    config_path = tmp_path / "config.toml"
    wizard.write_config(config_path)
    content = config_path.read_text(encoding="utf-8")
    assert f'root_dir = "{str(tmp_path).replace(chr(92), "/")}"' in content
    assert f'tool_root = "{str(tool_dir).replace(chr(92), "/")}"' in content

    # 修改场景不再显示首次配置的欢迎语
    assert "欢迎使用" not in _dialog_text(wizard)


def test_first_run_wizard_still_requires_root(qapp, tmp_path) -> None:
    """放宽校验只作用于修改场景；首次配置仍强制固件根目录有效。"""
    from fwasset.ui_qt.setup_wizard import SetupWizard

    wizard = SetupWizard(root_dir="", tool_root="", allow_skip=True)
    assert "请选择固件根目录" in wizard.validate_paths()
    assert wizard.resolved_root_dir() == ""
    assert "欢迎使用" in _dialog_text(wizard)


def _dialog_text(widget) -> str:
    from qfluentwidgets import BodyLabel

    return "\n".join(lbl.text() for lbl in widget.findChildren(BodyLabel))


def test_settings_interface_shows_paths_and_emits_configure_request(qapp) -> None:
    from fwasset.ui_qt.settings_interface import SettingsInterface

    panel = SettingsInterface()
    panel.set_paths("D:/firmware", "")
    assert panel.root_card.contentLabel.text() == "D:/firmware"
    assert panel.tool_card.contentLabel.text() == "未配置"
    # 完整路径通过 tooltip 可查看，避免长路径截断后不可读
    assert panel.root_card.toolTip() == "D:/firmware"

    requested: list[bool] = []
    panel.configure_requested.connect(lambda: requested.append(True))
    panel.root_card.button.click()
    assert requested == [True]
    panel.tool_card.button.click()
    assert requested == [True, True]


def test_workbench_configuration_notice_visibility(qapp) -> None:
    """未配置时指示可见、配置后隐藏；不再有「前往设置」按钮（入口统一由导航承担）。"""
    from fwasset.ui_qt.workbench_window import CONFIGURATION_NOTICE, WorkbenchInterface

    workbench = WorkbenchInterface()
    workbench._search_timer.stop()
    workbench._model_switch_timer.stop()

    workbench.set_configuration_required(True)
    assert not workbench.configuration_notice.isHidden()
    assert workbench.configuration_notice.text() == CONFIGURATION_NOTICE
    assert workbench.configuration_notice.action.isHidden()
    assert not hasattr(workbench, "open_settings_button")
    assert not hasattr(workbench, "scan_btn"), "重新读取已迁到软件修复页"

    workbench.set_configuration_required(False)
    assert workbench.configuration_notice.isHidden()


def test_settings_completion_reloads_and_reads_new_root(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QDialog

    import fwasset.core.settings as settings
    from fwasset.ui_qt import workbench_window as window_module
    from fwasset.ui_qt.setup_wizard import SetupWizard

    monkeypatch.setattr(settings, "DEFAULT_ROOT", "")
    monkeypatch.setattr(settings, "TOOL_ROOT", "")
    monkeypatch.setattr(SetupWizard, "exec", lambda _self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(SetupWizard, "root_dir", lambda _self: str(tmp_path))
    monkeypatch.setattr(SetupWizard, "tool_root", lambda _self: "")

    writes: list[bool] = []
    monkeypatch.setattr(SetupWizard, "write_config", lambda _self: writes.append(True))

    def reload_settings() -> tuple[str, str]:
        monkeypatch.setattr(settings, "DEFAULT_ROOT", str(tmp_path))
        return str(tmp_path), ""

    monkeypatch.setattr(window_module, "_reload_runtime_settings", reload_settings)
    window = window_module.QtWorkbenchWindow()
    scanned_roots: list[str] = []
    window.workbench._auto_scan = lambda root: scanned_roots.append(root)  # type: ignore[method-assign]

    window._open_configuration()
    qapp.processEvents()

    assert writes == [True]
    assert window.workbench.root_dir == str(tmp_path)
    assert scanned_roots == [str(tmp_path)]


def test_re_read_uses_configured_root_without_folder_picker(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.ui_qt import workbench_window as window_module

    monkeypatch.setattr(window_module, "DEFAULT_ROOT", str(tmp_path))
    workbench = window_module.WorkbenchInterface()
    workbench._search_timer.stop()
    workbench._model_switch_timer.stop()
    roots: list[str] = []

    def build_result(root: str, **_kwargs) -> dict:
        roots.append(root)
        return {
            "ok": True,
            "code": "ok",
            "message": "",
            "payload": {"assets": [], "errors": []},
        }

    class InlineThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(window_module, "build_scan_result", build_result)
    monkeypatch.setattr(window_module.threading, "Thread", InlineThread)
    workbench._start_scan()

    assert roots == [str(tmp_path)]
    assert workbench.root_dir == str(tmp_path)


def test_re_read_without_config_opens_settings(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fwasset.ui_qt import workbench_window as window_module

    monkeypatch.setattr(window_module, "DEFAULT_ROOT", "")
    workbench = window_module.WorkbenchInterface()
    workbench._search_timer.stop()
    workbench._model_switch_timer.stop()
    requested: list[bool] = []
    workbench.settings_requested.connect(lambda: requested.append(True))

    workbench._start_scan()

    assert requested == [True]


def test_populate_tree_empty_is_noop(qapp) -> None:
    grid = DataGrid(lambda _m: None)
    grid.populate_tree([])
    assert grid.tree.topLevelItemCount() == 0
    assert grid.get_selected_variant() is None


def test_single_variant_collapses_with_directory_name_and_badge(qapp) -> None:
    """单变体折叠为一行；程序名称始终用目录名（Issue 20-A）；徽章拼在名称列。"""
    row = ModuleRow(
        label="主板程序",
        source_kind="common",
        source_label="通用",
        variants=[_variant("主板程序", badge="★默认")],
    )
    grid = DataGrid(lambda _m: None)
    grid.populate_tree([row])

    assert grid.tree.topLevelItemCount() == 1
    item = grid.tree.topLevelItem(0)
    assert item.childCount() == 0
    assert item.text(0) == "主板程序"
    assert item.text(1) == "主板程序  ★默认"
    assert item.text(2) == "V40"
    assert item.text(3) == "通用"
    assert "回源" not in item.text(3)
    assert [grid.tree.headerItem().text(i) for i in range(4)] == [
        "类型",
        "程序名称",
        "版本",
        "归属",
    ]
    assert grid.variant_count() == 1


def test_grid_rows_align_with_headers(qapp) -> None:
    grid = DataGrid(lambda _m: None)
    grid.populate_tree([
        ModuleRow(
            label="主板程序",
            source_kind="common",
            source_label="通用",
            variants=[_variant("甲"), _variant("乙")],
        )
    ])
    left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    assert grid.tree.header().defaultAlignment() == left
    parent = grid.tree.topLevelItem(0)
    for item in (parent, parent.child(0)):
        for column in range(grid.tree.columnCount()):
            assert item.textAlignment(column) == left


def test_img_handcontrol_is_rendered_in_common_qt_grid(qapp, tmp_path) -> None:
    from fwasset.core.asset_index import save_assets
    from fwasset.core.file_scan import scan_firmware_assets
    from fwasset.ui_common.view_models.scheme_workbench_model import (
        SchemeWorkbenchModel,
    )

    root = tmp_path / "testprogram"
    model_root = root / "L36"
    model_root.mkdir(parents=True)
    (model_root / "平台配置.toml").write_text(
        '[[platform]]\nname = "单3D"\n', encoding="utf-8"
    )
    program = model_root / "通用" / "手控UI" / "YJ_d12x_massage_lcd_L50S_V21.07"
    program.mkdir(parents=True)
    (program / "d12x_mzkj_v1.0.0.img").write_bytes(b"firmware")
    (program / "bootcfg.txt").write_text("boot", encoding="utf-8")
    (program / "程序信息.toml").write_text('vendor = "测试"\n', encoding="utf-8")
    assets, issues = scan_firmware_assets(str(root))
    assert not issues
    db = tmp_path / "index.db"
    save_assets(assets, str(root), path=db)
    model = SchemeWorkbenchModel()
    model.bind(db, root, root)

    grid = DataGrid(lambda _message: None)
    grid.populate(model.get_common_modules("L36", "手控UI"))

    assert grid.tree.topLevelItemCount() == 1
    item = grid.tree.topLevelItem(0)
    assert item.text(0) == "手控UI"
    assert item.text(1) == program.name
    assert item.text(2) == "V21.07"
    assert item.text(3) == "通用"


def test_multi_variant_grouped_under_parent_row(qapp) -> None:
    """多变体收在模块行子节点下，子行显示原始变体名，默认展开。"""
    row = ModuleRow(
        label="手控UI",
        source_kind="custom",
        source_label="定制专属",
        variants=[
            _variant("以色列", kind="custom"),
            _variant("以色列-塞尔维亚", kind="custom"),
        ],
    )
    grid = DataGrid(lambda _m: None)
    grid.populate_tree([row])

    assert grid.tree.topLevelItemCount() == 1
    parent = grid.tree.topLevelItem(0)
    assert parent.childCount() == 2
    assert parent.isExpanded()
    assert parent.child(0).text(1) == "以色列"
    assert parent.child(1).text(1) == "以色列-塞尔维亚"
    assert parent.text(1) == "2 个变体"
    assert grid.variant_count() == 2


def test_populate_cards_groups_by_label(qapp) -> None:
    """populate 兼容路径：同 label 卡片归为一个模块行。"""
    cards = [
        ModuleCardData(
            asset={
                **_asset(),
                "firmware_label": "主板程序",
                "directory_name": "量产_默认",
                "version": "V40",
            },
            source_type="common_default",
            source_label="通用/量产_默认 (默认)",
            is_fallback=False,
            source_kind="common",
            default_badge="★默认",
        ),
        ModuleCardData(
            asset={
                **_asset(),
                "firmware_label": "主板程序",
                "directory_name": "防夹功能",
                "version": "V24",
            },
            source_type="common_variant",
            source_label="通用/防夹功能",
            is_fallback=False,
            source_kind="common",
        ),
    ]
    grid = DataGrid(lambda _m: None)
    grid.populate(cards)

    assert grid.tree.topLevelItemCount() == 1
    parent = grid.tree.topLevelItem(0)
    assert parent.text(0) == "主板程序"
    assert parent.childCount() == 2
    assert "★默认" in parent.child(0).text(1)
    assert "★默认" not in parent.child(1).text(1)


def test_activity_log_counts_unread_errors_and_view_marks_read(qapp) -> None:
    log = ActivityLog()
    changes: list[bool] = []
    log.changed.connect(lambda: changes.append(True))
    log.write("第一条日志")
    log.write("复制到 U 盘失败: 盘符不存在\n")
    log.write("")
    assert log.lines() == ["第一条日志", "复制到 U 盘失败: 盘符不存在"]
    assert log.unread_errors == 1
    assert len(changes) == 2

    view = LogView(log)
    assert "第一条日志" in view.log_text.toPlainText()
    assert log.unread_errors == 0, "打开日志即视为已读"
    view.clear_btn.click()
    assert log.lines() == []
    assert view.log_text.toPlainText() == ""


def test_model_dropdown_lists_all_models_and_shows_current(qapp) -> None:
    """左栏型号下拉常驻，含全部型号并显示当前型号；无型号时给出提示。"""
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    w.current_selection.model_name = "L36"
    w._refresh_model_selector(["L36", "L36双机芯-上3D-下2D", "L50S", "M3", "M5", "M8"])
    assert w.model_combo.count() == 6
    assert w.model_combo.text() == "L36"
    assert w.model_hint.isHidden()

    w.current_selection.model_name = ""
    w._refresh_model_selector([])
    assert w.model_combo.count() == 0
    assert not w.model_hint.isHidden()


def test_model_combo_restores_current_after_abandoned_typing(qapp) -> None:
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    w.current_selection.model_name = "L36"
    w._refresh_model_selector(["L36", "M8"])
    w.model_combo.setText("L5")
    w._sync_model_combo_text()
    assert w.model_combo.text() == "L36"


def test_model_switch_is_debounced_and_prefix_safe(
    qapp, tmp_path, monkeypatch
) -> None:
    """切型号经防抖确认：键入途中命中前缀型号（L36 是 L36双机芯-… 的前缀）
    会被后续输入覆盖，最终只切到用户真正选中的型号。"""
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    # 隔离默认索引库：bind(None) 会解析到本机真实 .runtime/fwasset.db
    monkeypatch.setattr(
        "fwasset.core.asset_index.ASSET_INDEX_PATH", tmp_path / "fwasset.db"
    )

    w = WorkbenchInterface()
    w.current_selection.model_name = "M8"
    w._refresh_model_selector(["L36", "L36双机芯-上3D-下2D", "M8"])

    # 模拟键入 "L36双…"：先命中前缀 L36，随后命中完整型号——防抖期内后者覆盖前者
    w._schedule_model_switch("L36")
    w._schedule_model_switch("L36双机芯-上3D-下2D")
    w._apply_pending_model()
    assert w.current_selection.model_name == "L36双机芯-上3D-下2D"

    # 非真实型号（输入中间态）不入队
    w._schedule_model_switch("L5")
    w._apply_pending_model()
    assert w.current_selection.model_name == "L36双机芯-上3D-下2D"

    # 防呆兜底：_on_model_changed 直接收到假型号也不切
    w._on_model_changed("L5")
    assert w.current_selection.model_name == "L36双机芯-上3D-下2D"


def test_sidebar_rebuild_clears_stale_pressed_and_hover_rows(qapp) -> None:
    """搜索结果展开为完整侧栏后，旧点击位置不得保留伪高亮。"""
    from PySide6.QtWidgets import QListWidgetItem

    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    w.current_selection.node_type = "custom_scheme"
    w.current_selection.scheme_name = "马来西亚"
    w._nav_entries = [
        ("all", ""),
        ("section", ""),
        ("common_type", "主板程序"),
        ("common_type", "语音程序"),
        ("section", ""),
        ("custom_scheme", "德国"),
        ("custom_scheme", "马来西亚"),
        ("custom_scheme", "西班牙"),
    ]
    for i in range(len(w._nav_entries)):
        w.nav.addItem(QListWidgetItem(str(i)))

    # 搜索态点击时「马来西亚」位于第 2 行；恢复完整侧栏后它移动到第 6 行。
    w.nav.delegate.setPressedRow(2)
    w.nav.delegate.setHoverRow(2)

    w._apply_nav_selection_highlight()

    assert w.nav.currentRow() == 6
    assert w.nav.delegate.selectedRows == {6}
    assert w.nav.delegate.pressedRow == -1
    assert w.nav.delegate.hoverRow == -1


def test_click_filtered_scheme_reselects_its_new_row_after_sidebar_rebuild(
    qapp,
) -> None:
    """真实鼠标点击返回后，delegate 选中集合必须跟随方案的新行号。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from fwasset.ui_common.view_models.scheme_workbench_model import WorkbenchSelection
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    w._search_timer.stop()
    w._model_switch_timer.stop()
    w._cached_load_timer.stop()
    w._usb_timer.stop()
    w._refresh_main_grid = lambda: None  # type: ignore[method-assign]
    w.workbench_model.build_sidebar_tree = lambda _model: {  # type: ignore[method-assign]
        "common": {"主板程序": 2, "手控UI": 3, "语音程序": 1},
        "custom": ["德国", "马来西亚", "美国", "西班牙"],
    }
    w.current_selection = WorkbenchSelection(model_name="L36", node_type="all")
    w.search_edit.blockSignals(True)
    w.search_edit.setText("马来西亚")
    w.search_edit.blockSignals(False)
    w._refresh_sidebar_tree()
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()

    old_row = next(
        i
        for i, entry in enumerate(w._nav_entries)
        if entry == ("custom_scheme", "马来西亚")
    )
    old_item = w.nav.item(old_row)
    click_pos = w.nav.visualItemRect(old_item).center()

    QTest.mouseClick(
        w.nav.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        click_pos,
    )
    qapp.processEvents()

    target_row = next(
        i
        for i, entry in enumerate(w._nav_entries)
        if entry == ("custom_scheme", "马来西亚")
    )
    selected_rows = {index.row() for index in w.nav.selectedIndexes()}

    assert target_row != old_row
    assert w.nav.currentRow() == target_row
    assert selected_rows == {target_row}
    assert w.nav.delegate.selectedRows == {target_row}
    assert w.nav.currentItem().text() == "马来西亚"


# ---------------------------------------------------------------------------
# Phase 3：操作面板注册表与四类面板构建
# ---------------------------------------------------------------------------


class _FakeHost:
    """满足 ui_qt PanelHost 协议的最小假宿主。"""

    def __init__(self, drive: str = "E:"):
        self.drive = drive
        self.calls: list[tuple] = []

    def _run_task(self, name, fn, on_done=None):
        self.calls.append(("run_task", name))

    def _selected_asset(self):
        return None

    def _open_current_asset_dir(self):
        self.calls.append(("open_dir",))

    def _copy_asset_dir_path(self):
        self.calls.append(("copy_dir",))

    def _copy_primary_file_path(self):
        self.calls.append(("copy_file",))

    def get_global_usb_drive(self) -> str:
        return self.drive


def _op_asset(**overrides) -> dict:
    base = {
        "firmware_type": "handcontrol_ui",
        "firmware_label": "手控UI",
        "flash_mode": "auto_usb",
        "usb_flow": "paired_files",
        "model": "L36",
        "version": "V1.0",
        "path": "D:/x/手控UI/A",
        "files": ["a.rom", "a.pkg"],
        "tool_name": "",
        "tool_dir": "",
    }
    base.update(overrides)
    return base


def test_qt_panel_registry_routes_all_four_modes(qapp) -> None:
    from fwasset.ui_qt.operation_panels import get_panel

    for mode in ("auto_usb", "tool_launch", "manual_doc", "disabled"):
        assert get_panel(mode) is not None, f"{mode} 应有注册面板"
    assert get_panel("nonexistent") is None


def test_auto_usb_panel_paired_files_shows_copy_button(qapp) -> None:
    from qfluentwidgets import PrimaryPushButton

    from fwasset.ui_qt.operation_panels import get_panel

    panel = get_panel("auto_usb")(
        asset=_op_asset(), log_fn=lambda _m: None, panel_host=_FakeHost()
    )
    panel.build()
    buttons = panel.findChildren(PrimaryPushButton)
    texts = {b.text() for b in buttons}
    assert "格式化并复制到 U 盘" in texts
    assert "一键烧录" not in texts


def test_auto_usb_panel_img_txt_uses_same_copy_button(qapp) -> None:
    from qfluentwidgets import PrimaryPushButton

    from fwasset.ui_qt.operation_panels import get_panel

    panel = get_panel("auto_usb")(
        asset=_op_asset(files=["bootcfg.txt", "d12x_mzkj_v1.0.0.img", "程序信息.toml"]),
        log_fn=lambda _m: None,
        panel_host=_FakeHost(),
    )
    panel.build()
    buttons = panel.findChildren(PrimaryPushButton)
    assert {b.text() for b in buttons} == {"格式化并复制到 U 盘"}


def test_auto_usb_panel_missing_pkg_shows_warning(qapp) -> None:
    from qfluentwidgets import BodyLabel, PrimaryPushButton

    from fwasset.ui_qt.operation_panels import get_panel

    panel = get_panel("auto_usb")(
        asset=_op_asset(files=["a.rom"]), log_fn=lambda _m: None, panel_host=_FakeHost()
    )
    panel.build()
    assert not panel.findChildren(PrimaryPushButton), "缺 PKG 不应出现一键烧录"
    labels = [w.text() for w in panel.findChildren(BodyLabel)]
    assert any("PKG" in t for t in labels)


def test_auto_usb_panel_directory_copy_has_options(qapp) -> None:
    from qfluentwidgets import CheckBox

    from fwasset.ui_qt.operation_panels import get_panel

    panel = get_panel("auto_usb")(
        asset=_op_asset(firmware_type="music_files", usb_flow="directory_copy"),
        log_fn=lambda _m: None,
        panel_host=_FakeHost(),
    )
    panel.build()
    checks = {c.text(): c.isChecked() for c in panel.findChildren(CheckBox)}
    assert checks == {"格式化": True, "完成后弹出": True}


def test_tool_launch_panel_without_tool_shows_nothing(qapp) -> None:
    """找不到工具时不给按钮、不给路径提示（是否保留打开工具待定）。"""
    from PySide6.QtWidgets import QWidget

    from fwasset.ui_qt.operation_panels import get_panel

    panel = get_panel("tool_launch")(
        asset=_op_asset(firmware_type="不存在的类型", flash_mode="tool_launch"),
        log_fn=lambda _m: None,
        panel_host=_FakeHost(),
    )
    panel.build()
    assert panel.findChildren(QWidget) == []


def test_tool_launch_panel_with_tool_offers_launch(qapp, monkeypatch) -> None:
    from qfluentwidgets import PrimaryPushButton

    from fwasset.ui_qt.operation_panels import get_panel, tool_launch_panel

    monkeypatch.setattr(
        tool_launch_panel, "discover_tool_path", lambda *_a, **_k: "D:/tools/a.exe"
    )
    panel = get_panel("tool_launch")(
        asset=_op_asset(firmware_type="不存在的类型", flash_mode="tool_launch"),
        log_fn=lambda _m: None,
        panel_host=_FakeHost(),
    )
    panel.build()
    texts = [b.text() for b in panel.findChildren(PrimaryPushButton)]
    assert texts == ["打开烧录工具"]


def test_detail_pane_shows_properties_and_hides_write_actions(qapp) -> None:
    """详情：属性、U 盘行只在 auto_usb；借用行不能删、不能改厂商。"""
    from fwasset.ui_qt.detail_pane import DetailPane

    pane = DetailPane()
    assert pane.content.isHidden() and not pane.empty.isHidden()

    local = ModuleVariant(
        asset={**_op_asset(), "vendor": "YJ"},
        name="YJ_d12x",
        version="V21.07",
        source_kind="common",
        source_label="通用",
    )
    mode = pane.show_variant(local, can_write=True, can_delete=True)
    assert mode == "auto_usb"
    assert pane.name_label.text().replace("\u200b", "") == "YJ_d12x"
    assert pane.vendor_value.text() == "YJ"
    assert pane.files_value.text().replace("\u200b", "") == "a.rom\na.pkg"
    assert pane.files_value.toolTip() == "a.rom\na.pkg"
    assert pane.source_tag.text() == "通用"
    assert not pane.usb_row.isHidden()
    assert not pane.delete_btn.isHidden() and not pane.vendor_edit.isHidden()

    borrowed = ModuleVariant(
        asset=_op_asset(flash_mode="tool_launch"),
        name="源程序",
        version="V1",
        source_kind="common",
        source_label="通用",
        shared_state="shared_hit",
        shared_source_label="自动更新·L50S",
        borrowed_only=True,
    )
    mode = pane.show_variant(borrowed, can_write=True, can_delete=False)
    assert mode == "tool_launch"
    assert pane.source_tag.text() == "来自 L50S"
    assert pane.usb_row.isHidden()
    assert pane.delete_btn.isHidden() and pane.vendor_edit.isHidden()

    missing = ModuleVariant(
        asset=_op_asset(),
        name="",
        version="",
        source_kind="common",
        source_label="通用",
        shared_state="shared_missing",
    )
    assert pane.show_variant(missing, can_write=True, can_delete=False) == ""
    assert not pane.notice.isHidden()
    assert not pane.open_dir_btn.isEnabled()

    pane.show_empty()
    assert pane.content.isHidden() and pane.footer.isHidden()


def test_selection_drives_commands_and_detail(qapp, monkeypatch) -> None:
    from fwasset.ui_qt import workbench_window as window_module

    monkeypatch.setattr(
        window_module, "write_gate_check", lambda *_a, **_k: _fake_gate(True)
    )
    w = window_module.WorkbenchInterface()
    w.current_selection.model_name = "L36"
    assert not w.update_btn.isEnabled() and not w.retired_btn.isEnabled()
    assert not hasattr(w, "more_btn"), "选中项操作只在详情底部与右键菜单"

    variant = ModuleVariant(
        asset=_op_asset(),
        name="A",
        version="V1.0",
        source_kind="common",
        source_label="通用",
    )
    w._on_grid_selection_changed(variant)
    assert w.update_btn.isEnabled() and w.retired_btn.isEnabled()
    assert w.active_operation_panel is not None
    assert w.detail.name_label.text() == "A"
    w._on_grid_selection_changed(None)
    assert not w.update_btn.isEnabled()
    assert w.active_operation_panel is None
    assert not w.detail.empty.isHidden()


def test_break_long_name_allows_wrapping_at_separators() -> None:
    from fwasset.ui_qt.detail_pane import break_long_name

    name = "YJ_3DMain_Foot_16_MA82G5C64_V40-221223-32023.0710.bin"
    broken = break_long_name(name)
    assert broken.replace("\u200b", "") == name
    assert broken.count("\u200b") == name.count("_") + name.count("-") + name.count(".")
    assert break_long_name("手控UI") == "手控UI"


def test_run_task_busy_guard_and_completion(qapp) -> None:
    """任务桥契约：忙时拒绝新任务；worker 结束后经 Signal 回投解除忙态。"""
    import time as _time

    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    logs: list[str] = []
    w.log_message.connect(logs.append)

    done = []
    w._run_task(
        "测试任务",
        lambda log_fn: (log_fn("working"), "ok")[-1],
        lambda r: done.append(r),
    )
    # 忙态下第二个任务应被拒绝
    w._run_task("第二任务", lambda log_fn: "no")
    deadline = _time.time() + 5
    while w._busy and _time.time() < deadline:
        qapp.processEvents()
        _time.sleep(0.02)
    qapp.processEvents()

    assert w._busy is False, "worker 完成后应解除忙态"
    assert done == ["ok"], "on_done 回调应收到任务结果"
    assert any("已有任务执行中" in m for m in logs)
    assert any("测试任务完成" in m for m in logs)


def test_wrapped_write_failure_is_not_logged_as_complete(qapp) -> None:
    """run_write 把服务结果包在 epoch 里；失败不能再记成「完成」。"""
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    logs: list[str] = []
    w.log_message.connect(logs.append)
    w._silent_tasks.add(7)
    w._on_task_done(
        7,
        "关联程序",
        {
            "epoch": 1,
            "result": {
                "ok": False,
                "code": "invalid_args",
                "message": "来源型号必须已配置 id",
                "payload": {},
            },
            "status": None,
        },
    )

    assert any("关联程序失败" in message for message in logs)
    assert any("来源型号必须已配置 id" in message for message in logs)
    assert not any(message == "关联程序完成" for message in logs)


def test_shared_badges_render_on_existing_variant_row() -> None:
    hit = _variant("快捷键", kind="common")
    hit.shared_state = "shared_hit"
    hit.shared_source_label = "L36"
    missing = _variant("快捷键", kind="common")
    missing.shared_state = "shared_missing"

    # 借用来源画在归属标签，不再拼进名称列
    assert DataGrid._variant_text(hit, "快捷键程序") == "快捷键"
    grid = DataGrid(lambda _m: None)
    grid.populate_tree(
        [ModuleRow(label="快捷键程序", source_kind="common", source_label="通用", variants=[hit])]
    )
    assert grid.tree.topLevelItem(0).text(3) == "来自 L36"
    assert "关联来源缺失" in DataGrid._variant_text(missing, "快捷键程序")


def test_present_result_keeps_resume_for_recovery_required(qapp) -> None:
    from fwasset.ui_common.workspace_actions import recovery_banner_text
    from fwasset.ui_qt.entry_flows import present_result
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    workbench = WorkbenchInterface()
    workbench._search_timer.stop()
    workbench._model_switch_timer.stop()
    workbench.apply_recovery_hold(
        recovery_banner_text("update_asset"), "resume_update_asset"
    )
    present_result(
        workbench,
        {
            "ok": False,
            "code": "recovery_required",
            "message": "需要恢复",
            "payload": {},
        },
        "update_asset",
    )
    assert workbench._resume_name == "resume_update_asset"
    assert workbench.recovery_banner.action.isHidden() is False
    assert workbench.recovery_banner.action.text() == "继续恢复"


def test_failed_cache_scan_holds_writes_and_starts_recovery(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fwasset.core.settings as settings
    from fwasset.ui_common.workspace_actions import StartupRecovery
    from fwasset.ui_qt import workbench_window as window_module

    monkeypatch.setattr(settings, "DEFAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(window_module, "DEFAULT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        window_module,
        "build_cached_scan_result",
        lambda **_kwargs: {
            "ok": False,
            "code": "failed",
            "message": "缓存不可用",
            "payload": {},
        },
    )
    started: list[str] = []

    def fake_recovery(root: str, **_kwargs: object) -> StartupRecovery:
        started.append((str(root), window.workbench._writes_held))
        return StartupRecovery(True, None, None, [])

    class InlineThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(window_module, "run_startup_recovery", fake_recovery)
    monkeypatch.setattr(window_module.threading, "Thread", InlineThread)
    window = window_module.QtWorkbenchWindow()
    workbench = window.workbench
    workbench._search_timer.stop()
    workbench._model_switch_timer.stop()

    assert workbench._writes_held is False
    assert started == []
    workbench._load_cached_assets()
    assert started == []
    assert workbench._writes_held is False
    window.show()
    qapp.processEvents()
    assert started == [(str(tmp_path), True)]


def test_repair_page_previews_when_shown(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    from fwasset.ui_qt.repair_interface import RepairInterface

    model = tmp_path / "M"
    model.mkdir()
    calls: list[str] = []

    def fake_preview(_configured: str, _workspace: str, model_root) -> dict:
        calls.append(model_root.name)
        return {
            "ok": True,
            "code": "ok",
            "message": "",
            "payload": {"preview": {"already_normalized": False}},
        }

    monkeypatch.setattr(
        "fwasset.ui_qt.repair_interface.preview_platform_normalize", fake_preview
    )
    monkeypatch.setattr(
        "fwasset.ui_qt.repair_interface.scan_legacy_excluded_dirs", lambda _root: []
    )
    monkeypatch.setattr(
        "fwasset.ui_qt.repair_interface.enumerate_model_roots", lambda _root: [model]
    )
    page = RepairInterface()
    page.set_workspace(str(tmp_path))
    assert calls == []
    assert page.caption.text() != "没有需要处理的项目"

    page.show()
    deadline = _time.time() + 3
    while page.rows.count() == 0 and _time.time() < deadline:
        qapp.processEvents()
        _time.sleep(0.02)

    assert calls == ["M"]
    assert page.rows.count() == 1
    assert "需要归一" in page.rows.item(0).text()


def test_wizard_keeps_draft_when_config_is_corrupt(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fwasset.core.settings as settings
    from fwasset.ui_qt.setup_wizard import SetupWizard

    broken = tmp_path / "config.toml"
    broken.write_bytes(b"[\n")
    monkeypatch.setattr(settings, "CONFIG_PATH", broken)
    root = tmp_path / "firmware"
    root.mkdir()
    wizard = SetupWizard(root_dir=str(root), tool_root="", allow_skip=False)
    wizard._root_edit.setText(str(root))

    wizard._on_accept()

    assert wizard.result() == 0
    assert "损坏" in wizard._error_label.text()
    assert wizard.root_dir() == str(root)
    assert broken.read_bytes() == b"[\n"


def test_declining_root_switch_does_not_write(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    import fwasset.core.settings as settings
    from fwasset.ui_qt import workbench_window as window_module
    from fwasset.ui_qt.setup_wizard import SetupWizard

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        'vendors = ["摩众"]\n\n[paths]\nroot_dir = "D:/keep"\ntool_root = ""\n',
        encoding="utf-8",
    )
    before = config.read_bytes()
    monkeypatch.setattr(settings, "CONFIG_PATH", config)
    monkeypatch.setattr(settings, "DEFAULT_ROOT", str(old))
    monkeypatch.setattr(settings, "TOOL_ROOT", "")
    asked: list[str] = []

    def decline(*_args, **_kwargs):
        asked.append("切换程序文件夹")
        return QMessageBox.StandardButton.No

    def finish(wizard: SetupWizard) -> int:
        wizard._root_edit.setText(str(new))
        wizard._on_accept()
        return int(wizard.result())

    monkeypatch.setattr(QMessageBox, "question", decline)
    monkeypatch.setattr(SetupWizard, "exec", finish)
    window = window_module.QtWorkbenchWindow()
    window.workbench._cached_load_timer.stop()
    window.workbench._usb_timer.stop()

    window._open_configuration()

    assert asked == ["切换程序文件夹"]
    held = window.findChild(SetupWizard)
    assert held is not None
    assert held.result() == 0
    assert held.root_dir() == str(new)
    assert config.read_bytes() == before


def test_accepting_root_switch_writes_only_after_confirm(
    qapp, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    import fwasset.core.settings as settings
    from fwasset.ui_common.workspace_actions import StartupRecovery
    from fwasset.ui_qt import workbench_window as window_module
    from fwasset.ui_qt.setup_wizard import SetupWizard

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    monkeypatch.setattr(settings, "DEFAULT_ROOT", str(old))
    monkeypatch.setattr(settings, "TOOL_ROOT", "")
    order: list[str] = []

    def accept_switch(*_args, **_kwargs):
        order.append("ask")
        return QMessageBox.StandardButton.Yes

    def record_write(_self, _config_path=None):
        order.append("write")
        return {"ok": True, "code": "ok", "message": "配置已保存", "payload": {}}

    def finish(wizard: SetupWizard) -> int:
        wizard._root_edit.setText(str(new))
        wizard._on_accept()
        return int(wizard.result())

    monkeypatch.setattr(QMessageBox, "question", accept_switch)
    monkeypatch.setattr(SetupWizard, "write_config", record_write)
    monkeypatch.setattr(SetupWizard, "exec", finish)
    monkeypatch.setattr(
        window_module,
        "_reload_runtime_settings",
        lambda: (str(new), ""),
    )
    monkeypatch.setattr(
        window_module,
        "run_startup_recovery",
        lambda *_args, **_kwargs: StartupRecovery(True, None, None, []),
    )
    window = window_module.QtWorkbenchWindow()
    window.workbench._cached_load_timer.stop()
    window.workbench._usb_timer.stop()
    window.workbench._auto_scan = lambda _root: None  # type: ignore[method-assign]

    window._open_configuration()

    assert order == ["ask", "write"]
    assert window.workbench.root_dir == str(new)


def test_borrow_candidates_excludes_current_model_and_other_modules(qapp) -> None:
    """借用候选排除本型号；给定类型时只留同类型（TASK-20260923）。"""
    from types import SimpleNamespace

    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    host = WorkbenchInterface.__new__(WorkbenchInterface)
    host.current_selection = SimpleNamespace(model_name="L36")
    host.workbench_model = SimpleNamespace(
        _all_assets=[
            {"firmware_label": "主板程序", "model": "L36", "directory_name": "本型号"},
            {"firmware_label": "主板程序", "model": "L50S", "directory_name": "可借用"},
            {"firmware_label": "腿部程序", "model": "L50S", "directory_name": "别的模块"},
        ],
        _model_of_asset=lambda a: str(a.get("model", "")),
    )

    names = [
        a["directory_name"] for a in host.borrow_candidates_for("主板程序")
    ]
    assert names == ["可借用"]
    # 新建程序不按类型过滤：列出其他型号的全部程序
    all_names = [a["directory_name"] for a in host.borrow_candidates_for("")]
    assert all_names == ["可借用", "别的模块"]

    host.current_selection = SimpleNamespace(model_name="")
    assert host.borrow_candidates_for("主板程序") == []
    assert host.asset_model_name({"model": "L50S"}) == "L50S"


def test_recycle_page_lists_pending_deletes_and_empty_state(qapp, tmp_path) -> None:
    """回收站页：空状态 + 只列可还原的删除项（TASK-20260923）。"""
    import time

    from fwasset.ui_qt.recycle_interface import RecycleInterface

    page = RecycleInterface()
    page.bind(None, str(tmp_path))
    page.reload()
    assert page.rows.count() == 0
    assert "回收站是空的" in page.caption.text()
    assert not page.empty_button.isEnabled()

    import fwasset.ui_qt.recycle_interface as module

    now = time.time()
    records = [
        {
            "id": "a",
            "kind": "undoable_delete",
            "status": "pending",
            "original_path": str(tmp_path / "L36" / "通用" / "主板程序" / "量产_默认"),
            "created_at": now,
            "expires_at": now + 3600.0,
        },
        {
            "id": "b",
            "kind": "transactional_retire",
            "status": "committed",
            "original_path": str(tmp_path / "旧版"),
            "created_at": now,
            "expires_at": 0.0,
        },
    ]
    original = module.list_records
    module.list_records = lambda _root: records
    try:
        page.reload()
    finally:
        module.list_records = original

    assert page.rows.count() == 1
    assert "量产_默认" in page.rows.item(0).text()
    assert page.empty_button.isEnabled()
    # 未选中行时逐项操作不可用
    assert not page.restore_button.isEnabled()


def _bare_workbench():
    from fwasset.ui_qt.workbench_window import WorkbenchInterface

    w = WorkbenchInterface()
    for timer in (w._search_timer, w._model_switch_timer, w._cached_load_timer, w._usb_timer):
        timer.stop()
    return w


def test_usb_combo_shows_selected_drive_not_placeholder(qapp, monkeypatch) -> None:
    """有 U 盘时显示并能选盘符；拔掉后显示占位文字且取不到盘符。"""
    import fwasset.ui_qt.workbench_window as module
    from fwasset.ui_qt.detail_pane import USB_PLACEHOLDER

    w = _bare_workbench()
    drives = ["E:\\", "F:\\"]
    monkeypatch.setattr(module, "get_usb_drives", lambda: list(drives))

    w._refresh_usb()
    assert w.usb_combo.text() == "E:\\"
    assert w.get_global_usb_drive() == "E:\\"
    w.usb_combo.setCurrentIndex(1)
    w._refresh_usb()
    assert w.usb_combo.text() == "F:\\", "刷新后保留用户选择"

    drives.clear()
    w._refresh_usb()
    assert w.usb_combo.text() == USB_PLACEHOLDER
    assert w.get_global_usb_drive() == ""

    drives.append("G:\\")
    w._refresh_usb()
    assert w.usb_combo.text() == "G:\\"


def test_usb_task_done_shows_eject_notice(qapp) -> None:
    w = _bare_workbench()
    toasts: list[tuple[str, str, str]] = []
    w._toast = lambda title, content="", *, error=False, level="": toasts.append(  # type: ignore[method-assign]
        (title, content, level)
    )

    ok = {"ok": True, "code": "ok", "message": "", "payload": {"ejected": True}}
    w._on_task_done(1, "复制到 U 盘", ok)
    failed_eject = {**ok, "payload": {"ejected": False}}
    w._on_task_done(2, "复制到 U 盘", failed_eject)
    w._on_task_done(3, "其他任务", {"ok": True, "payload": {}})

    assert toasts[0] == ("已复制到 U 盘", "U 盘已安全弹出，可以拔出", "success")
    assert toasts[1][2] == "warning"
    assert toasts[2] == ("其他任务完成", "", "")
    assert w._usb_timer.isActive(), "弹出后要刷新 U 盘列表"
