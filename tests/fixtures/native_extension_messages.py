# 将扩展主动输入接入纠偏和后续两个交付时机
def setup(api):
    # 特意使用命令前缀，验证默认按字面文本传递而不执行命令
    async def started(event):
        await api.send_user_message("/literal-steering", deliver_as="steer")
        await api.send_user_message("!literal-follow-up", deliver_as="follow_up")

    api.on("before_agent_start", started)
