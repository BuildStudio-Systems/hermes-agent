"""Per-request local thinking controls stay typed, scoped and narrowly allowed."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from gateway.platforms.api_server import _request_thinking_overrides
from agent.agent_init import _merge_custom_provider_extra_body
from agent.transports.chat_completions import ChatCompletionsTransport


@pytest.mark.parametrize("options", [None, [], {}, {"chat_template_kwargs": "false"},
    {"chat_template_kwargs": {"enable_thinking": "false", "preserve_thinking": 1,
                              "reasoning_effort": "invalid", "api_key": "not-forwarded"}}])
def test_invalid_or_missing_options_do_not_override_provider(options):
    assert _request_thinking_overrides(options) is None


def test_fast_removes_conflicting_effort_and_never_mutates_input():
    options = {"chat_template_kwargs": {"enable_thinking": False,
        "preserve_thinking": True, "reasoning_effort": "xhigh", "base_url": "not-forwarded"}}
    original = deepcopy(options)
    assert _request_thinking_overrides(options) == {"extra_body": {
        "chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": False}}}
    assert options == original


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
def test_per_request_levels_are_independent(effort):
    template = {"enable_thinking": True, "preserve_thinking": True, "reasoning_effort": effort}
    result = _request_thinking_overrides({"chat_template_kwargs": template})
    assert result == {"extra_body": {"chat_template_kwargs": template}}
    result["extra_body"]["chat_template_kwargs"]["enable_thinking"] = False
    assert template["enable_thinking"] is True
    assert _request_thinking_overrides(None) is None


@pytest.mark.parametrize("effort", [None, "low", "medium", "xhigh"])
def test_slider_reaches_model_transport_and_overrides_provider_default(effort):
    template = {"enable_thinking": effort is not None, "preserve_thinking": effort is not None}
    if effort:
        template["reasoning_effort"] = effort
    agent = SimpleNamespace(
        provider="custom", model="there-3.8", base_url="http://model.invalid/v1",
        request_overrides=_request_thinking_overrides({"chat_template_kwargs": template}),
    )
    providers = [{"base_url": agent.base_url, "model": agent.model, "extra_body": {
        "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "xhigh"},
        "top_k": 20,
    }}]
    original = deepcopy(providers)
    _merge_custom_provider_extra_body(agent, providers)
    transport = ChatCompletionsTransport()
    messages = [{"role": "user", "content": "hello"}]
    for _ in range(2):
        wire = transport.build_kwargs(
            model=agent.model, messages=messages, request_overrides=agent.request_overrides,
            reasoning_config={"enabled": bool(effort), **({"effort": effort} if effort else {})},
        )
        assert wire["extra_body"]["chat_template_kwargs"] == template
        assert wire["extra_body"]["top_k"] == 20
    assert providers == original
