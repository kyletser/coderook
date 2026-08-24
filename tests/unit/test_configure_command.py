from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.cli.commands import configure as configure_module
from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.core.configuration import ConfigurationService, ConfigurationValidationError
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorCheck, ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import ProviderRoute, get_route_preset


# 功能：验证写入 llm 配置时会替换旧表且保留前后其他 TOML 小节
# 设计：构造 llm 位于两个小节之间的文件，写入后用 tomllib 语义由 get_config 的底层格式保证并检查文本唯一性
def test_write_llm_config_preserves_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[core]\nport = 7440\n\n[llm]\nprovider = "anthropic"\n\n[logging]\nlevel = "DEBUG"\n',
        encoding="utf-8",
    )
    config = LlmConfig(
        provider="openai_compatible",
        default_model="deepseek-test",
        base_url="https://example.test/v1/chat/completions",
        api_key_env="OPENAI_API_KEY",
    )

    configure_module.write_llm_config(config, path, state_root=tmp_path)
    content = path.read_text(encoding="utf-8")

    assert content.count("[llm]") == 1
    assert 'provider = "openai_compatible"' in content
    assert "[core]\nport = 7440" in content
    assert '[logging]\nlevel = "DEBUG"' in content


# 功能：验证配置命令默认只选择用户级 TOML，不因仓库已有 .coderook/config.toml 而写入安全字段
# 设计：在临时工作区预建项目配置并清除显式覆盖，直接检查写入路径仍为用户级默认值
def test_config_write_path_ignores_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEROOK_CONFIG", raising=False)
    project_config = tmp_path / ".coderook" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text("[agent]\nmax_steps = 10\n", encoding="utf-8")

    assert configure_module.config_write_path() == configure_module._DEFAULT_CONFIG_PATH


# 功能：验证进程 CODEROOK_CONFIG 也不能让配置命令把 Provider 安全字段写回仓库配置
# 设计：把显式路径精确指向项目标准文件并断言写入路径选择阶段即失败，不触碰原文件
def test_config_write_path_rejects_explicit_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project_config = tmp_path / ".coderook" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text("[agent]\nmax_steps = 10\n", encoding="utf-8")
    monkeypatch.setenv("CODEROOK_CONFIG", str(project_config))

    with pytest.raises(SystemExit, match="cannot store provider route settings"):
        configure_module.config_write_path()

    assert project_config.read_text(encoding="utf-8") == "[agent]\nmax_steps = 10\n"


# 功能：验证交互配置保存凭据与用户 TOML 时完全不读取或改写仓库 .env
# 设计：预置含明文 key 的 .env 并比较调用前后原始字节，同时检查仅当前进程收到非敏感路由字段
def test_configure_never_rewrites_project_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dotenv_path = tmp_path / ".env"
    original_dotenv = (
        "CODEROOK_LLM_PROVIDER=openai_compatible\n"
        "CODEROOK_LLM_API_KEY_ENV=CODEROOK_LLM_API_KEY\n"
        "CODEROOK_LLM_API_KEY=old-secret\n"
    )
    dotenv_path.write_text(original_dotenv, encoding="utf-8")
    monkeypatch.setenv("CODEROOK_LLM_API_KEY", "old-secret")
    inputs = iter(["2", "new-model", "https://example.test/v1/chat/completions"])
    current = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="",
            base_url="",
            api_key_env="CODEROOK_LLM_API_KEY",
        )
    )
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="new-model",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CODEROOK_LLM_API_KEY",
        )
    )
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    result = configure_module.configure_llm(
        current,
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "",
        config_path=config_path,
        credential_file=credential_path,
        state_root=tmp_path,
    )
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))

    assert result is expected
    assert dotenv_path.read_text(encoding="utf-8") == original_dotenv
    assert configure_module.os.environ["CODEROOK_LLM_DEFAULT_MODEL"] == "new-model"
    assert configure_module.os.environ["CODEROOK_LLM_BASE_URL"] == (
        "https://example.test/v1/chat/completions"
    )
    assert credentials["api_keys"]["openai_compatible"] == "old-secret"
    assert "old-secret" not in config_path.read_text(encoding="utf-8")


# 功能：验证 Anthropic-compatible 向导允许官方接口留空并接受自定义模型和新 key
# 设计：模拟选择 Anthropic、输入模型、留空 endpoint 与隐藏 key，检查 TOML 和凭据文件职责分离
def test_configure_anthropic_official_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    inputs = iter(["1", "claude-custom", ""])
    expected = CodeRookConfig()
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    configure_module.configure_llm(
        CodeRookConfig(),
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "anthropic-secret",
        config_path=config_path,
        credential_file=credential_path,
        state_root=tmp_path,
    )

    assert 'provider = "anthropic"' in config_path.read_text(encoding="utf-8")
    assert 'base_url = ""' in config_path.read_text(encoding="utf-8")
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    assert credentials["api_keys"]["anthropic"] == "anthropic-secret"


# 功能：验证模型切换仅更新默认模型并保留当前 Provider、endpoint 和凭据引用
# 设计：使用自定义 OpenAI-compatible 配置和临时 TOML，比较保存文本与返回配置对象
def test_switch_llm_model_preserves_provider_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="old-model",
            router="static",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CUSTOM_API_KEY",
        )
    )
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="new-model",
            router="static",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CUSTOM_API_KEY",
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    path = tmp_path / "config.toml"

    result = configure_module.switch_llm_model(
        current,
        " new-model ",
        config_path=path,
        state_root=tmp_path,
    )
    content = path.read_text(encoding="utf-8")

    assert result is expected
    assert 'default_model = "new-model"' in content
    assert 'provider = "openai_compatible"' in content
    assert 'base_url = "https://example.test/v1/chat/completions"' in content
    assert 'api_key_env = "CUSTOM_API_KEY"' in content


# 功能：验证内置 Provider 配置会保存固定 endpoint、模型和独立凭据
# 设计：选择 DeepSeek 并使用临时 TOML/凭据文件，排除用户 .env 和真实密钥影响
def test_save_provider_config_uses_builtin_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="deepseek",
            default_model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    result = configure_module.save_provider_config(
        CodeRookConfig(),
        "deepseek",
        "deepseek-secret",
        "deepseek-v4-pro",
        config_path=config_path,
        credential_file=credential_path,
        state_root=tmp_path,
    )
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    content = config_path.read_text(encoding="utf-8")

    assert result is expected
    assert credentials["api_keys"]["deepseek"] == "deepseek-secret"
    assert 'provider = "deepseek"' in content
    assert 'default_model = "deepseek-v4-pro"' in content
    assert 'base_url = "https://api.deepseek.com/chat/completions"' in content
    assert "deepseek-secret" not in content


# 为统一配置向导返回与候选 route 摘要绑定的可控 Doctor 结果
class _CatalogDoctor:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds

    # 根据 route 声明生成全部必需 capability 状态，避免测试访问真实 Provider
    async def check(
        self,
        route: ProviderRoute,
        credential: object,
    ) -> ProviderDoctorResult:
        del credential
        required = {"streaming", "termination"}
        if route.supports_tools:
            required.add("tool_calling")
        if route.supports_parallel_tools:
            required.add("parallel_tools")
        if route.supports_images:
            required.add("images")
        status = "passed" if self.succeeds else "failed"
        return ProviderDoctorResult(
            status="ok" if self.succeeds else "error",
            category="ok" if self.succeeds else "network",
            route_id=route.id,
            message="verified" if self.succeeds else "unreachable",
            credential_source="file",
            readiness="verified" if self.succeeds else "failed",
            route_digest=route.validation_digest(),
            checked_at="2026-08-24T00:00:00+00:00",
            basic=ProviderDoctorCheck(status=status, message="basic"),
            capabilities={
                name: ProviderDoctorCheck(status=status, message=name) for name in required
            },
        )


# 功能：验证 coderook configure 使用统一 Catalog，Doctor 通过后才激活 route
# 设计：选择免密 Ollama 并注入成功 Doctor，避免真实网络和系统凭据依赖
def test_cmd_configure_validates_catalog_route_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    configuration = ConfigurationService(
        routes,
        CredentialStore(tmp_path / "credentials.json"),
    )
    inputs = iter(["8", "qwen-test"])
    monkeypatch.setattr(configure_module.sys.stdin, "isatty", lambda: True)

    configure_module.cmd_configure(
        CodeRookConfig(),
        input_fn=lambda _prompt: next(inputs),
        configuration=configuration,
        doctor=_CatalogDoctor(),
    )

    active = routes.active()
    assert active is not None
    assert active.id == "ollama"
    assert active.model == "qwen-test"
    assert active.has_current_doctor_receipt()
    marker = tmp_path / "migrations" / "provider-catalog-v1.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    assert "routes.json" not in manifest["entries"]
    assert (tmp_path / "routes.json").is_file()


# 功能：验证 Provider Doctor 失败时统一配置向导不写 route、活动项或密钥
# 设计：提交远端 DeepSeek 候选和内存 key，再让 Doctor 返回网络失败并检查两个存储边界
def test_cmd_configure_doctor_failure_does_not_submit_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credential_path = tmp_path / "credentials.json"
    configuration = ConfigurationService(routes, CredentialStore(credential_path))
    inputs = iter(["1", "deepseek-test"])
    monkeypatch.setattr(configure_module.sys.stdin, "isatty", lambda: True)

    with pytest.raises(ConfigurationValidationError, match="network"):
        configure_module.cmd_configure(
            CodeRookConfig(),
            input_fn=lambda _prompt: next(inputs),
            secret_fn=lambda _prompt: "candidate-secret",
            configuration=configuration,
            doctor=_CatalogDoctor(succeeds=False),
        )

    assert routes.list() == ()
    assert routes.active() is None
    assert not credential_path.exists()


# 功能：验证 config-status 默认路径用显式 env overlay 计算统一 readiness
# 设计：把用户目录隔离到临时路径并持久化 env route，断言状态和凭据来源脱敏输出
def test_print_llm_status_consumes_explicit_env_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: object,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)
    routes = RouteStore()
    route = get_route_preset("openai").model_copy(
        update={"id": "deployment", "credential_ref": "env:DEPLOYMENT_LLM_KEY"}
    )
    routes.add(route, activate=True)
    config = CodeRookConfig(
        llm=LlmConfig(credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"})
    )

    configure_module.print_llm_status(config)

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "status:   provider_unverified" in output
    assert "credential: env" in output
    assert "explicit-file-secret" not in output
