from __future__ import annotations

import json
import sys
from pathlib import Path

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
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore


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
    cmd_provider_use("work", route_store=routes)
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
    doctor_command.cmd_doctor(CodeRookConfig(), "work", as_json=True)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    payload = json.loads(output)
    assert payload["credential_source"] == "keyring"
    assert payload["category"] == "credential"
    assert "secret" not in output.casefold()


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
