"""Database access: engine, sessions, migrations and small typed helpers."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tgmt5.config import AppConfig
from tgmt5.persistence.models import AppEventRow, Base, Setting

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")      # durability over speed for execution state
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


class Database:
    def __init__(self, url: str, *, migrate: bool = True) -> None:
        self.url = url
        self.engine: Engine = create_engine(
            url, future=True, connect_args={"check_same_thread": False})
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self._factory = sessionmaker(self.engine, expire_on_commit=False, future=True)
        if migrate:
            self.upgrade()

    @classmethod
    def at_path(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite:///{path}")

    @classmethod
    def in_memory(cls) -> "Database":
        db = cls("sqlite+pysqlite:///:memory:", migrate=False)
        Base.metadata.create_all(db.engine)
        return db

    def upgrade(self) -> None:
        """Apply Alembic migrations up to head (idempotent)."""
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", self.url)
        cfg.attributes["connection"] = None
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(Setting, key)
            return row.value if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value

    def load_config(self) -> AppConfig:
        raw = self.get_setting("app_config")
        if raw is None:
            return AppConfig()
        cfg = AppConfig.model_validate(raw)
        return cfg

    def save_config(self, cfg: AppConfig) -> None:
        import json
        data = json.loads(cfg.model_dump_json())
        data["mode"] = "MONITOR"        # execution mode is never persisted as enabled
        self.set_setting("app_config", data)

    # ------------------------------------------------------------- events
    def log_event(self, level: str, category: str, message: str, data: dict | None = None) -> None:
        with self.session() as s:
            from tgmt5.logging_setup import redact
            s.add(AppEventRow(level=level, category=category, message=redact(message)[:4000], data=data,
                              ts=datetime.now(timezone.utc)))

    def recent_events(self, limit: int = 500, level: str | None = None,
                      search: str | None = None) -> list[AppEventRow]:
        with self.session() as s:
            q = select(AppEventRow).order_by(AppEventRow.id.desc()).limit(limit)
            if level:
                q = q.where(AppEventRow.level == level)
            if search:
                q = q.where(AppEventRow.message.ilike(f"%{search}%"))
            return list(s.scalars(q))
