"""Entrada serverless (Vercel).

Vive fora de `src/emporio` de propósito: é preocupação de implantação, não da
biblioteca. A hospedagem serverless impõe duas condições que o uso local não
tem, e as duas são resolvidas aqui antes de o app ser exposto.

1. **O disco do pacote é somente-leitura fora de `/tmp`.** O banco, o índice de
   políticas e o histórico de conversa são derivados e precisam de escrita, daí
   `EMPORIO_VAR_DIR` apontar para `/tmp`. As variáveis são definidas antes de
   importar `emporio.config`, porque a configuração é resolvida do ambiente no
   momento do import — por isso os imports abaixo não estão no topo.

2. **Cada instância fria começa sem os derivados do ETL.** O build roda na
   primeira requisição da instância (~1s) e é pulado nas seguintes. O relatório
   de qualidade não é gerado aqui (`write_report=False`): ele é artefato
   versionado do repositório e `docs/` não aceita escrita em produção.

Consequência conhecida: `/tmp` é por instância e efêmero, então o histórico de
conversa não é compartilhado entre instâncias nem sobrevive à reciclagem. Para
uma demonstração é aceitável; o caminho de produção seria o store de conversa
em banco gerenciado.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

os.environ.setdefault("EMPORIO_VAR_DIR", "/tmp/emporio/var")
os.environ.setdefault("EMPORIO_LOGS_DIR", "/tmp/emporio/logs")

from emporio.api import api as app  # noqa: E402
from emporio.config import settings  # noqa: E402


def _preparar() -> None:
    """Garante banco e índice nesta instância. Idempotente e barato."""
    settings.ensure_dirs()

    if not settings.db_path.exists():
        from emporio.data.loader import build_database

        build_database(write_report=False)

    if not settings.policy_index_path.exists():
        from emporio.retrieval.index import PolicyIndex

        PolicyIndex.build().save()


_preparar()

__all__ = ["app"]
