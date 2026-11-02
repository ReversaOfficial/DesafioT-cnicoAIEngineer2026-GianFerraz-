"""Consultas ao catálogo de produtos.

Decisão de arquitetura: ferramentas tipadas com SQL parametrizado, **não** um
agente de texto-para-SQL.

Para um catálogo deste tamanho e com regras de negócio estáveis, SQL gerado
pelo modelo traria riscos sem benefício: consulta que ignora
`availability`/`status`, join que ressuscita promoção vencida, e nenhuma
garantia de que a mesma pergunta produza a mesma resposta duas vezes. Com
ferramentas fixas, as regras do manual ficam dentro da consulta, são testáveis
e o espaço de ações do modelo é fechado. O custo é flexibilidade: uma pergunta
fora das dimensões previstas não é respondível — o README registra isso como
limitação, e `buscar_produtos` cobre as dimensões que o atendimento realmente
usa (texto livre, categoria, faixa de preço, disponibilidade).
"""

from __future__ import annotations

from emporio.data.db import connect, ensure_database
from emporio.text_utils import brl, normalize, tokenize
from emporio.tools.common import (
    MAX_RESULTS,
    PRODUCT_SELECT,
    ToolError,
    product_payload,
)


def _rows(sql: str, params: tuple | dict = ()) -> list:
    ensure_database()
    conn = connect(read_only=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _resolve_category(termo: str) -> tuple[int | None, str | None]:
    """Casa um texto livre com uma categoria do catálogo.

    Aceita id ("5"), nome exato ("Violões") ou parcial ("violao", "sopro").
    """
    cleaned = (termo or "").strip()
    if not cleaned:
        return None, None
    if cleaned.isdigit():
        found = _rows(
            "SELECT category_id, name FROM categories WHERE category_id = ?",
            (int(cleaned),),
        )
        return (found[0]["category_id"], found[0]["name"]) if found else (None, None)

    needle = normalize(cleaned)
    found = _rows(
        "SELECT category_id, name, name_norm FROM categories ORDER BY category_id"
    )
    for row in found:  # casamento exato primeiro
        if row["name_norm"] == needle:
            return row["category_id"], row["name"]
    for row in found:  # depois substring, nos dois sentidos
        if needle in row["name_norm"] or row["name_norm"] in needle:
            return row["category_id"], row["name"]
    return None, None


def listar_categorias() -> dict:
    """Categorias do catálogo com a quantidade de itens disponíveis em cada."""
    rows = _rows(
        """
        SELECT c.category_id, c.name, c.description,
               COUNT(p.product_id) AS total,
               SUM(CASE WHEN p.availability = 'in_stock' THEN 1 ELSE 0 END) AS disponiveis,
               MIN(CASE WHEN p.availability = 'in_stock' THEN p.price_brl END) AS menor_preco,
               MAX(CASE WHEN p.availability = 'in_stock' THEN p.price_brl END) AS maior_preco
          FROM categories c
          LEFT JOIN products p ON p.category_id = c.category_id
         GROUP BY c.category_id
         ORDER BY c.category_id
        """
    )
    categorias = []
    vazias = []
    for row in rows:
        disponiveis = int(row["disponiveis"] or 0)
        item = {
            "category_id": row["category_id"],
            "nome": row["name"],
            "descricao": row["description"],
            "produtos_cadastrados": int(row["total"] or 0),
            "produtos_disponiveis": disponiveis,
        }
        if disponiveis:
            item["faixa_de_preco"] = (
                f"{brl(row['menor_preco'])} a {brl(row['maior_preco'])}"
            )
        else:
            # Distinção que importa no atendimento: a categoria existe no
            # cadastro mas não há item para vender. Dizer "não encontrei" seria
            # impreciso; prometer o produto seria pior.
            item["observacao"] = (
                "Categoria cadastrada, mas sem nenhum item disponível no "
                "catálogo atual. Não ofereça produtos desta categoria."
            )
            vazias.append(row["name"])
        categorias.append(item)

    payload: dict = {"categorias": categorias}
    if vazias:
        payload["categorias_sem_itens"] = vazias
        payload["aviso"] = (
            "Estas categorias constam do cadastro mas não têm produto algum "
            "no catálogo: " + ", ".join(vazias) + ". Se o cliente pedir um "
            "item delas, informe que não trabalhamos com esses modelos no "
            "momento em vez de prometer consulta ao estoque."
        )
    return payload


def buscar_produtos(
    termo: str = "",
    categoria: str = "",
    preco_max: float | None = None,
    preco_min: float | None = None,
    marca: str = "",
    apenas_disponiveis: bool = True,
    limite: int = 5,
) -> dict:
    """Busca produtos no catálogo combinando texto livre e filtros.

    `apenas_disponiveis=True` é o default porque §7.3 proíbe confirmar
    disponibilidade sem verificar o estoque: o caminho fácil tem de ser o
    caminho correto.
    """
    limite = max(1, min(int(limite or 5), MAX_RESULTS))
    where: list[str] = []
    params: dict = {}

    category_id, category_name = (None, None)
    if categoria:
        category_id, category_name = _resolve_category(categoria)
        if category_id is None:
            disponiveis = [c["nome"] for c in listar_categorias()["categorias"]]
            raise ToolError(
                f"Categoria '{categoria}' não existe no catálogo.",
                suggestion="Categorias cadastradas: " + ", ".join(disponiveis),
            )
        where.append("p.category_id = :category_id")
        params["category_id"] = category_id

    if apenas_disponiveis:
        where.append("p.availability = 'in_stock'")

    if preco_max is not None:
        where.append("v.list_price * (1 - v.active_discount_percent / 100.0) <= :preco_max")
        params["preco_max"] = float(preco_max)
    if preco_min is not None:
        where.append("v.list_price * (1 - v.active_discount_percent / 100.0) >= :preco_min")
        params["preco_min"] = float(preco_min)

    if marca:
        where.append("p.brand_norm LIKE :marca")
        params["marca"] = f"%{normalize(marca)}%"

    # Texto livre: todos os tokens precisam aparecer no blob de busca (AND).
    # AND em vez de OR porque "violao eletrico" deve trazer violão eletro-
    # acústico, não todo violão mais toda guitarra elétrica.
    tokens = tokenize(termo) if termo else []
    for i, token in enumerate(tokens):
        key = f"tok{i}"
        where.append(f"p.search_blob LIKE :{key}")
        params[key] = f"%{token}%"

    sql = PRODUCT_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY v.list_price * (1 - v.active_discount_percent / 100.0) ASC,
                 p.name ASC
        LIMIT :limite
    """
    params["limite"] = limite
    rows = _rows(sql, params)

    produtos = [product_payload(row) for row in rows]
    payload: dict = {
        "total_encontrado": len(produtos),
        "produtos": produtos,
        "filtros_aplicados": {
            "termo": termo or None,
            "categoria": category_name,
            "marca": marca or None,
            "preco_min": preco_min,
            "preco_max": preco_max,
            "apenas_disponiveis": apenas_disponiveis,
        },
    }

    if not produtos:
        payload.update(_explain_empty_result(category_id, category_name, tokens, preco_max))
    return payload


def _explain_empty_result(
    category_id: int | None,
    category_name: str | None,
    tokens: list[str],
    preco_max: float | None,
) -> dict:
    """Diagnostica *por que* a busca veio vazia.

    Um "nenhum resultado" seco faz o modelo improvisar. Dizendo se a categoria
    está vazia, se o item existe mas sem estoque, ou se o teto de preço é baixo
    demais, o agente consegue dar uma resposta útil e honesta — e sugerir a
    alternativa correta, como §7.3 exige.
    """
    out: dict = {}

    if category_id is not None:
        total = _rows(
            "SELECT COUNT(*) AS n FROM products WHERE category_id = ?", (category_id,)
        )[0]["n"]
        if total == 0:
            out["motivo"] = "categoria_sem_produtos"
            out["explicacao"] = (
                f"A categoria '{category_name}' existe no cadastro, mas não há "
                "nenhum produto nela. A loja não tem itens desse tipo para "
                "oferecer no momento."
            )
            return out

    # O item existe mas está indisponível? Caso clássico de §7.3.
    if tokens:
        where = " AND ".join(f"p.search_blob LIKE :t{i}" for i in range(len(tokens)))
        params = {f"t{i}": f"%{t}%" for i, t in enumerate(tokens)}
        sql = PRODUCT_SELECT + f" WHERE {where} LIMIT 5"
        indisponiveis = _rows(sql, params)
        if indisponiveis:
            out["motivo"] = "encontrado_mas_indisponivel"
            out["explicacao"] = (
                "Há produto correspondente no catálogo, mas fora dos filtros "
                "de disponibilidade. Informe a situação real e ofereça "
                "alternativas semelhantes (§7.3)."
            )
            out["produtos_indisponiveis"] = [
                product_payload(row) for row in indisponiveis
            ]
            return out

    if preco_max is not None:
        scope = "SELECT MIN(price_brl) AS m FROM products WHERE availability = 'in_stock'"
        params_min: tuple = ()
        if category_id is not None:
            scope += " AND category_id = ?"
            params_min = (category_id,)
        cheapest = _rows(scope, params_min)[0]["m"]
        if cheapest:
            out["motivo"] = "fora_da_faixa_de_preco"
            out["explicacao"] = (
                f"Nada disponível até {brl(preco_max)}"
                + (f" em {category_name}" if category_name else "")
                + f". O item disponível mais barato{' dessa categoria' if category_name else ''} "
                f"custa {brl(cheapest)}."
            )
            out["menor_preco_disponivel"] = round(float(cheapest), 2)
            return out

    out["motivo"] = "sem_correspondencia"
    out["explicacao"] = (
        "Nenhum produto corresponde a todos os termos informados. Vale "
        "confirmar o modelo com o cliente ou oferecer uma alternativa."
    )
    parecidos = _buscar_parecidos(tokens, category_id)
    if parecidos:
        out["produtos_parecidos"] = parecidos
        out["explicacao"] += (
            " Há itens que correspondem a parte dos termos, listados em "
            "`produtos_parecidos` — informe que o modelo exato não existe no "
            "catálogo e ofereça esses equivalentes, como pede o §7.3."
        )
    return out


def _buscar_parecidos(tokens: list[str], category_id: int | None, limite: int = 3) -> list[dict]:
    """Produtos que casam com PARTE dos termos, ordenados por quantos casam.

    A busca principal exige todos os termos (AND), que é o comportamento certo
    para "violão nylon" mas cego para "Fender Stratocaster American
    Professional" quando o catálogo tem a "American Pro II": o termo
    'professional' derruba a consulta inteira e o agente fica sem o
    equivalente que existe.

    Esta busca de aproximação pontua cada produto disponível pelo número de
    termos presentes no seu texto buscável e devolve os melhores. Um atendente
    humano faria o mesmo: reconheceria a marca e o modelo e mostraria o que
    tem. Fica restrita ao resultado vazio, para não poluir a busca normal.
    """
    if not tokens:
        return []

    pontuacao = " + ".join(
        f"(CASE WHEN p.search_blob LIKE :t{i} THEN 1 ELSE 0 END)" for i in range(len(tokens))
    )
    params: dict = {f"t{i}": f"%{t}%" for i, t in enumerate(tokens)}
    where = ["p.availability = 'in_stock'"]
    if category_id is not None:
        where.append("p.category_id = :category_id")
        params["category_id"] = category_id

    sql = (
        PRODUCT_SELECT
        + f" WHERE {' AND '.join(where)} AND ({pontuacao}) > 0"
        + f" ORDER BY ({pontuacao}) DESC, v.list_price ASC LIMIT :limite"
    )
    params["limite"] = limite
    return [product_payload(row) for row in _rows(sql, params)]


def detalhar_produto(product_id: int | None = None, nome: str = "") -> dict:
    """Ficha completa de um produto, por id ou por nome.

    A busca por nome exige que **todos** os tokens informados apareçam, e se
    houver mais de um candidato devolve a lista para desambiguação em vez de
    escolher por conta própria — chutar o modelo errado gera preço errado.
    """
    if product_id is None and not nome.strip():
        raise ToolError(
            "Informe `product_id` ou `nome` do produto.",
            suggestion="Use buscar_produtos para localizar o item primeiro.",
        )

    if product_id is not None:
        rows = _rows(PRODUCT_SELECT + " WHERE p.product_id = :pid", {"pid": int(product_id)})
        if not rows:
            raise ToolError(f"Não existe produto com id {product_id} no catálogo.")
        return {"produto": product_payload(rows[0], detailed=True)}

    tokens = tokenize(nome)
    if not tokens:
        raise ToolError("Nome informado não tem nenhum termo utilizável.")
    where = " AND ".join(f"p.search_blob LIKE :t{i}" for i in range(len(tokens)))
    params = {f"t{i}": f"%{t}%" for i, t in enumerate(tokens)}
    rows = _rows(PRODUCT_SELECT + f" WHERE {where} ORDER BY p.name LIMIT 6", params)

    if not rows:
        return {
            "produto": None,
            "motivo": "nao_encontrado",
            "explicacao": (
                f"Nenhum produto do catálogo corresponde a '{nome}'. Não "
                "trabalhamos com esse modelo, ou o nome está diferente do "
                "cadastro. Não invente preço ou disponibilidade."
            ),
        }

    # Casamento exato de nome resolve a ambiguidade na hora.
    needle = normalize(nome)
    exact = [r for r in rows if r["name_norm"] == needle]
    if len(rows) > 1 and not exact:
        return {
            "produto": None,
            "motivo": "ambiguo",
            "explicacao": (
                f"'{nome}' corresponde a {len(rows)} produtos. Pergunte ao "
                "cliente qual deles antes de informar preço."
            ),
            "candidatos": [product_payload(r) for r in rows],
        }
    return {"produto": product_payload((exact or rows)[0], detailed=True)}


def promocoes_ativas(limite: int = 10) -> dict:
    """Promoções vigentes (`is_active = 1`), com preço antes e depois.

    Promoções inativas nunca aparecem: §7.3 determina informar com
    transparência quando uma promoção venceu e nunca prometer desconto fora de
    vigência.
    """
    limite = max(1, min(int(limite or 10), MAX_RESULTS))
    rows = _rows(
        PRODUCT_SELECT
        + """
         WHERE v.active_discount_percent > 0
         ORDER BY v.active_discount_percent DESC
         LIMIT :limite
        """,
        {"limite": limite},
    )
    itens = [product_payload(row, detailed=False) for row in rows]
    return {
        "total": len(itens),
        "promocoes": itens,
        "regras": [
            "Promoções não são cumulativas (§6.2).",
            "O desconto de 5% do PIX não se aplica sobre preço já promocional (§6.2).",
            "Promoção está sujeita a estoque; produto esgotado não gera reserva de preço (§6.2).",
        ],
    }
