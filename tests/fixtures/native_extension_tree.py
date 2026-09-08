import json


# 为分支导航提供本地摘要并记录切换前后的原生事件
def setup(api):
    path = api.workspace / "extension-tree.jsonl"

    # 保存事件供测试验证真实导航顺序
    def record(event):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    # 使用扩展摘要跳过模型摘要调用并附加分支标签
    def before(event):
        record(event)
        return {
            "summary": {"summary": "Extension branch summary", "details": {"source": "test"}},
            "label": "from-extension",
        }

    api.on("session_before_tree", before)
    api.on("session_tree", record)
