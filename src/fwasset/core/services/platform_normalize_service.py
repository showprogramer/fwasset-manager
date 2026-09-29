"""D5.5 存量 platform 归一：把多个 legacy ``[[platform]]`` 块合并为单一机芯类型块。

流程按父规格 D5.5：展示现有块与冲突 → 用户选定机芯类型 → 合并为单块 → 逐模块
处理 ``defaults`` 冲突。用户选择发生在**锁外**（预览时），``normalize_*`` 持写锁
并在锁内重验全部条件（D8）。

归一会删除 legacy 块，指向本型号块的 ``follow_default`` 会因此断链，所以残留
``follow_default`` 一律阻止归一，由用户先显式执行迁移（本服务不自动串联，见
`migrate_follow_defaults_for_normalize`）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

from fwasset.core.config_io import atomic_write_text
from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    same_path_identity,
)
from fwasset.core.platform_config import (
    PLATFORM_CONFIG_FILENAME,
    PLATFORM_CONFIG_HEADER,
    PlatformConfigStatus,
    PlatformDefaults,
    canonical_module_dir,
    serialize_platform_config,
)
from fwasset.core.reference_lookup import (
    check_reference_gate,
    enumerate_model_roots,
    find_references_to,
    is_blocking_issue,
)
from fwasset.core.services.reference_service import migrate_follow_default_refs
from fwasset.core.types import (
    ChassisType,
    ModuleConflictView,
    NormalizeExpectation,
    PlatformBlockView,
    PlatformNormalizePreview,
    ServiceResult,
)
from fwasset.core.workspace_transaction import (
    WorkspaceBusyError,
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
)

try:
    import tomllib
except ImportError:  # pragma: no cover - 老环境回退
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

__all__ = [
    "migrate_follow_defaults_for_normalize",
    "normalize_platform_config",
    "preview_platform_normalize",
]

_CHASSIS_TYPES: set[str] = set(get_args(ChassisType))

#: 当前与历史应用文件头；仅出现在文件起始、按顺序独占整行时不计入丢弃。
_APP_HEADER_COMMENTS = (PLATFORM_CONFIG_HEADER,)
_LEGACY_APP_HEADER_COMMENTS = (
    "# 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。",
    "# defaults 键 = 通用区模块目录名，值 = 默认变体子目录名（空串表示该模块唯一）。",
)


def _error(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": False, "code": code, "message": message, "payload": payload or {}}


def _ok(code: str, message: str, payload: dict[str, Any] | None = None) -> ServiceResult:
    return {"ok": True, "code": code, "message": message, "payload": payload or {}}


def _transaction_error(exc: Exception) -> ServiceResult:
    if isinstance(exc, WorkspaceBusyError):
        return _error("workspace_busy", "工作区正在执行另一项写操作，请稍后重试")
    if isinstance(exc, WorkspaceRecoveryRequiredError):
        return _error("recovery_required", "工作区存在待恢复的中断操作，暂不能写入")
    return _error("write_failed", f"归一写入失败：{exc}")


def _cas_write(path: Path, preimage: bytes, content: str) -> bool:
    """写前再次比较原字节；外部修改不允许被本次写覆盖（与 5a 同款）。"""
    current = path.read_bytes() if path.exists() else b""
    if current != preimage:
        return False
    atomic_write_text(path, content)
    return True


# --------------------------------------------------------------------------- #
# 4.2a 配置的单次读取
# --------------------------------------------------------------------------- #


def _read_platform_source(
    model_root: Path,
) -> tuple[bytes, dict[str, Any], list[PlatformDefaults], PlatformConfigStatus, str]:
    """对 ``平台配置.toml`` 只做**一次**读取，同时供给四处使用。

    返回 ``(preimage, data, platforms, status, detail)``：``preimage`` 供 CAS 与
    注释识别，``data`` 供未知字段检出，``platforms`` 供块与冲突分类。

    与 :func:`load_platform_config_with_status` 的口径差异（有意为之）：
    - 缺 ``name`` 的块按 ``load_platform_config_strict`` 判为 ``parse_error``，
      **不**静默跳过——归一是整体重写，跳过等于无声删除它的 ``defaults``；
    - ``name`` 与 ``defaults`` 值必须本来就是 TOML 字符串，**不**做 ``str()``
      强转——归一会把强转结果展示给用户再写回磁盘，``3`` 变 ``"3"`` 属无声
      改变 TOML 语义。只读路径的容错不适用于会写盘的这里。
    """
    toml_path = Path(model_root) / PLATFORM_CONFIG_FILENAME
    if not toml_path.exists():
        return b"", {}, [], "missing", ""
    if tomllib is None:
        return b"", {}, [], "parser_missing", "TOML 解析组件不可用（需要 tomllib 或 tomli）"

    try:
        preimage = toml_path.read_bytes()
        data = tomllib.loads(preimage.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return b"", {}, [], "parse_error", f"平台配置 TOML 解析失败: {exc}"

    if not isinstance(data, dict):
        return preimage, {}, [], "parse_error", "平台配置顶层必须是 table"

    raw = data.get("platform")
    if raw is None:
        return preimage, data, [], "ok", ""
    if not isinstance(raw, list):
        return preimage, data, [], "parse_error", "platform 必须是 array of tables"

    platforms: list[PlatformDefaults] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return preimage, data, [], "parse_error", "每个 [[platform]] 条目必须是 table"
        name_raw = entry.get("name", "")
        if name_raw is not None and not isinstance(name_raw, str):
            return (
                preimage,
                data,
                [],
                "parse_error",
                f"第 {index + 1} 个 [[platform]] 块的 name 必须是字符串",
            )
        name = str(name_raw or "").strip()
        if not name:
            return (
                preimage,
                data,
                [],
                "parse_error",
                f"第 {index + 1} 个 [[platform]] 块缺少 name",
            )
        defaults_raw = entry.get("defaults", {})
        if defaults_raw is None:
            defaults_raw = {}
        if not isinstance(defaults_raw, dict):
            return preimage, data, [], "parse_error", "platform.defaults 必须是 table"
        defaults: dict[str, str] = {}
        for key, value in defaults_raw.items():
            if not isinstance(value, str):
                return (
                    preimage,
                    data,
                    [],
                    "parse_error",
                    f"块「{name}」的 defaults 键「{key}」的值必须是字符串",
                )
            defaults[str(key)] = value
        platforms.append(PlatformDefaults(platform_name=name, defaults=defaults))
    return preimage, data, platforms, "ok", ""


def _config_read_error(status: PlatformConfigStatus, detail: str) -> ServiceResult | None:
    """把读取状态映射为拒绝结果；``ok`` 返回 ``None``。"""
    if status == "ok":
        return None
    if status == "missing":
        return _error(
            "platform_config_missing",
            "该型号没有平台配置，无可合并的配置块",
            {"status": status},
        )
    if status == "parser_missing":
        return _error("parser_missing", "平台配置读取组件不可用，已停止归一", {"detail": detail})
    return _error(
        "config_parse_error",
        "平台配置读取失败，已保留原文件",
        {"detail": detail, "status": status},
    )


# --------------------------------------------------------------------------- #
# 丢弃内容检出（第 7 节）
# --------------------------------------------------------------------------- #


def _comment_lines(text: str) -> list[tuple[int, str, bool]]:
    """按 TOML 词法扫描注释：返回 ``(行号, 注释原文, 是否独占整行)``。

    维护「是否在字符串内」状态，跳过 ``"..."`` / ``'...'`` / ``\"\"\"...\"\"\"`` /
    ``'''...'''`` 中的 ``#``；字符串外遇到的第一个 ``#`` 起至行尾即一处注释。
    一行里最多算一处。
    """
    found: list[tuple[int, str, bool]] = []
    delimiter = ""  # 多行字符串的结束符，空串表示不在多行字符串内
    for lineno, line in enumerate(text.splitlines()):
        index = 0
        length = len(line)
        in_single = False
        in_basic = False
        comment_at = -1
        while index < length:
            if delimiter:
                if line.startswith(delimiter, index):
                    index += len(delimiter)
                    delimiter = ""
                else:
                    index += 1
                continue
            char = line[index]
            if in_basic:
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    in_basic = False
                index += 1
                continue
            if in_single:
                if char == "'":
                    in_single = False
                index += 1
                continue
            if line.startswith('"""', index) or line.startswith("'''", index):
                delimiter = line[index : index + 3]
                index += 3
                continue
            if char == '"':
                in_basic = True
                index += 1
                continue
            if char == "'":
                in_single = True
                index += 1
                continue
            if char == "#":
                comment_at = index
                break
            index += 1
        if comment_at >= 0:
            whole_line = line[:comment_at].strip() == ""
            found.append((lineno, line[comment_at:].rstrip(), whole_line))
    return found


def _discarded_content(preimage: bytes, data: dict[str, Any]) -> list[str]:
    """归一整体重写会丢弃的内容摘要（注释、未知字段）。

    检出**不影响**归一能否执行，只影响告知——``平台配置.toml`` 是应用托管
    文件，存在未知字段是历史遗留，拒绝会让这些型号永远无法归一。
    """
    summary: list[str] = []

    try:
        text = preimage.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover - 解析阶段已拦住
        text = ""
    comments = _comment_lines(text)
    # 仅排除文件起始的完整应用文件头；历史两行头必须同时匹配。
    header_len = 0
    for expected_lines in (_LEGACY_APP_HEADER_COMMENTS, _APP_HEADER_COMMENTS):
        header = comments[: len(expected_lines)]
        if len(header) == len(expected_lines) and all(
            whole_line and raw.rstrip() == expected
            for (_lineno, raw, whole_line), expected in zip(header, expected_lines)
        ):
            header_len = len(expected_lines)
            break
    count = len(comments) - header_len
    if count > 0:
        summary.append(f"注释 {count} 处")

    for key in data:
        if key != "platform":
            summary.append(f"顶层字段「{key}」")
    platform_raw = data.get("platform")
    if isinstance(platform_raw, list):
        for entry in platform_raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "") or "")
            for key in entry:
                if key not in ("name", "defaults"):
                    summary.append(f"块「{name}」的字段「{key}」")
    return summary


# --------------------------------------------------------------------------- #
# 4.3 冲突分类
# --------------------------------------------------------------------------- #


def _classify(platforms: list[PlatformDefaults]) -> list[ModuleConflictView]:
    """按 canonical 模块键聚合全部块的 ``defaults`` 并分类。

    判定顺序是规范的一部分（4.3）：异值 → 同值多别名 → 唯一。把「去重后只剩
    一个值」放在前面会让别名键永远不被归并，型号还会被误判为已归一。
    """
    raw_keys: dict[str, set[str]] = {}
    values: dict[str, set[str]] = {}
    for block in platforms:
        for key, value in block.defaults.items():
            canon = canonical_module_dir(key)
            if not canon:
                continue
            raw_keys.setdefault(canon, set()).add(str(key))
            values.setdefault(canon, set()).add(str(value))

    conflicts: list[ModuleConflictView] = []
    for canon in sorted(raw_keys):
        sorted_values = sorted(values[canon])
        sorted_keys = sorted(raw_keys[canon])
        kind: Literal["unique", "alias_duplicate", "value_conflict"]
        if len(sorted_values) >= 2:
            kind = "value_conflict"
            resolved = ""
        elif len(sorted_keys) >= 2:
            kind = "alias_duplicate"
            resolved = sorted_values[0]
        else:
            kind = "unique"
            resolved = sorted_values[0]
        conflicts.append(
            {
                "module_key": canon,
                "raw_keys": sorted_keys,
                "values": sorted_values,
                "kind": kind,
                "resolved": resolved,
            }
        )
    return conflicts


def _expectation(
    platforms: list[PlatformDefaults], conflicts: list[ModuleConflictView]
) -> NormalizeExpectation:
    return {
        "block_names": [p.platform_name for p in platforms],
        "module_values": {c["module_key"]: list(c["values"]) for c in conflicts},
    }


def _is_normalized(
    platforms: list[PlatformDefaults], conflicts: list[ModuleConflictView]
) -> bool:
    return (
        len(platforms) == 1
        and platforms[0].platform_name in _CHASSIS_TYPES
        and all(c["kind"] == "unique" for c in conflicts)
    )


def _locked_chassis_type(platforms: list[PlatformDefaults]) -> str:
    """恰好一个枚举块时返回该块名（= 型号现行机芯类型），否则空串。

    D0.1a 已把「恰好一个枚举块」定为 ``chassis_type`` 的生产判据，这种型号
    已经有生效的机芯类型，D5.2 规定不可再改——与是否还有别名/冲突无关。
    """
    if len(platforms) == 1 and platforms[0].platform_name in _CHASSIS_TYPES:
        return platforms[0].platform_name
    return ""


# --------------------------------------------------------------------------- #
# 4.2 follow_default 前置
# --------------------------------------------------------------------------- #


def _follow_default_state(
    configured_root: str | Path | None, ws: Path, model_root: Path
) -> tuple[list[dict[str, Any]], ServiceResult | None]:
    """返回 ``(残留命中, 阻断结果)``。

    归属口径**只算入向**——「别的型号跟随**本型号**的默认」。这正是
    ``find_references_to(..., model_root, "model")`` 返回的集合。因果关系只有
    一条：归一删除本型号的 legacy 块 → 指向本型号块的 ``follow_default`` 断链。
    本型号作为借入方的条目指向别人的块，本次写入完全不碰。
    """
    lookup = find_references_to(configured_root, ws, model_root, "model")
    if not lookup["ok"]:
        return [], lookup
    result = lookup["payload"].get("result")
    if result is None:
        return [], None
    if any(is_blocking_issue(issue) for issue in result.issues):
        return [], _error(
            "reference_incomplete",
            "关联记录无法完整读取，已停止归一",
            {"issues": result.issues},
        )
    hits = [
        {
            "owner_root": hit.owner_root,
            "raw_key": hit.raw_key,
            "source_model_id": hit.source_model_id,
            "source_relative_path": hit.source_relative_path,
        }
        for hit in result.hits
        if hit.kind == "shared_follow_default"
    ]
    return hits, None


def _model_root_guard(
    workspace_root: str | Path, model_root: str | Path
) -> tuple[Path, Path, ServiceResult | None]:
    ws = Path(workspace_root).resolve()
    try:
        target = assert_within_workspace(model_root, ws)
    except PathGuardError as exc:
        return ws, Path(str(model_root)), _error(
            "out_of_workspace", f"型号目录不在当前工作区内：{exc}"
        )
    known = enumerate_model_roots(ws)
    if not any(same_path_identity(str(target), str(root)) for root in known):
        return ws, target, _error("invalid_model_root", "目标不是本工作区内的型号目录")
    return ws, target, None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def migrate_follow_defaults_for_normalize(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    *,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """``migrate_follow_default_refs`` 的直通转发（不改参数、不改 payload）。

    存在的意义是让 UI 从同一模块取到归一流程的两个步骤，而不是把 R8 服务散播
    到 UI。**不得**在其中加入额外写入。
    """
    return migrate_follow_default_refs(configured_root, workspace_root, log_fn=log_fn)


def preview_platform_normalize(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
) -> ServiceResult:
    """只读预览：现有块、逐模块冲突、待选机芯类型与将被丢弃的内容。

    **不持锁、不写盘**（D8：纯预览不持锁）。
    """
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws, target, guard_error = _model_root_guard(workspace_root, model_root)
    if guard_error is not None:
        return guard_error

    preimage, data, platforms, status, detail = _read_platform_source(target)
    read_error = _config_read_error(status, detail)
    if read_error is not None:
        return read_error
    if not platforms:
        return _error(
            "platform_config_missing",
            "该型号没有平台配置块，无可合并的内容",
            {"status": status},
        )

    # 4.2 步骤 2：阻断级 issue 直接返回，**不发布可执行内容**——命中清单可能
    # 不完整，据此展示的「将被合并的块」会漏掉真实引用。
    hits, blocked = _follow_default_state(configured_root, ws, target)
    if blocked is not None:
        return blocked

    conflicts = _classify(platforms)
    blocks: list[PlatformBlockView] = [
        {
            "block_index": index,
            "name": block.platform_name,
            "is_chassis_type": block.platform_name in _CHASSIS_TYPES,
            "defaults": dict(block.defaults),
        }
        for index, block in enumerate(platforms)
    ]
    preview: PlatformNormalizePreview = {
        "model_root": str(target),
        "blocks": blocks,
        "conflicts": conflicts,
        "chassis_candidates": sorted(
            {b["name"] for b in blocks if b["is_chassis_type"]}
        ),
        "needs_module_choice": sorted(
            c["module_key"] for c in conflicts if c["kind"] == "value_conflict"
        ),
        "follow_default_hits": hits,
        "follow_default_blocked": bool(hits),
        "already_normalized": _is_normalized(platforms, conflicts),
        "expectation": _expectation(platforms, conflicts),
        "discarded_content": _discarded_content(preimage, data),
    }
    return _ok("ok", f"平台配置预览完成：{len(blocks)} 个配置块", {"preview": preview})


def _resolve_merged_defaults(
    conflicts: list[ModuleConflictView], module_choices: dict[str, str] | None
) -> tuple[dict[str, str], ServiceResult | None]:
    """按分类与用户选择产出合并后的单块 ``defaults``（键一律 canonical）。"""
    choices = {str(k): str(v) for k, v in (module_choices or {}).items()}
    by_key = {c["module_key"]: c for c in conflicts}

    unexpected = sorted(
        key
        for key in choices
        if key not in by_key or by_key[key]["kind"] != "value_conflict"
    )
    if unexpected:
        return {}, _error(
            "invalid_args",
            f"模块「{unexpected[0]}」无需选择默认版本，请勿传入",
            {"keys": unexpected},
        )

    merged: dict[str, str] = {}
    for conflict in conflicts:
        key = conflict["module_key"]
        if conflict["kind"] != "value_conflict":
            merged[key] = conflict["resolved"]
            continue
        if key not in choices:
            return {}, _error(
                "module_choice_required",
                f"模块「{key}」存在多个默认版本，请先选定保留哪一个",
                {"module_key": key, "values": list(conflict["values"])},
            )
        chosen = choices[key]
        if chosen not in conflict["values"]:
            return {}, _error(
                "invalid_choice",
                f"模块「{key}」的选择不在候选版本中",
                {"module_key": key, "values": list(conflict["values"]), "chosen": chosen},
            )
        merged[key] = chosen
    return merged, None


def normalize_platform_config(
    configured_root: str | Path | None,
    workspace_root: str | Path,
    model_root: str | Path,
    chassis_type: str,
    module_choices: dict[str, str] | None = None,
    *,
    expected: NormalizeExpectation | None = None,
    log_fn: Callable[[str], None] = print,
) -> ServiceResult:
    """把该型号的多个 ``[[platform]]`` 块合并为单一机芯类型块（D5.5）。

    用户选择（``chassis_type`` / ``module_choices``）在锁外完成；锁内按当前磁盘
    重新独立判定全部拒绝条件，并用 ``expected`` 比对用户确认时所见的内容。
    **不可撤销**：被丢弃的 legacy 块名、落选取值、注释与未知字段不保留。
    """
    gate = check_reference_gate(configured_root, workspace_root)
    if gate is not None:
        return gate
    ws, target, guard_error = _model_root_guard(workspace_root, model_root)
    if guard_error is not None:
        return guard_error
    if chassis_type not in _CHASSIS_TYPES:
        return _error("invalid_chassis_type", f"非法的机芯类型：{chassis_type!r}")

    try:
        with WorkspaceTransaction(ws, operation="normalize_platform_config") as transaction:
            preimage, data, platforms, status, detail = _read_platform_source(target)
            read_error = _config_read_error(status, detail)
            if read_error is not None:
                transaction.commit()
                return read_error
            if not platforms:
                transaction.commit()
                return _error(
                    "platform_config_missing",
                    "该型号没有平台配置块，无可合并的内容",
                    {"status": status},
                )

            conflicts = _classify(platforms)

            # 4.7 步骤 4：follow_default 前置先于机芯类型锁与 unchanged 判定。
            # 顺序是规范的一部分——把 unchanged 放在前面会让「已归一但仍有入向
            # 残留」的型号拿到 ok=True，用户以为无事可做，实际迁移还没做。
            hits, blocked = _follow_default_state(configured_root, ws, target)
            if blocked is not None:
                transaction.commit()
                return blocked
            if hits:
                transaction.commit()
                return _error(
                    "follow_default_migration_required",
                    "仍有旧版跟随默认关联指向本型号，请先完成迁移再归一",
                    {"hits": hits},
                )

            locked = _locked_chassis_type(platforms)
            if locked and locked != chassis_type:
                transaction.commit()
                return _error(
                    "chassis_type_locked",
                    f"该型号的机芯类型已是「{locked}」，型号建成后不能更改",
                    {"current": locked, "requested": chassis_type},
                )

            if _is_normalized(platforms, conflicts) and locked == chassis_type:
                transaction.commit()
                return _ok("unchanged", "平台配置已归一，无需写入")

            if expected is not None:
                current = _expectation(platforms, conflicts)
                if current != expected:
                    transaction.commit()
                    return _error(
                        "stale_plan",
                        "平台配置已被修改，请重新确认归一内容",
                        {"expected": expected, "current": current},
                    )

            merged, choice_error = _resolve_merged_defaults(conflicts, module_choices)
            if choice_error is not None:
                transaction.commit()
                return choice_error

            content = serialize_platform_config(
                [PlatformDefaults(platform_name=chassis_type, defaults=merged)]
            )
            config_path = target / PLATFORM_CONFIG_FILENAME
            transaction.begin_product_write()
            if not _cas_write(config_path, preimage, content):
                transaction.commit()
                return _error("stale_plan", "平台配置已被其他操作修改，请重新读取后再试")
            transaction.commit()
    except Exception as exc:  # noqa: BLE001
        return _transaction_error(exc)

    discarded = _discarded_content(preimage, data)
    message = f"平台配置已归一为「{chassis_type}」，请重新读取程序列表以更新机芯类型"
    log_fn(message)
    return _ok(
        "ok",
        message,
        {
            "model_root": str(target),
            "chassis_type": chassis_type,
            "chassis_type_changed": locked != chassis_type,
            "defaults": merged,
            "discarded_content": discarded,
        },
    )
