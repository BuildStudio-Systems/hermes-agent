"""HTTP generation controls must reach the model and remain request-local."""
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def adapter_app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    app = web.Application()
    app.router.add_post('/v1/chat/completions', adapter._handle_chat_completions)
    return adapter, app


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('limit_name', ['max_tokens', 'max_completion_tokens'])
async def test_http_controls_reach_execution(stream, limit_name):
    adapter, app = adapter_app()
    with patch.object(adapter, '_run_agent', new_callable=AsyncMock) as run:
        run.return_value = ({'final_response': 'OK', 'messages': []},
                            {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/chat/completions', json={
                'messages': [{'role': 'user', 'content': 'Say OK'}],
                'temperature': 0, limit_name: 16, 'stream': stream,
            })
            assert response.status == 200
            await response.read()
        assert run.call_args.kwargs['generation_options'] == {'temperature': 0, 'max_tokens': 16}


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'temperature': True}, {'temperature': '0'}, {'temperature': -0.1},
    {'temperature': float('nan')}, {'temperature': float('inf')},
    {'max_tokens': True}, {'max_tokens': 0}, {'max_tokens': 2.5},
    {'max_tokens': 131073}, {'max_tokens': 16, 'max_completion_tokens': 17},
])
async def test_invalid_controls_fail_before_agent(invalid):
    adapter, app = adapter_app()
    with patch.object(adapter, '_run_agent', new_callable=AsyncMock) as run:
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/chat/completions', json={
                'messages': [{'role': 'user', 'content': 'Hello'}], **invalid,
            })
            assert response.status == 400
            assert (await response.json())['error']
        run.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_retries_distinguish_generation_settings():
    adapter, app = adapter_app()
    with patch.object(adapter, '_run_agent', new_callable=AsyncMock) as run:
        run.return_value = ({'final_response': 'OK', 'messages': []},
                            {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
        async with TestClient(TestServer(app)) as client:
            for temperature, limit in [(0, 16), (0, 16), (1, 16), (1, 24)]:
                response = await client.post('/v1/chat/completions',
                    headers={'Idempotency-Key': 'synthetic-settings'}, json={
                        'messages': [{'role': 'user', 'content': 'Hello'}],
                        'temperature': temperature, 'max_tokens': limit,
                    })
                assert response.status == 200
                await response.read()
        assert run.call_count == 3


def test_model_options_preserve_thinking_cap_and_defaults(monkeypatch):
    from tests.gateway.test_api_server import _patch_create_agent_runtime
    calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    _patch_create_agent_runtime(monkeypatch, {}, FakeAgent)
    monkeypatch.setattr('gateway.run._resolve_runtime_agent_kwargs', lambda: {
        'provider': 'custom', 'base_url': 'http://127.0.0.1:8000/v1',
        'api_mode': 'chat_completions', 'max_tokens': 64, 'request_overrides': None,
    })
    adapter, _ = adapter_app()
    monkeypatch.setattr(adapter, '_ensure_session_db', lambda: None)
    thinking = {'chat_template_kwargs': {'enable_thinking': False}}
    adapter._create_agent(model_options=thinking,
        generation_options={'temperature': 0, 'max_tokens': 16})
    adapter._create_agent(generation_options={'max_tokens': 1000})
    adapter._create_agent()
    adapter._create_agent(generation_options={'temperature': 0})
    assert calls[0]['max_tokens'] == 16
    assert calls[0]['request_overrides'] == {
        'temperature': 0,
        'extra_body': {'chat_template_kwargs': {'enable_thinking': False, 'preserve_thinking': False}},
    }
    assert calls[1]['max_tokens'] == 64
    assert calls[2]['max_tokens'] == 64
    assert calls[2]['request_overrides'] is None
    assert calls[3]['request_overrides'] == {'temperature': 0}
    assert thinking == {'chat_template_kwargs': {'enable_thinking': False}}
