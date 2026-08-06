# TUI 增量重构计划

**基准日期**：2026-08-06
**对象**：`src/code_rook/tui/app.py`（4176 行，18 个类，其中 `CodeRookTuiApp` 自第 1706 行起约 2470 行、159 个方法、约 25 个斜杠命令分支）
**定位**：本计划只做**结构性拆分**，不改任何交互语义、快捷键或 IPC 协议。每一步独立可交付、可回滚，且由现有 TUI 单元测试（`test_tui_app.py`、`test_tui_main.py`、`test_turn_panel.py`、`test_tui_workflow_panel.py`、`test_tui_clipboard.py`）守门。

## 为什么不做一次性重写

`CodeRookTuiApp` 承担了连接管理、事件路由、斜杠命令、十余种弹窗控件的编排。Textual 应用的消息泵、焦点与 worker 生命周期耦合紧密，一次性重写无法用单元测试完全覆盖（很多交互依赖真实终端事件循环），风险不可控。因此采用"每次只搬家、不装修"的增量策略。

## 现状结构（第 260–1706 行已是独立控件）

| 控件 | 行号起点 | 建议去向 |
|---|---|---|
| `LLMStreamBlock` / `ToolCallBlock` / `ToolStepGroup` | 260 / 352 / 456 | `tui/widgets/stream.py` |
| `PermissionSelect` / `PermissionBlock` / `PermissionModePicker` | 535 / 725 / 801 | `tui/widgets/permission.py` |
| `UserQuestionSelect` / `CheckpointPicker` / `PlanReview` | 881 / 999 / 1079 | `tui/widgets/pickers.py` |
| `SessionPicker` / `ModelPicker` / `ProviderPicker` | 1152 / 1242 / 1333 | `tui/widgets/selectors.py` |
| `ConfigApiKeyPrompt` / `SlashCompleteWidget` / `ChatTextArea` | 1422 / 1503 / 1584 | `tui/widgets/input.py` |
| `ModelSwitch` / `ConfigSwitch` | 1692 / 1698 | `tui/actions.py`（纯数据类） |

这些类彼此只通过 Textual 消息通信，移动风险最低，作为第一批。

## 分阶段计划

### 阶段 1：控件外迁（低风险，先行）

把上表控件按目标文件移动，`app.py` 改为 `from code_rook.tui.widgets... import ...`。保持类名与消息类型不变，避免破坏 `isinstance`/消息订阅。
验收：`tests/unit/test_tui_app.py` 全绿；`coderook-tui` 冒烟（打开、提问、审批一次）。

### 阶段 2：连接层抽离

把 `_socket_loop`、`_handle_event*`、重连与会话恢复逻辑抽为 `tui/connection.py` 的 `TuiConnection`：

- 持有 `SocketClient` 与订阅 topics（含 `replay_from_run`）；
- 以回调或 Textual `post_message` 把事件交回 App，App 不再直接操作 socket；
- 断线重连、`_resume_session_id` 恢复逻辑随迁。

验收：新增 `tests/unit/test_tui_connection.py` 覆盖"断线→重连→恢复同一会话"；既有 TUI 测试全绿。

### 阶段 3：斜杠命令表格化

把约 25 个 `if content == "/x"` 分支改为数据驱动的注册表（`tui/commands.py`）：

```python
SLASH_COMMANDS: list[SlashCommand]  # name/description/handler/需要连接与否
```

补全弹窗（`SlashCompleteWidget`）与帮助文案共用同一注册表，消除两处维护。动态 skill 名仍按现有逻辑追加。
验收：`test_tui_app.py` 中斜杠相关测试全绿；补全列表与现状逐条比对。

### 阶段 4：IPC 动作封装

把分散的 `self._client.send_command(...)` 调用收敛到 `tui/ipc_actions.py`（compact/tasks/workers/workflow/diff/rewind/context/turn.inspect/authority 等），统一超时与错误提示。App 方法只保留编排与渲染。
验收：对每个动作补一条 fake-client 单测；冒烟 `/tasks /workers /diff /rewind`。

### 阶段 5：事件渲染器拆分（可选，收益递减时停止）

`_handle_event_inner` 的约 30 个事件分支可按事件族拆为渲染函数（`llm.*` → stream 渲染、`tool.*` → 步骤组、`permission.*` → 审批卡）。此阶段与控件耦合最深，放在最后，且允许只做一半。

## 每步通用纪律

1. 每阶段一个独立提交，提交信息注明"仅移动/无行为变更"或列出行为差异；
2. 移动前后跑 `uv run pytest tests/unit -q` 与 `uv run mypy src`；
3. 不在拆分过程中顺手修 bug——发现的 bug 记录到单独任务，避免"重构+修 bug"混合提交导致回归难定位；
4. 任何阶段若测试暴露隐性耦合，优先在该阶段内解决耦合，而不是跳过。

## 明确不做

- 不更换 UI 框架、不重绘布局；
- 不改快捷键与既有交互流；
- 不改 IPC 命令与事件契约（契约变更走 `core/bus` + `WIRE_PROTOCOL.md` 流程）；
- 不引入新的状态管理库——Textual 消息机制足够。
