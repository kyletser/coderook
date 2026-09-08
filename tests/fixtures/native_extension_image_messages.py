import base64
import io

from PIL import Image


# 注册同时发送图片纠偏和图片后续消息的扩展
def setup(api):
    # 使用 Pi 形式的内嵌图片块，验证进入现有附件缩放及持久化链路
    async def started(event):
        for delivery, text, color in (
            ("steer", "/literal-steering", "blue"),
            ("follow_up", "!literal-follow-up", "red"),
        ):
            buffer = io.BytesIO()
            Image.new("RGB", (2400, 120), color).save(buffer, format="PNG")
            await api.send_user_message([
                {"type": "text", "text": text},
                {"type": "image", "mimeType": "image/png",
                 "data": base64.b64encode(buffer.getvalue()).decode("ascii")},
            ], deliver_as=delivery)

    api.on("before_agent_start", started)
