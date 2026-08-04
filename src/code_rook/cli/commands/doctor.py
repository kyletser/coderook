from __future__ import annotations

import asyncio

from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor, ProviderDoctorResult
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.route_store import RouteStore


# 诊断指定或当前活动路由，并只返回脱敏分类结果
async def diagnose_route(
    config: CodeRookConfig,
    route_id: str | None = None,
    *,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    doctor: ProviderDoctor | None = None,
) -> ProviderDoctorResult:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    registry = RouteRegistry(
        config.llm,
        route_store=routes,
        credential_store=credentials,
    )
    route = registry.route(route_id)
    credential = credentials.resolve(route.credential_ref)
    return await (doctor or ProviderDoctor()).check(route, credential)


# 运行 provider doctor，并按文本或 JSON 输出无敏感信息的结果
def cmd_doctor(
    config: CodeRookConfig,
    route_id: str | None = None,
    *,
    as_json: bool = False,
) -> None:
    result = asyncio.run(diagnose_route(config, route_id))
    if as_json:
        print(result.model_dump_json(indent=2))
        return
    print(f"route:      {result.route_id}")
    print(f"status:     {result.status}")
    print(f"category:   {result.category}")
    print(f"credential: {result.credential_source}")
    if result.http_status is not None:
        print(f"http:       {result.http_status}")
    print(f"message:    {result.message}")
