import json


# 注册与扩展源码同目录解析的 Skill、Prompt 和主题资源
def setup(api):
    # 记录发现原因并返回 Pi 兼容的资源目录字段
    def discover(event):
        path = api.workspace / "extension-resource-events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {
            "skillPaths": ["extension_resources/skills"],
            "promptPaths": ["extension_resources/prompts"],
            "themePaths": ["extension_resources/themes/light.json"],
        }

    api.on("resources_discover", discover)
