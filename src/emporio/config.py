"""Configuração central do projeto.

Tudo que varia por ambiente (provedor de LLM, chaves, caminhos, data de
referência) é resolvido aqui uma única vez. O resto do código importa
`settings` e nunca lê `os.environ` diretamente — isso mantém os testes
determinísticos e facilita trocar de provedor sem caçar variáveis soltas.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Raiz do repositório: .../src/emporio/config.py -> sobe 3 níveis
ROOT_DIR = Path(__file__).resolve().parents[2]

load_dotenv(ROOT_DIR / ".env")

# Campo Grande/MS opera em UTC-4 o ano inteiro (sem horário de verão desde 2019).
STORE_TIMEZONE = ZoneInfo("America/Campo_Grande")

# Defaults de modelo por provedor. Centralizados para que o `.env` possa ficar
# com EMPORIO_LLM_MODEL vazio e ainda assim funcionar.
DEFAULT_MODELS: dict[str, str] = {
    # Pinado de propósito, para que o comportamento seja reproduzível.
    #
    # Escolhido empiricamente: o Google descontinua versões para contas novas
    # sem aviso (a 2.5-flash retorna 404 em chave nova) e os modelos mais
    # recentes vivem saturados (a 3.8-flash devolveu 503 "high demand" em três
    # tentativas). A 3.5-flash é estável, atende em ~2,5s e tem function
    # calling confiável. `emporio models` lista o que a sua chave acessa.
    "gemini": "gemini-3.5-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "google/gemini-2.5-flash",
    "ollama": "qwen2.5:7b-instruct",
}

# Variável de ambiente que carrega a chave de cada provedor.
# Cadeia de reserva por provedor.
#
# O nível gratuito do Gemini concede ~20 requisições por dia POR MODELO, e um
# agente faz 2 a 3 chamadas por turno — ou seja, uma conversa de oito mensagens
# esgota a cota de um modelo. Como cada modelo tem a sua própria cota, trocar
# de modelo ao receber 429 mantém o atendimento de pé.
#
# A ordem é por capacidade decrescente: só desce para um modelo mais simples
# quando o melhor já não responde. A troca é registrada e aparece em
# `emporio doctor`, nunca acontece em silêncio.
FALLBACK_MODELS: dict[str, tuple[str, ...]] = {
    "gemini": (
        "gemini-3-flash-preview",
        "gemini-3.6-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
    ),
}

API_KEY_ENV: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": "",  # roda local, não exige chave
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Configuração imutável resolvida a partir do ambiente."""

    # --- LLM ---------------------------------------------------------------
    provider: str = field(
        default_factory=lambda: (os.getenv("EMPORIO_LLM_PROVIDER") or "gemini").strip().lower()
    )
    model: str = field(default_factory=lambda: (os.getenv("EMPORIO_LLM_MODEL") or "").strip())
    base_url: str = field(default_factory=lambda: (os.getenv("EMPORIO_LLM_BASE_URL") or "").strip())
    temperature: float = field(default_factory=lambda: _env_float("EMPORIO_LLM_TEMPERATURE", 0.3))
    max_tool_iterations: int = field(
        default_factory=lambda: _env_int("EMPORIO_MAX_TOOL_ITERATIONS", 6)
    )
    request_timeout: float = field(default_factory=lambda: _env_float("EMPORIO_LLM_TIMEOUT", 90.0))

    # --- Caminhos ----------------------------------------------------------
    root_dir: Path = ROOT_DIR
    data_dir: Path = ROOT_DIR / "data"
    var_dir: Path = ROOT_DIR / "var"
    logs_dir: Path = ROOT_DIR / "logs"

    # --- Observabilidade ---------------------------------------------------
    log_turns: bool = field(default_factory=lambda: _env_flag("EMPORIO_LOG_TURNS", True))

    # --- Tempo -------------------------------------------------------------
    # Permite congelar "agora" para reproduzir exemplos e testes.
    now_override: str = field(default_factory=lambda: (os.getenv("EMPORIO_NOW") or "").strip())

    # ----------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.var_dir / "emporio.sqlite3"

    @property
    def policy_index_path(self) -> Path:
        return self.var_dir / "policy_index.json"

    @property
    def conversations_db_path(self) -> Path:
        return self.var_dir / "conversations.sqlite3"

    @property
    def policy_pdf_path(self) -> Path:
        return self.data_dir / "políticas_da_loja.pdf"

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")

    @property
    def fallback_models(self) -> tuple[str, ...]:
        """Modelos de reserva, sem repetir o principal.

        Desativável com `EMPORIO_LLM_FALLBACK=0` para quem quiser garantia de
        que uma única versão de modelo atendeu a conversa inteira — é o que se
        quer ao medir, e o que o eval usa.
        """
        if not _env_flag("EMPORIO_LLM_FALLBACK", True):
            return ()
        principal = self.resolved_model
        return tuple(
            m for m in FALLBACK_MODELS.get(self.provider, ()) if m != principal
        )

    @property
    def api_key(self) -> str:
        env_name = API_KEY_ENV.get(self.provider, "")
        return (os.getenv(env_name) or "").strip() if env_name else ""

    def requires_api_key(self) -> bool:
        return bool(API_KEY_ENV.get(self.provider, ""))

    def now(self) -> datetime:
        """'Agora' no fuso da loja, respeitando `EMPORIO_NOW` quando definido.

        O override existe por dois motivos práticos: os exemplos de conversa do
        repositório precisam ser reproduzíveis, e as regras de prazo (7 dias de
        arrependimento, 30 de troca, 90 de garantia) só podem ser testadas de
        forma confiável com o tempo controlado.

        A variável é lida a cada chamada, não no construtor, para que um teste
        possa congelar a data com `monkeypatch.setenv` sem recriar o objeto de
        configuração inteiro.
        """
        raw = (os.getenv("EMPORIO_NOW") or self.now_override).strip()
        if raw:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=STORE_TIMEZONE)
                except ValueError:
                    continue
            raise ValueError(
                f"EMPORIO_NOW inválido: {raw!r}. Use 'YYYY-MM-DD' ou 'YYYY-MM-DDTHH:MM'."
            )
        return datetime.now(STORE_TIMEZONE)

    def ensure_dirs(self) -> None:
        self.var_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
