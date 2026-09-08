# 保存会话局部计数，验证多轮运行不会重复执行 setup
def setup(api):
    count = 0

    # 下一轮沿用当前扩展闭包和活动工具选择
    def started(event):
        nonlocal count
        count += 1
        if count == 1:
            api.set_active_tools(["read"])
        assert api.get_active_tools() == ["read"]
        return {"system_prompt": event["system_prompt"] + f"\nSession count: {count}"}

    # 会话结束时写入清理标记，普通 Run 结束不能触发
    def closed():
        marker = api.workspace / "session-extension-closed.txt"
        marker.write_text(str(count), encoding="utf-8")

    api.on("before_agent_start", started)
    api.on_shutdown(closed)
