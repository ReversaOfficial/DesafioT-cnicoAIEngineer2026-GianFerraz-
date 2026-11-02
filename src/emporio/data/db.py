"""Acesso ao banco SQLite da operação."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from emporio.config import settings


def connect(path: Path | None = None, *, read_only: bool = True) -> sqlite3.Connection:
    """Abre conexão com `row_factory` em dict-like.

    Em modo somente-leitura usamos a URI `mode=ro`: as ferramentas do agente
    nunca devem escrever na base da operação, e deixar isso explícito no driver
    é mais confiável do que confiar na disciplina de cada função.
    """
    db_path = path or settings.db_path
    if read_only:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def read_cursor(path: Path | None = None) -> Iterator[sqlite3.Cursor]:
    conn = connect(path, read_only=True)
    try:
        yield conn.cursor()
    finally:
        conn.close()


def database_exists(path: Path | None = None) -> bool:
    db_path = path or settings.db_path
    return db_path.exists() and db_path.stat().st_size > 0


def ensure_database(path: Path | None = None) -> Path:
    """Garante que o banco existe, rodando o ETL na primeira execução.

    Deixa a experiência de primeiro uso em um comando só (`emporio chat`) sem
    exigir que a pessoa lembre de rodar o build antes.
    """
    db_path = path or settings.db_path
    if not database_exists(db_path):
        from emporio.data.loader import build_database

        build_database(db_path=db_path)
    return db_path
