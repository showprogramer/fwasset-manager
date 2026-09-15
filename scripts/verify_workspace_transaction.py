"""TASK-20260915 隔离场景验证：新增事务、写入期硬退出、恢复与阻写。

在独立临时目录中执行，不触碰真实工作区。本模块无 UI 入口，按无 UI
等效规则以脚本完成场景验证，供人工复核与回归复跑。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fwasset.core.workspace_transaction import (
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
    load_workspace_status,
    recover_interrupted_workspace,
)


def state_files(workspace: Path) -> dict[str, object]:
    state_dir = workspace / ".fwasset" / "state"
    return {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(state_dir.iterdir())
        if p.suffix == ".json"
    }


def expect_blocked(workspace: Path, operation: str) -> str:
    try:
        with WorkspaceTransaction(workspace, operation=operation):
            raise AssertionError("不应进入事务体")
    except WorkspaceRecoveryRequiredError as exc:
        return str(exc)
    raise AssertionError("未抛出 WorkspaceRecoveryRequiredError")


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="fwasset-verify-wt-"))
    workspace = base / "workspace"
    workspace.mkdir()
    print(f"隔离工作区: {workspace}")

    with WorkspaceTransaction(workspace, operation="verify-create") as tx:
        tx.begin_product_write()
        tx.commit()
    status = load_workspace_status(workspace)
    print(f"场景1 新增事务提交后: {status}")
    print(f"场景1 状态目录: {state_files(workspace)}")
    assert status.state == "clean"
    assert status.generation == 2
    assert status.operation is None

    crash = (
        "import os\n"
        "from fwasset.core.workspace_transaction import WorkspaceTransaction\n"
        f"with WorkspaceTransaction(r'{workspace}', operation='verify-crash') as tx:\n"
        "    tx.begin_product_write()\n"
        "    os._exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", crash])
    assert result.returncode == 1, "子进程应模拟写入期硬退出"
    status = load_workspace_status(workspace)
    print(f"场景2 写入期硬退出后: {status}")
    print(f"场景2 状态目录: {state_files(workspace)}")
    assert status.state == "operation_in_progress"
    assert status.generation == 3
    assert status.operation == "verify-crash"

    message = expect_blocked(workspace, "verify-blocked")
    print(f"场景3 中断现场阻止新写事务: {message}")

    status = recover_interrupted_workspace(workspace)
    print(f"场景4 恢复流程后: {status}")
    print(f"场景4 状态目录: {state_files(workspace)}")
    assert status.state == "recovery_required"
    assert status.generation == 4
    assert status.operation == "verify-crash"

    message = expect_blocked(workspace, "verify-blocked-2")
    print(f"场景5 recovery_required 下写事务仍被阻止: {message}")

    print("全部断言通过")


if __name__ == "__main__":
    main()
