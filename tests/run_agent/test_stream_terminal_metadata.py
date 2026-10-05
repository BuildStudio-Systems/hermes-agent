"""Content buffering must not discard metadata carried in the same SSE chunk."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from openai import OpenAI
import pytest


@pytest.mark.parametrize('parts', [
    ['Document code', ': FILE-14c1aaf1'],
    ['Document code', ':', ' FILE-', '14c1aaf1'],
    ['id', 'entifier is FILE-14c1aaf1'],
])
@pytest.mark.parametrize('finish_reason', ['stop', 'length', 'content_filter'])
def test_buffered_text_preserves_terminal_metadata(parts, finish_reason):
    from run_agent import AIAgent
    from hermes_constants import PARTIAL_STREAM_STUB_ID

    chunks = []
    for index, text in enumerate(parts):
        terminal = index == len(parts) - 1
        chunks.append({'id': 'synthetic-stream', 'object': 'chat.completion.chunk',
                       'created': 0, 'model': 'test/model',
                       'choices': [{'index': 0, 'delta': {'content': text},
                                    'finish_reason': finish_reason if terminal else None}],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 8, 'total_tokens': 18} if terminal else None})
    wire = b''.join(('data: '+json.dumps(c)+'\n\n').encode() for c in chunks) + b'data: [DONE]\n\n'
    # Real SDK/SSE decoding over in-memory HTTP: no provider/network needed.
    client = OpenAI(api_key='synthetic', base_url='https://synthetic.invalid/v1',
                    http_client=httpx.Client(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=wire))))
    deltas = []
    with patch.object(AIAgent, '_create_request_openai_client', return_value=client), \
         patch.object(AIAgent, '_close_request_openai_client'):
        agent = AIAgent(api_key='synthetic', base_url='https://synthetic.invalid/v1',
                        model='test/model', quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.api_mode = 'chat_completions'
        agent.stream_delta_callback = deltas.append
        try:
            result = agent._interruptible_streaming_api_call({'model': 'test/model', 'messages': []})
        finally:
            client.close()
    assert result.id != PARTIAL_STREAM_STUB_ID
    assert result.choices[0].finish_reason == finish_reason
    assert result.choices[0].message.content == ''.join(parts)
    assert ''.join(deltas) == ''.join(parts)
    assert result.usage.total_tokens == 18


def test_buffered_content_does_not_skip_tool_delta():
    from run_agent import AIAgent
    tc = SimpleNamespace(index=0, id='synthetic-call', function=SimpleNamespace(name='synthetic_tool', arguments='{}'))
    chunk = SimpleNamespace(model='test/model', usage=None, choices=[SimpleNamespace(
        index=0, delta=SimpleNamespace(content='id', tool_calls=[tc], reasoning_content=None, reasoning=None),
        finish_reason='tool_calls')])
    client = MagicMock()
    client.chat.completions.create.return_value = iter([chunk])
    with patch.object(AIAgent, '_create_request_openai_client', return_value=client), \
         patch.object(AIAgent, '_close_request_openai_client'):
        agent = AIAgent(api_key='synthetic', base_url='https://synthetic.invalid/v1',
                        model='test/model', quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.api_mode = 'chat_completions'
        result = agent._interruptible_streaming_api_call({})
    assert result.choices[0].finish_reason == 'tool_calls'
    assert result.choices[0].message.tool_calls[0].function.name == 'synthetic_tool'
