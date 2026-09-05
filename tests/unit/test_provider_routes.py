from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl, ValidationError

from code_rook.core.configuration.service import ConfigurationService
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute, get_route_preset, list_route_presets


# 构造可写入临时存储的安全本地路由
def _route(route_id: str, *, credential_ref: str | None = None) -> ProviderRoute:
    return ProviderRoute(
        id=route_id,
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("http://127.0.0.1:11434/v1/chat/completions"),
        model=f"model-{route_id}",
        credential_ref=credential_ref or f"file:{route_id}",
    )


# 功能：验证两个并发 RouteStore 读改写不会彼此覆盖已成功提交的路由
# 设计：暂停首个事务读取后启动第二写入，迫使旧实现出现确定性交错并断言两次调用均成功保留
def test_route_store_serializes_concurrent_read_modify_write(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    first = RouteStore(path)
    second = RouteStore(path)
    loaded = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original_load = first._load

    # 在首个写事务已经读取文档且仍持锁时等待第二个执行流进入
    def paused_load():
        document = original_load()
        loaded.set()
        if not release.wait(timeout=5):
            raise TimeoutError("concurrent route test did not release first writer")
        return document

    first._load = paused_load  # type: ignore[method-assign]

    # 捕获线程异常并保持测试主线程可给出稳定断言
    def add(store: RouteStore, route_id: str) -> None:
        try:
            store.add(_route(route_id), activate=True)
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=add, args=(first, "legacy-deepseek"))
    second_thread = threading.Thread(target=add, args=(second, "concurrent-user-route"))
    first_thread.start()
    assert loaded.wait(timeout=5)
    second_thread.start()
    release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert {route.id for route in RouteStore(path).list()} == {
        "legacy-deepseek",
        "concurrent-user-route",
    }


# 功能：验证指向不存在目标的 routes.json 符号链接不能被当成空 Catalog 后覆盖
# 设计：创建 broken symlink 后同时调用 inspect 和 add，断言两条路径都失败关闭且目标仍缺失
def test_route_store_refuses_broken_symlink_document(tmp_path: Path) -> None:
    target = tmp_path / "outside-missing.json"
    link = tmp_path / "routes.json"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")
    store = RouteStore(link)

    with pytest.raises(RouteStoreError, match="regular file"):
        store.inspect()
    with pytest.raises(RouteStoreError, match="regular file"):
        store.add(_route("must-not-write"))

    assert not target.exists()


# 功能：验证 ProviderRoute 明确拆分 provider、wire、model、endpoint 与 credential ref
# 设计：构造完整路由并生成收据，断言收据只保留 origin 且不含 URL 路径或凭据正文
def test_provider_route_produces_sensitive_free_receipt() -> None:
    route = ProviderRoute(
        id="custom",
        provider="anthropic-compatible",
        wire_format="anthropic_messages",
        base_url=AnyHttpUrl("https://gateway.example.test/custom/messages"),
        model="model-x",
        credential_ref="file:custom-secret",
    )

    receipt = route.receipt("file")

    assert receipt.route_id == "custom"
    assert receipt.wire_format == "anthropic_messages"
    assert receipt.base_url_origin == "https://gateway.example.test"
    serialized = receipt.model_dump_json()
    assert "/custom/messages" not in serialized
    assert "custom-secret" not in serialized


# 功能：验证非 loopback 明文 HTTP 和 URL 内嵌凭据在模型入口被拒绝
# 设计：分别构造公网 HTTP 与 userinfo URL，覆盖两类不能被调用方绕过的安全边界
@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/v1/chat/completions",
        "https://user:password@api.example.test/v1/chat/completions",
    ],
)
def test_provider_route_rejects_insecure_endpoints(url: str) -> None:
    with pytest.raises(ValidationError):
        ProviderRoute(
            id="unsafe",
            provider="openai-compatible",
            wire_format="openai_chat",
            base_url=AnyHttpUrl(url),
            model="model",
            credential_ref="env:API_KEY",
        )


# 功能：验证 localhost、IPv4 与 IPv6 loopback 可使用明文 HTTP
# 设计：参数化三种本机表示，避免安全校验误伤本地模型服务
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions",
        "http://[::1]:11434/v1/chat/completions",
    ],
)
def test_provider_route_allows_loopback_http(url: str) -> None:
    route = ProviderRoute(
        id="local",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl(url),
        model="local-model",
        credential_ref="file:local",
    )

    assert route.base_url.scheme == "http"


# 功能：验证 OpenAI-compatible 标准 /v1 base URL 会补全为 Chat Completions endpoint
# 设计：同时覆盖公网兼容网关和已是完整 endpoint 的幂等输入，避免破坏自定义路径
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://gateway.example.test/compatible-mode/v1",
            "https://gateway.example.test/compatible-mode/v1/chat/completions",
        ),
        (
            "https://gateway.example.test/v1/chat/completions",
            "https://gateway.example.test/v1/chat/completions",
        ),
    ],
)
def test_provider_route_normalizes_openai_base_url(url: str, expected: str) -> None:
    route = ProviderRoute(
        id="compatible",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl(url),
        model="model",
        credential_ref="file:compatible",
    )

    assert str(route.base_url).rstrip("/") == expected


# 功能：验证 route 温度进入脱敏 receipt，并拒绝 Anthropic 不支持的范围与 thinking 组合
# 设计：先检查合法 temperature=0 往返，再用两个非法配置锁定 provider 原生约束
def test_provider_route_validates_and_receipts_temperature() -> None:
    route = ProviderRoute(
        id="deterministic",
        provider="anthropic",
        wire_format="anthropic_messages",
        base_url=AnyHttpUrl("https://api.anthropic.com"),
        model="claude-test",
        credential_ref="env:ANTHROPIC_API_KEY",
        temperature=0.0,
    )

    assert route.receipt("env").temperature == 0.0
    with pytest.raises(ValidationError, match="between 0 and 1"):
        ProviderRoute.model_validate({**route.model_dump(mode="python"), "temperature": 1.5})
    with pytest.raises(ValidationError, match="requires temperature=1"):
        ProviderRoute.model_validate({**route.model_dump(mode="python"), "thinking": "high"})


# 功能：验证未来 routes.json 版本会在读取时明确阻断而不是被旧版保存路径覆盖
# 设计：写入最小 version=99 文档后调用只读 list，断言专用存储错误且原始字节保持不变
def test_future_route_document_blocks_unsupported_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    original = '{"version":99,"active_route_id":null,"routes":[]}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RouteStoreError, match="newer than supported"):
        RouteStore(path).list()

    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "_quarantine").exists()


# 功能：验证 routes.json 符号链接在读写两条路径都失败关闭且不修改外部目标
# 设计：把存储路径链接到外部哨兵并分别调用 list/add，比较原始字节防止路径逃逸
def test_route_store_rejects_symlinked_document(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"version":1,"active_route_id":null,"routes":[]}', encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    link = state / "routes.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    before = outside.read_bytes()
    store = RouteStore(link)

    with pytest.raises(RouteStoreError, match="regular file"):
        store.list()
    with pytest.raises(RouteStoreError):
        store.add(_route("blocked"))

    assert outside.read_bytes() == before


# 功能：验证当前文档单条坏 route 被可恢复隔离而好 route 与 Core readiness 继续工作
# 设计：同一 routes.json 放入好坏两条且 active 指向坏项，先只读检查再业务读取，区分报告与隔离副作用
def test_route_store_isolates_bad_record_without_losing_good_routes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "routes.json"
    good = _route("good").model_dump(mode="json")
    bad = {
        **_route("bad").model_dump(mode="json"),
        "supports_tools": False,
        "supports_parallel_tools": True,
    }
    original = {
        "version": 1,
        "active_route_id": "bad",
        "routes": [good, bad],
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    store = RouteStore(path)

    inspection = store.inspect()

    assert inspection.valid_route_count == 1
    assert inspection.active_route_unavailable is True
    assert [issue.code for issue in inspection.issues] == [
        "invalid_route",
        "invalid_active_route",
    ]
    assert not (tmp_path / "_quarantine").exists()

    assert [route.id for route in store.list()] == ["good"]
    assert store.active() is None
    quarantine = next((tmp_path / "_quarantine").glob("route-*.invalid.json"))
    assert json.loads(quarantine.read_text(encoding="utf-8")) == bad
    assert json.loads(path.read_text(encoding="utf-8")) == original
    after = store.inspect()
    assert after.issues[0].quarantined is True

    readiness = ConfigurationService(
        store,
        CredentialStore(tmp_path / "credentials.json"),
    ).readiness()
    assert readiness.status == "configuration_invalid"
    assert readiness.local_ready is False
    assert readiness.route_id == "bad"


# 功能：验证 route presets 由统一 catalog 扩展九种正式 Provider 并保留三类自定义模板
# 设计：检查稳定顺序、来源标识和本地免密语义，防止 CLI 选择与 catalog 定义漂移
def test_route_presets_cover_v1_provider_families() -> None:
    routes = {route.id: route for route in list_route_presets()}

    assert tuple(routes) == (
        "deepseek",
        "openai",
        "anthropic",
        "siliconflow",
        "gemini",
        "moonshot",
        "openrouter",
        "ollama",
        "lm-studio",
        "openai-compatible",
        "anthropic-compatible",
        "opencode-zen",
    )
    assert str(routes["opencode-zen"].base_url).rstrip("/") == (
        "https://opencode.ai/zen/v1/chat/completions"
    )
    assert get_route_preset("anthropic").wire_format == "anthropic_messages"
    assert routes["gemini"].catalog_id == "gemini"
    assert routes["ollama"].credential_required is False
    assert routes["ollama"].credential_ref == "none:ollama"
    assert routes["lm-studio"].credential_required is False


# 功能：验证免密 route 只能指向 loopback，不能成为远端绕过凭据的开关
# 设计：在合法 HTTPS 远端上显式关闭 credential_required，断言模型层拒绝该危险组合
def test_provider_route_rejects_credential_free_remote_endpoint() -> None:
    with pytest.raises(ValidationError, match="only allowed on loopback"):
        ProviderRoute(
            id="unsafe-no-key",
            provider="openai-compatible",
            wire_format="openai_chat",
            base_url=AnyHttpUrl("https://api.example.test/v1/chat/completions"),
            model="model",
            credential_ref="none:unsafe",
            credential_required=False,
        )


# 功能：验证 RouteStore 的增删改、活动切换和重载保持一致
# 设计：在临时文件完成全生命周期并重建 store，证明原子持久化而非仅内存状态
def test_route_store_roundtrip_and_active_selection(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    store = RouteStore(path)
    store.add(_route("alpha"))
    store.add(_route("beta"))
    store.update(_route("beta").model_copy(update={"model": "updated"}))
    selected = store.set_active("beta")

    restored = RouteStore(path)

    assert selected.model == "updated"
    assert restored.active() == selected
    assert [route.id for route in restored.list()] == ["alpha", "beta"]
    restored.remove("beta")
    assert restored.active() is None
    assert [route.id for route in restored.list()] == ["alpha"]


# 功能：验证路由切换和编辑不会把凭据正文写入 route store
# 设计：使用 credential ref 而非 key，切换两条路由后扫描完整 JSON 中不存在模拟 secret
def test_route_store_never_persists_credential_body(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    store = RouteStore(path)
    store.add(_route("first", credential_ref="file:first"))
    store.add(_route("second", credential_ref="file:second"), activate=True)

    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)

    assert payload["active_route_id"] == "second"
    assert "secret-first-value" not in serialized
    assert "secret-second-value" not in serialized


# 功能：验证重复、缺失和损坏 route 操作都返回结构化存储错误
# 设计：覆盖重复 add、缺失 set_active 及非法 JSON，保证 CLI 可稳定映射错误
def test_route_store_rejects_invalid_operations(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    store = RouteStore(path)
    store.add(_route("alpha"))

    with pytest.raises(RouteStoreError, match="already exists"):
        store.add(_route("alpha"))
    with pytest.raises(RouteStoreError, match="not found"):
        store.set_active("missing")

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RouteStoreError, match="invalid route store"):
        store.list()
