from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import socket
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_rook.core.authority import detect_sandbox_capability
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor, ProviderDoctorResult
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.runtime.reconcile import RuntimeReconciler, RuntimeReconcileReport
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.store import SessionStore

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(api[_-]?key|token|secret|password)(\s*[:=]\s*)[^\s,;]+"
)


# 探测本地端口是否已有监听者，不发送业务数据
def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return True
    except OSError:
        return False


# 对诊断文本中的常见凭据形式做保守替换
def _redact(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            return f"{match.group(1)}<redacted>"
        return f"{match.group(2)}{match.group(3)}<redacted>"

    return _SECRET_RE.sub(replace, text)


# 构建不含密钥正文的环境、工具链、sandbox、端口、磁盘和 runtime 汇总
def build_system_report(config: CodeRookConfig) -> dict[str, Any]:
    state_root = Path("~/.coderook").expanduser()
    errors: list[dict[str, str]] = []
    try:
        runtime: dict[str, Any] = RuntimeReconciler(
            RuntimeStore(state_root / "runtime.db"),
            SessionStore(state_root / "sessions"),
            workspace=Path.cwd(),
            journal_path=state_root / "runtime-repair.jsonl",
        ).inspect().model_dump(mode="json")
    except Exception as exc:
        runtime = {"healthy": False, "issues": [{"code": "runtime_unreadable"}]}
        errors.append({"section": "runtime", "error": type(exc).__name__})
    disk = shutil.disk_usage(Path.cwd())
    sandbox = detect_sandbox_capability()
    try:
        active_route = RouteStore().active()
    except Exception as exc:
        active_route = None
        errors.append({"section": "routes", "error": type(exc).__name__})
    tools = {
        name: shutil.which(name) or "unavailable"
        for name in ("git", "rg", "node", "npm", "pyright", "tsc")
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "workspace": str(Path.cwd()),
        "config": {
            "core": {"host": config.host, "port": config.port},
            "api": {"host": config.api.host, "port": config.api.port},
            "active_route_id": active_route.id if active_route is not None else None,
            "api_token_configured": bool(config.api.token),
        },
        "ports": {
            "ipc_listening": _port_listening(config.host, config.port),
            "api_listening": _port_listening(config.api.host, config.api.port),
        },
        "sandbox": sandbox.model_dump(mode="json"),
        "tools": tools,
        "disk": {
            "total_bytes": disk.total,
            "free_bytes": disk.free,
            "free_percent": round(disk.free / max(1, disk.total) * 100, 2),
        },
        "runtime": runtime,
        "errors": errors,
    }


# 输出统一系统诊断报告，适合作为安装与升级后的第一检查入口
def cmd_system_doctor(config: CodeRookConfig, *, as_json: bool = False) -> None:
    report = build_system_report(config)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"platform: {report['platform']}  python={report['python']}")
    ports = report["ports"]
    assert isinstance(ports, dict)
    print(f"ports: ipc={ports['ipc_listening']} api={ports['api_listening']}")
    disk = report["disk"]
    assert isinstance(disk, dict)
    print(f"disk free: {disk['free_percent']}%")
    runtime = report["runtime"]
    assert isinstance(runtime, dict)
    print(f"runtime healthy: {not bool(runtime.get('issues'))}")


# 经显式确认生成默认脱敏的诊断 ZIP，不包含会话正文、凭据文件或原始 trace
def cmd_diagnostic_bundle(
    config: CodeRookConfig,
    output: Path,
    *,
    confirmed: bool,
) -> None:
    if not confirmed:
        raise SystemExit("diagnostic bundle requires --yes confirmation")
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    state_root = Path("~/.coderook").expanduser()
    report = build_system_report(config)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "system-report.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        for path in sorted(state_root.glob("*.log")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[-1_000_000:]
            except OSError:
                continue
            archive.writestr(f"logs/{path.name}", _redact(content))
    print(str(target))


# 诊断指定或当前活动路由，并只返回脱敏分类结果
async def diagnose_route(
    config: CodeRookConfig,
    route_id: str | None = None,
    *,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    doctor: ProviderDoctor | None = None,
) -> ProviderDoctorResult:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    registry = RouteRegistry(
        config.llm,
        route_store=routes,
        credential_store=credentials,
    )
    route = registry.route(route_id)
    credential = credentials.resolve(route.credential_ref)
    return await (doctor or ProviderDoctor()).check(route, credential)


# 运行 provider doctor，并按文本或 JSON 输出无敏感信息的结果
def cmd_doctor(
    config: CodeRookConfig,
    route_id: str | None = None,
    *,
    as_json: bool = False,
) -> int:
    result = asyncio.run(diagnose_route(config, route_id))
    if as_json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"route:      {result.route_id}")
        print(f"status:     {result.status}")
        print(f"category:   {result.category}")
        print(f"credential: {result.credential_source}")
        if result.http_status is not None:
            print(f"http:       {result.http_status}")
        print(f"message:    {result.message}")
    return 0 if result.status == "ok" else 1


# 检查或幂等修复 runtime 投影一致性，并支持稳定 JSON 输出
def cmd_runtime_doctor(*, repair: bool = False, as_json: bool = False) -> None:
    state_root = Path("~/.coderook").expanduser()
    reconciler = RuntimeReconciler(
        RuntimeStore(state_root / "runtime.db"),
        SessionStore(state_root / "sessions"),
        workspace=Path.cwd(),
        journal_path=state_root / "runtime-repair.jsonl",
    )
    report: RuntimeReconcileReport = (
        asyncio.run(reconciler.repair()) if repair else reconciler.inspect()
    )
    if as_json:
        print(report.model_dump_json(indent=2))
        return
    print(f"runtime schema: {report.runtime_schema_version}")
    print(
        f"sessions={report.session_count} threads={report.thread_count} "
        f"turns={report.turn_count} issues={len(report.issues)}"
    )
    for issue in report.issues:
        target = issue.turn_id or issue.thread_id or "runtime"
        repairable = " repairable" if issue.repairable else ""
        print(f"[{issue.severity}] {issue.code} {target}{repairable}: {issue.detail}")
    if report.repaired:
        print(f"repaired: {', '.join(report.repaired)}")
