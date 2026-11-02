"""Índice de políticas: construção, cache em disco e consulta."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from emporio.config import settings
from emporio.retrieval.bm25 import BM25Index
from emporio.retrieval.chunker import PolicyChunk, chunk_policy_pdf, chunk_tokens

INDEX_FORMAT_VERSION = 2


@dataclass
class PolicyHit:
    """Um trecho recuperado, pronto para ir ao contexto do LLM."""

    chunk: PolicyChunk
    score: float
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "secao": self.chunk.citation,
            "pagina": self.chunk.page,
            "texto": self.chunk.text,
            "relevancia": round(self.score, 3),
        }


class PolicyIndex:
    """Busca sobre o manual de políticas."""

    def __init__(self, chunks: list[PolicyChunk]) -> None:
        self.chunks = chunks
        self._bm25 = BM25Index([chunk_tokens(c) for c in chunks])

    # -- construção / persistência -----------------------------------------

    @classmethod
    def build(cls, pdf_path: Path | None = None) -> PolicyIndex:
        pdf = pdf_path or settings.policy_pdf_path
        if not pdf.exists():
            raise FileNotFoundError(
                f"Manual de políticas não encontrado em {pdf}. "
                "Confira se data/políticas_da_loja.pdf está no repositório."
            )
        return cls(chunk_policy_pdf(pdf))

    def save(self, path: Path | None = None) -> Path:
        target = path or settings.policy_index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": INDEX_FORMAT_VERSION,
            "chunks": [c.to_dict() for c in self.chunks],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> PolicyIndex | None:
        source = path or settings.policy_index_path
        if not source.exists():
            return None
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if payload.get("format_version") != INDEX_FORMAT_VERSION:
            return None  # formato antigo: reconstrói
        return cls([PolicyChunk.from_dict(c) for c in payload["chunks"]])

    # -- consulta -----------------------------------------------------------

    def search(self, query: str, limit: int = 4) -> list[PolicyHit]:
        return [
            PolicyHit(self.chunks[s.index], s.score, s.matched_terms)
            for s in self._bm25.search(query, limit=limit)
        ]

    def get_section(self, section: str) -> list[PolicyChunk]:
        """Acesso direto por número de seção — usado quando o agente já sabe
        exatamente qual regra precisa (ex.: a calculadora de devolução)."""
        wanted = section.strip().lstrip("§").strip()
        return [c for c in self.chunks if c.section == wanted]

    def section_text(self, section: str) -> str:
        return " ".join(c.text for c in self.get_section(section))


@lru_cache(maxsize=1)
def get_policy_index() -> PolicyIndex:
    """Índice compartilhado do processo, reconstruído se o cache não existir."""
    index = PolicyIndex.load()
    if index is None:
        index = PolicyIndex.build()
        index.save()
    return index
