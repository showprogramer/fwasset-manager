"""D1.6–D1.8 的受管写入口。

这里的三个入口刻意不复用旧 UI 服务的「直接写 TOML」行为：写入一律经过
configured-root gate、工作区事务锁与 generation。UI 编排在子任务 8 接入这些入口。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from fwasset.core.asset_index import AssetIndexError, replace_asset
from fwasset.core.asset_info import _merge_key_text, load_asset_info_with_status
from fwasset.core.config_io import atomic_write_text
from fwasset.core.file_scan import scan_firmware_subtree
from fwasset.core.managed_paths import managed_path_reason
from fwasset.core.model_config import (
    SharedModuleRef,
    _parse_shared_entry,
    load_model_config,
    load_shared_modules,
    serialize_model_config,
)
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    same_path_identity,
)
from fwasset.core.platform_config import (
    PlatformDefaults,
    canonical_module_dir,
    load_platform_config_with_status,
    serialize_platform_config,
)
from fwasset.core.reference_lookup import (
    ReferenceHit,
    _ModelEntry,
    check_reference_gate,
    enumerate_model_roots,
    find_references_to,
    is_blocking_issue,
)
from fwasset.core.services.reference_service import _check_canonical_conflicts
from fwasset.core.types import ChassisType, FirmwareAsset, ServiceResult
from fwasset.core.workspace_transaction import (
    WorkspaceBusyError,
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
    load_workspace_status,
)

__all__ = [
    "clear_shared_module",
    "register_shared_module",
    "set_asset_default",
    "undo_clear_shared_module",
    "update_asset_vendor",
]

_CHASSIS_TYPES: set[str] = set(get_args(ChassisType))


@dataclass(frozen=True)
class _ClearUndo:
    target_root: Path
    config_path: Path
    module_key: str
    preimage: bytes
    postimage: bytes
    expires_at: float


_CLEAR_UNDOS: dict[str, _ClearUndo] = {}


def _error(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def _ok(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": True, "code": code, "message": message, "payload": payload or {}}


def _transaction_error(exc: Exception) -> ServiceResult:
    if isinstance(exc, WorkspaceBusyError):
        return _error("workspace_busy", "工作区正在执行另一项写操作，请稍后重试")
    if isinstance(exc, WorkspaceRecoveryRequiredError):
        return _error("recovery_required", "工作区存在待恢复的中断操作，暂不能写入")
    return _error("write_failed", f"写入失败：{exc}")


def _asset_from_disk(
    workspace_root: Path, supplied: FirmwareAsset
) -> tuple[FirmwareAsset | None, ServiceResult | None]:
    """按目标路径冷扫，拒绝调用方伪造或过期的资产字段。"""
    raw_path = str(supplied.get("path", "") or "").strip()
    if not raw_path:
        return None, _error("invalid_args", "程序路径不能为空")
    try:
        asset_path = assert_within_workspace(raw_path, workspace_root)
    except PathGuardError as exc:
        return None, _error("out_of_workspace", f"程序不在当前工作区内：{exc}")
    if not asset_path.is_dir():
        return None, _error("invalid_asset", "程序目录不存在或不是目录")
    try:
        assets, issues = scan_firmware_subtree(str(workspace_root), str(asset_path))
    except Exception as exc:  # noqa: BLE001
        return None, _error("scan_failed", f"无法重新读取程序：{exc}")
    if any(str(issue.get("severity")) == "error" for issue in issues):
        return None, _error("scan_failed", "程序目录扫描失败，已停止写入", {"issues": issues})
    matches = [item for item in assets if same_path_identity(str(item["path"]), str(asset_path))]
    if len(matches) != 1:
        return None, _error("invalid_asset", "目标不是完整合法的程序")
    return matches[0], None


def _model_root_for_asset(asset: FirmwareAsset, workspace_root: Path) -> Path | None:
    candidate = str(asset.get("model_directory_path", "") or "").strip()
    if not candidate:
        return None
    try:
        root = assert_within_workspace(candidate, workspace_root)
    except PathGuardError:
        return None
    return root if root.is_dir() else None


def _bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _parse_model_toml(preimage: bytes) -> tuple[dict[str, Any], str, str]:
    """从已读取的型号配置字节解析，保证与 CAS preimage 同源。"""
    if not preimage:
        return {}, "missing", ""
    try:
        import tomllib

        data = tomllib.loads(preimage.decode("utf-8"))
    except UnicodeDecodeError:
        return {}, "parse_error", "型号配置不是有效 UTF-8"
    except Exception as exc:  # noqa: BLE001
        return {}, "parse_error", f"型号配置 TOML 解析失败: {exc}"
    if not isinstance(data, dict):
        return {}, "parse_error", "型号配置顶层必须是 table"
    return dict(data), "ok", ""


def _shared_pairs_from_data(
    data: dict[str, Any],
) -> tuple[list[tuple[str, SharedModuleRef]], list[str], str]:
    shared = data.get("shared_modules")
    if shared is None:
        return [], [], "ok"
    if not isinstance(shared, dict):
        return [], [], "parse_error"
    pairs: list[tuple[str, SharedModuleRef]] = []
    invalid: list[str] = []
    for raw_key, entry in shared.items():
        ref = _parse_shared_entry(raw_key, entry, strict=True)
        if ref is None:
            invalid.append(str(raw_key))
        else:
            pairs.append((str(raw_key), ref))
    return pairs, invalid, "ok"


def _cas_write(path: Path, preimage: bytes, content: str) -> bool:
    """写前再次比较原字节；外部修改不允许被本次写覆盖。"""
    if _bytes(path) != preimage:
        return False
    atomic_write_text(path, content)
    return True


def update_asset_vendor(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    asset: FirmwareAsset,
    vendor: str,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """D1.6：只更新 ``程序信息.toml`` 的厂商字段，不改变资产身份。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws = Path(workspace_root).resolve()
    scanned, err = _asset_from_disk(ws, asset)
    if err is not None:
        return err
    assert scanned is not None
    asset_path = Path(str(scanned["path"]))
    data, status, detail = load_asset_info_with_status(asset_path)
    if status in ("parse_error", "parser_missing"):
        return _error("metadata_corrupt", "程序信息读取失败，已保留原文件", {"detail": detail})
    old_vendor = data.get("vendor") if isinstance(data.get("vendor"), str) else ""
    if old_vendor == str(vendor):
        return _ok("unchanged", "厂商未变化，无需写入")

    try:
        with WorkspaceTransaction(ws, operation="update_asset_vendor") as transaction:
            fresh, fresh_err = _asset_from_disk(ws, scanned)
            if fresh_err is not None:
                transaction.commit()
                return fresh_err
            assert fresh is not None
            fresh_path = Path(str(fresh["path"]))
            data, status, detail = load_asset_info_with_status(fresh_path)
            if status in ("parse_error", "parser_missing"):
                transaction.commit()
                return _error("metadata_corrupt", "程序信息读取失败，已保留原文件", {"detail": detail})
            current_vendor = data.get("vendor") if isinstance(data.get("vendor"), str) else ""
            if current_vendor == str(vendor):
                transaction.commit()
                return _ok("unchanged", "厂商未变化，无需写入")
            preimage = _bytes(fresh_path / "程序信息.toml")
            try:
                text = preimage.decode("utf-8")
            except UnicodeDecodeError:
                transaction.commit()
                return _error("metadata_corrupt", "程序信息不是有效 UTF-8，已保留原文件")
            transaction.begin_product_write()
            if not _cas_write(fresh_path / "程序信息.toml", preimage, _merge_key_text(text, "vendor", str(vendor))):
                transaction.commit()
                return _error("stale_plan", "程序信息已被其他操作修改，请重新读取后再试")
            try:
                refreshed, refresh_err = _asset_from_disk(ws, fresh)
                if refresh_err is not None or refreshed is None:
                    transaction.commit()
                    return _error(
                        "rescan_failed",
                        "厂商已写入磁盘，但重新读取程序失败，请稍后重试",
                        {
                            "detail": str(refresh_err.get("message", ""))
                            if refresh_err is not None
                            else "",
                            "issues": refresh_err.get("payload", {}).get("issues", [])
                            if refresh_err is not None
                            else [],
                        },
                    )
                replace_asset(str(scanned["path"]), refreshed)
            except AssetIndexError as exc:
                transaction.commit()
                return _error("index_update_failed", "厂商已写入磁盘，请重新读取程序列表", {"detail": str(exc)})
            transaction.commit()
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)
    message = "已更新程序厂商"
    log_fn(message)
    return _ok("ok", message, {"asset_path": str(asset_path), "vendor": str(vendor)})


def _normalized_platform_for_default(
    model_root: Path, module_key: str
) -> tuple[PlatformDefaults | None, ServiceResult | None]:
    platforms, status, detail = load_platform_config_with_status(model_root)
    if status != "ok":
        return None, _error("platform_not_normalized", "平台配置尚未归一，不能设为默认", {"detail": detail})
    if len(platforms) != 1 or platforms[0].platform_name not in _CHASSIS_TYPES:
        return None, _error("platform_not_normalized", "平台配置尚未归一，不能设为默认")
    block = platforms[0]
    aliases = sorted(key for key in block.defaults if canonical_module_dir(key) == module_key)
    values = sorted({str(block.defaults[key]) for key in aliases})
    conflict = _check_canonical_conflicts(
        [_ModelEntry(model_root, "", [], [block])],
        [
            ReferenceHit(
                kind="platform_default",
                owner_root=str(model_root),
                owner_model_id="",
                config_path=str(model_root / "平台配置.toml"),
                canonical_key=module_key,
                block_index=0,
                raw_value=str(block.defaults[aliases[0]]) if aliases else "",
            )
        ],
    )
    payload = {"module_key": module_key, "keys": aliases, "values": values}
    if conflict is not None:
        return None, _error("canonical_conflict", "平台配置存在异值 canonical 别名", payload)
    if len(aliases) > 1:
        return None, _error("canonical_duplicate", "平台配置存在同值 canonical 别名", payload)
    return block, None


def set_asset_default(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    asset: FirmwareAsset,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """D1.7：把一个本型号通用区完整资产设为单一机芯类型的默认版本。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws = Path(workspace_root).resolve()
    scanned, err = _asset_from_disk(ws, asset)
    if err is not None:
        return err
    assert scanned is not None
    if scanned.get("category") != "common":
        return _error("invalid_asset", "只能将本型号通用区的完整程序设为默认")
    model_root = _model_root_for_asset(scanned, ws)
    module_key = canonical_module_dir(str(scanned.get("firmware_label", "") or ""))
    if model_root is None or not module_key:
        return _error("invalid_asset", "无法确定程序所属型号或模块")
    _block, normalized_error = _normalized_platform_for_default(model_root, module_key)
    if normalized_error is not None:
        return normalized_error
    module_dir = Path(str(scanned["path"])).parent
    lookup = find_references_to(configured_root, ws, module_dir, "module")
    if not lookup["ok"]:
        return lookup
    lookup_result = lookup["payload"].get("result")
    if lookup_result is not None:
        issues = lookup_result.issues
        if any(is_blocking_issue(issue) for issue in issues):
            return _error("reference_incomplete", "借用引用无法完整读取，已停止设默认", {"issues": issues})
        residual = [hit for hit in lookup_result.hits if hit.kind == "shared_follow_default"]
        if residual:
            return _error("follow_default_migration_required", "仍有旧版跟随默认借用，请先完成迁移", {"hits": residual})

    try:
        with WorkspaceTransaction(ws, operation="set_asset_default") as transaction:
            fresh, fresh_err = _asset_from_disk(ws, scanned)
            if fresh_err is not None:
                transaction.commit()
                return fresh_err
            assert fresh is not None
            fresh_model = _model_root_for_asset(fresh, ws)
            if fresh_model is None or fresh.get("category") != "common":
                transaction.commit()
                return _error("invalid_asset", "目标已不再是本型号通用区完整程序")
            block, block_error = _normalized_platform_for_default(fresh_model, module_key)
            if block_error is not None:
                transaction.commit()
                return block_error
            assert block is not None
            module_dir = Path(str(fresh["path"])).parent
            locked_lookup = find_references_to(configured_root, ws, module_dir, "module")
            if not locked_lookup["ok"]:
                transaction.commit()
                return locked_lookup
            locked_result = locked_lookup["payload"].get("result")
            if locked_result is not None:
                if any(is_blocking_issue(issue) for issue in locked_result.issues):
                    transaction.commit()
                    return _error(
                        "reference_incomplete",
                        "借用引用无法完整读取，已停止设默认",
                        {"issues": locked_result.issues},
                    )
                if any(hit.kind == "shared_follow_default" for hit in locked_result.hits):
                    transaction.commit()
                    return _error(
                        "follow_default_migration_required",
                        "仍有旧版跟随默认借用，请先完成迁移",
                    )
            config_path = fresh_model / "平台配置.toml"
            preimage = _bytes(config_path)
            platforms, status, _detail = load_platform_config_with_status(fresh_model)
            if status != "ok" or len(platforms) != 1:
                transaction.commit()
                return _error("platform_not_normalized", "平台配置尚未归一，不能设为默认")
            variant = Path(str(fresh["path"])).name
            stale_keys = [
                key
                for key in platforms[0].defaults
                if canonical_module_dir(key) == module_key
            ]
            for raw_key in stale_keys:
                del platforms[0].defaults[raw_key]
            platforms[0].defaults[module_key] = variant
            transaction.begin_product_write()
            if not _cas_write(config_path, preimage, serialize_platform_config(platforms)):
                transaction.commit()
                return _error("stale_plan", "平台配置已被其他操作修改，请重新读取后再试")
            transaction.commit()
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)
    message = f"已将「{Path(str(scanned['path'])).name}」设为{module_key}默认版本"
    log_fn(message)
    return _ok("ok", message, {"asset_path": str(scanned["path"]), "module_key": module_key})


def _shared_ref_fingerprint(ref: SharedModuleRef) -> str:
    payload = "\x1f".join(
        [
            ref.module_key,
            ref.source_model_id,
            ref.source_group,
            ref.source_module,
            ref.source_relative_path,
            ref.mode,
            ref.source_platform,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _overwrite_token(generation: int, ref: SharedModuleRef) -> dict[str, Any]:
    return {"generation": generation, "fingerprint": _shared_ref_fingerprint(ref)}


def _confirmation_required(
    module_key: str, existing: SharedModuleRef, generation: int
) -> ServiceResult:
    return _error(
        "confirmation_required",
        f"模块「{module_key}」已有借用登记，请确认覆盖",
        {"existing": existing, "overwrite_token": _overwrite_token(generation, existing)},
    )


def _stale_overwrite_plan(
    module_key: str, existing: SharedModuleRef | None, generation: int
) -> ServiceResult:
    payload: dict[str, Any] = {}
    if existing is not None:
        payload = {"existing": existing, "overwrite_token": _overwrite_token(generation, existing)}
    return _error("stale_plan", f"模块「{module_key}」的覆盖对象已改变，请重新确认", payload)


def register_shared_module(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    target_model_root: str | Path,
    source_asset: FirmwareAsset,
    *,
    mode: str = "static",
    overwrite_token: dict[str, Any] | None = None,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """D1.8：登记或以确认 token 覆盖一条 static/follow_asset 借用。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    if mode not in ("static", "follow_asset"):
        return _error("invalid_args", "新借用只支持固定或跟随该程序")
    ws = Path(workspace_root).resolve()
    try:
        target = assert_within_workspace(target_model_root, ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"目标型号不在当前工作区内：{exc}")
    if not target.exists():
        return _error("target_model_missing", "目标型号目录不存在")
    if not target.is_dir():
        return _error("invalid_target_model", "目标型号不是目录")
    source, source_error = _asset_from_disk(ws, source_asset)
    if source_error is not None:
        return source_error
    assert source is not None
    source_path = Path(str(source["path"]))
    if managed_path_reason(source_path, is_dir=True, workspace_root=ws) is not None:
        return _error("retired_anchor", "不能借用受管排除目录中的程序")
    source_root = _model_root_for_asset(source, ws)
    source_id, source_status, _detail = load_model_config(source_root) if source_root else ("", "missing", "")
    target_id, target_status, _detail = load_model_config(target)
    if source_root is None or source_status != "ok":
        return _error("invalid_args", "来源型号必须已配置 id")
    if target_status != "ok":
        return _error("invalid_target_model", "目标型号必须已配置 id")
    if same_path_identity(source_root, target) or source_id == target_id:
        return _error("self_reference", "不能借用本型号自己的程序")
    other_roots = [root for root in enumerate_model_roots(ws) if not same_path_identity(root, target)]
    if not other_roots:
        return _error("no_other_model", "当前没有其他可借用的型号")
    module_key = canonical_module_dir(str(source.get("firmware_label", "") or ""))
    if not module_key:
        return _error("invalid_asset", "来源程序缺少可识别模块")

    preview_generation = load_workspace_status(ws).generation
    preview_existing = next(
        (ref for ref in load_shared_modules(target) if ref.module_key == module_key), None
    )
    if preview_existing is not None and overwrite_token is None:
        return _confirmation_required(module_key, preview_existing, preview_generation)

    try:
        with WorkspaceTransaction(ws, operation="register_shared_module") as transaction:
            source, source_error = _asset_from_disk(ws, source)
            if source_error is not None:
                transaction.commit()
                return source_error
            assert source is not None
            source_path = Path(str(source["path"]))
            if managed_path_reason(source_path, is_dir=True, workspace_root=ws) is not None:
                transaction.commit()
                return _error("retired_anchor", "不能借用受管排除目录中的程序")
            source_root = _model_root_for_asset(source, ws)
            source_id, source_status, _detail = (
                load_model_config(source_root) if source_root else ("", "missing", "")
            )
            target_id, target_status, _detail = load_model_config(target)
            if source_root is None or source_status != "ok":
                transaction.commit()
                return _error("invalid_args", "来源型号必须已配置 id")
            if target_status != "ok":
                transaction.commit()
                return _error("invalid_target_model", "目标型号必须已配置 id")
            if same_path_identity(source_root, target) or source_id == target_id:
                transaction.commit()
                return _error("self_reference", "不能借用本型号自己的程序")
            other_roots = [
                root for root in enumerate_model_roots(ws) if not same_path_identity(root, target)
            ]
            if not other_roots:
                transaction.commit()
                return _error("no_other_model", "当前没有其他可借用的型号")
            module_key = canonical_module_dir(str(source.get("firmware_label", "") or ""))
            if not module_key:
                transaction.commit()
                return _error("invalid_asset", "来源程序缺少可识别模块")

            config_path = target / "型号配置.toml"
            preimage = _bytes(config_path)
            data, status, detail = _parse_model_toml(preimage)
            if status in ("parse_error", "parser_missing"):
                transaction.commit()
                return _error("config_parse_error", "型号配置读取失败，已停止写入", {"detail": detail})
            pairs, invalid_keys, pair_status = _shared_pairs_from_data(data)
            if pair_status != "ok":
                transaction.commit()
                return _error("canonical_conflict", "型号配置中的借用条目结构无效")
            matches = [ref for _raw_key, ref in pairs if ref.module_key == module_key]
            if invalid_keys or len(matches) > 1:
                transaction.commit()
                return _error("canonical_conflict", "型号配置中的借用条目存在 canonical 冲突")
            existing = matches[0] if matches else None
            if preview_existing is not None:
                if (
                    existing is None
                    or transaction.status.generation != preview_generation
                    or overwrite_token != _overwrite_token(preview_generation, preview_existing)
                    or _shared_ref_fingerprint(existing) != _shared_ref_fingerprint(preview_existing)
                ):
                    transaction.commit()
                    return _stale_overwrite_plan(module_key, existing, transaction.status.generation)
            elif existing is not None:
                transaction.commit()
                return _confirmation_required(module_key, existing, transaction.status.generation)
            shared = data.setdefault("shared_modules", {})
            if not isinstance(shared, dict):
                transaction.commit()
                return _error("canonical_conflict", "型号配置中的借用条目结构无效")
            rel = "/".join(source_path.relative_to(ws).parts)
            entry: dict[str, str] = {
                "source_model_id": source_id,
                "source_group": source_id,
                "source_module": module_key,
                "source_relative_path": rel,
            }
            if mode != "static":
                entry["mode"] = mode
            shared[module_key] = entry
            transaction.begin_product_write()
            if not _cas_write(config_path, preimage, serialize_model_config(data)):
                transaction.commit()
                return _error("stale_plan", "型号配置已被其他操作修改，请重新确认后再试")
            transaction.commit()
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)
    message = f"已登记「{module_key}」借用来源"
    log_fn(message)
    return _ok("ok", message, {"module_key": module_key, "source_model_id": source_id})


def clear_shared_module(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    target_model_root: str | Path,
    module_key: str,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """D10.1a：删除一条借用，并签发短时 CAS 撤销令牌。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws = Path(workspace_root).resolve()
    try:
        target = assert_within_workspace(target_model_root, ws)
    except PathGuardError as exc:
        return _error("out_of_workspace", f"目标型号不在当前工作区内：{exc}")
    if not target.is_dir():
        return _error("target_model_missing", "目标型号目录不存在")
    canonical_key = canonical_module_dir(module_key)
    if not canonical_key:
        return _error("invalid_args", "模块不能为空")
    config_path = target / "型号配置.toml"
    try:
        with WorkspaceTransaction(ws, operation="clear_shared_module") as transaction:
            preimage = _bytes(config_path)
            data, status, detail = _parse_model_toml(preimage)
            if status != "ok":
                transaction.commit()
                return _error("config_parse_error", "型号配置读取失败，已停止解除借用", {"detail": detail})
            shared = data.get("shared_modules")
            if not isinstance(shared, dict):
                transaction.commit()
                return _ok("unchanged", "未登记该模块借用，无需解除")
            raw_keys = [key for key in shared if canonical_module_dir(str(key)) == canonical_key]
            if not raw_keys:
                transaction.commit()
                return _ok("unchanged", "未登记该模块借用，无需解除")
            if len(raw_keys) != 1:
                transaction.commit()
                return _error("canonical_conflict", "型号配置中的借用条目存在 canonical 冲突")
            shared.pop(raw_keys[0])
            if not shared:
                data.pop("shared_modules", None)
            post_content = serialize_model_config(data)
            transaction.begin_product_write()
            if not _cas_write(config_path, preimage, post_content):
                transaction.commit()
                return _error("stale_plan", "型号配置已被其他操作修改，请重新读取后再试")
            postimage = _bytes(config_path)
            token = secrets.token_urlsafe(24)
            _CLEAR_UNDOS[token] = _ClearUndo(
                target_root=target,
                config_path=config_path,
                module_key=canonical_key,
                preimage=preimage,
                postimage=postimage,
                expires_at=time.monotonic() + 5.0,
            )
            transaction.commit()
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)
    message = f"已解除「{canonical_key}」借用"
    log_fn(message)
    return _ok("ok", message, {"module_key": canonical_key, "undo_token": token})


def undo_clear_shared_module(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    undo_token: str,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """D10.1a：仅在 postimage 未被改变时恢复已解除的借用。"""
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    undo = _CLEAR_UNDOS.pop(str(undo_token or ""), None)
    if undo is None or undo.expires_at < time.monotonic():
        return _error("undo_conflict", "解除借用后的内容已失效或已被修改，不能撤销")
    ws = Path(workspace_root).resolve()
    try:
        assert_within_workspace(undo.target_root, ws)
        with WorkspaceTransaction(ws, operation="undo_clear_shared_module") as transaction:
            if _bytes(undo.config_path) != undo.postimage:
                transaction.commit()
                return _error("undo_conflict", "型号配置已被修改，不能覆盖恢复")
            try:
                restored = undo.preimage.decode("utf-8")
            except UnicodeDecodeError:
                transaction.commit()
                return _error("undo_conflict", "撤销前配置不是有效 UTF-8，不能安全恢复")
            transaction.begin_product_write()
            if not _cas_write(undo.config_path, undo.postimage, restored):
                transaction.commit()
                return _error("undo_conflict", "型号配置已被修改，不能覆盖恢复")
            transaction.commit()
    except PathGuardError as exc:
        return _error("out_of_workspace", f"撤销目标不在当前工作区内：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)
    message = f"已恢复「{undo.module_key}」借用"
    log_fn(message)
    return _ok("ok", message, {"module_key": undo.module_key})
