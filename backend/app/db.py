from collections.abc import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)

REVIEWROW_COLUMNS = [
    "id",
    "review_session_id",
    "statement_transaction_id",
    "receipt_document_id",
    "match_decision_id",
    "status",
    "attention_required",
    "attention_note",
    "source_json",
    "suggested_json",
    "confirmed_json",
    "created_at",
    "updated_at",
]


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_table_info(conn, table_name: str):
    return conn.execute(text(f"PRAGMA table_info({_quote_sqlite_identifier(table_name)})")).fetchall()


def _sqlite_table_exists(conn, table_name: str) -> bool:
    return bool(_sqlite_table_info(conn, table_name))


def _drop_reviewrow_indexes_for_table(conn, table_name: str) -> None:
    index_rows = conn.execute(
        text(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = :table_name
              AND name LIKE 'ix_reviewrow_%'
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    for (index_name,) in index_rows:
        conn.execute(text(f"DROP INDEX IF EXISTS {_quote_sqlite_identifier(index_name)}"))


def _reviewrow_count(conn, table_name: str) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table_name)}")).scalar_one()


def _copy_reviewrow_rows(conn, source_table: str) -> None:
    column_list = ", ".join(REVIEWROW_COLUMNS)
    conn.execute(
        text(
            f"INSERT INTO reviewrow ({column_list}) "
            f"SELECT {column_list} FROM {_quote_sqlite_identifier(source_table)}"
        )
    )


def _repair_interrupted_reviewrow_migration(conn) -> None:
    if not _sqlite_table_exists(conn, "reviewrow_old"):
        return

    _drop_reviewrow_indexes_for_table(conn, "reviewrow_old")
    if not _sqlite_table_exists(conn, "reviewrow"):
        return

    old_count = _reviewrow_count(conn, "reviewrow_old")
    current_count = _reviewrow_count(conn, "reviewrow")
    if old_count and current_count == 0:
        _copy_reviewrow_rows(conn, "reviewrow_old")

    if old_count == 0 or current_count == 0:
        conn.execute(text("DROP TABLE reviewrow_old"))


def _migrate_reviewrow_nullable_for_sqlite() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    table = SQLModel.metadata.tables.get("reviewrow")
    if table is None:
        return
    with engine.begin() as conn:
        _repair_interrupted_reviewrow_migration(conn)
        table_info = _sqlite_table_info(conn, "reviewrow")
        if not table_info:
            return
        notnull_by_name = {row[1]: row[3] for row in table_info}
        if not notnull_by_name.get("receipt_document_id") and not notnull_by_name.get("match_decision_id"):
            return

        conn.execute(text("PRAGMA foreign_keys=OFF"))
        old_table_name = "reviewrow_old"
        if _sqlite_table_exists(conn, old_table_name):
            old_table_name = "reviewrow_migration_old"
            conn.execute(text(f"DROP TABLE IF EXISTS {_quote_sqlite_identifier(old_table_name)}"))
        conn.execute(text(f"ALTER TABLE reviewrow RENAME TO {_quote_sqlite_identifier(old_table_name)}"))
        _drop_reviewrow_indexes_for_table(conn, old_table_name)
        table.create(conn)
        _copy_reviewrow_rows(conn, old_table_name)
        conn.execute(text(f"DROP TABLE {_quote_sqlite_identifier(old_table_name)}"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def _ensure_reviewrow_indexes_for_sqlite() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    table = SQLModel.metadata.tables.get("reviewrow")
    if table is None:
        return
    with engine.begin() as conn:
        if not _sqlite_table_exists(conn, "reviewrow"):
            return
        _repair_interrupted_reviewrow_migration(conn)
        for index in table.indexes:
            index.create(conn, checkfirst=True)


def create_db_and_tables() -> None:
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    # Import table models before metadata creation.
    import app.models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            _repair_interrupted_reviewrow_migration(conn)
    SQLModel.metadata.create_all(engine)
    _migrate_reviewrow_nullable_for_sqlite()
    _ensure_reviewrow_indexes_for_sqlite()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
