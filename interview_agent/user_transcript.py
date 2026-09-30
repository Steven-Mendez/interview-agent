"""Forward user text by confirmed conversation turn, not by STT sentence.

RoomIO's lk.segment_id identifies an STT segment. It does not identify the
whole user reply. Keep incremental snapshots under one id until the public
conversation_item_added event confirms the exact text used by the agent.
"""

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import (
    AgentSession,
    AgentStateChangedEvent,
    ConversationItemAddedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
)
from livekit.agents.llm import ChatMessage

logger = logging.getLogger("interview_agent")
USER_TRANSCRIPT_TOPIC = "interview.user_transcription"
_PUBLISH_TIMEOUT = 5.0


@dataclass(frozen=True)
class _Snapshot:
    turn_id: str
    text: str
    final: bool
    incomplete: bool = False


class UserTranscriptForwarder:
    def __init__(self, session: AgentSession, room: rtc.Room):
        self._session = session
        # JobContext's room is not connected until session.start(). Resolve
        # its local participant only when there is actual text to publish.
        self._room = room
        self._turn_id = uuid.uuid4().hex
        self._finals: list[str] = []
        self._latest_text = ""
        self._activity_revision = 0
        self._orphan_revision: int | None = None
        self._user_speaking = session.user_state == "speaking"
        self._pending: deque[_Snapshot] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("conversation_item_added", self._on_item)
        session.on("agent_state_changed", self._on_agent_state)
        session.on("user_state_changed", self._on_user_state)
        self._task = asyncio.create_task(self._publish())

    def _enqueue(self, text: str, *, final: bool, incomplete: bool = False) -> None:
        snapshot = _Snapshot(self._turn_id, text, final, incomplete)
        # Coalesce unsent snapshots of this turn, never confirmed turns. A
        # slow publisher must not accumulate a full paragraph per STT word.
        if self._pending and self._pending[-1].turn_id == snapshot.turn_id:
            self._pending[-1] = snapshot
        else:
            self._pending.append(snapshot)
        self._ready.set()

    def _on_transcribed(self, event: UserInputTranscribedEvent) -> None:
        if self._closed or not event.transcript.strip():
            return
        self._activity_revision += 1
        text = " ".join([*self._finals, event.transcript])
        self._latest_text = text
        if event.is_final:
            self._finals.append(event.transcript)
        # STT-final is still interim at the conversation-turn level.
        self._enqueue(text, final=False)

    def _on_user_state(self, event: UserStateChangedEvent) -> None:
        self._user_speaking = event.new_state == "speaking"
        if self._user_speaking:
            self._activity_revision += 1

    def _on_agent_state(self, event: AgentStateChangedEvent) -> None:
        if event.new_state == "speaking":
            # Only text already present BEFORE this speech can be orphaned.
            # Barge-in or late STT changes the revision and invalidates it.
            self._orphan_revision = (
                self._activity_revision if self._latest_text and not self._user_speaking else None
            )

    def _on_item(self, event: ConversationItemAddedEvent) -> None:
        item = event.item
        if self._closed or not isinstance(item, ChatMessage):
            return
        text = item.text_content
        if not text or not text.strip():
            return
        if item.role == "assistant":
            orphaned = (
                not item.interrupted
                and not self._user_speaking
                and self._orphan_revision is not None
                and self._orphan_revision == self._activity_revision
            )
            self._orphan_revision = None
            # An interrupted assistant item arrives BEFORE the user item.
            # Keep that user's id and text until its authoritative commit.
            # Without an unambiguous speech boundary, also keep the text.
            if not self._latest_text or not orphaned:
                return
            self._enqueue(self._latest_text, final=False, incomplete=True)
        elif item.role == "user":
            self._enqueue(text, final=True)
        else:
            return
        self._finals.clear()
        self._latest_text = ""
        self._orphan_revision = None
        self._turn_id = uuid.uuid4().hex

    async def _publish(self) -> None:
        while True:
            await self._ready.wait()
            while self._pending:
                snapshot = self._pending.popleft()
                # A confirmed final has no newer snapshot to recover it.
                # Retry it once under the SAME id; clients already make
                # confirmed finals immutable, so successful retries are safe.
                for _ in range(2 if snapshot.final else 1):
                    try:
                        await asyncio.wait_for(
                            self._room.local_participant.send_text(
                                snapshot.text,
                                topic=USER_TRANSCRIPT_TOPIC,
                                attributes={
                                    "lk.segment_id": snapshot.turn_id,
                                    "lk.transcription_final": str(snapshot.final).lower(),
                                    "interview.incomplete": str(snapshot.incomplete).lower(),
                                },
                            ),
                            timeout=_PUBLISH_TIMEOUT,
                        )
                        break
                    except Exception:
                        # Permanent delivery failure leaves a partial marked
                        # incomplete. The committed transcript stays in DB.
                        logger.warning("could not publish user turn transcription")
            self._ready.clear()
            if self._closed:
                return

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session.off("user_input_transcribed", self._on_transcribed)
        self._session.off("conversation_item_added", self._on_item)
        self._session.off("agent_state_changed", self._on_agent_state)
        self._session.off("user_state_changed", self._on_user_state)
        self._ready.set()
        try:
            await asyncio.wait_for(self._task, timeout=2 * _PUBLISH_TIMEOUT + 0.1)
        except TimeoutError:
            logger.warning("user turn transcription publisher stopped before draining")
