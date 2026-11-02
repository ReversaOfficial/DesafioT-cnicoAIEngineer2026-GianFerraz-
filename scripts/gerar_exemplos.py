"""Regenera as conversas de exemplo de `examples/`.

Cada exemplo é uma conversa **real** com o agente, gravada com as consultas que
ele fez em cada turno. A data de referência de cada roteiro é fixada aqui para
que a transcrição seja reproduzível — sem isso, o exemplo de devolução mudaria
de veredito conforme o dia em que fosse rodado.

Uso:
    python scripts/gerar_exemplos.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (roteiro, saída, data de referência, título, contexto)
EXEMPLOS: list[tuple[str, str, str, str, str]] = [
    (
        "01-busca-e-orcamento.txt",
        "01-busca-e-orcamento.md",
        "2026-03-20T14:30",
        "Primeiro violão: da busca ao orçamento fechado",
        "Cliente iniciante, sem referência de marca, com orçamento de R$ 800. "
        "Mostra o caminho completo do atendimento comercial: busca no catálogo "
        "com teto de preço, detalhe do produto, composição de preço no PIX e no "
        "cartão, e frete na região metropolitana. Todo valor citado vem de "
        "consulta ao sistema — nenhum é calculado pelo modelo.",
    ),
    (
        "02-devolucao-fora-do-prazo.txt",
        "02-devolucao-fora-do-prazo.md",
        "2026-11-02T10:15",
        "Devolução fora do prazo (caso não trivial)",
        "O cenário sugerido no enunciado, no seu pior formato: o cliente quer "
        "devolver por arrependimento, mas o prazo de 7 dias corridos venceu há "
        "meses. Exige três coisas que o modelo não deve fazer sozinho — "
        "verificar a identidade antes de falar do pedido (§9), contar o prazo a "
        "partir da data de entrega (§4.1) e oferecer o caminho que ainda existe "
        "(§4.2 e §8). O agente também sustenta a recusa quando o cliente "
        "pressiona por exceção.",
    ),
    (
        "03-escopo-e-honestidade.txt",
        "03-escopo-e-honestidade.md",
        "2026-03-20T11:00",
        "Os três jeitos de dizer não",
        "Três recusas de naturezas diferentes, seguidas de um sim. Sopro e "
        "cordas orquestrais são categorias cadastradas mas **sem nenhum "
        "produto** — dizer 'vou consultar o estoque' seria falso. Cordas e "
        "palhetas são acessórios, que a loja não comercializa (§1). Imposto de "
        "renda está fora do escopo. Já a última pergunta é musical e adjacente, "
        "e aí o agente ajuda de verdade.",
    ),
    (
        "04-pedido-fora-do-horario.txt",
        "04-pedido-fora-do-horario.md",
        "2026-03-22T15:40",
        "Domingo à tarde, loja fechada, e a LGPD barrando acesso cruzado",
        "Atendimento fora do expediente num domingo. O §7.1 manda informar "
        "quando a equipe retorna, mas o agente segue resolvendo o que "
        "consegue. Identifica a cliente pelo telefone para tratá-la pelo nome "
        "(§7.2) e consulta o pedido dela. No último turno a cliente pede um "
        "pedido que **não é dela** — e a verificação de identidade do §9 "
        "recusa, sem vazar nada do titular real. Esse turno não estava "
        "planejado no roteiro: eu supus que o pedido 9 fosse da mesma cliente, "
        "e a proteção de dados mostrou que não era.",
    ),
    (
        "05-promocao-vencida.txt",
        "05-promocao-vencida.md",
        "2026-03-20T16:00",
        "Promoção vencida, PIX que não acumula e injeção de prompt",
        "O cliente cobra uma Black Friday de 30% que está cadastrada no sistema "
        "mas **inativa**: o §7.3 proíbe prometer desconto fora de vigência. "
        "Depois tenta somar os 5% do PIX sobre um preço já promocional, o que o "
        "§6.2 veda. Por último, tenta sobrescrever as instruções do agente — "
        "que recusa sem perder o tom da conversa.",
    ),
]


def main() -> int:
    scripts_dir = ROOT / "examples" / "scripts"
    out_dir = ROOT / "examples"
    falhas = 0

    for roteiro, saida, data, titulo, contexto in EXEMPLOS:
        print(f"\n=== {saida} (data de referência {data}) ===")
        comando = [
            sys.executable,
            "-m",
            "emporio.cli",
            "demo",
            "--script",
            str(scripts_dir / roteiro),
            "--out",
            str(out_dir / saida),
            "--date",
            data,
            "--title",
            titulo,
            "--context",
            contexto,
        ]
        resultado = subprocess.run(comando, cwd=ROOT, check=False)
        if resultado.returncode != 0:
            falhas += 1
            print(f"  !! falhou: {saida}")

    print(f"\n{len(EXEMPLOS) - falhas}/{len(EXEMPLOS)} exemplos gerados.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
