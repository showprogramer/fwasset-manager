from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from fwasset.core.types import FirmwareAsset


@runtime_checkable
class PanelHost(Protocol):
    """Qt 操作面板对宿主的依赖面。

    WorkbenchInterface 实现此协议。U 盘选择器由详情面板常驻持有（auto_usb 时
    显示在操作面板上方），面板经 get_global_usb_drive 取盘符；目录类交接操作
    在详情底部，不再由面板各自构建。
    """

    def _run_task(self, name: str, fn: Any, on_done: Any = None) -> None: ...
    def _selected_asset(self) -> FirmwareAsset | None: ...
    def get_global_usb_drive(self) -> str: ...
