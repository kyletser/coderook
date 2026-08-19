from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


# 从指定文件加载待验证模块，避免依赖 fixture 外部的导入路径
def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("benchmark_subject", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 逐条运行 JSON case，并在首个不匹配处返回可诊断错误
def main() -> int:
    task_id = sys.argv[1]
    root = Path(__file__).resolve().parent
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))[task_id]
    module = _load_module(root / "src" / f"{task_id}.py")
    for index, case in enumerate(cases):
        actual = module.solve(*case["args"])
        if actual != case["expected"]:
            print(f"case {index}: expected {case['expected']!r}, got {actual!r}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
