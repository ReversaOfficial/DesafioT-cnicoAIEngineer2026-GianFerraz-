"""Consulta de pedidos e elegibilidade de troca/devolução.

Duas decisões de projeto moram aqui.

**1. Verificação antes de revelar dados do pedido.** O §9 do manual trata de
LGPD, e `consultar_pedido` expõe nome, telefone, endereço de entrega e
histórico de compra. Um número de pedido é sequencial e trivial de adivinhar —
então a ferramenta exige que o cliente confirme nome ou telefone antes de
devolver qualquer dado pessoal. Sem confirmação ela não erra nem vaza: devolve
um pedido de verificação que o agente repassa ao cliente.

**2. Prazo é aritmética, não interpretação.** "Posso devolver?" depende de
contar dias corridos a partir de uma data, cruzar com três prazos diferentes
(7 de arrependimento, 30 de troca por defeito, 90 de garantia legal) e saber
qual se aplica ao motivo alegado. Pedir isso a um LLM é convidar erro
silencioso. `verificar_elegibilidade_devolucao` faz a conta e devolve o
veredito com a data-limite explícita e a seção citada.
"""

from __future__ import annotations

from datetime import date

from emporio.data.db import connect, ensure_database
from emporio.text_utils import brl, digits_only, normalize
from emporio.tools.common import (
    ToolError,
    add_business_days,
    days_between,
    format_date_pt,
    parse_iso_date,
)

# Prazos do manual, em dias corridos.
PRAZO_ARREPENDIMENTO_DIAS = 7  # §4.1
PRAZO_TROCA_PREFERENCIA_DIAS = 7  # §4.3
PRAZO_TROCA_DEFEITO_DIAS = 30  # §4.2
PRAZO_GARANTIA_LEGAL_DIAS = 90  # §8.1
PRAZO_REEMBOLSO_DIAS_UTEIS = 10  # §4.1

STATUS_DESCRICAO = {
    "pending": "aguardando confirmação do pagamento",
    "confirmed": "pagamento confirmado, pedido em preparação",
    "shipped": "despachado, em transporte",
    "delivered": "entregue",
    "cancelled": "cancelado",
}

MOTIVOS_VALIDOS = ("arrependimento", "defeito", "preferencia")

# §4.4 — produtos de liquidação com aviso explícito de "venda final" não são
# elegíveis. A base não registra esse aviso, apenas o nome da campanha; então
# campanhas de liquidação disparam uma verificação humana em vez de um
# veredito automático.
CAMPANHAS_LIQUIDACAO = ("liquidacao", "queima de estoque")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _fetch_order(order_id: int):
    ensure_database()
    conn = connect(read_only=True)
    try:
        order = conn.execute(
            """
            SELECT o.*, c.name AS customer_name, c.phone AS customer_phone,
                   c.phone_digits, c.name_norm AS customer_name_norm,
                   c.city AS customer_city, c.email AS customer_email,
                   c.is_metro_cg
              FROM orders o
              JOIN customers c ON c.customer_id = o.customer_id
             WHERE o.order_id = ?
            """,
            (int(order_id),),
        ).fetchone()
        if order is None:
            return None, []
        items = conn.execute(
            """
            SELECT oi.quantity, p.product_id, p.name, p.price_brl, p.category_id,
                   p.availability, p.status AS product_status
              FROM order_items oi
              JOIN products p ON p.product_id = oi.product_id
             WHERE oi.order_id = ?
            """,
            (int(order_id),),
        ).fetchall()
        return order, items
    finally:
        conn.close()


def _verifica_identidade(order, confirmacao: str) -> bool:
    """Confere se `confirmacao` corresponde ao titular do pedido.

    Aceita telefone em qualquer formatação (compara os 8 últimos dígitos, que
    é o que a pessoa costuma lembrar) ou o nome — completo, ou primeiro e
    último sobrenome.
    """
    texto = (confirmacao or "").strip()
    if not texto:
        return False

    digitos = digits_only(texto)
    if len(digitos) >= 8 and digitos[-8:] == order["phone_digits"][-8:]:
        return True

    informado = normalize(texto)
    if not informado:
        return False
    cadastrado = order["customer_name_norm"]
    if informado == cadastrado:
        return True
    # "Lucas Silva" deve casar com "Lucas Mendes da Silva".
    partes = [p for p in informado.split() if len(p) > 2]
    if len(partes) >= 2 and all(p in cadastrado for p in partes):
        return True
    return False


def _data_de_recebimento(order) -> tuple[date | None, str]:
    """Data usada como "recebimento do produto" e a ressalva correspondente.

    A base não registra a data real de entrega — apenas `estimated_delivery`.
    Usamos essa data como referência e devolvemos a ressalva junto, para o
    agente poder ser transparente sobre a aproximação.
    """
    estimada = parse_iso_date(order["estimated_delivery"])
    if estimada:
        return estimada, (
            "A base registra apenas a data de entrega *estimada*, não a data "
            "real de recebimento. O cálculo usa a estimada; se o cliente "
            "afirmar que recebeu em outra data, a equipe deve reconferir."
        )
    pedido = parse_iso_date(order["order_date"])
    return pedido, (
        "Este pedido não tem data de entrega registrada. O cálculo usa a data "
        "do pedido como referência, o que é conservador."
    )


def consultar_pedido(order_id: int, confirmacao: str = "") -> dict:
    """Situação de um pedido. Exige confirmação de identidade (§9 / LGPD).

    `confirmacao` deve ser o nome ou o telefone do titular do pedido.
    """
    order, items = _fetch_order(order_id)
    if order is None:
        raise ToolError(
            f"Não encontrei o pedido {order_id} no sistema.",
            suggestion=(
                "Confirme o número com o cliente. Se ele não tiver o número, "
                "use identificar_cliente pelo telefone para listar os pedidos."
            ),
        )

    if not _verifica_identidade(order, confirmacao):
        # Nenhum dado pessoal sai daqui até a confirmação.
        return {
            "verificacao_necessaria": True,
            "order_id": int(order_id),
            "motivo": (
                "Por proteção de dados (§9), preciso confirmar a identidade "
                "antes de informar os detalhes do pedido."
            ),
            "como_proceder": (
                "Peça ao cliente o nome completo OU o telefone cadastrado no "
                "pedido e chame esta ferramenta novamente passando o valor em "
                "`confirmacao`. Não revele nenhum dado do pedido antes disso."
            ),
            "confirmacao_recebida": bool(confirmacao),
        }

    itens = [
        {
            "product_id": i["product_id"],
            "nome": i["name"],
            "quantidade": i["quantity"],
            "preco_tabela_atual": round(float(i["price_brl"]), 2),
        }
        for i in items
    ]

    status = order["status"]
    payload: dict = {
        "order_id": order["order_id"],
        "cliente": order["customer_name"],
        "cidade": order["customer_city"],
        "data_do_pedido": format_date_pt(parse_iso_date(order["order_date"])),
        "status": status,
        "status_descricao": STATUS_DESCRICAO.get(status, status),
        "valor_total": round(float(order["total_brl"]), 2),
        "valor_total_formatado": brl(float(order["total_brl"])),
        "forma_de_pagamento": order["payment_method"],
        "itens": itens,
    }

    if order["estimated_delivery"]:
        payload["previsao_de_entrega"] = format_date_pt(
            parse_iso_date(order["estimated_delivery"])
        )
    if order["tracking_code"]:
        payload["codigo_de_rastreio"] = order["tracking_code"]
        payload["onde_rastrear"] = (
            "O código pode ser consultado no site dos Correios ou da Jadlog (§5.3)."
        )
    elif status in {"shipped", "delivered"}:
        # Inconsistência real da base: há pedido despachado sem código.
        payload["codigo_de_rastreio"] = None
        payload["observacao_rastreio"] = (
            "Este pedido não tem código de rastreio registrado no sistema. "
            "Não invente um código: informe que vai verificar com a equipe."
        )
    else:
        payload["observacao_rastreio"] = (
            "O código de rastreio é gerado apenas no despacho do pedido (§5.3)."
        )

    if order["notes"]:
        payload["observacoes_internas"] = order["notes"]

    # O total da base prevalece; o agente não recalcula pedido fechado.
    payload["nota_sobre_o_total"] = (
        "Valor conforme registrado na transação. Não recalcule a partir do "
        "preço de tabela atual: preços e promoções podem ter mudado desde a compra."
    )
    return payload


def identificar_cliente(telefone: str = "", nome: str = "") -> dict:
    """Localiza um cliente por telefone ou nome e lista seus pedidos.

    Serve ao §7.2 (cumprimentar pelo nome) e ao caso em que o cliente não tem
    o número do pedido em mãos. Só devolve dados quando a identificação é
    inequívoca: vários homônimos geram pedido de desambiguação, não uma lista
    com dados de pessoas diferentes.
    """
    if not telefone.strip() and not nome.strip():
        raise ToolError("Informe o telefone ou o nome do cliente.")

    ensure_database()
    conn = connect(read_only=True)
    try:
        rows = []
        if telefone.strip():
            digitos = digits_only(telefone)
            if len(digitos) >= 8:
                rows = conn.execute(
                    "SELECT * FROM customers WHERE substr(phone_digits, -8) = ?",
                    (digitos[-8:],),
                ).fetchall()
        if not rows and nome.strip():
            alvo = normalize(nome)
            rows = conn.execute(
                "SELECT * FROM customers WHERE name_norm = ?", (alvo,)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM customers WHERE name_norm LIKE ? LIMIT 6",
                    (f"%{alvo}%",),
                ).fetchall()

        if not rows:
            return {
                "encontrado": False,
                "explicacao": (
                    "Nenhum cliente com esse telefone ou nome no cadastro. "
                    "Pode ser primeiro contato — siga o atendimento normalmente "
                    "sem afirmar que existe cadastro."
                ),
            }
        if len(rows) > 1:
            return {
                "encontrado": False,
                "motivo": "ambiguo",
                "explicacao": (
                    f"{len(rows)} clientes correspondem à busca. Peça o "
                    "telefone completo para identificar com segurança."
                ),
                "quantidade_de_candidatos": len(rows),
            }

        customer = rows[0]
        pedidos = conn.execute(
            """
            SELECT order_id, order_date, status, total_brl, tracking_code
              FROM orders WHERE customer_id = ?
             ORDER BY order_date DESC
            """,
            (customer["customer_id"],),
        ).fetchall()
    finally:
        conn.close()

    return {
        "encontrado": True,
        "customer_id": customer["customer_id"],
        "nome": customer["name"],
        "primeiro_nome": customer["name"].split()[0],
        "cidade": customer["city"],
        "regiao_metropolitana_cg": bool(customer["is_metro_cg"]),
        "total_de_pedidos": len(pedidos),
        "pedidos": [
            {
                "order_id": p["order_id"],
                "data": format_date_pt(parse_iso_date(p["order_date"])),
                "status": p["status"],
                "status_descricao": STATUS_DESCRICAO.get(p["status"], p["status"]),
                "valor_total_formatado": brl(float(p["total_brl"])),
            }
            for p in pedidos
        ],
    }


def verificar_elegibilidade_devolucao(
    order_id: int,
    motivo: str = "arrependimento",
    confirmacao: str = "",
) -> dict:
    """Avalia troca/devolução de um pedido contra os prazos do manual.

    `motivo` deve ser 'arrependimento' (§4.1), 'defeito' (§4.2) ou
    'preferencia' (§4.3). Devolve veredito, data-limite e os próximos passos.
    """
    motivo_limpo = normalize(motivo or "arrependimento")
    if motivo_limpo not in MOTIVOS_VALIDOS:
        raise ToolError(
            f"Motivo '{motivo}' não reconhecido.",
            suggestion=f"Use um destes: {', '.join(MOTIVOS_VALIDOS)}.",
        )

    order, items = _fetch_order(order_id)
    if order is None:
        raise ToolError(f"Não encontrei o pedido {order_id} no sistema.")

    if not _verifica_identidade(order, confirmacao):
        return {
            "verificacao_necessaria": True,
            "order_id": int(order_id),
            "motivo": (
                "Por proteção de dados (§9), confirme a identidade antes de "
                "tratar da devolução."
            ),
            "como_proceder": (
                "Peça o nome completo ou o telefone cadastrado e chame "
                "novamente passando o valor em `confirmacao`."
            ),
        }

    from emporio.config import settings

    hoje = settings.now().date()
    status = order["status"]
    data_pedido = parse_iso_date(order["order_date"])
    recebimento, ressalva_data = _data_de_recebimento(order)

    base: dict = {
        "order_id": order["order_id"],
        "cliente": order["customer_name"],
        "status_do_pedido": status,
        "status_descricao": STATUS_DESCRICAO.get(status, status),
        "data_do_pedido": format_date_pt(data_pedido),
        "data_de_referencia_do_calculo": format_date_pt(recebimento),
        "data_de_hoje": format_date_pt(hoje),
        "motivo_avaliado": motivo_limpo,
        "ressalva_sobre_a_data": ressalva_data,
        "itens": [
            {"product_id": i["product_id"], "nome": i["name"], "quantidade": i["quantity"]}
            for i in items
        ],
    }

    # --- situações em que o prazo nem começou a contar --------------------
    if status == "cancelled":
        base.update(
            {
                "elegivel": False,
                "veredito": "pedido_cancelado",
                "explicacao": (
                    "Este pedido está cancelado, então não há o que devolver. "
                    + (f"Motivo registrado: {order['notes']}. " if order["notes"] else "")
                    + "Se houve cobrança, o estorno segue a forma de pagamento "
                    "original."
                ),
                "citacoes": ["§4 Política de Trocas e Devoluções"],
                "proximos_passos": [
                    "Confirmar com o cliente se houve cobrança.",
                    "Se houve, encaminhar à equipe para verificar o estorno.",
                ],
            }
        )
        return base

    if status in {"pending", "confirmed"}:
        base.update(
            {
                "elegivel": None,
                "veredito": "ainda_nao_entregue",
                "explicacao": (
                    "O pedido ainda não foi entregue ("
                    f"{STATUS_DESCRICAO.get(status, status)}), então o prazo de "
                    "devolução — que conta a partir do recebimento — ainda não "
                    "começou. O cliente quer cancelar antes do envio, e o "
                    "manual não traz regra específica para cancelamento "
                    "pré-despacho."
                ),
                "citacoes": ["§4.1 Direito de Arrependimento", "§10 Disposições Finais"],
                "proximos_passos": [
                    "Confirmar se o cliente quer cancelar antes do envio.",
                    "Encaminhar à equipe: o §10 orienta consultar a gerência "
                    "quando a política não cobre o caso. Não prometa prazo nem "
                    "condição de estorno por conta própria.",
                ],
            }
        )
        return base

    if status == "shipped":
        base.update(
            {
                "elegivel": None,
                "veredito": "em_transporte",
                "explicacao": (
                    "O pedido está em transporte. O prazo de 7 dias corridos "
                    "para arrependimento conta a partir do recebimento (§4.1), "
                    "então o cliente ainda terá o prazo integral quando o "
                    "produto chegar."
                ),
                "citacoes": ["§4.1 Direito de Arrependimento"],
                "proximos_passos": [
                    "Explicar que o prazo começa na entrega.",
                    "Se o cliente não quiser mais o produto, pode recusar o "
                    "recebimento — o §5.2 prevê recusa em caso de avaria, e "
                    "recusar simplifica a logística. Confirme o procedimento "
                    "com a equipe antes de garantir.",
                ],
            }
        )
        return base

    # --- pedido entregue: aqui a conta de prazo vale ----------------------
    if recebimento is None:
        raise ToolError(
            f"O pedido {order_id} não tem data utilizável para calcular prazo."
        )

    dias_desde_recebimento = days_between(recebimento, hoje)
    base["dias_desde_o_recebimento"] = dias_desde_recebimento

    prazos = {
        "arrependimento": PRAZO_ARREPENDIMENTO_DIAS,
        "preferencia": PRAZO_TROCA_PREFERENCIA_DIAS,
        "defeito": PRAZO_TROCA_DEFEITO_DIAS,
    }
    # §4.2 conta "após a compra"; §4.1 e §4.3, após o recebimento.
    inicio = data_pedido if motivo_limpo == "defeito" else recebimento
    if inicio is None:
        inicio = recebimento
    prazo_dias = prazos[motivo_limpo]
    limite = date.fromordinal(inicio.toordinal() + prazo_dias)
    dias_decorridos = days_between(inicio, hoje)
    dentro_do_prazo = dias_decorridos <= prazo_dias

    base.update(
        {
            "inicio_da_contagem": format_date_pt(inicio),
            "prazo_em_dias_corridos": prazo_dias,
            "data_limite": format_date_pt(limite),
            "dias_decorridos": dias_decorridos,
            "elegivel": dentro_do_prazo,
        }
    )
    if dentro_do_prazo:
        base["dias_restantes"] = prazo_dias - dias_decorridos
    else:
        base["dias_excedidos"] = dias_decorridos - prazo_dias

    if motivo_limpo == "arrependimento":
        base["citacoes"] = ["§4.1 Direito de Arrependimento (Compras Online)"]
        if dentro_do_prazo:
            reembolso = add_business_days(hoje, PRAZO_REEMBOLSO_DIAS_UTEIS)
            base["veredito"] = "elegivel_arrependimento"
            base["explicacao"] = (
                f"Dentro do prazo: a devolução por arrependimento pode ser "
                f"solicitada até {format_date_pt(limite)} "
                f"({prazo_dias} dias corridos após o recebimento), sem "
                "necessidade de justificativa."
            )
            base["condicoes"] = [
                "Produto na embalagem original, sem sinais de uso, com todos os "
                "acessórios e manuais (§4.1).",
            ]
            base["proximos_passos"] = [
                "O frete de devolução é por conta da loja em caso de "
                "arrependimento (§4.1).",
                f"Reembolso na mesma forma de pagamento original em até "
                f"{PRAZO_REEMBOLSO_DIAS_UTEIS} dias úteis — aproximadamente "
                f"{format_date_pt(reembolso)} (§4.1).",
                "Registrar a solicitação e encaminhar à equipe para a coleta.",
            ]
        else:
            base["veredito"] = "fora_do_prazo_arrependimento"
            base["explicacao"] = (
                f"Fora do prazo: o direito de arrependimento valia até "
                f"{format_date_pt(limite)} e hoje é {format_date_pt(hoje)} — "
                f"{base['dias_excedidos']} dias além do limite. Não é possível "
                "devolver por arrependimento."
            )
            base["alternativas"] = _alternativas_fora_do_prazo(recebimento, data_pedido, hoje)
            base["proximos_passos"] = [
                "Informar o cliente com clareza e empatia, sem prometer "
                "exceção.",
                "Verificar se o caso é, na verdade, defeito de fabricação — o "
                "prazo e o tratamento são outros.",
            ]

    elif motivo_limpo == "preferencia":
        base["citacoes"] = ["§4.3 Trocas por Preferência"]
        if dentro_do_prazo:
            base["veredito"] = "elegivel_troca_preferencia"
            base["explicacao"] = (
                f"Dentro do prazo: troca por preferência (cor, modelo, tamanho) "
                f"pode ser feita até {format_date_pt(limite)}, mediante "
                "disponibilidade do produto desejado."
            )
            base["condicoes"] = [
                "Produto em perfeito estado e na embalagem original (§4.3).",
                "Diferenças de valor são cobradas ou reembolsadas conforme o "
                "caso (§4.3).",
                "A troca depende de o item desejado estar disponível — "
                "confirme o estoque com buscar_produtos antes de garantir.",
            ]
        else:
            base["veredito"] = "fora_do_prazo_preferencia"
            base["explicacao"] = (
                f"Fora do prazo: troca por preferência valia até "
                f"{format_date_pt(limite)} ({prazo_dias} dias corridos)."
            )
            base["alternativas"] = _alternativas_fora_do_prazo(recebimento, data_pedido, hoje)

    else:  # defeito
        base["citacoes"] = ["§4.2 Trocas por Defeito", "§8.1 Garantia Legal"]
        garantia_limite = date.fromordinal(
            recebimento.toordinal() + PRAZO_GARANTIA_LEGAL_DIAS
        )
        dentro_garantia_legal = days_between(recebimento, hoje) <= PRAZO_GARANTIA_LEGAL_DIAS
        base["garantia_legal"] = {
            "prazo_dias": PRAZO_GARANTIA_LEGAL_DIAS,
            "data_limite": format_date_pt(garantia_limite),
            "dentro_do_prazo": dentro_garantia_legal,
        }
        if dentro_do_prazo:
            base["veredito"] = "elegivel_troca_defeito"
            base["explicacao"] = (
                f"Dentro do prazo: produto com defeito de fabricação pode ser "
                f"trocado até {format_date_pt(limite)} "
                f"({prazo_dias} dias corridos após a compra)."
            )
            base["condicoes"] = [
                "Danos por mau uso, queda, umidade excessiva ou modificação "
                "não autorizada não são cobertos (§4.2).",
            ]
            base["proximos_passos"] = [
                "Registrar a descrição do defeito.",
                "Encaminhar à equipe para avaliação do instrumento.",
            ]
        else:
            base["veredito"] = "fora_do_prazo_troca_direta"
            base["explicacao"] = (
                f"A troca direta na loja valia até {format_date_pt(limite)} "
                f"(30 dias corridos após a compra). Passado esse prazo, o "
                "caminho é a garantia."
            )
            base["proximos_passos"] = [
                (
                    f"A garantia legal de {PRAZO_GARANTIA_LEGAL_DIAS} dias vai "
                    f"até {format_date_pt(garantia_limite)} — "
                    + ("ainda vigente." if dentro_garantia_legal else "já vencida.")
                ),
                "Acionar a garantia diretamente com o fabricante. A Empório da "
                "Música pode intermediar o processo mediante solicitação (§4.2).",
                "A garantia do fabricante varia de 6 meses a 2 anos e está no "
                "certificado que acompanha o produto (§8.2).",
            ]
            base["nao_coberto_pela_garantia"] = [
                "Desgaste natural de peças (trastes, cordas, feltros, palhetas).",
                "Danos por mau uso, queda ou condições climáticas extremas.",
                "Reparos por terceiros não autorizados.",
                "Danos estéticos que não afetem a funcionalidade (§8.3).",
            ]

    restricoes = _restricoes_44(items)
    if restricoes:
        base["restricoes_a_verificar"] = restricoes
    return base


def _alternativas_fora_do_prazo(
    recebimento: date | None, data_pedido: date | None, hoje: date
) -> list[str]:
    """O que ainda resta ao cliente quando o prazo de devolução passou."""
    alternativas: list[str] = []
    if data_pedido:
        limite_defeito = date.fromordinal(
            data_pedido.toordinal() + PRAZO_TROCA_DEFEITO_DIAS
        )
        if days_between(data_pedido, hoje) <= PRAZO_TROCA_DEFEITO_DIAS:
            alternativas.append(
                f"Se houver defeito de fabricação, a troca é possível até "
                f"{format_date_pt(limite_defeito)} (§4.2)."
            )
    if recebimento:
        limite_garantia = date.fromordinal(
            recebimento.toordinal() + PRAZO_GARANTIA_LEGAL_DIAS
        )
        if days_between(recebimento, hoje) <= PRAZO_GARANTIA_LEGAL_DIAS:
            alternativas.append(
                f"A garantia legal de 90 dias contra defeito de fabricação "
                f"segue vigente até {format_date_pt(limite_garantia)} (§8.1)."
            )
        else:
            alternativas.append(
                "A garantia legal de 90 dias também venceu, mas a garantia do "
                "fabricante (6 meses a 2 anos) pode estar ativa — vale checar o "
                "certificado do produto (§8.2)."
            )
    return alternativas


def _restricoes_44(items) -> list[str]:
    """Itens que podem cair nas exceções do §4.4.

    A base não marca "venda final" nem personalização, então não há como
    decidir automaticamente. Em vez de ignorar a regra ou aplicá-la por
    suposição, sinalizamos o que precisa de conferência humana.
    """
    if not items:
        return []
    ensure_database()
    conn = connect(read_only=True)
    try:
        avisos: list[str] = []
        for item in items:
            promos = conn.execute(
                "SELECT description FROM promotions WHERE product_id = ?",
                (item["product_id"],),
            ).fetchall()
            for promo in promos:
                rotulo = normalize(promo["description"])
                if any(c in rotulo for c in CAMPANHAS_LIQUIDACAO):
                    avisos.append(
                        f"'{item['name']}' tem campanha de liquidação cadastrada "
                        f"('{promo['description']}'). O §4.4 exclui da troca "
                        "produtos de liquidação **com aviso explícito de venda "
                        "final** — a base não registra esse aviso, então "
                        "confirme com a equipe se foi o caso desta compra."
                    )
                    break
        if avisos:
            avisos.append(
                "O §4.4 também exclui instrumentos com personalização ou setup "
                "sob encomenda e boquilhas de sopro. Esses dados não constam do "
                "sistema — confirme com o cliente se houve ajuste sob encomenda."
            )
        return avisos
    finally:
        conn.close()
