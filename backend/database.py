from __future__ import annotations

import re
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


def database_url() -> str:
    configured = os.environ.get("DATABASE_URL")
    if configured:
        if configured.startswith("postgres://"):
            return configured.replace("postgres://", "postgresql+psycopg://", 1)
        if configured.startswith("postgresql://"):
            return configured.replace("postgresql://", "postgresql+psycopg://", 1)
        return configured
    path = Path(os.environ.get("PAPERS_DB", Path(__file__).parent / "data" / "papers.sqlite3"))
    return f"sqlite:///{path.resolve()}"


def create_database_engine(url: str | None = None) -> Engine:
    resolved = url or database_url()
    options: dict[str, Any] = {"pool_pre_ping": True}
    if resolved.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(resolved, **options)


class CompatibleRow:
    def __init__(self, row: Any) -> None:
        self._values = tuple(row)
        self._mapping = row._mapping

    def __getitem__(self, key: str | int) -> Any:
        return self._values[key] if isinstance(key, int) else self._mapping[key]


class CompatibleResult:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.rowcount = result.rowcount

    def fetchone(self) -> CompatibleRow | None:
        row = self._result.fetchone()
        return CompatibleRow(row) if row is not None else None

    def fetchall(self) -> list[CompatibleRow]:
        return [CompatibleRow(row) for row in self._result.fetchall()]

    def __iter__(self) -> Iterator[CompatibleRow]:
        return (CompatibleRow(row) for row in self._result)


class CompatibleConnection:
    _positional = re.compile(r"\?")

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    @classmethod
    def _bind(cls, sql: str, parameters: Sequence[Any] | dict[str, Any]) -> tuple[Any, Any]:
        sql = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            sql,
            flags=re.IGNORECASE,
        )
        if re.match(r"\s*INSERT\s+INTO\b", sql, re.IGNORECASE) and "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if isinstance(parameters, dict):
            return text(sql), parameters
        bound: dict[str, Any] = {}

        def replace(_match: re.Match[str]) -> str:
            key = f"p{len(bound)}"
            bound[key] = parameters[len(bound)]
            return f":{key}"

        statement = cls._positional.sub(replace, sql)
        if len(bound) != len(parameters):
            raise ValueError("SQL placeholder count does not match the parameters supplied.")
        return text(statement), bound

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> CompatibleResult:
        statement, bound = self._bind(sql, parameters)
        return CompatibleResult(self._connection.execute(statement, bound))

    def executemany(self, sql: str, parameters: Sequence[Sequence[Any]]) -> CompatibleResult:
        if not parameters:
            return CompatibleResult(self._connection.execute(text("SELECT 1 WHERE 1 = 0")))
        statement, _ = self._bind(sql, parameters[0])
        keys = [f"p{index}" for index in range(len(parameters[0]))]
        batch = [dict(zip(keys, row, strict=True)) for row in parameters]
        return CompatibleResult(self._connection.execute(statement, batch))


@contextmanager
def database_connection(engine: Engine) -> Iterator[CompatibleConnection]:
    with engine.begin() as connection:
        yield CompatibleConnection(connection)
