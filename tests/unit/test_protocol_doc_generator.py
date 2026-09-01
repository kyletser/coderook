from __future__ import annotations

from scripts.gen_protocol_doc import _union_models, generate

from code_rook.core.bus.commands import Command
from code_rook.core.bus.events import Event


# 功能：验证 Wire Protocol 文档包含 Command 与 Event 判别联合的每个叶子模型
# 设计：直接从类型联合发现模型并匹配生成标题，避免手工 import 清单与 --check 一起自洽漂移
def test_generated_protocol_covers_complete_discriminated_unions() -> None:
    content = generate()
    models = _union_models(Command) | _union_models(Event)

    missing = [
        model.__name__
        for model in models
        if f"### {model.__name__}\n" not in content
    ]

    assert missing == []
