"""Adaptador do Google Gemini (API REST v1beta).

Provedor default do projeto: a chave do Google AI Studio é gratuita e não pede
cartão, então quem clona o repositório consegue rodar o agente sem custo — o
que importa num projeto que precisa ser avaliado por outra pessoa.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from emporio.llm.base import (
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    Usage,
    friendly_http_error,
    parse_arguments,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
MAX_RETRIES = 3


def _vale_trocar_de_modelo(erro: LLMError) -> bool:
    """Se o erro é do modelo, trocar resolve; se é da chave, não.

    Cota esgotada (429) e versão descontinuada (404) são específicos do modelo
    e cada modelo tem a sua própria cota gratuita. Credencial inválida (401/403)
    vale para todos, e insistir só gastaria tempo.
    """
    texto = str(erro).lower()
    return (
        "429" in texto
        or "404" in texto
        or "limite de requisi" in texto
        or "não encontrado" in texto
        or "instabilidade" in texto  # 503 "high demand" é por modelo
    )


class GeminiClient:
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "",
        temperature: float = 0.3,
        timeout: float = 90.0,
        fallback_models: tuple[str, ...] = (),
    ) -> None:
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY não definida. Pegue uma chave gratuita em "
                "https://aistudio.google.com/apikey e coloque no arquivo .env."
            )
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.fallback_models = fallback_models
        # Modelo que efetivamente atendeu a última chamada. Pode diferir de
        # `model` quando a cota do principal esgota.
        self.active_model = model
        # Troca de modelo registrada para aparecer no CLI e no log — fallback
        # silencioso esconderia que a resposta veio de um modelo mais simples.
        self.model_switches: list[tuple[str, str, str]] = []

    # -- tradução do formato neutro ----------------------------------------

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Converte os schemas para `functionDeclarations`.

        Detalhe que custa um 400 na primeira tentativa: o Gemini rejeita
        `parameters` com `properties` vazio em tipo OBJECT. Ferramenta sem
        argumento tem de omitir `parameters` por completo.
        """
        declarations = []
        for tool in tools:
            declaration: dict[str, Any] = {
                "name": tool["name"],
                "description": tool["description"],
            }
            params = tool.get("parameters") or {}
            if params.get("properties"):
                schema: dict[str, Any] = {
                    "type": params.get("type", "object"),
                    "properties": params["properties"],
                }
                if params.get("required"):
                    schema["required"] = params["required"]
                declaration["parameters"] = schema
            declarations.append(declaration)
        return [{"functionDeclarations": declarations}]

    @staticmethod
    def _convert_messages(messages: list[Message]) -> list[dict]:
        contents: list[dict] = []
        for message in messages:
            if message.role == "user":
                contents.append({"role": "user", "parts": [{"text": message.content}]})
            elif message.role == "assistant":
                parts: list[dict] = []
                if message.content:
                    parts.append({"text": message.content})
                for call in message.tool_calls:
                    part: dict[str, Any] = {
                        "functionCall": {"name": call.name, "args": call.arguments}
                    }
                    # Obrigatório nos modelos Gemini 3.x: a assinatura de
                    # raciocínio recebida com a chamada tem de voltar junto
                    # dela, ou a API responde 400 reclamando de
                    # `thought_signature` ausente.
                    signature = call.metadata.get("thoughtSignature")
                    if signature:
                        part["thoughtSignature"] = signature
                    parts.append(part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif message.role == "tool":
                # O Gemini casa o resultado pelo NOME da função, não por id.
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.tool_name,
                                    "response": {"resultado": message.content},
                                }
                            }
                        ],
                    }
                )
        return contents

    # -- chamada ------------------------------------------------------------

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "contents": self._convert_messages(messages),
            "generationConfig": {"temperature": self.temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = self._convert_tools(tools)

        # Tenta o modelo ativo e, se a cota dele acabou (ou a versão saiu do
        # ar), desce na cadeia de reserva. Cada modelo do Gemini tem cota
        # gratuita própria, então isso é o que mantém o atendimento de pé para
        # quem está no nível gratuito.
        candidatos = [self.active_model]
        candidatos += [m for m in self.fallback_models if m != self.active_model]

        ultimo_erro: LLMError | None = None
        for modelo in candidatos:
            url = f"{self.base_url}/v1beta/models/{modelo}:generateContent"
            try:
                data = self._post(url, payload)
            except LLMError as exc:
                ultimo_erro = exc
                if not _vale_trocar_de_modelo(exc):
                    raise
                continue
            if modelo != self.active_model:
                motivo = "cota esgotada ou modelo indisponível"
                self.model_switches.append((self.active_model, modelo, motivo))
                self.active_model = modelo
            return self._parse(data)

        assert ultimo_erro is not None
        raise LLMError(
            f"Todos os modelos tentados falharam ({', '.join(candidatos)}). "
            f"Último erro: {ultimo_erro}"
        ) from ultimo_erro

    def _post(self, url: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": self.api_key},
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:  # rede caiu, DNS, timeout
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 200:
                return response.json()
            # 429 e 5xx valem nova tentativa; o resto é erro definitivo.
            if response.status_code == 429 or response.status_code >= 500:
                last_error = friendly_http_error(
                    self.provider, response.status_code, response.text
                )
                time.sleep(2.0 * (attempt + 1))
                continue
            raise friendly_http_error(self.provider, response.status_code, response.text)
        raise LLMError(f"gemini: falha após {MAX_RETRIES} tentativas. {last_error}")

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        candidates = data.get("candidates") or []
        if not candidates:
            # Acontece quando o filtro de segurança bloqueia a resposta.
            feedback = data.get("promptFeedback", {})
            raise LLMError(
                "gemini: resposta sem candidatos. "
                f"promptFeedback={feedback}"
            )
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, part in enumerate(parts):
            if "text" in part:
                texts.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                metadata: dict[str, Any] = {}
                # Preservada para ser devolvida no próximo turno (ver
                # `_convert_messages`).
                signature = part.get("thoughtSignature")
                if signature:
                    metadata["thoughtSignature"] = signature
                tool_calls.append(
                    ToolCall(
                        id=f"gemini-{index}",
                        name=call.get("name", ""),
                        arguments=parse_arguments(call.get("args")),
                        metadata=metadata,
                    )
                )

        meta = data.get("usageMetadata") or {}
        return LLMResponse(
            text="".join(texts).strip(),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=int(meta.get("promptTokenCount") or 0),
                completion_tokens=int(meta.get("candidatesTokenCount") or 0),
            ),
            finish_reason=candidate.get("finishReason", ""),
        )
