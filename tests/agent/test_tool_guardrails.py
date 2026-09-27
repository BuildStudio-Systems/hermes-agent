"""Pure tool-call guardrail primitive tests."""

import json

import pytest

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)




def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_default_repeated_identical_failed_call_warns_without_blocking():
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert decisions[0].action == "allow"
    assert [d.action for d in decisions[1:]] == ["warn", "warn", "warn", "warn"]
    assert {d.code for d in decisions[1:]} == {"repeated_exact_failure_warning"}
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_total_failure_budget_survives_successful_diagnostics_and_tool_changes():
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "hard_stop_enabled": True,
        "hard_stop_after": {"total_failure": 3},
    }))
    for index, tool in enumerate(("terminal", "read_file", "terminal"), 1):
        assert controller.before_call(tool, {"attempt": index}).allows_execution
        decision = controller.after_call(tool, {"attempt": index}, "Error: synthetic", failed=True)
        if index < 3:
            assert not decision.should_halt
            controller.after_call("terminal", {"command": "pwd"}, "ok", failed=False)
    assert decision.code == "total_tool_failure_halt"
    assert decision.count == 3
    # The rest of an already emitted sequential batch must not run either.
    blocked = controller.before_call("write_file", {"path": "not-created"})
    assert not blocked.allows_execution
    assert blocked.should_halt
    controller.after_call("terminal", {}, "ok", failed=False)
    assert not controller.before_call("terminal", {}).allows_execution
    controller.reset_for_turn()
    assert controller.before_call("terminal", {}).allows_execution
    assert not controller.after_call("terminal", {}, "Error", failed=True).should_halt


@pytest.mark.parametrize("enabled,limit", [(False, 2), (True, 0)])
def test_total_failure_budget_respects_opt_out(enabled, limit):
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({
        "hard_stop_enabled": enabled,
        "hard_stop_after": {"total_failure": limit},
    }))
    for index in range(15):
        assert controller.before_call("terminal", {"attempt": index}).allows_execution
        assert not controller.after_call("terminal", {"attempt": index}, "Error", failed=True).should_halt
        controller.after_call("terminal", {"command": "pwd"}, "ok", failed=False)


def test_total_failure_budget_parses_nested_and_flat_settings():
    assert ToolCallGuardrailConfig.from_mapping({"hard_stop_after": {"total_failure": 4}}).total_failure_halt_after == 4
    assert ToolCallGuardrailConfig.from_mapping({"total_failure_halt_after": 6}).total_failure_halt_after == 6
    for invalid in (-1, "invalid", None):
        assert ToolCallGuardrailConfig.from_mapping({"total_failure_halt_after": invalid}).total_failure_halt_after == 12


def test_failure_budget_loads_from_isolated_profile_without_changing_yaml(tmp_path, monkeypatch):
    from hermes_cli.config import load_config_readonly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = tmp_path / "config.yaml"
    original = (
        "tool_loop_guardrails:\n"
        "  hard_stop_enabled: true\n"
        "  hard_stop_after:\n"
        "    total_failure: 2\n"
    ).encode("utf-8")
    profile.write_bytes(original)
    loaded = load_config_readonly()
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig.from_mapping(loaded["tool_loop_guardrails"])
    )
    controller.after_call("terminal", {"command": "fail-1"}, '{"exit_code":1}')
    controller.after_call("terminal", {"command": "diagnose"}, '{"exit_code":0}')
    decision = controller.after_call("terminal", {"command": "fail-2"}, '{"exit_code":1}')
    assert decision.code == "total_tool_failure_halt"
    assert profile.read_bytes() == original


def test_hard_stop_enabled_blocks_repeated_exact_failure_before_next_execution():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    assert controller.before_call("web_search", args).action == "allow"
    first = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert first.action == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    second = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
    assert second.action == "warn"
    assert second.code == "repeated_exact_failure_warning"

    blocked = controller.before_call("web_search", args)
    assert blocked.action == "block"
    assert blocked.code == "repeated_exact_failure_block"
    assert blocked.count == 2














def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig  # noqa: E402






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == 50
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == 50


def test_web_search_cap_blocks_after_limit_regardless_of_hard_stop():
    # Loop caps fire even with hard_stop_enabled=False (the per-turn loop
    # detector's flag). Each distinct query avoids the loop detector so we know
    # the block came from the loop cap, not exact-failure repetition.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(3):
        assert controller.before_call("web_search", {"query": f"q{i}"}).action == "allow"
    decision = controller.before_call("web_search", {"query": "q4"})
    assert decision.action == "block"
    assert decision.code == "loop_web_search_cap"
    assert decision.should_halt is True








