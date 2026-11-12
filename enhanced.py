"""Opt-in, fail-closed integration of WeChat OCR/vision and daily reporting.

No automatic action occurs until a user explicitly enables enhanced mode, selects a
known group, and supplies calibrated absolute screen regions and template images.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
import re
import time
from typing import Any

from reporting import DailyReporter, ReportingConfig
from vision import Region, VisionConfig, VisionHelper

LOG = logging.getLogger("hongbao.enhanced")
_CURRENCY = re.compile(r"(?:[¥￥]\s*\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s*元)")


def _region(values: Any, label: str) -> Region:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"{label} must be [left, top, width, height]")
    return Region(*values)


def _paths(items: Any, root: Path) -> tuple[Path, ...]:
    if not isinstance(items, list):
        raise ValueError("template paths must be an array")
    paths = tuple((root / value).resolve() for value in items if isinstance(value, str) and value.strip())
    if len(paths) != len(items) or any(not p.is_file() for p in paths):
        raise ValueError("all template paths must refer to existing image files")
    return paths


def _strings(items: Any, label: str) -> tuple[str, ...]:
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(items)


@dataclass(frozen=True)
class Settings:
    vision: VisionConfig
    reporting: ReportingConfig
    db_path: Path
    records_dir: Path
    selected_group: str
    navigate_group: bool
    webhook_env: str
    allow_empty_report: bool
    push_time: day_time | None
    scan_interval: float
    direction: str
    once: bool
    scan_once: bool
    dry_run: bool
    max_opens: int | None


def load_settings(config_path: Path, *, dry_run: bool = False,
                  scan_once: bool = False, once: bool = False,
                  max_opens: int | None = None) -> Settings:
    """Load the opt-in enhanced object from config, rejecting missing safety fields."""
    root = config_path.resolve().parent
    data = json.loads(config_path.read_text(encoding="utf-8"))
    cfg = data.get("enhanced")
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        raise ValueError("set enhanced.enabled=true in the JSON config to enable enhanced mode")
    groups = _strings(cfg.get("allowed_groups"), "allowed_groups")
    selected = cfg.get("selected_group")
    if not isinstance(selected, str) or not selected or selected not in groups:
        raise ValueError("selected_group must exactly match an allowed_groups entry")
    templates = cfg.get("templates")
    if not isinstance(templates, dict):
        raise ValueError("enhanced.templates is required")
    header = _region(cfg.get("header_region"), "header_region")
    chat = _region(cfg.get("chat_region"), "chat_region")
    optional = lambda key: _region(cfg[key], key) if cfg.get(key) is not None else None
    status = cfg.get("card_status_roi")
    if not isinstance(status, list) or len(status) != 4 or any(type(x) is not int for x in status):
        raise ValueError("card_status_roi must be [left, top, width, height] relative to the card")
    vcfg = VisionConfig(
        allowed_groups=frozenset(groups), header_region=header, chat_region=chat,
        card_templates=_paths(templates.get("cards"), root),
        open_templates=_paths(templates.get("open"), root),
        close_templates=_paths(templates.get("close", []), root),
        claimed_templates=_paths(templates.get("claimed", []), root),
        list_region=optional("list_region"), history_region=optional("history_region"),
        popup_region=optional("popup_region"), result_region=optional("result_region"),
        card_status_roi=tuple(status),
        available_texts=_strings(cfg.get("available_texts", ["微信红包", "领取红包", "恭喜发财"]), "available_texts"),
        card_wait=float(cfg.get("card_wait", 0.5)), result_wait=float(cfg.get("result_wait", 1.0)),
        threshold=float(cfg.get("threshold", 0.84)),
        max_scroll_attempts=cfg.get("max_scroll_attempts", 0),
        navigation_scroll_attempts=cfg.get("navigation_scroll_attempts", 0),
        scroll_step=cfg.get("scroll_step", 3),
    )
    if not vcfg.open_templates:
        raise ValueError("at least one open template is required")
    if cfg.get("navigate_group", False) is True and vcfg.list_region is None:
        raise ValueError("list_region is required when navigate_group is true")
    report = cfg.get("reporting", {})
    if not isinstance(report, dict):
        raise ValueError("reporting must be an object")
    rcfg = ReportingConfig(
        default_messages=_strings(report.get("default_messages", []), "default_messages"),
        removal_phrases=_strings(report.get("removal_phrases", []), "removal_phrases"),
        header=str(report.get("header", "")), footer=str(report.get("footer", "")),
    )
    push = cfg.get("push_time")
    if push is not None:
        if not isinstance(push, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", push):
            raise ValueError("push_time must be 24-hour HH:MM")
        push_time = day_time.fromisoformat(push)
    else:
        push_time = None
    webhook_env = cfg.get("webhook_env", "WECOM_WEBHOOK_URL")
    if not isinstance(webhook_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", webhook_env):
        raise ValueError("webhook_env must name an uppercase environment variable")
    direction = cfg.get("scroll_direction", "up")
    if direction not in ("up", "down"):
        raise ValueError("scroll_direction must be up or down")
    interval = float(cfg.get("scan_interval", 1.0))
    if not 0.2 <= interval <= 3600:
        raise ValueError("scan_interval must be between 0.2 and 3600 seconds")
    max_opens = max_opens if max_opens is not None else cfg.get("max_opens")
    if max_opens is not None and (type(max_opens) is not int or max_opens <= 0):
        raise ValueError("max_opens must be a positive integer")
    return Settings(
        vision=vcfg, reporting=rcfg, db_path=(root / report.get("db_path", "records/packets.sqlite3")).resolve(),
        records_dir=(root / report.get("records_dir", "records")).resolve(),
        selected_group=selected, navigate_group=cfg.get("navigate_group", False) is True,
        webhook_env=webhook_env,
        allow_empty_report=cfg.get("allow_empty_report", False) is True, push_time=push_time,
        scan_interval=interval, direction=direction,
        once=once or cfg.get("once", False), scan_once=scan_once or cfg.get("scan_once", False),
        dry_run=dry_run or cfg.get("dry_run", False), max_opens=max_opens,
    )


def extract_packet(lines: list[str] | tuple[str, ...], defaults: tuple[str, ...] = ()) -> tuple[str, str, str] | None:
    """Conservative OCR extraction. Missing/conflicting sender or amount is never logged.

    Sender pattern is a site-specific heuristic; visually verify extracted text.
    """
    text = [line.strip() for line in lines if line.strip()]
    candidates = [(item, match) for item in text for match in _CURRENCY.finditer(item)]
    if len(candidates) != 1:
        return None
    amount = candidates[0][1].group(0)
    senders = set()
    for item in text:
        match = re.search(r"(?:来自|发送者[：:]|领取了)([^\s：:]{1,40})的红包", item)
        if match:
            senders.add(match.group(1).strip())
            continue
        match = re.search(r"^([^\s：:]{1,40})的红包$", item)
        if match:
            senders.add(match.group(1).strip())
    if len(senders) != 1:
        return None
    sender = next(iter(senders))
    message = ""
    for item in text:
        if item in defaults:
            message = item
            break
    if not message:
        leftovers = [item for item in text if item != candidates[0][0] and sender not in item
                     and item not in ("微信红包", "红包", "开", "已领取", "红包详情")]
        if len(leftovers) == 1:
            message = leftovers[0]
    return sender, amount, message


def _push_if_due(settings: Settings, reporter: DailyReporter, now: datetime) -> None:
    if settings.push_time is None or now.time() < settings.push_time:
        return
    # Dispatch yesterday's final ledger after midnight; late screenshots need review.
    day = now.date() - timedelta(days=1)
    webhook = os.getenv(settings.webhook_env, "")
    if not webhook:
        LOG.warning("Webhook environment variable is missing; report remains local")
        return
    if reporter.count_day(day) == 0 and not settings.allow_empty_report:
        return
    try:
        if reporter.run_daily_notification(day, webhook):
            LOG.info("Daily report sent for %s", day)
    except Exception as exc:
        LOG.warning("Daily report send failed (%s); retry next scan", type(exc).__name__)


def run(settings: Settings) -> int:
    reporter = DailyReporter(settings.db_path, settings.records_dir, settings.reporting)
    helper = VisionHelper(settings.vision)
    count = 0
    seen_cards: set[tuple[Any, ...]] = set()
    seen_day = date.today()
    try:
        while True:
            if date.today() != seen_day:
                seen_day = date.today()
                seen_cards.clear()
            current_group = helper.verify_group()
            if current_group is not None and current_group != settings.selected_group and settings.navigate_group:
                # Never navigate from a chat outside the explicit allowlist.
                if not settings.dry_run:
                    helper.navigate_to_group(settings.selected_group)
                current_group = helper.verify_group()
            if current_group != settings.selected_group:
                if not settings.dry_run:
                    _push_if_due(settings, reporter, datetime.now())
                LOG.warning("Selected group mismatch or unreadable; no packet clicks")
                if settings.scan_once:
                    return 1
                time.sleep(settings.scan_interval)
                continue
            if not settings.dry_run:
                _push_if_due(settings, reporter, datetime.now())
            if settings.dry_run:
                result = helper.scan_once(direction=settings.direction, seen_cards=seen_cards)
                LOG.info("Dry run: eligible packet %s", "detected (no click)" if result else "not found")
            else:
                result = helper.process_one(direction=settings.direction, seen_cards=seen_cards)
                if result and result.card_clicked:
                    seen_cards.add(result.card_key)
                if result and result.open_clicked and result.status == "opened" and result.group == settings.selected_group:
                    count += 1
                    # Result-page OCR is captured before closing; never guess a missing amount.
                    details = extract_packet(tuple(line.text for line in result.result_lines),
                                             tuple(settings.reporting.default_messages))
                    if details is None:
                        LOG.warning("Amount/sender OCR uncertain; packet not recorded (no guessed amount)")
                    else:
                        sender, amount, message = details
                        identity = json.dumps([result.group, sender, amount, message], ensure_ascii=False)
                        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                        reporter.record_packet(sender, amount, message, event_id=event_id)
                    if settings.once or (settings.max_opens is not None and count >= settings.max_opens):
                        return 0
            if settings.scan_once:
                return 0 if result else 1
            time.sleep(settings.scan_interval)
    except KeyboardInterrupt:
        return 0


def send_report(settings: Settings, day: date) -> bool:
    """Manual/Task Scheduler entry point; does not require a desktop or OCR."""
    reporter = DailyReporter(settings.db_path, settings.records_dir, settings.reporting)
    if reporter.count_day(day) == 0 and not settings.allow_empty_report:
        raise ValueError("no packets recorded for this date; refusing to send an empty report")
    webhook = os.getenv(settings.webhook_env, "")
    if not webhook:
        raise ValueError("Webhook environment variable is missing")
    return reporter.run_daily_notification(day, webhook)
