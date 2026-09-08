# 在调用前或结果阶段终止工具批次，另一路径用于验证混合批次继续
def setup(api):
    api.on("tool_call", lambda event: {
        "block": True, "terminate": True, "reason": "Stopped by extension",
    } if event["input"].get("path") == "blocked.txt" else None)
    api.on("tool_result", lambda event: {
        "terminate": event["input"].get("path") != "continue.txt",
    })
