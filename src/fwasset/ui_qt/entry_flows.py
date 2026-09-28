"""空工作区录入、更新程序与改厂商。对话框只收集输入，判定留给服务。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from fwasset.core.firmware_catalog import enabled_firmware_types
from fwasset.core.platform_config import canonical_module_dir
from fwasset.core.reference_lookup import find_references_to
from fwasset.core.services.asset_service import create_asset, delete_asset
from fwasset.core.services.layout_update_service import (
    change_asset_semantics,
    list_retired_versions,
    restore_retired_version,
    update_asset,
)
from fwasset.core.services.model_scheme_service import (
    create_model,
    create_scheme,
    delete_model,
    delete_scheme,
    rename_model,
    rename_scheme,
)
from fwasset.core.services.write_edit_service import (
    register_shared_module,
    update_asset_vendor,
)
from fwasset.core.settings import load_vendor_candidates
from fwasset.core.types import (
    ChassisType,
    ClearDefaultsKind,
    FirmwareAsset,
    RetiredVersionView,
    RetireMode,
)
from fwasset.ui_common.asset_name_prefill import (
    classify_create_source,
    describe_create_source,
    handcontrol_gap_hint,
    prefill_asset_name,
)
from fwasset.ui_common.workbench_helpers import module_label_from_asset
from fwasset.ui_common.workspace_actions import (
    ProgramUpdatePlan,
    direct_scheme_dir_names,
    effect_for_result,
    plan_program_update,
    stage_files_as_named_dir,
)
from fwasset.ui_qt.design_tokens import SPACE_LG
from fwasset.ui_qt.program_form import FileDropZone, ScopeCombo

_CHASSIS: tuple[ChassisType, ...] = ("单3D", "单2D", "双2D", "上3D下2D")


def present_result(host: Any, result: dict[str, Any], entry: str) -> str:
    """按编排动作展示服务消息。返回动作名。"""
    effect = effect_for_result(result, entry=entry)
    message = str(result.get("message") or "")
    if effect.note:
        message = f"{message}\n\n{effect.note}"
    host._log(message)
    if effect.action in {"keep_dialog", "alert"}:
        QMessageBox.warning(host, "无法完成", message)
    elif effect.action == "rescan_hint":
        QMessageBox.information(host, "请重新读取程序列表", message)
    elif effect.action == "recovery_banner":
        resume = getattr(host, "_resume_name", None)
        banner = str(getattr(host, "_recovery_message", "") or "") or message
        host.apply_recovery_hold(banner, resume_name=resume)
    return effect.action


def open_delete_asset(host: Any, asset: FirmwareAsset) -> None:
    """删除程序：服务锁外反查 → 跨型号命中则影响对话框 → 确认 → 进回收站。

    反查由 ``delete_asset`` 内部完成，UI 不再自己调 ``find_references_to``，
    也不自行判定谁能删。
    """
    asset_path = str(asset.get("path", ""))
    if not asset_path:
        QMessageBox.information(host, "删除", "这一行没有磁盘路径，无法删除。")
        return
    if not host._write_gate(asset_path):
        return
    name = str(asset.get("directory_name", "")) or Path(asset_path).name

    def call(confirm_shared: bool, log: Any) -> dict[str, Any]:
        return delete_asset(
            host.root_dir,
            host.root_dir,
            asset_path,
            confirm_shared=confirm_shared,
            log_fn=log,
        )

    def done(result: dict[str, Any]) -> None:
        action = present_result(host, result, "delete_asset")
        if action == "delete_confirm":
            payload = result.get("payload") or {}
            if not _confirm_delete_impact(host, name, result, payload):
                return
            host.run_write("删除", lambda log: call(True, log), done)
            return
        if action in {"toast", "rescan_hint"}:
            # 不再弹 5 秒撤销条：删除进内置回收站，还原入口在回收站页面。
            host._refresh_main_grid(reload_data=True)

    host.run_write("删除", lambda log: call(False, log), done)


def _confirm_delete_impact(
    host: Any, name: str, result: dict[str, Any], payload: dict[str, Any]
) -> bool:
    """影响对话框：列出跨型号命中与备用副本，说明回收站保留期。"""
    lines = [str(result.get("message") or f"确认删除「{name}」？")]
    hits = list(payload.get("hits") or [])
    if hits:
        lines.append("")
        lines.append(f"引用命中 {len(hits)} 条：")
        for hit in hits:
            owner = str(hit.get("owner_root", "")) or "未知位置"
            kind = str(hit.get("kind", "")) or "引用"
            lines.append(f" · {owner}（{kind}）")
    retired = int(payload.get("retired_copies") or 0)
    if retired:
        lines.append("")
        lines.append(f"该程序另有 {retired} 份备用副本，不随本次删除一起移除。")
    lines.append("")
    lines.append(
        "删除后进入软件的回收站，1 小时内可在那里还原。"
        "超时或手动清理后永久删除，电脑回收站里也没有，届时需要手动重新新增。"
    )
    answer = QMessageBox.question(host, "确认删除", "\n".join(lines))
    return answer == QMessageBox.StandardButton.Yes


def open_create_model(host: Any) -> None:
    if not host._write_gate(host.root_dir):
        return
    dialog = QDialog(host)
    dialog.setWindowTitle("新增型号")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
    name = QLineEdit(dialog)
    name.setPlaceholderText("型号名称")
    chassis = QComboBox(dialog)
    chassis.addItem("请选择机芯类型", "")
    for item in _CHASSIS:
        chassis.addItem(item, item)
    layout.addWidget(QLabel("名称"))
    layout.addWidget(name)
    layout.addWidget(QLabel("机芯类型"))
    layout.addWidget(chassis)
    buttons = _button_row(dialog, "创建")
    layout.addLayout(buttons[0])

    def submit() -> None:
        model_name = name.text().strip()
        chosen = str(chassis.currentData() or "")
        if not model_name or not chosen:
            QMessageBox.information(host, "新增型号", "请填写名称并选择机芯类型。")
            return
        buttons[1].setEnabled(False)

        def run(log: Any) -> dict[str, Any]:
            return create_model(
                host.root_dir, host.root_dir, model_name, chosen, log_fn=log
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "create_model")
            if action in {"toast", "rescan_hint"}:
                dialog.accept()
                host.refresh_model_chips(model_name)
            else:
                buttons[1].setEnabled(True)

        host.run_write("新增型号", run, done)

    buttons[1].clicked.connect(submit)
    dialog.exec()


def _selected_model_root(host: Any) -> Path | None:
    model_name = host.current_selection.model_name
    model_root = host.model_root_for(model_name) if model_name else None
    if model_root is None:
        QMessageBox.information(host, "型号管理", "请先选择型号。")
    return model_root


def _selected_scheme_root(host: Any) -> Path | None:
    model_root = _selected_model_root(host)
    if model_root is None:
        return None
    names = direct_scheme_dir_names(model_root)
    if not names:
        QMessageBox.information(host, "方案管理", "当前型号还没有定制方案。")
        return None
    current = host.current_selection.scheme_name
    if host.current_selection.node_type == "custom_scheme" and current in names:
        return model_root / "定制" / current
    name, accepted = QInputDialog.getItem(host, "选择定制方案", "方案", names, 0, False)
    return model_root / "定制" / name if accepted and name else None


def open_rename_model(host: Any) -> None:
    target = _selected_model_root(host)
    if target is None or not host._write_gate(target):
        return
    name, accepted = QInputDialog.getText(host, "重命名型号", "新名称", text=target.name)
    new_name = name.strip()
    if not accepted or not new_name or new_name == target.name:
        return

    def done(result: dict[str, Any]) -> None:
        if present_result(host, result, "rename_model") in {"toast", "rescan_hint"}:
            host.refresh_model_chips(new_name)

    host.run_write(
        "重命名型号",
        lambda log: rename_model(host.root_dir, host.root_dir, target, new_name, log_fn=log),
        done,
    )


def open_create_scheme(host: Any) -> None:
    model_root = _selected_model_root(host)
    if model_root is None or not host._write_gate(model_root):
        return
    name, accepted = QInputDialog.getText(host, "新建定制方案", "方案名")
    scheme_name = name.strip()
    if not accepted or not scheme_name:
        return

    def done(result: dict[str, Any]) -> None:
        if present_result(host, result, "create_scheme") in {"toast", "rescan_hint"}:
            host._refresh_main_grid(reload_data=True)

    host.run_write(
        "新建方案",
        lambda log: create_scheme(host.root_dir, host.root_dir, model_root, scheme_name, log_fn=log),
        done,
    )


def open_rename_scheme(host: Any) -> None:
    target = _selected_scheme_root(host)
    if target is None or not host._write_gate(target):
        return
    name, accepted = QInputDialog.getText(host, "重命名定制方案", "新名称", text=target.name)
    new_name = name.strip()
    if not accepted or not new_name or new_name == target.name:
        return

    def done(result: dict[str, Any]) -> None:
        if present_result(host, result, "rename_scheme") in {"toast", "rescan_hint"}:
            host.current_selection.scheme_name = new_name
            host._refresh_main_grid(reload_data=True)

    host.run_write(
        "重命名方案",
        lambda log: rename_scheme(host.root_dir, host.root_dir, target, new_name, log_fn=log),
        done,
    )


def _open_delete_container(host: Any, kind: str) -> None:
    target = _selected_model_root(host) if kind == "model" else _selected_scheme_root(host)
    if target is None or not host._write_gate(target):
        return
    label = "型号" if kind == "model" else "定制方案"
    answer = QMessageBox.question(
        host, f"删除{label}",
        f"确认删除{label}「{target.name}」及其全部内容？\n删除后可在回收站还原。",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    service = delete_model if kind == "model" else delete_scheme

    def run(confirm_shared: bool, log: Any) -> dict[str, Any]:
        return service(
            host.root_dir, host.root_dir, target,
            confirm_shared=confirm_shared, log_fn=log,
        )

    def done(result: dict[str, Any]) -> None:
        action = present_result(host, result, f"delete_{kind}")
        if action == "delete_confirm":
            hits = list((result.get("payload") or {}).get("hits") or [])
            owners = "\n".join(f" · {hit.get('owner_root', '未知位置')}" for hit in hits)
            answer = QMessageBox.question(
                host, "确认跨型号影响",
                f"{result.get('message', '')}\n{owners}\n\n继续删除？",
            )
            if answer == QMessageBox.StandardButton.Yes:
                host.run_write(f"删除{label}", lambda log: run(True, log), done)
            return
        if action in {"toast", "rescan_hint"}:
            if kind == "model":
                host.refresh_model_chips()
            else:
                host.current_selection.node_type = "all"
                host.current_selection.scheme_name = ""
                host._refresh_main_grid(reload_data=True)

    host.run_write(f"删除{label}", lambda log: run(False, log), done)


def open_delete_model(host: Any) -> None:
    _open_delete_container(host, "model")


def open_delete_scheme(host: Any) -> None:
    _open_delete_container(host, "scheme")


def open_create_asset(host: Any) -> None:
    model_name = host.current_selection.model_name
    model_root = host.model_root_for(model_name)
    if model_root is None:
        QMessageBox.information(host, "新建程序", "请先选择型号。")
        return
    if not host._write_gate(model_root):
        return
    dialog = QDialog(host)
    dialog.setWindowTitle("新建程序")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
    source_label = QLabel("尚未选择来源", dialog)
    modules = QComboBox(dialog)
    modules.setObjectName("module_combo")
    for item in enabled_firmware_types():
        modules.addItem(str(item.get("label") or item.get("key")), item.get("label"))
    asset_name = QLineEdit(dialog)
    vendors = QComboBox(dialog)
    vendors.setObjectName("vendor_combo")
    vendors.setEditable(True)
    candidates = load_vendor_candidates()
    for item in candidates:
        vendors.addItem(item)
    if candidates:
        vendors.setCurrentIndex(0)
    else:
        vendors.setEditText("")
    hint = QLabel("", dialog)
    hint.setWordWrap(True)

    layout.addWidget(QLabel("来源"))
    layout.addWidget(source_label)
    source_row = QHBoxLayout()
    file_button = QPushButton("选择文件", dialog)
    borrow_button = QPushButton("使用其他型号的程序", dialog)
    source_row.addWidget(file_button)
    source_row.addWidget(borrow_button)
    layout.addLayout(source_row)

    layout.addWidget(QLabel("程序类型"))
    layout.addWidget(modules)

    # 借用只跟随来源程序的后续更新，不再选择固定版本。
    borrow_source = QComboBox(dialog)
    borrow_source.setObjectName("borrow_source_combo")
    borrow_source_label = QLabel("源程序", dialog)
    layout.addWidget(borrow_source_label)
    layout.addWidget(borrow_source)

    layout.addWidget(QLabel("程序名"))
    layout.addWidget(asset_name)
    layout.addWidget(QLabel("厂商"))
    layout.addWidget(vendors)

    # 范围：通用 / 各方案 / + 新建定制方案…
    layout.addWidget(QLabel("通用/定制"))
    scope = ScopeCombo(
        dialog,
        model_root=Path(model_root),
        on_create=_scheme_creator(host, Path(model_root)),
    )
    layout.addWidget(scope)

    layout.addWidget(hint)
    chosen: dict[str, Any] = {"kind": "", "source": None, "files": []}
    borrow_state: dict[str, Any] = {"on": False, "assets": []}
    buttons = _button_row(dialog, "保存")
    layout.addLayout(buttons[0])

    def sync_mode() -> None:
        """借用与导入互斥：切换时同步各控件可用性。"""
        on = bool(borrow_state["on"])
        borrow_source_label.setVisible(on)
        borrow_source.setVisible(on)
        borrow_source.setEnabled(on)
        # 借用不产生新程序目录，也只支持通用；程序类型由源程序决定
        modules.setEnabled(not on)
        asset_name.setEnabled(not on)
        vendors.setEnabled(not on)
        scope.set_common_only(on)
        if on:
            asset_name.setText("")
            hint.setText("使用其他型号的程序只支持通用范围，不产生新的程序目录。")

    sync_mode()

    def apply_pick(directory: str | None = None, files: list[str] | None = None) -> None:
        picked = classify_create_source(directory=directory, files=files)
        if not picked.kind:
            return
        chosen["kind"] = picked.kind
        chosen["source"] = picked.source
        chosen["files"] = list(picked.files)
        source_label.setText(describe_create_source(picked))
        asset_name.setText(
            prefill_asset_name(
                source_kind=picked.kind,
                source=picked.source,
                files=chosen["files"],
            )
        )
        hint.setText(handcontrol_gap_hint(chosen["files"], asset_name.text()))

    def enter_file_mode() -> None:
        """点「选择文件」立即切回导入模式；取消文件对话框也不回到借用。"""
        if not borrow_state["on"]:
            return
        borrow_state["on"] = False
        source_label.setText("尚未选择来源")
        hint.setText("")
        sync_mode()

    def pick_files() -> None:
        enter_file_mode()
        paths, _selected_filter = QFileDialog.getOpenFileNames(dialog, "选择文件")
        if not paths:
            return
        apply_pick(files=list(paths))

    def reload_borrow_candidates() -> None:
        finder = getattr(host, "borrow_candidates_for", None)
        assets: list[FirmwareAsset] = list(finder("")) if finder else []
        borrow_state["assets"] = assets
        borrow_source.blockSignals(True)
        borrow_source.clear()
        if not assets:
            borrow_source.addItem("其他型号还没有程序")
            borrow_source.setCurrentIndex(0)
            borrow_source.blockSignals(False)
            return
        model_of = getattr(host, "asset_model_name", None)
        for item in assets:
            model = (
                (model_of(item) if model_of else "")
                or str(item.get("model", ""))
                or "未知型号"
            )
            kind = module_label_from_asset(item)
            name = str(item.get("directory_name", "")) or "未命名"
            borrow_source.addItem(f"{model} / {kind} / {name}")
        borrow_source.setCurrentIndex(0)
        borrow_source.blockSignals(False)
        sync_module_to_source()

    def sync_module_to_source() -> None:
        """程序类型跟随所选源程序，借用不能改类型。"""
        assets = list(borrow_state["assets"])
        index = borrow_source.currentIndex()
        if not assets or index < 0 or index >= len(assets):
            return
        key = canonical_module_dir(module_label_from_asset(assets[index]))
        for i in range(modules.count()):
            label = str(modules.itemData(i) or modules.itemText(i))
            if canonical_module_dir(label) == key:
                modules.setCurrentIndex(i)
                return

    def enter_borrow_mode() -> None:
        borrow_state["on"] = True
        chosen["kind"] = ""
        chosen["source"] = None
        chosen["files"] = []
        source_label.setText("已选择：使用其他型号的程序")
        reload_borrow_candidates()
        sync_mode()

    file_button.clicked.connect(pick_files)
    borrow_button.clicked.connect(enter_borrow_mode)
    borrow_source.currentIndexChanged.connect(lambda _i: sync_module_to_source())

    def submit_borrow() -> None:
        assets = list(borrow_state["assets"])
        index = borrow_source.currentIndex()
        if not assets or index < 0 or index >= len(assets):
            QMessageBox.information(host, "新建程序", "请先选择要使用的程序。")
            return
        source_asset = assets[index]
        buttons[1].setEnabled(False)

        def run(
            log: Any, token: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return register_shared_module(
                host.root_dir,
                host.root_dir,
                model_root,
                source_asset,
                mode="follow_asset",
                overwrite_token=token,
                log_fn=log,
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "register_shared_module")
            if action in {"toast", "rescan_hint"}:
                dialog.accept()
                host._refresh_main_grid(reload_data=True)
                return
            if action == "overwrite_confirm":
                token = dict(result.get("payload", {})).get("overwrite_token")
                answer = QMessageBox.question(
                    host,
                    "覆盖关联",
                    str(result.get("message") or "该模块已有关联，确认覆盖？"),
                )
                if answer == QMessageBox.StandardButton.Yes and token is not None:
                    host.run_write(
                        "关联程序", lambda log: run(log, token), done
                    )
                    return
            buttons[1].setEnabled(True)

        host.run_write("关联程序", run, done)

    def submit_create() -> None:
        mode = str(chosen["kind"])
        source = chosen["source"]
        files = list(chosen["files"])
        if not source:
            QMessageBox.information(host, "新建程序", "请先选择来源。")
            return
        module_name = str(modules.currentData() or modules.currentText() or "")
        program_name = asset_name.text().strip()
        vendor = vendors.currentText().strip()
        if not program_name or not module_name:
            QMessageBox.information(host, "新建程序", "请填写程序名并选择程序类型。")
            return
        scheme_name = scope.scheme()
        scope_value = "定制" if scheme_name else "通用"
        payload_source: str | list[str] = files if mode == "files" else str(source)
        buttons[1].setEnabled(False)

        def run(log: Any) -> dict[str, Any]:
            return create_asset(
                host.root_dir,
                host.root_dir,
                source=payload_source,
                source_kind=mode,
                model_root=model_root,
                scope=scope_value,
                scheme_name=scheme_name,
                module_name=module_name,
                asset_name=program_name,
                vendor=vendor,
                log_fn=log,
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "create_asset")
            if action in {"toast", "rescan_hint"}:
                dialog.accept()
                host._refresh_main_grid(reload_data=True)
                return
            buttons[1].setEnabled(True)

        host.run_write("新建程序", run, done)

    def submit() -> None:
        if borrow_state["on"]:
            submit_borrow()
        else:
            submit_create()

    buttons[1].clicked.connect(submit)
    dialog.exec()


def open_change_vendor(host: Any) -> None:
    variant = host.grid_panel.get_selected_variant()
    if variant is None:
        QMessageBox.information(host, "改厂商", "请先在网格中选择一个程序。")
        return
    asset: FirmwareAsset = variant.asset
    if not host._write_gate(asset.get("path", "")):
        return
    current = str(asset.get("vendor") or "")
    dialog = QDialog(host)
    dialog.setWindowTitle("改厂商")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
    combo = QComboBox(dialog)
    combo.setEditable(True)
    options = list(load_vendor_candidates())
    if current and current not in options:
        options.insert(0, current)
    for item in options:
        combo.addItem(item)
    if current:
        combo.setCurrentText(current)
    elif options:
        combo.setCurrentIndex(0)
    layout.addWidget(combo)
    buttons = _button_row(dialog, "保存")
    layout.addLayout(buttons[0])

    def submit() -> None:
        vendor = combo.currentText().strip()
        buttons[1].setEnabled(False)

        def run(log: Any) -> dict[str, Any]:
            return update_asset_vendor(
                host.root_dir, host.root_dir, asset, vendor, log_fn=log
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "update_vendor")
            if action in {"toast", "rescan_hint"} or result.get("code") == "stale_plan":
                dialog.accept()
                if result.get("ok"):
                    host._refresh_main_grid(reload_data=True)
            else:
                buttons[1].setEnabled(True)

        host.run_write("改厂商", run, done)

    buttons[1].clicked.connect(submit)
    dialog.exec()


def _scheme_of(model_root: Path, asset_path: Path) -> str:
    """程序所在方案目录名；在通用下返回空串。"""
    try:
        parts = asset_path.relative_to(model_root).parts
    except ValueError:
        return ""
    return parts[1] if len(parts) > 1 and parts[0] == "定制" else ""


def _scheme_creator(host: Any, model_root: Path) -> Any:
    """范围下拉「+ 新建定制方案…」的落盘回调。"""

    def create(name: str, finish: Any) -> None:
        def run(log: Any) -> dict[str, Any]:
            return create_scheme(
                host.root_dir, host.root_dir, model_root, name, log_fn=log
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "create_scheme")
            if action in {"toast", "rescan_hint"}:
                host._refresh_main_grid(reload_data=True)
            finish(action in {"toast", "rescan_hint"})

        host.run_write("新建方案", run, done)

    return create


_UPDATE_KIND_TEXT = {
    "update": "换成新程序",
    "change_type": "改程序类型",
    "general_to_custom": "从通用移到定制方案",
    "custom_to_general": "从定制方案移回通用",
    "custom_scheme_move": "换到另一个定制方案",
}


def open_update_program(host: Any) -> None:
    """更新程序：表单填成目标样子，按改了哪几项派生操作（TASK-20260924）。"""
    variant = host.grid_panel.get_selected_variant()
    if variant is None:
        QMessageBox.information(host, "更新程序", "请先在网格中选择一个程序。")
        return
    if getattr(variant, "borrowed_only", False):
        QMessageBox.information(
            host, "更新程序", "这是关联的其他型号程序，请到源型号里更新。"
        )
        return
    asset: FirmwareAsset = variant.asset
    old_path = Path(str(asset.get("path") or ""))
    if not host._write_gate(str(old_path)):
        return
    found_root = host.model_root_for(host.current_selection.model_name)
    if found_root is None:
        QMessageBox.information(host, "更新程序", "请先选择型号。")
        return
    model_root = Path(found_root)
    old_module = module_label_from_asset(asset)
    old_scheme = _scheme_of(model_root, old_path)

    dialog = QDialog(host)
    dialog.setWindowTitle("更新程序")
    dialog.resize(560, 0)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
    scope_text = old_scheme or "通用"
    layout.addWidget(
        QLabel(f"正在更新：<b>{old_path.name} · {old_module} · {scope_text}</b>", dialog)
    )

    layout.addWidget(QLabel("新的程序", dialog))
    drop = FileDropZone(dialog)
    layout.addWidget(drop)

    layout.addWidget(QLabel("程序类型", dialog))
    modules = QComboBox(dialog)
    modules.setObjectName("module_combo")
    for item in enabled_firmware_types():
        modules.addItem(str(item.get("label") or item.get("key")), item.get("label"))
    for i in range(modules.count()):
        label = str(modules.itemData(i) or modules.itemText(i))
        if canonical_module_dir(label) == canonical_module_dir(old_module):
            modules.setCurrentIndex(i)
            break
    layout.addWidget(modules)

    layout.addWidget(QLabel("归属厂商", dialog))
    vendors = QComboBox(dialog)
    vendors.setObjectName("vendor_combo")
    vendors.setEditable(True)
    current_vendor = str(asset.get("vendor") or "")
    options = list(load_vendor_candidates())
    if current_vendor and current_vendor not in options:
        options.insert(0, current_vendor)
    for item in options:
        vendors.addItem(item)
    if current_vendor:
        vendors.setCurrentText(current_vendor)
    layout.addWidget(vendors)

    layout.addWidget(QLabel("通用/定制", dialog))
    scope = ScopeCombo(
        dialog, model_root=model_root, on_create=_scheme_creator(host, model_root)
    )
    scope.set_scheme(old_scheme)
    layout.addWidget(scope)

    layout.addWidget(QLabel("程序名称", dialog))
    program_name = QLineEdit(old_path.name, dialog)
    layout.addWidget(program_name)

    retire_row = QHBoxLayout()
    retire_row.addWidget(QLabel("旧程序", dialog))
    backup = QRadioButton("留作备用副本", dialog)
    trash = QRadioButton("删除旧程序", dialog)
    backup.setChecked(True)
    retire_group = QButtonGroup(dialog)
    retire_group.addButton(backup)
    retire_group.addButton(trash)
    retire_row.addWidget(backup)
    retire_row.addWidget(trash)
    retire_row.addStretch(1)
    layout.addLayout(retire_row)

    hint = QLabel("", dialog)
    hint.setObjectName("update_hint")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    buttons = _button_row(dialog, "更新")
    layout.addLayout(buttons[0])
    chosen: dict[str, list[str]] = {"files": []}

    def current_plan() -> ProgramUpdatePlan:
        return plan_program_update(
            model_root,
            old_path,
            old_module,
            old_scheme,
            str(modules.currentData() or modules.currentText()),
            scope.scheme(),
            program_name.text(),
        )

    def refresh() -> None:
        plan = current_plan()
        if not chosen["files"]:
            hint.setText("请选择新的程序文件。")
        elif not plan.kind:
            hint.setText(plan.error)
        else:
            hint.setText(f"将{_UPDATE_KIND_TEXT[plan.kind]}：{plan.new_path}")
        buttons[1].setEnabled(bool(chosen["files"]) and bool(plan.kind))

    def on_files(paths: list[str]) -> None:
        picked = classify_create_source(files=list(paths))
        if not picked.kind:
            return
        chosen["files"] = [str(item) for item in picked.files]
        drop.show_files(chosen["files"])
        prefilled = prefill_asset_name(
            source_kind=picked.kind, source=picked.source, files=list(picked.files)
        )
        if prefilled:
            program_name.setText(prefilled)
        refresh()

    drop.files_chosen.connect(on_files)
    modules.currentIndexChanged.connect(lambda _i: refresh())
    scope.scope_changed.connect(refresh)
    program_name.textChanged.connect(lambda _t: refresh())
    refresh()

    def execute(plan: ProgramUpdatePlan) -> None:
        files = list(chosen["files"])
        name = program_name.text().strip()
        vendor = vendors.currentText().strip()
        mode: RetireMode = "retire_to_trash" if trash.isChecked() else "retire_to_backup"

        def run(log: Any) -> dict[str, Any]:
            try:
                temp_root, source = stage_files_as_named_dir(files, name)
            except OSError as exc:
                return {
                    "ok": False,
                    "code": "source_unreadable",
                    "message": f"读取所选文件失败：{exc}",
                    "payload": {},
                }
            try:
                if plan.kind != "update":
                    return dict(
                        change_asset_semantics(
                            host.root_dir,
                            host.root_dir,
                            str(old_path),
                            str(source),
                            str(plan.new_path),
                            cast(ClearDefaultsKind, plan.kind),
                            retire_mode=mode,
                            vendor=vendor,
                            log_fn=log,
                        )
                    )
                result = dict(
                    update_asset(
                        host.root_dir,
                        host.root_dir,
                        asset,
                        str(source),
                        retire_mode=mode,
                        log_fn=log,
                    )
                )
                payload = dict(result.get("payload") or {})
                replacement = str(payload.get("replacement") or "")
                if result.get("ok") and vendor and replacement:
                    # update_asset 不带厂商：新程序上岗后再单独写入
                    written = update_asset_vendor(
                        host.root_dir,
                        host.root_dir,
                        cast(FirmwareAsset, {"path": replacement}),
                        vendor,
                        log_fn=log,
                    )
                    if not written.get("ok"):
                        log(f"厂商未写入：{written.get('message')}")
                return result
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "update_asset")
            if action in {"toast", "rescan_hint"}:
                dialog.accept()
                host._refresh_main_grid(reload_data=True)
                return
            if action != "keep_dialog":
                dialog.accept()
                return
            buttons[1].setEnabled(True)

        host.run_write("更新程序", run, done)

    def submit() -> None:
        plan = current_plan()
        if not chosen["files"] or not plan.kind:
            return
        buttons[1].setEnabled(False)

        def lookup(log: Any) -> dict[str, Any]:
            del log
            return dict(
                find_references_to(host.root_dir, host.root_dir, str(old_path), "asset")
            )

        def looked(result: dict[str, Any]) -> None:
            if not result.get("ok"):
                present_result(host, result, "update_asset")
                buttons[1].setEnabled(True)
                return
            found = dict(result.get("payload") or {}).get("result")
            if list(getattr(found, "hits", []) or []):
                answer = QMessageBox.question(
                    host,
                    "更新程序",
                    "以下关联或默认指向这个程序：\n"
                    f"{_format_hits(result)}\n\n确认更新？",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    buttons[1].setEnabled(True)
                    return
            execute(plan)

        host.run_write("查看引用", lookup, looked)

    buttons[1].clicked.connect(submit)
    dialog.exec()


def open_retired_versions(host: Any) -> None:
    """查看当前程序的备用副本，并调用受管服务恢复可交换的版本。"""
    variant = host.grid_panel.get_selected_variant()
    if variant is not None and getattr(variant, "borrowed_only", False):
        QMessageBox.information(
            host, "备用版本", "这是关联的其他型号程序，请到源型号查看备用版本。"
        )
        return
    selected_path = str(variant.asset.get("path") or "") if variant else ""
    if not selected_path:
        selected_path = QFileDialog.getExistingDirectory(
            host, "选择当前程序目录", str(host.root_dir)
        )
        if not selected_path:
            return
    current_path = Path(selected_path)
    listed = list_retired_versions(host.root_dir, host.root_dir, current_path)
    if not listed["ok"]:
        present_result(host, dict(listed), "restore_retired_version")
        return
    versions = cast(list[RetiredVersionView], listed["payload"].get("versions", []))

    dialog = QDialog(host)
    dialog.setWindowTitle(f"备用版本 · {current_path.name}")
    dialog.resize(660, 400)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
    explanation = QLabel(
        "恢复会把选中的旧程序重新放回原位置，当前程序会留作备用副本。"
        "不同程序类型或方案的副本暂不能直接恢复。",
        dialog,
    )
    explanation.setWordWrap(True)
    layout.addWidget(explanation)

    version_list = QListWidget(dialog)
    version_list.setObjectName("retired_versions_list")
    layout.addWidget(version_list)
    if versions:
        for index, entry in enumerate(versions):
            original_name = Path(entry["retired_from"]).name if entry["retired_from"] else "?"
            label = f"{original_name} · {entry['retired_at'] or '时间未知'}"
            if not entry["can_restore"]:
                label += f" · 不可恢复：{entry['reason']}"
            item = QListWidgetItem(label, version_list)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(entry["backup_path"])
    else:
        empty = QListWidgetItem("当前程序没有备用版本", version_list)
        empty.setFlags(Qt.ItemFlag.NoItemFlags)

    row = QHBoxLayout()
    row.addStretch(1)
    close_button = QPushButton("关闭", dialog)
    restore_button = QPushButton("恢复选中版本", dialog)
    restore_button.setObjectName("restore_retired_button")
    restore_button.setEnabled(False)
    row.addWidget(close_button)
    row.addWidget(restore_button)
    layout.addLayout(row)
    close_button.clicked.connect(dialog.reject)

    def selected_version() -> RetiredVersionView | None:
        item = version_list.currentItem()
        if item is None:
            return None
        index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not 0 <= index < len(versions):
            return None
        return versions[index]

    def selection_changed() -> None:
        entry = selected_version()
        restore_button.setEnabled(bool(entry and entry["can_restore"]))

    version_list.currentItemChanged.connect(lambda _now, _old: selection_changed())

    def restore() -> None:
        entry = selected_version()
        if entry is None or not entry["can_restore"]:
            return
        if not host._write_gate(str(current_path)):
            return
        answer = QMessageBox.question(
            dialog,
            "恢复备用版本",
            f"将「{Path(entry['retired_from']).name}」恢复为当前程序？\n"
            "现有程序会留作新的备用副本。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        restore_button.setEnabled(False)

        def run(log: Any) -> dict[str, Any]:
            return dict(
                restore_retired_version(
                    host.root_dir,
                    host.root_dir,
                    entry["backup_path"],
                    log_fn=log,
                )
            )

        def done(result: dict[str, Any]) -> None:
            action = present_result(host, result, "restore_retired_version")
            if action in {"toast", "rescan_hint"}:
                dialog.accept()
                host._refresh_main_grid(reload_data=True)
            elif action == "recovery_banner":
                dialog.reject()
            else:
                selection_changed()

        host.run_write("恢复备用版本", run, done)

    restore_button.clicked.connect(restore)
    dialog.exec()


def _format_hits(result: dict[str, Any]) -> str:
    lookup = result.get("payload", {}).get("result")
    hits = list(getattr(lookup, "hits", []) or [])
    if not hits:
        return "没有关联或默认指向这个程序"
    lines = [f"共 {len(hits)} 条"]
    for hit in hits:
        lines.append(f"{hit.kind} · {hit.module_key} · {hit.owner_root}")
    return "\n".join(lines)


def _button_row(dialog: QDialog, confirm_text: str) -> tuple[QHBoxLayout, QPushButton]:
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("取消", dialog)
    confirm = QPushButton(confirm_text, dialog)
    row.addWidget(cancel)
    row.addWidget(confirm)
    cancel.clicked.connect(dialog.reject)
    return row, confirm


def monotonic_now() -> float:
    return time.monotonic()
