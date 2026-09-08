# 注册一个本地命令和一个主动提交会话任务的命令
def setup(api):
    # 原样接收参数，向用户返回结果而不调用模型
    def greet(arguments):
        return f"Hello: {arguments}"

    # 使用同一会话的正式输入入口启动模型任务
    async def ask(arguments):
        await api.send_user_message(arguments)
        return "Request completed"

    api.register_command("greet", greet, description="Local greeting")
    api.register_command("ask-agent", ask, description="Send a task")

    # 接管固定测试输入，验证所有传输入口不会创建空任务。
    def receive(event):
        return {"action": "handled" if event["text"] == "__local_input__" else "continue"}

    api.on("input", receive)
