import json


# 接管虚拟命令并记录 Pi 兼容的 user_bash 事件
def setup(api):
    # 返回完整替代结果，证明不会落入本地 Bash 执行
    def intercept(event):
        path = api.workspace / "extension-user-bash.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        if event["command"] == "virtual-command":
            return {"result": {
                "output": "virtual output",
                "exitCode": 0,
                "cancelled": False,
                "truncated": False,
            }}
        return None

    api.on("user_bash", intercept)
