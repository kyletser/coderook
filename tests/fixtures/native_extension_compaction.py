import json


# 用本地摘要接管会话压缩，并记录压缩前后的生命周期事件
def setup(api):
    path = api.workspace / "extension-compaction.jsonl"

    # 保存可序列化事件供集成测试检查提交顺序
    def record(event):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    # 直接返回扩展摘要，默认 Provider 不应被调用
    def before(event):
        record(event)
        return {"compaction": {
            "summary": "Extension supplied checkpoint",
            "firstKeptEntryId": "",
            "tokensBefore": event["preparation"]["tokensBefore"],
            "details": {"owner": "extension"},
        }}

    api.on("session_before_compact", before)
    api.on("session_compact", record)
    api.on("session_compact_failed", record)
