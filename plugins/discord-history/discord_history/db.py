"""Small psycopg connection and transactional migration helpers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

# psycopg is imported lazily inside connect() so the plugin package stays
# importable on hosts without the driver installed.

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def connect(dsn: str, **kwargs: Any) -> "psycopg.Connection[Any]":
    if not dsn or not dsn.strip():
        raise ValueError("database DSN is required")
    import psycopg

    return psycopg.connect(dsn, autocommit=False, **kwargs)


@contextmanager
def connection(dsn: str, *, connector: Callable[..., Any] = connect, **kwargs: Any) -> Iterator[Any]:
    conn = connector(dsn, **kwargs)
    try:
        yield conn
    finally:
        conn.close()


def apply_migrations(conn: Any, migration_dir: Path = MIGRATIONS) -> list[str]:
    """Apply each pending SQL migration in its own marker-coupled transaction."""
    applied: list[str] = []
    files = sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql"))
    try:
        # Bootstrap the marker table before deciding what is pending. This is
        # deliberately separate from versioned migrations so a second run can
        # report an empty applied set instead of replaying every idempotent DDL.
        with conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS discord_archive")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS discord_archive.schema_migrations ("
                "version integer PRIMARY KEY,name text NOT NULL,"
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
        versions: dict[int, Path] = {}
        for path in files:
            version = int(path.name.split("_", 1)[0])
            if version in versions:
                raise ValueError(f"duplicate migration version: {version}")
            versions[version] = path
        for version, path in sorted(versions.items()):
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"discord_history_migration:{version}",),
                )
                existing = conn.execute(
                    "SELECT 1 FROM discord_archive.schema_migrations WHERE version=%s",
                    (version,),
                ).fetchone()
                if existing:
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO discord_archive.schema_migrations(version,name) "
                    "VALUES(%s,%s) ON CONFLICT(version) DO NOTHING",
                    (version, path.stem),
                )
                applied.append(path.name)
    except Exception:
        conn.rollback()
        raise
    return applied
