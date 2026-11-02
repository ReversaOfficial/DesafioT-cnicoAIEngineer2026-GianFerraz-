"""Persistência do histórico de conversa.

O desafio deixa a persistência a critério do candidato ("implemente se fizer
sentido para a experiência do usuário"). Aqui faz sentido, e por um motivo
concreto do domínio: o canal é WhatsApp (§7). Conversa de WhatsApp não começa
e termina numa sessão — o cliente pergunta o preço hoje, desaparece, e volta
amanhã com "e aquele violão, ainda tem?". Sem histórico persistido o agente
perde o fio e o cliente repete tudo.

SQLite em arquivo, não Redis nem Postgres: o volume é de atendimento de loja,
o dado é simples e a dependência é zero. O schema separa `messages` de
`tool_calls` para que o histórico de auditoria (quem consultou o quê, quando)
sobreviva à poda de contexto.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from emporio.config import settings
from emporio.llm.base import Message, ToolCall

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'cli',
    customer_ref TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    tool_calls  TEXT NOT NULL DEFAULT '',
    tool_name   TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    created_at  TEXT NOT NULL,
    name        TEXT NOT NULL,
    arguments   TEXT NOT NULL DEFAULT '{}',
    ok          INTEGER NOT NULL DEFAULT 1,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionInfo:
    session_id: str
    created_at: str
    updated_at: str
    channel: str
    title: str
    n_messages: int


class ConversationStore:
    """Leitura e escrita do histórico de conversas."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.conversations_db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- sessões -----------------------------------------------------------

    def create_session(self, *, channel: str = "cli", customer_ref: str = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        stamp = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, channel, customer_ref)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, stamp, stamp, channel, customer_ref),
            )
        return session_id

    def ensure_session(self, session_id: str | None, *, channel: str = "cli") -> str:
        """Retorna uma sessão existente ou cria uma nova."""
        if not session_id:
            return self.create_session(channel=channel)
        with self._connect() as conn:
            found = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if found:
                return session_id
            stamp = _now()
            conn.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, channel)"
                " VALUES (?, ?, ?, ?)",
                (session_id, stamp, stamp, channel),
            )
        return session_id

    def list_sessions(self, limit: int = 20) -> list[SessionInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, COUNT(m.id) AS n_messages
                  FROM sessions s
                  LEFT JOIN messages m ON m.session_id = s.session_id
                 GROUP BY s.session_id
                 ORDER BY s.updated_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SessionInfo(
                session_id=r["session_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                channel=r["channel"],
                title=r["title"],
                n_messages=r["n_messages"],
            )
            for r in rows
        ]

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # -- mensagens ---------------------------------------------------------

    def append_messages(self, session_id: str, messages: list[Message]) -> None:
        if not messages:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["last"])
            stamp = _now()
            for message in messages:
                seq += 1
                conn.execute(
                    """
                    INSERT INTO messages
                        (session_id, seq, role, content, tool_calls, tool_name,
                         tool_call_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        seq,
                        message.role,
                        message.content,
                        json.dumps(
                            [
                                {
                                    "id": c.id,
                                    "name": c.name,
                                    "arguments": c.arguments,
                                    # Precisa sobreviver à persistência: sem a
                                    # assinatura de raciocínio, uma sessão
                                    # retomada é rejeitada pelos modelos
                                    # Gemini 3.x com HTTP 400.
                                    "metadata": c.metadata,
                                }
                                for c in message.tool_calls
                            ],
                            ensure_ascii=False,
                        )
                        if message.tool_calls
                        else "",
                        message.tool_name,
                        message.tool_call_id,
                        stamp,
                    ),
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (stamp, session_id),
            )

    def load_messages(self, session_id: str) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        messages: list[Message] = []
        for row in rows:
            calls: list[ToolCall] = []
            if row["tool_calls"]:
                try:
                    calls = [
                        ToolCall(
                            id=c["id"],
                            name=c["name"],
                            arguments=c["arguments"],
                            metadata=c.get("metadata") or {},
                        )
                        for c in json.loads(row["tool_calls"])
                    ]
                except (json.JSONDecodeError, KeyError, TypeError):
                    calls = []
            messages.append(
                Message(
                    role=row["role"],
                    content=row["content"],
                    tool_calls=calls,
                    tool_name=row["tool_name"],
                    tool_call_id=row["tool_call_id"],
                )
            )
        return messages

    def transcript(self, session_id: str) -> list[dict]:
        """Só as falas de cliente e agente, para exibir ou exportar."""
        return [
            {"role": m.role, "content": m.content}
            for m in self.load_messages(session_id)
            if m.role in {"user", "assistant"} and m.content
        ]

    # -- auditoria ---------------------------------------------------------

    def record_tool_calls(self, session_id: str, invocations: list) -> None:
        if not invocations:
            return
        stamp = _now()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO tool_calls
                    (session_id, created_at, name, arguments, ok, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        stamp,
                        inv.name,
                        json.dumps(inv.arguments, ensure_ascii=False, default=str),
                        1 if inv.ok else 0,
                        inv.duration_ms,
                    )
                    for inv in invocations
                ],
            )

    def tool_usage_stats(self) -> list[dict]:
        """Agregado de uso de ferramentas — útil para avaliar o roteamento."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT name, COUNT(*) AS chamadas,
                       SUM(ok) AS sucessos,
                       ROUND(AVG(duration_ms), 1) AS media_ms
                  FROM tool_calls
                 GROUP BY name
                 ORDER BY chamadas DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]
