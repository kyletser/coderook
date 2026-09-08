import json
from pathlib import Path


# 注册请求变换和响应观察处理器。
def setup(api):
    # 在实际请求冻结前追加可验证的系统提示。
    def before_request(event):
        payload = event["payload"]
        payload["system"] = payload["system"] + "\n\nExtension provider hook active."
        return {"payload": payload}

    # 将统一响应写入临时工作区，证明观察发生在真实调用之后。
    def after_response(event):
        target = Path(api.workspace) / "provider-response.json"
        target.write_text(json.dumps(event["response"]), encoding="utf-8")

    api.on("before_provider_request", before_request)
    api.on("after_provider_response", after_response)
