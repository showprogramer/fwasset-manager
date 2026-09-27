from __future__ import annotations

import importlib
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QDialog,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    FluentIcon,
    FluentWindow,
    ListWidget,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    StrongBodyLabel,
    SubtitleLabel,
    Theme,
    TogglePushButton,
    setTheme,
)

from fwasset.core.asset_helpers import open_path_in_explorer
from fwasset.core.logging_utils import FileLogger
from fwasset.core.quarantine import recover_on_startup, sweep_expired
from fwasset.core.reference_lookup import enumerate_model_roots
from fwasset.core.services.layout_update_service import (
    resume_change_asset_semantics,
    resume_normalize_module_leaf,
    resume_restore_retired_version,
    resume_update_asset,
)
from fwasset.core.services.platform_default_service import canonical_module_dir
from fwasset.core.services.scan_service import (
    build_cached_scan_result,
    build_scan_result,
)
from fwasset.core.settings import DEFAULT_ROOT
from fwasset.core.staging_io import cleanup_staging_area
from fwasset.core.types import FirmwareAsset, QuarantineRecord, ServiceResult
from fwasset.core.usb_ops import get_usb_drives
from fwasset.core.workspace_transaction import (
    WorkspaceLock,
    load_workspace_status,
    recover_interrupted_workspace,
)
from fwasset.ui_common.view_models.scan_state_model import ScanStateModel
from fwasset.ui_common.view_models.scheme_workbench_model import (
    ModuleCardData,
    ModuleVariant,
    SchemeWorkbenchModel,
    WorkbenchSelection,
)
from fwasset.ui_common.workbench_helpers import (
    flash_mode_label,
    model_chip_values,
    module_label_from_asset,
    shared_conflict_prompt_message,
    shared_register_action_label,
    shared_register_dialog_title,
    shared_replace_action_label,
    shared_source_picker_caption,
    shared_unregister_action_label,
    shared_unregister_confirm_message,
    write_gate_check,
)
from fwasset.ui_common.workspace_actions import (
    borrow_resubmit,
    recovery_banner_text,
    resume_function_name,
    run_startup_recovery,
    undo_bar_callable,
)
from fwasset.ui_qt.data_grid import DataGrid
from fwasset.ui_qt.design_tokens import (
    SEARCH_MIN_WIDTH,
    SHARED_SOURCE_PICKER_DEFAULT_SIZE,
    SHARED_SOURCE_PICKER_ITEM_HEIGHT,
    SHARED_SOURCE_PICKER_MIN_WIDTH,
    SIDEBAR_WIDTH,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    SPACE_XXS,
    USB_COMBO_MIN_WIDTH,
)
from fwasset.ui_qt.entry_flows import (
    open_change_vendor,
    open_create_asset,
    open_create_model,
    open_delete_asset,
    open_retired_versions,
    open_update_program,
    present_result,
)
from fwasset.ui_qt.log_panel import LogPanel
from fwasset.ui_qt.operation_panels import get_panel
from fwasset.ui_qt.recycle_interface import RecycleInterface
from fwasset.ui_qt.repair_interface import RepairInterface
from fwasset.ui_qt.settings_interface import SettingsInterface

SEARCH_REFRESH_DEBOUNCE_MS = 180


def _reload_runtime_settings() -> tuple[str, str]:
    """重新读取本机配置，并刷新本模块持有的默认根目录绑定。"""
    import fwasset.core.settings as settings

    importlib.reload(settings)
    global DEFAULT_ROOT  # noqa: PLW0603
    DEFAULT_ROOT = settings.DEFAULT_ROOT
    return DEFAULT_ROOT, settings.TOOL_ROOT


def _format_source_item_qt(asset: FirmwareAsset) -> str:
    """来源选择对话框的一项：型号/变体 + 完整路径。"""
    model = str(asset.get("model", "")) or "未知型号"
    variant = str(asset.get("directory_name", "")) or "-"
    path = str(asset.get("path", ""))
    return f"{model} · {variant}\n{path}"


class WorkbenchInterface(QWidget):
    """程序资产工作台（Qt 版）。布局与交互对齐 ui/workbench_panel.py。"""

    # Signal 元类型只能是运行时类型，TypedDict（ServiceResult）不可用；
    # 类型契约由槽函数 _handle_scan_result 的参数标注承担。
    scan_result_ready = Signal(dict)
    # 后台任务结果回投
    # 传 (task_id, name, result)；on_done 按 id 在 UI 线程查找执行（不 marshal callable）
    _task_done = Signal(int, str, object)  # task_id, name, result
    _task_failed = Signal(int, str, str)  # task_id, name, error
    # 日志跨线程回投：worker 线程里调用 _log 时不能直写 QPlainTextEdit
    log_message = Signal(str)
    settings_requested = Signal()
    _recovery_ready = Signal(str, object)

    busy_message = "已有任务执行中，请稍后"
    scanning_message = "正在读取程序文件夹，请稍后再执行任务"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchInterface")

        self.root_dir: str = DEFAULT_ROOT
        self.workbench_model = SchemeWorkbenchModel()
        self.scan_state_model = ScanStateModel()
        self.current_selection = WorkbenchSelection()
        self._available_models: list[str] = []
        self._nav_entries: list[
            tuple[str, str]
        ] = []  # (kind, key)：all / common_type / custom_scheme
        self._file_logger = FileLogger()
        self._busy = False
        self.active_operation_panel = None
        self._task_seq = 0
        self._pending_task_callbacks: dict[int, object] = {}
        self._silent_tasks: set[int] = set()
        self._dialog_epoch = 0
        self._writes_held = False
        self._recovery_running = False
        self._recovered_for = ""
        self._resume_name: str | None = None
        self._recovery_message = ""
        self._undo_token = ""
        self._undo_started = 0.0
        self._undo_caller = None
        self.repair_interface = None
        self.recycle_interface = None

        self._build_layout()

        self.scan_result_ready.connect(self._handle_scan_result)
        self._task_done.connect(self._on_task_done)
        self._task_failed.connect(self._on_task_failed)
        self.log_message.connect(self._append_log)
        self._recovery_ready.connect(self._on_recovery_ready)
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.timeout.connect(self._expire_undo)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_REFRESH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._on_search_debounced)
        self.search_edit.textChanged.connect(lambda _t: self._search_timer.start())

        # 型号下拉的切换防抖：EditableComboBox 在输入文字恰好等于某项时会立即
        # 发 currentIndexChanged（"L36" 是 "L36双机芯-…" 的前缀，输到一半就会
        # 命中），所以切型号必须经短暂防抖确认，键入中间态被后续输入取消。
        self._pending_model = ""
        self._model_switch_timer = QTimer(self)
        self._model_switch_timer.setSingleShot(True)
        self._model_switch_timer.setInterval(250)
        self._model_switch_timer.timeout.connect(self._apply_pending_model)

        self._usb_timer = QTimer(self)
        self._usb_timer.setSingleShot(True)
        self._usb_timer.timeout.connect(self._refresh_usb)
        self._usb_timer.start(100)
        self._cached_load_timer = QTimer(self)
        self._cached_load_timer.setSingleShot(True)
        self._cached_load_timer.timeout.connect(self._load_cached_assets)
        self._cached_load_timer.start(100)

    # ------------------------------------------------------------------ 布局
    def _build_layout(self) -> None:
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(0, 0, SPACE_SM, SPACE_SM)
        root_layout.setSpacing(SPACE_MD)

        # --- 左侧边栏 ---
        sidebar = QFrame(self)
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_XS, 0)
        side_layout.addWidget(SubtitleLabel("程序资产", sidebar))
        side_layout.addWidget(CaptionLabel("通用模块与定制方案", sidebar))

        self.nav = ListWidget(sidebar)
        side_layout.addWidget(self.nav, stretch=1)
        self.nav.currentRowChanged.connect(self._on_nav_changed)

        self.scan_btn = PrimaryPushButton("重新读取程序文件夹", sidebar)
        self.scan_btn.clicked.connect(self._on_scan_button_click)
        side_layout.addWidget(self.scan_btn)
        side_layout.addSpacing(SPACE_SM)
        root_layout.addWidget(sidebar)

        # --- 右侧主区 ---
        main = QVBoxLayout()
        main.setSpacing(SPACE_SM)

        # 头部：标题行
        title_row = QHBoxLayout()
        self.header_title = SubtitleLabel("请选择左侧分类进行过滤", self)
        title_row.addWidget(self.header_title)
        title_row.addStretch(1)
        self.header_badge = StrongBodyLabel("", self)
        title_row.addWidget(self.header_badge)
        main.addLayout(title_row)

        # 配置提示（无按钮）：入口统一由左侧导航「设置」承担。
        self.configuration_notice = CaptionLabel(
            "尚未配置程序文件夹，请前往左侧「设置」完成配置。", self
        )
        self.configuration_notice.setVisible(not bool(self.root_dir.strip()))
        main.addWidget(self.configuration_notice)

        banner_row = QHBoxLayout()
        self.recovery_banner = CaptionLabel("", self)
        self.recovery_banner.setWordWrap(True)
        self.recovery_banner.hide()
        self.resume_button = PushButton("继续恢复", self)
        self.resume_button.hide()
        self.resume_button.clicked.connect(self._continue_recovery)
        banner_row.addWidget(self.recovery_banner, stretch=1)
        banner_row.addWidget(self.resume_button)
        main.addLayout(banner_row)

        self.undo_frame = QFrame(self)
        undo_row = QHBoxLayout(self.undo_frame)
        self.undo_label = CaptionLabel("", self.undo_frame)
        self.undo_button = PushButton("撤销", self.undo_frame)
        self.undo_button.clicked.connect(self._click_undo)
        undo_row.addWidget(self.undo_label, stretch=1)
        undo_row.addWidget(self.undo_button)
        self.undo_frame.hide()
        main.addWidget(self.undo_frame)

        entry_row = QHBoxLayout()
        for label, slot in (
            ("新增型号", lambda: open_create_model(self)),
            ("新建程序", lambda: open_create_asset(self)),
            ("更新程序", lambda: open_update_program(self)),
            ("备用版本", lambda: open_retired_versions(self)),
            ("改厂商", lambda: open_change_vendor(self)),
        ):
            button = PushButton(label, self)
            button.clicked.connect(slot)
            entry_row.addWidget(button)
        entry_row.addStretch(1)
        main.addLayout(entry_row)

        # 头部：过滤行（型号 chips + 搜索 + U盘）
        filter_row = QHBoxLayout()
        filter_row.addWidget(BodyLabel("型号", self))
        self.chip_bar = QHBoxLayout()
        self.chip_bar.setSpacing(SPACE_XS)
        filter_row.addLayout(self.chip_bar)
        self.model_hint = CaptionLabel("读取后显示可选型号", self)
        filter_row.addWidget(self.model_hint)
        filter_row.addStretch(1)

        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText("搜索模块 / 版本 / 方案 / 平台")
        self.search_edit.setMinimumWidth(SEARCH_MIN_WIDTH)
        filter_row.addWidget(self.search_edit, stretch=2)

        filter_row.addWidget(BodyLabel("U 盘", self))
        self.usb_combo = ComboBox(self)
        self.usb_combo.setMinimumWidth(USB_COMBO_MIN_WIDTH)
        filter_row.addWidget(self.usb_combo)
        usb_refresh = PushButton("刷新", self)
        usb_refresh.clicked.connect(self._refresh_usb)
        filter_row.addWidget(usb_refresh)
        main.addLayout(filter_row)

        # 数据表格
        self.grid_panel = DataGrid(self._log, self)
        self.grid_panel.selection_changed.connect(self._on_grid_selection_changed)
        self.grid_panel.variant_right_clicked.connect(self._on_grid_right_click)
        main.addWidget(self.grid_panel, stretch=1)

        # 操作区：选择摘要 + 按 flash_mode 挂载的操作面板
        ops = QFrame(self)
        self.ops_layout = QVBoxLayout(ops)
        self.ops_layout.setContentsMargins(SPACE_XS, SPACE_XXS, SPACE_XS, SPACE_XXS)
        self.selection_summary = CaptionLabel("未选择变体", ops)
        self.ops_layout.addWidget(self.selection_summary)
        self.ops_placeholder = BodyLabel("展开模块并选择具体变体以查看操作", ops)
        self.ops_layout.addWidget(self.ops_placeholder)
        main.addWidget(ops)

        # 日志
        self.log_panel = LogPanel(self)
        main.addWidget(self.log_panel)

        root_layout.addLayout(main, stretch=1)

    # ------------------------------------------------------------------ 日志
    def _log(self, message: str) -> None:
        # 可能被 worker 线程调用（扫描/烧录任务的 log_fn），经 Signal 回投 UI 线程
        self.log_message.emit(message or "")

    def _append_log(self, message: str) -> None:
        self.log_panel.write(message)
        self._file_logger.log(message)

    # ------------------------------------------------------------------ 后台任务（PanelHost）
    def _run_task(self, name: str, fn, on_done=None, *, silent: bool = False) -> None:
        if self._busy:
            self._log(self.busy_message)
            return
        if self.scan_state_model.is_scanning:
            self._log(self.scanning_message)
            return
        self._busy = True
        task_id = self._task_seq
        self._task_seq += 1
        # 回调只存 UI 侧 map，Signal 不传 callable
        if callable(on_done):
            self._pending_task_callbacks[task_id] = on_done
        if silent:
            self._silent_tasks.add(task_id)

        def worker():
            try:
                result = fn(self._log)
                self._task_done.emit(task_id, name, result)
            except Exception as exc:  # noqa: BLE001
                self._task_failed.emit(task_id, name, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_task_done(self, task_id: int, name: str, result) -> None:
        self._busy = False
        on_done = self._pending_task_callbacks.pop(task_id, None)
        silent = task_id in self._silent_tasks
        self._silent_tasks.discard(task_id)
        if callable(on_done):
            try:
                on_done(result)
            except Exception as exc:  # noqa: BLE001
                self._log(f"{name} 回调异常: {exc}")
        # run_write 把 ServiceResult 包在 epoch 里；按内层 ok 区分完成/失败。
        reported = result
        if (
            isinstance(result, dict)
            and "ok" not in result
            and isinstance(result.get("result"), dict)
            and "ok" in result["result"]
        ):
            reported = result["result"]
        if isinstance(reported, dict) and "ok" in reported:
            if reported.get("ok"):
                self._log(f"{name}完成")
            else:
                msg = str(reported.get("message") or "未知错误")
                self._log(f"{name}失败: {msg}")
                if not silent:
                    QMessageBox.warning(self, f"{name}失败", msg)
        else:
            self._log(f"{name}完成")

    def _on_task_failed(self, task_id: int, name: str, error: str) -> None:
        self._busy = False
        self._pending_task_callbacks.pop(task_id, None)
        self._log(f"{name}失败: {error}")
        QMessageBox.critical(self, f"{name}失败", error)

    # ------------------------------------------------------------------ PanelHost 选中资产与交接动作
    def _selected_asset(self) -> FirmwareAsset | None:
        data = self.grid_panel.get_selected_data()
        if not data or data.shared_state == "shared_missing":
            return None
        return data.effective_asset or data.asset

    def _open_current_asset_dir(self) -> None:
        data = self.grid_panel.get_selected_data()
        if data:
            open_path_in_explorer(
                str((data.effective_asset or data.asset).get("path", "")), self._log
            )

    def _copy_asset_dir_path(self) -> None:
        data = self.grid_panel.get_selected_data()
        if data:
            self._copy_to_clipboard(
                str((data.effective_asset or data.asset).get("path", ""))
            )

    def _copy_primary_file_path(self) -> None:
        data = self.grid_panel.get_selected_data()
        if not data:
            return
        asset = data.asset
        files = list(asset.get("files", []))
        path = str(asset.get("path", ""))
        if files:
            self._copy_to_clipboard(str(Path(path) / files[0]))
        else:
            self._copy_to_clipboard(path)

    def _launch_tool_and_open_asset_dir(self) -> None:
        panel = self.active_operation_panel
        if panel is not None and hasattr(panel, "_launch_current_tool"):
            panel._launch_current_tool()
        self._open_current_asset_dir()

    # ------------------------------------------------------------------ USB
    def _refresh_usb(self) -> None:
        drives = get_usb_drives() or [""]
        current = self.usb_combo.currentText()
        self.usb_combo.clear()
        self.usb_combo.addItems(drives)
        if current in drives:
            self.usb_combo.setCurrentText(current)
        self._log(f"U盘刷新: {', '.join([d for d in drives if d]) or '未发现'}")

    def get_global_usb_drive(self) -> str:
        return self.usb_combo.currentText()

    # ------------------------------------------------------------------ 配置状态
    def set_configuration_required(self, required: bool) -> None:
        """显示或隐藏未配置程序文件夹的非阻断提示。"""
        self.configuration_notice.setVisible(required)

    def set_configured_root(self, root_dir: str) -> None:
        """更新当前会话的根目录，并同步隐藏配置提示。"""
        previous = os.path.normcase((self.root_dir or "").strip())
        self.root_dir = root_dir
        self.set_configuration_required(not bool(root_dir.strip()))
        if self.repair_interface is not None:
            self.repair_interface.set_workspace(root_dir)
        if self.recycle_interface is not None:
            self.recycle_interface.bind(self, root_dir)
        current = os.path.normcase(root_dir.strip())
        if current and current != previous:
            self._recovered_for = ""
            self._maybe_start_recovery(root_dir)

    # ------------------------------------------------------------------ 扫描
    @property
    def _scan_cancel_event(self) -> threading.Event | None:
        return self.scan_state_model.cancel_event

    def _on_scan_button_click(self) -> None:
        if self._scan_cancel_event is not None:
            self._scan_cancel_event.set()
            self._log("正在取消读取，请稍候...")
            self.scan_btn.setText("取消中...")
            self.scan_btn.setEnabled(False)
            return
        self._start_scan()

    def _start_scan(self) -> None:
        """重新读取已在“设置”中配置的程序文件夹。"""
        root = DEFAULT_ROOT.strip()
        if not root:
            self._log("请先在“设置”中配置程序文件夹。")
            self.settings_requested.emit()
            return
        self.set_configured_root(root)
        self._read_program_folder(root)

    def _auto_scan(self, directory: str) -> None:
        """配置完成后自动读取指定的已配置根目录。"""
        if not directory or not directory.strip():
            return
        self.set_configured_root(directory)
        self._read_program_folder(directory)

    def _read_program_folder(self, directory: str) -> None:
        """执行单工作区的后台读取；调用方必须已确定根目录。"""
        if self._busy:
            self._log(self.busy_message)
            return

        cancel_event = threading.Event()
        self.scan_state_model.replace(cancel_event)
        self.scan_btn.setText("取消读取")
        self._log(f"开始读取程序文件夹: {directory}")

        def run_scan():
            result = build_scan_result(
                directory, log_fn=self._log, cancel_event=cancel_event
            )
            # Signal 跨线程 emit 自动走 queued connection，回到 UI 线程处理
            self.scan_result_ready.emit(result)

        threading.Thread(target=run_scan, daemon=True).start()

    def _load_cached_assets(self) -> None:
        result = build_cached_scan_result(log_fn=self._log)
        self._handle_scan_result(result)

    def _handle_scan_result(self, result: ServiceResult) -> None:
        self.scan_state_model.replace(None)
        self.scan_btn.setText("重新读取程序文件夹")
        self.scan_btn.setEnabled(True)

        if not result["ok"]:
            self._log(f"加载失败: {result['message']}")
            return

        # 取消：库未写、payload 为空，勿当「扫描完成 0 项」并错误 rebind
        if result.get("code") == "cancelled":
            self._log(str(result.get("message") or "程序列表读取已取消"))
            return

        payload = result["payload"]
        if "asset_count" in payload:
            self._log(f"已加载上次读取的程序列表，共有 {payload['asset_count']} 个项目")
        else:
            assets = payload.get("assets", [])
            errors = payload.get("errors", [])
            self._log(f"程序列表读取完成，共找到 {len(assets)} 个项目")
            if errors:
                self._log(f"读取过程中有 {len(errors)} 个错误")

        root = (self.root_dir or "").strip()
        configured_root: str | None = None
        if root:
            # 配置根存在：型号 id 自动写入走门闩（R8 规则 6）
            configured_root = root
        else:
            # 本机未配置 DEFAULT_ROOT 时，从最近一次扫描的 scan_meta 恢复扫描根，
            # 避免绑定到 "." 后型号列表退化成文件名解析的噪声假型号。
            # 注意：恢复出的根不得当作配置根——此时 configured_root 保持 None，
            # 型号 id 只读、不自动创建。
            meta = payload.get("scan_meta") or []
            if meta:
                root = str(meta[0].get("root_dir", "")).strip()
                if root:
                    self.root_dir = root
                    self._log(f"使用上次读取的程序文件夹: {root}")
        root_dir = Path(root) if root else Path(".")
        self.workbench_model.bind(None, root_dir, configured_root)

        models = self._directory_chip_names()
        self._refresh_model_selector(models)

        if models:
            if (
                not self.current_selection.model_name
                or self.current_selection.model_name not in models
            ):
                self._on_model_changed(models[0])
            else:
                self._refresh_model_selector()
                self._refresh_sidebar_tree()
                self._refresh_main_grid()

    # ------------------------------------------------------------------ 型号 chips
    def _on_model_changed(self, choice: str) -> None:
        # 只接受真实型号：可搜索下拉的中间输入态（如敲了一半的 "L5"）不得
        # 污染 model_name，否则视图会静默变空。
        if not choice or choice not in self._available_models:
            return
        self.current_selection.model_name = choice
        self.current_selection.node_type = "all"
        self._refresh_model_selector()
        self._refresh_sidebar_tree()
        self._refresh_main_grid()

    def _refresh_model_selector(self, models: list[str] | None = None) -> None:
        if models is not None:
            self._available_models = models
        while self.chip_bar.count():
            item = self.chip_bar.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        chips, _overflow = model_chip_values(
            self._available_models, self.current_selection.model_name
        )
        self.model_hint.setText("还没有型号" if not self._available_models else "")
        self.model_hint.setVisible(not chips)
        for model in chips:
            btn = TogglePushButton(model, self)
            btn.setChecked(model == self.current_selection.model_name)
            btn.clicked.connect(
                lambda _c=False, value=model: self._on_model_changed(value)
            )
            self.chip_bar.addWidget(btn)

        # 常驻可搜索型号下拉（含全部型号）：型号是作用域选择器，不走搜索框；
        # 型号少时也保留（用户要求，便于验证与键盘定位），多时是唯一入口。
        if self._available_models:
            combo = EditableComboBox(self)
            combo.addItems(self._available_models)
            combo.setPlaceholderText("搜索型号…")
            combo.setCurrentIndex(-1)
            combo.setMinimumWidth(150)
            completer = QCompleter(self._available_models, combo)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            combo.setCompleter(completer)
            # 不接 currentTextChanged（每键都发）；currentIndexChanged 只在文本
            # 精确命中某项/从补全菜单选中时发，再经防抖确认，见 _schedule_model_switch。
            combo.currentIndexChanged.connect(
                lambda i, c=combo: self._schedule_model_switch(c.itemText(i))
            )
            self.chip_bar.addWidget(combo)
            self.model_combo = combo

    def _schedule_model_switch(self, model_name: str) -> None:
        if not model_name or model_name not in self._available_models:
            return
        self._pending_model = model_name
        self._model_switch_timer.start()

    def _apply_pending_model(self) -> None:
        name = self._pending_model
        self._pending_model = ""
        if name and name != self.current_selection.model_name:
            self._on_model_changed(name)

    # ------------------------------------------------------------------ 侧边树
    def _nav_entry_matches_selection(self, kind: str, key: str) -> bool:
        sel = self.current_selection
        if kind == "all":
            return sel.node_type == "all"
        if kind == "common_type":
            return sel.node_type == "common_type" and sel.common_type == key
        if kind == "custom_scheme":
            return sel.node_type == "custom_scheme" and sel.scheme_name == key
        return False

    def _clear_nav_pointer_highlight(self) -> None:
        """清除 qfluentwidgets delegate 缓存的鼠标瞬态行。"""
        self.nav._setPressedRow(-1)
        self.nav._setHoverRow(-1)

    def _apply_nav_selection_highlight(self) -> None:
        """按 current_selection 定位高亮（重建列表后索引会变，不能依赖旧 currentRow）。"""
        target = -1
        for i, (kind, key) in enumerate(self._nav_entries):
            if kind == "section":
                continue
            if self._nav_entry_matches_selection(kind, key):
                target = i
                break
        # qfluentwidgets 的 delegate 会独立缓存鼠标按压/悬停行。搜索态点击后
        # 立即重建完整列表时，旧行号会映射到另一个项目并留下伪高亮。
        self._clear_nav_pointer_highlight()
        self.nav.blockSignals(True)
        try:
            if target >= 0:
                self.nav.setCurrentRow(
                    target,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
                item = self.nav.item(target)
                if item is not None:
                    self.nav.scrollToItem(item)
            else:
                self.nav.setCurrentRow(-1)
        finally:
            self.nav.blockSignals(False)
        # currentRowChanged 在 mousePressEvent 内同步触发；上述代码返回后，
        # 外层点击仍会把旧行重新写入 selection model。下一轮事件循环必须
        # 重新执行完整选择，而不只是清 delegate 的鼠标状态。
        QTimer.singleShot(0, self._finalize_nav_selection_highlight)

    def _finalize_nav_selection_highlight(self) -> None:
        """鼠标点击调用栈结束后，按逻辑选择强制同步 Qt 与 QFluent 状态。"""
        target = next(
            (
                i
                for i, (kind, key) in enumerate(self._nav_entries)
                if kind != "section" and self._nav_entry_matches_selection(kind, key)
            ),
            -1,
        )
        self._clear_nav_pointer_highlight()
        self.nav.blockSignals(True)
        try:
            if target >= 0:
                self.nav.setCurrentRow(
                    target,
                    QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
            else:
                self.nav.clearSelection()
                self.nav.setCurrentRow(-1)
        finally:
            self.nav.blockSignals(False)

    def _refresh_sidebar_tree(self) -> None:
        self.nav.blockSignals(True)
        self.nav.clear()
        self._nav_entries.clear()

        model_name = self.current_selection.model_name
        if not model_name:
            self.nav.blockSignals(False)
            return

        tree_data = self.workbench_model.build_sidebar_tree(model_name)
        search_kw = self.search_edit.text().lower().strip()

        def _add_entry(text: str, kind: str, key: str) -> None:
            item = QListWidgetItem(text)
            self.nav.addItem(item)
            self._nav_entries.append((kind, key))

        def _add_section(text: str) -> None:
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.nav.addItem(item)
            self._nav_entries.append(("section", ""))

        _add_entry("全部程序与模块", "all", "")

        if tree_data["common"]:
            _add_section("── 通用模块 ──")
            for fw_label, count in tree_data["common"].items():
                if search_kw and search_kw not in fw_label.lower():
                    continue
                _add_entry(f"{fw_label} ({count})", "common_type", fw_label)

        if tree_data["custom"]:
            _add_section("── 定制方案 ──")
            for scheme in tree_data["custom"]:
                if search_kw and search_kw not in scheme.lower():
                    continue
                _add_entry(f"◆ {scheme}", "custom_scheme", scheme)

        self.nav.blockSignals(False)
        # 重建后按逻辑选中项高亮（勿保留点击时的旧行号）
        self._apply_nav_selection_highlight()

    def _on_nav_changed(self, row: int) -> None:
        if not (0 <= row < len(self._nav_entries)):
            return
        kind, key = self._nav_entries[row]
        if kind == "section":
            return
        if kind == "all":
            self.current_selection.node_type = "all"
        elif kind == "common_type":
            self.current_selection.node_type = "common_type"
            self.current_selection.common_type = key
        elif kind == "custom_scheme":
            self.current_selection.node_type = "custom_scheme"
            self.current_selection.scheme_name = key
            # Issue 19-A：进入方案默认满树，清掉全局搜索残留（避免「以色列」滤成只剩手控）。
            # 必须同步重建侧栏：blockSignals 清搜索不会触发 debounce，否则侧栏
            # 仍按旧关键词过滤，其它定制方案会「消失」。重建后按 scheme 重定位高亮。
            if self.search_edit.text().strip():
                self.search_edit.blockSignals(True)
                self.search_edit.clear()
                self.search_edit.blockSignals(False)
                self._refresh_sidebar_tree()
        self._refresh_main_grid()

    # ------------------------------------------------------------------ 搜索
    def _on_search_debounced(self) -> None:
        self._refresh_sidebar_tree()
        self._refresh_main_grid()

    # ------------------------------------------------------------------ 主表格
    def _refresh_main_grid(self, *, reload_data: bool = False) -> None:
        """重绘网格。``reload_data`` 为真时先把磁盘真源重新读进缓存。

        写操作只改磁盘与 SQLite，进程缓存不会自己变；不重载就要重启应用
        才看得到正确结果。
        """
        if reload_data:
            self.workbench_model.reload()
            self._refresh_sidebar_tree()
        model_name = self.current_selection.model_name
        node_type = self.current_selection.node_type
        search_kw = self.search_edit.text().lower().strip()

        if not model_name or not node_type:
            self.header_title.setText("请选择左侧分类进行过滤")
            self.header_badge.setText("")
            return

        cards_data: list[ModuleCardData] = []
        if node_type == "all":
            self.header_title.setText(f"全部模块 ({model_name})")
            self.header_badge.setText("全部")
            cards_data = self.workbench_model.get_all_modules(model_name, search_kw)
        elif node_type == "common_type":
            fw_label = self.current_selection.common_type
            self.header_title.setText(f"通用模块: {fw_label}")
            self.header_badge.setText("通用模块")
            cards_data = self.workbench_model.get_common_modules(
                model_name, fw_label, search_kw
            )
        elif node_type == "custom_scheme":
            scheme_name = self.current_selection.scheme_name
            self.header_title.setText(f"定制方案: {scheme_name}")
            self.header_badge.setText("整机模块")
            rows = self.workbench_model.get_scheme_module_tree(
                model_name, scheme_name, search_kw
            )
            self.grid_panel.populate_tree(rows)
            self._on_grid_selection_changed(None)
            return

        self.grid_panel.populate(cards_data)
        self._on_grid_selection_changed(None)

    # ------------------------------------------------------------------ 选择与操作区
    def _on_grid_selection_changed(self, variant: ModuleVariant | None) -> None:
        # 先拆掉上一个操作面板，避免选行叠加旧面板
        if self.active_operation_panel is not None:
            self.ops_layout.removeWidget(self.active_operation_panel)
            self.active_operation_panel.deleteLater()
            self.active_operation_panel = None

        if not variant:
            self.selection_summary.setText("未选择变体")
            self.ops_placeholder.setText("展开模块并选择具体变体以查看操作")
            self.ops_placeholder.show()
            return

        if variant.shared_state == "shared_missing":
            self.selection_summary.setText("借用来源缺失 · 不可烧录")
            self.ops_placeholder.setText("借用来源缺失，未回落本地副本")
            self.ops_placeholder.show()
            return

        asset = variant.effective_asset or variant.asset
        mode = str(asset.get("flash_mode", "disabled")) or "disabled"
        module = str(asset.get("firmware_label", "")) or str(
            asset.get("firmware_type", "")
        )
        version = variant.version or str(asset.get("version", "")) or "-"
        self.selection_summary.setText(
            f"已选：{module} / {variant.name or str(asset.get('directory_name', ''))}"
            f" · {version} · {variant.source_label} · {flash_mode_label(mode)}"
        )

        PanelClass = get_panel(mode)
        if PanelClass is None:
            self.ops_placeholder.setText(f"暂不支持的操作模式: {mode}")
            self.ops_placeholder.show()
            return

        self.ops_placeholder.hide()
        panel = PanelClass(asset=asset, log_fn=self._log, panel_host=self)
        panel.build()
        self.ops_layout.addWidget(panel)
        self.active_operation_panel = panel

    # ------------------------------------------------------------------ 右键菜单（共享登记与目录操作）
    def _on_grid_right_click(self, variant: ModuleVariant, global_pos) -> None:
        menu = RoundMenu(parent=self)

        asset = variant.asset
        model_name = self.current_selection.model_name
        module = module_label_from_asset(asset)
        module_key = canonical_module_dir(module)
        gate = write_gate_check(
            DEFAULT_ROOT, self.root_dir, self._shared_write_target(variant)
        )
        write_ok = gate["ok"]

        # --- 共享登记（B2）：右键目标型号的模块行 → 登记/取消共享来源 ---
        if module_key:
            shared_res = self.workbench_model.resolve_shared_module(
                model_name, module_key
            )

            if shared_res is not None and write_ok:
                reg_action = Action(
                    FluentIcon.SYNC, shared_replace_action_label(module), menu
                )
                reg_action.triggered.connect(
                    lambda _c=False: self._register_shared_source(variant)
                )
                menu.addAction(reg_action)
                unreg_action = Action(
                    FluentIcon.CANCEL, shared_unregister_action_label(module), menu
                )
                unreg_action.triggered.connect(
                    lambda _c=False: self._unregister_shared(variant)
                )
                menu.addAction(unreg_action)
            elif write_ok:
                reg_action = Action(
                    FluentIcon.LINK, shared_register_action_label(module), menu
                )
                reg_action.triggered.connect(
                    lambda _c=False: self._register_shared_source(variant)
                )
                menu.addAction(reg_action)
            menu.addSeparator()

        open_action = Action(FluentIcon.FOLDER, "打开所在目录", menu)
        open_action.triggered.connect(lambda: self._open_asset_dir(variant))
        menu.addAction(open_action)
        copy_action = Action(FluentIcon.COPY, "复制目录路径", menu)
        copy_action.triggered.connect(
            lambda: self._copy_to_clipboard(str(variant.asset.get("path", "")))
        )
        menu.addAction(copy_action)

        # 删除：借用来源行不给——那份程序不属于本型号，要删得去源型号。
        if (
            write_ok
            and variant.shared_state != "shared_hit"
            and not variant.borrowed_only
        ):
            menu.addSeparator()
            delete_action = Action(FluentIcon.DELETE, "删除", menu)
            delete_action.triggered.connect(
                lambda _c=False: open_delete_asset(self, variant.asset)
            )
            menu.addAction(delete_action)

        menu.exec(global_pos)

    def _write_gate(self, target_path: str | Path) -> bool:
        """统一写入门闩（TASK-20260806 R1/R10）。

        先叠加「扫描/烧录进行中」两态，再走三检查纯函数；不通过时弹提示并
        返回 False，调用方必须据此中止写操作。恢复未完成时普通写入保持禁用。
        """
        if self._writes_held:
            QMessageBox.warning(
                self,
                "无法写入",
                self._recovery_message or "完成恢复前不能写入。",
            )
            return False
        if self.scan_state_model.is_scanning:
            QMessageBox.warning(self, "无法写入", self.scanning_message)
            return False
        if self._busy:
            QMessageBox.warning(self, "无法写入", self.busy_message)
            return False
        gate = write_gate_check(DEFAULT_ROOT, self.root_dir, target_path)
        if not gate["ok"]:
            QMessageBox.warning(self, "无法写入", gate["message"])
            return False
        return True

    def borrow_candidates_for(self, module: str = "") -> list[FirmwareAsset]:
        """其他型号的程序，供借用选择；``module`` 为空时不按程序类型过滤。

        新建程序的「使用其他型号的程序」列出全部其他型号的程序，程序类型跟随
        所选源程序；右键登记借用（``_register_shared_source``）按所在行的模块过滤。
        """
        target_model = self.current_selection.model_name
        if not target_model:
            return []
        module_key = canonical_module_dir(str(module)) if module else ""
        candidates: list[FirmwareAsset] = []
        for a in self.workbench_model._all_assets:
            try:
                owner = self.workbench_model._model_of_asset(a)
            except Exception:  # noqa: BLE001
                continue
            if not owner or owner == target_model:
                continue
            label = canonical_module_dir(module_label_from_asset(a))
            if module_key and label != module_key:
                continue
            candidates.append(a)
        return candidates

    def asset_model_name(self, asset: FirmwareAsset) -> str:
        """按所在型号目录给出型号名；资产自带的 model 来自文件名解析，可能不准。"""
        try:
            return str(self.workbench_model._model_of_asset(asset))
        except Exception:  # noqa: BLE001
            return ""

    # --- 共享登记入口对话框（B2）---
    def _shared_write_target(self, variant: ModuleVariant) -> str:
        """借用登记写的是当前型号的型号配置；只借用的行 asset 在源型号，不能拿它过门闩。"""
        if variant.borrowed_only:
            root = self.model_root_for(self.current_selection.model_name)
            return str(root) if root is not None else ""
        return str(variant.asset.get("path", ""))

    def _register_shared_source(self, variant: ModuleVariant) -> None:
        if not self._write_gate(self._shared_write_target(variant)):
            return
        target_model = self.current_selection.model_name
        if not target_model:
            self._log("请先选择目标型号")
            return
        module = module_label_from_asset(variant.asset)
        module_key = canonical_module_dir(module)
        candidates = self.borrow_candidates_for(module)
        self._show_shared_source_picker(target_model, module_key, module, candidates)

    def _show_shared_source_picker(
        self,
        target_model: str,
        module_key: str,
        module_label: str,
        candidates: list[FirmwareAsset],
    ) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(shared_register_dialog_title(module_label))
        dlg.setMinimumWidth(SHARED_SOURCE_PICKER_MIN_WIDTH)
        dlg.resize(*SHARED_SOURCE_PICKER_DEFAULT_SIZE)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        layout.setSpacing(SPACE_MD)

        layout.addWidget(SubtitleLabel("选择借用", dlg))

        caption = CaptionLabel(shared_source_picker_caption(module_label), dlg)
        caption.setWordWrap(True)
        layout.addWidget(caption)

        list_widget = QListWidget(dlg)
        list_widget.setWordWrap(True)
        layout.addWidget(list_widget, stretch=1)

        def _fill(items: list[FirmwareAsset]) -> None:
            list_widget.clear()
            if not items:
                empty_item = QListWidgetItem("其他型号还没有这个程序类型的程序")
                empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                list_widget.addItem(empty_item)
                return
            for a in items:
                item = QListWidgetItem(_format_source_item_qt(a))
                item.setToolTip(str(a.get("path", "")))
                item.setSizeHint(QSize(0, SHARED_SOURCE_PICKER_ITEM_HEIGHT))
                list_widget.addItem(item)

        _fill(candidates)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        confirm_btn = PrimaryPushButton("确认登记", dlg)
        confirm_btn.setEnabled(False)
        cancel_btn = PushButton("取消", dlg)
        btn_row.addWidget(confirm_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        list_widget.currentRowChanged.connect(
            lambda row: confirm_btn.setEnabled(row >= 0)
        )

        def _confirm() -> None:
            row = list_widget.currentRow()
            if row < 0:
                return
            source_asset = candidates[row]
            dlg.accept()
            self._do_register_shared(
                target_model,
                module_key,
                module_label,
                source_asset,
                "follow_asset",
            )

        confirm_btn.clicked.connect(_confirm)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _do_register_shared(
        self,
        target_model: str,
        module_key: str,
        module_label: str,
        source_asset: FirmwareAsset,
        mode: str = "follow_asset",
        overwrite_token: dict | None = None,
    ) -> None:
        del module_key
        model_root = str(self.model_root_for(target_model) or "")
        if not self._write_gate(model_root or target_model):
            return

        def run(log):
            return self.workbench_model.register_borrow(
                target_model,
                source_asset,
                mode=mode,
                overwrite_token=overwrite_token,
                log_fn=log,
            )

        def done(result: dict) -> None:
            if result.get("code") == "confirmation_required":
                answer = QMessageBox.question(
                    self,
                    "覆盖借用",
                    str(result.get("message") or shared_conflict_prompt_message(module_label)),
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                again = borrow_resubmit(
                    {"mode": mode, "overwrite_token": overwrite_token},
                    result.get("payload") or {},
                )
                self._do_register_shared(
                    target_model,
                    "",
                    module_label,
                    source_asset,
                    str(again["mode"]),
                    again["overwrite_token"],
                )
                return
            present_result(self, result, "register_borrow")
            if result.get("ok"):
                self._refresh_main_grid(reload_data=True)

        self.run_write("登记借用", run, done)

    def _unregister_shared(self, variant: ModuleVariant) -> None:
        if not self._write_gate(self._shared_write_target(variant)):
            return
        target_model = self.current_selection.model_name
        module = module_label_from_asset(variant.asset)
        module_key = canonical_module_dir(module)
        answer = QMessageBox.question(
            self,
            "解除借用",
            shared_unregister_confirm_message(module),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def run(log):
            return self.workbench_model.clear_borrow(target_model, module_key, log)

        def done(result: dict) -> None:
            present_result(self, result, "clear_borrow")
            token = str((result.get("payload") or {}).get("undo_token") or "")
            if result.get("code") == "ok" and token:
                self.show_undo_bar(
                    "已解除借用",
                    token,
                    lambda undo_token, log: self.workbench_model.undo_clear_borrow(
                        undo_token, log
                    ),
                )
            if result.get("ok"):
                self._refresh_main_grid(reload_data=True)

        self.run_write("解除借用", run, done)

    # ------------------------------------------------------------------ 工具
    def _open_asset_dir(self, variant: ModuleVariant) -> None:
        open_path_in_explorer(str(variant.asset.get("path", "")), self._log)

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._log(f"已复制: {text}")

    def run_write(self, name: str, fn, on_done) -> None:
        """后台执行一次写入。晚到的结果若对话框代次已变，则丢弃。"""
        self._dialog_epoch += 1
        epoch = self._dialog_epoch

        def wrapped(log):
            result = fn(log)
            status = None
            if self.root_dir:
                try:
                    status = load_workspace_status(self.root_dir)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"读取工作区状态失败: {exc}")
            return {"epoch": epoch, "result": result, "status": status}

        def done(payload: dict) -> None:
            if payload.get("epoch") != self._dialog_epoch:
                return
            status = payload.get("status")
            if status is not None and getattr(status, "state", "") == "recovery_required":
                self.apply_recovery_hold(
                    recovery_banner_text(getattr(status, "operation", None)),
                    resume_function_name(getattr(status, "operation", None)),
                )
            on_done(payload.get("result") or {})

        self._run_task(name, wrapped, done, silent=True)

    def model_root_for(self, model_name: str) -> Path | None:
        found = self.workbench_model._model_root_path_for_name(model_name)
        if found is not None:
            return found
        if self.root_dir and model_name:
            candidate = Path(self.root_dir) / model_name
            if candidate.is_dir():
                return candidate
        return None

    def refresh_model_chips(self, select: str | None = None) -> None:
        names = self._directory_chip_names()
        if select and select not in names:
            self.workbench_model.ensure_model_directory(select)
            names = [*names, select]
        self._refresh_model_selector(names)
        if select:
            self._on_model_changed(select)

    def _directory_chip_names(self) -> list[str]:
        root = Path(self.root_dir) if self.root_dir else None
        if root is None or not root.is_dir():
            return self.workbench_model.load_all_models()
        roots = enumerate_model_roots(root)
        if len(roots) == 1 and roots[0].resolve() == root.resolve():
            return self.workbench_model.load_all_models()
        for path in roots:
            self.workbench_model.ensure_model_directory(path.name)
        return [path.name for path in roots]

    def show_undo_bar(self, text: str, token: str, caller) -> None:
        self._undo_token = token
        self._undo_caller = caller
        self._undo_started = time.monotonic()
        self.undo_label.setText(text)
        self.undo_frame.show()
        self._undo_timer.start(5000)

    def _expire_undo(self) -> None:
        self._undo_token = ""
        self._undo_caller = None
        self.undo_frame.hide()

    def _click_undo(self) -> None:
        token = self._undo_token
        caller = self._undo_caller
        started = self._undo_started
        if not token or caller is None:
            return
        if not undo_bar_callable(started_at=started, now=time.monotonic()):
            self._expire_undo()
            return
        self._expire_undo()

        def run(log):
            return caller(token, log)

        def done(result: dict) -> None:
            present_result(self, result, "undo")
            if result.get("ok"):
                self._refresh_main_grid(reload_data=True)

        self.run_write("撤销", run, done)

    def apply_recovery_hold(self, message: str, resume_name: str | None) -> None:
        self._writes_held = True
        self._recovery_message = message
        self._resume_name = resume_name
        self.recovery_banner.setText(message)
        self.recovery_banner.show()
        self.resume_button.setVisible(bool(resume_name))

    def _hide_recovery_banner(self) -> None:
        self.recovery_banner.hide()
        self.resume_button.hide()
        self._recovery_message = ""

    def _hold_writes_until_recovery(self) -> None:
        """根目录已经可用时，三步恢复完成前禁止普通写入。"""
        root = (self.root_dir or "").strip()
        if not root or not Path(root).is_dir():
            return
        key = os.path.normcase(root)
        if self._recovery_running or key == self._recovered_for:
            return
        self.apply_recovery_hold("正在检查未完成的写入…", None)

    def _maybe_start_recovery(self, root: str) -> None:
        key = os.path.normcase((root or "").strip())
        if not key or not Path(root).is_dir():
            return
        if self._recovery_running or key == self._recovered_for:
            return
        self._recovery_running = True
        self._writes_held = True
        self.apply_recovery_hold("正在检查未完成的写入…", None)

        def sweep(workspace_root: Path) -> list[QuarantineRecord]:
            # sweep_expired 要求调用方已持锁；与前三步一样自取自放。
            with WorkspaceLock(workspace_root):
                return sweep_expired(workspace_root)

        def worker() -> None:
            try:
                outcome = run_startup_recovery(
                    root,
                    recover_interrupted=recover_interrupted_workspace,
                    recover_on_startup=recover_on_startup,
                    cleanup=cleanup_staging_area,
                    sweep=sweep,
                )
                self._recovery_ready.emit(key, outcome)
            except Exception as exc:  # noqa: BLE001
                self._recovery_ready.emit(key, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _on_recovery_ready(self, key: str, outcome: object) -> None:
        self._recovery_running = False
        current = os.path.normcase((self.root_dir or "").strip())
        if key != current:
            if current:
                self._maybe_start_recovery(self.root_dir)
            else:
                self._writes_held = False
                self._hide_recovery_banner()
            return
        if isinstance(outcome, Exception):
            self.apply_recovery_hold(f"启动恢复失败: {outcome}", None)
            self._log(f"启动恢复失败: {outcome}")
            return
        self._recovered_for = key
        self._writes_held = not bool(getattr(outcome, "writes_enabled", False))
        banner = getattr(outcome, "banner", None)
        if banner:
            self.apply_recovery_hold(str(banner), getattr(outcome, "resume_name", None))
        else:
            self._resume_name = None
            self._writes_held = False
            self._hide_recovery_banner()
        for path, text in getattr(outcome, "cleanup_errors", []):
            self._log(f"清理暂存失败 {path}: {text}")

    def _continue_recovery(self) -> None:
        name = self._resume_name
        if not name:
            return
        answer = QMessageBox.question(self, "继续恢复", "按上次中断的操作继续恢复？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        calls = {
            "resume_normalize_module_leaf": resume_normalize_module_leaf,
            "resume_update_asset": resume_update_asset,
            "resume_change_asset_semantics": resume_change_asset_semantics,
            "resume_restore_retired_version": resume_restore_retired_version,
        }
        fn = calls.get(name)
        if fn is None:
            return

        def run(log):
            return fn(self.root_dir, self.root_dir, log_fn=log)

        def done(result: dict) -> None:
            present_result(self, result, "resume")
            self._recovered_for = ""
            self._maybe_start_recovery(self.root_dir)

        self.run_write("继续恢复", run, done)


class QtWorkbenchWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "按摩椅程序资产管理系统 | Massage Chair Firmware Asset Manager"
        )
        self.resize(1400, 850)

        self.workbench = WorkbenchInterface(self)
        self.settings_interface = SettingsInterface(self)
        self.repair_interface = RepairInterface(self)
        self.recycle_interface = RecycleInterface(self)
        self.workbench.repair_interface = self.repair_interface
        self.workbench.recycle_interface = self.recycle_interface
        self.workbench.settings_requested.connect(self._open_settings)
        self.settings_interface.configure_requested.connect(self._open_configuration)

        self.addSubInterface(self.workbench, FluentIcon.HOME, "程序资产工作台")
        self.addSubInterface(self.recycle_interface, FluentIcon.DELETE, "回收站")
        self.addSubInterface(self.repair_interface, FluentIcon.INFO, "软件修复")
        self.addSubInterface(self.settings_interface, FluentIcon.SETTING, "设置")
        self._refresh_settings_interface()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.workbench._hold_writes_until_recovery()
        self.workbench._maybe_start_recovery(self.workbench.root_dir)

    def _refresh_settings_interface(self) -> None:
        import fwasset.core.settings as settings

        self.settings_interface.set_paths(settings.DEFAULT_ROOT, settings.TOOL_ROOT)
        self.repair_interface.set_workspace(settings.DEFAULT_ROOT)
        self.recycle_interface.bind(self.workbench, settings.DEFAULT_ROOT)
        self.workbench.set_configuration_required(
            not bool(settings.DEFAULT_ROOT.strip())
        )

    def _open_settings(self) -> None:
        self._refresh_settings_interface()
        self.switchTo(self.settings_interface)

    def _open_configuration(self) -> None:
        import fwasset.core.settings as settings
        from fwasset.ui_qt.setup_wizard import SetupWizard

        previous_root = settings.DEFAULT_ROOT.strip()
        wizard = SetupWizard(
            self,
            root_dir=settings.DEFAULT_ROOT,
            tool_root=settings.TOOL_ROOT,
            allow_skip=False,
        )

        def confirm_switch() -> bool:
            next_root = wizard.resolved_root_dir()
            if not previous_root or os.path.normcase(previous_root) == os.path.normcase(
                next_root
            ):
                return True
            answer = QMessageBox.question(
                wizard,
                "切换程序文件夹",
                "将切换到新的程序文件夹并重新读取程序列表。"
                "当前列表会更新为新位置的内容，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes

        wizard.set_before_write(confirm_switch)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return

        written = getattr(wizard, "_config_written", None)
        if not isinstance(written, dict):
            written = wizard.write_config()
        if isinstance(written, dict) and not written.get("ok"):
            QMessageBox.warning(self, "无法保存配置", str(written.get("message") or ""))
            return
        root_dir, _tool_root = _reload_runtime_settings()
        self._refresh_settings_interface()
        self.workbench.set_configured_root(root_dir)
        self.switchTo(self.workbench)
        if root_dir and os.path.normcase(previous_root) != os.path.normcase(root_dir):
            QTimer.singleShot(0, lambda: self.workbench._auto_scan(root_dir))


def main() -> int:
    app = QApplication(sys.argv)
    setTheme(Theme.AUTO)

    # ── 首次配置向导 ────────────────────────────────────────────────────
    # 仅在冻结 exe 且 root_dir 未配置时弹出；开发模式跳过。
    import fwasset.core.settings as settings

    wizard_completed = False
    if getattr(sys, "frozen", False) and not settings.DEFAULT_ROOT:
        from fwasset.ui_qt.setup_wizard import SetupWizard

        wizard = SetupWizard()
        if wizard.exec() == QDialog.DialogCode.Accepted:
            written = getattr(wizard, "_config_written", None)
            if not isinstance(written, dict):
                written = wizard.write_config()
            if isinstance(written, dict) and written.get("ok"):
                _reload_runtime_settings()
                wizard_completed = True
            else:
                detail = (
                    str(written.get("message") or "")
                    if isinstance(written, dict)
                    else "配置保存失败"
                )
                QMessageBox.warning(None, "无法保存配置", detail)

    window = QtWorkbenchWindow()
    window.show()

    # 向导完成后自动扫描配置的根目录
    if wizard_completed and DEFAULT_ROOT:
        QTimer.singleShot(300, lambda: window.workbench._auto_scan(DEFAULT_ROOT))

    # 自动化验证钩子（截图 / 自动选行 / 定时退出），供开发与 Phase 4 回归用
    if os.environ.get("FWASSET_QT_AUTOSELECT", ""):
        QTimer.singleShot(
            1000, lambda: window.workbench.grid_panel.select_first_variant()
        )
    shot = os.environ.get("FWASSET_QT_SCREENSHOT", "")
    if shot:
        QTimer.singleShot(
            1500,
            lambda: (window.grab().save(shot), print(f"SCREENSHOT={shot}", flush=True)),
        )
    quit_ms = int(os.environ.get("FWASSET_QT_QUIT_MS", "0") or "0")
    if quit_ms > 0:
        QTimer.singleShot(quit_ms, app.quit)

    return app.exec()


if __name__ == "__main__":
    # 开发直跑入口；正式入口在 app.main。
    raise SystemExit(main())
