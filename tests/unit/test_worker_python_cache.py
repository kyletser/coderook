from __future__ import annotations

import json
import sys
from pathlib import Path

from code_rook.core.subagent.models import WriteClaim
from code_rook.core.subagent.tool import SpawnAgentTool
from code_rook.core.tools.builtin.bash import BashTool


# 功能：显式 py_compile 的缓存离开工作树且执行后清理，真实源文件变更仍可审查
# 设计：运行真实 Python 编译器而非模拟环境变量，复现 dont_write_bytecode 无法禁止的缓存副作用
async def test_worker_explicit_compile_keeps_workspace_clean(tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text("answer = 42\n", encoding="utf-8")
    tool = BashTool(tmp_path, isolate_python_cache=True)
    code = (
        "import json,py_compile,sys; "
        "py_compile.compile('target.py',doraise=True); "
        "print(json.dumps(sys.pycache_prefix))"
    )
    result = await tool.invoke({"command": f'"{sys.executable}" -c "{code}"'})
    assert not result.is_error, result.content
    cache = Path(json.loads(result.content))
    assert not cache.is_relative_to(tmp_path)
    assert not cache.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["target.py"]
    claim = WriteClaim(exact_files=["target.py"])
    assert SpawnAgentTool._claim_violations(["outside.py"], claim) == ["outside.py"]


# 功能：编译失败仍返回真实错误且不把失败缓存混入工作树
# 设计：故意提供语法错误，验证隔离缓存不会把命令失败伪装成成功
async def test_worker_compile_failure_is_not_hidden(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = await BashTool(tmp_path, isolate_python_cache=True).invoke(
        {"command": f'"{sys.executable}" -m py_compile broken.py'},
    )
    assert result.is_error
    assert "SyntaxError" in result.content
    assert not (tmp_path / "__pycache__").exists()
