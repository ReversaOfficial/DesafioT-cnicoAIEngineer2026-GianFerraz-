"""Catálogo de ferramentas expostas ao modelo.

As descrições e os schemas aqui **são parte do prompt**. Um parâmetro mal
descrito custa mais acurácia do que qualquer ajuste de temperatura, então cada
descrição diz não só o que a ferramenta faz, mas *quando* usá-la — e, quando
relevante, quando não usar.

O schema é JSON Schema puro. Cada adaptador de provedor traduz para o formato
que o seu endpoint espera (Gemini, OpenAI e Anthropic divergem nos detalhes),
mantendo um único ponto de verdade.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from emporio.tools.catalog import (
    buscar_produtos,
    detalhar_produto,
    listar_categorias,
    promocoes_ativas,
)
from emporio.tools.orders import (
    consultar_pedido,
    identificar_cliente,
    verificar_elegibilidade_devolucao,
)
from emporio.tools.policies import consultar_politicas
from emporio.tools.pricing import calcular_preco
from emporio.tools.shipping import calcular_frete
from emporio.tools.store import informacoes_da_loja


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict]
    # Ferramentas que leem dado pessoal são marcadas para o log de auditoria.
    sensitive: bool = field(default=False)

    def json_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="buscar_produtos",
        description=(
            "Busca instrumentos no catálogo da loja. Use SEMPRE que o cliente "
            "pedir recomendação, perguntar o que existe de um tipo, ou pedir "
            "opções dentro de um orçamento. Por padrão retorna apenas itens "
            "disponíveis em estoque. Quando não há resultado, o retorno explica "
            "o motivo (categoria vazia, item sem estoque, preço fora da faixa) "
            "— use essa explicação na resposta ao cliente."
        ),
        parameters=_schema(
            {
                "termo": {
                    "type": "string",
                    "description": (
                        "Texto livre: modelo, marca, tipo ou característica. "
                        "Ex.: 'violão nylon', 'Yamaha', 'tampo sólido'. "
                        "Todos os termos informados precisam casar."
                    ),
                },
                "categoria": {
                    "type": "string",
                    "description": (
                        "Nome ou id da categoria. Ex.: 'Violões', 'Guitarras', "
                        "'Ukuleles', 'Baixos', 'Teclados e Pianos', "
                        "'Baterias e Percussão'."
                    ),
                },
                "preco_max": {
                    "type": "number",
                    "description": "Teto de preço em reais. Ex.: 1000 para 'até R$ 1.000'.",
                },
                "preco_min": {"type": "number", "description": "Piso de preço em reais."},
                "marca": {"type": "string", "description": "Filtra por marca exata."},
                "apenas_disponiveis": {
                    "type": "boolean",
                    "description": (
                        "Default true. Só mude para false se o cliente perguntar "
                        "especificamente por um item que pode estar esgotado."
                    ),
                },
                "limite": {
                    "type": "integer",
                    "description": "Quantos produtos retornar (1 a 10, default 5).",
                },
            }
        ),
        handler=buscar_produtos,
    ),
    ToolSpec(
        name="detalhar_produto",
        description=(
            "Ficha completa de um instrumento: descrição, especificações, "
            "preço, promoção ativa e disponibilidade. Use quando o cliente "
            "perguntar sobre um modelo específico pelo nome, ou depois de uma "
            "busca, para detalhar um item. Se o nome for ambíguo, o retorno traz "
            "os candidatos — pergunte ao cliente qual é, não escolha por ele."
        ),
        parameters=_schema(
            {
                "product_id": {"type": "integer", "description": "Id do produto, se conhecido."},
                "nome": {
                    "type": "string",
                    "description": "Nome ou parte do nome. Ex.: 'Takamine GD20'.",
                },
            }
        ),
        handler=detalhar_produto,
    ),
    ToolSpec(
        name="listar_categorias",
        description=(
            "Lista as categorias do catálogo com quantidade de itens e faixa de "
            "preço. Use quando o cliente perguntar o que a loja vende, ou quando "
            "precisar saber se a loja atende um tipo de instrumento. Importante: "
            "algumas categorias constam do cadastro mas estão sem nenhum item — "
            "o retorno sinaliza quais."
        ),
        parameters=_schema({}),
        handler=listar_categorias,
    ),
    ToolSpec(
        name="promocoes_ativas",
        description=(
            "Promoções vigentes no momento, com preço original, percentual e "
            "preço final. Use quando o cliente perguntar sobre ofertas, "
            "descontos ou promoções. Só retorna promoção ativa — se o cliente "
            "citar uma campanha que não aparece aqui, ela não está valendo."
        ),
        parameters=_schema(
            {"limite": {"type": "integer", "description": "Quantas retornar (default 10)."}}
        ),
        handler=promocoes_ativas,
    ),
    ToolSpec(
        name="calcular_preco",
        description=(
            "Composição de preço de um produto: preço de tabela, promoção "
            "ativa, valor no PIX e todas as opções de parcelamento viáveis. Use "
            "SEMPRE que o cliente perguntar preço, 'quanto fica', 'dá pra "
            "parcelar', 'quanto sai no PIX' ou pedir condição de pagamento. "
            "Nunca calcule desconto ou parcela de cabeça — as regras de parcela "
            "mínima e de não-cumulatividade do PIX com promoção já estão "
            "aplicadas no retorno."
        ),
        parameters=_schema(
            {
                "product_id": {"type": "integer", "description": "Id do produto."},
                "quantidade": {"type": "integer", "description": "Default 1."},
                "forma_pagamento": {
                    "type": "string",
                    "description": (
                        "Opcional. Destaca a forma pedida pelo cliente: 'pix', "
                        "'boleto', 'debito', 'credito', '6x', '12x'."
                    ),
                },
            },
            required=["product_id"],
        ),
        handler=calcular_preco,
    ),
    ToolSpec(
        name="calcular_frete",
        description=(
            "Frete e prazo de entrega para uma cidade. Use quando o cliente "
            "perguntar sobre entrega, frete, prazo ou 'quanto tempo chega'. Na "
            "região metropolitana de Campo Grande a regra é fechada (grátis "
            "acima de R$ 500). Fora dela o valor depende de cotação por CEP e "
            "peso — nesse caso informe modalidades e prazos, nunca um valor."
        ),
        parameters=_schema(
            {
                "cidade": {"type": "string", "description": "Cidade de entrega."},
                "valor_do_pedido": {
                    "type": "number",
                    "description": (
                        "Valor total em reais. Necessário na região "
                        "metropolitana para saber se o frete é grátis."
                    ),
                },
                "product_id": {
                    "type": "integer",
                    "description": (
                        "Opcional. Permite avisar sobre frete especial de "
                        "instrumento de grande porte."
                    ),
                },
            },
            required=["cidade"],
        ),
        handler=calcular_frete,
    ),
    ToolSpec(
        name="consultar_pedido",
        description=(
            "Situação de um pedido: status, itens, valor, previsão de entrega e "
            "código de rastreio. Use quando o cliente perguntar sobre um pedido "
            "dele. ATENÇÃO: por proteção de dados, a ferramenta exige "
            "confirmação de identidade. Se o cliente não informou nome ou "
            "telefone, chame sem `confirmacao`, receba o pedido de verificação "
            "e peça o dado a ele antes de revelar qualquer informação."
        ),
        parameters=_schema(
            {
                "order_id": {"type": "integer", "description": "Número do pedido."},
                "confirmacao": {
                    "type": "string",
                    "description": (
                        "Nome completo ou telefone do titular, para conferência. "
                        "Passe exatamente o que o cliente informou."
                    ),
                },
            },
            required=["order_id"],
        ),
        handler=consultar_pedido,
        sensitive=True,
    ),
    ToolSpec(
        name="identificar_cliente",
        description=(
            "Localiza um cliente por telefone ou nome e lista os pedidos dele. "
            "Use no começo do atendimento, se o cliente se identificar, para "
            "poder chamá-lo pelo nome; ou quando ele quiser falar de um pedido "
            "mas não tiver o número em mãos."
        ),
        parameters=_schema(
            {
                "telefone": {
                    "type": "string",
                    "description": "Telefone em qualquer formato. Ex.: '(67) 99812-3456'.",
                },
                "nome": {"type": "string", "description": "Nome completo do cliente."},
            }
        ),
        handler=identificar_cliente,
        sensitive=True,
    ),
    ToolSpec(
        name="verificar_elegibilidade_devolucao",
        description=(
            "Avalia se um pedido pode ser trocado ou devolvido, fazendo a "
            "contagem de prazo contra as regras do manual. Use SEMPRE que o "
            "cliente falar de devolver, trocar, cancelar, se arrepender da "
            "compra ou reclamar de defeito. Nunca responda sobre prazo de "
            "devolução de cabeça: o retorno traz o veredito, a data-limite e os "
            "próximos passos já calculados. Também exige confirmação de "
            "identidade."
        ),
        parameters=_schema(
            {
                "order_id": {"type": "integer", "description": "Número do pedido."},
                "motivo": {
                    "type": "string",
                    "enum": ["arrependimento", "defeito", "preferencia"],
                    "description": (
                        "'arrependimento' quando desistiu da compra; 'defeito' "
                        "quando o produto veio com problema de fabricação; "
                        "'preferencia' quando quer trocar por outra cor, modelo "
                        "ou tamanho."
                    ),
                },
                "confirmacao": {
                    "type": "string",
                    "description": "Nome completo ou telefone do titular do pedido.",
                },
            },
            required=["order_id"],
        ),
        handler=verificar_elegibilidade_devolucao,
        sensitive=True,
    ),
    ToolSpec(
        name="consultar_politicas",
        description=(
            "Busca no manual interno de políticas e procedimentos. Use para "
            "qualquer pergunta sobre REGRA da loja: formas de pagamento, trocas "
            "e devoluções, frete, garantia, promoções, privacidade/LGPD. O "
            "retorno traz os trechos com a seção de origem — responda com base "
            "neles e cite a seção quando houver prazo, valor ou condição."
        ),
        parameters=_schema(
            {
                "pergunta": {
                    "type": "string",
                    "description": (
                        "A dúvida, em linguagem natural. Ex.: 'posso parcelar "
                        "em quantas vezes', 'o que a garantia não cobre'."
                    ),
                },
                "limite": {
                    "type": "integer",
                    "description": "Quantos trechos retornar (1 a 5, default 3).",
                },
            },
            required=["pergunta"],
        ),
        handler=consultar_politicas,
    ),
    ToolSpec(
        name="informacoes_da_loja",
        description=(
            "Dados da loja: endereço, telefone, WhatsApp, e-mail, CNPJ, horário "
            "de funcionamento e se está aberta AGORA. Use para perguntas de "
            "localização, contato e horário. O campo `agora` já diz se a loja "
            "está aberta e, se estiver fechada, quando reabre — informe isso ao "
            "cliente fora do expediente."
        ),
        parameters=_schema(
            {
                "incluir_horarios": {
                    "type": "boolean",
                    "description": "Default true.",
                }
            }
        ),
        handler=informacoes_da_loja,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[dict]:
    return [tool.json_schema() for tool in TOOLS]


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS_BY_NAME.get(name)
