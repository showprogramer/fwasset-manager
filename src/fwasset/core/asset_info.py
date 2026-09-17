"""资产目录内 ``程序信息.toml`` 读写原语（TASK-20260917，父规格 D6.2）。

模式参照 :mod:`fwasset.core.platform_config`：

- 状态四类 ``ok / missing / parse_error / parser_missing``；
- ``load_asset_info_with_status`` 严格读取（TOML 语法错误或顶层非 table →
  ``parse_error``），vendor 取值对调用方值级宽松（缺失 / 非字符串 / 空串 → ``""``）；
- ``save_vendor`` 严格读取现有文件后**文本级合并**写入：只替换/追加顶层
  ``vendor`` 赋值行，其余键、注释与格式原样保留（D6.2「解析保留未知键」）；
  ``parse_error / parser_missing`` 拒绝写入并保留原文件；IO 异常自然上抛，
  由服务层包装为 ServiceResult（子任务 5a）。
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

# 顶层 vendor 赋值行（table 头之前的范围由调用方划定）。
_VENDOR_LINE_RE = re.compile(r"^\s*vendor\s*=")
_TABLE_HEADER_RE = re.compile(r"^\s*\[")


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


def _merge_vendor_text(text: str, vendor: str) -> str:
    """文本级合并：替换顶层既有 ``vendor =`` 赋值行，否则插入顶层范围末尾。

    只动顶层（首个 table 头之前）：嵌套在 table 里的 ``vendor`` 属未知键，
    原样保留。行尾统一为 ``\\n``。
    """
    lines = text.splitlines()
    new_line = f"vendor = {_toml_str(vendor)}"
    top_end = len(lines)
    for index, line in enumerate(lines):
        if _TABLE_HEADER_RE.match(line):
            top_end = index
            break
    for index in range(top_end):
        if _VENDOR_LINE_RE.match(lines[index]):
            lines[index] = new_line
            return "\n".join(lines) + "\n"
    lines.insert(top_end, new_line)
    return "\n".join(lines) + "\n"


def save_vendor(asset_dir: str | Path, vendor: str) -> tuple[AssetInfoStatus, str]:
    """把 ``vendor`` 合并写入资产目录下的 ``程序信息.toml``。

    - 先严格读取现有文件：``parse_error / parser_missing`` → 拒绝写入并
      保留原文件（D6.2「解析损坏不静默覆盖」）；``missing`` 按空文档起写；
    - 合并保留未知键与注释，经 :func:`~fwasset.core.config_io.atomic_write_text`
      原子落盘；IO 异常自然上抛。
    """
    toml_path = Path(asset_dir) / ASSET_METADATA_FILENAME
    _data, status, error = load_asset_info_with_status(asset_dir)
    if status in ("parse_error", "parser_missing"):
        return status, error
    try:
        existing = toml_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    atomic_write_text(toml_path, _merge_vendor_text(existing, str(vendor)))
    return "ok", ""
