"""Interrupted text retries must not duplicate streamed downloadable answers."""
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _join_truncated_parts
from agent.stream_replay import StreamReplayPrefix
from gateway.platforms.api_server import _StreamingMediaResolver
from tests.run_agent.test_partial_stream_finish_reason import loop_agent  # noqa: F401
from tests.run_agent.test_continuation_ceiling_wedge import _stub
from tests.run_agent.test_run_agent import _mock_response
from tests.run_agent.test_streaming import _make_stream_chunk


@pytest.mark.parametrize("size", [1, 4, 19, 4096])
@pytest.mark.parametrize("replay", [True, False])
def test_real_loop_recovers_stream_and_final_answer(loop_agent, size, replay):
    prefix = "Synthetic two-file answer.\nMEDIA:/tmp/first.pdf\nMEDIA"
    suffix = ":/tmp/second.pdf"
    second = prefix + suffix if replay else suffix
    delivered = []
    loop_agent.stream_delta_callback = delivered.append
    attempts = iter([(prefix, _stub(prefix)), (second, _mock_response(content=second))])

    def provider(*args, **kwargs):
        text, response = next(attempts)
        for offset in range(0, len(text), size):
            loop_agent._fire_stream_delta(text[offset:offset + size])
        return response

    with (
        patch.object(loop_agent, "_interruptible_streaming_api_call", side_effect=provider) as upstream,
        patch.object(loop_agent, "_persist_session"),
        patch.object(loop_agent, "_save_trajectory"),
        patch.object(loop_agent, "_cleanup_task_resources"),
    ):
        result = loop_agent.run_conversation("Synthetic download test")
    assert upstream.call_count == 2
    assert result["final_response"] == prefix + suffix
    assert "".join(x for x in delivered if x is not None) == prefix + suffix

    # Exercise the real incremental media parser on those delivered chunks.
    media = _StreamingMediaResolver(lambda text: text.replace(
        "MEDIA:/tmp/first.pdf", "[first](/safe/first)"
    ).replace("MEDIA:/tmp/second.pdf", "[second](/safe/second)"))
    resolved = []
    for chunk in delivered:
        if chunk:
            resolved.extend(media.feed(chunk))
    resolved.extend(media.finish())
    assert "".join(resolved) == "Synthetic two-file answer.\n[first](/safe/first)\n[second](/safe/second)"


def test_divergent_retry_preserves_every_character():
    replay = StreamReplayPrefix("The old answer")
    assert replay.feed("The ") == ""
    assert replay.feed("new answer") == "The new answer"
    assert replay.feed(" continues") == " continues"


def test_actual_provider_stream_drop_and_replay(loop_agent):
    prefix = "Synthetic PDF answer.\nMEDIA:/tmp/first.pdf\nMEDIA"
    complete = prefix + ":/tmp/second.pdf"
    delivered = []
    loop_agent.stream_delta_callback = delivered.append
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        iter([_make_stream_chunk(content=prefix)]),  # No finish_reason: true drop.
        iter([_make_stream_chunk(content=complete, finish_reason="stop")]),
    ]
    with (
        patch.object(loop_agent, "_create_request_openai_client", return_value=client),
        patch.object(loop_agent, "_close_request_openai_client"),
        patch.object(loop_agent, "_persist_session"),
        patch.object(loop_agent, "_save_trajectory"),
        patch.object(loop_agent, "_cleanup_task_resources"),
    ):
        result = loop_agent.run_conversation("Synthetic PDF stream recovery")
    assert client.chat.completions.create.call_count == 2
    assert result["final_response"] == complete
    assert "".join(x for x in delivered if x is not None) == complete


def test_repeat_is_not_removed_outside_network_boundary():
    assert _join_truncated_parts(["Repeat.", "Repeat."]) == "Repeat.\nRepeat."


def test_recovery_rearms_against_combined_prefix():
    assert _join_truncated_parts(
        ["first ", "first second ", "first second third"],
        replay_boundaries={1, 2},
    ) == "first second third"


def test_next_normal_response_keeps_legitimate_repeat(loop_agent):
    delivered = []
    loop_agent.stream_delta_callback = delivered.append
    loop_agent._reset_stream_delivery_tracking()
    loop_agent._fire_stream_delta("Repeated answer.")
    loop_agent._stream_recovery_pending = True
    loop_agent._reset_stream_delivery_tracking()
    loop_agent._fire_stream_delta("Repeated answer.")
    loop_agent._reset_stream_delivery_tracking()
    loop_agent._fire_stream_delta("Repeated answer.")
    assert "".join(delivered) == "Repeated answer.Repeated answer."
