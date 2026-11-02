"""Cálculo de frete e prazo de entrega (§5 do manual).

O manual separa dois regimes: região metropolitana de Campo Grande, com regra
fechada de valor e prazo, e demais cidades, atendidas por Correios/Jadlog com
cotação por CEP, peso e dimensão. Para o segundo caso o valor **não é
calculável** com os dados disponíveis — a ferramenta devolve as modalidades e
prazos e deixa explícito que o valor depende de cotação. Isso é melhor do que
estimar: o manual (§7.1) proíbe informar prazo ou preço sem consultar o
sistema, e um número inventado aqui é exatamente o que gera frustração.
"""

from __future__ import annotations

from emporio.data.db import connect, ensure_database
from emporio.data.loader import METRO_CAMPO_GRANDE
from emporio.text_utils import brl, normalize

# §5.1 — região metropolitana de Campo Grande.
FREE_SHIPPING_THRESHOLD = 500.0
FLAT_FEE_BELOW_THRESHOLD = 35.0
METRO_DELIVERY_DAYS = "1 a 3 dias úteis"

# §5.2 — demais cidades.
CARRIER_OPTIONS = (
    {"modalidade": "PAC (Correios)", "prazo_estimado": "5 a 12 dias úteis"},
    {"modalidade": "SEDEX (Correios)", "prazo_estimado": "2 a 5 dias úteis"},
    {"modalidade": "Jadlog (.package)", "prazo_estimado": "3 a 8 dias úteis"},
)

# §5.2 — itens de grande porte exigem cotação individual. Casamos por
# categoria e por palavra no nome, porque a base não tem peso nem dimensão.
BULKY_CATEGORY_IDS = (3,)  # Baterias e Percussão
BULKY_KEYWORDS = ("bateria", "piano", "contrabaixo", "acustica")


def _is_metro(cidade: str) -> bool:
    return normalize(cidade) in METRO_CAMPO_GRANDE


def _bulky_warning(product_id: int | None) -> str | None:
    if product_id is None:
        return None
    ensure_database()
    conn = connect(read_only=True)
    try:
        row = conn.execute(
            "SELECT name, name_norm, category_id FROM products WHERE product_id = ?",
            (int(product_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    bulky = row["category_id"] in BULKY_CATEGORY_IDS or any(
        word in row["name_norm"] for word in BULKY_KEYWORDS
    )
    if bulky:
        return (
            f"'{row['name']}' é instrumento de grande porte. Pode exigir frete "
            "especial com cotação individual (§5.2)."
        )
    return None


def calcular_frete(
    cidade: str,
    valor_do_pedido: float | None = None,
    product_id: int | None = None,
) -> dict:
    """Frete e prazo para uma cidade, conforme §5.

    `valor_do_pedido` só é necessário na região metropolitana, onde a regra de
    gratuidade depende do total.
    """
    cidade_limpa = (cidade or "").strip()
    if not cidade_limpa:
        return {
            "erro": "Informe a cidade de entrega para calcular o frete.",
            "sugestao": "Pergunte ao cliente para qual cidade é a entrega.",
        }

    metro = _is_metro(cidade_limpa)
    payload: dict = {
        "cidade": cidade_limpa,
        "regiao": "metropolitana de Campo Grande" if metro else "fora da região metropolitana",
        "seguro": "Todos os envios incluem seguro contra extravio e danos (§5.2).",
    }

    if metro:
        payload["prazo_estimado"] = METRO_DELIVERY_DAYS
        payload["modalidade"] = "Entrega por motoboy próprio da loja"
        payload["observacao_entrega"] = (
            "O cliente é contactado por telefone antes da entrega (§5.1)."
        )
        if valor_do_pedido is None:
            payload["regra_de_valor"] = (
                f"Frete grátis para pedidos acima de {brl(FREE_SHIPPING_THRESHOLD)}. "
                f"Abaixo disso, taxa fixa de {brl(FLAT_FEE_BELOW_THRESHOLD)} (§5.1)."
            )
            payload["valor_frete"] = None
            payload["sugestao"] = (
                "Informe o valor do pedido para saber se o frete é grátis."
            )
        elif float(valor_do_pedido) > FREE_SHIPPING_THRESHOLD:
            payload["valor_frete"] = 0.0
            payload["valor_frete_formatado"] = "grátis"
            payload["regra_de_valor"] = (
                f"Pedido de {brl(float(valor_do_pedido))} ultrapassa "
                f"{brl(FREE_SHIPPING_THRESHOLD)}: frete grátis (§5.1)."
            )
        else:
            payload["valor_frete"] = FLAT_FEE_BELOW_THRESHOLD
            payload["valor_frete_formatado"] = brl(FLAT_FEE_BELOW_THRESHOLD)
            faltam = FREE_SHIPPING_THRESHOLD - float(valor_do_pedido)
            payload["regra_de_valor"] = (
                f"Pedido de {brl(float(valor_do_pedido))} está abaixo de "
                f"{brl(FREE_SHIPPING_THRESHOLD)}: taxa fixa de "
                f"{brl(FLAT_FEE_BELOW_THRESHOLD)} (§5.1)."
            )
            if faltam > 0:
                payload["dica"] = (
                    f"Faltam {brl(faltam)} para o pedido ter frete grátis."
                )
    else:
        payload["valor_frete"] = None
        payload["modalidades"] = list(CARRIER_OPTIONS)
        payload["regra_de_valor"] = (
            "Fora da região metropolitana o valor do frete é calculado por CEP "
            "de destino, peso e dimensões do produto (§5.2) — não há tabela "
            "fixa. Informe as modalidades e prazos, e diga que o valor exato "
            "sai na finalização do pedido ou com a equipe."
        )
        payload["rastreamento"] = (
            "Todas as modalidades têm rastreamento. O código é enviado por "
            "e-mail e WhatsApp no despacho, no formato BR + 9 caracteres + BR "
            "(§5.3)."
        )

    aviso = _bulky_warning(product_id)
    if aviso:
        payload["aviso_grande_porte"] = aviso

    payload["avaria"] = (
        "Em caso de avaria o cliente deve recusar o recebimento e contatar a "
        "loja imediatamente (§5.2)."
    )
    return payload
