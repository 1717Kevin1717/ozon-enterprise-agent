"""Isolated execution adapter; never use the online evaluation API or its oracle."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from contextlib import ExitStack
from unittest.mock import patch

from .schemas import GoldenCase

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


def safe_environment() -> dict[str, str]:
    # Allowlist OS runtime variables, not arbitrary inherited credentials/config.
    names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(
        APP_ENV="test", DATABASE_URL=MEMORY_URL, LLM_PROVIDER="disabled", LLM_API_KEY="",
        ZHIPU_API_KEY="", QWEN_API_KEY="", DASHSCOPE_API_KEY="", DEEPSEEK_API_KEY="",
        PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
    )
    return env


def execute_isolated(case: GoldenCase) -> dict:
    """Called only in a fresh worker; no application modules may predate safety guards."""
    if "app.core.config" in sys.modules or "app.db.session" in sys.modules:
        raise RuntimeError("Evaluation worker must start before application imports")
    import httpx
    import sqlalchemy.ext.asyncio as sa_async
    from pydantic_settings import BaseSettings

    original_settings_init = BaseSettings.__init__
    original_engine = sa_async.create_async_engine
    guard_stats = {"settings_without_dotenv": 0, "memory_engines": 0, "blocked_http_attempts": 0}

    def safe_settings(instance, *args, **kwargs):
        kwargs["_env_file"] = None
        kwargs["_secrets_dir"] = None
        guard_stats["settings_without_dotenv"] += 1
        original_settings_init(instance, *args, **kwargs)

    def memory_engine(url, *args, **kwargs):
        if str(url) != MEMORY_URL:
            raise RuntimeError("Non-memory database forbidden in evaluation")
        guard_stats["memory_engines"] += 1
        return original_engine(url, *args, **kwargs)

    def deny_http(*args, **kwargs):
        guard_stats["blocked_http_attempts"] += 1
        raise RuntimeError("External HTTP forbidden in evaluation")

    with ExitStack() as guards:
        guards.enter_context(patch.dict(os.environ, safe_environment(), clear=True))
        guards.enter_context(patch.object(BaseSettings, "__init__", safe_settings))
        guards.enter_context(patch.object(sa_async, "create_async_engine", memory_engine))
        guards.enter_context(patch.object(httpx.AsyncClient, "send", deny_http))
        guards.enter_context(patch.object(httpx.Client, "send", deny_http))
        guards.enter_context(patch("urllib.request.urlopen", deny_http))
        from app.core.config import settings
        if settings.database_url != MEMORY_URL or settings.zhipu_api_key or settings.llm_api_key or settings.qwen_api_key or settings.dashscope_api_key or settings.deepseek_api_key:
            raise RuntimeError("Unsafe evaluation settings")
        result = asyncio.run(_execute(case))
        result["safety"] = {**guard_stats, "database_url": MEMORY_URL, "real_key_loaded": False, "llm_mode": case.setup.mock_provider_route if case.setup.mock_provider_route != "disabled" else case.setup.mock_llm}
        return result


async def _execute(case: GoldenCase) -> dict:
    from app.agent import model_agent
    from app.agent import zhipu_agent as provider
    from app.agent.model_router import ModelRouter
    from app.agent.providers.base import ModelCapabilities, ModelProvider, ProviderCallResult, ProviderFailure
    from app.agent.tools import rule_agent_ask
    from app.db.models import Base, Company
    from app.db.session import engine, SessionLocal
    from app.repositories.products import ProductRepository
    from app.schemas.products import ProductIn
    from app.services.demo_dataset import enterprise_demo_candidates
    from app.schemas.agent import AgentRunResult

    company = f"golden-{case.case_id.lower()}"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            session.add(Company(id=company, name="Golden Mock Enterprise (not Ozon market data)"))
            await session.commit()
            repo = ProductRepository(session, company)
            inputs = enterprise_demo_candidates() if case.setup.dataset == "mock_enterprise_v1" else []
            inputs += [ProductIn(**item.model_dump()) for item in case.setup.additions]
            catalog = []
            for item in inputs:
                for override in case.setup.provenance_overrides:
                    if override.title != item.title:
                        continue
                    if override.clear_provenance:
                        item = item.model_copy(update={"field_lineage": {}, "raw_payload": {}, "sales_source": "not_provided"})
                    else:
                        record = {"source_type": override.source_type or "mock", "provider": override.provider or "mock_enterprise_catalog"}
                        if override.age_days is not None:
                            record["observed_at"] = (datetime.now(UTC) - timedelta(days=override.age_days)).isoformat()
                            record["collected_at"] = record["observed_at"]
                        item = item.model_copy(update={"field_lineage": {**item.field_lineage, override.field: record}})
                product = await repo.upsert(item)
                analysis = await repo.analyze(product)
                # Oracle input captured BEFORE the Agent runs; no search/filter tool used.
                catalog.append({"id": product.id, "title": product.title, "external_product_id": product.external_product_id, "price": product.current_price, "currency": product.currency, "margin": product.current_margin_rate, "risk": analysis.risk_level, "compliance": product.compliance_status})
            title_map = {row["title"]: row["id"] for row in catalog}

            def identity(title):
                if title not in title_map:
                    raise ValueError("Fixture title is missing")
                return title_map[title]

            foreign_catalog, foreign_denials = [], []
            if case.setup.foreign_additions:
                from app.agent.tools import get_product, get_product_history, calculate_profit
                foreign_company = company + "-other"
                session.add(Company(id=foreign_company, name="Other tenant fixture"))
                await session.commit()
                foreign_repo = ProductRepository(session, foreign_company)
                for item in case.setup.foreign_additions:
                    foreign = await foreign_repo.upsert(ProductIn(**item.model_dump(), raw_payload={"demo_data": True}))
                    await foreign_repo.analyze(foreign)
                    foreign_catalog.append({"id": foreign.id, "title": foreign.title})
                    foreign_denials += [await tool(repo, foreign.id, "company_admin") for tool in (get_product, get_product_history, calculate_profit)]

            def expand(query):
                expanded = re.sub(r"\{product_id:([^}]+)\}", lambda match: identity(match[1]), query)
                foreign_map = {item["title"]: item["id"] for item in foreign_catalog}
                return re.sub(r"\{foreign_product_id:([^}]+)\}", lambda match: foreign_map[match[1]], expanded)

            selected = [identity(title) for title in case.setup.selected_titles]
            query = expand(case.query)
            session_id = None
            if case.setup.previous_query:
                previous = await rule_agent_ask(session, company, "golden-user", "company_admin", expand(case.setup.previous_query), None, selected)
                session_id = previous["session_id"]
            mock_calls = 0
            if case.setup.mock_provider_route != "disabled":
                class EvalProvider(ModelProvider):
                    def __init__(self, name, frames=(), failure=None, *, reasoning=False):
                        super().__init__(api_key="offline-mock-not-real", base_url="https://offline.invalid", model_name=name + "-offline")
                        self.provider_name = name
                        self.frames = list(frames)
                        self.failure = failure
                        self.capabilities = ModelCapabilities(
                            supports_text=True, supports_structured_output=True,
                            supports_reasoning=reasoning, supports_tool_planning=True,
                        )

                    async def semantic_interpret(self, query, context_slots):
                        nonlocal mock_calls
                        mock_calls += 1
                        if self.failure:
                            raise ProviderFailure(self.failure, self.provider_name)
                        return ProviderCallResult(json.dumps(self.frames.pop(0), ensure_ascii=False), self.provider_name, self.model_name)

                    async def reason(self, decision_packet):
                        nonlocal mock_calls
                        mock_calls += 1
                        if self.failure:
                            raise ProviderFailure(self.failure, self.provider_name)
                        return ProviderCallResult(json.dumps(self.frames.pop(0), ensure_ascii=False), self.provider_name, self.model_name)

                mode = case.setup.mock_provider_route
                providers = []
                if mode != "provider_unconfigured":
                    providers.append(EvalProvider(
                        "qwen", [case.setup.semantic_plan],
                        failure="NETWORK_TIMEOUT" if mode == "qwen_timeout_deepseek" else None,
                    ))
                if mode == "qwen_timeout_deepseek":
                    providers.append(EvalProvider("deepseek", [case.setup.semantic_plan], reasoning=True))
                elif mode == "qwen_reasoning":
                    providers.append(EvalProvider("deepseek", [case.setup.reasoning_plan], reasoning=True))
                model_router = ModelRouter(
                    providers, primary_name="qwen", reasoning_name="deepseek",
                    data_mode="mock_only" if mode == "qwen_reasoning" else "disabled",
                )
                with patch.object(model_agent, "build_model_router", lambda: model_router):
                    result = await model_agent.dual_model_agent_ask(
                        session, company, "golden-user", "company_admin", query, session_id, selected,
                        selection_revision=1 if selected else 0, selection_bound_session_id=session_id,
                    )
            elif case.setup.mock_llm == "disabled":
                async def semantic_mock(*args):
                    nonlocal mock_calls
                    mock_calls += 1
                    return case.setup.semantic_plan
                result = await rule_agent_ask(session, company, "golden-user", "company_admin", query, session_id, selected,
                    semantic_planner=semantic_mock if case.setup.semantic_plan is not None else None)
            else:
                ids = [identity(title) for title in case.setup.mock_product_titles]

                class MockClient:
                    def __init__(self, *args, **kwargs):
                        pass
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        return False
                    async def post(self, *args, **kwargs):
                        nonlocal mock_calls
                        import httpx
                        mock_calls += 1
                        if mock_calls == 1:
                            count = 2 if case.setup.mock_llm == "repeated_tool" else 1
                            calls = [{"id": f"mock-call-{i}", "function": {"name": "compare_products", "arguments": json.dumps({"product_ids": ids})}} for i in range(count)]
                            body = {"choices": [{"message": {"content": "", "tool_calls": calls}}]}
                        elif case.setup.mock_llm == "timeout_after_tool":
                            raise httpx.ReadTimeout("Controlled mock timeout after successful tool")
                        else:
                            body = {"choices": [{"message": {"content": "Mock planning finished; business truth remains backend-owned."}}]}
                        return httpx.Response(200, json=body, request=httpx.Request("POST", "https://mock.invalid"))

                with patch.object(provider.httpx, "AsyncClient", MockClient), patch.object(provider.settings, "zhipu_api_key", "mock-eval-key-not-real"):
                    result = await provider.zhipu_agent_ask(session, company, "golden-user", "company_admin", query, session_id, selected)
            validated = AgentRunResult.model_validate(result).model_dump(mode="json")
            return {"query": query, "result": validated, "catalog": catalog, "mock_calls": mock_calls, "company_id": company, "foreign_catalog": foreign_catalog, "foreign_denials": foreign_denials}
    finally:
        await engine.dispose()
