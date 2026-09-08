from code_rook.core.agent_runtime.messages import from_provider


# 注册只影响当前模型请求的上下文转换链，不向会话持久历史插入消息
def setup(api):
    # 在每次请求中加入临时上下文并记录完整工具结果是否已经到达
    def append_context(event):
        messages = event["messages"]
        count = sum(message["role"] == "toolResult" for message in messages)
        messages.append({"role": "user", "content": f"Request-only context: {count}"})

    # 验证后一钩子可以读取前一钩子原地变换后的内容并返回替换列表
    async def replace_context(event):
        assert "Request-only context:" in event["messages"][-1]["content"]
        return {"messages": from_provider(event["messages"])}

    api.on("context", append_context)
    api.on("context", replace_context)
