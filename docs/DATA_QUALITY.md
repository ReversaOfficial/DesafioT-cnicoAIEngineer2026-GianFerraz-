# Relatório de qualidade dos dados

> Arquivo **gerado automaticamente** por `emporio build` (`src/emporio/data/loader.py`). Não editar à mão.

Gerado em: 2026-11-02 09:29:44 -0300

## Volumetria carregada

| Tabela | Linhas |
| --- | --- |
| `categories` | 9 |
| `customers` | 50 |
| `order_items` | 22 |
| `orders` | 20 |
| `products` | 65 |
| `promotions` | 25 |

## Resumo

- **2** ocorrências de atenção (`warning`)
- **6** observações (`info`)

Nenhuma delas interrompe a carga. A política do ETL é *degradar com transparência*: carregar o que existe, registrar o que não fecha e nunca deixar o agente apresentar como certo um número que a origem contradiz.

## Ocorrências de atenção

### `categories` — Categorias cadastradas sem nenhum produto

- **Onde:** 6 (Instrumentos de Sopro (Madeiras)), 7 (Instrumentos de Sopro (Metais)), 8 (Cordas Orquestrais)
- **Decisão:** A ferramenta de catálogo informa explicitamente que a categoria existe mas está sem itens, em vez de responder 'não encontrei' — são coisas diferentes para o cliente.

### `products` — Produto marcado como lançamento futuro já consta em pedidos

- **Onde:** produto 130 (Kala KA-ABP-CTG Barítono Cedar), pedidos=[3, 14]
- **Decisão:** Pedidos históricos são preservados; o catálogo segue tratando o item como indisponível para nova venda.

## Observações

### `orders` — Total do pedido difere da soma dos itens a preço de tabela

- **Onde:** pedido 3: total=3450.00 vs itens=3498.00
- **Decisão:** `orders.total_brl` prevalece — é o valor efetivamente transacionado (pode conter desconto ou frete da época). O agente nunca recalcula total de pedido fechado.

### `orders` — Total do pedido difere da soma dos itens a preço de tabela

- **Onde:** pedido 20: total=1400.00 vs itens=1488.00
- **Decisão:** `orders.total_brl` prevalece — é o valor efetivamente transacionado (pode conter desconto ou frete da época). O agente nunca recalcula total de pedido fechado.

### `promotions` — Produto com mais de uma promoção cadastrada

- **Onde:** produto 93 (Martin D-28 Dreadnought Natural): 15%/inativa, 10%/inativa
- **Decisão:** Promoções não são cumulativas (§6.2): a view `v_product_pricing` aplica apenas o MAIOR desconto entre as ativas e ignora as inativas.

### `promotions` — Produto com mais de uma promoção cadastrada

- **Onde:** produto 96 (Giannini GF-3D Dreadnought Sunburst): 18%/inativa, 40%/inativa
- **Decisão:** Promoções não são cumulativas (§6.2): a view `v_product_pricing` aplica apenas o MAIOR desconto entre as ativas e ignora as inativas.

### `promotions` — Produto com mais de uma promoção cadastrada

- **Onde:** produto 99 (Tagima Classical Elétrico Nylon Sunburst): 8%/inativa, 14%/inativa
- **Decisão:** Promoções não são cumulativas (§6.2): a view `v_product_pricing` aplica apenas o MAIOR desconto entre as ativas e ignora as inativas.

### `products` — Produto descontinuado com estoque remanescente

- **Onde:** produto 136 (Ibanez Bass 2X Electric Bass), estoque=13
- **Decisão:** Tratado como fora de catálogo: não entra em recomendação. Se perguntado diretamente, o agente informa a descontinuação e sugere equivalentes (§7.3).
