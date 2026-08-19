from __future__ import annotations

import sys
from pathlib import Path


# 检查解释答案包含任务要求的全部关键事实且不是空占位
def main() -> int:
    task_id = sys.argv[1]
    root = Path(__file__).resolve().parent
    answer = (root / f"{task_id}.md").read_text(encoding="utf-8").lower()
    required = {
        "explain-cache": ("ttl", "monotonic", "expired"),
        "explain-lock": ("atomic", "replace", "process"),
        "explain-cache-concurrency": ("thread", "race", "lock"),
        "explain-cache-memory": ("expired", "lazy", "memory"),
        "explain-atomic-durability": ("fsync", "directory", "durability"),
        "explain-atomic-failure": ("temporary", "cleanup", "exception"),
    }[task_id]
    missing = [term for term in required if term not in answer]
    if len(answer.strip()) < 120 or missing:
        print(f"answer too short or missing facts: {missing}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
