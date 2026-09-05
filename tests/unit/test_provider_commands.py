from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from code_rook.cli import main as cli_main_module
from code_rook.cli.commands import doctor as doctor_command
from code_rook.cli.commands.provider import (
    cmd_model_list,
    cmd_provider_add,
    cmd_provider_edit,
    cmd_provider_list,
    cmd_provider_remove,
    cmd_provider_use,
)
from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.core.daemon_lock import DaemonLock
from code_rook.core.llm.credentials import CredentialResolution, CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorCheck, ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import ProviderRoute, get_route_preset
from code_rook.core.upgrade import UpgradeStateLockError


class _MemoryKeyring:
    # 初始化内存凭据字典，隔离测试与操作系统 keyring
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    # 从内存字典读取指定账户凭据
    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    # 向内存字典写入指定账户凭据
    def set_password(self, service: str, account: str, password: str) -> None:
        self.values[(service, account)] = password

    # 从内存字典删除指定账户凭据
    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class _SuccessfulDoctor:
    # 返回与 route 声明能力和摘要一致的脱敏完整诊断结果
    async def check(self, route: ProviderRoute, credential: object) -> ProviderDoctorResult:
        del credential
        return ProviderDoctorResult(
            status="ok",
            category="ok",
            route_id=route.id,
            message="all required checks passed",
            credential_source="env",
            readiness="verified",
            route_digest=route.validation_digest(),
            checked_at="2026-08-24T00:00:00+00:00",
            basic=ProviderDoctorCheck(status="passed", message="bounded request passed"),
            capabilities={
                "streaming": ProviderDoctorCheck(status="passed", message="stream passed"),
                "termination": ProviderDoctorCheck(status="passed", message="terminal passed"),
                "tool_calling": ProviderDoctorCheck(status="passed", message="tool passed"),
                "parallel_tools": ProviderDoctorCheck(status="passed", message="parallel passed"),
                "images": ProviderDoctorCheck(
                    status="passed" if route.supports_images else "unsupported",
                    message="image capability",
                ),
            },
        )


class _CredentialCapturingDoctor(_SuccessfulDoctor):
    # 初始化 Doctor 入参捕获列表
    def __init__(self) -> None:
        self.values: list[str | None] = []

    # 记录 Doctor 收到的凭据正文后返回固定脱敏结果
    async def check(
        self,
        route: ProviderRoute,
        credential: CredentialResolution,
    ) -> ProviderDoctorResult:
        self.values.append(credential.value)
        return await super().check(route, credential)


# 功能：验证首次 Provider CLI 写入前已冻结不含本次新增 route 的升级快照
# 设计：先直接布置旧 route，再走真实 add 命令并读取 marker 指向的备份文档比较 ID 集合
def test_provider_first_write_creates_pre_mutation_backup(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_MemoryKeyring(),
    )
    existing = get_route_preset("ollama").model_copy(update={"id": "existing"})
    routes.add(existing, activate=True)

    cmd_provider_add(
        "new-local",
        preset="ollama",
        provider=None,
        wire_format=None,
        base_url=None,
        model="qwen-new",
        credential_ref=None,
        set_key=False,
        activate=False,
        route_store=routes,
        credential_store=credentials,
    )

    marker = json.loads(
        (tmp_path / "migrations" / "provider-catalog-v1.json").read_text(encoding="utf-8")
    )
    backup_routes = json.loads(
        (Path(marker["backup_dir"]) / "routes.json").read_text(encoding="utf-8")
    )
    assert {route["id"] for route in backup_routes["routes"]} == {"existing"}
    assert {route.id for route in routes.list()} == {"existing", "new-local"}


# 功能：验证自定义兼容端点可以覆盖 preset 的默认本地地址与协议字段
# 设计：从 openai-compatible 模板新增路由后读取持久记录，防止 CLI 悄悄拿 Ollama 地址做 Doctor
def test_provider_preset_accepts_explicit_endpoint_overrides(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_MemoryKeyring(),
    )

    cmd_provider_add(
        "dashscope",
        preset="openai-compatible",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url="https://dashscope.example/v1/chat/completions",
        model="qwen3.8-flash",
        credential_ref="env:DASHSCOPE_API_KEY",
        set_key=False,
        activate=True,
        route_store=routes,
        credential_store=credentials,
    )

    route = routes.get("dashscope")
    assert route is not None
    assert str(route.base_url) == "https://dashscope.example/v1/chat/completions"
    assert route.model == "qwen3.8-flash"


# 功能：验证 Provider CLI 遇到用户状态锁竞争时不会绕过备份直接写 route
# 设计：持有生产同名 OS 文件锁后调用 add，断言明确异常、route 缺失且迁移 marker 未产生
def test_provider_write_lock_conflict_fails_closed(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_MemoryKeyring(),
    )
    lock = DaemonLock(tmp_path / "state-mutation.lock")
    lock.acquire()
    try:
        with pytest.raises(UpgradeStateLockError, match="state mutation"):
            cmd_provider_add(
                "blocked",
                preset="ollama",
                provider=None,
                wire_format=None,
                base_url=None,
                model="qwen-blocked",
                credential_ref=None,
                set_key=False,
                activate=False,
                route_store=routes,
                credential_store=credentials,
            )
    finally:
        lock.release()

    assert routes.list() == ()
    assert not (tmp_path / "migrations" / "provider-catalog-v1.json").exists()


# 功能：验证 provider add/edit/use/list/model/remove 使用同一 RouteStore 完成完整生命周期
# 设计：使用 preset 和临时 JSON 存储，逐步断言活动路由、模型更新及可观察输出
def test_provider_command_lifecycle(tmp_path: Path, capsys: object) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(tmp_path / "credentials.json", backend=_MemoryKeyring())
    config = CodeRookConfig()

    cmd_provider_add(
        "work",
        preset="openai",
        provider=None,
        wire_format=None,
        base_url=None,
        model="gpt-work",
        credential_ref="env:TEST_OPENAI_KEY",
        set_key=False,
        activate=True,
        route_store=routes,
        credential_store=credentials,
    )
    cmd_provider_edit(
        "work",
        provider=None,
        wire_format="openai_responses",
        base_url="https://api.openai.com/v1/responses",
        model="gpt-work-2",
        credential_ref=None,
        set_key=False,
        activate=False,
        route_store=routes,
        credential_store=credentials,
    )
    cmd_provider_use(
        "work",
        route_store=routes,
        credential_store=credentials,
        doctor=_SuccessfulDoctor(),  # type: ignore[arg-type]
    )
    cmd_provider_list(config, route_store=routes, credential_store=credentials)
    cmd_model_list(config, route_store=routes)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "* work" in output
    assert "gpt-work-2" in output
    assert "openai_responses" in output
    assert routes.active() == routes.get("work")

    cmd_provider_remove(
        "work",
        delete_credential=False,
        route_store=routes,
        credential_store=credentials,
    )
    assert routes.list() == ()


# 功能：验证不同 route 通过隐藏输入保存的密钥互相隔离且不会出现在命令输出
# 设计：注入内存 keyring 和两个固定 secret，轮换其中一个后核对另一个值保持不变
def test_provider_keys_are_isolated_and_redacted(tmp_path: Path, capsys: object) -> None:
    backend = _MemoryKeyring()
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(tmp_path / "credentials.json", backend=backend)

    for route_id, preset, secret in (
        ("one", "openai", "secret-one"),
        ("two", "anthropic", "secret-two"),
    ):
        cmd_provider_add(
            route_id,
            preset=preset,
            provider=None,
            wire_format=None,
            base_url=None,
            model=None,
            credential_ref=None,
            set_key=True,
            activate=False,
            route_store=routes,
            credential_store=credentials,
            secret_fn=lambda _prompt, value=secret: value,
        )

    cmd_provider_edit(
        "one",
        provider=None,
        wire_format=None,
        base_url=None,
        model=None,
        credential_ref=None,
        set_key=True,
        activate=False,
        route_store=routes,
        credential_store=credentials,
        secret_fn=lambda _prompt: "secret-one-new",
    )
    cmd_provider_list(
        CodeRookConfig(),
        route_store=routes,
        credential_store=credentials,
    )

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "secret-one" not in output
    assert "secret-two" not in output
    assert credentials.resolve(routes.get("one").credential_ref).value == "secret-one-new"
    assert credentials.resolve(routes.get("two").credential_ref).value == "secret-two"


# 功能：验证 CLI 从统一 catalog 新增 Ollama route 时不要求 --set-key 或伪造 credential ref
# 设计：使用临时 RouteStore 跳过在线 Doctor，检查持久 route 保留免密和 catalog 来源元数据
def test_provider_adds_local_catalog_route_without_key(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(tmp_path / "credentials.json", backend=_MemoryKeyring())

    cmd_provider_add(
        "local",
        preset="ollama",
        provider=None,
        wire_format=None,
        base_url=None,
        model="qwen3-coder",
        credential_ref=None,
        set_key=False,
        activate=True,
        route_store=routes,
        credential_store=credentials,
    )

    route = routes.get("local")
    assert route.catalog_id == "ollama"
    assert route.credential_required is False
    assert route.credential_ref == "none:ollama"
    assert credentials.resolve(route.credential_ref).source == "missing"


# 功能：验证 CLI 在 fresh install 使用共享 readiness 报告未配置而非伪造 legacy 默认 route
# 设计：注入空 route 与 credential 存储并检查输出，锁定配置服务和 CLI 的同一事实来源
def test_provider_list_reports_unconfigured_fresh_install(
    tmp_path: Path,
    capsys: object,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(tmp_path / "credentials.json", backend=_MemoryKeyring())

    cmd_provider_list(
        CodeRookConfig(),
        route_store=routes,
        credential_store=credentials,
    )

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "status=unconfigured" in output
    assert "legacy-anthropic" not in output


# 功能：验证接收 CodeRookConfig 的 provider list 使用显式 env overlay 计算 readiness
# 设计：持久 route 引用 env key，但只在隐藏配置字段提供值，断言 CLI 显示 env 来源且不泄密
def test_provider_list_consumes_explicit_env_overlay(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)  # type: ignore[attr-defined]
    config = CodeRookConfig(
        llm=LlmConfig(credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"})
    )
    routes = RouteStore(tmp_path / "routes.json")
    route = get_route_preset("openai").model_copy(
        update={"id": "deployment", "credential_ref": "env:DEPLOYMENT_LLM_KEY"}
    )
    routes.add(route, activate=True)

    cmd_provider_list(config, route_store=routes)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "credential=env" in output
    assert "readiness=provider_unverified" in output
    assert "explicit-file-secret" not in output


# 功能：验证 provider add、edit 和 use 的 Doctor 都消费同一显式 env overlay
# 设计：三次真实配置事务共用一个捕获 Doctor，断言每次仅收到内存密钥且 CLI 输出不泄漏
def test_provider_mutations_consume_explicit_env_overlay(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)  # type: ignore[attr-defined]
    config = CodeRookConfig(
        llm=LlmConfig(credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"})
    )
    routes = RouteStore(tmp_path / "routes.json")
    doctor = _CredentialCapturingDoctor()

    cmd_provider_add(
        "deployment",
        preset="openai",
        provider=None,
        wire_format=None,
        base_url=None,
        model="gpt-overlay",
        credential_ref="env:DEPLOYMENT_LLM_KEY",
        set_key=False,
        activate=False,
        route_store=routes,
        validate=True,
        doctor=doctor,  # type: ignore[arg-type]
        config=config,
    )
    cmd_provider_edit(
        "deployment",
        provider=None,
        wire_format=None,
        base_url=None,
        model="gpt-overlay-2",
        credential_ref=None,
        set_key=False,
        activate=False,
        route_store=routes,
        validate=True,
        doctor=doctor,  # type: ignore[arg-type]
        config=config,
    )
    cmd_provider_use(
        "deployment",
        route_store=routes,
        doctor=doctor,  # type: ignore[arg-type]
        config=config,
    )

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert doctor.values == ["explicit-file-secret"] * 3
    assert "explicit-file-secret" not in output


# 功能：验证 CLI doctor 把显式 env overlay 解析出的凭据传给真实 Provider 检查入口
# 设计：捕获 Doctor 入参但返回固定脱敏结果，覆盖 diagnose_route 自建 CredentialStore 的默认路径
def test_doctor_consumes_explicit_env_overlay(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)  # type: ignore[attr-defined]
    config = CodeRookConfig(
        llm=LlmConfig(credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"})
    )
    routes = RouteStore(tmp_path / "routes.json")
    route = get_route_preset("openai").model_copy(
        update={"id": "deployment", "credential_ref": "env:DEPLOYMENT_LLM_KEY"}
    )
    routes.add(route, activate=True)
    doctor = _CredentialCapturingDoctor()

    result = asyncio.run(
        doctor_command.diagnose_route(
            config,
            route_store=routes,
            doctor=doctor,  # type: ignore[arg-type]
        )
    )

    assert result.status == "ok"
    assert doctor.values == ["explicit-file-secret"]
    assert "explicit-file-secret" not in result.model_dump_json()


# 功能：验证 doctor JSON 只输出分类、状态和凭据来源，不泄露任何密钥正文
# 设计：替换网络诊断为固定脱敏结果，再解析实际 CLI JSON 输出检查字段集合和敏感串
def test_doctor_json_is_redacted(monkeypatch: object, capsys: object) -> None:
    # 返回与真实 doctor 相同的脱敏结果模型
    async def fake_diagnose(
        _config: CodeRookConfig,
        _route_id: str | None,
    ) -> ProviderDoctorResult:
        return ProviderDoctorResult(
            status="error",
            category="credential",
            route_id="work",
            message="credential was rejected by the provider",
            credential_source="keyring",
            http_status=401,
        )

    monkeypatch.setattr(doctor_command, "diagnose_route", fake_diagnose)  # type: ignore[attr-defined]
    exit_code = doctor_command.cmd_doctor(CodeRookConfig(), "work", as_json=True)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    payload = json.loads(output)
    assert payload["credential_source"] == "keyring"
    assert payload["category"] == "credential"
    assert "secret" not in output.casefold()
    assert exit_code == 1


# 功能：验证 argparse 的 provider list 路径分发到新的 route 管理命令
# 设计：替换配置、迁移和命令实现后调用真实 main，覆盖解析器与 dispatch 的连接点
def test_main_dispatches_provider_list(monkeypatch: object) -> None:
    calls: list[CodeRookConfig] = []
    config = CodeRookConfig()
    monkeypatch.setattr(sys, "argv", ["coderook", "provider", "list"])  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_main_module, "migrate_legacy_state", lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_main_module, "get_config", lambda: config)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_main_module, "setup_logging", lambda _config: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        cli_main_module,
        "cmd_provider_list",
        lambda current: calls.append(current),
    )  # type: ignore[attr-defined]

    cli_main_module.main()

    assert calls == [config]


# 功能：验证 provider test 将诊断失败状态传播为进程非零退出码
# 设计：替换网络诊断并调用真实 argparse 分发，防止 JSON 错误结果被自动化误判为成功
def test_main_propagates_provider_test_failure(monkeypatch: object) -> None:
    calls: list[tuple[str | None, bool]] = []
    config = CodeRookConfig()
    monkeypatch.setattr(  # type: ignore[attr-defined]
        sys,
        "argv",
        ["coderook", "provider", "test", "work", "--json"],
    )
    monkeypatch.setattr(cli_main_module, "migrate_legacy_state", lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_main_module, "get_config", lambda: config)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_main_module, "setup_logging", lambda _config: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_main_module,
        "cmd_doctor",
        lambda _config, route_id, *, as_json: calls.append((route_id, as_json)) or 1,
    )

    exit_code = cli_main_module.main()

    assert exit_code == 1
    assert calls == [("work", True)]
