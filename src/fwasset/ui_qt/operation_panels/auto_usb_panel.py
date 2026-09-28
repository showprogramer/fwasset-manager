from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QMessageBox
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    PrimaryPushButton,
)

from fwasset.core.asset_helpers import (
    asset_rom_pkg_files,
    asset_usb_flow,
    handcontrol_copy_filenames,
)
from fwasset.core.services.flash_service import run_handcontrol_copy
from fwasset.core.services.music_flash_service import run_music_flash
from fwasset.ui_qt.operation_panels.base import BaseOperationPanel
from fwasset.ui_qt.operation_panels.registry import register

COPY_TO_USB_TEXT = "格式化并复制到 U 盘"


@register("auto_usb")
class AutoUsbPanel(BaseOperationPanel):
    """auto_usb 类型操作面板：把手控或音乐文件复制到 U 盘。"""

    def build(self):
        usb_flow = asset_usb_flow(self.asset)
        if usb_flow == "directory_copy":
            self._build_directory_copy_usb_ops()
            return

        names = handcontrol_copy_filenames(list(self.asset.get("files", [])))
        if usb_flow == "paired_files" and names:
            btn = PrimaryPushButton(FluentIcon.SAVE, COPY_TO_USB_TEXT, self)
            btn.clicked.connect(self._copy_handcontrol_to_usb)
            self.body.addWidget(btn)
            self.body.addWidget(CaptionLabel("会清空 U 盘，完成后自动弹出", self))
            return

        if usb_flow == "paired_files":
            rom_file, pkg_file = asset_rom_pkg_files(self.asset)
            missing: list[str] = []
            if rom_file and not pkg_file:
                missing.append("PKG")
            elif pkg_file and not rom_file:
                missing.append("ROM")
            if missing:
                self._add_text(
                    f"当前手控资源缺少 {' / '.join(missing)} 文件，无法复制到 U 盘。"
                )
                return
            self._add_text("当前手控资源没有可复制的固件文件。")
            return

        self._add_text("当前 USB 流程未配置，无法确定刷写方式。")

    def _add_text(self, text: str) -> None:
        label = BodyLabel(text, self)
        label.setWordWrap(True)
        self.body.addWidget(label)

    def _build_directory_copy_usb_ops(self):
        row = QHBoxLayout()
        self.format_first = CheckBox("格式化", self)
        self.format_first.setChecked(True)
        row.addWidget(self.format_first)
        self.eject_after = CheckBox("完成后弹出", self)
        self.eject_after.setChecked(True)
        row.addWidget(self.eject_after)
        row.addStretch(1)
        self.body.addLayout(row)

        run_btn = PrimaryPushButton(FluentIcon.SAVE, "执行目录刷机流程", self)
        run_btn.clicked.connect(self._run_directory_flash)
        self.body.addWidget(run_btn)
        hint = CaptionLabel("按目录复制到 U 盘，仅适用于音乐文件资源。", self)
        hint.setWordWrap(True)
        self.body.addWidget(hint)

    # -- USB 操作方法 --
    def _resolve_drive(self) -> str:
        """取全局选择的 U 盘盘符；为空则提示并返回空串。"""
        drive = (self._panel_host.get_global_usb_drive() or "").strip()
        if not drive:
            QMessageBox.warning(self, "未选择 U 盘", "请先选择目标 U 盘后再继续。")
        return drive

    def _copy_handcontrol_to_usb(self):
        asset = self._panel_host._selected_asset()
        drive = self._resolve_drive()
        if not asset or not drive:
            return
        if not handcontrol_copy_filenames(list(asset.get("files", []))):
            return

        reply = QMessageBox.question(
            self,
            "确认格式化",
            f"将格式化 U 盘 {drive}（FAT32），所有现有内容将被清除，"
            "然后把程序文件复制到 U 盘并弹出。\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        source_dir = str(asset["path"])
        self._panel_host._run_task(
            "复制到 U 盘",
            lambda log_fn: run_handcontrol_copy(drive, source_dir, log_fn=log_fn),
        )

    def _run_directory_flash(self):
        asset = self._panel_host._selected_asset()
        drive = self._resolve_drive()
        if not asset or not drive:
            return
        format_first = self.format_first.isChecked()
        eject_after = self.eject_after.isChecked()
        self._panel_host._run_task(
            "目录刷机",
            lambda log_fn: run_music_flash(
                asset["path"],
                drive,
                format_first=format_first,
                eject_after=eject_after,
                log_fn=log_fn,
            ),
            lambda result: self._log(f"结果: {result.get('message')}"),
        )
