# Arquitetura

Complemento ao [README](../README.md), que traz as decisões e suas
justificativas. Aqui o foco é *como as peças se encaixam*.

## Princípio organizador

Uma única pergunta guiou todas as fronteiras do projeto:

> **O que acontece se o modelo errar isto?**

Quando a resposta é "o cliente recebe um preço errado", "o prazo informado está
errado" ou "dado pessoal vaza", aquilo não é responsabilidade do modelo. Vai
para código Python com teste. Quando a resposta é "a frase fica menos natural",
é responsabilidade do modelo.

Daí a divisão: o LLM decide **o que** consultar e **como** conversar; as
ferramentas determinam **o que é verdade**.

## Camadas

```
src/emporio/
├── config.py        configuração, fuso da loja, data de referência
├── text_utils.py    normalização PT-BR (compartilhada por ETL e busca)
│
├── data/            ETL: 6 CSVs → SQLite + auditoria de qualidade
├── retrieval/       manual de políticas: chunking por seção + BM25
├── tools/           11 ferramentas determinísticas (nenhuma chama LLM)
├── llm/             adaptadores de provedor atrás de uma interface
├── agent/           persona, prompt do sistema, laço ReAct, serviço de sessão
├── memory/          persistência de conversa e trilha de auditoria
│
├── cli.py           interface de terminal
├── api.py           API HTTP + UI web
└── evaluation.py    harness de avaliação
```

Dependências apontam para baixo. `tools/` não conhece `llm/`; `llm/` não
conhece `tools/`. Quem os costura é `agent/`, e é só ali que existe
orquestração.

Um efeito prático disso: **a maior parte do sistema é testável sem LLM**. Os
271 testes rodam sem rede e sem chave, porque as regras de negócio não estão no
prompt — estão em funções.

## Fluxo de um turno

```
1. Cliente envia mensagem
         │
2. AgentService.open_session()
   └── carrega histórico da sessão do SQLite de conversas
         │
3. Agent.send()
   ├── build_system_prompt()
   │   └── injeta data, hora e se a loja está ABERTA agora (fuso UTC-4)
   │
   ├──▶ LLM.complete(system, histórico, 11 schemas de ferramenta)
   │         │
   │         ├── respondeu texto? ──────────────▶ fim do laço
   │         │
   │         └── pediu ferramentas?
   │                │
   │                ├── _coerce_arguments()      alinha tipos ao schema
   │                ├── spec.handler(**args)     executa Python puro
   │                ├── erro? vira {"erro": ...} e volta ao modelo como dado
   │                └── resultado serializado entra no histórico
   │                        │
   │                        └──▶ volta ao LLM (até 6 iterações)
   │
   ├── teto atingido? uma última chamada SEM ferramentas, para fechar o turno
   └── grava logs/turns.jsonl
         │
4. AgentService persiste turno + chamadas de ferramenta
         │
5. Resposta ao cliente (+ trace, se pedido)
```

### Três defesas do laço

São elas que justificam ter escrito o laço à mão em vez de usar framework.

**Coerção de argumentos.** Modelos menores mandam `"preco_max": "1000"` em vez
de número, inventam parâmetro que não existe no schema, e passam `"null"` como
string. O laço alinha o que é alinhável e descarta o resto, em vez de estourar
um `TypeError` no meio do atendimento.

**Poda de histórico que respeita o formato.** O contexto é limitado a 24
mensagens. A poda avança até encontrar uma mensagem de usuário, porque todo
provedor rejeita um histórico que começa com resultado de ferramenta solto ou
que tem resultado sem a chamada correspondente.

**Resposta final forçada.** Se o modelo insistir em consultar até o teto de
iterações, o laço faz uma última chamada **sem oferecer ferramentas**,
instruindo-o a responder com o que já apurou. Sem isso, o cliente receberia
silêncio no caso exato em que a pergunta era difícil.

## A camada de ferramentas

Onze ferramentas, agrupadas por natureza do dado:

| Ferramenta | Fonte | Natureza |
| --- | --- | --- |
| `buscar_produtos` | SQLite | dado |
| `detalhar_produto` | SQLite | dado |
| `listar_categorias` | SQLite | dado |
| `promocoes_ativas` | SQLite | dado |
| `calcular_preco` | SQLite + regras §3/§3.1/§6.2 | **dado + regra** |
| `calcular_frete` | regras §5.1/§5.2 | regra |
| `consultar_pedido` | SQLite (dado pessoal) | dado |
| `identificar_cliente` | SQLite (dado pessoal) | dado |
| `verificar_elegibilidade_devolucao` | SQLite + regras §4/§8 | **dado + regra** |
| `consultar_politicas` | índice BM25 do PDF | política |
| `informacoes_da_loja` | §1.2/§2 estruturados | política |

As duas em negrito são o coração do projeto: cruzam o dado da operação com a
regra do manual e devolvem um veredito, não matéria-prima para o modelo
raciocinar.

### Contratos que toda ferramenta respeita

1. **Retorno é `dict` serializável.** Nada de objeto que o LLM não consiga ler.
2. **Chaves em português.** O modelo responde em português; manter o payload no
   mesmo idioma reduz tradução espúria e mantém o contexto coerente.
3. **Erro é dado, não exceção.** `ToolError` vira `{"erro": ..., "sugestao":
   ...}` e volta ao modelo, que então pede ao cliente o que falta.
4. **Resultado vazio vem com diagnóstico.** `buscar_produtos` explica *por que*
   não achou — categoria sem itens, item sem estoque, teto de preço baixo — e
   isso é o que permite ao agente dar uma resposta útil em vez de improvisar.
5. **Regra citada junto do dado.** Quando a resposta depende de uma política, o
   payload traz a seção (`§4.1`, `§6.2`), para o agente poder citar a fonte.
6. **Banco em modo somente-leitura.** As ferramentas nunca escrevem na base da
   operação; isso está no driver, não na disciplina de quem escreve o código.

## Dados

### Esquema

```
categories ──┬── products ──┬── order_items ── orders ── customers
             │              │
             │              └── promotions
             │
             └── v_product_pricing  (view: preço vigente)
```

A view `v_product_pricing` é onde mora a regra de promoção:

```sql
COALESCE((SELECT MAX(discount_percent) FROM promotions
           WHERE product_id = p.product_id AND is_active = 1), 0)
```

`MAX` entre as **ativas**, porque §6.2 proíbe cumulatividade (então escolhe-se
uma) e §7.3 proíbe prometer promoção vencida (então ignoram-se as inativas).
Esta base tem 25 promoções e só 4 ativas, além de três produtos com promoção
duplicada — sem essa regra centralizada, cada ferramenta erraria do seu jeito.

### Campos derivados no ETL

`availability` funde `status` + `stock_quantity` em um vocabulário fechado;
`brand` sai do nome do produto; `search_blob` concatena nome, marca, categoria,
descrição e specs já normalizados; `phone_digits` guarda só dígitos;
`is_metro_cg` resolve a regra de frete por cidade. Detalhe e justificativa de
cada um no [README](../README.md#tratamento-dos-dados).

## Retrieval

```
políticas_da_loja.pdf
   │
   ├── extract_pages()        pypdf + reconstrução de espaçamento
   │                          remove cabeçalho/rodapé repetidos
   │
   ├── _locate_sections()     localiza os 28 títulos do sumário usando
   │                          fold_preserving (minúsculo, sem acento,
   │                          MESMO comprimento → índice vale no original)
   │
   ├── _split_long()          fatia seção longa em bullet ou frase
   │
   └── 26 chunks com {seção, título, página, texto}
            │
            └── BM25Index(tokens)   título conta 2x (boost de campo)
                     │
                     └── search(query)
                            └── expand_query()  sinônimos do domínio
                                                + stemmer mínimo de verbo
```

O truque de `fold_preserving` merece nota: `strip_accents` normal pode mudar o
comprimento da string (um caractere decompõe em vários), o que desalinha
índices. A versão que preserva comprimento mapeia caractere a caractere, e é o
que permite achar o título no texto dobrado e recortar o trecho **no texto
original**, com formatação intacta.

## Observabilidade

| Onde | O que |
| --- | --- |
| `logs/turns.jsonl` | Um objeto por turno: ferramentas usadas, argumentos, duração, tokens, se bateu o teto de iterações. Formato pensado para `jq` |
| `var/conversations.sqlite3` | Tabela `tool_calls` com a trilha de auditoria, que sobrevive à poda de contexto |
| `emporio sessions` | Agregado de uso de ferramentas: chamadas, sucessos, latência média |
| CLI `--trace` / UI "consultas" | Trace ao vivo, por turno |

Resultado de ferramenta marcada como `sensitive` **não vai para o log** — não
faz sentido criar um segundo lugar com dado pessoal só para observabilidade.

## Por que não há Docker

Dependeria de Docker instalado para rodar algo que roda com `pip install -e .`
e Python 3.10. O `emporio doctor` cobre o problema que o Docker resolveria aqui
("por que não funciona na minha máquina?") sem adicionar um pré-requisito. Em
produção, com webhook e fila, Docker passaria a fazer sentido.
