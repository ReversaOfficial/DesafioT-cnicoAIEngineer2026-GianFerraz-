"""ETL: CSVs da operação -> SQLite, com relatório de qualidade de dados.

Por que SQLite e não ler os CSVs direto em memória a cada pergunta:

  * As regras de negócio (promoção ativa, disponibilidade, faixa de preço)
    viram consulta declarativa em vez de laço em Python — menos código e menos
    chance de duas ferramentas divergirem na mesma regra.
  * O ETL é o lugar natural para normalizar texto, derivar campos e *registrar
    o que está inconsistente na origem* uma única vez, em vez de tratar
    exceções espalhadas pelas ferramentas.
  * Troca a base por um Postgres real sem mexer nas ferramentas.

O ETL é idempotente: recriar o banco do zero é sempre seguro.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from emporio import __version__
from emporio.config import settings
from emporio.text_utils import digits_only, normalize

# ---------------------------------------------------------------------------
# Regras de domínio aplicadas no ETL
# ---------------------------------------------------------------------------

# Região Metropolitana de Campo Grande (MS), instituída pela Lei Complementar
# estadual nº 51/2010. Define a regra de frete do §5.1 do manual (grátis acima
# de R$ 500). Dos municípios abaixo, apenas Campo Grande aparece na base de
# clientes — os demais estão aqui para o caso de a base crescer.
METRO_CAMPO_GRANDE: frozenset[str] = frozenset(
    normalize(c)
    for c in (
        "Campo Grande",
        "Bandeirantes",
        "Corguinho",
        "Dois Irmãos do Buriti",
        "Jaraguari",
        "Nova Alvorada do Sul",
        "Ribas do Rio Pardo",
        "Rochedo",
        "Sidrolândia",
        "Terenos",
    )
)

# Vocabulário fechado de disponibilidade, derivado de `status` + `stock_quantity`.
AVAILABILITY_IN_STOCK = "in_stock"
AVAILABILITY_OUT_OF_STOCK = "out_of_stock"
AVAILABILITY_DISCONTINUED = "discontinued"
AVAILABILITY_COMING_SOON = "coming_soon"

CSV_FILES = {
    "categories": "desafio_tecnico_ai_eng - categories.csv",
    "customers": "desafio_tecnico_ai_eng - customers.csv",
    "order_items": "desafio_tecnico_ai_eng - order_items.csv",
    "orders": "desafio_tecnico_ai_eng - orders.csv",
    "products": "desafio_tecnico_ai_eng - products.csv",
    "promotions": "desafio_tecnico_ai_eng - promotions.csv",
}

# Marcas conhecidas de dois tokens — sem isso "Music Man Bass 1X" viraria
# marca "Music".
MULTIWORD_BRANDS = ("Music Man", "Paulo Son", "Di Giorgio", "Luthier Brasil")


@dataclass
class Finding:
    """Uma inconsistência encontrada na origem.

    O ETL não "conserta" silenciosamente: registra, decide uma política
    explícita e documenta. Dado de operação corrigido sem rastro é pior do que
    dado errado conhecido.
    """

    severity: str  # "warning" | "info"
    table: str
    issue: str
    detail: str
    decision: str


# ---------------------------------------------------------------------------
# Helpers de parsing
# ---------------------------------------------------------------------------


def _read_csv(data_dir: Path, key: str) -> list[dict[str, str]]:
    path = data_dir / CSV_FILES[key]
    if not path.exists():
        raise FileNotFoundError(
            f"CSV não encontrado: {path}\n"
            f"Esperado em {data_dir}. Confira se a pasta data/ do desafio está no lugar."
        )
    # utf-8-sig remove BOM que planilhas do Google/Excel costumam deixar.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [
            {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


def _to_float(raw: str, *, default: float = 0.0) -> float:
    """Converte número de planilha. Aceita '1.234,56', '1234.56' e '1234'."""
    text = (raw or "").strip()
    if not text:
        return default
    text = text.replace("R$", "").replace(" ", "")
    if "," in text:  # formato brasileiro: ponto é milhar, vírgula é decimal
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _to_int(raw: str, *, default: int = 0) -> int:
    try:
        return int(float((raw or "").strip() or default))
    except ValueError:
        return default


def _parse_date(raw: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_brand(name: str) -> str:
    """Primeiro token do nome do produto, tratando marcas compostas."""
    for brand in MULTIWORD_BRANDS:
        if name.lower().startswith(brand.lower()):
            return brand
    return name.split(" ", 1)[0] if name else ""


def _derive_availability(status: str, stock: int) -> str:
    """`status` + estoque -> um único vocabulário de disponibilidade.

    `discontinued` e `coming_soon` vencem o estoque: o manual (§7.3) manda
    informar que o produto saiu do catálogo / ainda não chegou, mesmo que haja
    unidades registradas no sistema.
    """
    if status == "discontinued":
        return AVAILABILITY_DISCONTINUED
    if status == "coming_soon":
        return AVAILABILITY_COMING_SOON
    return AVAILABILITY_IN_STOCK if stock > 0 else AVAILABILITY_OUT_OF_STOCK


def _specs_to_text(specs_json: str) -> str:
    """Achata o JSON de especificações em texto buscável.

    'top: Spruce Sólido' precisa ser encontrável por quem pergunta
    "violão de tampo sólido".
    """
    try:
        data = json.loads(specs_json) if specs_json else {}
    except json.JSONDecodeError:
        return specs_json or ""
    if not isinstance(data, dict):
        return str(data)
    return " ".join(f"{k} {v}" for k, v in data.items())


# ---------------------------------------------------------------------------
# ETL
# ---------------------------------------------------------------------------


def build_database(
    *,
    data_dir: Path | None = None,
    db_path: Path | None = None,
    write_report: bool = True,
) -> list[Finding]:
    """Reconstrói o banco a partir dos CSVs. Retorna as inconsistências achadas."""
    data_dir = data_dir or settings.data_dir
    db_path = db_path or settings.db_path
    findings: list[Finding] = []

    raw = {key: _read_csv(data_dir, key) for key in CSV_FILES}

    # --- categorias --------------------------------------------------------
    categories = [
        {
            "category_id": _to_int(r["category_id"]),
            "name": r["name"],
            "name_norm": normalize(r["name"]),
            "description": r.get("description", ""),
        }
        for r in raw["categories"]
    ]
    category_names = {c["category_id"]: c["name"] for c in categories}

    # --- produtos ----------------------------------------------------------
    products = []
    for r in raw["products"]:
        pid = _to_int(r["product_id"])
        name = r["name"]
        brand = _extract_brand(name)
        stock = _to_int(r["stock_quantity"])
        status = (r["status"] or "active").strip().lower()
        cat_id = _to_int(r["category_id"])
        specs_text = _specs_to_text(r.get("specs", ""))
        products.append(
            {
                "product_id": pid,
                "name": name,
                "name_norm": normalize(name),
                "brand": brand,
                "brand_norm": normalize(brand),
                "category_id": cat_id,
                "price_brl": _to_float(r["price_brl"]),
                "stock_quantity": stock,
                "status": status,
                "availability": _derive_availability(status, stock),
                "description": r.get("description", ""),
                "specs_json": r.get("specs", "") or "{}",
                "created_at": r.get("created_at", ""),
                "search_blob": normalize(
                    " ".join(
                        [
                            name,
                            brand,
                            category_names.get(cat_id, ""),
                            r.get("description", ""),
                            specs_text,
                        ]
                    )
                ),
            }
        )
    products_by_id = {p["product_id"]: p for p in products}

    # --- clientes ----------------------------------------------------------
    customers = []
    for r in raw["customers"]:
        city_norm = normalize(r.get("city", ""))
        customers.append(
            {
                "customer_id": _to_int(r["customer_id"]),
                "name": r["name"],
                "name_norm": normalize(r["name"]),
                "phone": r.get("phone", ""),
                "phone_digits": digits_only(r.get("phone", "")),
                "email": r.get("email", ""),
                "email_norm": normalize(r.get("email", "")),
                "city": r.get("city", ""),
                "city_norm": city_norm,
                "is_metro_cg": 1 if city_norm in METRO_CAMPO_GRANDE else 0,
            }
        )

    # --- pedidos -----------------------------------------------------------
    orders = []
    for r in raw["orders"]:
        orders.append(
            {
                "order_id": _to_int(r["order_id"]),
                "customer_id": _to_int(r["customer_id"]),
                "order_date": r.get("order_date", ""),
                "status": (r.get("status") or "").strip().lower(),
                "total_brl": _to_float(r["total_brl"]),
                "payment_method": (r.get("payment_method") or "").strip().lower(),
                "tracking_code": (r.get("tracking_code") or "").strip(),
                "estimated_delivery": (r.get("estimated_delivery") or "").strip(),
                "notes": r.get("notes", ""),
            }
        )
    orders_by_id = {o["order_id"]: o for o in orders}

    # --- itens de pedido ---------------------------------------------------
    # PK composta (order_id, product_id): o mesmo produto repetido no mesmo
    # pedido é somado em vez de violar a chave.
    merged_items: dict[tuple[int, int], int] = {}
    for r in raw["order_items"]:
        key = (_to_int(r["order_id"]), _to_int(r["product_id"]))
        qty = _to_int(r["quantity"], default=1)
        if key in merged_items:
            findings.append(
                Finding(
                    "info",
                    "order_items",
                    "Linhas duplicadas para o mesmo produto no mesmo pedido",
                    f"pedido {key[0]}, produto {key[1]}",
                    "Quantidades somadas em uma única linha.",
                )
            )
        merged_items[key] = merged_items.get(key, 0) + qty
    order_items = [
        {"order_id": oid, "product_id": pid, "quantity": qty}
        for (oid, pid), qty in merged_items.items()
    ]

    # --- promoções ---------------------------------------------------------
    promotions = [
        {
            "promotion_id": _to_int(r["promotion_id"]),
            "product_id": _to_int(r["product_id"]),
            "discount_percent": _to_float(r["discount_percent"]),
            "description": r.get("description", ""),
            "is_active": 1 if _to_int(r.get("is_active", "0")) == 1 else 0,
        }
        for r in raw["promotions"]
    ]

    findings.extend(
        _audit(
            categories=categories,
            products=products,
            products_by_id=products_by_id,
            customers=customers,
            orders=orders,
            orders_by_id=orders_by_id,
            order_items=order_items,
            promotions=promotions,
        )
    )

    _write_database(
        db_path,
        categories=categories,
        products=products,
        customers=customers,
        orders=orders,
        order_items=order_items,
        promotions=promotions,
    )

    if write_report:
        _write_report(findings, counts={k: len(v) for k, v in raw.items()})

    return findings


def _audit(
    *,
    categories: list[dict],
    products: list[dict],
    products_by_id: dict[int, dict],
    customers: list[dict],
    orders: list[dict],
    orders_by_id: dict[int, dict],
    order_items: list[dict],
    promotions: list[dict],
) -> list[Finding]:
    """Verificações de integridade. Nenhuma delas interrompe o ETL.

    A política geral é *degradar com transparência*: carregamos o que existe e
    deixamos registrado o que não fecha, para que o agente nunca apresente como
    certo um número que a origem contradiz.
    """
    out: list[Finding] = []
    product_ids = set(products_by_id)
    order_ids = set(orders_by_id)
    customer_ids = {c["customer_id"] for c in customers}
    category_ids = {c["category_id"] for c in categories}

    # Integridade referencial
    for item in order_items:
        if item["product_id"] not in product_ids:
            out.append(
                Finding(
                    "warning",
                    "order_items",
                    "Item referencia produto inexistente",
                    f"pedido {item['order_id']} -> produto {item['product_id']}",
                    "Linha descartada na carga.",
                )
            )
        if item["order_id"] not in order_ids:
            out.append(
                Finding(
                    "warning",
                    "order_items",
                    "Item referencia pedido inexistente",
                    f"pedido {item['order_id']}",
                    "Linha descartada na carga.",
                )
            )
    for o in orders:
        if o["customer_id"] not in customer_ids:
            out.append(
                Finding(
                    "warning",
                    "orders",
                    "Pedido referencia cliente inexistente",
                    f"pedido {o['order_id']} -> cliente {o['customer_id']}",
                    "Pedido mantido; consulta por cliente retorna vazio.",
                )
            )
    for p in products:
        if p["category_id"] not in category_ids:
            out.append(
                Finding(
                    "warning",
                    "products",
                    "Produto em categoria inexistente",
                    f"produto {p['product_id']} -> categoria {p['category_id']}",
                    "Produto mantido sem nome de categoria.",
                )
            )

    # Total do pedido x soma dos itens pelo preço de tabela atual
    items_by_order: dict[int, list[dict]] = {}
    for item in order_items:
        items_by_order.setdefault(item["order_id"], []).append(item)
    for oid, order in orders_by_id.items():
        its = items_by_order.get(oid, [])
        if not its:
            out.append(
                Finding(
                    "warning",
                    "orders",
                    "Pedido sem itens",
                    f"pedido {oid}",
                    "Total do pedido é usado como está.",
                )
            )
            continue
        computed = sum(
            products_by_id[i["product_id"]]["price_brl"] * i["quantity"]
            for i in its
            if i["product_id"] in products_by_id
        )
        if abs(computed - order["total_brl"]) > 0.01:
            out.append(
                Finding(
                    "info",
                    "orders",
                    "Total do pedido difere da soma dos itens a preço de tabela",
                    f"pedido {oid}: total={order['total_brl']:.2f} vs itens={computed:.2f}",
                    "`orders.total_brl` prevalece — é o valor efetivamente "
                    "transacionado (pode conter desconto ou frete da época). "
                    "O agente nunca recalcula total de pedido fechado.",
                )
            )

    # Catálogo x categorias
    used_categories = {p["category_id"] for p in products}
    empty = sorted(category_ids - used_categories)
    if empty:
        names = ", ".join(f"{cid} ({dict((c['category_id'], c['name']) for c in categories)[cid]})" for cid in empty)
        out.append(
            Finding(
                "warning",
                "categories",
                "Categorias cadastradas sem nenhum produto",
                names,
                "A ferramenta de catálogo informa explicitamente que a "
                "categoria existe mas está sem itens, em vez de responder "
                "'não encontrei' — são coisas diferentes para o cliente.",
            )
        )

    # Promoções
    by_product: dict[int, list[dict]] = {}
    for pr in promotions:
        by_product.setdefault(pr["product_id"], []).append(pr)
    for pid, rows in by_product.items():
        if len(rows) > 1:
            actives = [r for r in rows if r["is_active"]]
            detail = f"produto {pid} ({products_by_id.get(pid, {}).get('name', '?')}): " + ", ".join(
                f"{r['discount_percent']:.0f}%/{'ativa' if r['is_active'] else 'inativa'}" for r in rows
            )
            out.append(
                Finding(
                    "info" if len(actives) <= 1 else "warning",
                    "promotions",
                    "Produto com mais de uma promoção cadastrada",
                    detail,
                    "Promoções não são cumulativas (§6.2): a view "
                    "`v_product_pricing` aplica apenas o MAIOR desconto entre "
                    "as ativas e ignora as inativas.",
                )
            )
        if any(r["is_active"] for r in rows) and pid in products_by_id:
            prod = products_by_id[pid]
            if prod["availability"] != AVAILABILITY_IN_STOCK:
                out.append(
                    Finding(
                        "warning",
                        "promotions",
                        "Promoção ativa em produto não disponível",
                        f"produto {pid} ({prod['name']}), disponibilidade={prod['availability']}",
                        "O agente informa o preço promocional mas deixa claro "
                        "que o item não está disponível (§7.3).",
                    )
                )

    # Produtos
    for p in products:
        if p["status"] == "discontinued" and p["stock_quantity"] > 0:
            out.append(
                Finding(
                    "info",
                    "products",
                    "Produto descontinuado com estoque remanescente",
                    f"produto {p['product_id']} ({p['name']}), estoque={p['stock_quantity']}",
                    "Tratado como fora de catálogo: não entra em recomendação. "
                    "Se perguntado diretamente, o agente informa a "
                    "descontinuação e sugere equivalentes (§7.3).",
                )
            )
        if p["status"] == "coming_soon":
            sold = [
                i["order_id"]
                for i in order_items
                if i["product_id"] == p["product_id"]
            ]
            if sold:
                out.append(
                    Finding(
                        "warning",
                        "products",
                        "Produto marcado como lançamento futuro já consta em pedidos",
                        f"produto {p['product_id']} ({p['name']}), pedidos={sorted(sold)}",
                        "Pedidos históricos são preservados; o catálogo segue "
                        "tratando o item como indisponível para nova venda.",
                    )
                )

    # Pedidos sem rastreio quando o status sugere despacho
    for o in orders:
        if o["status"] in {"shipped", "delivered"} and not o["tracking_code"]:
            out.append(
                Finding(
                    "warning",
                    "orders",
                    "Pedido despachado/entregue sem código de rastreio",
                    f"pedido {o['order_id']} (status={o['status']})",
                    "O agente informa que o código não está no sistema e "
                    "oferece acionar a equipe, em vez de inventar um código.",
                )
            )

    return out


def _write_database(
    db_path: Path,
    *,
    categories: list[dict],
    products: list[dict],
    customers: list[dict],
    orders: list[dict],
    order_items: list[dict],
    promotions: list[dict],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)

        def insert(table: str, rows: list[dict]) -> None:
            if not rows:
                return
            cols = list(rows[0].keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
            conn.executemany(sql, rows)

        # Ordem importa: FKs ligadas.
        insert("categories", categories)
        insert("products", products)
        insert("customers", customers)
        insert("orders", orders)
        # Itens órfãos já foram reportados no audit; aqui são filtrados.
        valid_products = {p["product_id"] for p in products}
        valid_orders = {o["order_id"] for o in orders}
        insert(
            "order_items",
            [
                i
                for i in order_items
                if i["product_id"] in valid_products and i["order_id"] in valid_orders
            ],
        )
        insert("promotions", [p for p in promotions if p["product_id"] in valid_products])

        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [
                ("etl_version", __version__),
                ("built_at", datetime.now().astimezone().isoformat(timespec="seconds")),
                ("n_products", str(len(products))),
                ("n_orders", str(len(orders))),
                ("n_customers", str(len(customers))),
                ("n_promotions", str(len(promotions))),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _write_report(findings: list[Finding], *, counts: dict[str, int]) -> None:
    """Gera docs/DATA_QUALITY.md — o relatório é artefato versionado do ETL."""
    docs = settings.root_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    warnings = [f for f in findings if f.severity == "warning"]
    infos = [f for f in findings if f.severity == "info"]

    lines: list[str] = [
        "# Relatório de qualidade dos dados",
        "",
        "> Arquivo **gerado automaticamente** por `emporio build` "
        "(`src/emporio/data/loader.py`). Não editar à mão.",
        "",
        f"Gerado em: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "## Volumetria carregada",
        "",
        "| Tabela | Linhas |",
        "| --- | --- |",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(counts.items())]
    lines += [
        "",
        "## Resumo",
        "",
        f"- **{len(warnings)}** ocorrências de atenção (`warning`)",
        f"- **{len(infos)}** observações (`info`)",
        "",
        "Nenhuma delas interrompe a carga. A política do ETL é *degradar com "
        "transparência*: carregar o que existe, registrar o que não fecha e "
        "nunca deixar o agente apresentar como certo um número que a origem "
        "contradiz.",
        "",
    ]

    for title, group in (("Ocorrências de atenção", warnings), ("Observações", infos)):
        if not group:
            continue
        lines += [f"## {title}", ""]
        for f in group:
            lines += [
                f"### `{f.table}` — {f.issue}",
                "",
                f"- **Onde:** {f.detail}",
                f"- **Decisão:** {f.decision}",
                "",
            ]

    (docs / "DATA_QUALITY.md").write_text("\n".join(lines), encoding="utf-8")
