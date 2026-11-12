"""Isolated red-packet ledger and enterprise WeChat daily notification.

Amounts are stored as integer cents in SQLite. Pass the platform packet identifier
as ``event_id`` when available: identical OCR content cannot uniquely identify two
otherwise distinct packets. Without an identifier, canonical day/sender/amount/message
fields are fingerprinted, so identical observations are conservatively deduplicated.

The caller schedules ``run_daily_notification`` once a day (for example with Task
Scheduler). The function itself is intentionally independent of any GUI or daemon.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Sequence
from urllib.parse import urlsplit
from urllib.request import Request


# A currency marker disambiguates an amount from unrelated OCR numbers.
_AMOUNT = re.compile(r"(?<![\d.])(?:[¥￥]\s*(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s*元)")
_SPACE = re.compile(r"\s+")
_CENT = Decimal("0.01")
_WEBHOOK_HOST = "qyapi.weixin.qq.com"


def parse_amount(text: str) -> Decimal:
    """Extract exactly one nonnegative RMB amount from OCR text (¥1.20, 1元).

    An unmarked number, multiple different currency amounts, invalid grouping,
    fractional cents, or an implausibly large value raises ValueError rather than
    silently adding a wrong amount to the ledger.
    """
    if not isinstance(text, str):
        raise TypeError("amount OCR text must be a string")
    values = []
    for match in _AMOUNT.finditer(text):
        raw = match.group(1) or match.group(2)
        integer = raw.split(".")[0]
        if "," in integer and not re.fullmatch(r"\d{1,3}(?:,\d{3})+", integer):
            raise ValueError("invalid thousands grouping in amount")
        try:
            amount = Decimal(raw.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError("invalid currency amount") from exc
        if amount > Decimal("999999999999.99"):
            raise ValueError("currency amount is too large")
        values.append(amount.quantize(_CENT))
    if len(values) != 1:
        raise ValueError("expected exactly one currency amount")
    return values[0]


def _clean(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("sender and message must be strings")
    return _SPACE.sub(" ", value).strip()


@dataclass(frozen=True)
class ReportingConfig:
    """Custom report text and OCR message cleanup; phrases match literally."""

    default_messages: Sequence[str] = ()
    removal_phrases: Sequence[str] = ()
    header: str = ""
    footer: str = ""


def clean_message(message: str, config: ReportingConfig = ReportingConfig()) -> str:
    """Omit known defaults, then remove configured literal phrases."""
    message = _clean(message)
    defaults = {_clean(item) for item in config.default_messages}
    if message in defaults:
        return ""
    for phrase in config.removal_phrases:
        if phrase:
            message = message.replace(phrase, "")
    message = _clean(message)
    return "" if message in defaults else message


def _day(day: date | datetime | str | None) -> str:
    if day is None:
        return date.today().isoformat()
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    if isinstance(day, str):
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day:
            raise ValueError("day must use YYYY-MM-DD")
        return day
    raise TypeError("day must be a date, datetime, YYYY-MM-DD string, or None")


def _webhook(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("webhook URL must be a string")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid webhook port") from exc
    if (parts.scheme != "https" or parts.hostname != _WEBHOOK_HOST
            or port not in (None, 443) or parts.username or parts.password
            or parts.path != "/cgi-bin/webhook/send" or not parts.query
            or parts.fragment):
        raise ValueError("webhook must be a qyapi.weixin.qq.com HTTPS webhook/send URL")
    # The only accepted query parameter is the key; do not log the URL.
    from urllib.parse import parse_qs
    query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"key"} or len(query["key"]) != 1 or not query["key"][0]:
        raise ValueError("webhook must have exactly one nonempty key")


class DailyReporter:
    """Durable packet deduplication, deterministic daily text, and notification.

    SQLite is the source of truth; the TXT file is regenerated after each new event.
    A failed TXT write does not duplicate the database event when retried: call
    ``write_day`` to regenerate an interrupted or manually deleted record.
    """

    def __init__(self, db_path: str | Path, records_dir: str | Path,
                 config: ReportingConfig | None = None):
        self.db_path = Path(db_path)
        self.records_dir = Path(records_dir)
        self.config = config if config is not None else ReportingConfig()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS packets (
                fingerprint TEXT PRIMARY KEY, day TEXT NOT NULL,
                sender TEXT NOT NULL, cents INTEGER NOT NULL,
                message TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS packets_day ON packets(day)")
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                day TEXT PRIMARY KEY, sent_at TEXT NOT NULL)""")

    @contextmanager
    def _db(self):
        """Commit/rollback and explicitly close the SQLite handle on Windows."""
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_packet(self, sender: str, amount_text: str, message: str = "",
                      event_id: str | None = None,
                      occurred_at: date | datetime | str | None = None) -> bool:
        """Save one event and refresh its daily TXT; return False for a duplicate.

        event_id is a stable packet ID, unique across days. For non-ID observations,
        identical normalized content on the same day is treated as the same packet.
        """
        day = _day(occurred_at)
        sender = _clean(sender)
        if not sender:
            raise ValueError("sender cannot be empty")
        message = clean_message(message, self.config)
        cents = int(parse_amount(amount_text) * 100)
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("event_id must be a nonempty string")
            identity = ["id", day, event_id.strip()]
        else:
            identity = ["content", day, sender, cents, message]
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                                   separators=(",", ":")).encode("utf-8")).hexdigest()
        with self._db() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO packets VALUES (?, ?, ?, ?, ?)",
                (fingerprint, day, sender, cents, message))
            inserted = bool(result.rowcount)
        # Regenerate on repeats as well, recovering from an interrupted TXT write.
        self.write_day(day)
        return inserted

    def count_day(self, day: date | datetime | str | None = None) -> int:
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM packets WHERE day = ?", (_day(day),)).fetchone()[0])

    def render_day(self, day: date | datetime | str | None = None) -> str:
        """Render each packet in stable order, grouped totals and exact RMB total."""
        day = _day(day)
        with self._db() as db:
            rows = db.execute("SELECT sender, cents, message FROM packets "
                              "WHERE day = ? ORDER BY sender, cents, message, fingerprint",
                              (day,)).fetchall()
        lines = [self.config.header] if self.config.header else []
        lines.append(f"{day} 红包记录")
        totals: dict[str, int] = {}
        for sender, cents, message in rows:
            totals[sender] = totals.get(sender, 0) + cents
            lines.append(f"{sender}：{Decimal(cents) / 100:.2f}元" +
                         (f" — {message}" if message else ""))
        lines.append("捐赠人汇总：")
        for sender in sorted(totals):
            lines.append(f"{sender}：{Decimal(totals[sender]) / 100:.2f}元")
        lines.append(f"今日合计功德善款{Decimal(sum(totals.values())) / 100:.2f}元")
        if self.config.footer:
            lines.append(self.config.footer)
        return "\n".join(lines) + "\n"

    def write_day(self, day: date | datetime | str | None = None) -> Path:
        """Atomically replace a day's UTF-8 TXT snapshot."""
        day = _day(day)
        path = self.records_dir / f"{day}.txt"
        # A stable temp name is safe for distinct days; a sibling temporary file
        # preserves replace atomicity on Windows and avoids partially read reports.
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=self.records_dir, prefix=f".{day}.",
                                         suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(self.render_day(day))
            except BaseException:
                stream.close()
                temp.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return path

    def notify_day(self, day: date | datetime | str | None,
                   webhook_url: str, timeout: float = 10) -> bool:
        """Post text once per day; return False after a successfully sent day.

        Network/WeCom failures raise without marking a day sent, permitting retries.
        Concurrent caller processes serialize via SQLite BEGIN IMMEDIATE. A crash
        after remote acceptance but before the local commit can cause a retry; true
        exactly-once delivery cannot be guaranteed by a remote non-idempotent API.
        """
        _webhook(webhook_url)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        day = _day(day)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM notifications WHERE day = ?", (day,)).fetchone():
                return False
            content = self.render_day(day)
            payload = json.dumps({"msgtype": "text", "text": {"content": content}},
                                 ensure_ascii=False).encode("utf-8")
            request = Request(webhook_url, data=payload,
                              headers={"Content-Type": "application/json; charset=utf-8"},
                              method="POST")
            # urllib follows redirects by default: disable redirects to prevent
            # sending private daily records to an attacker-controlled destination.
            from urllib.request import build_opener, HTTPHandler, HTTPSHandler, HTTPRedirectHandler
            class _NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            # build_opener uses HTTPS verification from the stdlib SSL defaults.
            opener = build_opener(HTTPHandler, HTTPSHandler, _NoRedirect)
            with opener.open(request, timeout=timeout) as response:
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError("webhook response too large")
                result = json.loads(body)
            if not isinstance(result, dict) or result.get("errcode") != 0:
                # Never include remote error bodies or webhook URL in exceptions.
                raise RuntimeError("enterprise WeChat webhook rejected the message")
            db.execute("INSERT INTO notifications VALUES (?, ?)",
                       (day, datetime.now().isoformat()))
        return True

    def run_daily_notification(self, day: date | datetime | str | None,
                               webhook_url: str, timeout: float = 10) -> bool:
        """Scheduled-job entry point: refresh TXT and post once if not already sent."""
        self.write_day(day)
        return self.notify_day(day, webhook_url, timeout)


def run_daily_notification(db_path: str | Path, records_dir: str | Path,
                           day: date | datetime | str | None, webhook_url: str,
                           config: ReportingConfig | None = None,
                           timeout: float = 10) -> bool:
    """Convenient importable scheduled-job entry point."""
    return DailyReporter(db_path, records_dir, config).run_daily_notification(
        day, webhook_url, timeout)
