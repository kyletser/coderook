from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.markup import render

from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.route_store import RouteStore
from code_rook.tui.app import CodeRookTuiApp
from code_rook.tui.product import (
    ReadinessCard,
    RunEvidenceReducer,
    RunResultCard,
    SafeErrorCard,
    detect_locale,
    diagnostic_id,
    load_saved_locale,
    normalize_locale,
    save_locale,
    tr,
)
from code_rook.tui.widgets.input import (
    _clear_input_history,
    _input_history_path,
    _load_input_history,
    _save_input_history_entry,
)


# 功能：验证新增产品文案支持中英文且未知语言安全回退中文
# 设计：直接读取同一稳定 key，避免依赖 Textual 渲染和终端 locale
def test_product_text_has_zh_en_and_locale_fallback() -> None:
    assert tr("readiness.unconfigured.title", "zh") == "欢迎使用 CodeRook"
    assert tr("readiness.unconfigured.title", "en") == "Welcome to CodeRook"
    assert tr("readiness.unconfigured.title", "fr") == "欢迎使用 CodeRook"


# 功能：验证系统语言检测支持规范标签、下划线标签和显式产品覆盖
# 设计：注入独立环境字典，避免测试修改进程级 locale 并覆盖优先级与安全回退
def test_product_locale_detection_is_canonical_and_deterministic() -> None:
    assert normalize_locale("en_GB.UTF-8") == "en-US"
    assert normalize_locale("zh_Hans_CN") == "zh-CN"
    assert detect_locale({"LANG": "en_GB.UTF-8"}) == "en-US"
    assert detect_locale({"CODEROOK_LOCALE": "zh-CN", "LANG": "en_US"}) == "zh-CN"
    assert detect_locale({"LANG": "fr_FR.UTF-8"}) == "zh-CN"


# 功能：验证用户语言偏好原子保存、重新加载并优先于系统语言
# 设计：使用临时设置文件完成一次跨实例往返，同时验证显式环境覆盖最高优先级
def test_saved_locale_roundtrip_and_priority(tmp_path: Path) -> None:
    settings = tmp_path / "ui.json"

    assert save_locale("en-GB", settings) == "en-US"
    assert load_saved_locale(settings) == "en-US"
    assert detect_locale({"LANG": "zh_CN"}, settings_path=settings) == "en-US"
    assert (
        detect_locale(
            {"CODEROOK_LOCALE": "zh-CN", "LANG": "en_US"},
            settings_path=settings,
        )
        == "zh-CN"
    )


# 功能：验证结构化错误卡不会渲染原异常或其中的密钥
# 设计：使用包含测试密钥的异常生成诊断 ID，检查卡片只保留类别、动作和短编号
def test_safe_error_card_never_renders_raw_exception() -> None:
    secret = "sk-test-secret-value-123456"
    identifier = diagnostic_id("submission", RuntimeError(secret))
    card = SafeErrorCard("submission", identifier, action="submission")
    plain = render(str(card.content)).plain

    assert secret not in plain
    assert identifier in plain
    assert "submission" in plain
    assert "原输入已保留" in plain


# 功能：验证全局 audit.degraded 事件展示明确修复动作且不进入普通 run reducer
# 设计：直接调用独立 daemon handler，检查卡片包含服务端诊断 ID 和失败关闭提示
def test_tui_audit_degraded_event_renders_repair_card() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Any] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_daemon_event(
        {
            "type": "audit.degraded",
            "source": "runtime_projection",
            "diagnostic_id": "AUD-ABC123",
            "error_type": "OSError",
        }
    )

    assert len(appended) == 1
    assert isinstance(appended[0], SafeErrorCard)
    plain = render(str(appended[0].content)).plain
    assert "AUD-ABC123" in plain
    assert "修改类工具已暂停" in plain
    assert "doctor runtime --repair" in plain


# 功能：验证 readiness 卡在未配置时提供非阻塞欢迎和明确配置动作
# 设计：用最小只读快照渲染卡片，断言不会伪装模型已可执行或要求立即退出
def test_readiness_card_explains_unconfigured_state() -> None:
    readiness = SimpleNamespace(
        status="unconfigured",
        route_id=None,
        model=None,
        local_ready=False,
    )
    plain = render(str(ReadinessCard(readiness).content)).plain

    assert "欢迎使用 CodeRook" in plain
    assert "/config" in plain
    assert "/provider" in plain


# 功能：验证提交任务前缺少 route 会保留草稿且不会进入 run 创建路径
# 设计：驱动真实提交 handler 并替换输入框/发送入口，固定 readiness 是唯一前置门禁
async def test_submission_blocks_before_run_when_unconfigured(tmp_path: Path) -> None:
    class _Prompt:
        # 初始化提交 handler 所需的最小输入框状态
        def __init__(self) -> None:
            self.text = "修复登录"
            self.disabled = False
            self.border_title = "消息"
            self.history: list[str] = []

        # 记录成功接收的历史；被 readiness 拦截时不应调用
        def record_history(self, value: str) -> None:
            self.history.append(value)

        # 测试替身无需真实焦点系统
        def focus(self) -> None:
            return None

    class _Event:
        # 绑定输入框并模拟 Textual Submitted.value
        def __init__(self, prompt: _Prompt) -> None:
            self.text_area = prompt
            self.value = prompt.text

    route_store = RouteStore(tmp_path / "routes.json")
    credential_store = CredentialStore(tmp_path / "credentials.json")
    app = CodeRookTuiApp(
        "127.0.0.1",
        9999,
        route_store=route_store,
        credential_store=credential_store,
    )
    prompt = _Prompt()
    appended: list[Any] = []
    started: list[str] = []
    app._client = object()  # type: ignore[assignment]
    app._session_id = "session-1"
    app._prompt = lambda: prompt  # type: ignore[method-assign]
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._begin_message = (  # type: ignore[method-assign]
        lambda _prompt, content, _mode: started.append(content)
    )

    await app.on_chat_text_area_submitted(_Event(prompt))  # type: ignore[arg-type]

    assert started == []
    assert prompt.text == "修复登录"
    assert prompt.history == []
    assert any(isinstance(widget, ReadinessCard) for widget in appended)


# 功能：验证工作区历史路径隔离、敏感输入不落盘且可以清空
# 设计：对两个 workspace 使用显式路径，只向其中一个写普通值和密钥后检查物理文件
def test_workspace_history_isolated_redacted_and_clearable(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    state_root = tmp_path / "state"
    first_path = _input_history_path(first, state_root=state_root)
    second_path = _input_history_path(second, state_root=state_root)

    _save_input_history_entry("普通任务", path=first_path)
    _save_input_history_entry("api_key=sk-secret-secret-123456", path=first_path)
    _save_input_history_entry("另一个项目", path=second_path)

    assert _load_input_history(path=first_path) == ["普通任务"]
    assert _load_input_history(path=second_path) == ["另一个项目"]
    _clear_input_history(first, state_root=state_root)
    assert _load_input_history(path=first_path) == []
    assert _load_input_history(path=second_path) == ["另一个项目"]


# 功能：验证结果 reducer 以 durable receipt 覆盖实时 route 并汇总完整证据字段
# 设计：先喂入冲突的实时事件，再提供 inspector receipt，断言权威 route、成本和文件胜出
def test_run_result_reducer_prefers_authoritative_receipt() -> None:
    reducer = RunEvidenceReducer()
    reducer.consume(
        {
            "type": "run.started",
            "run_id": "run-1",
            "ts": "2026-08-24T10:00:00+00:00",
        }
    )
    reducer.consume(
        {
            "type": "llm.route_selected",
            "run_id": "run-1",
            "route_id": "event-route",
            "model": "event-model",
        }
    )
    finish = {
        "type": "run.finished",
        "run_id": "run-1",
        "status": "success",
        "steps": 4,
        "ts": "2026-08-24T10:00:03+00:00",
    }
    reducer.consume(finish)
    inspection = {
        "turn": {"status": "completed"},
        "receipt": {
            "status": "completed",
            "started_at": "2026-08-24T10:00:00+00:00",
            "finished_at": "2026-08-24T10:00:02.5+00:00",
            "route": {"route_id": "receipt-route", "model": "receipt-model"},
            "cost": 0.0123,
            "files_changed": ["src/app.py", "tests/test_app.py"],
            "verification": [{"verdict": "pass", "gate_count": 3, "passed": 3}],
            "unavailable": ["context_selection"],
            "error_classification": None,
        },
    }

    result = reducer.finalize(finish, inspection)
    plain = render(str(RunResultCard(result).content)).plain

    assert result.status == "success"
    assert result.duration == "2.5s"
    assert result.route == "receipt-route"
    assert result.model == "receipt-model"
    assert result.cost == "0.0123"
    assert result.files == ["src/app.py", "tests/test_app.py"]
    assert result.verification_status == "pass"
    assert "4 steps" in plain
    assert "/changes" in plain
    assert "/review" in plain
    assert "/rewind" in plain
    assert "/turn run-1" in plain


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_title"),
    [
        ("length", "incomplete", "Task incomplete"),
        ("incomplete", "incomplete", "Task incomplete"),
        ("content_filtered", "content_filtered", "Model content filtered"),
        ("transport_error", "transport_error", "Model transport interrupted"),
        ("cancelled", "interrupted", "Task interrupted"),
    ],
)
# 功能：验证模型终止语义在结果卡中保持可区分而不统一误报为普通失败
# 设计：固定 run status=failed 并逐项改变 outcome，证明结构化终止原因优先于旧状态字段
def test_run_result_preserves_structured_model_outcomes(
    outcome: str,
    expected_status: str,
    expected_title: str,
) -> None:
    reducer = RunEvidenceReducer()
    finish = {
        "type": "run.finished",
        "run_id": f"run-{outcome}",
        "status": "failed",
        "outcome": outcome,
        "failure_category": "model",
        "steps": 1,
    }

    result = reducer.finalize(finish)
    plain = render(str(RunResultCard(result, locale="en-US").content)).plain

    assert result.status == expected_status
    assert expected_title in plain


# 功能：验证 durable receipt 的 outcome 能覆盖可能滞后的 run.finished 状态
# 设计：让事件声称 failed、收据声明 incomplete，断言权威持久证据决定最终卡片语义
def test_run_result_prefers_receipt_outcome_over_event_status() -> None:
    reducer = RunEvidenceReducer()
    result = reducer.finalize(
        {
            "type": "run.finished",
            "run_id": "run-receipt-outcome",
            "status": "failed",
            "outcome": "failed",
            "steps": 1,
        },
        {
            "turn": {"status": "failed"},
            "receipt": {"status": "failed", "outcome": "incomplete"},
        },
    )

    assert result.status == "incomplete"


# 功能：验证结果卡只使用当前 TurnReceipt 的逐文件行数并诚实标记未知总计
# 设计：提供一项完整统计和一项 None，断言已知和数可见且不会借用 workspace 当前 diff 补齐
def test_run_result_line_stats_are_receipt_scoped_and_honest() -> None:
    reducer = RunEvidenceReducer()
    finish = {
        "type": "run.finished",
        "run_id": "run-lines",
        "status": "success",
        "steps": 2,
    }
    inspection = {
        "receipt": {
            "status": "completed",
            "changes": [
                {"path": "src/a.py", "additions": 7, "deletions": 2},
                {"path": "src/b.py", "additions": None, "deletions": None},
            ],
            "unavailable": ["change_line_stats"],
        }
    }

    result = reducer.finalize(finish, inspection)
    plain = render(str(RunResultCard(result, locale="en-US").content)).plain

    assert result.files == ["src/a.py", "src/b.py"]
    assert result.additions == 7
    assert result.deletions == 2
    assert result.line_stats_unknown is True
    assert "Known lines +7/-2" in plain
    assert "totals unknown" in plain


# 功能：验证缺少 inspector 时结果卡明确标记未知而不把零改动当作已证实
# 设计：仅提供 run.finished 事件，检查 reducer 把 route、cost、files 和 verification 列为未验证
def test_run_result_fallback_marks_authoritative_fields_unverified() -> None:
    reducer = RunEvidenceReducer()
    finish = {
        "type": "run.finished",
        "run_id": "run-offline",
        "status": "failed",
        "reason": "llm_error",
        "failure_category": "network",
        "steps": 1,
        "ts": "2026-08-24T10:00:00+00:00",
    }
    reducer.consume(finish)

    result = reducer.finalize(finish)

    assert set(result.unverified) == {
        "route",
        "cost",
        "files_changed",
        "verification",
    }
    assert result.failure == "network"


# 功能：验证延迟完成的结果加载不会在用户切换会话后串入新 transcript
# 设计：在 reducer 已收到终态后把结果事件标记为旧会话，直接执行 worker 并断言无控件追加
async def test_delayed_result_card_drops_after_session_switch() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    finish = {
        "type": "run.finished",
        "run_id": "run-old",
        "status": "success",
        "steps": 1,
        "_tui_session_id": "session-old",
    }
    appended: list[Any] = []
    app._session_id = "session-new"
    app._run_evidence.consume(finish)
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    await app._render_run_result(finish)

    assert appended == []
    assert "run-old" not in app._rendered_result_runs

    app._session_id = "session-old"
    await app._render_run_result(finish)

    assert len(appended) == 1
    assert "run-old" in app._rendered_result_runs


# 功能：验证结果卡只接受稳定失败分类码，不会展示模型或系统抛出的任意异常正文
# 设计：把测试密钥嵌入 run reason，检查 reducer 折叠分类且最终卡片不含原文
def test_run_result_redacts_non_classification_failure_text() -> None:
    reducer = RunEvidenceReducer()
    secret = "provider failed with sk-secret-secret-123456"
    finish = {
        "type": "run.finished",
        "run_id": "run-secret",
        "status": "failed",
        "reason": secret,
        "steps": 1,
    }
    reducer.consume(finish)

    result = reducer.finalize(finish)
    plain = render(str(RunResultCard(result).content)).plain

    assert result.failure == "runtime_failure"
    assert secret not in plain
