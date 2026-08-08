"""Tests for the pre_gateway_dispatch plugin hook.

The hook allows plugins to intercept incoming messages before auth and
agent dispatch. It runs in _handle_message and acts on returned action
dicts: {"action": "skip"|"rewrite"|"allow"}.
"""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _BusyPreDispatchAdapter(BasePlatformAdapter):
    def __init__(self, platform: Platform = Platform.WHATSAPP):
        super().__init__(PlatformConfig(enabled=True, token="test"), platform)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


def _mark_session_active(adapter: BasePlatformAdapter, event: MessageEvent) -> str:
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    return session_key


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(text: str = "hello", platform: Platform = Platform.WHATSAPP) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id="15551234567@s.whatsapp.net",
            chat_id="15551234567@s.whatsapp.net",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner(platform: Platform):
    from gateway.run import GatewayRunner

    config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True)},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    return runner, adapter


@pytest.mark.asyncio
async def test_internal_events_bypass_hook(monkeypatch):
    """Internal events (event.internal=True) skip the plugin hook entirely."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    called = {"count": 0}

    def _fake_hook(name, **kwargs):
        called["count"] += 1
        return [{"action": "skip"}]

    async def _capture(event, source, _quick_key, _run_generation):
        return "ok"

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    event = _make_event("hi")
    event.internal = True

    # Even though the hook would say skip, internal events bypass it.
    await runner._handle_message(event)
    assert called["count"] == 0

@pytest.mark.asyncio
async def test_hook_fires_without_session_store_attribute(monkeypatch):
    """A runner missing session_store still delivers the event to plugins.

    Regression: the hook kwargs read ``self.session_store`` directly, so a
    partially-initialized runner raised AttributeError inside the dispatch
    try-block — the hook never fired, and every message logged
    "pre_gateway_dispatch invocation failed: 'GatewayRunner' object has no
    attribute 'session_store'". Plugins must receive the event (with
    session_store=None) instead.
    """
    _clear_auth_env(monkeypatch)

    seen = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            seen["session_store"] = kwargs.get("session_store", "MISSING")
            return [{"action": "skip", "reason": "plugin-handled"}]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)
    del runner.session_store

    result = await runner._handle_message(_make_event("hi"))
    assert result is None
    # Hook actually fired (skip short-circuited before auth) with a None store.
    assert seen == {"session_store": None}
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_skip_bypasses_busy_session_queue(monkeypatch):
    """A plugin-handled control command must not wait behind an active turn."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "skip", "reason": "plugin-handled"}]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"

    adapter = _BusyPreDispatchAdapter()
    adapter.set_message_handler(AsyncMock(return_value=""))
    adapter.set_busy_session_pre_dispatch_handler(
        runner._handle_active_session_pre_dispatch
    )
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    event = _make_event("confirm decision-1 option-a r1")
    session_key = _mark_session_active(adapter, event)

    await adapter.handle_message(event)

    assert session_key not in adapter._pending_messages
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_runs_once_when_busy_event_is_dispatched_later(monkeypatch):
    """A queued event must not repeat pre-dispatch plugin side effects."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    calls = {"count": 0}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            calls["count"] += 1
            return [{"action": "allow"}]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        return "ok"

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    event = _make_event("ordinary follow-up")
    handled = await runner._handle_active_session_pre_dispatch(
        event,
        "agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
    )
    assert handled is False

    await runner._handle_message(event)

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_hook_failure_during_busy_path_remains_retryable(monkeypatch):
    """A transient busy-path hook error must not mark the event completed."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    calls = {"count": 0}

    def _fake_hook(name, **kwargs):
        if name != "pre_gateway_dispatch":
            return []
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary hook failure")
        return [{"action": "skip", "reason": "plugin-handled"}]

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "queue"

    event = _make_event("confirm decision-1 option-a r1")
    handled = await runner._handle_active_session_pre_dispatch(
        event,
        "agent:main:whatsapp:dm:15551234567@s.whatsapp.net",
    )
    assert handled is False

    retry_result = await runner._handle_message(event)

    assert retry_result is None
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_busy_hook_rewrite_rechecks_command_bypass(monkeypatch):
    """A rewrite to /status must be dispatched inline, not queued as text."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "rewrite", "text": "/status"}]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    adapter = _BusyPreDispatchAdapter()
    adapter.set_message_handler(AsyncMock(return_value="status-ok"))
    adapter.set_busy_session_pre_dispatch_handler(
        runner._handle_active_session_pre_dispatch
    )
    adapter.set_busy_session_handler(AsyncMock(return_value=True))

    event = _make_event("ordinary text")
    session_key = _mark_session_active(adapter, event)

    await adapter.handle_message(event)

    assert event.text == "/status"
    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_busy_hook_respects_ignored_slack_channel(monkeypatch):
    """Ignored Slack channels must stop before lifecycle/plugin dispatch."""
    runner, _adapter = _make_runner(Platform.SLACK)
    runner.config.platforms[Platform.SLACK].extra["ignored_channels"] = ["C_PRD"]
    runner._startup_restore_in_progress = False

    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("hook should not run")
        ),
    )

    event = MessageEvent(
        text="confirm decision-1 option-a r1",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            user_id="U_USER",
            chat_id="C_PRD",
            chat_type="group",
        ),
    )

    handled = await runner._handle_active_session_pre_dispatch(event, "slack-session")

    assert handled is True


@pytest.mark.asyncio
async def test_profile_busy_hook_uses_profile_runtime_scope(monkeypatch, tmp_path):
    """Multiplex busy hooks must run under the event's owning profile."""
    import gateway.run as gateway_run

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    profile_home = tmp_path / "coder"
    active_scopes = []
    seen = {}

    @contextmanager
    def _fake_scope(path):
        active_scopes.append(Path(path))
        try:
            yield
        finally:
            active_scopes.pop()

    async def _capture(event, session_key):
        seen["profile"] = event.source.profile
        seen["scope"] = active_scopes[-1] if active_scopes else None
        seen["session_key"] = session_key
        return False

    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", _fake_scope)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
    runner._handle_active_session_pre_dispatch = _capture

    handler = runner._make_profile_busy_session_pre_dispatch_handler("coder")
    event = _make_event("hello")

    handled = await handler(event, "session-1")

    assert handled is False
    assert seen == {
        "profile": "coder",
        "scope": profile_home,
        "session_key": "session-1",
    }
