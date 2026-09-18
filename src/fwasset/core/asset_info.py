"""资产目录内 ``程序信息.toml`` 读写原语（TASK-20260917，父规格 D6.2）。

模式参照 :mod:`fwasset.core.platform_config`：

- 状态四类 ``ok / missing / parse_error / parser_missing``；
- ``load_asset_info_with_status`` 严格读取（TOML 语法错误或顶层非 table →
  ``parse_error``），vendor 取值对调用方值级宽松（缺失 / 非字符串 / 空串 → ``""``）；
- ``save_vendor`` 严格读取现有文件后**文本级合并**写入：只替换/追加顶层
  ``vendor`` 赋值行，其余键、注释与格式原样保留（D6.2「解析保留未知键」）；
  ``parse_error / parser_missing`` 拒绝写入并保留原文件；IO 异常自然上抛，
  由服务层包装为 ServiceResult（子任务 5a）。

待补齐候选期（D7.5，子任务 5）另有 ``import_state`` / ``intended_firmware_type``
两个键，由 ``save_candidate_metadata`` 写入、``clear_candidate_metadata`` 在补齐
后删除，读取走 ``*_from_asset_info``。这份元数据是候选清单的**磁盘真源**——
不得只放内存或只放 SQLite。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from fwasset.core.config_io import atomic_write_text
from fwasset.core.managed_paths import ASSET_METADATA_FILENAME

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


AssetInfoStatus = Literal[
    "ok",
    "missing",
    "parse_error",
    "parser_missing",
]

# 顶层赋值行（table 头之前的范围由调用方划定）。
_VENDOR_LINE_RE = re.compile(r"^\s*vendor\s*=")
_TABLE_HEADER_RE = re.compile(r"^\s*\[")

#: 待补齐候选期的元数据键（D7.5）。二者共同构成「这是一份待补齐内容」的
#: 磁盘真源；补齐完成后**一并删除**（子任务 5 Q1 裁决）——有 ``import_state``
#: 就是待补齐，没有就是普通程序信息，候选区不保留 ``complete`` 中间态。
IMPORT_STATE_KEY = "import_state"
INTENDED_FIRMWARE_TYPE_KEY = "intended_firmware_type"

#: 候选期 ``import_state`` 的唯一合法值。
IMPORT_STATE_INCOMPLETE = "incomplete"


def load_asset_info_with_status(
    asset_dir: str | Path,
) -> tuple[dict[str, Any], AssetInfoStatus, str]:
    """严格读取资产目录下的 ``程序信息.toml``。

    返回 ``(data, status, error)``：

    - ``ok``：可解析且顶层为 table（允许空文档）；
    - ``missing``：文件不存在；
    - ``parse_error``：TOML 语法错误或顶层不是 table；
    - ``parser_missing``：tomllib/tomli 均不可用。
    """
    toml_path = Path(asset_dir) / ASSET_METADATA_FILENAME
    if not toml_path.exists():
        return {}, "missing", ""
    if tomllib is None:
        return {}, "parser_missing", "TOML 解析组件不可用（需要 tomllib 或 tomli）"

    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001
        return {}, "parse_error", f"程序信息 TOML 解析失败: {exc}"

    if not isinstance(data, dict):
        return {}, "parse_error", "程序信息顶层必须是 table"
    return data, "ok", ""


def vendor_from_asset_info(data: dict[str, Any]) -> str:
    """从已解析的程序信息表提取 vendor（值级宽松）。

    ``vendor`` 为非空字符串 → 原值；键缺失 / 非字符串 / 空串 → ``""``
    （与缺文件同语义，D6.3 存量语义，不产生 issue）。
    """
    value = data.get("vendor")
    if isinstance(value, str) and value:
        return value
    return ""


_TOML_SHORT_ESCAPES: dict[int, str] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _toml_str(value: str) -> str:
    """Serialize a string as a TOML basic string.

    Escapes quotes, backslashes and control characters (TOML v1.0:
    ``\\b \\t \\n \\f \\r``; remaining < 0x20 and 0x7F as ``\\uXXXX``).
    """
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if code in _TOML_SHORT_ESCAPES:
            out.append(_TOML_SHORT_ESCAPES[code])
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _top_level_end(lines: list[str]) -> int:
    """返回顶层范围的结束下标（首个 table 头所在行，没有则是行数）。"""
    for index, line in enumerate(lines):
        if _TABLE_HEADER_RE.match(line):
            return index
    return len(lines)


def _key_line_re(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _merge_key_text(text: str, key: str, value: str | None) -> str:
    """文本级合并：替换/插入/删除顶层 ``<key> =`` 赋值行。

    只动顶层（首个 table 头之前）：嵌套在 table 里的同名键属未知键，原样
    保留。``value`` 为 ``None`` 表示**删除该键**（顶层无此键时是空操作）。
    行尾统一为 ``\\n``；结果为空时返回空串，不写出孤零零的换行。
    """
    lines = text.splitlines()
    pattern = _key_line_re(key)
    top_end = _top_level_end(lines)

    if value is None:
        kept = [
            line
            for index, line in enumerate(lines)
            if not (index < top_end and pattern.match(line))
        ]
        return "\n".join(kept) + "\n" if kept else ""

    new_line = f"{key} = {_toml_str(value)}"
    for index in range(top_end):
        if pattern.match(lines[index]):
            lines[index] = new_line
            return "\n".join(lines) + "\n"
    lines.insert(top_end, new_line)
    return "\n".join(lines) + "\n"


def _merge_vendor_text(text: str, vendor: str) -> str:
    """文本级合并：替换顶层既有 ``vendor =`` 赋值行，否则插入顶层范围末尾。"""
    return _merge_key_text(text, "vendor", vendor)


def save_vendor(asset_dir: str | Path, vendor: str) -> tuple[AssetInfoStatus, str]:
    """把 ``vendor`` 合并写入资产目录下的 ``程序信息.toml``。

    - 先严格读取现有文件：``parse_error / parser_missing`` → 拒绝写入并
      保留原文件（D6.2「解析损坏不静默覆盖」）；``missing`` 按空文档起写；
    - 合并保留未知键与注释，经 :func:`~fwasset.core.config_io.atomic_write_text`
      原子落盘；IO 异常自然上抛。
    """
    return _save_keys(asset_dir, {"vendor": str(vendor)})


def _save_keys(
    asset_dir: str | Path, updates: dict[str, str | None]
) -> tuple[AssetInfoStatus, str]:
    """把多个顶层键一次性合并写入（``None`` 表示删除该键）。

    与 :func:`save_vendor` 同样先严格读取：``parse_error / parser_missing``
    拒绝写入并保留原文件。多键在同一次读-改-写内完成，避免逐键写盘时崩在
    中间留下「只改了一半」的元数据。
    """
    toml_path = Path(asset_dir) / ASSET_METADATA_FILENAME
    _data, status, error = load_asset_info_with_status(asset_dir)
    if status in ("parse_error", "parser_missing"):
        return status, error
    try:
        text = toml_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for key, value in updates.items():
        text = _merge_key_text(text, key, value)
    atomic_write_text(toml_path, text)
    return "ok", ""


def import_state_from_asset_info(data: dict[str, Any]) -> str:
    """从已解析的程序信息表提取 ``import_state``（值级宽松，同 vendor 口径）。

    非字符串 / 缺失 / 空串 → ``""``（即「不是待补齐候选」）。**值是否合法由
    调用方判断**：``scan_incomplete_imports`` 对非 ``incomplete`` 的非空值要
    产出诊断，不能在这里静默归一掉（D7.5「元数据悬空时给出诊断」）。
    """
    value = data.get(IMPORT_STATE_KEY)
    if isinstance(value, str) and value:
        return value
    return ""


def intended_firmware_type_from_asset_info(data: dict[str, Any]) -> str:
    """从已解析的程序信息表提取 ``intended_firmware_type``（值级宽松）。"""
    value = data.get(INTENDED_FIRMWARE_TYPE_KEY)
    if isinstance(value, str) and value:
        return value
    return ""


def save_candidate_metadata(
    asset_dir: str | Path,
    *,
    vendor: str,
    intended_firmware_type: str,
) -> tuple[AssetInfoStatus, str]:
    """写入候选期三件套：``vendor`` + ``intended_firmware_type`` + ``import_state``。

    一次读-改-写落盘（D7.5「清单真源在磁盘」）。``intended_firmware_type``
    为空串时不写该键——来源无法判断意图类型是允许的，候选仍靠
    ``import_state`` 被识别。
    """
    updates: dict[str, str | None] = {
        "vendor": str(vendor),
        IMPORT_STATE_KEY: IMPORT_STATE_INCOMPLETE,
    }
    if intended_firmware_type:
        updates[INTENDED_FIRMWARE_TYPE_KEY] = str(intended_firmware_type)
    return _save_keys(asset_dir, updates)


def clear_candidate_metadata(asset_dir: str | Path) -> tuple[AssetInfoStatus, str]:
    """补齐完成后删除候选期键，``vendor`` 保留（子任务 5 Q1 裁决）。

    删除而非置 ``complete``：候选区不保留「既非待补齐、又没离开」的中间态。
    删完后文件里通常只剩 ``vendor``，是一份普通的程序信息。
    """
    return _save_keys(
        asset_dir, {IMPORT_STATE_KEY: None, INTENDED_FIRMWARE_TYPE_KEY: None}
    )
