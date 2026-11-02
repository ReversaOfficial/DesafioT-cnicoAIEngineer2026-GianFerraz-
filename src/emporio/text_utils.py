"""Normalização de texto em português.

Usada tanto pelo ETL (colunas `*_norm`) quanto pelo índice de políticas. Ficar
em um módulo único garante que indexação e consulta apliquem exatamente a mesma
transformação — divergência aqui é causa clássica de "o documento existe mas a
busca não acha".
"""

from __future__ import annotations

import re
import unicodedata

# Stopwords de português reduzidas ao que realmente aparece em perguntas de
# atendimento. Lista curta de propósito: cortar demais prejudica consultas
# curtas como "a loja abre?".
STOPWORDS: frozenset[str] = frozenset(
    """
    a as ao aos à às o os do dos da das de pelo pela pelos pelas no na nos nas
    um uma uns umas e ou mas que qual quais quando como onde para por com sem
    sobre em se eu me meu minha meus minhas voce vocês vc seu sua seus suas
    nos nosso nossa é sao ser tem ter tenho temos foi era esta estao estou
    ja mais menos muito tambem so ate apos antes isso isto aquilo aquele
    ha haver vai vou pode podem posso preciso queria quero gostaria
    """.split()
)

_NON_WORD = re.compile(r"[^0-9a-z]+")


def strip_accents(text: str) -> str:
    """Remove diacríticos preservando os caracteres base ('violão' -> 'violao')."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados. Base de toda comparação."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", strip_accents(text).lower()).strip()


def tokenize(text: str, *, drop_stopwords: bool = True, min_len: int = 2) -> list[str]:
    """Quebra o texto em tokens normalizados.

    Mantém números com 1 dígito (preços como "7 dias" importam) mas descarta
    letras isoladas, que só geram ruído no BM25.
    """
    raw = _NON_WORD.split(normalize(text))
    tokens: list[str] = []
    for tok in raw:
        if not tok:
            continue
        if tok.isdigit():
            tokens.append(tok)
            continue
        if len(tok) < min_len:
            continue
        if drop_stopwords and tok in STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


def digits_only(text: str) -> str:
    """Extrai apenas dígitos — usado para casar telefone em qualquer formatação."""
    return re.sub(r"\D+", "", text or "")


def brl(value: float) -> str:
    """Formata um valor no padrão monetário brasileiro: 1234.5 -> 'R$ 1.234,50'."""
    inteiro, _, centavos = f"{float(value):,.2f}".partition(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"
