from __future__ import annotations

import argparse
import hashlib
import io
import json
from collections import defaultdict
from pathlib import Path

import httpx

from code_rook.benchmark.swebench import SWEbenchInstance

DATASET = 'SWE-bench/SWE-bench_Lite'
REVISION = 'b0dde1093fe417d83b7184254edf8199c1f0dff5'
SEED = 'coderook-lite-20260907-v1'


# 按预先固定哈希排序并跨仓库轮转选题；选择过程不读取 patch、测试结果或难度标签
def select_instances(rows: list[dict], count: int, exclude: set[str]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row['instance_id'] not in exclude:
            groups[row['repo']].append(row)

    # 用稳定种子生成与输入顺序无关的排序键
    def key(value: str) -> str:
        return hashlib.sha256((SEED + ':' + value).encode()).hexdigest()

    for group in groups.values():
        group.sort(key=lambda item: key(item['instance_id']))
    selected: list[dict] = []
    while len(selected) < count:
        progressed = False
        for repo in sorted(groups, key=key):
            if groups[repo] and len(selected) < count:
                selected.append(groups[repo].pop(0))
                progressed = True
        if not progressed:
            raise ValueError('not enough distinct instances')
    return selected


# 下载固定版本并分别保存 Agent 可见清单与仅供官方判分的原始记录，拒绝覆盖已有切片
def main() -> None:
    import pyarrow.parquet as pq

    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit('output already exists; frozen slices must not be overwritten')
    url = f'https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data/test-00000-of-00001.parquet'
    response = httpx.get(url, follow_redirects=True, timeout=90)
    response.raise_for_status()
    rows = pq.read_table(io.BytesIO(response.content)).to_pylist()
    pilot = select_instances(rows, 5, set())
    formal = select_instances(rows, 20, {row['instance_id'] for row in pilot})
    output.mkdir(parents=True)
    manifest = {'dataset': DATASET, 'revision': REVISION, 'seed': SEED,
                'parquet_sha256': hashlib.sha256(response.content).hexdigest(),
                'selection': 'hash-ordered repository round robin; disjoint pilot/formal',
                'pilot': [row['instance_id'] for row in pilot],
                'formal': [row['instance_id'] for row in formal]}
    for name, selected in [('pilot', pilot), ('formal', formal)]:
        safe = [SWEbenchInstance.model_validate(row).model_dump() for row in selected]
        (output / f'{name}.json').write_text(json.dumps(safe, indent=2), encoding='utf-8')
        # 该文件只由可信评测控制器读取，不挂载到 Agent 容器
        (output / f'{name}.evaluation.json').write_text(
            json.dumps(selected, indent=2), encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
