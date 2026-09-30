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

from livekit.agents import AgentSession, ConversationItemAddedEvent, UserInputTranscribedEvent
from livekit.agents.llm import ChatMessage

logger = logging.getLogger("interview_agent")
USER_TRANSCRIPT_TOPIC = "interview.user_transcription"
_PUBLISH_TIMEOUT = 5.0


@dataclass(frozen=True)
class _Snapshot:
    turn_id: str
    text: str
    final: bool


class UserTranscriptForwarder:
    def __init__(self, session: AgentSession, participant):
        self._session = session
        self._participant = participant
        self._turn_id = uuid.uuid4().hex
        self._finals: list[str] = []
        self._pending: deque[_Snapshot] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("conversation_item_added", self._on_item)
        self._task = asyncio.create_task(self._publish())

    def _enqueue(self, text: str, *, final: bool) -> None:
        snapshot = _Snapshot(self._turn_id, text, final)
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
        text = " ".join([*self._finals, event.transcript])
        if event.is_final:
            self._finals.append(event.transcript)
        # STT-final is still interim at the conversation-turn level.
        self._enqueue(text, final=False)

    def _on_item(self, event: ConversationItemAddedEvent) -> None:
        item = event.item
        if self._closed or not isinstance(item, ChatMessage) or item.role != "user":
            return
        text = item.text_content
        if text and text.strip():
            self._enqueue(text, final=True)
        self._finals.clear()
        self._turn_id = uuid.uuid4().hex

    async def _publish(self) -> None:
        while True:
            await self._ready.wait()
            while self._pending:
                snapshot = self._pending.popleft()
                try:
                    await asyncio.wait_for(
                        self._participant.send_text(
                            snapshot.text,
                            topic=USER_TRANSCRIPT_TOPIC,
                            attributes={
                                "lk.segment_id": snapshot.turn_id,
                                "lk.transcription_final": str(snapshot.final).lower(),
                            },
                        ),
                        timeout=_PUBLISH_TIMEOUT,
                    )
                except Exception:
                    # Delivery failures must not stop the interview or log
                    # candidate text. A later snapshot/final can recover.
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
        self._ready.set()
        try:
            await asyncio.wait_for(self._task, timeout=_PUBLISH_TIMEOUT)
        except TimeoutError:
            logger.warning("user turn transcription publisher stopped before draining")
