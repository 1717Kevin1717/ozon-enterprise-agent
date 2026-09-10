import asyncio
import json
import ssl

import httpx
import pytest

from app.agent.providers import DeepSeekProvider, ProviderFailure, QwenProvider, ZhipuProvider
from app.agent.providers.openai_compatible import _classify_http_error, _error_code, extract_message_content
from app.schemas.agent import ModelInput


def _provider(provider_cls=QwenProvider, *, key="test-key"):
    return provider_cls(api_key=key, base_url="https://provider.invalid/v1", model_name="test-model", timeout_seconds=1)


def test_provider_capabilities_are_explicit():
    assert _provider(QwenProvider).capabilities.supports_multimodal is True
    assert _provider(DeepSeekProvider).capabilities.supports_reasoning is True
    assert _provider(ZhipuProvider).provider_name == "zhipu"
    assert _provider(QwenProvider).status()["status"] == "configured_unverified"
    assert _provider(ZhipuProvider).status()["status"] == "legacy_configured_unverified"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "AUTH_FAILED"), (403, "AUTH_FAILED"), (404, "MODEL_NOT_FOUND"), (429, "RATE_LIMITED"), (503, "PROVIDER_INTERNAL_ERROR")],
)
def test_http_failure_codes_are_sanitized(status, expected):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    assert _error_code(httpx.HTTPStatusError("hidden upstream detail", request=request, response=response)) == expected


def test_timeout_failure_code_is_distinct():
    assert _error_code(httpx.ReadTimeout("hidden")) == "NETWORK_TIMEOUT"


def test_missing_key_stops_before_network():
    provider = _provider(key="")
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.semantic_interpret("匿名问题", {}))
    assert caught.value.code == "KEY_MISSING"


def test_deepseek_rejects_multimodal_input_before_network():
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_provider(DeepSeekProvider).semantic_interpret(
            ModelInput(text="检查图片", images=["opaque:image-1"]), {}
        ))
    assert caught.value.code == "MODEL_UNAVAILABLE"


def test_invalid_response_is_rejected(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", args[0])
            return httpx.Response(200, request=request, json={"choices": []})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_provider().semantic_interpret("匿名问题", {"available_sources": []}))
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.failure_stage == "RESPONSE_PARSE"
    assert caught.value.exception_class in {"IndexError", "KeyError"}
    assert caught.value.upstream_status == 200


def test_structured_response_and_usage_are_returned(monkeypatch):
    frame = {"intent": "product_price"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", args[0])
            return httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": json.dumps(frame)}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            })

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = asyncio.run(_provider().semantic_interpret("匿名问题", {}))
    assert json.loads(result.content) == frame
    assert result.usage["total_tokens"] == 11


def test_qwen_semantic_request_is_non_thinking_and_bounded(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["url"] = args[0]
            captured["headers"] = kwargs["headers"]
            captured["method"] = "POST"
            captured["body"] = kwargs["json"]
            request = httpx.Request("POST", args[0])
            return httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": json.dumps({"intent": "product_price"})}}],
            })

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    asyncio.run(_provider(QwenProvider).semantic_interpret("匿名问题", {}))

    body = captured["body"]
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert 300 <= body["max_tokens"] <= 800
    assert body.get("stream", False) is False
    assert captured["timeout"] == 1
    assert captured["url"] == "https://provider.invalid/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["headers"]["Authorization"] != "Bearer "
    assert json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize("provider_cls", [DeepSeekProvider, ZhipuProvider])
def test_qwen_non_thinking_option_does_not_leak_to_other_providers(monkeypatch, provider_cls):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["body"] = kwargs["json"]
            request = httpx.Request("POST", args[0])
            return httpx.Response(200, request=request, json={
                "choices": [{"message": {"content": json.dumps({"intent": "unknown"})}}],
            })

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    asyncio.run(_provider(provider_cls).semantic_interpret("匿名问题", {}))

    assert "enable_thinking" not in captured["body"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "AUTH_FAILED"), (429, "RATE_LIMITED")],
)
def test_provider_http_status_failure_keeps_safe_diagnostics(monkeypatch, status, expected):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", args[0])
            return httpx.Response(status, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_provider().semantic_interpret("匿名问题", {}))

    assert caught.value.code == expected
    assert caught.value.exception_class == "HTTPStatusError"
    assert caught.value.failure_stage == "RESPONSE_READ"
    assert caught.value.upstream_status == status


def _transport_failure(monkeypatch, exception_factory):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise exception_factory(httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_provider().semantic_interpret("匿名问题", {}))
    return caught.value


@pytest.mark.parametrize(
    ("exception_factory", "code", "stage"),
    [
        (lambda request: httpx.ConnectTimeout("hidden", request=request), "NETWORK_TIMEOUT", "DNS_CONNECT"),
        (lambda request: httpx.ReadTimeout("hidden", request=request), "NETWORK_TIMEOUT", "RESPONSE_WAIT"),
        (lambda request: httpx.ProxyError("hidden", request=request), "PROXY_ERROR", "REQUEST_SEND"),
        (lambda request: httpx.ConnectError("hidden", request=request), "CONNECT_ERROR", "DNS_CONNECT"),
        (lambda request: httpx.RemoteProtocolError("hidden", request=request), "REMOTE_PROTOCOL_ERROR", "RESPONSE_READ"),
        (lambda request: httpx.ReadError("hidden", request=request), "RESPONSE_READ_ERROR", "RESPONSE_READ"),
    ],
)
def test_transport_failures_are_classified(monkeypatch, exception_factory, code, stage):
    failure = _transport_failure(monkeypatch, exception_factory)
    assert failure.code == code
    assert failure.failure_stage == stage
    assert failure.exception_class
    assert failure.upstream_status is None


def test_tls_failure_is_classified_from_exception_chain(monkeypatch):
    def tls_error(request):
        try:
            raise ssl.SSLError("hidden")
        except ssl.SSLError as cause:
            error = httpx.ConnectError("hidden", request=request)
            error.__cause__ = cause
            return error

    failure = _transport_failure(monkeypatch, tls_error)
    assert failure.code == "TLS_ERROR"
    assert failure.failure_stage == "TLS_CONNECT"


def test_request_serialization_failure_stops_before_transport():
    provider = _provider()
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider._chat(
            messages=[{"role": "user", "content": "anonymous"}],
            max_tokens=700,
            request_options={"not_json": {"set-value"}},
        ))
    assert caught.value.code == "REQUEST_SERIALIZATION_ERROR"
    assert caught.value.failure_stage == "SERIALIZATION"
    assert caught.value.exception_class == "TypeError"


def test_unexpected_local_transport_error_is_sanitized(monkeypatch):
    failure = _transport_failure(monkeypatch, lambda request: RuntimeError("must not escape"))
    assert failure.code == "PROVIDER_INTERNAL_ERROR"
    assert failure.failure_stage == "REQUEST_SEND"
    assert failure.exception_class == "RuntimeError"
    assert "must not escape" not in str(failure)


def test_unexpected_request_build_error_is_sanitized(monkeypatch):
    provider = _provider()

    def fail_build(**kwargs):
        raise RuntimeError("must not escape")

    monkeypatch.setattr(provider, "_build_request_body", fail_build)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.semantic_interpret("匿名问题", {}))
    assert caught.value.code == "PROVIDER_INTERNAL_ERROR"
    assert caught.value.failure_stage == "REQUEST_BUILD"
    assert caught.value.exception_class == "RuntimeError"
    assert "must not escape" not in str(caught.value)


def test_failure_classification_never_uses_exception_message():
    request = httpx.Request("POST", "https://provider.invalid")
    classification = _classify_http_error(httpx.ConnectError("secret-like-message", request=request))
    assert classification.code == "CONNECT_ERROR"
    assert "secret" not in classification.code


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"intent": "product_detail", "confidence": 0.9}, '{"intent":"product_detail","confidence":0.9}'),
        ([{"type": "text", "text": '{"intent":"product_detail"}'}], '{"intent":"product_detail"}'),
        ([{"type": "text", "text": {"value": '{"intent":"product_detail"}'}}], '{"intent":"product_detail"}'),
    ],
)
def test_a01_supported_structured_content_envelopes_are_normalized(content, expected):
    payload = {"choices": [{"message": {"content": content}}]}
    assert extract_message_content(payload) == expected


@pytest.mark.parametrize("payload", [
    {},
    {"choices": []},
    {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": [123]}}]},
])
def test_a04_malformed_provider_envelope_remains_invalid_response(payload):
    with pytest.raises((KeyError, IndexError, TypeError, ValueError)):
        extract_message_content(payload)


@pytest.mark.parametrize("content", ["", "   ", [], [{"type": "text", "text": ""}]])
def test_a05_empty_provider_content_remains_invalid_response(content):
    with pytest.raises(ValueError):
        extract_message_content({"choices": [{"message": {"content": content}}]})
