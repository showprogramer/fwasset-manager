"""工作台纯函数助手（不依赖具体 UI 框架）。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    normalize_workspace_path,
)
from fwasset.core.types import FirmwareAsset, ServiceResult


def source_tag(
    source_label: str,
    source_kind: str,
    shared_state: str = "local",
    shared_source_label: str = "",
) -> tuple[str, str]:
    """表格「归属」列标签：(文案, 语义)。语义为 accent / warning / error。

    关联命中只显示来源型号（「自动更新·L50S」→「来自 L50S」），完整文案作提示。
    """
    if shared_state == "shared_hit":
        source = shared_source_label.rsplit("·", 1)[-1].removeprefix("来自").strip()
        return (f"来自 {source}" if source else "来自其他型号", "warning")
    if shared_state == "shared_missing":
        return ("关联缺失", "error")
    label = source_label.strip()
    if label == "通用默认" or (not label and source_kind == "common"):
        label = "通用"
    return (label or ("通用" if source_kind == "common" else "定制专属"), "accent")


def relative_time_text(seconds_ago: float) -> str:
    """状态栏索引时间：刚刚 / N 分钟前 / N 小时前 / N 天前。"""
    seconds = max(0, int(seconds_ago))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def usb_copy_notice(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """U 盘复制成功后的提示：(标题, 内容, 级别 success / warning / info)。

    payload 不含 ``ejected`` 时不是 U 盘任务，返回 ``None``。
    """
    if "ejected" not in payload:
        return None
    if payload.get("ejected"):
        return ("已复制到 U 盘", "U 盘已安全弹出，可以拔出", "success")
    if payload.get("eject_requested", True):
        return ("文件已复制到 U 盘", "自动弹出失败，请先在任务栏安全弹出再拔出", "warning")
    return ("已复制到 U 盘", "未自动弹出，拔出前请先安全弹出", "info")


def breadcrumb_text(node_type: str, common_type: str = "", scheme_name: str = "") -> str:
    """主区标题的分类部分（型号名另行显示）。"""
    if node_type == "common_type":
        return f"通用程序 › {common_type}"
    if node_type == "custom_scheme":
        return f"定制程序 › {scheme_name}"
    if node_type == "all":
        return "全部程序"
    return ""


def flash_mode_label(mode: str) -> str:
    labels = {
        "auto_usb": "USB刷机",
        "tool_launch": "工具烧录",
        "manual_doc": "说明操作",
        "disabled": "不可烧录",
    }
    return labels.get(mode, mode or "-")


def module_label_from_asset(asset: FirmwareAsset) -> str:
    """资产的模块显示名（如 蓝牙程序、主板程序）。"""
    return (
        str(asset.get("firmware_label", "")).strip()
        or str(asset.get("firmware_type", "")).strip()
        or "程序"
    )


def set_default_action_label(
    model_name: str,
    module: str,
    *,
    is_current: bool = False,
) -> str:
    """右键「设默认」菜单文案：型号 + 模块，不出现配置块名（如标准单机芯3D）。

    例：``设为「L36」蓝牙程序默认版本`` / ``✓ 已是「L36」蓝牙程序默认版本``
    """
    model = (model_name or "").strip() or "当前型号"
    mod = (module or "").strip() or "程序"
    core = f"「{model}」{mod}默认版本"
    if is_current:
        return f"✓ 已是{core}"
    return f"设为{core}"


def set_default_confirm_message(
    model_name: str,
    module: str,
    variant_name: str,
) -> str:
    """设默认确认框正文（型号 + 模块）。"""
    model = (model_name or "").strip() or "当前型号"
    mod = (module or "").strip() or "程序"
    shown = (variant_name or "").strip() or "-"
    target = f"「{model}」{mod}默认版本"
    return f"将「{shown}」设为{target}？\n\n定制方案缺少该模块时，将使用此程序补齐。"


# --- 共享登记入口文案（Phase B2） ---
# 禁词语：UI 文本不得出现「回源」（仅内部术语）。


def shared_register_action_label(module: str) -> str:
    """右键项：为目标模块登记借用。"""
    mod = (module or "").strip() or "该模块"
    return f"为「{mod}」关联其他型号的程序…"


def shared_replace_action_label(module: str) -> str:
    """右键项：替换目标模块已经登记的借用。"""
    mod = (module or "").strip() or "该模块"
    return f"更换「{mod}」的关联…"


def shared_unregister_action_label(module: str) -> str:
    """右键项：解除目标模块的借用。"""
    mod = (module or "").strip() or "该模块"
    return f"解除「{mod}」的关联"


def shared_register_dialog_title(module: str) -> str:
    mod = (module or "").strip() or "该模块"
    return f"为「{mod}」关联其他型号的程序"


def shared_unregister_confirm_message(module: str) -> str:
    mod = (module or "").strip() or "该模块"
    return (
        f"确认解除「{mod}」的关联？\n\n"
        "只删除关联记录，不会删除固件文件。"
    )


def shared_conflict_prompt_message(module: str) -> str:
    mod = (module or "").strip() or "该模块"
    return f"「{mod}」已有关联，是否换成新的来源程序？"


def shared_source_picker_caption(module: str) -> str:
    """来源选择对话框顶部说明。"""
    mod = (module or "").strip() or "该模块"
    return f"从其他型号选择一个「{mod}」程序进行关联"


def write_gate_check(
    configured_root: str | Path | None,
    scanned_root: str | Path | None,
    target_path: str | Path,
) -> ServiceResult:
    configured = normalize_workspace_path(configured_root)
    scanned = normalize_workspace_path(scanned_root)
    if not configured:
        return {
            "ok": False,
            "code": "not_configured",
            "message": "请先在设置中配置程序文件夹",
            "payload": {},
        }
    if scanned != configured:
        return {
            "ok": False,
            "code": "root_changed",
            "message": "配置已变更，请先重新读取程序文件夹",
            "payload": {},
        }
    try:
        assert_within_workspace(target_path, configured_root or "")
    except PathGuardError:
        return {
            "ok": False,
            "code": "out_of_workspace",
            "message": "目标路径不在当前工作区内",
            "payload": {},
        }
    return {"ok": True, "code": "ok", "message": "", "payload": {}}
