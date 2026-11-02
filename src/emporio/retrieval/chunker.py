"""Manual de políticas (PDF) -> chunks com rótulo de seção.

O chunking é **consciente da estrutura do documento**, e não de tamanho fixo.
Motivo: o agente cita a política para o cliente ("conforme o §4.1 do nosso
manual"), então a fronteira do chunk precisa coincidir com a fronteira da
regra. Um corte cego a cada N caracteres separa "7 dias corridos" do "Direito
de Arrependimento" e produz resposta confiante e errada.

O sumário é declarado explicitamente em `OUTLINE`. É uma escolha consciente:
o corpus é um único manual interno estável (v2.1, 8 páginas), e um sumário
explícito dá fronteiras exatas e citáveis — algo que heurística de regex sobre
texto extraído de PDF não garante. O teste `test_retrieval.py` falha se
qualquer seção deixar de ser encontrada, então uma revisão do PDF quebra o
build de forma visível em vez de degradar em silêncio. Seções não previstas
caem no fallback por página.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

from emporio.text_utils import fold_preserving, tokenize

# Sumário do manual (v2.1 — Junho de 2025).
OUTLINE: list[tuple[str, str]] = [
    ("1", "Sobre a Empório da Música"),
    ("1.1", "Missão"),
    ("1.2", "Dados da Empresa"),
    ("2", "Horário de Funcionamento"),
    ("3", "Formas de Pagamento"),
    ("3.1", "Regras de Parcelamento"),
    ("4", "Política de Trocas e Devoluções"),
    ("4.1", "Direito de Arrependimento (Compras Online)"),
    ("4.2", "Trocas por Defeito"),
    ("4.3", "Trocas por Preferência"),
    ("4.4", "Itens Não Elegíveis para Troca"),
    ("5", "Política de Frete e Entregas"),
    ("5.1", "Entregas na Região Metropolitana de Campo Grande"),
    ("5.2", "Entregas para Outras Cidades"),
    ("5.3", "Código de Rastreamento"),
    ("6", "Promoções e Descontos"),
    ("6.1", "Tipos de Promoção"),
    ("6.2", "Regras de Promoções"),
    ("7", "Atendimento via WhatsApp"),
    ("7.1", "Diretrizes de Atendimento"),
    ("7.2", "Fluxo de Atendimento Padrão"),
    ("7.3", "Situações Especiais"),
    ("8", "Garantia"),
    ("8.1", "Garantia Legal"),
    ("8.2", "Garantia do Fabricante"),
    ("8.3", "O que Não Cobre a Garantia"),
    ("9", "Privacidade e Proteção de Dados"),
    ("10", "Disposições Finais"),
]

# Cabeçalho/rodapé repetidos em toda página — puro ruído para o índice.
_BOILERPLATE = (
    re.compile(r"Empório\s+da\s+Música\s+Manual\s+de\s+Políticas\s+e\s+Procedimentos", re.I),
    re.compile(r"Página\s+\d+", re.I),
)

# Acima disso a seção é fatiada; abaixo fica inteira.
MAX_CHUNK_CHARS = 1100
MIN_CHUNK_CHARS = 120


@dataclass
class PolicyChunk:
    """Um trecho citável do manual."""

    chunk_id: str
    section: str  # "4.1"
    title: str  # "Direito de Arrependimento (Compras Online)"
    text: str
    page: int
    part: int = 1
    n_parts: int = 1

    @property
    def citation(self) -> str:
        """Rótulo curto que o agente usa ao citar a fonte."""
        return f"§{self.section} {self.title}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PolicyChunk:
        return cls(**data)


def extract_pages(pdf_path: Path) -> list[str]:
    """Texto de cada página, com espaçamento reconstruído.

    A extração do pypdf devolve este PDF praticamente um token por linha. O
    `re.sub` abaixo recompõe o fluxo em texto corrido — o layout visual é
    perdido (inclusive as tabelas, que viram sequências de células), mas o
    conteúdo fica inteiro e buscável, que é o que a recuperação precisa.
    """
    reader = PdfReader(str(pdf_path))
    pages: list[str] = []
    for page in reader.pages:
        raw = page.extract_text() or ""
        text = re.sub(r"\s+", " ", raw).strip()
        for pattern in _BOILERPLATE:
            text = pattern.sub(" ", text)
        pages.append(re.sub(r"\s+", " ", text).strip())
    return pages


def _locate_sections(flat: str) -> list[tuple[str, str, int]]:
    """Posição inicial de cada seção do sumário no texto corrido.

    A busca roda sobre o texto *dobrado* (minúsculo, sem acento, mesmo
    comprimento), então o índice encontrado vale no texto original.
    """
    folded = fold_preserving(flat)
    found: list[tuple[str, str, int]] = []
    cursor = 0
    for number, title in OUTLINE:
        folded_title = fold_preserving(title)
        # Tenta com o número ("4.1 direito de..."), que é como aparece no
        # documento, e cai para o título puro se a numeração vier separada.
        candidates = (
            fold_preserving(f"{number}. {title}"),
            fold_preserving(f"{number} {title}"),
            folded_title,
        )
        position = -1
        for candidate in candidates:
            position = folded.find(candidate, cursor)
            if position != -1:
                break
        if position == -1:
            # Última tentativa: a seção pode estar antes do cursor se o título
            # aparecer também no sumário/capa.
            position = folded.find(folded_title)
        if position == -1:
            continue
        found.append((number, title, position))
        cursor = position + 1
    return found


def _split_long(text: str) -> list[str]:
    """Fatia uma seção longa em partes, preferindo quebrar em bullet ou frase."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    # Bullets do manual usam "•"; quando existem, são a fronteira natural.
    if text.count("•") >= 2:
        head, _, rest = text.partition("•")
        pieces = [p.strip() for p in rest.split("•") if p.strip()]
        parts: list[str] = []
        current = head.strip()
        for piece in pieces:
            candidate = f"{current} • {piece}".strip()
            if len(candidate) > MAX_CHUNK_CHARS and len(current) >= MIN_CHUNK_CHARS:
                parts.append(current)
                current = f"{head.strip()} • {piece}"  # repete o lead-in por contexto
            else:
                current = candidate
        if current.strip():
            parts.append(current.strip())
        return parts

    # Sem bullets: janela por frase.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > MAX_CHUNK_CHARS and len(current) >= MIN_CHUNK_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current.strip():
        parts.append(current.strip())
    return parts


def chunk_policy_pdf(pdf_path: Path) -> list[PolicyChunk]:
    """Lê o PDF e devolve os chunks rotulados por seção."""
    pages = extract_pages(pdf_path)

    # Texto corrido + mapa de offset -> página, para citar a página de origem.
    page_offsets: list[tuple[int, int]] = []
    buffer: list[str] = []
    cursor = 0
    for index, page_text in enumerate(pages, start=1):
        page_offsets.append((index, cursor))
        buffer.append(page_text)
        cursor += len(page_text) + 1
    flat = " ".join(buffer)

    located = _locate_sections(flat)
    if not located:
        # Fallback: uma seção por página. Mantém o sistema funcional se o
        # documento for reestruturado.
        return [
            PolicyChunk(
                chunk_id=f"page-{i}",
                section=f"p.{i}",
                title=f"Página {i} do manual",
                text=text,
                page=i,
            )
            for i, text in enumerate(pages, start=1)
            if len(text) >= MIN_CHUNK_CHARS
        ]

    chunks: list[PolicyChunk] = []
    # Seções de nível superior costumam ter só o título antes da primeira
    # subseção (ex.: "5. Política de Frete e Entregas" seguido direto de 5.1).
    # Em vez de descartar esse trecho — o que apagaria do índice as palavras do
    # título do capítulo — ele é carregado para o chunk seguinte, que passa a
    # ter o contexto do pai.
    pending = ""
    for i, (number, title, start) in enumerate(located):
        end = located[i + 1][2] if i + 1 < len(located) else len(flat)
        body = flat[start:end].strip()
        if len(body) < MIN_CHUNK_CHARS and i + 1 < len(located):
            pending = f"{pending} {body}".strip()
            continue
        if pending:
            body = f"{pending} {body}".strip()
            pending = ""
        page = 1
        for page_number, offset in page_offsets:
            if start >= offset:
                page = page_number
        parts = _split_long(body)
        for part_index, part_text in enumerate(parts, start=1):
            chunks.append(
                PolicyChunk(
                    chunk_id=f"{number}-{part_index}",
                    section=number,
                    title=title,
                    text=part_text,
                    page=page,
                    part=part_index,
                    n_parts=len(parts),
                )
            )

    # Capa / dados de contato ficam antes da seção 1 e contêm endereço e
    # telefone — informação muito pedida no atendimento.
    preamble = flat[: located[0][2]].strip()
    if len(preamble) >= MIN_CHUNK_CHARS:
        chunks.insert(
            0,
            PolicyChunk(
                chunk_id="0-1",
                section="0",
                title="Identificação e contato da loja (capa do manual)",
                text=preamble,
                page=1,
            ),
        )
    return chunks


def chunk_tokens(chunk: PolicyChunk) -> list[str]:
    """Tokens indexados de um chunk.

    O título entra duas vezes de propósito: quem pergunta "qual o horário de
    funcionamento" usa as palavras do título, não do corpo. É um boost de
    campo barato, equivalente ao peso por campo de um índice invertido real.
    """
    return tokenize(f"{chunk.title} {chunk.title} {chunk.text}")
