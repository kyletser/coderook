from __future__ import annotations

from code_rook.core.authority import ToolAction
from code_rook.core.presets import MINIMAL_PRESET, AgentPreset, _preset_digest


# 功能：AgentPreset.digest 必须跨进程确定，不受 PYTHONHASHSEED 影响
# 设计：对同一 payload 构造两种集合列表顺序，直接验证归一摘要一致
def test_preset_digest_is_order_insensitive_for_set_fields() -> None:
    payload_a = MINIMAL_PRESET.model_dump(mode="json")
    payload_b = dict(payload_a)
    for field in ("authority_ceiling", "tool_allowlist"):
        value = payload_b.get(field)
        if isinstance(value, list):
            payload_b[field] = list(reversed(value))
    assert _preset_digest(payload_a) == _preset_digest(payload_b)


# 功能：authority_ceiling 完整集合（无 allowlist 的 standard 形态）同样参与顺序归一
# 设计：乱序重排全量 ToolAction，覆盖 tool_allowlist 为 None 时的 ceiling 分支
def test_preset_digest_normalizes_full_authority_ceiling() -> None:
    preset = AgentPreset(
        id="digest-probe",
        name="Digest Probe",
        stability=MINIMAL_PRESET.stability,
    )
    payload = preset.model_dump(mode="json")
    ceiling = list(payload["authority_ceiling"])
    assert isinstance(ceiling, list) and len(ceiling) == len(ToolAction)
    payload_shuffled = dict(payload)
    payload_shuffled["authority_ceiling"] = list(reversed(ceiling))
    assert preset.digest == _preset_digest(payload_shuffled)
