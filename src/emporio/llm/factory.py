"""Seleção do provedor a partir da configuração."""

from __future__ import annotations

from emporio.config import DEFAULT_MODELS, settings
from emporio.llm.anthropic import AnthropicClient
from emporio.llm.base import LLMClient, LLMError
from emporio.llm.gemini import GeminiClient
from emporio.llm.openai_compat import OpenAICompatClient

OPENAI_COMPATIBLE = ("openai", "groq", "openrouter", "ollama")
SUPPORTED = ("gemini", *OPENAI_COMPATIBLE, "anthropic")


def build_client(
    provider: str | None = None,
    model: str | None = None,
) -> LLMClient:
    """Instancia o adaptador do provedor configurado."""
    chosen = (provider or settings.provider).strip().lower()
    chosen_model = (model or settings.resolved_model or DEFAULT_MODELS.get(chosen, "")).strip()

    if chosen not in SUPPORTED:
        raise LLMError(
            f"Provedor '{chosen}' não suportado. Opções: {', '.join(SUPPORTED)}. "
            "Ajuste EMPORIO_LLM_PROVIDER no .env."
        )
    if not chosen_model:
        raise LLMError(
            f"Nenhum modelo definido para '{chosen}'. Defina EMPORIO_LLM_MODEL no .env."
        )

    common = {
        "model": chosen_model,
        "base_url": settings.base_url,
        "temperature": settings.temperature,
        "timeout": settings.request_timeout,
    }
    if chosen == "gemini":
        return GeminiClient(
            api_key=settings.api_key,
            fallback_models=settings.fallback_models,
            **common,
        )
    if chosen == "anthropic":
        return AnthropicClient(api_key=settings.api_key, **common)
    return OpenAICompatClient(provider=chosen, api_key=settings.api_key, **common)


def describe_configuration() -> dict:
    """Resumo da configuração ativa, para o CLI e o endpoint de health.

    Nunca expõe o valor da chave — apenas se existe.
    """
    config = {
        "provedor": settings.provider,
        "modelo": settings.resolved_model,
        "temperatura": settings.temperature,
        "chave_configurada": bool(settings.api_key) or settings.provider == "ollama",
        "base_url": settings.base_url or "(default do provedor)",
        "max_iteracoes_de_ferramenta": settings.max_tool_iterations,
    }
    reservas = settings.fallback_models
    config["modelos_de_reserva"] = ", ".join(reservas) if reservas else "desativado"
    return config
