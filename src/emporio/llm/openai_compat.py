"""Adaptador para endpoints compatíveis com a API da OpenAI.

Um adaptador cobre quatro provedores — OpenAI, Groq, OpenRouter e Ollama —
porque todos expõem `/v1/chat/completions` com o mesmo contrato de function
calling. É o melhor retorno por linha de código desta camada.
"""

from __future__ import annotations

import json
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

BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}
MAX_RETRIES = 3


class OpenAICompatClient:
    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        *,
        base_url: str = "",
        temperature: float = 0.3,
        timeout: float = 90.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or BASE_URLS.get(provider, BASE_URLS["openai"])).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        # Ollama roda local e não autentica; os demais exigem chave.
        if provider != "ollama" and not api_key:
            raise LLMError(
                f"Chave de API não definida para o provedor '{provider}'. "
                "Preencha o .env conforme o .env.example."
            )

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _convert_messages(system: str, messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for message in messages:
            if message.role == "user":
                out.append({"role": "user", "content": message.content})
            elif message.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant"}
                entry["content"] = message.content or None
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                out.append(entry)
            elif message.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
        return out

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(system, messages),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "openrouter":
            # O OpenRouter pede identificação do app; sem isso há rate limit menor.
            headers["HTTP-Referer"] = "https://github.com/"
            headers["X-Title"] = "Emporio da Musica Agent"

        data = self._post(f"{self.base_url}/chat/completions", payload, headers)
        return self._parse(data)

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
            except httpx.HTTPError as exc:
                last_error = exc
                if self.provider == "ollama":
                    raise LLMError(
                        "Não consegui falar com o Ollama em "
                        f"{self.base_url}. Ele está rodando? (`ollama serve`)"
                    ) from exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 or response.status_code >= 500:
                last_error = friendly_http_error(
                    self.provider, response.status_code, response.text
                )
                time.sleep(2.0 * (attempt + 1))
                continue
            raise friendly_http_error(self.provider, response.status_code, response.text)
        raise LLMError(f"{self.provider}: falha após {MAX_RETRIES} tentativas. {last_error}")

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Resposta sem 'choices': {str(data)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}

        tool_calls = [
            ToolCall(
                id=call.get("id") or f"call-{i}",
                name=(call.get("function") or {}).get("name", ""),
                arguments=parse_arguments((call.get("function") or {}).get("arguments")),
            )
            for i, call in enumerate(message.get("tool_calls") or [])
        ]

        usage = data.get("usage") or {}
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
            finish_reason=choice.get("finish_reason", ""),
        )
