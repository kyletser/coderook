from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class ShadowGreeting(BaseTool):
    name = "extension_greeting"
    description = "Later extension must not win."
    input_schema = {"type": "object", "properties": {}}
    side_effect = ToolSideEffect.NONE

    # 返回可识别文本以证明后加载同名扩展没有抢占执行
    async def invoke(self, params):
        return ToolResult("later extension")


# 注册与前一个扩展同名的工具用于优先级测试
def setup(api):
    api.register_tool(ShadowGreeting())
