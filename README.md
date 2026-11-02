# Téo — agente de atendimento da Empório da Música

Agente de atendimento por mensagem de texto para a **Empório da Música**, loja
de instrumentos musicais de Campo Grande/MS. Responde sobre catálogo, preço,
condição de pagamento, frete, pedidos e as regras do manual interno da loja —
consultando dados reais antes de afirmar qualquer coisa.

Desafio Técnico AI Engineer · [Artefact](https://www.artefact.com/) · Gian Ferraz

```
Cliente ──▶ Téo (LLM) ──▶ decide: é dado ou é política?
                 │
                 ├─▶ 11 ferramentas determinísticas ──▶ SQLite (catálogo, pedidos, promoções)
                 └─▶ busca no manual de políticas  ──▶ BM25 sobre o PDF, com citação de seção
```

> **A ideia central do projeto em uma frase:** o LLM decide *o que* perguntar e
> *como* conversar; preço, prazo e disponibilidade saem de código Python
> testado. Um agente de atendimento que erra um preço não é um bug de
> usabilidade — o próprio manual da loja (§7.1) chama isso de risco de
> propaganda enganosa.

---

## Sumário

- [Como rodar](#como-rodar)
- [O que o agente faz](#o-que-o-agente-faz)
- [Arquitetura](#arquitetura)
- [Decisões técnicas e por quê](#decisões-técnicas-e-por-quê)
- [Tratamento dos dados](#tratamento-dos-dados)
- [Suposições e ambiguidades resolvidas](#suposições-e-ambiguidades-resolvidas)
- [Como eu sei que funciona](#como-eu-sei-que-funciona)
- [Exemplos de conversa](#exemplos-de-conversa)
- [Limitações conhecidas](#limitações-conhecidas)
- [O que eu faria com mais tempo](#o-que-eu-faria-com-mais-tempo)
- [Uso de assistentes de código](#uso-de-assistentes-de-código)
- [Estrutura do repositório](#estrutura-do-repositório)

---

## Como rodar

Requisitos: **Python 3.10+**. Nada além disso — sem Docker, sem banco externo,
sem modelo local para baixar.

### 1. Instalar

```bash
git clone https://github.com/ReversaOfficial/DesafioT-cnicoAIEngineer2026-GianFerraz-.git
cd DesafioT-cnicoAIEngineer2026-GianFerraz-

python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

### 2. Configurar o provedor do modelo

```bash
cp .env.example .env     # Windows: copy .env.example .env
```

Abra o `.env` e preencha **uma** chave. O default é o Google Gemini porque a
chave é **gratuita e não pede cartão de crédito** — quem for avaliar o projeto
consegue rodar sem custo:

```ini
EMPORIO_LLM_PROVIDER=gemini
GEMINI_API_KEY=sua_chave_aqui
```

> Pegue a chave em **<https://aistudio.google.com/apikey>** → *Create API key*.
> Leva menos de um minuto e o nível gratuito é suficiente para a avaliação
> inteira.

Outros provedores funcionam trocando duas linhas — veja
[Modelo e provedor](#2-modelo-e-provedor).

### 3. Preparar os dados

```bash
python -m emporio.cli build
```

Carrega os seis CSVs em um SQLite local, indexa o manual de políticas e grava
o relatório de qualidade dos dados em [`docs/DATA_QUALITY.md`](docs/DATA_QUALITY.md).

### 4. Conversar

```bash
# chat no terminal, mostrando as consultas que o agente faz
python -m emporio.cli chat --trace
```

```bash
# interface web em http://127.0.0.1:8000
python -m emporio.cli serve
```

```bash
# pergunta avulsa, sem abrir sessão
python -m emporio.cli ask "Quais violões vocês têm até R$ 1.000?"
```

### Diagnóstico

Se algo não subir, este comando diz exatamente o que falta:

```bash
python -m emporio.cli doctor
```

### Todos os comandos

| Comando | O que faz |
| --- | --- |
| `build` | CSV → SQLite + índice de políticas + relatório de qualidade |
| `chat` | Chat interativo no terminal (`--trace` mostra as consultas) |
| `ask "..."` | Pergunta avulsa |
| `serve` | API HTTP + interface web de chat |
| `tool <nome> k=v` | Executa uma ferramenta **sem LLM** — confere um preço ou prazo sem gastar chamada de modelo |
| `eval` | Roda o conjunto de avaliação do agente |
| `demo --script ...` | Roda um roteiro e grava a transcrição em Markdown |
| `sessions` | Sessões gravadas e estatística de uso de ferramentas |
| `doctor` | Diagnóstico do ambiente |

Se você tem `make`: `make install`, `make build`, `make chat`, `make test`,
`make eval`.

### Rodar sem chave de API

A camada determinística — que é onde vivem as regras de negócio — não depende
de LLM nenhum:

```bash
pytest                                                   # 271 testes
python -m emporio.cli tool buscar_produtos categoria=Violões preco_max=1000
python -m emporio.cli tool calcular_preco product_id=127 forma_pagamento=10x
```

---

## O que o agente faz

**Persona.** O agente se chama **Téo**. O §7.1 do manual define o tom do
atendimento: *"informal mas profissional... como se estivesse conversando com
um amigo que entende de música"*. A persona foi escrita a partir dessa
definição, não inventada: mensagens curtas de conversa, tratamento por "você",
valores em formato brasileiro, emoji com parcimônia e nenhum em assunto
delicado. Ele é transparente sobre ser um assistente virtual se perguntarem, e
nunca diz que é humano.

**Capacidades.**

| Pergunta do cliente | Como o agente resolve |
| --- | --- |
| "Quais violões até R$ 1.000?" | `buscar_produtos` com teto de preço, só itens em estoque |
| "Quanto custa o Takamine GD20?" | `detalhar_produto` + `calcular_preco` — e **não** oferece a promoção de 12% que está cadastrada mas inativa |
| "Dá pra parcelar em 10x?" | `calcular_preco` aplica as faixas de parcela mínima do §3.1 e informa o máximo real |
| "Quanto fica no PIX?" | 5% de desconto — **salvo** se houver promoção ativa, pois §6.2 proíbe acumular |
| "Posso devolver meu pedido?" | `verificar_elegibilidade_devolucao` confere identidade, conta o prazo e devolve veredito com data-limite |
| "Onde está meu pedido?" | `consultar_pedido`, **após** confirmar nome ou telefone (§9 / LGPD) |
| "Vocês estão abertos?" | `informacoes_da_loja` com fuso da loja (UTC-4); fora do expediente informa quando a equipe retorna |
| "Frete pra Dourados?" | `calcular_frete` informa modalidades e prazos e **não inventa valor** — fora da região metropolitana depende de cotação |
| "Tem saxofone?" | A categoria existe no cadastro mas está **sem produtos**: o agente diz que não trabalha com o item em vez de prometer consultar o estoque |
| "Vendem cordas e palhetas?" | Acessório — a loja só vende instrumentos (§1). Redireciona sem inventar loja parceira |
| "Me explica o IR do MEI?" | Fora de escopo. Recusa em uma frase e volta ao atendimento |

---

## Arquitetura

```
┌──────────────────────────────────────────────────────────────────┐
│  Interfaces                                                      │
│  CLI (rich)        API FastAPI + UI web        pytest/eval       │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  AgentService — sessão + histórico persistido                    │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│  Agent — laço ReAct com function calling                         │
│  prompt do sistema · coerção de argumentos · poda de histórico   │
│  teto de iterações · log estruturado por turno                   │
└──────┬──────────────────────────────────────────┬────────────────┘
       │                                          │
┌──────▼─────────────────┐              ┌─────────▼────────────────┐
│  LLM (provider-agnóstico)│            │  11 ferramentas          │
│  Gemini · OpenAI-compat  │            │  determinísticas         │
│  Anthropic · Ollama      │            │  (nenhuma chama LLM)     │
└──────────────────────────┘            └─────────┬────────────────┘
                                                  │
                             ┌────────────────────┴───────────────┐
                             │                                    │
                   ┌─────────▼──────────┐          ┌──────────────▼─────────┐
                   │  SQLite da operação │          │  Índice de políticas   │
                   │  ETL dos 6 CSVs     │          │  BM25 sobre o PDF      │
                   │  + view de preço    │          │  chunks por seção      │
                   └─────────────────────┘          └────────────────────────┘
```

**Fluxo de um turno.** A mensagem entra no `AgentService`, que carrega o
histórico da sessão. O `Agent` monta o prompt do sistema (incluindo data, hora
e se a loja está aberta **agora**) e chama o LLM com os 11 schemas de
ferramenta. Se o modelo pede ferramentas, elas são executadas em Python, os
resultados voltam como mensagens e o laço repete — até no máximo 6 iterações.
Cada turno é persistido e registrado em `logs/turns.jsonl`.

Detalhes em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Decisões técnicas e por quê

### 1. Abordagem: function calling com ferramentas tipadas + RAG só onde faz sentido

**O que eu escolhi.** Um laço ReAct com *function calling* nativo, onde o
modelo escolhe entre 11 ferramentas Python determinísticas. Dados estruturados
(catálogo, pedidos, promoções) são consultados por SQL parametrizado dentro
dessas ferramentas. O manual de políticas, que é texto corrido, é consultado
por RAG.

**Por que não um agente de texto-para-SQL.** Foi a alternativa que considerei
primeiro, e descartei por três motivos concretos nesta base:

1. O catálogo tem 65 produtos e as dimensões de consulta do atendimento são
   poucas e estáveis (texto livre, categoria, faixa de preço, marca,
   disponibilidade). A flexibilidade do SQL gerado não é usada.
2. As regras de negócio estão nos `JOIN`s. A base tem 25 promoções, das quais
   **apenas 4 estão ativas**, e três produtos têm promoção duplicada. Um SQL
   gerado que esqueça `WHERE is_active = 1` oferece desconto vencido ao
   cliente — exatamente o que o §7.3 proíbe. Com ferramentas fixas, essa regra
   mora em uma *view* (`v_product_pricing`) e tem teste.
3. Determinismo. A mesma pergunta produz a mesma consulta, sempre. Com
   geração de SQL, não.

**Por que não RAG em tudo.** Preço e estoque mudam e precisam de agregação e
filtro — colocar o catálogo em um índice vetorial transformaria "violões até
R$ 1.000" em um problema de similaridade, quando é um `WHERE price <= 1000`.
RAG ficou onde o dado é efetivamente não-estruturado: o manual de políticas.

**Por que não LangChain / LlamaIndex.** O laço do agente tem ~150 linhas
([`agent/loop.py`](src/emporio/agent/loop.py)) e nelas estão três defesas que
eu quis escrever explicitamente: coerção de argumentos malformados, poda de
histórico que não quebra o formato de nenhum provedor, e uma resposta final
forçada quando o teto de iterações é atingido. Com um framework, o prompt real
passa a ser decidido por ele, a versão dita o comportamento, e responder "por
que o modelo chamou esta ferramenta?" vira arqueologia. Para 11 ferramentas e
um fluxo linear, o framework custaria mais do que entrega.

*Quando eu mudaria de ideia:* acima de ~25 ferramentas (aí vale roteamento
hierárquico), com necessidade de execução paralela de ferramentas, ou com
múltiplos agentes especializados se coordenando. Nenhum desses é o caso aqui.

**A fronteira que mais importa: o que o LLM não decide.** Três coisas saem de
código, não do modelo:

- **Preço e parcelamento** ([`tools/pricing.py`](src/emporio/tools/pricing.py)).
  É a regra que um LLM "quase acerta": ele lembra dos 5% do PIX e esquece que
  não acumulam com preço promocional; ou oferece 10x em um produto de R$ 359,00,
  cuja parcela ficaria em R$ 35,90 — abaixo do mínimo de R$ 100 da faixa.
- **Contagem de prazo** ([`tools/orders.py`](src/emporio/tools/orders.py)).
  "Posso devolver?" exige contar dias corridos desde uma data e cruzar com três
  prazos distintos (7 de arrependimento, 30 de troca por defeito, 90 de
  garantia legal), sabendo qual se aplica ao motivo alegado.
- **Horário de funcionamento** ([`tools/store.py`](src/emporio/tools/store.py)).
  "Vocês estão abertos agora?" é aritmética de fuso (Campo Grande é UTC-4), não
  interpretação de prosa.

### 2. Modelo e provedor

**Default: Google Gemini (`gemini-2.5-flash`).** Critério decisivo: a chave do
AI Studio é gratuita e não exige cartão. Um projeto que precisa ser avaliado
por outra pessoa não pode ter um paywall na porta. O modelo também é rápido o
suficiente para um atendimento que o manual limita a 5 minutos de resposta
(§7.1) e tem function calling nativo confiável.

**Provider-agnóstico de verdade.** A camada LLM é uma interface
(`Message` / `ToolCall` / `LLMResponse`) com três adaptadores:

| Provedor | Como ativar |
| --- | --- |
| Gemini | `EMPORIO_LLM_PROVIDER=gemini` + `GEMINI_API_KEY` |
| OpenAI | `EMPORIO_LLM_PROVIDER=openai` + `OPENAI_API_KEY` |
| Anthropic | `EMPORIO_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` |
| Groq (grátis) | `EMPORIO_LLM_PROVIDER=groq` + `GROQ_API_KEY` |
| OpenRouter | `EMPORIO_LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY` |
| Ollama (local) | `EMPORIO_LLM_PROVIDER=ollama` (sem chave) |

Um único adaptador cobre OpenAI, Groq, OpenRouter e Ollama, porque os quatro
expõem `/v1/chat/completions` com o mesmo contrato.

**Por que REST direto em vez dos SDKs oficiais.** Os três adaptadores somam
~250 linhas, não trazem árvore de dependências, não quebram em atualização de
SDK, e deixam o protocolo de function calling visível. Em um projeto cujo
objetivo é demonstrar como a orquestração funciona, esconder isso atrás de um
SDK seria contraproducente. O preço é escrever à mão os detalhes de cada API —
e eles são reais, cada um com teste:

- O Gemini rejeita `parameters` com `properties` vazio em tipo `OBJECT`, então
  ferramenta sem argumento precisa omitir `parameters` por completo.
- O Gemini casa resultado de ferramenta pelo **nome**; a OpenAI, por **id**.
- A Anthropic exige que `tool_result`s consecutivos sejam agrupados em **uma
  única** mensagem.

### 3. Arquitetura de retrieval: BM25 com chunking por seção

O manual tem 8 páginas e virou **26 chunks**.

**Chunking consciente da estrutura, não de tamanho.** O corte segue o sumário
do documento. O motivo é direto: o agente cita a política para o cliente
("conforme o §4.1"), então a fronteira do chunk precisa coincidir com a
fronteira da regra. Um corte cego a cada N caracteres separa "7 dias corridos"
do título "Direito de Arrependimento" e produz resposta confiante e errada.
Seções de capítulo que trazem só o título são anexadas ao chunk seguinte,
preservando o contexto do pai. Há fallback por página se o documento mudar de
estrutura, e um teste falha se qualquer seção do sumário deixar de ser
encontrada — assim uma revisão do PDF quebra o build de forma visível em vez de
degradar em silêncio.

**BM25 em vez de embeddings.** Decisão tomada pelo tamanho e pela natureza do
corpus:

- São ~26 chunks. Recuperação densa resolve descasamento de vocabulário, que é
  um problema de corpus grande e heterogêneo. Aqui o cliente usa praticamente
  as palavras do documento: "troca", "frete", "parcelar", "garantia".
- Onde o vocabulário realmente difere, o descasamento é previsível e pequeno
  ("devolver" ↔ "arrependimento", "abre" ↔ "funcionamento"). Um mapa de
  sinônimos do domínio cobre isso com precisão e custo zero, com um
  *stemmer* mínimo para flexão verbal ("chegar" → "chega").
- Elimina dependência de API de embedding ou modelo local de ~500 MB. O índice
  é determinístico, roda offline e é **explicável** — dá para apontar
  exatamente qual termo casou.

**Medição.** Sobre 17 perguntas realistas de atendimento:
**recall@3 = 17/17** e **recall@1 = 10/17**. O recall@3 é a métrica que
importa, porque a ferramenta entrega três trechos ao modelo. Os 7 casos que não
lideram erram todos para uma seção *vizinha do mesmo assunto* ("quanto tempo de
garantia" traz §8.2 antes de §8.1), e esse contexto vizinho é informação
legítima para a resposta — trocar de ranqueador por isso seria otimizar a
métrica errada.

*Quando eu mudaria:* com múltiplos documentos, catálogo de políticas por
região, ou perguntas parafraseadas longe do vocabulário da fonte. O caminho
seria híbrido — BM25 + denso com *reciprocal rank fusion* — mantendo o
chunking por seção, que continua correto.

### 4. Estratégia de prompt

O prompt do sistema ([`agent/prompts.py`](src/emporio/agent/prompts.py)) tem
~7.500 caracteres em quatro camadas, nesta ordem:

1. **Identidade** — quem o Téo é e como fala, derivado do §7.1.
2. **Contexto dinâmico** — data, hora e se a loja está aberta, **recalculados a
   cada turno**. Sem isso o modelo responde "estamos abertos" num domingo,
   porque não tem relógio.
3. **Regras duras** — oito proibições verificáveis, cada uma **citando a seção
   do manual que a origina**. Escritas como "nunca informe preço sem chamar
   `calcular_preco`", não como "seja preciso com preços": instrução
   verificável é seguida, conselho genérico não. Dar o *motivo* da restrição
   (e não só a ordem) reduz violação na prática mais do que qualquer outra
   coisa que eu testei.
4. **Roteamento dado × política** — a decisão que o enunciado pede
   explicitamente, dita de forma direta e reforçada nas descrições das
   ferramentas.

Duas coisas que considero parte da estratégia de prompt, e não detalhe:

- **As descrições das ferramentas são prompt.** Cada uma diz não só o que faz,
  mas *quando* usar e quando não usar. Um parâmetro mal descrito custa mais
  acurácia do que qualquer ajuste de temperatura.
- **Os retornos das ferramentas carregam orientação.** Quando uma busca vem
  vazia, o payload explica *por quê* (categoria sem itens, item sem estoque,
  teto de preço baixo) e o que fazer. Um "nenhum resultado" seco é o que faz o
  modelo improvisar.

**Defesa contra injeção de prompt.** A regra 8 instrui a ignorar instruções
embutidas na mensagem do cliente que tentem mudar as regras, o papel ou revelar
o prompt — respondendo com naturalidade e seguindo o atendimento. Há um caso de
avaliação e um exemplo de conversa cobrindo isso.

**Temperatura 0.3.** Baixa o suficiente para disciplina de ferramenta e valores
estáveis, alta o suficiente para a conversa não soar robótica — o que o §7.1
pede explicitamente.

### 5. Persistência do histórico

**Implementei, e o motivo é do domínio.** O canal é WhatsApp (§7). Conversa de
WhatsApp não começa e termina numa sessão: o cliente pergunta o preço hoje,
desaparece, e volta amanhã com *"e aquele violão, ainda tem?"*. Sem histórico
persistido o agente perde o fio e o cliente repete tudo.

SQLite em arquivo — o volume é de atendimento de loja e a dependência é zero.
O schema separa `messages` de `tool_calls`, para que a trilha de auditoria
(quem consultou o quê, quando) sobreviva à poda de contexto. CLI e interface
web compartilham o mesmo armazenamento, então dá para começar uma conversa no
terminal e continuar no navegador.

**Poda de contexto.** O histórico enviado ao modelo é limitado a 24 mensagens,
e a poda nunca deixa o contexto começar com um resultado de ferramenta solto —
formato que todos os provedores rejeitam. Resultados de ferramenta acima de
6.000 caracteres são truncados com aviso.

### 6. Interface

Três, cada uma com um propósito:

- **CLI** é a principal. Tem `--trace`, que mostra cada ferramenta chamada com
  argumentos e tempo. Para quem avalia o agente, ver *por que* ele respondeu
  aquilo vale mais do que a resposta.
- **API HTTP** mostra o agente como serviço — o caminho real de produção seria
  um webhook de WhatsApp apontando para `POST /api/chat`.
- **UI web** é um chat em arquivo único, sem build e sem framework, com o mesmo
  botão de "consultas". É onde o atendimento se parece com o que a loja usaria.

---

## Tratamento dos dados

O ETL ([`data/loader.py`](src/emporio/data/loader.py)) carrega os seis CSVs em
SQLite. É idempotente — recriar do zero é sempre seguro.

**Transformações aplicadas:**

| Transformação | Por quê |
| --- | --- |
| Colunas `*_norm` com texto sem acento e minúsculo | SQLite não tem função de remoção de acentos; normalizar no ETL faz "violao yamaha" encontrar "Violão Yamaha" sem custo em tempo de consulta |
| Campo derivado `availability` (`in_stock`/`out_of_stock`/`discontinued`/`coming_soon`) | Funde `status` + `stock_quantity` em um vocabulário fechado, para que nenhuma ferramenta reimplemente (e divirja em) a regra |
| `brand` extraída do nome | Filtro por marca é pedido recorrente. Trata marcas compostas — sem isso "Music Man Bass" viraria marca "Music" |
| `search_blob` com nome + marca + categoria + descrição + specs | Faz "tampo sólido" ser buscável, já que está só dentro do JSON de especificações |
| `specs` JSON achatado em texto | Mesma razão |
| `phone_digits` só com dígitos | Casa telefone em qualquer formatação na verificação de identidade |
| `is_metro_cg` por cidade | Alimenta a regra de frete do §5.1 (Região Metropolitana de Campo Grande, Lei Complementar estadual 51/2010) |
| View `v_product_pricing` | Aplica o **maior desconto entre as promoções ativas** e ignora as inativas. §6.2: promoções não são cumulativas; §7.3: promoção vencida não pode ser prometida |
| Banco aberto em modo somente-leitura pelas ferramentas | As ferramentas do agente nunca devem escrever na base da operação; deixar isso no driver é mais confiável que disciplina |

**O ETL não conserta a origem em silêncio.** Ele audita, decide uma política
explícita e documenta tudo em [`docs/DATA_QUALITY.md`](docs/DATA_QUALITY.md),
gerado automaticamente. Dado de operação corrigido sem rastro é pior do que
dado errado conhecido. O que esta base tem de verdade:

| Achado | Decisão |
| --- | --- |
| **3 categorias sem nenhum produto** (sopro madeiras, sopro metais, cordas orquestrais) | As ferramentas informam explicitamente que a categoria existe mas está vazia — dizer "não encontrei" seria impreciso, prometer o produto seria pior |
| **2 pedidos com total divergente** da soma dos itens (pedidos 3 e 20) | `orders.total_brl` prevalece: é o valor efetivamente transacionado. O agente nunca recalcula pedido fechado, porque preços e promoções mudam |
| **3 produtos com promoção duplicada** (93, 96, 99) | A view aplica apenas uma, a de maior desconto entre as ativas (§6.2) |
| **1 produto descontinuado com 13 unidades** em estoque (136) | Tratado como fora de catálogo; não entra em recomendação. Se perguntado diretamente, informa a descontinuação e sugere equivalentes (§7.3) |
| **1 produto "lançamento futuro" que já consta em 2 pedidos** (130) | Pedidos históricos preservados; o catálogo segue tratando como indisponível para nova venda |
| **1 produto ativo com estoque zero** (96) | `out_of_stock`: o agente informa e oferece alternativa (§7.3) |
| **Pedidos despachados sem código de rastreio** | O agente informa que o código não está no sistema e oferece acionar a equipe, em vez de inventar um código |

---

## Suposições e ambiguidades resolvidas

O enunciado pede para assumir uma interpretação razoável e documentá-la. Estas
são todas:

| # | Ambiguidade | Decisão e justificativa |
| --- | --- | --- |
| 1 | O §3 diz "cartão de crédito — até 12x — parcela mínima de R$ 100", mas o §3.1 detalha faixas (até 3x sem mínimo, 4–6x mínimo R$ 80, 7–12x mínimo R$ 100). **Conflitam.** | Tratei o §3 como resumo e o **§3.1 como regra vigente**, por ser a mais específica. É o princípio normal de interpretação de norma. |
| 2 | O §1.2 registra `(67) 3341-4444` como "Telefone / WhatsApp"; o §7 apresenta `(67) 3321-4500` como o canal de WhatsApp. | Informo **os dois**, o primeiro como telefone da loja e o segundo como WhatsApp, e a ferramenta devolve a observação de que o manual divergiu. Preferi sinalizar a ambiguidade a escolher um lado em silêncio. |
| 3 | O e-mail aparece como `contato@emporiodamusica.com.br` na capa e **com acento** no §1.2. | Adotei a versão sem acento, que é o domínio válido. |
| 4 | O manual afirma catálogo de "mais de 300 instrumentos"; a base tem **65**. | A base é a fonte da verdade para o que o agente oferece. O agente nunca cita o tamanho do catálogo. |
| 5 | O manual afirma que a loja vende sopro e cordas orquestrais; essas categorias estão **vazias**. | O catálogo manda. O agente informa que não trabalha com esses itens no momento. |
| 6 | A base **não registra a data real de entrega**, apenas `estimated_delivery`. O prazo de arrependimento conta "após o recebimento" (§4.1). | Uso `estimated_delivery` como referência e **devolvo a ressalva junto do veredito**, para o agente poder ser transparente sobre a aproximação. Sem data de entrega, uso a data do pedido, que é conservador. |
| 7 | O §4.1 trata de "compras online"; a base não distingue canal. | Tratei todos os pedidos da base como online, que é o cenário em que um agente de WhatsApp atua. |
| 8 | O §4.4 exclui da troca produtos de liquidação **com aviso explícito de "venda final"**; a base registra o nome da campanha, mas não o aviso. | Não decido automaticamente. Quando o item tem campanha de liquidação cadastrada, o resultado **sinaliza para conferência humana**. Aplicar a exceção por suposição recusaria devolução legítima. |
| 9 | O manual não cobre **cancelamento antes do despacho**, situação dos pedidos `pending` e `confirmed`. | O agente explica que o prazo de devolução ainda não começou e **escala**, citando o §10 ("consultar a gerência quando a política não resolve"). Não promete prazo nem condição de estorno. |
| 10 | Não existe calendário de feriados na base, e o §2 diz "domingos e feriados, fechado". | Detecto apenas domingo. Prazos em dias úteis excluem só sábado e domingo e são apresentados como **estimativa**. |
| 11 | O §5.1 fala em "região metropolitana de Campo Grande" sem listar municípios. | Usei a composição oficial da RMCG (Lei Complementar estadual 51/2010). Dos 10 municípios, só Campo Grande aparece na base de clientes. |
| 12 | O §7.1 manda "sugerir lojas parceiras" para acessórios, mas o manual **não lista nenhuma**. | O agente redireciona sem citar nome de loja. Inventar parceiro seria pior do que não sugerir. |
| 13 | Os dados de operação terminam em **março de 2026** — todos os pedidos estão no passado em relação a hoje. | O relógio real é a referência default. A variável `EMPORIO_NOW` permite congelar a data, o que torna os exemplos reproduzíveis e os prazos testáveis. Está documentada no `.env.example`. |
| 14 | O enunciado não define se o agente pode concluir venda. | Não pode. Ele levanta informação, explica a regra e encaminha — o manual o descreve como apoio à equipe de atendimento, e o §10 manda escalar na dúvida. |

---

## Como eu sei que funciona

Duas camadas de verificação, com propósitos diferentes.

### Testes automatizados — a camada determinística

```bash
pytest                                      # 271 testes, ~15s
pytest --cov=emporio --cov-report=term      # cobertura: 83%
```

Nenhum teste precisa de rede ou de chave de API, e todos rodam contra os
**dados reais do desafio**. Para uma base deste tamanho isso é vantagem, não
atalho: as asserções passam a valer sobre os casos de verdade em vez de um
cenário idealizado que nunca pega o bug real. Destaques:

- **LGPD testada por tentativa de vazamento**: o teste procura nome, telefone e
  valores dentro do payload não autorizado, em vez de só checar uma flag.
- **Prazos testados nas fronteiras**: último dia do prazo vale, dia seguinte
  não; e os quatro status de pedido que mudam o veredito.
- **Parcelamento nas fronteiras**: R$ 480,00 permite exatamente 6x de R$ 80,00;
  R$ 479,00 não.
- **O laço do agente é exercitado com um dublê de LLM programável** — cobre
  encadeamento de ferramentas, teto de iterações, ferramenta inexistente,
  argumento ausente e coerção de tipo, sem custo e sem variância.
- **A tradução de formato dos três provedores é testada sem rede**, fixando os
  detalhes de protocolo que custam um HTTP 400.

### Avaliação do agente — a camada com LLM

```bash
python -m emporio.cli eval --report
```

O conjunto dourado em [`eval/cases.json`](eval/cases.json) tem **20 casos** e
mede três coisas que falham por motivos diferentes: **disciplina de consulta**
(chamou ferramenta antes de afirmar?), **roteamento** (chamou a ferramenta
certa?) e **conteúdo**.

Metade dos casos é **negativa** — verifica o que a resposta *não* pode conter.
É aí que um agente de atendimento causa prejuízo real: oferecer promoção
vencida, somar desconto que não acumula, prometer parcelamento abaixo do mínimo,
revelar dado de pedido sem verificar identidade, inventar frete que depende de
cotação. As verificações casam por número e palavra-chave, nunca por frase
inteira: o agente tem liberdade de redação, não de fato.

Casos cujo resultado depende do tempo carregam a própria data de referência, de
modo que o score não muda conforme o dia da execução. O comando sai com código
1 se algum caso reprovar, servindo direto como *gate* de CI.

Resultado da última execução: [`docs/EVALUATION.md`](docs/EVALUATION.md).

---

## Exemplos de conversa

Cinco conversas em [`examples/`](examples/). **São transcrições reais**, geradas
por `emporio demo` a partir dos roteiros em
[`examples/scripts/`](examples/scripts/) — não são diálogos escritos à mão.
Cada uma registra, turno por turno, as ferramentas que o agente efetivamente
consultou.

| Exemplo | O que demonstra |
| --- | --- |
| [01 — Primeiro violão: da busca ao orçamento](examples/01-busca-e-orcamento.md) | Fluxo comercial completo: busca com teto de preço, detalhe, PIX × parcelamento, frete |
| [02 — Devolução fora do prazo](examples/02-devolucao-fora-do-prazo.md) | **O caso não trivial.** Verificação de identidade, contagem de prazo vencido, alternativa real, e sustentação da recusa sob pressão |
| [03 — Os três jeitos de dizer não](examples/03-escopo-e-honestidade.md) | Categoria vazia, acessório fora de escopo, assunto fora da loja — e um "sim" quando a pergunta é musical |
| [04 — Domingo, loja fechada, pedido sem rastreio](examples/04-pedido-fora-do-horario.md) | Horário de retorno, identificação pelo telefone, e dizer que o código não existe em vez de inventá-lo |
| [05 — Promoção vencida e injeção de prompt](examples/05-promocao-vencida.md) | Campanha inativa, PIX que não acumula com promoção, e tentativa de sobrescrever as instruções |

Para regenerar todas:

```bash
python scripts/gerar_exemplos.py
```

---

## Limitações conhecidas

Sendo direto sobre o que não está resolvido:

1. **O agente não transaciona.** Não fecha venda, não registra pedido, não
   processa pagamento, não autoriza devolução. Foi decisão de escopo
   (suposição 14), mas é a lacuna mais evidente para uso real — faltaria uma
   camada de escrita com idempotência, confirmação em dois passos e trilha de
   auditoria.
2. **Prazos em dias úteis são otimistas.** Sem calendário de feriados, só
   sábado e domingo são excluídos. Em semana de feriado o prazo informado erra
   para menos. Mitigado apresentando como estimativa; a correção é uma tabela
   de feriados nacionais e de Campo Grande/MS.
3. **O frete fora da região metropolitana não tem valor.** O manual remete a
   cotação por CEP, peso e dimensão, e a base não tem peso nem dimensão. O
   agente informa modalidades e prazos e diz que o valor sai na finalização.
   Preferi isso a estimar um número.
4. **A data de recebimento é a estimada, não a real.** Afeta diretamente a
   contagem do prazo de arrependimento. O veredito sempre vem com a ressalva,
   mas é aproximação.
5. **Busca só nas dimensões previstas.** É o custo da escolha por ferramentas
   tipadas em vez de texto-para-SQL. "Qual o violão mais vendido?" não é
   respondível — exigiria agregação sobre `order_items` que eu não expus, e
   com 22 itens de pedido a resposta seria estatisticamente vazia de todo modo.
6. **Sem suporte a imagem ou áudio.** Em WhatsApp real, cliente manda foto do
   instrumento com defeito e áudio. O projeto é só texto, como o enunciado pede.
7. **Uma sessão é um cliente.** A identificação por telefone não é
   autenticação. Para produção, a identidade viria do próprio canal (o número
   de origem no WhatsApp), não de o cliente digitar o nome.
8. **O índice de políticas é reconstruído no deploy, não versionado.** Está em
   `var/`, que é gitignored. Primeira execução do `build` custa ~1s.
9. **Sem streaming de resposta.** O cliente espera a resposta completa. Em
   WhatsApp isso é natural; numa UI web, streaming melhoraria a percepção de
   latência.
10. **O eval depende do modelo.** O score varia com provedor e versão de
    modelo. É esperado — é uma medida do sistema completo, não de regressão de
    código. Para isso existem os 271 testes determinísticos.

---

## O que eu faria com mais tempo

Em ordem de retorno, pensando em levar isto a produção:

1. **Camada de escrita com confirmação em dois passos.** Reservar item, abrir
   solicitação de devolução, registrar reclamação — com idempotência por chave
   de requisição e confirmação explícita do cliente antes de qualquer efeito.
   É o que transforma o agente de consulta em atendimento de ponta a ponta.
2. **Handoff para humano como ferramenta de primeira classe.** Hoje o agente
   diz "vou verificar com a equipe". Deveria abrir um ticket com o resumo da
   conversa, o motivo do escalonamento e o SLA do §7.3 (24 horas úteis),
   devolvendo um número de protocolo ao cliente.
3. **Webhook de WhatsApp Business API.** A API já está pronta para isso; falta
   verificação de assinatura, fila para lidar com rajada de mensagens,
   agrupamento de mensagens fragmentadas (cliente que manda cinco linhas
   seguidas) e idempotência por id de mensagem.
4. **Retrieval híbrido.** BM25 + denso com *reciprocal rank fusion*, mantendo o
   chunking por seção. Só vale quando o corpus de políticas crescer — hoje
   seria complexidade sem ganho mensurável, e eu quis evitar
   over-engineering pelo gosto da técnica.
5. **Eval mais largo, com juiz LLM para tom.** Os 20 casos cobrem fato e
   roteamento. Tom e empatia (que o §7.1 trata como requisito) precisam de um
   avaliador diferente — um LLM-as-judge com rubrica, rodando sobre conversas
   de múltiplos turnos, não só perguntas isoladas.
6. **Observabilidade de verdade.** Hoje é JSONL por turno. O caminho é
   OpenTelemetry com *span* por chamada de ferramenta e por chamada de modelo,
   e um painel com distribuição de ferramentas, taxa de turnos que batem o teto
   de iterações e custo por atendimento.
7. **Cache semântico de perguntas frequentes.** "Qual o horário?" e "onde fica?"
   devem ser a maioria do volume real e não precisam de LLM. Um cache na
   entrada cortaria custo e latência sem mudar a arquitetura.
8. **Avaliação de regressão de prompt em CI.** Rodar o eval a cada mudança no
   prompt do sistema, comparando com o baseline, para que ajuste de redação não
   quebre disciplina de ferramenta silenciosamente.
9. **Testes de propriedade sobre a precificação.** `hypothesis` gerando valores
   e formas de pagamento, verificando invariantes: PIX nunca cobra mais que
   tabela, parcela nunca abaixo do mínimo da faixa, soma das parcelas igual ao
   total.

---

## Uso de assistentes de código

Usei o **Claude Code** (Anthropic) como par de programação ao longo de todo o
projeto, e considero importante ser transparente sobre como — o enunciado pede
justamente isso.

**O que eu fiz e o que deleguei.** As decisões de arquitetura foram minhas e
são a parte do trabalho que eu defenderia numa discussão técnica: a fronteira
entre o que o LLM decide e o que fica em código determinístico; a escolha de
ferramentas tipadas em vez de texto-para-SQL; BM25 em vez de embeddings para um
corpus de 8 páginas; o chunking por seção para permitir citação; exigir
verificação de identidade antes de expor dados de pedido; e tratar as
divergências do manual sinalizando a ambiguidade em vez de escolher um lado em
silêncio.

**Como usei a ferramenta na prática:**

- **Exploração dos dados primeiro, código depois.** Antes de escrever qualquer
  coisa, usei o assistente para perfilar as seis tabelas e o PDF em busca de
  casos de borda. Foi assim que apareceram os achados que moldaram o projeto:
  só 4 das 25 promoções estão ativas, 3 categorias estão vazias, 2 pedidos têm
  total divergente, 1 produto descontinuado ainda tem estoque. Esses achados
  são a razão de a camada de ferramentas ter o formato que tem.
- **Escrita de código sob especificação minha.** Eu definia a regra e o
  comportamento esperado — "as faixas de parcela mínima do §3.1, e pedir 10x
  num item de R$ 359 deve recusar explicando o motivo e informando o máximo" —
  e revisava a implementação. O trabalho repetitivo (adaptadores de provedor,
  serialização, estrutura dos testes) foi onde o ganho de velocidade foi maior.
- **Testes como forma de revisão.** Vários testes nasceram de eu pedir ao
  assistente para tentar quebrar uma regra que eu tinha acabado de escrever.
  Três assunções minhas caíram assim: o nome do cliente que eu havia suposto
  para um pedido estava errado (e a verificação de identidade corretamente
  recusou), minha expectativa de que toda busca por "violao nylon" retornaria
  só violões estava errada (um ukulele barítono de cordas de nylon casa
  legitimamente), e meu limiar de recall@1 estava acima do real — em vez de
  ajustar o ranqueador para passar no teste, documentei o número verdadeiro e o
  motivo.
- **Revisão de prosa técnica.** README e comentários passaram por revisão de
  clareza. O conteúdo é meu; a forma melhorou.

**Onde eu discordei do assistente.** Em dois pontos segui contra a primeira
sugestão: a proposta inicial incluía LangChain para o laço do agente, e eu
preferi as ~150 linhas explícitas pelos motivos que estão na seção de decisões;
e houve sugestão de embeddings para o retrieval, que eu considerei
over-engineering para 26 chunks.

**Minha leitura sobre isso.** Assistente de código muda o custo de escrever,
não o de decidir. O que ficou mais rápido foi a digitação e o scaffolding; o
que continuou custando o mesmo foi olhar os dados, escolher onde colocar a
fronteira entre modelo e código, e decidir o que fazer quando a fonte se
contradiz. É nessa segunda parte que está o projeto.

---

## Estrutura do repositório

```
.
├── README.md                     este arquivo
├── pyproject.toml                dependências e configuração de ferramentas
├── .env.example                  modelo de configuração (nada secreto)
├── Makefile                      atalhos
│
├── data/                         material fornecido pela loja (intocado)
│   ├── desafio_tecnico_ai_eng - *.csv
│   └── políticas_da_loja.pdf
│
├── docs/
│   ├── ARCHITECTURE.md           detalhe de arquitetura e fluxo de um turno
│   ├── DATA_QUALITY.md           relatório gerado pelo ETL
│   ├── EVALUATION.md             resultado gerado pelo eval
│   └── desafio_tecnico_ai_eng_artefact.pdf
│
├── examples/                     conversas reais com o agente
│   ├── 0X-*.md                   transcrições geradas por `emporio demo`
│   └── scripts/                  roteiros que as produzem
│
├── eval/cases.json               conjunto dourado de avaliação (20 casos)
│
├── scripts/gerar_exemplos.py     regenera as transcrições
│
├── src/emporio/
│   ├── config.py                 configuração central, fuso e data de referência
│   ├── text_utils.py             normalização PT-BR compartilhada
│   ├── cli.py                    interface de terminal
│   ├── api.py                    API HTTP
│   ├── evaluation.py             harness de avaliação
│   ├── web/index.html            UI de chat
│   ├── data/                     ETL CSV → SQLite + auditoria
│   ├── retrieval/                chunking por seção + BM25
│   ├── tools/                    as 11 ferramentas determinísticas
│   ├── llm/                      adaptadores de provedor
│   ├── agent/                    persona, prompt e laço ReAct
│   └── memory/                   persistência de conversa
│
└── tests/                        271 testes, sem rede e sem chave de API
```

---

<sub>Projeto desenvolvido para a primeira fase do processo seletivo de AI
Engineer da Artefact. A Empório da Música é uma loja fictícia; os dados são os
fornecidos no desafio.</sub>
