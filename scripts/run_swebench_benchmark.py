from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from code_rook.benchmark.experiment import resolve_experiment_candidate
from code_rook.benchmark.swebench import SWEbenchInstance
from code_rook.core.config import get_config

IMAGE = 'coderook-swebench:20260907'
CONTROLLER = 'coderook-swebench-controller-20260907'


# 执行不携带凭据的 Docker 控制命令并为失败保留可诊断输出
def docker(*args: str, timeout: float = 1800) -> str:
    result = subprocess.run(['docker', *args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {result.stderr[-3000:]}')
    return result.stdout.strip()


# 启动仅可信 Harness 使用的控制器；Agent 容器永不继承 Docker socket 或评测答案挂载
def controller(root: Path) -> None:
    names = docker('ps', '-a', '--format', '{{.Names}}').splitlines()
    if CONTROLLER in names:
        bound = docker('inspect', CONTROLLER, '--format', '{{json .Mounts}}')
        if not any(Path(m.get('Source', '')).name == root.name
                   for m in json.loads(bound) if m.get('Destination') == '/evidence'):
            raise RuntimeError('existing controller belongs to a different experiment')
        docker('start', CONTROLLER)
        return
    docker('run', '-d', '--name', CONTROLLER,
           '--mount', f'type=bind,source={root},target=/evidence',
           '-v', '/var/run/docker.sock:/var/run/docker.sock', IMAGE, 'sleep', 'infinity')


# 将只含当前运行时代码和 Python 依赖的目录传入实例，不复制主机工作区或用户配置
def copy_runtime(name: str) -> None:
    producer = subprocess.Popen(['docker', 'cp', f'{CONTROLLER}:/opt/coderook', '-'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert producer.stdout is not None
    consumer = subprocess.Popen(['docker', 'cp', '-', f'{name}:/opt/'],
                                stdin=producer.stdout, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    producer.stdout.close()
    try:
        _, errors = consumer.communicate(timeout=300)
        producer.wait(timeout=30)
        if consumer.returncode or producer.returncode:
            raise RuntimeError(f'runtime transfer failed: {errors.decode(errors="replace")}')
    finally:
        for process in (consumer, producer):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


# 创建不会修复目标缺陷的新增文件补丁，避免官方 Harness 跳过空 patch 而没有真正测试基线
def baseline_patch() -> str:
    return ('diff --git a/coderook_baseline_control.txt b/coderook_baseline_control.txt\n'
            'new file mode 100644\n--- /dev/null\n+++ b/coderook_baseline_control.txt\n'
            '@@ -0,0 +1 @@\n+Environment control; no repository implementation is changed.\n')


# 只从官方实例 report 读取 resolved，找不到判分产物时保持未知而不是按进程退出码推断成功
def official_result(root: Path, run_id: str, instance_id: str) -> bool | None:
    for path in (root / 'logs').rglob('report.json'):
        if run_id in path.parts and instance_id in path.parts:
            value = json.loads(path.read_text(encoding='utf-8')).get(instance_id, {}).get('resolved')
            if isinstance(value, bool):
                return value
    return None


# 用独立 run_id 调用官方判分，保留所有日志并禁止把 Agent 自称成功作为 resolved
def evaluate(root: Path, cohort: str, predictions: str, run_id: str,
             instance_ids: list[str]) -> int:
    command = ['docker', 'exec', CONTROLLER, 'python', '-m',
               'swebench.harness.run_evaluation', '--dataset_name',
               f'/evidence/frozen/{cohort}.evaluation.json', '--predictions_path',
               predictions, '--run_id', run_id, '--max_workers', '1', '--timeout', '1200',
               '--instance_ids', *instance_ids]
    with (root / f'{run_id}.console.log').open('w', encoding='utf-8') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=2400 * len(instance_ids))
    return result.returncode


# 汇总每个冻结实例的官方成绩与真实 usage，包含失败及未评分项且不使用 Agent 自报成功
def summarize(root: Path, cohort: str, run_id: str, rows: list[dict]) -> dict:
    results = []
    for row in rows:
        instance_id = row['instance_id']
        path = root / cohort / instance_id / 'record.json'
        record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        execution = record.get('execution', {})
        usage = execution.get('usage', [])
        event_path = path.parent / 'evidence/events.jsonl'
        events = [json.loads(line) for line in event_path.read_text(encoding='utf-8').splitlines()] \
            if event_path.exists() else []
        failed_calls = [event for event in events if event.get('type') == 'tool.call_failed']
        results.append({'instance_id': instance_id,
            'resolved': official_result(root, run_id, instance_id),
            'runtime_status': execution.get('status', 'not_run'),
            'runtime_reason': execution.get('reason'),
            'infrastructure_error': record.get('infrastructure_error'),
            'input_tokens': sum(item.get('input_tokens', 0) for item in usage),
            'output_tokens': sum(item.get('output_tokens', 0) for item in usage),
            'elapsed_s': execution.get('elapsed_s'), 'tool_calls': execution.get('tool_calls', 0),
            'steps': sum(event.get('type') == 'step.started' for event in events),
            'tool_errors': len(failed_calls),
            'schema_errors': sum(event.get('error_class') == 'schema_error' for event in failed_calls),
            'missing_action_errors': sum('action is required for tool:' in event.get('error_message', '')
                                         for event in failed_calls),
            'artifact_reads': sum(event.get('type') == 'tool.call_started'
                                  and event.get('tool_name') == 'artifact_read' for event in events)})
    elapsed = [row['elapsed_s'] for row in results if row['elapsed_s'] is not None]
    summary = {'cohort': cohort, 'official_run_id': run_id, 'total': len(rows),
        'resolved': sum(row['resolved'] is True for row in results),
        'unresolved': sum(row['resolved'] is False for row in results),
        'ungraded': sum(row['resolved'] is None for row in results),
        'runtime_successes': sum(row['runtime_status'] == 'success' for row in results),
        'tool_errors': sum(row['tool_errors'] for row in results),
        'schema_errors': sum(row['schema_errors'] for row in results),
        'input_tokens': sum(row['input_tokens'] for row in results),
        'output_tokens': sum(row['output_tokens'] for row in results),
        'median_agent_elapsed_s': statistics.median(elapsed) if elapsed else None,
        'estimated_dollar_cost': None, 'results': results}
    (root / f'{cohort}-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key != 'results'}, indent=2))
    return summary


# 使用冻结任务在独立官方实例容器运行 CodeRook；每题尝试一次并在退出后才导出补丁
def run_instance(root: Path, row: dict, args: argparse.Namespace) -> dict:
    safe = SWEbenchInstance.model_validate(row)
    destination = root / ('preflight' if args.stage == 'preflight' else args.cohort) / safe.instance_id
    if destination.exists():
        raise RuntimeError(f'attempt already exists: {safe.instance_id}')
    destination.mkdir(parents=True)
    name = 'coderook-lite-' + hashlib.sha256(safe.instance_id.encode()).hexdigest()[:12]
    started = time.monotonic()
    resolved, candidate = resolve_experiment_candidate(
        get_config(), expected_model=args.model, require_pricing=False,
    )
    # 这里仅传入白名单字段，完整 dataset 行（含标准补丁与隐藏测试）绝不进入 Agent
    payload = {**safe.model_dump(), 'route': resolved.route.model_dump(mode='json'),
               'receipt': resolved.receipt.model_dump(mode='json'),
               'credential': resolved.credential, 'max_steps': args.max_steps,
               'wall_time': args.wall_time, 'smoke': args.stage == 'preflight'}
    if args.stage == 'preflight':
        payload['credential'] = 'local-smoke-no-model-credential'
    created = False
    record: dict = {'instance_id': safe.instance_id, 'candidate': candidate,
                    'image': row['image'], 'max_steps': args.max_steps,
                    'wall_time': args.wall_time, 'official_resolved': None}
    try:
        available = docker('image', 'ls', '-q', row['image'])
        if not available:
            docker('pull', row['image'], timeout=2400)
        record['image_digest'] = docker('image', 'inspect', row['image'], '--format', '{{.Id}}')
        docker('run', '-d', '--name', name, '--cap-drop', 'ALL', '--security-opt',
               'no-new-privileges', '--pids-limit', '512', '--memory', '6g', '--cpus', '4',
               '-w', '/testbed', row['image'], 'sleep', 'infinity')
        created = True
        head = docker('exec', name, 'git', '-C', '/testbed', 'rev-parse', 'HEAD')
        if head != safe.base_commit:
            # 官方镜像可追加只调整权限的准备提交；逐文件比较内容，不能误当成答案或代码差异
            trees = [[entry.split(' ', 1)[1] for entry in docker(
                'exec', name, 'git', '-C', '/testbed', 'ls-tree', '-r', '-z', revision,
            ).split('\0') if entry] for revision in (safe.base_commit, head)]
            if trees[0] != trees[1]:
                raise RuntimeError(f'instance baseline content mismatch: {head}')
        record['image_head'] = head
        record['base_commit'] = safe.base_commit
        docker('exec', name, 'git', '-C', '/testbed', 'config', 'core.fileMode', 'false')
        copy_runtime(name)
        docker('exec', name, '/opt/coderook/venv/bin/python', '-c',
               'import code_rook.core.runner; print("runtime imports OK")')
        command = ['docker', 'exec', '-i', '-e', 'HOME=/tmp/coderook-home', name,
                   'bash', '-c', 'source /opt/miniconda3/bin/activate && conda activate testbed '
                   '&& exec /opt/coderook/venv/bin/python /opt/coderook/agent.py']
        print(f'Agent starting: {safe.instance_id}', flush=True)
        with (destination / 'console.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log,
                                       stderr=subprocess.STDOUT)
            try:
                process.communicate(json.dumps(payload).encode(), timeout=args.wall_time + 90)
                record['agent_exit_code'] = process.returncode
            except subprocess.TimeoutExpired:
                docker('stop', '-t', '3', name)
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError('agent container wall-time limit reached') from None
        docker('cp', f'{name}:/coderook-evidence', str(destination / 'evidence'))
        execution = json.loads((destination / 'evidence/execution.json').read_text(encoding='utf-8'))
        record['execution'] = execution
        # runtime 产物不是用户提交，明确排除；用户源码与新增回归测试仍被包含
        docker('exec', name, 'git', '-C', '/testbed', 'add', '-N', '--', '.',
               ':(exclude).coderook')
        patch = docker('exec', name, 'git', '-C', '/testbed', 'diff', '--binary',
                       '--no-ext-diff', head, '--', '.', ':(exclude).coderook')
        if patch:
            patch += '\n'
        prediction = {'instance_id': safe.instance_id,
                      'model_name_or_path': f'coderook-{args.model}', 'model_patch': patch}
        (destination / 'prediction.json').write_text(json.dumps(prediction), encoding='utf-8')
        record['prediction_sha256'] = hashlib.sha256(patch.encode()).hexdigest()
    except Exception as exc:
        record['infrastructure_error'] = str(exc).replace(resolved.credential, '[REDACTED]')
    finally:
        if created:
            docker('rm', '-f', name)
        record['total_elapsed_s'] = time.monotonic() - started
        (destination / 'record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(f'Attempt retained: {safe.instance_id}', flush=True)
    return record


# 分离环境烟测、真实解题及判分入口，不覆盖已存在的尝试或把未评分任务算作通过
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--stage', choices=['controls', 'preflight', 'run', 'evaluate'], required=True)
    parser.add_argument('--cohort', choices=['pilot', 'formal'], default='pilot')
    parser.add_argument('--model', default='qwen3.8-flash')
    parser.add_argument('--max-steps', type=int, default=40)
    parser.add_argument('--wall-time', type=int, default=900)
    args = parser.parse_args()
    root = args.root.resolve()
    rows = json.loads((root / f'frozen/{args.cohort}.evaluation.json').read_text(encoding='utf-8'))
    controller(root)
    if args.stage == 'preflight':
        record = run_instance(root, rows[0], args)
        if not record.get('execution', {}).get('smoke_passed'):
            print(json.dumps(record, indent=2))
            return 1
        return 0
    if args.stage == 'controls':
        row = rows[0]
        control = root / 'baseline-control.jsonl'
        control.write_text(json.dumps({'instance_id': row['instance_id'],
                           'model_name_or_path': 'baseline-control',
                           'model_patch': baseline_patch()}) + '\n', encoding='utf-8')
        results = {}
        for kind, path in [('baseline', '/evidence/baseline-control.jsonl'), ('gold', 'gold')]:
            print(f'Official environment control: {kind}', flush=True)
            run_id = f'coderook-lite-control-{kind}-{int(time.time())}'
            if evaluate(root, args.cohort, path, run_id,
                        [row['instance_id']]):
                return 1
            results[kind] = {'run_id': run_id,
                             'resolved': official_result(root, run_id, row['instance_id'])}
            print(json.dumps(results[kind]), flush=True)
        results['passed'] = (results['baseline']['resolved'] is False
                             and results['gold']['resolved'] is True)
        (root / 'environment-controls.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        return 0 if results['passed'] else 1
    if args.stage == 'run':
        controls = json.loads((root / 'environment-controls.json').read_text(encoding='utf-8'))
        smoke_id = json.loads((root / 'frozen/pilot.json').read_text(encoding='utf-8'))[0]['instance_id']
        smoke = json.loads((root / 'preflight' / smoke_id / 'record.json').read_text(encoding='utf-8'))
        if not controls.get('passed') or not smoke.get('execution', {}).get('smoke_passed'):
            raise SystemExit('official environment controls and keyless Agent preflight must pass first')
        image_id = docker('image', 'inspect', IMAGE, '--format', '{{.Id}}')
        metadata_path = root / f'{args.cohort}-execution-contract.json'
        if metadata_path.exists():
            raise SystemExit('execution contract already exists; do not overwrite a scored run')
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        metadata_path.write_text(json.dumps({'runtime_commit': commit, 'runtime_image': image_id,
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'agent_adapter_sha256': hashlib.sha256(Path('scripts/swebench_container_agent.py').read_bytes()).hexdigest(),
            'attempts_per_task': 1, 'max_steps': args.max_steps, 'wall_time': args.wall_time,
            'model': args.model, 'temperature': 0.0, 'strategy': 'single/direct',
            'dollar_cost': 'unavailable; user authorized experiments without a dollar cap'},
            indent=2), encoding='utf-8')
        for row in rows:
            record = run_instance(root, row, args)
            if record.get('infrastructure_error'):
                print(record['infrastructure_error'], flush=True)
                return 1
        return 0
    predictions = []
    for row in rows:
        path = root / args.cohort / row['instance_id'] / 'prediction.json'
        if not path.exists():
            raise SystemExit(f'missing attempt: {row["instance_id"]}')
        predictions.append(json.loads(path.read_text(encoding='utf-8')))
    output = root / f'{args.cohort}-predictions.jsonl'
    output.write_text(''.join(json.dumps(row) + '\n' for row in predictions), encoding='utf-8')
    run_id = f'coderook-lite-{args.cohort}-{int(time.time())}'
    code = evaluate(root, args.cohort, f'/evidence/{output.name}',
                    run_id, [row['instance_id'] for row in rows])
    summarize(root, args.cohort, run_id, rows)
    return code


if __name__ == '__main__':
    sys.exit(main())
