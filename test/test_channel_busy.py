"""The hand-off from a channel's busy path to the dashboard turn driving a resumed session.

``dashboard/channel_busy.py`` is what a Discord, Telegram or Teams dispatcher calls
when a message arrives for a resumed ``dashboard:`` session that is mid-turn. These
tests pin its three answers against a real ``DashboardState`` and slot: nothing to do
when no dashboard turn holds the session, a queue entry carrying channel provenance
and the admission containment when one does, and a refusal (nothing queued) when the
message carries attachments the dashboard queue cannot hold.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_busy
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.channel_busy import (
    CHANNEL_ORIGIN_META_KEY,
    HANDOFF_ATTACHMENTS_REFUSED,
    HANDOFF_NOT_DASHBOARD_TURN,
    HANDOFF_QUEUED,
    channel_binding_released,
    channel_origin_address,
    dashboard_turn_in_progress,
    hand_to_dashboard_turn,
)
from kiro_crew.dashboard.chat_utils import dashboard_command_word
from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY
from kiro_crew.messaging.link import ChannelLink


def _state(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    return state


async def _running_slot(state: Any, name: str = "chat-1") -> tuple[Any, asyncio.Future]:
    """A slot whose dashboard turn is in flight: ``slot.task`` is a pending future."""
    slot = state.get_or_create_slot(name)
    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    slot.task = pending
    assert slot.running
    return slot, pending


class TestDashboardTurnInProgress:
    def test_a_non_dashboard_key_never_has_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert dashboard_turn_in_progress(state, "telegram:7:gen0") is False

    def test_no_open_slot_means_no_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert dashboard_turn_in_progress(state, "dashboard:absent") is False

    def test_an_idle_slot_is_not_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        """A channel turn on the resumed key leaves the slot idle from the
        dashboard's point of view -- it holds the session lease, not ``slot.task``
        -- so the channel keeps its own steer/queue path for that case."""
        state = _state(tmp_path, monkeypatch)
        state.get_or_create_slot("chat-1")
        assert dashboard_turn_in_progress(state, "dashboard:chat-1") is False

    @pytest.mark.asyncio
    async def test_a_live_task_is_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        _slot, pending = await _running_slot(state)
        try:
            assert dashboard_turn_in_progress(state, "dashboard:chat-1") is True
        finally:
            pending.cancel()

    def test_between_stages_counts_as_a_dashboard_turn(self, tmp_path, monkeypatch) -> None:
        """Between a plan's stages the task is gone but the plan is live; a message
        admitted then must wait in the slot queue rather than start a rival turn --
        the same predicate the peer send path reads."""
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        slot._in_stage_execution = True
        assert dashboard_turn_in_progress(state, "dashboard:chat-1") is True


class TestHandToDashboardTurn:
    def test_no_dashboard_turn_hands_nothing_off(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")

        outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "follow-up")

        assert outcome == HANDOFF_NOT_DASHBOARD_TURN
        assert slot._queue == []

    def test_no_slot_hands_nothing_off(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        assert hand_to_dashboard_turn(state, "dashboard:absent", "x") == HANDOFF_NOT_DASHBOARD_TURN
        assert hand_to_dashboard_turn(None, "dashboard:chat-1", "x") == HANDOFF_NOT_DASHBOARD_TURN

    @pytest.mark.asyncio
    async def test_a_running_dashboard_turn_takes_the_message_onto_the_slot_queue(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        slot.linked_session_key = "discord:u1:gen0"
        try:
            outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "and the weather?")
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert [q["content"] for q in slot._queue] == ["and the weather?"]
        entry = slot._queue[0]
        # An allow-listed human typed it into the conversation bound to this session,
        # and that conversation is a channel: both provenance flags, so the drain
        # keeps the LINKED exemption AND channel authority for any directive.
        assert entry.get("_directive_user_origin") is True
        assert entry.get("_directive_channel_origin") is True
        # The admission containment is stamped with ``linked`` already held, so the
        # drain's re-validation does not drop the entry for the link that made this
        # hand-off possible in the first place.
        admitted = entry["meta"][QUEUED_CONTAINMENT_META_KEY]
        assert admitted["linked"] is True
        # Announced to open dashboard clients like any queued send.
        events = [call.args[0] for call in state.broadcast_ws.call_args_list]
        assert "queue_push" in events

    @pytest.mark.asyncio
    async def test_attachments_are_refused_rather_than_queued_without_their_files(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        try:
            outcome = hand_to_dashboard_turn(
                state, "dashboard:chat-1", "see attached", has_attachments=True
            )
        finally:
            pending.cancel()

        assert outcome == HANDOFF_ATTACHMENTS_REFUSED
        assert slot._queue == []

    def test_attachments_do_not_matter_when_nothing_is_handed_off(
        self, tmp_path, monkeypatch
    ) -> None:
        """An idle slot answers "not mine" before the attachment rule: the channel's
        own queue carries attachments fine, so it must get the message."""
        state = _state(tmp_path, monkeypatch)
        state.get_or_create_slot("chat-1")
        outcome = hand_to_dashboard_turn(
            state, "dashboard:chat-1", "see attached", has_attachments=True
        )
        assert outcome == HANDOFF_NOT_DASHBOARD_TURN

    @pytest.mark.asyncio
    async def test_the_entry_goes_through_the_dashboards_own_producer(
        self, tmp_path, monkeypatch
    ) -> None:
        """One queue, one producer: the hand-off calls ``queue_for_next_turn`` rather
        than appending to the slot directly, so persistence, the crew log and the
        broadcast stay the dashboard's own."""
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        calls: list[dict[str, Any]] = []

        def _producer(st: Any, sl: Any, text: str, **kwargs: Any) -> str:
            calls.append({"state": st, "slot": sl, "text": text, **kwargs})
            return "q-1"

        monkeypatch.setattr(channel_busy, "queue_for_next_turn", _producer)
        try:
            outcome = hand_to_dashboard_turn(state, "dashboard:chat-1", "hello")
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert calls == [
            {
                "state": state,
                "slot": slot,
                "text": "hello",
                "directive_user_origin": True,
                "channel_origin": True,
                "channel_address": None,
            }
        ]


class TestTheEntryCarriesItsConversation:
    """A handed-off entry records WHERE it came from, because the drain may have to
    report a drop to that conversation after the binding it rode in on is gone."""

    @pytest.mark.asyncio
    async def test_the_conversation_rides_on_the_entry(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        try:
            outcome = hand_to_dashboard_turn(
                state,
                "dashboard:chat-1",
                "and the weather?",
                origin=ChannelLink("discord", channel_id="c1"),
            )
        finally:
            pending.cancel()

        assert outcome == HANDOFF_QUEUED
        assert slot._queue[0]["meta"][CHANNEL_ORIGIN_META_KEY] == {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
        }
        # Beside the containment snapshot, never in place of it: that snapshot is
        # the drain's own authorization input.
        assert slot._queue[0]["meta"][QUEUED_CONTAINMENT_META_KEY]

    def test_an_entry_without_the_stamp_has_no_address(self) -> None:
        assert channel_origin_address(None) is None
        assert channel_origin_address({}) is None
        # A half-written stamp is not an address: a notice cannot be sent to it.
        assert (
            channel_origin_address({CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord"}}) is None
        )

    def test_a_stamped_entry_reads_back_as_its_link(self) -> None:
        link = channel_origin_address(
            {CHANNEL_ORIGIN_META_KEY: {"channel_type": "telegram", "channel_id": "7"}}
        )
        assert link is not None
        assert (link.channel_type, link.channel_id) == ("telegram", "7")


class TestABindingReleasedWhileTheEntryWaited:
    """The binding IS the entry's reply route: a channel resumes a dashboard session
    through an inbound-capable mirror link, so a mirror that is gone at the drain
    means the conversation left the session and the message cannot be
    answered where it was sent."""

    def test_a_channel_entry_is_released_when_the_mirror_is_gone(self) -> None:
        meta = {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"}}
        assert channel_binding_released({"mirrored": False}, meta) is True

    def test_a_live_mirror_keeps_the_entry(self) -> None:
        meta = {CHANNEL_ORIGIN_META_KEY: {"channel_type": "discord", "channel_id": "c1"}}
        assert channel_binding_released({"mirrored": True}, meta) is False

    def test_a_composer_entry_is_never_released(self) -> None:
        """Composer text, a peer send and automation lose nothing when a mirror
        disappears, so this constraint is scoped to the channel hand-off alone."""
        assert channel_binding_released({"mirrored": False}, None) is False
        assert channel_binding_released({"mirrored": False}, {"sendId": "s-1"}) is False


class TestTheDrainDropsAndReportsIt:
    """The dashboard drain's own drop notice lands on the slot transcript, which the
    channel user is not reading, and its sender notice keys on a sender SLOT, which a
    channel has none of. The channel is told through its own transport instead."""

    @pytest.mark.asyncio
    async def test_the_entry_is_dropped_and_the_conversation_is_told(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _state(tmp_path, monkeypatch)
        slot, pending = await _running_slot(state)
        # Mirrored at admission (the conversation resumes this session), gone at the
        # drain (it ran `!unlink`, `!new`, or rotated away while the entry waited).
        probes = iter(["discord:c1:", ""])
        monkeypatch.setattr(
            "kiro_crew.dashboard.session_control._probe_channel_mirror",
            lambda st, sl: next(probes, ""),
        )
        hand_to_dashboard_turn(
            state,
            "dashboard:chat-1",
            "and the weather?",
            origin=ChannelLink("discord", channel_id="c1"),
        )
        assert len(slot._queue) == 1
        told: list[tuple[str, dict, str]] = []

        async def _notify(st: Any, session_key: str, origin: Any, *, reason: str) -> bool:
            told.append((session_key, origin.to_dict(), reason))
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        try:
            cr._drop_stale_admissions(state, slot)
            await asyncio.sleep(0)  # the notice is fire-and-forget
        finally:
            pending.cancel()

        assert slot._queue == []
        assert told, "the channel conversation was told nothing"
        session_key, address, reason = told[0]
        assert session_key == "dashboard:chat-1"
        assert address["channel_id"] == "c1"
        assert "left the session" in reason
        # The slot keeps its own notice too: both records exist, neither replaces
        # the other.
        assert any(
            "Queued message dropped" in (m.get("content") or "")
            for m in slot.messages
            if isinstance(m, dict)
        )


class TestChannelTextIsProseOnTheDashboard:
    """The channel's own command intercept already ran everything that conversation
    may command. What it forwarded is what its user meant the model to READ, so the
    dashboard must not re-read it as a command on the owner's authority."""

    def test_a_channel_message_naming_a_dashboard_command_is_not_one(self) -> None:
        assert dashboard_command_word("/workflow deploy prod", channel_origin=True) == ""
        assert dashboard_command_word("/goal ship it", channel_origin=True) == ""

    def test_composer_text_keeps_its_command_word(self) -> None:
        assert dashboard_command_word("/workflow deploy prod", channel_origin=False) == "/workflow"
        assert dashboard_command_word("  ", channel_origin=False) == ""
        assert dashboard_command_word("just prose", channel_origin=False) == "just"
