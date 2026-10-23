"""A confirmed merchant batch must become a tenant-scoped Agent collection."""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.models import Base, Product, ProductSnapshot
from app.db.session import get_session
from app.main import app
from app.services.product_import import ImportConflict, confirm_import, latest_confirmed_batch, preview_import
from app.agent.tools import agent_v1_ask
from tests.test_merchant_csv_import_d1 import complete_item, csv_bytes, database


def test_confirmed_batch_enters_agent_collection(monkeypatch):
    async def run():
        engine, sessions = await database()

        async def override_session():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        try:
            monkeypatch.setattr(settings, "app_env", "test")
            monkeypatch.setattr(settings, "demo_identity_enabled", True)
            monkeypatch.setattr(settings, "llm_provider", "disabled")
            client = TestClient(app)
            content = csv_bytes(
                complete_item("B-1", name="自有商品甲"),
                complete_item("B-2", name="自有商品乙", expected_price_rub="110"),
                complete_item("B-3", name="自有商品丙", purchase_cost=""),
            )
            preview = client.post("/api/v1/products/import/preview", files={"file": ("batch.csv", content)})
            assert preview.status_code == 200, preview.text
            batch_id = preview.json()["data"]["preview_id"]
            confirm = client.post("/api/v1/products/import/confirm", data={"preview_id": batch_id},
                                  files={"file": ("batch.csv", content)})
            assert confirm.status_code == 200, confirm.text
            expected = set(confirm.json()["data"]["product_ids"])
            first = client.post("/api/v1/agent/ask", json={"query": "查看我刚导入的商品"})
            assert first.status_code == 200, first.text
            result = first.json()["data"]
            assert result["intent"] == "collection_analysis", result
            collection = result["task_state"]["active_collection"]
            assert collection is not None, result
            assert collection["collection_type"] == "merchant_import_batch"
            assert collection["source_batch_id"] == batch_id
            assert set(collection["product_ids"]) == expected
            assert collection["require_gate_pass"] is False
            assert result["collection_analysis"]["member_count"] == 3
            assert "不代表通过正式推荐门禁" in result["answer"]
            incomplete = [product for product in result["products"] if product["title"] == "自有商品丙"]
            assert len(incomplete) == 1
            assert incomplete[0]["net_profit"] is None
            assert result["session_id"]
            ready = client.post("/api/v1/agent/ask", json={
                "query": "这批商品哪些可以计算利润？", "session_id": result["session_id"],
            })
            assert ready.status_code == 200, ready.text
            ready_data = ready.json()["data"]
            assert ready_data["intent"] == "collection_analysis", ready_data
            assert ready_data["collection_analysis"]["member_count"] == 2
            assert set(ready_data["task_state"]["active_collection"]["product_ids"]) == expected
            recommendation = client.post("/api/v1/agent/ask", json={
                "query": "从这批商品中推荐前三个", "session_id": result["session_id"],
            })
            assert recommendation.status_code == 200, recommendation.text
            recommendation_data = recommendation.json()["data"]
            assert recommendation_data["collection_analysis"]["require_gate_pass"] is True, recommendation_data["understanding"]
            assert recommendation_data["collection_analysis"]["eligible_count"] == 0
            ranked = client.post("/api/v1/agent/ask", json={
                "query": "把这批商品按净利率从高到低排序", "session_id": result["session_id"],
            })
            assert ranked.status_code == 200, ranked.text
            ranked_data = ranked.json()["data"]
            assert ranked_data["intent"] == "collection_analysis", ranked_data
            assert ranked_data["task_state"]["active_collection"]["product_ids"]
            assert set(ranked_data["task_state"]["active_collection"]["product_ids"]) == expected
            assert ranked_data["task_state"]["active_collection"]["ranking_dimensions"] == ["profit"]
            assert len(ranked_data["task_state"]["active_collection"]["ranked_product_ids"]) == 2
            excluded = ranked_data["collection_analysis"]["excluded_from_ranking"]
            assert [item["title"] for item in excluded] == ["自有商品丙"]
            assert "procurement_cost" in excluded[0]["missing_fields"]
            assert "无法参与本次可靠利润排序：自有商品丙" in ranked_data["answer"]
            ranked_ids = ranked_data["task_state"]["active_collection"]["ranked_product_ids"]
            leader = client.post("/api/v1/agent/ask", json={
                "query": "第一名为什么排在前面？", "session_id": result["session_id"],
            })
            assert leader.status_code == 200, leader.text
            leader_data = leader.json()["data"]
            assert leader_data["product_ids"][0] == ranked_ids[0], leader_data
            assert "净利率" in leader_data["answer"] or "利润" in leader_data["answer"]
            assert leader_data["task_state"]["active_collection"]["ranking_dimensions"] == ["profit"]
            assert set(leader_data["task_state"]["active_collection"]["product_ids"]) == expected
            scenario = client.post("/api/v1/agent/ask", json={
                "query": "如果第一名售价下降10%，净利率会怎样？", "session_id": result["session_id"],
            })
            assert scenario.status_code == 200, scenario.text
            scenario_data = scenario.json()["data"]
            assert scenario_data["response_type"] == "scenario_report", scenario_data
            assert scenario_data["simulation"]["hypothetical"][0]["product_id"] == ranked_ids[0]
            assert set(scenario_data["task_state"]["active_collection"]["product_ids"]) == expected
            second = client.post("/api/v1/agent/ask", json={
                "query": "那第二名呢？", "session_id": result["session_id"],
            })
            assert second.status_code == 200, second.text
            second_data = second.json()["data"]
            assert second_data["understanding"]["ordinal_reference"] == 2, second_data["understanding"]
            assert second_data["response_type"] == "scenario_report", second_data
            assert second_data["simulation"]["operation"] == "UPDATE_SCENARIO"
            assert second_data["product_ids"] == [ranked_ids[1]]
            assert len(second_data["simulation"]["overrides"]) == 2
            assert second_data["simulation"]["overrides"][0]["target_product_ids"] == [ranked_ids[0]]
            assert second_data["simulation"]["overrides"][1]["target_product_ids"] == [ranked_ids[1]]
            reset = client.post("/api/v1/agent/ask", json={
                "query": "撤销刚才的假设", "session_id": result["session_id"],
            })
            assert reset.status_code == 200, reset.text
            assert reset.json()["data"]["simulation"]["operation"] == "RESET_SCENARIO"
            assert reset.json()["data"]["task_state"]["active_collection"]["ranked_product_ids"] == ranked_ids
            gaps = client.post("/api/v1/agent/ask", json={
                "query": "这批商品哪些缺少成本信息？", "session_id": result["session_id"],
            })
            assert gaps.status_code == 200, gaps.text
            gap_data = gaps.json()["data"]
            assert gap_data["intent"] == "collection_analysis", gap_data
            assert set(gap_data["task_state"]["active_collection"]["product_ids"]) == expected
            assert gap_data["task_state"]["active_collection"]["source_batch_id"] == batch_id
            assert [product["title"] for product in gap_data["products"]] == ["自有商品丙"]
            assert gap_data["products"][0]["net_profit"] is None
            assert gap_data["task_state"]["active_collection"]["ranked_product_ids"] == ranked_ids
            comparison = client.post("/api/v1/agent/ask", json={
                "query": "比较当前第一名和第二名", "session_id": result["session_id"],
            })
            assert comparison.status_code == 200, comparison.text
            comparison_data = comparison.json()["data"]
            assert set(ranked_ids).issubset(set(comparison_data["product_ids"])), comparison_data
            assert comparison_data["task_state"]["active_collection"]["ranked_product_ids"] == ranked_ids
            async with sessions() as verify_session:
                products = (await verify_session.scalars(select(Product))).all()
                assert sorted(product.current_price for product in products) == [100, 100, 110]
                assert all((product.raw_payload or {}).get("_merchant_import", {}).get("source_type")
                           == "MERCHANT_INTERNAL" for product in products)
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    asyncio.run(run())


def test_file_sqlite_concurrent_confirmation_is_atomic(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'imports.db').as_posix()}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            content = csv_bytes(complete_item("CONCURRENT"))
            async with sessions() as session:
                preview = await preview_import(session, "tenant-a", "user-a", content)

            async def confirm():
                async with sessions() as session:
                    return await confirm_import(session, "tenant-a", "user-a", preview.preview_id, content)

            outcomes = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Product)) == 1, outcomes
                assert await session.scalar(select(func.count()).select_from(ProductSnapshot)) == 1, outcomes
            assert all(not isinstance(item, Exception) for item in outcomes), outcomes
            assert sorted(item.replayed for item in outcomes) == [False, True]
            same_identity = csv_bytes(complete_item("CONCURRENT-2"))
            async with sessions() as session:
                first_preview = await preview_import(session, "tenant-a", "user-a", same_identity)
                second_preview = await preview_import(session, "tenant-a", "user-a", same_identity)

            async def confirm_preview(preview_id):
                async with sessions() as session:
                    return await confirm_import(session, "tenant-a", "user-a", preview_id, same_identity)

            competing = await asyncio.gather(
                confirm_preview(first_preview.preview_id),
                confirm_preview(second_preview.preview_id), return_exceptions=True,
            )
            assert sum(not isinstance(item, Exception) for item in competing) == 1, competing
            assert sum(isinstance(item, ImportConflict) and item.code == "PREVIEW_STALE"
                       for item in competing) == 1, competing
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Product)) == 2
                assert await session.scalar(select(func.count()).select_from(ProductSnapshot)) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_batch_scope_and_over_100_builds_complete_bounded_collection():
    async def run():
        engine, sessions = await database()
        try:
            async with sessions() as session:
                content = csv_bytes(*(complete_item(f"B-{index}", name=f"Synthetic {index}")
                                      for index in range(101)))
                preview = await preview_import(session, "tenant-a", "user-a", content)
                result = await confirm_import(session, "tenant-a", "user-a", preview.preview_id, content)
                assert len(result.product_ids) == 101
                assert len(result.row_outcomes) == 101
                assert await latest_confirmed_batch(session, "tenant-b", "user-a") is None
                assert await latest_confirmed_batch(session, "tenant-a", "user-b") is None
                own = await latest_confirmed_batch(session, "tenant-a", "user-a")
                assert own is not None and own[0] == preview.preview_id
                answer = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                            "分析本次导入的商品", None)
                assert answer["response_type"] == "collection_report", answer
                assert answer["collection_analysis"]["member_count"] == 101
                assert len(answer["task_state"]["active_collection"]["product_ids"]) == 101
                assert len(set(answer["task_state"]["active_collection"]["product_ids"])) == 101
                unauthorized = await agent_v1_ask(session, "tenant-b", "user-b", "analyst",
                                                   "查看本次导入的商品", answer["session_id"])
                assert unauthorized["session_id"] != answer["session_id"]
                assert unauthorized["response_type"] == "clarification"
                assert unauthorized["tool_call_count"] == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
