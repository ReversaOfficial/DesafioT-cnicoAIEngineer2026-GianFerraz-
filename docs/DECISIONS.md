# Log de decisões

Este documento registra **cada ação tomada no projeto, em ordem, com o motivo**.

O [README](../README.md) justifica as escolhas de arquitetura em profundidade.
Aqui o recorte é outro: um log cronológico, um commit por entrada, no formato
*pergunta → escolha → por que nesta base → alternativa descartada → quando eu
mudaria de ideia*. A última linha é a que me interessa mais: uma decisão que eu
não sei em que condição revisaria não é uma decisão, é um palpite que deu certo.

O princípio que amarra tudo, e que apliquei a cada fronteira do sistema:

> **"O que acontece se o modelo errar isto?"** Quando a resposta é preço errado,
> prazo errado ou dado pessoal vazado, vai para código com teste. Quando é uma
> frase menos simpática, fica com o modelo.

---

## Índice

| # | Decisão | Commit |
|---|---|---|
| 1 | Dependências leves e auditáveis, sem SDK de provedor | `3cbf00d` |
| 2 | ETL concentra todo o tratamento de dados num lugar | `87a1a8d` |
| 3 | BM25 com chunking por seção, não por tamanho | `b3d62bc` |
| 4 | Ferramentas tipadas em vez de texto-para-SQL | `a843bf0` |
| 5 | Laço ReAct próprio, com três defesas explícitas | `c98f637` |
| 6 | Três interfaces, e um comando que roda ferramenta sem LLM | `b8a62a6` |
| 7 | Testes contra os dados reais, não contra mocks | `5496354` |
| 8 | Conjunto dourado com metade dos casos negativos | `f5ebfd9` |
| 9 | README com a alternativa descartada em cada decisão | `d29a0ae` |
| 10 | Correções que só a API de verdade revelou | `a540889` |
| 11 | Separar caso reprovado de caso não executado | `5c5cd7b` |
| 12 | Cadeia de modelos de reserva, nunca silenciosa | `bfb0a75` |
| 13 | Oferecer modelo próximo quando o exato não existe | `9b30052` |
| 14 | Contrariar o linter num falso positivo verificado | `154455a` |
| 15 | Transcrições geradas, não escritas à mão | `b593cca` |
| 16 | Não insistir com backoff em erro que é do modelo | `2fca292` |
| 17 | Caminho de erro da CLI legível | `b7b5c51` |
| 18 | Site online com a parte que nunca quebra em primeiro plano | `5b91239` |

---

## 1. Dependências leves e auditáveis, sem SDK de provedor

**Commit:** `3cbf00d` · *estrutura inicial do projeto e dados do desafio*

**A pergunta.** Que dependências um agente de atendimento precisa carregar?

**A escolha.** Layout `src/`, configuração central (provedor, caminhos, data de
referência) e utilitários de normalização PT-BR compartilhados entre ETL e
busca. Seis dependências: `httpx` para falar com os provedores via REST, `pypdf`
para o manual, `typer`/`rich` para a CLI, `fastapi`/`uvicorn` para a API.

**Por que.** Projeto leve e auditável. Quem avalia consegue ler a árvore de
dependências inteira.

**A alternativa descartada.** Os SDKs oficiais de cada provedor. Decisão
detalhada na entrada 5, onde ela tem consequência concreta.

**Quando eu mudaria de ideia.** Se precisasse de streaming, batching ou upload
de arquivo — aí o SDK paga a própria complexidade.

---

## 2. ETL concentra todo o tratamento de dados num lugar

**Commit:** `87a1a8d` · *ETL dos CSVs para SQLite com auditoria de qualidade*

**A pergunta.** Onde moram as decisões de tratamento de dados — espalhadas pelas
ferramentas, ou num lugar só?

**A escolha.** Um ETL idempotente que carrega as seis tabelas em SQLite e
concentra quatro decisões:

- **colunas `*_norm`** com texto sem acento, para que "violao yamaha" encontre
  "Violão Yamaha" sem custo em tempo de consulta;
- **campo derivado `availability`**, que funde `status` + `stock_quantity` num
  vocabulário fechado, para que nenhuma ferramenta reimplemente a regra;
- **`brand` extraída do nome**, tratando marcas compostas como Music Man;
- **view `v_product_pricing`** aplicando o maior desconto **entre as ativas**.

**Por que nesta base.** Promoção não é cumulativa (§6.2) e promoção vencida não
pode ser prometida ao cliente (§7.3) — se a view não filtrasse por ativa, o
agente ofereceria desconto expirado com preço calculado corretamente, que é o
pior tipo de erro: convincente.

**A auditoria não conserta a origem em silêncio.** Cada divergência é registrada
com a decisão tomada em [`DATA_QUALITY.md`](DATA_QUALITY.md). Achados reais:
duas divergências entre total do pedido e soma dos itens, três produtos com
promoção duplicada, um produto descontinuado ainda com estoque, um lançamento
futuro que já aparece em pedidos, três categorias sem nenhum produto.

**Quando eu mudaria de ideia.** Catálogo grande e mutável pediria ETL
incremental com captura de mudança, em vez de recarga completa.

---

## 3. BM25 com chunking por seção, não por tamanho

**Commit:** `b3d62bc` · *indexação do manual de políticas com BM25*

**A pergunta.** Como recuperar uma regra de um PDF de 8 páginas de um jeito que
permita **citar a seção** ao cliente?

**A escolha.** Duas partes. O corte segue o sumário do documento, não um tamanho
fixo; seções de capítulo que trazem só o título são carregadas para o chunk
seguinte, preservando o contexto do pai. E Okapi BM25 em Python puro, com
expansão de consulta do domínio. Há fallback por página se o documento mudar.

**Por que o chunking é por seção.** O agente cita a política, então a fronteira
do chunk precisa coincidir com a fronteira da regra. Um corte cego a cada N
caracteres separa "7 dias corridos" do título "Direito de Arrependimento" e
produz **resposta confiante e errada** — o modelo recebe o número sem o contexto
que o qualifica.

**Por que BM25 e não embeddings.** O corpus tem ~26 chunks e o cliente usa
praticamente as palavras do manual. Onde o vocabulário difere ("devolver" ×
"arrependimento", "abre" × "funcionamento") o descasamento é *previsível*, e um
mapa de sinônimos cobre com precisão. Resultado: zero dependência de API de
embedding ou modelo local de 500 MB, índice determinístico e explicável.

**Medido.** Sobre 17 perguntas realistas de atendimento: **recall@3 = 17/17**,
recall@1 = 10/17.

**Quando eu mudaria de ideia.** Corpus crescendo ou vocabulário do cliente
divergindo do documento — aí vale híbrido BM25 + denso com RRF. O caminho está
registrado no README.

---

## 4. Ferramentas tipadas em vez de texto-para-SQL

**Commit:** `a843bf0` · *camada de ferramentas determinísticas e motor de regras*

**A pergunta.** O modelo consulta os dados escrevendo SQL, ou através de
ferramentas tipadas?

**A escolha.** Onze ferramentas tipadas sobre o SQLite e o índice de políticas.
Nenhuma chama LLM. **Esta é a decisão central do projeto:** o modelo decide o
que perguntar e como conversar; preço, prazo e disponibilidade saem de código
testável.

**O que virou código, e por que cada um:**

- **`pricing`** — §3, §3.1 e §6.2 do manual. Resolve o caso que um LLM "quase
  acerta": os 5% do PIX **não** acumulam com preço promocional, e o parcelamento
  respeita parcela mínima por faixa (até 3x sem mínimo, 4-6x mínimo R$ 80,00,
  7-12x mínimo R$ 100,00).
- **`orders`** — contagem de prazo em aritmética, não em interpretação. Cruza os
  três prazos (7 dias de arrependimento, 30 de troca por defeito, 90 de garantia
  legal), trata pedido cancelado, pendente e em trânsito de forma distinta, e
  devolve veredito com data-limite e seção citada.
- **`consultar_pedido` e `verificar_elegibilidade_devolucao`** exigem
  confirmação de nome ou telefone antes de revelar dado pessoal. Número de
  pedido é sequencial e trivial de adivinhar; o §9 trata de LGPD.
- **`buscar_produtos`** explica **por que** a busca veio vazia (categoria sem
  itens, item sem estoque, teto de preço baixo). "Nenhum resultado" seco é o que
  faz o modelo improvisar; com o diagnóstico ele responde o que o §7.3 pede.
- **`store`** — horário estruturado em código, não recuperado como prosa, porque
  "vocês estão abertos agora?" exige aritmética de fuso (UTC-4).

**Divergências do manual são expostas, não resolvidas em silêncio** — telefone
do WhatsApp no §1.2 contra o §7, e-mail acentuado. O agente sinaliza a
ambiguidade em vez de escolher um lado sozinho.

**A alternativa descartada.** Agente de texto-para-SQL. Com catálogo pequeno e
regras estáveis, SQL gerado pelo modelo traz risco sem ganho: **21 das 25
promoções estão inativas**, e uma consulta que esqueça `is_active = 1` oferece
desconto vencido. O mesmo para um join que ressuscite promoção expirada.

**Quando eu mudaria de ideia.** Catálogo grande com perguntas analíticas abertas
("qual categoria mais cresceu no trimestre?") — aí texto-para-SQL com validação
de esquema e lista branca de tabelas passa a valer o risco.

---

## 5. Laço ReAct próprio, com três defesas explícitas

**Commit:** `c98f637` · *adaptadores de provedor, laço ReAct e persistência*

**A pergunta.** Framework de agente, ou laço escrito à mão?

**A escolha.** Laço próprio, ~150 linhas, com function calling nativo e teto de
iterações. E o LLM atrás de uma interface única (`Message`/`ToolCall`/
`LLMResponse`): trocar de provedor é mudar uma variável de ambiente, e o agente
não sabe com quem fala.

**As três defesas que eu quis escrever explicitamente** — são o motivo de não
usar framework:

1. **Coerção de argumentos.** Modelo menor manda `"1000"` string onde se espera
   número, inventa parâmetro e passa `"null"` como texto. O laço ajusta o
   ajustável e descarta o resto, em vez de estourar `TypeError`.
2. **Poda de histórico** que nunca deixa o contexto começar com resultado de
   ferramenta solto — formato que todo provedor rejeita.
3. **Ao bater o teto de iterações**, faz uma última chamada **sem ferramentas**
   para fechar o turno com o que apurou, em vez de devolver vazio.

**Erro de ferramenta volta ao modelo como conteúdo, não como exceção**, para ele
pedir ao cliente o dado que falta em vez de o atendimento morrer.

**Por que REST direto em vez dos SDKs.** Os três adaptadores somam ~250 linhas,
nenhuma árvore de dependência, nada quebra em atualização de SDK, e o protocolo
de function calling fica visível. Erro HTTP vira mensagem acionável: 401 aponta
o `.env`, 429 explica a cota.

- **`gemini`** é o default: a chave do AI Studio é gratuita e não pede cartão,
  então quem avalia roda sem custo.
- **`openai_compat`**: um adaptador cobre OpenAI, Groq, OpenRouter e Ollama, que
  compartilham `/v1/chat/completions`.
- **`anthropic`**: Messages API, agrupando `tool_result`s consecutivos numa
  única mensagem — mandar um por mensagem é erro de formato.

**Prompt em quatro camadas:** identidade; contexto dinâmico (data, hora e se a
loja está aberta, injetados a cada turno); regras duras escritas como proibições
verificáveis, cada uma com a seção do manual que a origina; e roteamento dado ×
política. Inclui defesa contra injeção de prompt vinda da mensagem do cliente.

**Persistência em SQLite porque o canal é WhatsApp (§7)** — conversa de WhatsApp
não começa e termina numa sessão. Resultado de ferramenta sensível é omitido do
log, para não criar um segundo lugar com dado pessoal.

**Quando eu mudaria de ideia.** Múltiplos agentes coordenados ou planejamento de
várias etapas com replanejamento — aí a infraestrutura de um framework passa a
compensar o que ela esconde.

---

## 6. Três interfaces, e um comando que roda ferramenta sem LLM

**Commit:** `b8a62a6` · *interface de terminal, API HTTP e UI web de chat*

**A pergunta.** Que interface serve a quem vai avaliar o projeto?

**A escolha.** CLI como interface principal, com `--trace` mostrando cada
ferramenta chamada, com argumentos e tempo. **Ver por que o agente respondeu
aquilo vale mais do que a resposta.**

Comandos: `build`, `chat` (com `/trace` `/reset` `/prompt` `/ferramentas`),
`ask`, `tool` (executa uma ferramenta **sem LLM**), `sessions`, `serve`,
`doctor`, `eval`, `demo`, `models`.

**O `tool` existe para conferir preço ou prazo sem gastar chamada de modelo** —
e virou a forma mais direta de verificar uma regra de negócio na fonte.

**O `doctor`**, quando falta chave, aponta o que ainda funciona sem ela em vez
de só reclamar. **A API sobe mesmo sem chave:** `/api/health` informa o que está
faltando e `/api/chat` devolve 503 com mensagem acionável, em vez de o processo
morrer no import.

**UI web em arquivo único, sem build e sem framework:** chat estilo WhatsApp com
indicador de digitação, sugestões e um botão que revela as consultas de cada
resposta. Sessão compartilhada com a CLI pelo mesmo histórico persistido — dá
para começar no terminal e continuar no browser.

`schema.sql` e a página web são declarados como `package-data`: são lidos em
tempo de execução e faltariam numa instalação não-editável.

**Quando eu mudaria de ideia.** Em produção o canal é o WhatsApp de verdade, e a
UI web viraria painel de operação, não interface de cliente.

---

## 7. Testes contra os dados reais, não contra mocks

**Commit:** `5496354` · *suíte de testes sobre os dados reais do desafio*

**A pergunta.** A suíte roda contra fixtures idealizadas ou contra a base do
desafio?

**A escolha.** Contra a base real. Com este volume isso é vantagem, não atalho:
as asserções passam a valer sobre os casos de verdade — categoria vazia,
descontinuado com estoque, promoção duplicada — em vez de um cenário idealizado
que nunca pega o bug real. Nada depende de rede ou de chave de API.

**O que a suíte cobre, e por que cada escolha:**

- **`pricing`** nas fronteiras exatas: R$ 480,00 permite exatamente 6x de
  R$ 80,00; R$ 479,00 não. E teste explícito de que o desconto duplo do PIX com
  promoção **não** acontece.
- **`orders`** — LGPD verificada **por tentativa de vazamento**: o teste procura
  nome, telefone e valores dentro do payload não autorizado, em vez de checar
  uma flag. Flag passa quando a implementação mente; busca no payload não.
- **`agent`** — laço exercitado com um **dublê de LLM programável**. Cobre
  encadeamento de ferramentas, teto de iterações, ferramenta inexistente,
  argumento ausente e coerção de tipo. O dublê determinístico permite testar o
  orquestrador sem testar o modelo.
- **`llm`** — tradução de formato dos três provedores sem rede. Fixa os detalhes
  de protocolo que custam um HTTP 400: Gemini rejeitando `properties` vazio,
  Gemini casando resultado por nome e OpenAI por id, Anthropic exigindo
  `tool_result`s consecutivos agrupados.
- **`retrieval`** — cobertura de seções do manual como guarda de regressão. Se
  uma revisão do PDF mudar a estrutura, o teste falha aqui em vez de o agente
  passar a responder política pela metade. Verifica também que os números das
  regras (7, 30, 90, 500) não foram separados da regra pelo chunking.

**Uma decisão que vale registrar:** o `recall@1` é guarda de regressão em 55%,
com o motivo documentado no próprio teste. Os casos que não lideram erram para
uma seção **vizinha do mesmo assunto**, e o `recall@3` — a métrica que importa,
porque a ferramenta entrega três trechos — é 17/17. Trocar de ranqueador por
causa do recall@1 seria otimizar a métrica errada.

**Ajuste decorrente do que a suíte revelou:** `/api/session` passou a ler direto
do armazenamento, porque consultar histórico não deveria exigir LLM configurado.

---

## 8. Conjunto dourado com metade dos casos negativos

**Commit:** `f5ebfd9` · *harness de avaliação e gerador de conversas de exemplo*

**A pergunta.** Como saber que o agente está bom, em vez de achar?

**A escolha.** Um conjunto dourado em `eval/cases.json` que fixa comportamentos
observáveis e mede **três coisas que falham por motivos diferentes**: disciplina
de consulta (chamou ferramenta antes de afirmar?), roteamento (chamou a
ferramenta certa?) e conteúdo.

**Metade dos casos é negativa** — verifica o que a resposta **não** pode conter.
É aí que um agente de atendimento causa prejuízo real: oferecer promoção
vencida, somar desconto que não acumula, prometer parcelamento abaixo do mínimo,
revelar dado de pedido sem verificar identidade, inventar frete que depende de
cotação.

**As verificações casam por número e palavra-chave, nunca por frase inteira:** o
agente tem liberdade de redação, não de fato. (Esta regra nasceu de um erro meu
— ver a seção final.)

Casos cujo resultado depende do tempo carregam a própria data de referência, de
modo que o score não muda conforme o dia da execução. `eval --report` grava
[`EVALUATION.md`](EVALUATION.md) e sai com código 1 se algum caso reprovar,
servindo direto como gate de CI.

**`emporio demo`** roda um roteiro e grava a transcrição em Markdown com as
consultas de cada turno. É assim que os exemplos em `examples/` são produzidos:
**conversa real com o agente, não diálogo escrito à mão.**

---

## 9. README com a alternativa descartada em cada decisão

**Commit:** `d29a0ae` · *README com justificativa das decisões e arquitetura*

**A escolha.** Cobrir os três pontos do enunciado — execução, justificativa de
cada decisão técnica, e limitações com o que eu faria com mais tempo — sendo que
**cada decisão vem com a alternativa descartada e o motivo concreto nesta base**,
não argumento genérico.

Seção de suposições com as 14 ambiguidades que encontrei, incluindo os dois
pontos em que o **próprio manual se contradiz** (parcela mínima no §3 × §3.1, e
qual número é o WhatsApp) e os três em que o manual divergiu da base (catálogo
de 300 itens contra 65; categorias de sopro e cordas vendidas porém vazias).

**Limitações escritas sem amenizar:** o agente não transaciona, prazo em dias
úteis ignora feriado, a data de recebimento é a estimada e não a real, e a busca
só responde nas dimensões previstas.

`ARCHITECTURE.md` complementa com o fluxo de um turno, os contratos que toda
ferramenta respeita, e o princípio que guiou todas as fronteiras — o mesmo que
abre este documento.

---

## 10. Correções que só a API de verdade revelou

**Commit:** `a540889` · *thought_signature do Gemini 3.x e comando para listar modelos*

**A pergunta.** O que quebra quando o agente sai do teste e fala com a API real?

**Três coisas, e nenhuma aparecia nos testes:**

1. **Modelos Gemini 3.x exigem que o `thoughtSignature`** recebido junto da
   chamada de função seja devolvido com ela na requisição seguinte, ou a API
   responde HTTP 400. `ToolCall` ganhou um campo `metadata` neutro para carregar
   metadado opaco de provedor, e a persistência grava esse campo — **sem isso,
   uma sessão retomada seria rejeitada.** Que é justamente o caso de uso do
   WhatsApp.
2. **O modelo default dava 404.** `gemini-2.5-flash` responde "no longer
   available to new users" em chave nova, e os mais recentes vivem saturados
   (`3.8-flash` devolveu 503 "high demand" em três tentativas). Default passou
   para `gemini-3.5-flash`, escolhido **empiricamente** entre os que respondem
   com function calling.
3. **Novo comando `emporio models`**, que lista os modelos que a chave realmente
   acessa. Existe porque este é o erro mais chato de diagnosticar: o 404 sozinho
   não diz o que usar no lugar. A mensagem de erro agora aponta o comando.

**Detalhe de segurança no mesmo commit:** a UI passou a renderizar ênfase leve
em markdown, que o modelo produz naturalmente num canal de mensagem. A conversão
roda sempre **depois** do escape de HTML, para que nada vindo do modelo ou do
cliente injete marcação.

---

## 11. Separar caso reprovado de caso não executado

**Commit:** `5c5cd7b` · *fix(eval)*

**A pergunta.** O que o relatório deve dizer quando a cota do provedor estoura
no meio da avaliação?

**O problema concreto.** Na primeira execução real, ao esgotar a cota do nível
gratuito, **14 dos 20 casos falharam por HTTP 429 e o relatório marcou todos
como reprovados**, produzindo 6/20 (30%). Um relatório assim é indistinguível de
um agente quebrado: **mede a cota do dia, não o comportamento.**

**A escolha.** `CaseResult` passou a distinguir `executado` de `passou`, e o
score é calculado só sobre os casos que efetivamente rodaram. Casos não
executados aparecem em seção própria, identificados como falha de
infraestrutura. Ao detectar erro de cota a execução **para**, em vez de insistir:
cada caso seguinte gastaria três retentativas para produzir o mesmo erro. Nova
opção `--delay` espaça as requisições em nível gratuito com limite por minuto.

**O comando sai com código 1 apenas por falha de comportamento.** Cota esgotada
não deve derrubar pipeline de CI — não é regressão do código.

**Quando eu mudaria de ideia.** Com conta paga isso deixa de ser o caminho
comum, mas a distinção continua certa: medir o sistema e medir a infraestrutura
são duas perguntas diferentes.

---

## 12. Cadeia de modelos de reserva, nunca silenciosa

**Commit:** `bfb0a75` · *troca automática de modelo ao esgotar a cota do nível gratuito*

**A pergunta.** Como manter o projeto executável por quem avalia, sabendo que o
nível gratuito acaba?

**A medição que forçou a decisão.** O nível gratuito do Gemini concede ~20
requisições por dia **por modelo**. O agente faz 2 a 3 chamadas por turno — ou
seja, **uma conversa de oito mensagens esgota a cota de um modelo e o
atendimento morre no meio.** Para um projeto que precisa ser executável por quem
for avaliá-lo, isso é inaceitável.

**A escolha.** Como cada modelo tem cota própria, o cliente Gemini desce uma
cadeia de reserva ao receber 429 (cota), 404 (versão descontinuada) ou 503
(modelo saturado). **Credencial inválida não dispara troca:** 401 vale para
todos os modelos, e insistir só gastaria tempo.

**A troca nunca é silenciosa.** Aparece no terminal, no `doctor` e no rodapé das
transcrições de exemplo. **Esconder que a resposta veio de um modelo mais
simples seria desonesto com quem lê o exemplo.**

**Desligável com `EMPORIO_LLM_FALLBACK=0`**, que é exatamente o que se quer ao
medir: garante que uma única versão de modelo atendeu a conversa inteira.

**No mesmo commit, a regra 7 do prompt ficou mais apertada:** o agente não pode
prometer reservar ou separar produto, só oferecer o que de fato executa. Isso
apareceu numa transcrição real, onde ele ofereceu "posso deixar separado para
você?" — algo que o sistema não faz.

---

## 13. Oferecer modelo próximo quando o exato não existe

**Commit:** `9b30052` · *feat(catalog)*

**A pergunta.** O que a busca faz quando o cliente pede um modelo que não existe
no catálogo, mas existe um equivalente?

**O que expôs o problema.** A primeira execução completa do eval deu 16/20.
Nenhum dos quatro reprovados era erro do agente — mas o quarto escondia um
buraco real. Caso: *"Quanto custa a Fender Stratocaster American Professional?"*
O catálogo tem a **Fender Stratocaster American Pro II** (produto 145,
R$ 12.500,00), mas `buscar_produtos` exige que **todos** os termos casem, e o
termo "professional" derrubava a consulta inteira. Resultado vazio — e o agente
ficava sem o equivalente que o §7.3 manda oferecer.

**A escolha.** **Apenas quando o resultado vem vazio**, a busca pontua cada
produto disponível pelo número de termos presentes no texto buscável e devolve
os três melhores em `produtos_parecidos`.

**Por que restrito ao vazio.** O AND continua sendo o comportamento certo na
busca normal: "violão nylon" não deve trazer todo violão mais toda guitarra. A
aproximação fica onde não há nada a poluir.

**E marca ausente de verdade continua sem equivalente:** "Gretsch G2622
Streamliner" não casa nenhum termo e não inventa sugestão. Melhor não responder
do que aproximar errado.

**Prompt reforçado no mesmo ponto:** diante de produto inexistente, sem estoque
ou descontinuado, o §7.3 não manda só avisar — manda **oferecer alternativa**.
Então o agente traz a opção concreta com nome e preço na mesma mensagem, em vez
de devolver "quer que eu veja outras opções?" e empurrar o trabalho ao cliente.

---

## 14. Contrariar o linter num falso positivo verificado

**Commit:** `154455a` · *qualidade de código e armadilha do sqlite3.Row documentada*

**A pergunta.** O que fazer quando o linter aponta cinco ocorrências de um
problema que não existe?

**O caso.** O `ruff` levantou **SIM118** em cinco lugares, sugerindo trocar
`coluna in row.keys()` por `coluna in row`. Em `sqlite3.Row` isso é um **falso
positivo que quebraria o código em silêncio**: a iteração do tipo é sobre os
**valores**, não sobre as chaves, então a checagem passaria a devolver `False`
sempre — e o código seguiria rodando, só com todo campo opcional tratado como
ausente.

**A escolha.** **Verifiquei no interpretador antes de decidir**, e mantive o
`.keys()`. A exceção agora mora num helper único, `row_has`, com o motivo
documentado **em um lugar só**, em vez de cinco `noqa` espalhados que ninguém
revisita.

**Por que isso importa mais do que parece.** Um `noqa` sem explicação é um
convite para alguém "limpar" a regra no futuro e reintroduzir o bug. O helper
nomeado transforma a exceção em decisão legível.

**Duas regras ficaram no `ignore` do `pyproject`, com justificativa escrita:**
B008, porque `typer.Argument(...)` em default é o idioma da própria Typer; e
SIM905, porque a lista de stopwords é um bloco de texto por legibilidade.

**No mesmo commit:** removido código morto (`_row_to_dict`, sem nenhum uso) e
simplificada a verificação de identidade, **mantendo a exigência de dois
nomes** — primeiro nome sozinho é fácil demais de adivinhar, e a verificação
existe por causa do §9.

---

## 15. Transcrições geradas, não escritas à mão

**Commit:** `b593cca` · *cinco conversas reais com o agente e print da interface*

**A escolha.** As cinco transcrições em `examples/` são geradas por
`emporio demo` contra a API de verdade, com as consultas de ferramenta
capturadas turno a turno. **Não são diálogos escritos à mão** — o que significa
que elas podem me contradizer, e uma delas contradisse.

| # | Cenário | O que demonstra |
|---|---|---|
| 01 | Busca e orçamento | Todo valor confere com a base: R$ 699,90 de tabela, R$ 664,90 no PIX, 6x de R$ 116,65, frete grátis acima de R$ 500 |
| 02 | Devolução fora do prazo | O caso não trivial do enunciado: pede identidade, calcula a data-limite vencida (24/02/2026), **sustenta a recusa quando o cliente pressiona por exceção** e redireciona para a garantia do fabricante quando ele muda o motivo para defeito |
| 03 | Três recusas e um sim | Categoria cadastrada porém vazia, acessório fora de escopo, assunto fora da loja — e uma pergunta musical que o agente responde de verdade. Não inventa loja parceira |
| 04 | Domingo, loja fechada | Informa o retorno, identifica pelo telefone, e no último turno **a verificação do §9 recusa acesso a um pedido que não era da cliente** |
| 05 | Promoção vencida | Black Friday inativa recusada com transparência, os quatro descontos ativos com preço exato, o PIX que não acumula, e uma tentativa de injeção de prompt recusada sem perder o tom |

**O turno que eu não planejei.** No exemplo 04 eu supus que o pedido 9 fosse da
mesma cliente. A proteção de dados mostrou que não era e recusou o acesso. **O
cenário do exemplo foi reescrito para descrever o que de fato acontece**, em vez
de eu ajustar o roteiro para esconder que minha suposição estava errada.

O rodapé de cada transcrição registra quando um modelo de reserva assumiu no
meio da conversa, por esgotamento da cota gratuita.

---

## 16. Não insistir com backoff em erro que é do modelo

**Commit:** `2fca292` · *fix(llm)*

**A pergunta.** Vale retentar quando o provedor devolve 429 ou 503?

**O sintoma.** A interface web ficava **mais de um minuto** com o indicador de
digitação ligado quando a cota esgotava.

**A causa.** O retry fazia três tentativas com backoff contra o **mesmo modelo**
em 429 e 503, antes de a troca de modelo acontecer.

**A escolha.** Não retentar. Cota e sobrecarga valem para o modelo inteiro, então
retentar é desperdício garantido — quem resolve é a troca de modelo (entrada
12). O retry passou a cobrir só o que é transitório de verdade: falha de rede. O
erro do modelo sobe na hora para a cadeia de reserva decidir.

**Medido.** Tempo até uma resposta útil com a cota esgotada: **de mais de um
minuto para 11 segundos.**

**Quando eu mudaria de ideia.** Com conta paga, 429 passa a ser raro e
genuinamente transitório — aí o retry volta a fazer sentido para esse código.

---

## 17. Caminho de erro da CLI legível

**Commit:** `b7b5c51` · *fix(cli)*

**A pergunta.** O que acontece quando alguém digita um argumento errado no
`emporio tool`?

**O problema.** O comando chamava o handler da ferramenta direto e deixava
escapar o `TypeError` de argumento obrigatório ausente, **imprimindo um
traceback Python** na cara de quem digitou. E o próprio `doctor` sugere esse
comando como forma de inspecionar a camada determinística sem gastar chamada de
modelo — ou seja, errar um nome de argumento era um caminho **provável**.

**A escolha.** Conferir o schema da ferramenta antes de chamar o handler: dizer
o que falta e listar o que aquela ferramenta aceita, saindo com código 1.
Argumento sem efeito também passou a ser avisado, porque era descartado em
silêncio — `calcular_preco parcelas=10` parecia aceito, não era, e a resposta
vinha igual.

**Escopo.** Mudança restrita ao caminho de erro da CLI. O laço do agente já
tratava argumento ausente pelo seu próprio caminho, coberto por teste (entrada
5, defesa 1).

---

## 18. Site online com a parte que nunca quebra em primeiro plano

**Commits:** `5b91239`, `3b9b7e9`

**A pergunta.** Como deixar o projeto acessível sem instalação, sabendo que a
cota gratuita do provedor não sustenta um link público?

**A conta que decidiu o desenho.** Seis modelos na cadeia de reserva, ~20
requisições por dia cada, 2 a 3 chamadas por turno: **40 a 60 turnos diários
para todos os visitantes somados**. Uma conversa de avaliação tem 5 a 8 turnos,
então o chat sozinho aguentaria de 6 a 12 conversas por dia. Um link que fica
morto a maior parte do tempo é pior do que não ter link.

**A escolha.** Duas superfícies, e a que não depende de LLM é a que sustenta a
demonstração:

- `POST /api/tool/{nome}` executa qualquer uma das 11 ferramentas sem modelo,
  espelhando o `emporio tool` da CLI. Sem chave, sem cota, de pé sempre.
- `/ferramentas` exercita esse endpoint com um roteiro clicável dos casos que
  mais importam, cada um explicando a decisão que demonstra.

**O que isso não afrouxa.** Ferramenta que lê dado pessoal continua exigindo
confirmação de identidade, porque a regra vive dentro da ferramenta e não na
camada de transporte. Há teste de tentativa de vazamento pelo HTTP para fixar
isso, além do que já existia na ferramenta.

**Duas condições que o uso local não tem, e como foram resolvidas.** Em
serverless o disco do pacote é somente-leitura fora de `/tmp`, e cada instância
fria começa sem os derivados do ETL. `var_dir` e `logs_dir` passaram a vir do
ambiente — default inalterado, então nada muda localmente — e `api/index.py`
roda o ETL mais a indexação na primeira requisição da instância, em 1,3s
medidos. O relatório de qualidade não é gerado em produção (`write_report=False`)
porque é artefato versionado e `docs/` não aceita escrita.

**O erro que o primeiro deploy revelou.** Eu havia posto um rewrite de `/(.*)`
para `/api/index.py` no `vercel.json`. Isso reescreve o próprio path, então o
ASGI recebia `/api/index.py` literal e o FastAPI respondia 404 em **toda** rota,
inclusive na raiz. O preset `fastapi` do Vercel já roteia todo o tráfego para o
app: o rewrite era redundante e atropelava esse roteamento. Removido em
`3b9b7e9`.

**Limitação registrada no próprio módulo.** `/tmp` é por instância e efêmero, de
modo que o histórico de conversa não é compartilhado entre instâncias nem
sobrevive à reciclagem. Para demonstração é aceitável; em produção o store de
conversa iria para banco gerenciado.

**Quando eu mudaria de ideia.** Com chave paga o chat deixa de ser o elo frágil
e passa a ser a vitrine principal — e aí o histórico compartilhado deixa de ser
detalhe e vira requisito.

---

## As vezes em que eu estava errado

Esta seção existe porque é a parte mais útil do projeto para mim. Em todos os
casos abaixo, a verificação derrubou **minha** afirmação, não a do agente — e em
nenhum deles eu ajustei o sistema para o meu erro passar.

1. **A premissa falsa da Fender.** Eu escrevi um caso de eval afirmando que
   Fender não estava no catálogo, e reprovando o agente por citar um preço. A
   consulta mostrou que a **Fender Stratocaster American Pro II existe**
   (produto 145, R$ 12.500,00) e que o valor citado estava **correto**. Eu havia
   afirmado sem verificar. O caso foi reescrito, e o episódio virou a melhoria
   da entrada 13.

2. **Três dos meus próprios testes estavam errados.** O caso do horário exigia
   chamada de ferramenta para algo que o prompt do sistema já injeta a cada
   turno — a resposta estava certa e o teste é que estava errado. Dois outros
   casavam frase literal demais: o agente escreveu "a gente está sem ele em
   estoque" e "não temos nenhuma promoção de Black Friday ativa", que dizem
   exatamente o que se pedia, mas não batiam com as cadeias que eu havia
   listado. Daí a regra de casar por número e palavra-chave, nunca por frase.

3. **O pedido que eu supus ser da mesma cliente.** No exemplo 04, a verificação
   de identidade do §9 recusou o acesso e mostrou que minha suposição estava
   errada. Reescrevi o cenário para descrever o comportamento real.

4. **Meu limiar de recall@1 estava acima do real.** Em vez de ajustar o
   ranqueador para passar no teste, documentei o número verdadeiro e o motivo —
   os casos que não lideram erram para uma seção vizinha do mesmo assunto, e a
   métrica que importa é o recall@3.

5. **"Violão nylon" trazendo um ukulele.** Minha expectativa era de que a busca
   retornasse só violões. Um ukulele barítono de cordas de nylon casa
   legitimamente. O teste estava certo; minha expectativa, não.

**O que isso diz sobre o projeto:** o harness não serve para produzir um número
bonito. Serve para descobrir que três testes meus estavam errados, que uma
suposição minha não tinha sido verificada, e que a busca tinha um buraco real.
**Esse ciclo é o motivo de ter avaliação — não o score final.**

---

## Estado verificado da entrega

Medido num clone limpo deste repositório, em ambiente virtual novo:

| Verificação | Resultado |
|---|---|
| `pytest` | **315 testes passando**, ~5s, sem rede e sem chave de API |
| Cobertura | **81%** |
| `ruff check .` | limpo |
| `emporio build` do zero | **0,96s** — 8 ocorrências de qualidade, 26 trechos indexados |
| `emporio doctor` sem chave | Aponta o que falta e o que ainda funciona |
| `emporio eval --report` | **21/21 (100%)** em 02/11/2026, os oito eixos em 100%, nenhum caso pulado |
| Produção | `/api/health` responde 200 com banco e índice construídos na instância fria; `/api/tools` lista as 11; `/ferramentas` e `/` servidas; `POST /api/tool/{nome}` registrado no OpenAPI |

O relatório em [`EVALUATION.md`](EVALUATION.md) registra a execução completa:
**21 casos de 21, todos aprovados**, com os oito eixos em 100%. Chegar nele
dependeu da cadeia de seis modelos de reserva da entrada 12 — a cota de um
único modelo do nível gratuito não cobre uma execução inteira. E a leitura só é
confiável por causa da entrada 11: o score é calculado sobre os casos que
efetivamente rodaram, de modo que um relatório parcial se declara parcial em vez
de se passar por reprovação.
