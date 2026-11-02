"""Serviço de atendimento: agente + persistência, em um ponto único.

CLI e API web conversam com esta classe, não com o `Agent` diretamente. Sem
isso, a lógica de sessão (carregar histórico, gravar o turno, registrar as
chamadas de ferramenta) teria duas implementações divergentes — e a da web
acabaria esquecendo metade.
"""

from __future__ import annotations

from dataclasses import dataclass

from emporio.agent.loop import Agent, AgentTurn
from emporio.config import settings
from emporio.llm.base import LLMClient
from emporio.llm.factory import build_client, describe_configuration
from emporio.memory.store import ConversationStore


@dataclass
class SessionHandle:
    session_id: str
    agent: Agent
    turns: int = 0


class AgentService:
    """Gerencia sessões de atendimento com histórico persistido."""

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        store: ConversationStore | None = None,
        persist: bool = True,
    ) -> None:
        self.client = client or build_client()
        self.store = store or (ConversationStore() if persist else None)
        self._sessions: dict[str, SessionHandle] = {}

    # ------------------------------------------------------------------
    def open_session(self, session_id: str | None = None, *, channel: str = "cli") -> str:
        """Abre uma sessão nova ou retoma uma existente, com histórico."""
        if self.store is None:
            resolved = session_id or "efemera"
            self._sessions[resolved] = SessionHandle(resolved, Agent(self.client))
            return resolved

        resolved = self.store.ensure_session(session_id, channel=channel)
        history = self.store.load_messages(resolved)
        self._sessions[resolved] = SessionHandle(
            resolved, Agent(self.client, history=history)
        )
        return resolved

    def _handle(self, session_id: str) -> SessionHandle:
        handle = self._sessions.get(session_id)
        if handle is None:
            self.open_session(session_id)
            handle = self._sessions[session_id]
        return handle

    # ------------------------------------------------------------------
    def send(self, session_id: str, message: str, idioma: str = "pt") -> AgentTurn:
        """Processa uma mensagem e persiste o turno."""
        handle = self._handle(session_id)
        turn = handle.agent.send(message, idioma=idioma)
        handle.turns += 1

        if self.store is not None:
            self.store.append_messages(session_id, turn.messages_added)
            self.store.record_tool_calls(session_id, turn.tool_invocations)
        return turn

    # ------------------------------------------------------------------
    def transcript(self, session_id: str) -> list[dict]:
        if self.store is None:
            handle = self._sessions.get(session_id)
            if handle is None:
                return []
            return [
                {"role": m.role, "content": m.content}
                for m in handle.agent.history
                if m.role in {"user", "assistant"} and m.content
            ]
        return self.store.transcript(session_id)

    def reset_session(self, session_id: str) -> str:
        """Descarta o histórico e devolve uma sessão limpa."""
        if self.store is not None:
            self.store.delete_session(session_id)
        self._sessions.pop(session_id, None)
        return self.open_session(None)

    def configuration(self) -> dict:
        config = describe_configuration()
        config["persistencia"] = (
            str(self.store.path) if self.store is not None else "desativada"
        )
        config["data_de_referencia"] = settings.now().strftime("%d/%m/%Y %H:%M")
        if settings.now_override:
            config["aviso_data"] = (
                f"EMPORIO_NOW está definido como '{settings.now_override}': o "
                "agente opera com data congelada."
            )
        return config
