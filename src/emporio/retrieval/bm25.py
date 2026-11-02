"""Okapi BM25 em Python puro, com expansão de consulta para o domínio.

Por que BM25 e não embeddings, para **este** corpus:

  * O corpus é um manual de 8 páginas (~30 chunks). Recuperação dense resolve
    descasamento de vocabulário, que é um problema de corpus grande e
    heterogêneo — aqui o cliente usa praticamente as palavras do documento
    ("troca", "frete", "parcelar", "garantia").
  * Onde o vocabulário realmente difere ("devolver" x "arrependimento",
    "abre" x "funcionamento"), o descasamento é previsível e pequeno: um mapa
    de sinônimos do domínio cobre com precisão e sem custo.
  * Elimina dependência de API de embedding ou modelo local de ~500 MB. O
    projeto roda offline, o índice é determinístico e o resultado é
    explicável — dá para apontar exatamente qual termo casou.

O caminho de evolução está no README: com catálogo de políticas maior ou
multi-documento, o certo é híbrido (BM25 + dense com reciprocal rank fusion).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from emporio.text_utils import tokenize

K1 = 1.5  # saturação de frequência de termo
B = 0.75  # normalização por tamanho do documento

# Expansão de consulta específica do atendimento. Cada chave, quando aparece na
# pergunta, injeta os termos equivalentes usados pelo manual. Resolve o
# descasamento de vocabulário sem o custo de recuperação dense.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "devolver": ("devolucao", "arrependimento", "reembolso"),
    "devolucao": ("arrependimento", "reembolso", "devolver"),
    "devolvo": ("devolucao", "arrependimento"),
    "arrependi": ("arrependimento", "devolucao", "reembolso"),
    "arrepender": ("arrependimento", "devolucao"),
    "cancelar": ("cancelamento", "devolucao", "arrependimento"),
    "estorno": ("reembolso", "devolucao"),
    "dinheiro": ("reembolso", "pagamento"),
    "trocar": ("troca", "trocas"),
    "troca": ("trocas", "defeito", "preferencia"),
    "quebrado": ("defeito", "avaria", "garantia"),
    "quebrou": ("defeito", "garantia", "avaria"),
    "defeituoso": ("defeito", "garantia"),
    "estragou": ("defeito", "garantia"),
    "abre": ("horario", "funcionamento", "atendimento"),
    "abrem": ("horario", "funcionamento"),
    "fecha": ("horario", "funcionamento"),
    "aberto": ("horario", "funcionamento"),
    "horario": ("funcionamento", "atendimento"),
    "hora": ("horario", "funcionamento"),
    "domingo": ("horario", "funcionamento", "feriados"),
    "sabado": ("horario", "funcionamento"),
    "feriado": ("feriados", "horario", "funcionamento"),
    "endereco": ("rua", "centro", "dados", "empresa", "localizacao"),
    "localizacao": ("endereco", "rua", "centro"),
    "onde": ("endereco", "rua", "localizacao"),
    "telefone": ("whatsapp", "contato", "dados"),
    "contato": ("telefone", "whatsapp", "email"),
    "frete": ("entrega", "entregas", "envio", "correios"),
    "entrega": ("frete", "entregas", "prazo", "correios"),
    "entregar": ("entrega", "frete", "prazo"),
    "envio": ("frete", "entrega", "correios"),
    "prazo": ("dias", "uteis", "entrega"),
    "chega": ("prazo", "entrega", "dias"),
    "demora": ("prazo", "entrega", "dias", "uteis"),
    "leva": ("prazo", "entrega", "dias"),
    "tempo": ("prazo", "dias", "uteis"),
    "rastreio": ("rastreamento", "codigo", "correios"),
    "rastrear": ("rastreamento", "codigo"),
    "parcelar": ("parcelamento", "parcela", "credito", "vezes"),
    "parcela": ("parcelamento", "credito"),
    "parcelado": ("parcelamento", "parcela", "credito"),
    "vezes": ("parcelamento", "parcela", "credito"),
    "cartao": ("credito", "debito", "pagamento", "bandeiras"),
    "pix": ("pagamento", "desconto", "vista"),
    "boleto": ("pagamento", "bancario", "vista"),
    "pagar": ("pagamento", "formas"),
    "pagamento": ("formas", "pix", "cartao", "boleto"),
    "desconto": ("promocao", "promocoes", "descontos"),
    "promocao": ("promocoes", "desconto", "descontos"),
    "oferta": ("promocao", "desconto"),
    "garantia": ("defeito", "fabricante", "legal"),
    "consertar": ("garantia", "defeito", "reparo"),
    "nota": ("fiscal", "pedido"),
    "acessorio": ("acessorios", "escopo", "cordas", "palhetas", "cabos"),
    "corda": ("cordas", "acessorios", "escopo"),
    "palheta": ("palhetas", "acessorios", "escopo"),
    "cabo": ("cabos", "acessorios", "escopo"),
    "pedal": ("pedais", "acessorios", "escopo"),
    "amplificador": ("amplificadores", "acessorios", "escopo"),
    "case": ("cases", "acessorios", "escopo"),
    "dados": ("privacidade", "lgpd", "protecao"),
    "privacidade": ("lgpd", "dados", "protecao"),
    "lgpd": ("privacidade", "dados", "protecao"),
    "excluir": ("exclusao", "dados", "lgpd"),
}


@dataclass
class ScoredDoc:
    index: int
    score: float
    matched_terms: tuple[str, ...]


def _synonyms_for(token: str) -> tuple[str, ...]:
    """Sinônimos do token, tolerando flexão verbal simples.

    O mapa guarda a forma mais curta ("chega", "troca"). Em vez de enumerar
    toda conjugação, tentamos variações de infinitivo/3ª pessoa. É um stemmer
    deliberadamente minúsculo: cobre "chegar/chegam", "trocar", "devolver" sem
    o risco de sobre-radicalização de um RSLP completo.
    """
    if token in SYNONYMS:
        return SYNONYMS[token]
    variants: list[str] = []
    if token.endswith("r"):  # chegar -> chega
        variants.append(token[:-1])
    if token.endswith("m"):  # chegam -> chega
        variants.append(token[:-1])
    for suffix in ("ndo", "ram", "rei", "ria"):  # chegando, chegaram, ...
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            variants.append(token[: -len(suffix)])
    for variant in variants:
        if variant in SYNONYMS:
            return SYNONYMS[variant]
    return ()


def expand_query(query: str) -> list[str]:
    """Tokens da pergunta mais os sinônimos do domínio.

    Sinônimos entram **depois** dos termos originais e sem deduplicar os
    originais, de modo que o termo que a pessoa realmente escreveu continue
    pesando mais do que o termo expandido.
    """
    tokens = tokenize(query)
    expanded = list(tokens)
    seen = set(tokens)
    for token in tokens:
        for synonym in _synonyms_for(token):
            if synonym not in seen:
                expanded.append(synonym)
                seen.add(synonym)
    return expanded


class BM25Index:
    """Índice invertido com ranqueamento Okapi BM25."""

    def __init__(self, documents: list[list[str]]) -> None:
        self.documents = documents
        self.n_docs = len(documents)
        self.doc_lengths = [len(doc) for doc in documents]
        self.avg_doc_length = (
            sum(self.doc_lengths) / self.n_docs if self.n_docs else 0.0
        )
        self.term_freqs: list[Counter[str]] = [Counter(doc) for doc in documents]

        doc_freq: Counter[str] = Counter()
        for doc in documents:
            doc_freq.update(set(doc))
        self.doc_freq = doc_freq

        # IDF pré-calculado (variante com +1 no numerador, sempre positiva —
        # evita que termo presente em quase todos os documentos receba peso
        # negativo e empurre o chunk relevante para baixo).
        self.idf: dict[str, float] = {
            term: math.log(1 + (self.n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    def score(self, query_tokens: list[str]) -> list[ScoredDoc]:
        """Pontua todos os documentos. Retorna em ordem decrescente."""
        results: list[ScoredDoc] = []
        for index in range(self.n_docs):
            freqs = self.term_freqs[index]
            length = self.doc_lengths[index] or 1
            total = 0.0
            matched: list[str] = []
            for term in query_tokens:
                freq = freqs.get(term, 0)
                if not freq:
                    continue
                idf = self.idf.get(term, 0.0)
                denominator = freq + K1 * (1 - B + B * length / (self.avg_doc_length or 1))
                total += idf * (freq * (K1 + 1)) / denominator
                matched.append(term)
            if total > 0:
                results.append(ScoredDoc(index, total, tuple(dict.fromkeys(matched))))
        results.sort(key=lambda r: (-r.score, r.index))
        return results

    def search(self, query: str, limit: int = 4) -> list[ScoredDoc]:
        return self.score(expand_query(query))[:limit]
