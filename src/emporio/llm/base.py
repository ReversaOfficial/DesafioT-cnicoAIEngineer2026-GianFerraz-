"""Contrato comum dos provedores de LLM.

O agente conversa com esta interface, nunca com um SDK. Trocar de provedor é
mudar uma variável de ambiente, e o laço do agente não sabe se está falando
com Gemini, OpenAI, Anthropic ou um modelo local no Ollama.

Por que REST direto em vez dos SDKs oficiais: três adaptadores somam ~250
linhas, não trazem árvore de dependências, não quebram em atualização de SDK e
deixam o protocolo de function calling visível — o que importa quando o
objetivo é demonstrar como a orquestração funciona, não escondê-la.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Falha ao chamar o provedor, já com mensagem acionável."""


@dataclass
class ToolCall:
    """Pedido do modelo para executar uma ferramenta."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Metadados opacos do provedor que precisam voltar intactos no histórico.
    # O campo é neutro de propósito: só o adaptador sabe o que guarda ali.
    # Caso concreto: os modelos Gemini 3.x exigem que o `thoughtSignature`
    # recebido na chamada de função seja devolvido junto dela na próxima
    # requisição, sob pena de HTTP 400.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """Mensagem neutra de provedor.

    `role` é um de: 'user', 'assistant', 'tool'. Mensagens de ferramenta
    carregam `tool_call_id` e `tool_name` porque OpenAI casa resultado por id
    e Gemini casa por nome — o formato neutro guarda os dois.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    tool_name: str = ""

    def to_log(self) -> dict:
        payload: dict[str, Any] = {"role": self.role}
        if self.content:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [
                {"name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ]
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        return payload


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """Interface implementada por cada adaptador."""

    provider: str
    model: str

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Normaliza argumentos de tool call.

    OpenAI devolve string JSON, Gemini e Anthropic devolvem objeto. Modelos
    menores às vezes devolvem JSON malformado ou vazio — nesse caso entregamos
    dicionário vazio e deixamos a validação da ferramenta reclamar com uma
    mensagem útil, em vez de estourar o laço do agente.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def friendly_http_error(provider: str, status: int, body: str) -> LLMError:
    """Converte erro HTTP em orientação acionável.

    Chave errada e cota estourada são os dois tropeços de quem roda o projeto
    pela primeira vez; a mensagem diz o que fazer em vez de despejar o JSON.
    """
    snippet = body.strip()[:400]
    if status in (401, 403):
        return LLMError(
            f"{provider}: credencial rejeitada (HTTP {status}). Verifique a "
            f"chave de API no arquivo .env.\nResposta: {snippet}"
        )
    if status == 429:
        return LLMError(
            f"{provider}: limite de requisições atingido (HTTP 429). Aguarde "
            f"alguns segundos ou troque de modelo/provedor.\nResposta: {snippet}"
        )
    if status == 404:
        # O Google descontinua versões para contas novas sem aviso. Este é o
        # erro que mais custa tempo a quem clona o projeto meses depois, então
        # a mensagem aponta o comando que resolve.
        dica = (
            "\nRode `emporio models` para ver o que a sua chave acessa e "
            "ajuste EMPORIO_LLM_MODEL no .env."
            if provider == "gemini"
            else ""
        )
        return LLMError(
            f"{provider}: modelo não encontrado (HTTP 404). Confirme o valor de "
            f"EMPORIO_LLM_MODEL.{dica}\nResposta: {snippet}"
        )
    if status >= 500:
        return LLMError(
            f"{provider}: instabilidade no provedor (HTTP {status}). Tente "
            f"novamente.\nResposta: {snippet}"
        )
    return LLMError(f"{provider}: HTTP {status}.\nResposta: {snippet}")
