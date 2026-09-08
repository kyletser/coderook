from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class GreetingTool(BaseTool):
    name = "extension_greeting"
    description = "Return a local greeting."
    prompt_snippet = "A deterministic local greeting"
    prompt_guidelines = ("Only greet when explicitly requested.",)
    input_schema = {"type": "object", "properties": {}}
    side_effect = ToolSideEffect.NONE

    # 返回确定性结果供真实模型管线测试使用
    async def invoke(self, params):
        return ToolResult("Hello from a Python extension")


# 注册工具并安排可观测的结束回调
def setup(api):
    api.register_tool(GreetingTool())
    api.on("tool_result", lambda event: {
        "content": event["content"] + " [extension processed]",
        "details": {"ui_only": "display-metadata"},
        "images": [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6l8AAAAASUVORK5CYII=",
        }}],
    })
    api.on("before_agent_start", lambda event: {
        "system_prompt": event["system_prompt"] + "\nUse the extension greeting when requested.",
        "message": {"custom_type": "greeting-context", "content": "Greeting context from extension",
                    "display": False, "details": {"version": 1}},
    })
    api.on("before_agent_start", lambda event: {
        "message": {"custom_type": "greeting-note", "content": "Visible extension note",
                    "display": True},
    })
    api.on("run.finished", lambda event: (api.workspace / "extension-finished.txt").write_text(
        event["status"],
    ))
    api.on_shutdown(lambda: (api.workspace / "extension-closed.txt").write_text("closed"))
