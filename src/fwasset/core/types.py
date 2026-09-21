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

# 型号机芯类型（TASK-20260916，父规格 D0.1）：`平台配置.toml` 的单个
# ``[[platform]]`` 块 name 属于此枚举时写入 ``FirmwareAsset.chassis_type``。
ChassisType = Literal["单3D", "单2D", "双2D", "上3D下2D"]

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

# ---------------------------------------------------------------------------
# 新路径准入（TASK-20260917-admission-import-primitives，父规格 D3 / D0.2）
# ---------------------------------------------------------------------------

AdmissionKind = Literal[
    "model",  # 新建型号：workspace_root/<型号名>
    "scheme",  # 新建方案：<型号根>/定制/<方案名>
    "asset",  # 新增程序：通用/<模块>/<程序名> 或 定制/<方案>/<模块>/<程序名>
]


class StagedImport(TypedDict):
    """``stage_import_*`` 的返回：staging 会话目录与被忽略的来源元数据文件。

    ``session`` 为绝对路径字符串（promote_import 的输入）；只过滤会落到
    资产目录根层的 ``程序信息.toml``（D4.3：导入不信任来源元数据）。
    """

    session: str
    skipped_metadata: list[str]

# ---------------------------------------------------------------------------
# 工作区事务操作日志（TASK-20260915-atomic-dir-primitives，父规格 D8.3）
# ---------------------------------------------------------------------------


class OperationProduct(TypedDict):
    """本操作创建或提升的产物（D1.4c 删除产物原语的身份来源）。

    ``path`` 为绝对路径字符串；``manifest`` 为提升时记录的目录 manifest 哈希。
    """

    path: str
    manifest: str


class OperationLog(TypedDict):
    """持久化操作日志（``operation.json``）的形状。

    旧格式缺少 ``products`` / ``details`` 时按空列表 / 空字典读取；
    ``details`` 承载各操作自定义负载（plan 摘要、旧/新路径等）。
    """

    operation: str
    phase: str
    started_at: float
    products: list[OperationProduct]
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# 隔离区记录（TASK-20260916，父规格 D2.5）
# ---------------------------------------------------------------------------

QuarantineKind = Literal[
    "undoable_delete",  # D10.1 各删除操作，保留撤销窗口
    "transactional_retire",  # update / 改类型的旧程序退位，仅供失败补偿
]

QuarantineStatus = Literal[
    "moving",  # 已落盘登记、内容尚未移入隔离区（崩溃恢复的中间态）
    "pending",  # 撤销窗口内（仅 undoable_delete）或等待送出（transactional_retire）
    "committed",  # 撤销窗口已过 / retire 已确认，待异步送系统回收站
    "sent",  # 已送达系统回收站，记录可清理
    "send_failed",  # 送出失败，保留清单供启动恢复重试
]


class QuarantineRecord(TypedDict):
    """一条隔离操作清单条目（持久化于 ``workspace_state`` 受管根）。

    ``manifest`` 复用 :func:`fwasset.core.manifest.directory_manifest_hash`
    （不含 mtime）；``expires_at`` 仅 ``undoable_delete`` 有意义，
    ``transactional_retire`` 恒为 0（不展示、不承诺撤销）。

    ``removed_containers``（父规格 D10.1b）记录本次删除时**由应用自动
    ``rmdir`` 掉的空父容器**，自外向内排列（先祖在前），撤销时按逆序重建
    后才能把内容移回。只登记应用自己删掉的目录——不能在撤销时按
    ``original_path`` 逆推并无条件 ``mkdir``，那样分不清「本次删掉的空容器」
    与「用户此前就手工删掉的目录」，会凭空造出用户没要的容器。旧记录缺该
    键读为 ``[]``，撤销时不重建任何容器（与扩字段前行为一致）。
    """

    id: str
    kind: QuarantineKind
    workspace_root: str
    original_path: str
    quarantine_path: str
    manifest: str
    status: QuarantineStatus
    created_at: float
    expires_at: float
    removed_containers: list[str]


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


class ScanIssue(TypedDict):
    """扫描诊断条目（TASK-20260916，父规格 D0.1a 第 5 条）。

    ``severity`` 分级：目录读取失败 / 用户取消 → ``error``；chassis 配置
    ``parse_error`` / ``parser_missing`` → ``warning``（不阻断索引对账）。
    ``path`` 为关联路径，无关联路径留空串；chassis 警告记 ``平台配置.toml``
    完整路径。
    """

    severity: Literal["warning", "error"]
    message: str
    path: str


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
    platform: str  # legacy：整机平台（如 "双机芯-上3D-下2D"），仅保留旧数据读取；默认平台留 ""
    scheme_name: str  # 定制方案名，如 "以色列-Royal-Z9"；通用区为 ""
    scheme_path: str  # 定制方案根目录绝对路径；通用区为 ""
    # --- 型号机芯类型（D0.1）：必填，legacy / 未识别留 "" ---
    chassis_type: ChassisType | Literal[""]
    # --- 归属厂商（D6.2/D6.3）：必填，legacy / 元数据缺失留 "" ---
    vendor: str


# ---------------------------------------------------------------------------
# R8 引用反查与级联改写（TASK-20260901-r8-reference-integrity）
# ---------------------------------------------------------------------------

ReferenceTargetKind = Literal["model", "scheme", "module", "asset"]
#: ``clear_defaults``（D1.3）只由 ``build_clear_defaults_plan`` 签发：语义变化
#: 时清除失效的 platform defaults 条目，``build_rewrite_plan`` 不接受该取值。
ReferenceOperation = Literal["rename", "update", "clear_defaults"]

#: D1.3 语义变化种类。``change_type`` 改程序类型；``general_to_custom`` /
#: ``custom_to_general`` 通用区与定制区互转；``custom_scheme_move`` 定制区内
#: 换方案。各自的 defaults 处理见 ``build_clear_defaults_plan``。
ClearDefaultsKind = Literal[
    "change_type",
    "general_to_custom",
    "custom_to_general",
    "custom_scheme_move",
]

#: D1.4b 退位方式。两种记录不得合并。
RetireMode = Literal["retire_to_trash", "retire_to_backup"]


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
