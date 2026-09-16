"""Phase 0 spike: PySide6 + QFluentWidgets 渲染真实固件数据。

验证点：观感、中文字体、高 DPI、交互（左侧导航切换右侧表格）、
PyInstaller 可打包性、启动时长。

用法:
    uv run python scripts/spike_pyside6.py --root "D:\\按摩器程序"
    uv run python scripts/spike_pyside6.py --screenshot spike.png --quit-after 3000
"""

# ruff: noqa: E402  # sys.path 注入必须先于 fwasset/PySide6 导入，import 顺序为设计需要
from __future__ import annotations

import sys
import time

_T0 = time.perf_counter()

import argparse
import tempfile
from pathlib import Path

# 源码运行时保证 fwasset 可导入（frozen 时由 PyInstaller 收集）
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from fwasset.ui.view_models.scheme_workbench_model import SchemeWorkbenchModel
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FluentWindow,
    ListWidget,
    SearchLineEdit,
    Theme,
    TreeWidget,
    setTheme,
)

from fwasset.core.asset_index import save_assets
from fwasset.core.file_scan import scan_firmware_assets


def build_model(root: Path) -> tuple[SchemeWorkbenchModel, str]:
    db = Path(tempfile.mkdtemp()) / "spike.db"
    assets, _issues = scan_firmware_assets(str(root))
    save_assets(assets, str(root), path=db)
    model = SchemeWorkbenchModel()
    model.bind(db, root)
    models = model.load_all_models()
    return model, (models[0] if models else "")


class WorkbenchSpike(QWidget):
    """最小工作台：左导航（通用模块/定制方案）→ 右表格（模块层级）。"""

    def __init__(self, model: SchemeWorkbenchModel, model_name: str):
        super().__init__()
        self.setObjectName("workbenchSpike")
        self.model = model
        self.model_name = model_name

        layout = QHBoxLayout(self)

        # 左：导航列表
        self.nav = ListWidget(self)
        self.nav.setFixedWidth(240)
        tree_data = model.build_sidebar_tree(model_name)
        self._nav_entries: list[tuple[str, str]] = []  # (kind, key)
        for label, count in tree_data["common"].items():
            self.nav.addItem(f"{label} ({count})")
            self._nav_entries.append(("common", label))
        for scheme in tree_data["custom"]:
            self.nav.addItem(f"◆ {scheme}")
            self._nav_entries.append(("scheme", scheme))
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav)

        # 右：搜索 + 表格
        right = QVBoxLayout()
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("搜索模块 / 版本 / 方案 / 平台")
        right.addWidget(self.search)

        self.grid = TreeWidget(self)
        self.grid.setColumnCount(5)
        self.grid.setHeaderLabels(
            ["程序类型", "程序名称", "版本", "程序归属", "程序文件"]
        )
        self.grid.setColumnWidth(0, 170)
        self.grid.setColumnWidth(1, 260)
        self.grid.setColumnWidth(2, 90)
        self.grid.setColumnWidth(3, 110)
        right.addWidget(self.grid)
        layout.addLayout(right)

        if self._nav_entries:
            # 默认选中第一个定制方案（最能代表整机层级视图），否则第一项
            first_scheme = next(
                (
                    i
                    for i, (kind, _k) in enumerate(self._nav_entries)
                    if kind == "scheme"
                ),
                0,
            )
            self.nav.setCurrentRow(first_scheme)

    def _on_nav_changed(self, row: int) -> None:
        if not (0 <= row < len(self._nav_entries)):
            return
        kind, key = self._nav_entries[row]
        self.grid.clear()
        if kind == "scheme":
            rows = self.model.get_scheme_module_tree(self.model_name, key)
        else:
            cards = self.model.get_common_modules(self.model_name, key)
            from fwasset.ui.view_models.scheme_workbench_model import (
                ModuleRow,
                ModuleVariant,
            )

            variants = [
                ModuleVariant(
                    asset=c.asset,
                    name=str(c.asset.get("directory_name", "")),
                    version=str(c.asset.get("version", "")),
                    source_kind="common",
                    source_label="通用默认",
                    default_badge=c.default_badge,
                )
                for c in cards
            ]
            rows = (
                [
                    ModuleRow(
                        label=key,
                        source_kind="common",
                        source_label="通用默认",
                        variants=variants,
                    )
                ]
                if variants
                else []
            )

        for mrow in rows:
            if len(mrow.variants) == 1:
                v = mrow.variants[0]
                name = "默认" if v.name == mrow.label else v.name
                if v.default_badge:
                    name = f"{name}  {v.default_badge}"
                files = list(v.asset.get("files", []))
                item = QTreeWidgetItem(
                    [
                        mrow.label,
                        name,
                        v.version or "-",
                        v.source_label,
                        files[0] if files else "",
                    ]
                )
                self.grid.addTopLevelItem(item)
                continue
            parent = QTreeWidgetItem([mrow.label, "", "", mrow.source_label, ""])
            self.grid.addTopLevelItem(parent)
            for v in mrow.variants:
                name = v.name + (f"  {v.default_badge}" if v.default_badge else "")
                files = list(v.asset.get("files", []))
                parent.addChild(
                    QTreeWidgetItem(
                        [
                            "",
                            name,
                            v.version or "-",
                            v.source_label,
                            files[0] if files else "",
                        ]
                    )
                )
            parent.setExpanded(True)


class SpikeWindow(FluentWindow):
    def __init__(self, model: SchemeWorkbenchModel, model_name: str):
        super().__init__()
        self.setWindowTitle(f"fwasset · PySide6 Spike · 型号 {model_name or '-'}")
        self.resize(1180, 720)
        self.workbench = WorkbenchSpike(model, model_name)
        self.addSubInterface(self.workbench, FluentIcon.HOME, "程序资产工作台")
        placeholder = QWidget()
        placeholder.setObjectName("settingsPlaceholder")
        self.addSubInterface(placeholder, FluentIcon.SETTING, "设置（占位）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="", help="固件根目录（必填）")
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--quit-after", type=int, default=0, help="毫秒后自动退出")
    args = parser.parse_args()
    if not args.root:
        parser.error("--root 必填，如 --root D:\\按摩器程序")

    app = QApplication(sys.argv)
    setTheme(Theme.AUTO)

    model, model_name = build_model(Path(args.root))
    window = SpikeWindow(model, model_name)
    window.show()

    def _report_startup():
        print(f"STARTUP_MS={int((time.perf_counter() - _T0) * 1000)}", flush=True)

    QTimer.singleShot(0, _report_startup)

    if args.screenshot:

        def _grab():
            window.grab().save(args.screenshot)
            print(f"SCREENSHOT={args.screenshot}", flush=True)

        QTimer.singleShot(1200, _grab)

    if args.quit_after > 0:
        QTimer.singleShot(args.quit_after, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
