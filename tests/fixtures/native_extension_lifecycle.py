import json


# 注册完整 Agent/Turn/Message/Tool 生命周期观察器并写入工作区测试日志
def setup(api):
    path = api.workspace / "extension-lifecycle.jsonl"

    # 将事件逐行写入，测试可验证顺序和事件负载且不依赖模块存活时间
    def record(event):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    for name in (
        "agent_start", "agent_end", "agent_settled", "turn_start", "turn_end",
        "message_start", "message_update", "message_end",
        "tool_execution_start", "tool_execution_end",
        "session_start", "session_info_changed", "session_shutdown",
        "session_before_fork",
    ):
        api.on(name, record)
