from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm.credentials import (
    CredentialStore,
    CredentialStoreError,
    inspect_credential_store,
    llm_is_configured,
    resolve_api_key,
    save_api_key,
)


class _MemoryBackend:
    # 初始化内存 keyring 与可选写入故障
    def __init__(self, *, fail_write: bool = False) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_write = fail_write

    # 从内存 keyring 读取凭据
    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    # 向内存 keyring 保存凭据或模拟后端不可用
    def set_password(self, service: str, account: str, password: str) -> None:
        if self.fail_write:
            raise RuntimeError("backend unavailable")
        self.values[(service, account)] = password

    # 从内存 keyring 删除凭据
    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


# 功能：验证 Anthropic 与 OpenAI-compatible 的 API key 在同一凭据文件中独立保存
# 设计：依次写入两个 provider 后读取 JSON，并分别解析配置，防止切换 provider 时覆盖另一份密钥
def test_credentials_keep_provider_keys_independent(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("anthropic", "ant-key", path)
    save_api_key("openai_compatible", "openai-key", path)

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["api_keys"] == {
        "anthropic": "ant-key",
        "openai_compatible": "openai-key",
    }
    assert resolve_api_key(LlmConfig(provider="anthropic"), path) == "ant-key"
    assert (
        resolve_api_key(
            LlmConfig(provider="openai_compatible", api_key_env="OPENAI_API_KEY"),
            path,
        )
        == "openai-key"
    )
    assert inspect_credential_store(path) == "ready"


# 功能：验证不存在的凭据文档在只读健康检查中明确区分为 missing
# 设计：对未创建路径直接检查并确认无目录或文件副作用，锁定 Doctor 的新安装语义
def test_missing_credential_store_status_has_no_side_effect(tmp_path: Path) -> None:
    path = tmp_path / "state" / "credentials.json"

    status = inspect_credential_store(path)

    assert status == "missing"
    assert not path.parent.exists()


# 功能：验证显式环境变量中的 API key 优先于用户凭据文件
# 设计：给同一 provider 同时设置两种来源并断言环境值胜出，保持既有部署配置的最高优先级
def test_environment_api_key_overrides_saved_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("anthropic", "saved-key", path)
    monkeypatch.setenv("CUSTOM_ANTHROPIC_KEY", "environment-key")
    config = LlmConfig(provider="anthropic", api_key_env="CUSTOM_ANTHROPIC_KEY")

    assert resolve_api_key(config, path) == "environment-key"


# 功能：验证显式 env overlay 可供旧 Provider 判定使用且真实进程环境保持最高优先级
# 设计：同一变量依次由 overlay 与进程环境提供，断言解析与 configured 判定均消费同一无全局污染链路
def test_explicit_env_overlay_resolves_with_process_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LlmConfig(
        provider="deepseek",
        default_model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/chat/completions",
        api_key_env="DEPLOYMENT_LLM_KEY",
        credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"},
    )
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)

    assert resolve_api_key(config, tmp_path / "missing.json") == "explicit-file-secret"
    assert llm_is_configured(config, tmp_path / "missing.json") is True

    monkeypatch.setenv("DEPLOYMENT_LLM_KEY", "process-secret")

    assert resolve_api_key(config, tmp_path / "missing.json") == "process-secret"
    assert "explicit-file-secret" not in repr(config)
    assert "process-secret" not in repr(config)

    monkeypatch.setenv("DEPLOYMENT_LLM_KEY", "")

    assert resolve_api_key(config, tmp_path / "missing.json") is None


# 功能：验证 OpenAI-compatible 缺少 endpoint 时即使有 key 也仍被判定为未配置
# 设计：先保存有效 key，再分别检查空地址和完整地址，覆盖首次启动引导的端点判定边界
def test_openai_configuration_requires_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("openai_compatible", "saved-key", path)
    incomplete = LlmConfig(
        provider="openai_compatible",
        default_model="model",
        base_url="",
        api_key_env="OPENAI_API_KEY",
    )
    complete = LlmConfig(
        provider="openai_compatible",
        default_model="model",
        base_url="https://example.test/v1/chat/completions",
        api_key_env="OPENAI_API_KEY",
    )

    assert llm_is_configured(incomplete, path) is False
    assert llm_is_configured(complete, path) is True


# 功能：验证七种云端 Provider 都能使用各自独立凭据完成旧配置兼容判定
# 设计：逐个保存不同 Key 并使用 catalog endpoint，覆盖新增 Provider 标识与凭据隔离
@pytest.mark.parametrize(
    ("provider", "api_key_env", "base_url"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions"),
        ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        ("anthropic", "ANTHROPIC_API_KEY", ""),
        (
            "siliconflow",
            "SILICONFLOW_API_KEY",
            "https://api.siliconflow.cn/v1/chat/completions",
        ),
        (
            "gemini",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        ),
        (
            "moonshot",
            "MOONSHOT_API_KEY",
            "https://api.moonshot.ai/v1/chat/completions",
        ),
        (
            "openrouter",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/api/v1/chat/completions",
        ),
    ],
)
def test_builtin_provider_configuration_is_supported(
    tmp_path: Path,
    provider: str,
    api_key_env: str,
    base_url: str,
) -> None:
    path = tmp_path / "credentials.json"
    save_api_key(provider, f"{provider}-key", path)
    config = LlmConfig(
        provider=provider,
        default_model="model",
        base_url=base_url,
        api_key_env=api_key_env,
    )

    assert llm_is_configured(config, path) is True


# 功能：验证 Ollama 与 LM Studio 旧配置兼容入口不要求伪造或保存 API key
# 设计：使用不存在的凭据文件和本地 endpoint 参数化检查，锁定 catalog 的免密语义
@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("ollama", "http://127.0.0.1:11434/v1/chat/completions"),
        ("lm_studio", "http://127.0.0.1:1234/v1/chat/completions"),
    ],
)
def test_local_provider_configuration_does_not_require_key(
    tmp_path: Path,
    provider: str,
    base_url: str,
) -> None:
    config = LlmConfig(
        provider=provider,
        default_model="local-model",
        base_url=base_url,
        api_key_env="",
    )

    assert llm_is_configured(config, tmp_path / "missing.json") is True


# 功能：验证 route 凭据优先写入 OS keyring 并以引用解析
# 设计：注入内存后端，断言用户文件未创建且解析结果只报告 keyring 来源
def test_route_credential_prefers_keyring(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend())

    reference = store.save("route-a", "secret-a")
    resolved = store.resolve(reference)

    assert reference == "keyring:route-a"
    assert resolved.value == "secret-a"
    assert resolved.source == "keyring"
    assert not path.exists()


# 功能：验证 keyring 不可用时自动回退到权限收紧文件
# 设计：让后端写入抛错，检查 file ref、V2 文档和重新实例化后的解析结果
def test_route_credential_falls_back_to_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend(fail_write=True))

    reference = store.save("route-a", "secret-a")
    restored = CredentialStore(path, backend=_MemoryBackend()).resolve(reference)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert reference == "file:route-a"
    assert restored.value == "secret-a"
    assert restored.source == "file"
    assert payload["route_credentials"] == {"route-a": "secret-a"}


# 功能：验证 keyring 回退日志不会转储可能回显 API Key 的后端异常正文
# 设计：让后端异常故意携带密钥并捕获日志，仅允许异常类型与公开 route ID 出现
def test_keyring_fallback_log_redacts_backend_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _LeakyBackend(_MemoryBackend):
        # 模拟不可信 keyring 后端把密码放进异常正文
        def set_password(self, service: str, account: str, password: str) -> None:
            del service, account
            raise RuntimeError(password)

    path = tmp_path / "credentials.json"
    with caplog.at_level(logging.WARNING):
        reference = CredentialStore(path, backend=_LeakyBackend()).save(
            "route-a",
            "private-api-key",
        )

    assert reference == "file:route-a"
    assert "private-api-key" not in caplog.text
    assert "RuntimeError" in caplog.text


# 功能：验证两个 route 的凭据保存、切换解析和删除互不覆盖
# 设计：强制文件后端保存两项，只删除第一项后确认第二项仍能解析
def test_route_credentials_are_isolated_by_route(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend(fail_write=True))
    first_ref = store.save("first", "first-secret")
    second_ref = store.save("second", "second-secret")

    store.delete(first_ref)

    assert store.resolve(first_ref).source == "missing"
    assert store.resolve(second_ref).value == "second-secret"


# 功能：验证 env credential ref 仅报告 presence/source 且缺失时安全降级
# 设计：设置一次环境变量后删除，比较两次解析而不经过任何凭据文件
def test_route_credential_resolves_environment_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialStore(tmp_path / "credentials.json", backend=_MemoryBackend())
    monkeypatch.setenv("ROUTE_TEST_KEY", "environment-secret")

    present = store.resolve("env:ROUTE_TEST_KEY")
    monkeypatch.delenv("ROUTE_TEST_KEY")
    missing = store.resolve("env:ROUTE_TEST_KEY")

    assert present.value == "environment-secret" and present.source == "env"
    assert missing.value is None and missing.source == "missing"


# 功能：验证 route env 引用消费显式文件 overlay 且进程值覆盖它
# 设计：用同一 CredentialStore 先在无全局变量时解析 overlay，再设置进程变量比较来源和值
def test_route_credential_uses_overlay_below_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialStore(
        tmp_path / "credentials.json",
        backend=_MemoryBackend(),
        env_overlay={"ROUTE_TEST_KEY": "explicit-file-secret"},
    )
    monkeypatch.delenv("ROUTE_TEST_KEY", raising=False)

    from_file = store.resolve("env:ROUTE_TEST_KEY")
    monkeypatch.setenv("ROUTE_TEST_KEY", "process-secret")
    from_process = store.resolve("env:ROUTE_TEST_KEY")

    assert from_file.value == "explicit-file-secret" and from_file.source == "env"
    assert from_process.value == "process-secret" and from_process.source == "env"
    assert "explicit-file-secret" not in repr(store)


# 功能：验证未来版本凭据文件不会被当前客户端降级覆盖并丢失未知数据
# 设计：写入 version 99 与额外字段后尝试保存，断言失败关闭且原始字节完全不变
def test_future_credentials_version_is_preserved_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    original = (
        '{"version":99,"api_keys":{},"route_credentials":{},'
        '"future_encryption":{"kind":"v3"}}\n'
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(CredentialStoreError, match="newer than supported") as caught:
        save_api_key("anthropic", "must-not-write", path)

    assert caught.value.code == "unsupported_version"
    assert path.read_text(encoding="utf-8") == original
    assert inspect_credential_store(path) == "invalid"


# 功能：验证当前版本出现未知顶层字段时拒绝写回而不是静默删除扩展数据
# 设计：保持 version 2 仅增加一个字段，覆盖未来生产者未升版本时的保守兼容路径
def test_unknown_credentials_fields_block_lossy_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    original = (
        '{"version":2,"api_keys":{},"route_credentials":{},'
        '"encryption_metadata":{"key_id":"future"}}\n'
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(CredentialStoreError, match="unknown fields") as caught:
        CredentialStore(path, backend=_MemoryBackend(fail_write=True)).save(
            "route-a",
            "must-not-write",
        )

    assert caught.value.code == "invalid_format"
    assert path.read_text(encoding="utf-8") == original
    assert inspect_credential_store(path) == "invalid"


# 功能：验证语法损坏的凭据文档不会在文件后端回退时被空结构覆盖
# 设计：保留包含密钥片段的原始坏字节并强制 keyring 失败，断言 typed 错误和逐字节不变
def test_corrupt_credential_document_is_preserved_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    original = b'{"api_keys":{"private":"never-print"}'
    path.write_bytes(original)

    with pytest.raises(CredentialStoreError, match="invalid JSON") as caught:
        CredentialStore(path, backend=_MemoryBackend(fail_write=True)).save(
            "route-a",
            "must-not-write",
        )

    assert caught.value.code == "invalid_json"
    assert "never-print" not in str(caught.value)
    assert path.read_bytes() == original


# 功能：验证无 version 的旧凭据文档可在首次保存时迁移到 v2 且保留已有密钥
# 设计：构造最早期仅 api_keys 格式，再添加独立 provider 并检查两项都存在
def test_legacy_credentials_document_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text('{"api_keys":{"anthropic":"legacy-key"}}\n', encoding="utf-8")

    save_api_key("openai", "new-key", path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 2
    assert payload["api_keys"] == {
        "anthropic": "legacy-key",
        "openai": "new-key",
    }


# 功能：验证凭据文件符号链接不能把读取或写入重定向到用户选择路径之外
# 设计：链接到含哨兵密钥的外部文件后分别解析和保存，断言 typed 拒绝且外部字节不变
def test_credential_file_symlink_is_rejected_without_external_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    original = '{"version":2,"api_keys":{"anthropic":"sentinel"},"route_credentials":{}}\n'
    outside.write_text(original, encoding="utf-8")
    linked = tmp_path / "credentials.json"
    try:
        os.symlink(outside, linked)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    with pytest.raises(CredentialStoreError, match="regular file") as read_error:
        resolve_api_key(LlmConfig(provider="anthropic"), linked)
    with pytest.raises(CredentialStoreError, match="regular file") as write_error:
        save_api_key("anthropic", "must-not-write", linked)

    assert read_error.value.code == "unsafe_path"
    assert write_error.value.code == "unsafe_path"
    assert inspect_credential_store(linked) == "invalid"
    assert outside.read_text(encoding="utf-8") == original


# 功能：验证凭据父目录符号链接不能把新文档创建到外部目录
# 设计：把预期父目录链接到带哨兵的外部目录后保存，断言失败关闭且外部目录没有凭据文件
def test_credential_parent_symlink_is_rejected_without_external_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("preserve", encoding="utf-8")
    linked_parent = tmp_path / "state"
    try:
        os.symlink(outside, linked_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    target = linked_parent / "credentials.json"

    with pytest.raises(CredentialStoreError, match="parent directory") as caught:
        save_api_key("anthropic", "must-not-write", target)

    assert caught.value.code == "unsafe_path"
    assert inspect_credential_store(target) == "invalid"
    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "preserve"
    assert not (outside / "credentials.json").exists()
