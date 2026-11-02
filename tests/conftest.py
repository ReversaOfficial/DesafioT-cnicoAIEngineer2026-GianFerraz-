"""Fixtures compartilhadas.

Os testes rodam contra os **dados reais do desafio**, não contra mocks. Para
uma base deste tamanho isso é vantagem, não atalho: as asserções passam a
valer sobre os casos de verdade (categoria vazia, produto descontinuado com
estoque, promoção duplicada) em vez de sobre um cenário idealizado que nunca
pega o bug real.

Nenhum teste precisa de rede ou de chave de API: o laço do agente é exercitado
com um dublê de LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from emporio.data.db import database_exists
from emporio.data.loader import build_database
from emporio.llm.base import LLMResponse, Message, ToolCall, Usage
from emporio.retrieval.index import PolicyIndex, get_policy_index


@pytest.fixture(scope="session", autouse=True)
def _ambiente_preparado():
    """Garante banco e índice antes de qualquer teste."""
    if not database_exists():
        build_database()
    get_policy_index()
    yield


@pytest.fixture(scope="session")
def indice_politicas() -> PolicyIndex:
    return get_policy_index()


@pytest.fixture
def agora(monkeypatch):
    """Congela a data de referência. Uso: `agora("2026-02-20")`."""

    def _set(valor: str) -> None:
        monkeypatch.setenv("EMPORIO_NOW", valor)

    return _set


# ---------------------------------------------------------------------------
# Dublê de LLM
# ---------------------------------------------------------------------------


@dataclass
class FakeLLM:
    """LLM programável, para testar o laço do agente sem rede.

    Recebe uma fila de respostas e as devolve em ordem, registrando o que foi
    perguntado. Permite testar exatamente o que importa no orquestrador —
    encadeamento de ferramentas, coerção de argumentos, teto de iterações —
    de forma determinística e sem custo.
    """

    scripted: list[LLMResponse] = field(default_factory=list)
    provider: str = "fake"
    model: str = "fake-1"
    calls: list[dict] = field(default_factory=list)

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": [t["name"] for t in (tools or [])],
            }
        )
        if not self.scripted:
            return LLMResponse(text="(fim do roteiro)", usage=Usage(1, 1))
        return self.scripted.pop(0)

    # -- construtores de conveniência -----------------------------------

    @staticmethod
    def texto(conteudo: str) -> LLMResponse:
        return LLMResponse(text=conteudo, usage=Usage(10, 5))

    @staticmethod
    def chama(ferramenta: str, /, **argumentos) -> LLMResponse:
        # Primeiro parâmetro posicional-apenas: ferramentas do projeto têm
        # argumento chamado `nome`, que colidiria com um parâmetro nomeado.
        return LLMResponse(
            tool_calls=[
                ToolCall(id=f"c-{ferramenta}", name=ferramenta, arguments=argumentos)
            ],
            usage=Usage(10, 5),
        )

    @staticmethod
    def chama_varias(*pares: tuple[str, dict]) -> LLMResponse:
        return LLMResponse(
            tool_calls=[
                ToolCall(id=f"c-{i}", name=nome, arguments=args)
                for i, (nome, args) in enumerate(pares)
            ],
            usage=Usage(10, 5),
        )

    # -- inspeção --------------------------------------------------------

    def ultimo_resultado_de_ferramenta(self) -> dict:
        """Payload que a última chamada entregou de volta ao modelo."""
        for message in reversed(self.calls[-1]["messages"]):
            if message.role == "tool":
                return json.loads(message.content)
        raise AssertionError("Nenhuma mensagem de ferramenta no histórico.")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
