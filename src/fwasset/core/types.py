from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

FirmwareType = Literal[
    "mainboard",
    "handcontrol_ui",
    "music_bt",
    "music_files",
    "voice",
    "shortcut_key",
    "movement_3d",
    "movement_2d",
    "knob_switch",
    "leg",
    "knee",
    "sonic",
    "health_detection",
    "commercial_mainboard",
    "seat_occupancy",
    "card_reader",
    "leyao_yao",
    "triple_combo",
    "aging",
    "segmented_screen",
]
FlashMode = Literal["auto_usb", "tool_launch", "manual_doc", "disabled"]
UsbFlow = Literal["paired_files", "directory_copy", ""]

# ---------------------------------------------------------------------------
# 受管路径与工作区布局（TASK-20260905，父规格 D4.3 / D8.2 / D9.0）
# ---------------------------------------------------------------------------

ManagedPathReason = Literal[
    "retired_versions",  # 资产内部 旧版本/ 备用副本
    "asset_metadata",  # 程序信息.toml
    "staging",  # 导入与写操作暂存区
    "quarantine",  # 删除隔离区（撤销窗口）
    "incomplete_candidate",  # 待补齐候选区
    "workspace_state",  # generation / 操作日志 / 隔离清单
]

WorkspaceLayout = Literal[
    "single_model",  # 旧布局：工作区根本身就是型号
    "multi_model",  # 工作区/型号 结构，含一个或多个型号
    "empty",  # 合法工作区，暂无型号
    "invalid",  # 不可读，或混合/无法归类的内容
]

WorkspaceState = Literal[
    "clean",
    "operation_in_progress",
    "recovery_required",
]


class ServiceResult(TypedDict):
    ok: bool
    code: str
    message: str
    payload: dict[str, Any]


class SerialPortInfo(TypedDict):
    device: str
    description: str
    hwid: str


class SerialCommandPayload(TypedDict, total=False):
    device: str
    command: str
    response: str
    raw_response: bytes
    old_baud: int
    new_baud: int
    matched_baud: int
    expected_baud: int
    warning: str


class SerialCommandResult(TypedDict):
    ok: bool
    code: str
    message: str
    payload: SerialCommandPayload


class FlashJobPayload(TypedDict, total=False):
    firmware_type: FirmwareType
    drive: str
    removed_count: int
    copy_ok: bool
    ejected: bool


class FlashJobResult(TypedDict):
    ok: bool
    code: str
    message: str
    payload: FlashJobPayload


class HandcontrolFolder(TypedDict):
    path: str
    rom_file: str
    pkg_file: str
    model: str
    version: str
    label: str


class ToolRegistration(TypedDict):
    name: str
    path: str
    directory: str


class FirmwareAsset(TypedDict):
    series: str
    firmware_type: FirmwareType
    firmware_label: str
    flash_mode: FlashMode
    usb_flow: UsbFlow
    model: str
    version: str
    model_directory_name: str
    model_directory_path: str
    path: str
    directory_name: str
    files: list[str]
    modified_time: float
    tool_name: str
    tool_path: str
    tool_dir: str
    label: str
    # --- L36 新目录结构字段（向后兼容：旧目录留空字符串）---
    category: Literal["common", "custom", ""]  # 通用 / 定制
    platform: str  # 整机平台，如 "双机芯-上3D-下2D"；默认平台留 ""
    scheme_name: str  # 定制方案名，如 "以色列-Royal-Z9"；通用区为 ""
    scheme_path: str  # 定制方案根目录绝对路径；通用区为 ""


# ---------------------------------------------------------------------------
# R8 引用反查与级联改写（TASK-20260901-r8-reference-integrity）
# ---------------------------------------------------------------------------

ReferenceTargetKind = Literal["model", "scheme", "module", "asset"]
ReferenceOperation = Literal["rename", "update"]


@dataclass
class ReferenceSemantics:
    """程序的身份语义快照：update 级联用其判定「身份不变」。

    ``module_key`` 为 canonical 模块键（``canonical_module_dir`` 之后的值）；
    ``scheme_name`` 为定制方案名，通用区为空串。
    """

    model_id: str
    module_key: str
    source_group: str
    scheme_name: str = ""


@dataclass
class RewriteRequest:
    """级联改写请求（R8 规则 4，单一请求类型）。

    rename：``old_path`` → ``new_path``（target_kind 可为 model/scheme/module/asset）。
    update：``old_path`` 程序退位、``replacement_path`` 上岗（target_kind 固定 asset），
    携带新旧语义快照，由 build 派生权威语义交叉校验。
    """

    operation: ReferenceOperation
    target_kind: ReferenceTargetKind
    old_path: str
    new_path: str = ""  # rename 必填
    replacement_path: str = ""  # update 必填
    old_semantics: ReferenceSemantics | None = None  # update 必填
    new_semantics: ReferenceSemantics | None = None  # update 必填
