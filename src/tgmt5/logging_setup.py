"""Structured (JSON-lines) logging with secret redaction."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_SECRETS: set[str] = set()
_LOCK = threading.Lock()
_PATTERNS = [
    re.compile(r"(?i)(password|passwd|api_hash|api-hash|session|token|secret)(\s*[=:]\s*)([^\s,;'\"]+)"),
    re.compile(r"\b1[A-Za-z0-9_-]{200,}={0,2}"),        # Telethon StringSession-like blobs
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        with _LOCK:
            _SECRETS.add(value)


def redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 3
                     else "***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = redact(msg)
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
             "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
             "thread": record.threadName}
        if record.exc_info:
            d["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(d, ensure_ascii=False)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(log_dir / "tgmt5.jsonl", maxBytes=10_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(RedactingFilter())
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    sh.addFilter(RedactingFilter())
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("telethon").setLevel(logging.WARNING)
