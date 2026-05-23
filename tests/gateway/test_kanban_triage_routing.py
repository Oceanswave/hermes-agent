"""Tests for triage-first gateway chat routing into Kanban."""

import json
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli import kanban_db


def _source(*, message_id: str = "m1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_name="Hermes Ops",
        chat_type="dm",
        user_id="user-1",
        user_name="Sean",
        message_id=message_id,
    )


def _event(text: str, *, message_id: str = "m1") -> MessageEvent:
    return MessageEvent(text=text, source=_source(message_id=message_id), message_id=message_id)


def _enabled_config(**overrides) -> GatewayConfig:
    config = {
        "enabled": True,
        "default_mode": "normal",
        "triage_assignee": "paul",
        "fallback_board": "inbox",
        "create_missing_boards": True,
        "board_create_policy": "explicit_project_only",
        "acknowledge": True,
        "boards": {
            "inbox": {"aliases": ["inbox"], "default_category": "general"},
            "hermes-agent": {
                "aliases": ["hermes", "hermes agent", "agent"],
                "default_category": "engineering",
            },
        },
        "categories": {
            "general": {"board": "inbox", "assignee": "paul"},
            "engineering": {"board": "hermes-agent", "assignee": "paul"},
        },
    }
    config.update(overrides)
    return GatewayConfig.from_dict({"kanban_triage": config})


def _task_count(board: str) -> int:
    with kanban_db.connect(board=board) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    yield home


def test_config_round_trips_kanban_triage_block():
    cfg = _enabled_config(default_mode="triage_first", min_confidence=0.8)

    assert cfg.kanban_triage["enabled"] is True
    assert cfg.kanban_triage["default_mode"] == "triage_first"
    assert cfg.to_dict()["kanban_triage"]["min_confidence"] == 0.8


def test_slash_command_bypasses_triage_creation():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    result = route_chat_to_kanban_if_needed(_event("/help"), _enabled_config())

    assert result.created is False
    assert result.bypass_rule == "command_prefix"
    assert _task_count("inbox") == 0


def test_explicit_kanban_request_creates_triage_card_on_alias_board_and_acknowledges():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    result = route_chat_to_kanban_if_needed(
        _event("add this to kanban for Hermes Agent: fix gateway retries"),
        _enabled_config(),
    )

    assert result.created is True
    assert result.task_id is not None
    assert result.ack is not None
    assert result.task_id.startswith("t_")
    assert result.board_slug == "hermes-agent"
    assert f"board=hermes-agent task={result.task_id}" in result.ack

    with kanban_db.connect(board="hermes-agent") as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (result.task_id,)).fetchone()
        assert task["status"] == "triage"
        assert task["assignee"] == "paul"
        assert "Original message" in task["body"]
        assert "fix gateway retries" in task["body"]
        comment = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (result.task_id,),
        ).fetchone()
        payload = json.loads(comment["body"])
        assert payload["chat_routing"]["routing_version"] == 1
        assert payload["chat_routing"]["classification"]["intent"] == "task_request"
        assert payload["chat_routing"]["board_decision"]["board_slug"] == "hermes-agent"
        assert payload["chat_routing"]["task_decision"]["task_id"] == result.task_id


def test_triage_first_bypasses_open_ended_chat_question():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    result = route_chat_to_kanban_if_needed(
        _event("what do you think about this?"),
        _enabled_config(default_mode="triage_first"),
    )

    assert result.created is False
    assert result.bypass_rule == "conversation"
    assert _task_count("inbox") == 0


def test_immediate_execution_keyword_bypasses_triage():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    result = route_chat_to_kanban_if_needed(
        _event("now: check disk usage"),
        _enabled_config(default_mode="force_triage"),
    )

    assert result.created is False
    assert result.bypass_rule == "immediate_execution"
    assert _task_count("inbox") == 0


def test_unknown_explicit_project_creates_board_only_when_policy_allows():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    disabled = route_chat_to_kanban_if_needed(
        _event("make a task for project: new launch site: prepare mobile QA"),
        _enabled_config(create_missing_boards=False),
    )
    assert disabled.created is True
    assert disabled.board_slug == "inbox"
    assert kanban_db.board_exists("new-launch-site") is False

    created = route_chat_to_kanban_if_needed(
        _event("make a task for project: new launch site: prepare mobile QA", message_id="m2"),
        _enabled_config(create_missing_boards=True, board_create_policy="explicit_project_only"),
    )
    assert created.created is True
    assert created.board_slug == "new-launch-site"
    assert kanban_db.board_exists("new-launch-site") is True


def test_duplicate_delivery_returns_existing_task_id_without_second_card():
    from gateway.kanban_triage import route_chat_to_kanban_if_needed

    config = _enabled_config()
    first = route_chat_to_kanban_if_needed(
        _event("add this to kanban for Hermes Agent: fix gateway retries"), config
    )
    second = route_chat_to_kanban_if_needed(
        _event("add this to kanban for Hermes Agent: fix gateway retries"), config
    )

    assert first.created is True
    assert second.created is True
    assert second.task_id == first.task_id
    assert _task_count("hermes-agent") == 1


@pytest.mark.asyncio
async def test_gateway_runner_short_circuits_agent_when_triage_created(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = _enabled_config()
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._running_agent_started_at = lambda *_args, **_kwargs: 0
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._is_telegram_topic_root_lobby = lambda source: False
    runner._check_slash_access = lambda source, command: None
    runner._run_agent = AsyncMock(side_effect=AssertionError("triage message reached agent"))
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError("triage message reached agent"))
    runner.session_store = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = await runner._handle_message(
        _event("add this to kanban for Hermes Agent: fix gateway retries")
    )

    assert result.startswith("Queued for triage: board=hermes-agent task=t_")
    runner._handle_message_with_agent.assert_not_called()
