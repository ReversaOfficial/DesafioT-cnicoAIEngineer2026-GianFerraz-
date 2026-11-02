"""API HTTP e interface web de chat.

A API existe por dois motivos: mostrar que o agente é consumível como serviço
(o caminho real seria um webhook de WhatsApp) e servir a UI de chat, que é
onde o atendimento se parece com o que a loja usaria de verdade.

O serviço de agente é único para todo o processo e as sessões são
identificadas por `session_id` no corpo da requisição — o mesmo histórico
persistido que a CLI usa, então dá para começar uma conversa no terminal e
continuar no navegador.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from emporio import __version__
from emporio.agent.prompts import AGENT_NAME, SAUDACAO_INICIAL
from emporio.agent.service import AgentService
from emporio.config import settings
from emporio.llm.base import LLMError

WEB_DIR = Path(__file__).parent / "web"

api = FastAPI(
    title="Empório da Música — Agente de Atendimento",
    version=__version__,
    description=(
        "Agente de atendimento ao cliente da Empório da Música. "
        "Consulta catálogo, pedidos e o manual interno de políticas."
    ),
)


@lru_cache(maxsize=1)
def get_service() -> AgentService:
    """Serviço compartilhado, criado na primeira requisição.

    Lazy de propósito: `uvicorn` deve subir mesmo sem chave de API, para que
    `/api/health` possa informar o que está faltando em vez de o processo
    morrer no import.
    """
    return AgentService()


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    mensagem: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(
        default=None, description="Omita para abrir uma sessão nova."
    )
    incluir_trace: bool = Field(
        default=False, description="Inclui as ferramentas consultadas na resposta."
    )


class ToolTrace(BaseModel):
    ferramenta: str
    argumentos: dict
    ok: bool
    duracao_ms: int


class ChatResponse(BaseModel):
    resposta: str
    session_id: str
    iteracoes: int
    duracao_ms: int
    tokens: int
    trace: list[ToolTrace] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@api.get("/api/health")
def health() -> dict:
    """Situação do serviço e da configuração — sem expor a chave."""
    from emporio.data.db import database_exists
    from emporio.llm.factory import describe_configuration

    config = describe_configuration()
    pronto = bool(config["chave_configurada"]) and database_exists()
    return {
        "status": "ok" if pronto else "configuracao_incompleta",
        "versao": __version__,
        "agente": AGENT_NAME,
        "dados_carregados": database_exists(),
        "indice_de_politicas": settings.policy_index_path.exists(),
        "llm": config,
        "data_de_referencia": settings.now().strftime("%d/%m/%Y %H:%M"),
    }


@api.get("/api/saudacao")
def saudacao() -> dict:
    return {"agente": AGENT_NAME, "mensagem": SAUDACAO_INICIAL}


@api.get("/api/tools")
def listar_ferramentas() -> dict:
    """Ferramentas disponíveis ao agente, com os schemas."""
    from emporio.tools.registry import TOOLS

    return {
        "total": len(TOOLS),
        "ferramentas": [
            {
                "nome": tool.name,
                "descricao": tool.description,
                "parametros": tool.parameters,
                "le_dado_pessoal": tool.sensitive,
            }
            for tool in TOOLS
        ],
    }


@api.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Envia uma mensagem ao agente e devolve a resposta."""
    try:
        service = get_service()
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    session_id = service.open_session(request.session_id, channel="web")
    try:
        turn = service.send(session_id, request.mensagem)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(
        resposta=turn.reply,
        session_id=session_id,
        iteracoes=turn.iterations,
        duracao_ms=turn.duration_ms,
        tokens=turn.usage.total,
        trace=[
            ToolTrace(
                ferramenta=inv.name,
                argumentos=inv.arguments,
                ok=inv.ok,
                duracao_ms=inv.duration_ms,
            )
            for inv in turn.tool_invocations
        ]
        if request.incluir_trace
        else [],
    )


@api.get("/api/session/{session_id}")
def historico(session_id: str) -> dict:
    """Transcrição de uma sessão."""
    service = get_service()
    mensagens = service.transcript(session_id)
    if not mensagens:
        raise HTTPException(status_code=404, detail="Sessão sem mensagens ou inexistente.")
    return {"session_id": session_id, "mensagens": mensagens}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


# `response_model=None`: o retorno é um Response pronto, não um modelo a
# serializar — sem isso o FastAPI tenta derivar schema da união de tipos.
@api.get("/", response_model=None, include_in_schema=False)
def index() -> Response:
    page = WEB_DIR / "index.html"
    if not page.exists():  # pragma: no cover
        return JSONResponse({"erro": "Interface web não encontrada."}, status_code=404)
    return FileResponse(page, media_type="text/html")
