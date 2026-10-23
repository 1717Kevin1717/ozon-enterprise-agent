"""D1.3 API/Repository regressions over isolated synthetic merchant batches."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.models import MerchantImportPreview, Product
from app.db.session import get_session
from app.main import app
from app.agent.tools import agent_v1_ask
from app.agent.model_router import ModelRouter
from app.services.product_import import confirm_import, preview_import
from test_model_router import MockProvider, frame, install_router
from tests.test_merchant_csv_import_d1 import complete_item, csv_bytes, database


def test_cross_product_scenario_preserves_canonical_rank_and_undo_scope(monkeypatch):
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
                complete_item("D13-1", name="合成候选甲", expected_price_rub="120"),
                complete_item("D13-2", name="合成候选乙", expected_price_rub="110"),
                complete_item("D13-3", name="合成候选丙", purchase_cost=""),
            )
            preview = client.post("/api/v1/products/import/preview", files={"file": ("batch.csv", content)})
            assert preview.status_code == 200, preview.text
            confirmation = client.post("/api/v1/products/import/confirm", data={
                "preview_id": preview.json()["data"]["preview_id"],
            }, files={"file": ("batch.csv", content)})
            assert confirmation.status_code == 200, confirmation.text
            first = client.post("/api/v1/agent/ask", json={"query": "查看本次导入的商品"}).json()["data"]
            session_id = first["session_id"]

            def ask(query):
                response = client.post("/api/v1/agent/ask", json={"query": query, "session_id": session_id})
                assert response.status_code == 200, response.text
                return response.json()["data"]

            ranked = ask("把可计算利润的商品按净利率从高到低排序")
            canonical = ranked["task_state"]["active_collection"]
            ranked_ids = canonical["ranked_product_ids"]
            assert len(ranked_ids) == 2, ranked
            assert len(canonical["product_ids"]) == 3
            first_scenario = ask("如果第一名售价下降10%，净利率会怎样？")
            assert first_scenario["response_type"] == "scenario_report", first_scenario
            assert first_scenario["product_ids"] == [ranked_ids[0]]
            assert "商家提供或估计" in first_scenario["answer"]
            assert "假设结果只是测算" in first_scenario["answer"]
            first_delta = first_scenario["simulation"]["delta"][ranked_ids[0]]
            assert first_delta["net_margin"] < 0
            second_scenario = ask("那第二名呢？")
            assert second_scenario["response_type"] == "scenario_report", second_scenario
            assert second_scenario["product_ids"] == [ranked_ids[1]]
            second_result = next(item for item in second_scenario["simulation"]["hypothetical"]
                                 if item["product_id"] == ranked_ids[1])
            assert f"{second_result['net_profit']:.2f} RUB" in second_scenario["answer"]
            assert f"{second_result['net_margin']:.1%}" in second_scenario["answer"]
            assert f"{second_result['roi']:.1%}" in second_scenario["answer"]
            assert "合成候选乙" in second_scenario["answer"]
            overrides = second_scenario["simulation"]["overrides"]
            assert [(item["field"], item["operation"], item["value"], item["target_product_ids"])
                    for item in overrides] == [
                ("current_price", "DECREASE_PERCENT", 10.0, [ranked_ids[0]]),
                ("current_price", "DECREASE_PERCENT", 10.0, [ranked_ids[1]]),
            ]
            assert second_scenario["task_state"]["active_collection"] == canonical
            assert second_scenario["simulation"]["delta"][ranked_ids[0]] == first_delta
            assert second_scenario["simulation"]["delta"][ranked_ids[1]]["net_margin"] < 0
            undone = ask("撤销第二名的假设")
            assert undone["response_type"] == "scenario_report", undone
            assert undone["simulation"]["operation"] == "REMOVE_OVERRIDE", undone
            assert [item["target_product_ids"] for item in undone["simulation"]["overrides"]] == [[ranked_ids[0]]]
            assert undone["task_state"]["active_collection"] == canonical
            assert undone["simulation"]["delta"][ranked_ids[0]] == first_delta
            assert undone["simulation"]["delta"][ranked_ids[1]]["net_margin"] == 0
            leader = ask("第一名现在还是谁？")
            assert leader["product_ids"][0] == ranked_ids[0], leader
            comparison = ask("比较第一名和第二名")
            assert set(comparison["product_ids"]) == set(ranked_ids), comparison
            assert comparison["task_state"]["active_collection"]["ranked_product_ids"] == ranked_ids
            assert comparison["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
            after_comparison = ask("那第二名呢？")
            assert after_comparison["product_ids"] == [ranked_ids[1]], after_comparison
            assert after_comparison["intent"] != "scenario_analysis", after_comparison
            assert after_comparison["task_state"]["active_collection"] == canonical
            async with sessions() as session:
                products = (await session.scalars(select(Product))).all()
                assert sorted(product.current_price for product in products) == [100, 110, 120]
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    asyncio.run(run())


def test_150_member_batch_is_complete_and_batch_id_is_tenant_scoped():
    async def run():
        engine, sessions = await database()
        try:
            async with sessions() as session:
                content = csv_bytes(*(complete_item(f"D13-{index:03d}", name=f"Synthetic {index:03d}")
                                      for index in range(150)))
                preview = await preview_import(session, "tenant-a", "user-a", content)
                confirmed = await confirm_import(session, "tenant-a", "user-a", preview.preview_id, content)
                assert len(confirmed.product_ids) == 150
                own = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                         f"分析导入批次 {preview.preview_id} 的商品", None)
                assert own["response_type"] == "collection_report", own
                collection = own["task_state"]["active_collection"]
                assert collection["source_batch_id"] == preview.preview_id
                assert len(collection["product_ids"]) == 150
                assert len(set(collection["product_ids"])) == 150
                assert own["collection_analysis"]["member_count"] == 150
                assert "商家提供或估计" in own["answer"]
                sorted_result = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                                    "把这批商品按净利率排序", own["session_id"])
                assert sorted_result["collection_analysis"]["member_count"] == 150, sorted_result
                ordered = sorted_result["task_state"]["active_collection"]["ranked_product_ids"]
                assert len(ordered) == 150
                assert ordered == sorted(ordered, reverse=True)  # Equal metrics: stable product-id tie break.
                missing_previous = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                                       "分析上一批导入的商品", None)
                assert missing_previous["response_type"] == "clarification"
                assert missing_previous["tool_call_count"] == 0
                next_content = csv_bytes(complete_item("D13-NEXT", name="Next synthetic"))
                next_preview = await preview_import(session, "tenant-a", "user-a", next_content)
                await confirm_import(session, "tenant-a", "user-a", next_preview.preview_id, next_content)
                previous = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                              "查看上一批导入的商品", None)
                assert previous["collection_analysis"]["member_count"] == 150
                assert previous["task_state"]["active_collection"]["source_batch_id"] == preview.preview_id
                now = datetime.now(UTC).replace(tzinfo=None)
                oversized = MerchantImportPreview(
                    company_id="tenant-a", user_id="user-a", file_sha256="0" * 64,
                    expires_at=now + timedelta(minutes=15), confirmed_at=now + timedelta(seconds=1),
                    result_json={"product_ids": [f"synthetic-{index}" for index in range(201)]},
                )
                session.add(oversized)
                await session.commit()
                too_large = await agent_v1_ask(session, "tenant-a", "user-a", "analyst",
                                               f"分析导入批次 {oversized.id} 的商品", None)
                assert too_large["response_type"] == "clarification"
                assert too_large["tool_call_count"] == 0
                assert "200" in too_large["answer"]
                alien = await agent_v1_ask(session, "tenant-b", "user-a", "analyst",
                                           f"分析导入批次 {preview.preview_id} 的商品", None)
                assert alien["response_type"] == "clarification"
                assert alien["tool_call_count"] == 0
                assert alien["task_state"]["active_collection"] is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_provider_cannot_turn_financial_readiness_into_formal_evidence_exclusion(monkeypatch):
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
                complete_item("D13-P1", name="合成商品甲"),
                complete_item("D13-P2", name="合成商品乙", expected_price_rub="115"),
                complete_item("D13-P3", name="合成待补商品", purchase_cost=""),
            )
            preview = client.post("/api/v1/products/import/preview", files={"file": ("batch.csv", content)})
            assert preview.status_code == 200
            confirmed = client.post("/api/v1/products/import/confirm", data={
                "preview_id": preview.json()["data"]["preview_id"],
            }, files={"file": ("batch.csv", content)})
            assert confirmed.status_code == 200
            established = client.post("/api/v1/agent/ask", json={"query": "查看本次导入的商品"}).json()["data"]

            semantic = frame("collection_analysis", ["profit"], ["analyze_collection"])
            semantic.update({
                "operation": "RERANK_COLLECTION", "collection_reference": "current_collection",
                "requires_context": True, "exclude_insufficient_data": True,
            })
            qwen = MockProvider("qwen", [semantic])
            install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
            response = client.post("/api/v1/agent/ask", json={
                "query": "把可以计算利润的商品按净利率排序",
                "session_id": established["session_id"],
                "provider_execution_policy": "QWEN_ONLY", "require_semantic_handoff": True,
            })
            assert response.status_code == 200, response.text
            result = response.json()["data"]
            assert len(qwen.calls) == 1
            assert result["active_provider"] == "qwen"
            assert result["fallback_used"] is False
            assert result["understanding"]["exclude_insufficient_data"] is False
            assert result["collection_analysis"]["member_count"] == 3
            assert len(result["task_state"]["active_collection"]["ranked_product_ids"]) == 2
            assert len(result["collection_analysis"]["excluded_from_ranking"]) == 1
            assert all(item["decision_status"] == "INSUFFICIENT_DATA" for item in result["products"])
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    asyncio.run(run())


def test_metric_ranking_and_negative_recommendation_assessment_keep_separate_gates(monkeypatch):
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
                complete_item("D13-G1", name="合成候选甲"),
                complete_item("D13-G2", name="合成候选乙", expected_price_rub="115"),
                complete_item("D13-G3", name="合成待补成本商品", purchase_cost=""),
            )
            preview = client.post("/api/v1/products/import/preview", files={"file": ("batch.csv", content)})
            assert preview.status_code == 200, preview.text
            confirmed = client.post("/api/v1/products/import/confirm", data={
                "preview_id": preview.json()["data"]["preview_id"],
            }, files={"file": ("batch.csv", content)})
            assert confirmed.status_code == 200, confirmed.text
            established = client.post("/api/v1/agent/ask", json={"query": "查看本次导入的商品"}).json()["data"]
            session_id = established["session_id"]
            original_ids = established["task_state"]["active_collection"]["product_ids"]

            for query in (
                "在能够可靠计算利润的商品中，按净利率从高到低排序，列出前三名，并说明哪些商品因证据不足暂时不能正式推荐。",
                "把这批商品按利润率排前三，并指出哪些暂时无法正式推荐",
            ):
                response = client.post("/api/v1/agent/ask", json={"query": query, "session_id": session_id})
                assert response.status_code == 200, response.text
                result = response.json()["data"]
                collection = result["collection_analysis"]
                assert collection["require_gate_pass"] is False, result
                assert collection["source_member_count"] == 3, result
                assert collection["ranking_result_count"] == 2, result
                assert collection["excluded_count"] == 1, result
                assert len(collection["ranked_product_ids"]) == 2, result
                assert len(collection["excluded_from_ranking"]) == 1, result
                assert collection["excluded_from_ranking"][0]["reason"] == "FINANCIAL_INPUTS_MISSING"
                assert "procurement_cost" in collection["excluded_from_ranking"][0]["missing_fields"]
                assert result["task_state"]["active_collection"]["product_ids"] == original_ids
                assert len(result["task_state"]["active_collection"]["product_ids"]) == 3
                assert "原集合共 3 个商品" in result["answer"]
                assert "本次有 2 个满足分析或排序条件" in result["answer"]
                assert "无法参与本次可靠利润排序" in result["answer"]
                assert "不代表通过正式推荐门禁" in result["answer"]
                assert all(item["decision_status"] == "INSUFFICIENT_DATA" for item in result["products"])

            fresh = client.post("/api/v1/agent/ask", json={"query": "查看本次导入的商品"})
            assert fresh.status_code == 200, fresh.text
            recommendation = client.post("/api/v1/agent/ask", json={
                "query": "从这批商品中推荐前三个", "session_id": fresh.json()["data"]["session_id"],
            })
            assert recommendation.status_code == 200, recommendation.text
            assert recommendation.json()["data"]["collection_analysis"]["require_gate_pass"] is True
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()

    asyncio.run(run())
