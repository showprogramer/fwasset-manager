"""首次配置向导。

冻结 exe 首次启动时，若 AppData 无配置文件，弹出此对话框引导用户
选择固件根目录（必填）和工具根目录（可选）。用户也可跳过，
待进入主界面后从“设置”完成配置。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)


class SetupWizard(QDialog):
    """首次配置和后续路径修改共用的对话框。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        root_dir: str = "",
        tool_root: str = "",
        allow_skip: bool = True,
    ) -> None:
        super().__init__(parent)
        self._allow_skip = allow_skip
        self.setWindowTitle("初始配置" if allow_skip else "程序文件夹设置")
        self.setMinimumWidth(540)
        self._initial_root_dir = root_dir
        self._initial_tool_root = tool_root
        self._root_edit: QLineEdit
        self._tool_edit: QLineEdit
        self._error_label: CaptionLabel
        self._config_written = None
        self._before_write: Callable[[], bool] | None = None
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(28, 24, 28, 24)

        layout.addWidget(SubtitleLabel(self.windowTitle(), self))

        if self._allow_skip:
            description_lines = [
                "欢迎使用固件资产管理工具。",
                "请选择固件根目录（存放各型号程序文件夹的目录），配置完成后即可开始使用。",
                "工具根目录为可选项，留空不影响主要功能。",
                "跳过不会保存配置，下次启动仍会显示此向导。也可进入主界面的“设置”完成配置。",
            ]
        else:
            description_lines = [
                "修改程序文件夹路径。",
                "留空的项将保持当前配置不变。",
            ]
        desc = BodyLabel("\n".join(description_lines), self)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 首次配置时固件根目录必填；修改场景留空表示保持原值（方案 A）。
        root_label = "固件根目录（必填）" if self._allow_skip else "固件根目录"
        self._root_edit, root_row = self._make_path_row(
            root_label, self._initial_root_dir
        )
        layout.addWidget(root_row)

        self._tool_edit, tool_row = self._make_path_row(
            "工具根目录（可选）", self._initial_tool_root
        )
        layout.addWidget(tool_row)

        self._error_label = CaptionLabel("", self)
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)
        layout.addSpacing(8)

        btn_row = QHBoxLayout()
        skip_btn = PushButton("跳过" if self._allow_skip else "取消")
        skip_btn.setFixedWidth(90)
        skip_btn.clicked.connect(self.reject)

        ok_btn = PrimaryPushButton("完成配置")
        ok_btn.setFixedWidth(110)
        ok_btn.clicked.connect(self._on_accept)

        btn_row.addWidget(skip_btn)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _make_path_row(
        self, label_text: str, initial_value: str
    ) -> tuple[QLineEdit, QWidget]:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(BodyLabel(label_text, container))

        row = QHBoxLayout()
        edit = QLineEdit(container)
        edit.setPlaceholderText(
            "点击“浏览”选择目录…" if self._allow_skip else "留空保持当前配置"
        )
        edit.setText(initial_value)
        row.addWidget(edit)

        browse_btn = PushButton("浏览", container)
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(lambda: self._browse(edit))
        row.addWidget(browse_btn)

        layout.addLayout(row)
        return edit, container

    # ------------------------------------------------------------------ Actions
    def _browse(self, edit: QLineEdit) -> None:
        start = edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择目录", start)
        if path:
            edit.setText(path)

    def _on_accept(self) -> None:
        error = self.validate_paths()
        self._error_label.setText(error)
        if error:
            return
        if self._before_write is not None and not self._before_write():
            self._error_label.setText("已取消切换，配置未修改。")
            return
        written = self.write_config()
        self._config_written = written
        if isinstance(written, dict) and not written.get("ok"):
            self._error_label.setText(str(written.get("message") or ""))
            return
        self.accept()

    # ------------------------------------------------------------------ Public helpers
    def set_before_write(self, callback: Callable[[], bool] | None) -> None:
        """写盘前的确认。返回假则不写、不关闭，草稿留在表单里。"""
        self._before_write = callback

    def root_dir(self) -> str:
        return self._root_edit.text().strip()

    def tool_root(self) -> str:
        return self._tool_edit.text().strip()

    def resolved_root_dir(self) -> str:
        """实际写入配置的固件根目录：修改场景留空时保持原值。"""
        return self.root_dir() or (
            "" if self._allow_skip else self._initial_root_dir.strip()
        )

    def resolved_tool_root(self) -> str:
        """实际写入配置的工具根目录：修改场景留空时保持原值。"""
        return self.tool_root() or (
            "" if self._allow_skip else self._initial_tool_root.strip()
        )

    def validate_paths(self) -> str:
        """返回首个用户可见校验错误；空字符串表示可保存。

        首次配置（``allow_skip=True``）强制要求固件根目录有效；
        修改场景允许留空以保持原配置，但填写的项必须是有效目录。
        """
        root_dir = self.root_dir()
        if self._allow_skip and not root_dir:
            return "请选择固件根目录。"
        if root_dir and not Path(root_dir).is_dir():
            return "固件根目录不存在或不是目录，请重新选择。"

        tool_root = self.tool_root()
        if tool_root and not Path(tool_root).is_dir():
            return "工具根目录不存在或不是目录，请重新选择。"
        return ""

    def write_config(self, config_path: Path | None = None):
        """将已校验的路径写入配置，保留厂商名单等其他键。"""
        from fwasset.core.settings import save_path_settings

        return save_path_settings(
            self.resolved_root_dir(),
            self.resolved_tool_root(),
            config_path=config_path,
        )
