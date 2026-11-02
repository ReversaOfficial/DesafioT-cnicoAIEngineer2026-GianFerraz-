"""Consulta ao manual de políticas (RAG sobre o PDF)."""

from __future__ import annotations

from emporio.retrieval.index import get_policy_index
from emporio.tools.common import ToolError

MAX_CHUNKS = 5


def consultar_politicas(pergunta: str, limite: int = 3) -> dict:
    """Busca trechos do manual de políticas relevantes para a pergunta.

    Devolve o texto **com a seção de origem**, para que o agente possa citar a
    fonte. A instrução de responder apenas com base nos trechos recuperados
    está no prompt do sistema; aqui garantimos que a citação sempre acompanha
    o conteúdo.
    """
    if not (pergunta or "").strip():
        raise ToolError("Informe a pergunta a consultar no manual de políticas.")

    limite = max(1, min(int(limite or 3), MAX_CHUNKS))
    index = get_policy_index()
    hits = index.search(pergunta, limit=limite)

    if not hits:
        return {
            "encontrado": False,
            "trechos": [],
            "explicacao": (
                "Nenhum trecho do manual responde a essa pergunta. O assunto "
                "pode estar fora do escopo do manual — nesse caso, diga que vai "
                "verificar com a equipe em vez de deduzir uma regra (§10)."
            ),
        }

    return {
        "encontrado": True,
        "total": len(hits),
        "trechos": [hit.to_dict() for hit in hits],
        "instrucao": (
            "Responda apenas com o que está nestes trechos. Se a resposta não "
            "estiver aqui, diga que vai confirmar com a equipe. Cite a seção "
            "quando a regra tiver prazo, valor ou condição."
        ),
    }


def obter_secao_politica(secao: str) -> dict:
    """Retorna uma seção específica do manual, pelo número (ex.: '4.1')."""
    index = get_policy_index()
    chunks = index.get_section(secao)
    if not chunks:
        disponiveis = sorted({c.section for c in index.chunks})
        raise ToolError(
            f"Seção '{secao}' não existe no manual.",
            suggestion="Seções disponíveis: " + ", ".join(disponiveis),
        )
    return {
        "secao": chunks[0].citation,
        "pagina": chunks[0].page,
        "texto": " ".join(c.text for c in chunks),
    }
