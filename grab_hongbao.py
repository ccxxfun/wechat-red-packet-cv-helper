from __future__ import annotations

import argparse
import json
import logging
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("hongbao")
cv2 = None
np = None
pyautogui = None


@dataclass(frozen=True)
class Match:
    center: tuple[int, int]
    top_left: tuple[int, int]
    size: tuple[int, int]
    score: float


@dataclass(frozen=True)
class AppConfig:
    confidence_threshold: float = 0.8
    hongbao_template: Path = Path("hongbao_icon.png")
    open_template: Path = Path("open_button.png")
    close_template: Path = Path("close_button.png")
    scan_interval: float = 0.5
    popup_wait: tuple[float, float] = (0.5, 1.0)
    after_open_wait: tuple[float, float] = (3.0, 4.0)
    cooldown: tuple[float, float] = (1.0, 2.0)
    click_jitter: int = 5
    region: tuple[int, int, int, int] | None = None
    max_opens: int | None = None
    once: bool = False
    scan_once: bool = False
    dry_run: bool = False
    debug_dir: Path | None = None


def random_sleep(seconds: tuple[float, float]) -> None:
    time.sleep(random.uniform(seconds[0], seconds[1]))


def parse_region(value: str | list[int] | tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise ValueError("region must be left,top,width,height")
        region = tuple(int(part) for part in parts)
    else:
        if len(value) != 4:
            raise ValueError("region must contain four integers")
        region = tuple(int(part) for part in value)
    left, top, width, height = region
    if width <= 0 or height <= 0:
        raise ValueError("region width and height must be positive")
    if left < 0 or top < 0:
        raise ValueError("region left and top must be zero or positive")
    return left, top, width, height


def parse_range(value: Any, field_name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} must be a two-item list, for example [0.5, 1.0]")
    low, high = float(value[0]), float(value[1])
    if low < 0 or high < 0 or high < low:
        raise ValueError(f"{field_name} must be non-negative and ordered")
    return low, high


def resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path


def load_config_file(path: Path | None) -> AppConfig:
    config = AppConfig()
    if path is None:
        return config

    base_dir = path.parent
    with path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)

    updates: dict[str, Any] = {}
    if "confidence_threshold" in data:
        updates["confidence_threshold"] = float(data["confidence_threshold"])
    if "scan_interval" in data:
        updates["scan_interval"] = float(data["scan_interval"])
    if "click_jitter" in data:
        updates["click_jitter"] = int(data["click_jitter"])
    if "region" in data:
        updates["region"] = parse_region(data["region"])
    if "max_opens" in data:
        updates["max_opens"] = None if data["max_opens"] is None else int(data["max_opens"])
    if "once" in data:
        updates["once"] = bool(data["once"])
    if "scan_once" in data:
        updates["scan_once"] = bool(data["scan_once"])
    if "dry_run" in data:
        updates["dry_run"] = bool(data["dry_run"])
    if "debug_dir" in data and data["debug_dir"]:
        updates["debug_dir"] = resolve_path(data["debug_dir"], base_dir)

    templates = data.get("templates", {})
    if "hongbao" in templates:
        updates["hongbao_template"] = resolve_path(templates["hongbao"], base_dir)
    if "open" in templates:
        updates["open_template"] = resolve_path(templates["open"], base_dir)
    if "close" in templates:
        updates["close_template"] = resolve_path(templates["close"], base_dir)

    delays = data.get("delays", {})
    if "popup_wait" in delays:
        updates["popup_wait"] = parse_range(delays["popup_wait"], "delays.popup_wait")
    if "after_open_wait" in delays:
        updates["after_open_wait"] = parse_range(delays["after_open_wait"], "delays.after_open_wait")
    if "cooldown" in delays:
        updates["cooldown"] = parse_range(delays["cooldown"], "delays.cooldown")

    return replace(config, **updates)


def apply_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    updates: dict[str, Any] = {}
    if args.threshold is not None:
        updates["confidence_threshold"] = args.threshold
    if args.hongbao_template is not None:
        updates["hongbao_template"] = Path(args.hongbao_template)
    if args.open_template is not None:
        updates["open_template"] = Path(args.open_template)
    if args.close_template is not None:
        updates["close_template"] = Path(args.close_template)
    if args.scan_interval is not None:
        updates["scan_interval"] = args.scan_interval
    if args.click_jitter is not None:
        updates["click_jitter"] = args.click_jitter
    if args.region is not None:
        updates["region"] = parse_region(args.region)
    if args.max_opens is not None:
        updates["max_opens"] = args.max_opens
    if args.debug_dir is not None:
        updates["debug_dir"] = Path(args.debug_dir)
    if args.once:
        updates["once"] = True
    if args.scan_once:
        updates["scan_once"] = True
    if args.dry_run:
        updates["dry_run"] = True
    return replace(config, **updates)


def validate_config(config: AppConfig) -> None:
    if not 0 < config.confidence_threshold <= 1:
        raise ValueError("confidence threshold must be within (0, 1]")
    if config.scan_interval < 0:
        raise ValueError("scan interval must be non-negative")
    if config.click_jitter < 0:
        raise ValueError("click jitter must be zero or positive")
    if config.max_opens is not None and config.max_opens <= 0:
        raise ValueError("max opens must be positive")

    required_templates = [config.hongbao_template, config.open_template]
    missing = [str(path) for path in required_templates if not path.exists()]
    if missing:
        raise FileNotFoundError("required template missing: " + ", ".join(missing))

    if config.debug_dir is not None:
        config.debug_dir.mkdir(parents=True, exist_ok=True)


def load_template(path: Path) -> Any:
    if not path.exists():
        return None
    template = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if template is None:
        LOGGER.warning("Could not read template: %s", path)
    return template


def save_debug_image(
    screenshot_bgr: Any,
    match: Match,
    label: str,
    debug_dir: Path,
) -> None:
    marked = screenshot_bgr.copy()
    x, y = match.top_left
    width, height = match.size
    cv2.rectangle(marked, (x, y), (x + width, y + height), (0, 255, 0), 2)
    cv2.putText(
        marked,
        f"{label} {match.score:.3f}",
        (x, max(12, y - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 255, 0),
        1,
        cv2.LINE_AA,
    )
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = debug_dir / f"{timestamp}-{label}.png"
    cv2.imwrite(str(filename), marked)
    LOGGER.debug("Saved debug image: %s", filename)


def load_runtime_dependencies() -> None:
    global cv2, np, pyautogui

    try:
        import cv2 as cv2_module
        import numpy as numpy_module
        import pyautogui as pyautogui_module
    except ImportError as exc:
        raise RuntimeError("runtime dependency missing; install dependencies with pip install -r requirements.txt") from exc

    cv2 = cv2_module
    np = numpy_module
    pyautogui = pyautogui_module


def find_image_on_screen(
    template_path: Path,
    confidence: float,
    region: tuple[int, int, int, int] | None = None,
    debug_dir: Path | None = None,
    label: str = "match",
) -> Match | None:
    if cv2 is None or np is None or pyautogui is None:
        raise RuntimeError("runtime dependencies are not loaded")

    template = load_template(template_path)
    if template is None:
        return None

    screenshot = pyautogui.screenshot(region=region)
    screenshot_np = np.array(screenshot)
    screenshot_bgr = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)

    result = cv2.matchTemplate(screenshot_bgr, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    LOGGER.debug("%s score: %.3f", label, max_val)

    if max_val < confidence:
        return None

    template_height, template_width = template.shape[:2]
    offset_x = region[0] if region else 0
    offset_y = region[1] if region else 0
    match = Match(
        center=(offset_x + max_loc[0] + template_width // 2, offset_y + max_loc[1] + template_height // 2),
        top_left=(max_loc[0], max_loc[1]),
        size=(template_width, template_height),
        score=float(max_val),
    )

    if debug_dir is not None:
        save_debug_image(screenshot_bgr, match, label, debug_dir)

    return match


def click_at(position: tuple[int, int], jitter: int, dry_run: bool, duration: float = 0.1) -> None:
    x, y = position
    if jitter:
        x += random.randint(-jitter, jitter)
        y += random.randint(-jitter, jitter)
    if dry_run:
        LOGGER.info("Dry run: would click at (%s, %s)", x, y)
        return
    if pyautogui is None:
        raise RuntimeError("pyautogui is not available; install dependencies with pip install -r requirements.txt")
    pyautogui.moveTo(x, y, duration=duration)
    pyautogui.click()


def process_one_hongbao(config: AppConfig) -> bool:
    hongbao = find_image_on_screen(
        config.hongbao_template,
        config.confidence_threshold,
        region=config.region,
        debug_dir=config.debug_dir,
        label="hongbao",
    )
    if hongbao is None:
        return False

    LOGGER.info("Red packet detected at %s, score=%.3f", hongbao.center, hongbao.score)
    click_at(hongbao.center, config.click_jitter, config.dry_run, duration=0.2)

    if config.dry_run:
        return True

    random_sleep(config.popup_wait)
    open_button = find_image_on_screen(
        config.open_template,
        config.confidence_threshold,
        region=config.region,
        debug_dir=config.debug_dir,
        label="open",
    )
    if open_button is None:
        LOGGER.warning("Open button was not found after clicking the red packet")
        return False

    LOGGER.info("Open button detected at %s, score=%.3f", open_button.center, open_button.score)
    click_at(open_button.center, 0, config.dry_run)
    random_sleep(config.after_open_wait)

    close_button = find_image_on_screen(
        config.close_template,
        config.confidence_threshold,
        region=config.region,
        debug_dir=config.debug_dir,
        label="close",
    )
    if close_button is None:
        LOGGER.info("Close button not found; pressing ESC")
        pyautogui.press("esc")
    else:
        LOGGER.info("Close button detected at %s, score=%.3f", close_button.center, close_button.score)
        click_at(close_button.center, 0, config.dry_run)

    return True


def run(config: AppConfig) -> int:
    validate_config(config)
    load_runtime_dependencies()
    pyautogui.FAILSAFE = True

    LOGGER.info("WeChat red packet helper started")
    LOGGER.info("Move the mouse to the top-left corner to trigger PyAutoGUI failsafe")
    LOGGER.info("Template threshold: %.2f", config.confidence_threshold)
    if config.region:
        LOGGER.info("Scanning region: left=%s top=%s width=%s height=%s", *config.region)
    if config.dry_run:
        LOGGER.info("Dry run enabled: no mouse clicks will be sent")

    opened = 0
    try:
        while True:
            matched = process_one_hongbao(config)
            if matched:
                opened += 1
                LOGGER.info("Completed red packet flow count: %s", opened)
                if config.once:
                    LOGGER.info("Once mode complete")
                    return 0
                if config.max_opens is not None and opened >= config.max_opens:
                    LOGGER.info("Reached max opens: %s", config.max_opens)
                    return 0
                random_sleep(config.cooldown)
            elif config.scan_once:
                LOGGER.info("Scan-once mode complete; no red packet found")
                return 1
            else:
                time.sleep(config.scan_interval)
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PC WeChat red packet helper based on OpenCV template matching.",
    )
    parser.add_argument("--config", help="Path to a JSON config file")
    parser.add_argument("--threshold", type=float, help="Template matching confidence, for example 0.8")
    parser.add_argument("--hongbao-template", help="Path to the red packet template image")
    parser.add_argument("--open-template", help="Path to the open-button template image")
    parser.add_argument("--close-template", help="Path to the close-button template image")
    parser.add_argument("--scan-interval", type=float, help="Seconds between scans when nothing is found")
    parser.add_argument("--region", help="Limit scanning to left,top,width,height")
    parser.add_argument("--click-jitter", type=int, help="Random pixel offset for clicking the packet")
    parser.add_argument("--max-opens", type=int, help="Stop after opening this many red packets")
    parser.add_argument("--once", action="store_true", help="Stop after one detected red packet flow")
    parser.add_argument("--scan-once", action="store_true", help="Scan one time and exit")
    parser.add_argument("--dry-run", action="store_true", help="Detect and log only; do not click")
    parser.add_argument("--debug-dir", help="Save matched screenshots to this folder")
    parser.add_argument("--verbose", action="store_true", help="Show debug-level matching scores")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose)

    try:
        config_path = Path(args.config) if args.config else None
        config = apply_cli_overrides(load_config_file(config_path), args)
        return run(config)
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
