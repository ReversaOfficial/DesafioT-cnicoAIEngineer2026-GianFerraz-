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
                    parts.append(
                        {"functionCall": {"name": call.name, "args": call.arguments}}
                    )
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
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        payload: dict[str, Any] = {
            "contents": self._convert_messages(messages),
            "generationConfig": {"temperature": self.temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = self._convert_tools(tools)

        data = self._post(url, payload)
        return self._parse(data)

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
                tool_calls.append(
                    ToolCall(
                        id=f"gemini-{index}",
                        name=call.get("name", ""),
                        arguments=parse_arguments(call.get("args")),
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
