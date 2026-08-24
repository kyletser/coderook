from __future__ import annotations

from code_rook.core.audit import AuditHealth, AuditIncident


# 功能：验证首次审计写入故障切换为 degraded 并只广播一次脱敏事件
# 设计：连续注入两个含敏感文本的异常，断言首因锁定且事件不暴露异常正文
async def test_audit_health_degrades_once_with_redacted_incident() -> None:
    incidents: list[AuditIncident] = []

    # 收集可见审计事件而不依赖 EventBus 实现
    async def receive(incident: AuditIncident) -> None:
        incidents.append(incident)

    health = AuditHealth(receive)
    first = await health.degrade("runtime", OSError("secret-token"))
    second = await health.degrade("events", ValueError("other-secret"))

    assert health.degraded is True
    assert first == second
    assert incidents == [first]
    assert "secret" not in first.model_dump_json()


# 功能：验证审计状态只能通过显式修复动作恢复为 healthy
# 设计：先触发降级再调用 repair API，锁定不会被普通读取隐式清除的契约
async def test_audit_health_requires_explicit_repair() -> None:
    health = AuditHealth()
    await health.degrade("events", OSError("disk full"))

    await health.mark_repaired()

    assert health.degraded is False
    assert health.incident is None
