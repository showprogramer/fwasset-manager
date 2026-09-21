"""受管路径判定、内部写授权守卫与工作区布局识别（TASK-20260905，子任务 1a）。

三件事，都是无状态纯函数：

1. :func:`managed_path_reason` / :func:`should_exclude_managed_path`——回答
   「遍历时这个路径要不要跳过、按哪一类跳过」（父规格 D4.3）；
2. :func:`managed_root` / :func:`assert_managed_write`——应用内部目录的**唯一**
   生成入口与写授权守卫（父规格 D8.2）；
3. :func:`detect_workspace_layout`——单/多型号布局识别（父规格 D9.0）。

两条边界必须守住：

- **排除判定不是写授权。** ``should_exclude_managed_path`` 只说明「扫描/复制时
  跳过」；任何对受管目录的写入都要另外通过 :func:`assert_managed_write`。
- **受管身份靠工作区锚定，不靠名字。** 用户自建的 ``staging/``、工作区外的同名
  ``.fwasset/`` 都是普通目录；只有位于**本工作区**受管根之下的路径才归应用所有。

生命周期、TTL、清理与事务恢复不在本模块（子任务 1b）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fwasset.core.path_guard import (
    PathGuardError,
    assert_within_workspace,
    is_same_or_under,
)
from fwasset.core.types import ManagedPathReason, WorkspaceLayout

#: 应用内部受管根目录名（工作区内唯一容器，用户不应手工放置内容）。
MANAGED_ROOT_DIRNAME = ".fwasset"

#: 资产内部备用副本目录名（精确目录段比较，见 D4.1）。
RETIRED_VERSIONS_DIRNAME = "旧版本"

#: D4.2 备用副本元数据文件名。禁止再写字面量 ``"退位信息.toml"``。
RETIRED_METADATA_FILENAME = "退位信息.toml"

#: 程序元数据文件名（D6.2）。
ASSET_METADATA_FILENAME = "程序信息.toml"

#: 工作区**内部**受管区域 → `.fwasset/` 下的子目录名。
_INNER_SUBDIRS: dict[ManagedPathReason, str] = {
    "staging": "staging",
    "incomplete_candidate": "incomplete",
    "workspace_state": "state",
}

#: 隔离根目录名。父规格 D2.5 / D8.2 要求它位于**工作区同卷的受控兄弟目录**
#: （删除撤销窗口内内容仍在盘上，但不能落在工作区内被扫描或被用户误操作）。
QUARANTINE_SIBLING_DIRNAME = ".fwasset-quarantine"

#: 受管根内的所有权标记文件名。路径形状不证明所有权——用户可能预先建了同名
#: 目录（``quarantine`` 还在工作区外），必须靠应用自己写下的标记确认。
MANAGED_OWNER_MARKER = ".fwasset-owned"

#: 有独立根目录的受管类别（判定顺序无关，互不重叠）。
_MANAGED_KINDS: tuple[ManagedPathReason, ...] = (
    "staging",
    "incomplete_candidate",
    "workspace_state",
    "quarantine",
)


def _segments(path: str | Path) -> list[str]:
    """返回归一化后的路径段（normcase + 正斜杠），供精确段比较使用。

    顺序与 :mod:`fwasset.core.path_guard` 一致：先 ``normcase``（Windows 的 C
    实现会把 ``/`` 也规范化为 ``\\``），再统一为正斜杠切分。反过来会让
    Windows 上的分隔符归一失效。
    """
    normalized = os.path.normcase(str(path)).replace("\\", "/")
    return [seg for seg in normalized.split("/") if seg]


def _normalized(path: str | Path) -> str:
    """normcase + 正斜杠归一后的比较值（与 ``path_guard`` 同一顺序）。"""
    return os.path.normcase(str(path)).replace("\\", "/").rstrip("/")


def _resolved_or_none(path: str | Path) -> Path | None:
    """尽量返回 resolved 路径；无法解析时返回 ``None``。

    受管归属必须按**解析后身份**判断：junction / 符号链接指向受管区域时要命中，
    ``a/../b`` 这类词法形式不能被误判为受管（``..`` 会先被折叠）。

    捕获 ``RuntimeError``：部分平台/版本在链接循环上由 ``resolve()`` 抛出而非
    返回，未捕获会让扫描整体崩掉。
    """
    try:
        return Path(str(path)).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def managed_root(workspace_root: str | Path, kind: ManagedPathReason) -> Path:
    """返回 ``kind`` 对应的受管根目录（应用生成，不接受用户来源路径）。

    - ``staging`` / ``incomplete_candidate`` / ``workspace_state`` 位于工作区内
      ``.fwasset/<子目录>``；
    - ``quarantine`` 位于**工作区同卷的兄弟目录**（父规格 D2.5 / D8.2）——删除
      内容在撤销窗口内不得留在工作区内被扫描到；
    - ``retired_versions`` / ``asset_metadata`` 在业务资产内部，没有独立根，
      传入即 ``ValueError``。
    """
    raw_root = str(workspace_root or "").strip()
    if not raw_root:
        raise PathGuardError("工作区根目录未配置，请先在设置中配置程序文件夹")
    root = _resolved_or_none(raw_root)
    if root is None:
        raise PathGuardError(f"工作区根目录无法解析，已拒绝：{raw_root}")

    if kind == "quarantine":
        parent = root.parent
        if parent == root:
            # 工作区是卷根时没有兄弟位置可用（D2.5 首批不做跨卷分支）。
            raise PathGuardError(f"工作区位于卷根，无法建立同卷隔离目录：{root}")
        return parent / f"{QUARANTINE_SIBLING_DIRNAME}-{root.name}"

    subdir = _INNER_SUBDIRS.get(kind)
    if subdir is None:
        raise ValueError(f"没有内部根目录的受管类别：{kind}")
    return root / MANAGED_ROOT_DIRNAME / subdir


def managed_path_reason(
    path: str | Path,
    *,
    is_dir: bool,
    workspace_root: str | Path | None = None,
) -> ManagedPathReason | None:
    """判定 ``path`` 属于哪一类受管路径；普通业务路径返回 ``None``。

    - ``旧版本`` 按**精确目录段**比较（``旧版本说明`` 这类前缀目录不命中），
      同名**文件**不命中；
    - ``程序信息.toml`` 仅在 ``is_dir=False`` 时命中；
    - 内部受管区域只在给出 ``workspace_root``、且 ``path`` 确实位于该工作区的
      ``.fwasset/<子目录>`` 之下时命中。缺少 ``workspace_root`` 时不做猜测。

    调用方按用途分流：scanner / USB 复制直接跳过；「软件修复」可按类别决定
    读取元数据或报告。
    """
    segments = _segments(path)
    if not segments:
        return None

    retired = os.path.normcase(RETIRED_VERSIONS_DIRNAME)
    # 目录段命中即整枝受管；末段是文件时不参与目录段比较。
    dir_segments = segments if is_dir else segments[:-1]
    if any(seg == retired for seg in dir_segments):
        return "retired_versions"

    if not is_dir and segments[-1] == os.path.normcase(ASSET_METADATA_FILENAME):
        return "asset_metadata"

    if workspace_root is None or not str(workspace_root).strip():
        return None

    # 归属判定看两种身份，任一命中即受管：
    #   ① 词法身份（先 os.path.normpath 折叠 `..`，不碰文件系统）——受管根被换成
    #      junction 时解析后会指向别处，只看解析结果会把内部区域整片漏排；
    #   ② 解析后身份——junction / 符号链接**指向受管区域**的别名要命中。
    # 只用①会漏掉②的别名；只用②会漏掉①的重定向根。
    #
    # 但**不能**反过来把「受管根解析后的目标」整体认作受管：那样一个指向真实
    # 资产目录的 staging junction 会让该资产目录被判为受管，扫描直接少掉资产。
    # 受管根被重定向属于不可信现场，由 1b 的初始化在写盘前经
    # assert_managed_root_not_redirected 拒绝。
    lexical = os.path.normpath(str(path))
    resolved = _resolved_or_none(path)
    for kind in _MANAGED_KINDS:
        try:
            root = managed_root(workspace_root, kind)
        except PathGuardError:
            # 卷根工作区没有隔离兄弟目录；其余类别不受影响，继续判定。
            continue
        if is_same_or_under(lexical, root):
            return kind
        if resolved is not None and is_same_or_under(resolved, root):
            return kind
    return None


def should_exclude_managed_path(
    path: str | Path,
    *,
    is_dir: bool,
    workspace_root: str | Path | None = None,
) -> bool:
    """:func:`managed_path_reason` 的薄包装：遍历/复制时是否跳过该路径。

    **不表示写授权**——受管目录的写入另见 :func:`assert_managed_write`。
    """
    return (
        managed_path_reason(path, is_dir=is_dir, workspace_root=workspace_root)
        is not None
    )


def assert_managed_root_not_redirected(
    workspace_root: str | Path, kind: ManagedPathReason
) -> Path:
    """校验受管根及其最近的存在祖先没有被重定向，返回受管根路径。

    ``mkdir`` 会跟随路径上任意一段 junction / 符号链接：``.fwasset`` 被指向工作
    区外时，初始化会直接在外部目录落盘。本函数是**只读前置检查**，供 1b 的初始
    化流程在持锁后调用。

    **它不能单独消除竞争**：检查与创建之间目标仍可被替换或抢占。真正的原子创建
    （持锁、O_EXCL 语义、失败恢复）属于子任务 1b 的事务基础，不在本模块。
    """
    root = managed_root(workspace_root, kind)
    existing = root
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            break
        existing = parent
    resolved = _resolved_or_none(existing)
    if resolved is None:
        raise PathGuardError(f"受管目录「{kind}」路径无法解析，已拒绝：{root}")
    if _normalized(resolved) != _normalized(existing):
        raise PathGuardError(
            f"受管目录「{kind}」路径被重定向，已拒绝：{existing} → {resolved}"
        )
    return root


def is_managed_root_owned(workspace_root: str | Path, kind: ManagedPathReason) -> bool:
    """受管根是否已由应用初始化（存在、带所有权标记、且未被重定向）。

    路径形状不能证明所有权：用户可能预先建了同名目录并往里放东西，尤其
    ``quarantine`` 在工作区**外**，不受工作区守卫保护。根不存在时返回 ``False``。

    受管根的**创建**不在本模块——它需要持锁与失败恢复，属子任务 1b。
    """
    try:
        root = assert_managed_root_not_redirected(workspace_root, kind)
    except (PathGuardError, ValueError):
        return False
    return (root / MANAGED_OWNER_MARKER).is_file()


def assert_managed_write(
    path: str | Path,
    workspace_root: str | Path,
    *,
    expect: ManagedPathReason,
) -> Path:
    """断言 ``path`` 是 ``expect`` 受管根之下的合法写入目标，返回 resolved Path。

    三层校验，缺一不可，**没有跳过开关**：

    1. 拒绝相对路径与显式 ``..`` 段；工作区内的三类（staging / 候选区 / 状态）
       额外经 :func:`~fwasset.core.path_guard.assert_within_workspace`；
    2. 目标必须落在 ``expect`` 指定的受管根内；
    3. 受管根必须已带所有权标记且未被重定向——**路径形状不证明所有权**，
       用户预建的同名目录不得因为「路径长得对」就获得写授权。

    受管根的**建立**不在本模块——它需要持锁与失败恢复（子任务 1b）；初始化
    不通过放宽本守卫来完成。

    **被排除不等于可写**：``旧版本/`` 会被扫描排除，但它是用户资产，不是受管
    写入区；隔离区路径也不能拿 staging 的授权写入。``quarantine`` 位于工作区外
    的兄弟目录（D2.5），工作区守卫不适用，第 3 层是它唯一的所有权保障。
    """
    root = managed_root(workspace_root, expect)
    if expect != "quarantine":
        resolved = assert_within_workspace(path, workspace_root)
    else:
        raw_path = str(path)
        if not Path(raw_path).is_absolute():
            raise PathGuardError(f"路径必须为绝对路径，已拒绝写入：{raw_path}")
        if any(seg == ".." for seg in Path(raw_path).parts):
            raise PathGuardError(f"路径含越界段「..」，已拒绝写入：{raw_path}")
        candidate = _resolved_or_none(raw_path)
        if candidate is None:
            raise PathGuardError(f"路径无法解析，已拒绝写入：{raw_path}")
        resolved = candidate

    if not is_same_or_under(resolved, root):
        raise PathGuardError(
            f"目标不在受管目录「{expect}」内，已拒绝写入：{path}（受管根：{root}）"
        )
    if not is_managed_root_owned(workspace_root, expect):
        raise PathGuardError(
            f"受管目录「{expect}」尚未由应用初始化或已被占用，已拒绝写入：{root}"
        )
    return resolved


# ---------------------------------------------------------------------------
# 工作区布局识别（D9.0）
# ---------------------------------------------------------------------------


def _has_model_marker(root: Path) -> bool:
    """型号根领域标志：与 ``reference_lookup._has_model_marker`` 同一规则。

    两处必须一致，否则布局判定与引用反查会对同一目录给出不同归属。
    """
    from fwasset.core.model_config import MODEL_CONFIG_FILENAME
    from fwasset.core.platform_config import PLATFORM_CONFIG_FILENAME

    return (
        (root / "通用").is_dir()
        or (root / "定制").is_dir()
        or (root / MODEL_CONFIG_FILENAME).is_file()
        or (root / PLATFORM_CONFIG_FILENAME).is_file()
    )


def _is_model_root_own_entry(entry: Path, *, is_dir: bool) -> bool:
    """是否为型号根自身的合法一级内容（``single_model`` 下允许出现在工作区根）。

    **名称与类型都要对**：``通用`` / ``定制`` 必须是目录，两份型号级配置必须是
    普通文件。只比名称会让一个名为 ``型号配置.toml`` 的**目录**把工作区判成
    ``single_model``，而后续配置读取根本用不了它。其余散落内容一律算未知。
    """
    from fwasset.core.model_config import MODEL_CONFIG_FILENAME
    from fwasset.core.platform_config import PLATFORM_CONFIG_FILENAME

    name = os.path.normcase(entry.name)
    if name in {os.path.normcase("通用"), os.path.normcase("定制")}:
        return is_dir
    if name in {
        os.path.normcase(MODEL_CONFIG_FILENAME),
        os.path.normcase(PLATFORM_CONFIG_FILENAME),
    }:
        return not is_dir
    return False


def _is_managed_container(entry: Path, *, is_dir: bool) -> bool:
    """是否为应用内部容器目录 ``.fwasset``（Windows 大小写不敏感）。

    容器本身不是受管根（受管根是它的子目录），但同样不算未知内容。
    """
    return is_dir and os.path.normcase(entry.name) == os.path.normcase(
        MANAGED_ROOT_DIRNAME
    )


def _is_ignorable_entry(entry: Path, workspace_root: Path, *, is_dir: bool) -> bool:
    """一级条目是否可忽略（不算型号，也不算「无法归类的内容」）。

    只忽略**应用自身产生**或**扫描器明确排除**的内容：受管根、``.fwasset``
    容器、``旧版本``/``程序信息.toml``、以及被 scanner 排除关键词命中的**目录**。

    关键词只对目录生效——scanner 的排除语义是「整枝目录不要」，工作区根下的
    散落**文件**（``接线图.txt`` 之类）不是扫描排除对象，必须计入未知内容，
    否则会把用户堆放资料的目录误判成空工作区。
    """
    if should_exclude_managed_path(entry, is_dir=is_dir, workspace_root=workspace_root):
        return True
    if _is_managed_container(entry, is_dir=is_dir):
        return True
    if not is_dir:
        return False
    from fwasset.core.settings import SCAN_EXCLUDE_DIR_KEYWORDS

    lower_name = entry.name.lower()
    return any(
        keyword and str(keyword).lower() in lower_name
        for keyword in SCAN_EXCLUDE_DIR_KEYWORDS
    )


def detect_workspace_layout(workspace_root: str | Path) -> WorkspaceLayout:
    """识别工作区布局（D9.0）。

    - 根自带型号标志、且根下**没有**型号子目录 → ``single_model``（旧布局）；
    - 根无标志、有带标志的一级子目录、且其余一级条目均可忽略 → ``multi_model``；
    - 根无标志、无型号、无未知内容 → ``empty``；
    - 根不可读、**根标志与型号子目录混合**、或存在无法归类的一级内容
      → ``invalid``。

    只看目录与配置，**不查资产索引**：空白型号（只有配置、没有程序）仍算型号，
    扫描零资产不等于没有型号（D9.1）。``invalid`` 绝不能被当作空工作区初始化，
    否则会在用户的既有资料上直接建结构；混合布局也不能按 ``single_model``
    处理，否则子任务 3a 的迁移会用错误的归属搬运目录。
    """
    raw_root = str(workspace_root or "").strip()
    if not raw_root:
        return "invalid"
    root = Path(raw_root)
    if not root.is_dir():
        return "invalid"
    root = root.resolve()

    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return "invalid"

    root_is_model = _has_model_marker(root)
    model_count = 0
    has_unknown = False
    for child in children:
        is_dir = child.is_dir()
        if _is_ignorable_entry(child, root, is_dir=is_dir):
            continue
        if is_dir and _has_model_marker(child):
            model_count += 1
            continue
        # single_model 布局下，通用/ 定制/ 与两份型号配置是根自身的内容；
        # 其余散落文件仍算未知项（不得因为「根是型号」就整片放行）。
        if root_is_model and _is_model_root_own_entry(child, is_dir=is_dir):
            continue
        has_unknown = True

    if root_is_model:
        # 根既是型号、又含型号子目录 → 归属不明，交由人工或迁移流程处理。
        return "invalid" if (model_count or has_unknown) else "single_model"
    if has_unknown:
        return "invalid"
    return "multi_model" if model_count else "empty"
