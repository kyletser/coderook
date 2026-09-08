# 用启动和结果钩子让模型先读取一次，然后只回答
def setup(api):
    api.on("before_agent_start", lambda event: api.set_active_tools(["read"]))
    api.on("tool_result", lambda event: api.set_active_tools([]))
