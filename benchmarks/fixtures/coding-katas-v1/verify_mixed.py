from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


# 加载跨语言任务中的 Python 编码器
def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("benchmark_encoder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 验证 Python 编码结果可被 TypeScript 解码器还原为统一契约
def main() -> int:
    task_id = sys.argv[1]
    root = Path(__file__).resolve().parent
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))[task_id]
    module = _load_module(root / "src" / f"{task_id}.py")
    for index, case in enumerate(cases):
        encoded = module.solve(*case["args"])
        result = subprocess.run(
            ["node", str(root / "mixed_runner.mjs"), task_id],
            input=encoded,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stderr)
            return 1
        actual = json.loads(result.stdout)
        if actual != case["expected"]:
            print(f"case {index}: expected {case['expected']!r}, got {actual!r}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
