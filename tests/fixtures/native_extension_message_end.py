# 注册链式最终消息替换，用于验证结果、历史与后续上下文保持一致
def setup(api):
    # 第一层替换 assistant 正文，同时保持原消息角色和终止状态
    def replace(event):
        message = event["message"]
        if message["role"] != "assistant":
            return None
        return {"message": {
            **message,
            "content": [{"type": "text", "text": "Replaced by extension"}],
        }}

    # 第二层验证能看到前一层结果并继续链式修改
    def append(event):
        message = event["message"]
        if message["role"] != "assistant":
            return None
        return {"message": {
            **message,
            "content": [{"type": "text", "text": message["content"][0]["text"] + " twice"}],
        }}

    api.on("message_end", replace)
    api.on("message_end", append)
