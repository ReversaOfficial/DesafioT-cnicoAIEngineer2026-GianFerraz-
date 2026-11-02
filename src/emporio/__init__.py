"""Agente de atendimento da Empório da Música.

Pacote organizado em camadas, de baixo para cima:

    data/       ETL dos CSVs para SQLite + relatório de qualidade
    retrieval/  indexação e busca no manual de políticas (PDF)
    tools/      ferramentas determinísticas expostas ao LLM
    llm/        adaptadores de provedores (Gemini, OpenAI-compat, Anthropic)
    agent/      laço de function calling, persona e prompts
    memory/     persistência de conversas
    cli.py      interface de terminal
    api.py      API HTTP + UI web
"""

__version__ = "1.0.0"
