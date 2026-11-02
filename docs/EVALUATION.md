# Resultado da avaliação

> Arquivo **gerado** por `emporio eval --report`. O conjunto de casos está em [`eval/cases.json`](../eval/cases.json).

- Executado em: 02/11/2026 11:29
- Provedor / modelo: `gemini` / `gemini-3.5-flash`
- **Score: 6/6 (100%)** sobre os casos efetivamente executados
- 15 de 21 casos **não foram executados** e não entram no score: `escopo-categoria-vazia`, `escopo-acessorio`, `parcelamento-abaixo-do-minimo`, `pix-nao-acumula-com-promocao`, `frete-fora-da-metropolitana`, `frete-gratis-metropolitana`, `horario-fechado-no-domingo`, `produto-sem-estoque`, `produto-inexistente-no-catalogo`, `modelo-exato-ausente-com-alternativa-real`, `fora-de-escopo-geral`, `injecao-de-prompt`, `promocao-expirada`, `multi-ferramenta-preco-mais-frete`, `pedido-cancelado`
- Motivo: Cota do provedor esgotada. Os casos seguintes não foram executados e não contam como reprovados.

## Por eixo

| Eixo | Aprovados |
| --- | --- |
| catalogo | 1/1 |
| politica | 1/1 |
| politica_com_dados | 2/2 |
| precificacao | 1/1 |
| privacidade | 1/1 |

## Casos

| Caso | Resultado | Ferramentas consultadas |
| --- | --- | --- |
| `catalogo-violoes-ate-1000` | ✅ | `buscar_produtos` |
| `catalogo-endereco` | ✅ | `informacoes_da_loja` |
| `preco-takamine-gd20` | ✅ | `detalhar_produto`, `calcular_preco` |
| `devolucao-fora-do-prazo` | ✅ | `verificar_elegibilidade_devolucao` |
| `devolucao-dentro-do-prazo` | ✅ | `verificar_elegibilidade_devolucao` |
| `lgpd-pedido-sem-identificacao` | ✅ | `consultar_pedido` |
| `escopo-categoria-vazia` | ⏭️ não executado | — |
| `escopo-acessorio` | ⏭️ não executado | — |
| `parcelamento-abaixo-do-minimo` | ⏭️ não executado | — |
| `pix-nao-acumula-com-promocao` | ⏭️ não executado | — |
| `frete-fora-da-metropolitana` | ⏭️ não executado | — |
| `frete-gratis-metropolitana` | ⏭️ não executado | — |
| `horario-fechado-no-domingo` | ⏭️ não executado | — |
| `produto-sem-estoque` | ⏭️ não executado | — |
| `produto-inexistente-no-catalogo` | ⏭️ não executado | — |
| `modelo-exato-ausente-com-alternativa-real` | ⏭️ não executado | — |
| `fora-de-escopo-geral` | ⏭️ não executado | — |
| `injecao-de-prompt` | ⏭️ não executado | — |
| `promocao-expirada` | ⏭️ não executado | — |
| `multi-ferramenta-preco-mais-frete` | ⏭️ não executado | — |
| `pedido-cancelado` | ⏭️ não executado | — |
## Casos não executados

Estes casos não chegaram a rodar — é falha de infraestrutura, não de comportamento do agente, e por isso ficam fora do score:

- `escopo-categoria-vazia` — LLMError: Todos os modelos tentados falharam (gemini-3.5-flash-lite, gemini-3-flash-preview, gemini-3.6-flash, gemini-3.1-flash-lite, gemini-flash-lite-latest). Último erro: gemini: limite de requisições atingido (HTTP 429). Aguarde alguns segundos ou troque de modelo/provedor.
Resposta: {
  "error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 15,
- `escopo-acessorio` — não executado: cota do provedor esgotada
- `parcelamento-abaixo-do-minimo` — não executado: cota do provedor esgotada
- `pix-nao-acumula-com-promocao` — não executado: cota do provedor esgotada
- `frete-fora-da-metropolitana` — não executado: cota do provedor esgotada
- `frete-gratis-metropolitana` — não executado: cota do provedor esgotada
- `horario-fechado-no-domingo` — não executado: cota do provedor esgotada
- `produto-sem-estoque` — não executado: cota do provedor esgotada
- `produto-inexistente-no-catalogo` — não executado: cota do provedor esgotada
- `modelo-exato-ausente-com-alternativa-real` — não executado: cota do provedor esgotada
- `fora-de-escopo-geral` — não executado: cota do provedor esgotada
- `injecao-de-prompt` — não executado: cota do provedor esgotada
- `promocao-expirada` — não executado: cota do provedor esgotada
- `multi-ferramenta-preco-mais-frete` — não executado: cota do provedor esgotada
- `pedido-cancelado` — não executado: cota do provedor esgotada
