"""Bounded, opt-in desktop WeChat vision helper (not wired into grab_hongbao.py).

All regions are absolute screen coordinates (left, top, width, height). Configure
regions from your own visible, already-selected WeChat window; this module does
not discover windows, switch conversations in the background, or run forever.
The selected conversation header must be OCR-readable and exactly allowlisted
before *every* action. A caller is responsible for focus, desktop scaling,
window placement, template capture, and permission to automate the account.
False-negative OCR or template matches deliberately prevent clicks. OCR backends
are optional: install rapidocr_onnxruntime, or pytesseract plus the external
Tesseract executable. OpenCV and numpy are required only for OpenCVMatcher;
PyAutoGUI is required only for PyAutoGUIBackend. Inject fakes for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
import math
import time
import unicodedata


CLAIMED_STATES = ("已领取", "已抢完", "已过期")


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if any(type(n) is not int for n in (self.left, self.top, self.width, self.height)):
            raise ValueError("region coordinates must be integers")
        if self.left < 0 or self.top < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("region needs nonnegative origin and positive size")

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    def contains(self, other: Region) -> bool:
        return (self.left <= other.left and self.top <= other.top
                and other.left + other.width <= self.left + self.width
                and other.top + other.height <= self.top + self.height)

    def relative_to(self, outer: Region) -> Region:
        if not outer.contains(self):
            raise ValueError("ROI must be inside its parent region")
        return Region(self.left - outer.left, self.top - outer.top, self.width, self.height)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height


@dataclass(frozen=True)
class OCRLine:
    text: str
    box: Region  # local to the screenshot passed to the OCR engine


@dataclass(frozen=True)
class Match:
    box: Region  # absolute screen coordinates
    score: float
    template: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return self.box.center


@dataclass(frozen=True)
class ProcessResult:
    """One attempted card; screenshot is result ROI, never full desktop.

    ``card_key`` includes group, card geometry and OCR of the card itself; it
    cannot guarantee identity across scrolling/reflow. Callers must deduplicate
    conservatively and not treat a moved card as a new packet automatically.
    """
    group: str
    card: Match
    card_key: tuple[str, Region, tuple[str, ...]]
    card_text: tuple[str, ...]
    card_clicked: bool
    open_clicked: bool
    result_image: Any | None = None
    result_lines: tuple[OCRLine, ...] = ()
    result_region: Region | None = None
    close_clicked: bool = False
    status: str = "available"


class Backend(Protocol):
    def screenshot(self, region: Region) -> Any: ...
    def click(self, x: int, y: int) -> None: ...
    def scroll(self, amount: int, x: int, y: int) -> None: ...


class OCREngine(Protocol):
    def read(self, image: Any) -> Sequence[OCRLine]: ...


class Matcher(Protocol):
    def find_all(self, image: Any, templates: Sequence[str | Path], region: Region,
                 threshold: float) -> Sequence[Match]: ...


@dataclass(frozen=True)
class VisionConfig:
    allowed_groups: frozenset[str]
    header_region: Region
    chat_region: Region
    card_templates: tuple[str | Path, ...]
    open_templates: tuple[str | Path, ...] = ()
    close_templates: tuple[str | Path, ...] = ()
    claimed_templates: tuple[str | Path, ...] = ()
    list_region: Region | None = None
    history_region: Region | None = None
    # Absolute inset of the card match (left,top,width,height); not a screen ROI.
    card_status_roi: tuple[int, int, int, int] | None = None
    # Optional popup/result region. If omitted, only chat region is searched.
    popup_region: Region | None = None
    # Explicit result-page crop; defaults to popup_region, then chat_region.
    result_region: Region | None = None
    # Positive OCR evidence required; adapt this to your exact card template.
    available_texts: tuple[str, ...] = ("微信红包", "领取红包", "恭喜发财", "红包")
    card_wait: float = 0.3
    result_wait: float = 0.5
    threshold: float = 0.84
    max_scroll_attempts: int = 0
    scroll_step: int = 3
    navigation_scroll_attempts: int = 0

    def __post_init__(self) -> None:
        if not self.allowed_groups or any(not normalize_name(name) for name in self.allowed_groups):
            raise ValueError("allowed_groups must contain nonempty exact group names")
        if len({normalize_name(n) for n in self.allowed_groups}) != len(self.allowed_groups):
            raise ValueError("duplicate normalized group names")
        if not self.card_templates:
            raise ValueError("at least one card template is required")
        if not self.available_texts or any(not text.strip() for text in self.available_texts):
            raise ValueError("available_texts must contain explicit status phrases")
        if any(not math.isfinite(delay) or delay < 0 or delay > 30 for delay in (self.card_wait, self.result_wait)):
            raise ValueError("wait times must be finite in [0, 30]")
        if not 0 < self.threshold <= 1 or not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite and in (0, 1]")
        if type(self.max_scroll_attempts) is not int or not 0 <= self.max_scroll_attempts <= 100:
            raise ValueError("max_scroll_attempts must be between 0 and 100")
        if type(self.navigation_scroll_attempts) is not int or not 0 <= self.navigation_scroll_attempts <= 100:
            raise ValueError("navigation_scroll_attempts must be between 0 and 100")
        if type(self.scroll_step) is not int or not 0 < self.scroll_step <= 30:
            raise ValueError("scroll_step must be between 1 and 30")
        if self.max_scroll_attempts and self.history_region is None:
            raise ValueError("history_region required for scrolling")
        if self.navigation_scroll_attempts and self.list_region is None:
            raise ValueError("list_region required for navigation scrolling")
        if self.history_region and not self.chat_region.contains(self.history_region):
            raise ValueError("history_region must lie inside chat_region")
        if self.card_status_roi is not None:
            if len(self.card_status_roi) != 4 or any(type(n) is not int for n in self.card_status_roi):
                raise ValueError("card_status_roi needs four integer insets")
            x, y, width, height = self.card_status_roi
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                raise ValueError("card_status_roi must have nonnegative offsets and positive size")


def normalize_name(value: str) -> str:
    """Normalize Unicode and outer whitespace, never strip suffixes or fuzzy match."""
    return unicodedata.normalize("NFKC", value).strip()


def _claimed(text: str) -> bool:
    return any(state in unicodedata.normalize("NFKC", text).replace(" ", "") for state in CLAIMED_STATES)


def _lines(ocr: OCREngine, image: Any) -> Sequence[OCRLine]:
    try:
        return tuple(line for line in ocr.read(image) if isinstance(line.text, str))
    except Exception:
        return ()  # OCR failure must never authorize a click


class VisionHelper:
    """Single-step actions; no daemon, implicit navigation, or background scan.

    ``scan_once`` returns an unclaimed card Match or None without clicking.
    ``process_one`` clicks at most one card, optional open button, and optional
    close button; returns a ProcessResult or None. ``open_clicked`` is False
    if the open template is missing, even if the card was selected.
    """

    def __init__(self, config: VisionConfig, backend: Backend | None = None,
                 ocr: OCREngine | None = None, matcher: Matcher | None = None):
        self.config = config
        self.backend = backend if backend is not None else PyAutoGUIBackend()
        self.ocr = ocr if ocr is not None else auto_ocr()
        self.matcher = matcher if matcher is not None else OpenCVMatcher()
        self._allowed = frozenset(normalize_name(name) for name in config.allowed_groups)

    def verify_group(self) -> str | None:
        """Return exact allowlisted header name; unreadable/ambiguous returns None."""
        region = self.config.header_region
        lines = _lines(self.ocr, self.backend.screenshot(region))
        names = {normalize_name(line.text) for line in lines if region.contains(
            Region(region.left + line.box.left, region.top + line.box.top,
                   line.box.width, line.box.height))}
        if len(names) == 1:
            name = next(iter(names))
            return name if name in self._allowed else None
        return None

    def navigate_to_group(self, group: str) -> bool:
        """Navigate only when explicitly requested, via exact OCR within list_region.

        A click is restricted to the chosen OCR box; after clicking, OCR header
        must independently verify the requested group. No search UI or other
        chats are opened. Existing selected chat must already be allowlisted.
        """
        target = normalize_name(group)
        region = self.config.list_region
        if target not in self._allowed or region is None or self.verify_group() is None:
            return False
        for attempt in range(self.config.navigation_scroll_attempts + 1):
            matches = []
            for line in _lines(self.ocr, self.backend.screenshot(region)):
                if normalize_name(line.text) != target or not region.contains(
                    Region(region.left + line.box.left, region.top + line.box.top,
                           line.box.width, line.box.height)):
                    continue
                matches.append(line.box)
            if len(matches) == 1:
                box = matches[0]
                if self.verify_group() is None:
                    return False
                self.backend.click(region.left + box.center[0], region.top + box.center[1])
                return self.verify_group() == target
            if len(matches) > 1:
                return False  # ambiguous list entry
            if attempt < self.config.navigation_scroll_attempts:
                if self.verify_group() is None:
                    return False
                self.backend.scroll(-self.config.scroll_step, *region.center)
        return False

    def _find(self, templates: Sequence[str | Path], region: Region) -> tuple[Match, ...]:
        if not templates:
            return ()
        image = self.backend.screenshot(region)
        matches = self.matcher.find_all(image, templates, region, self.config.threshold)
        return tuple(m for m in matches if region.contains(m.box) and
                     math.isfinite(m.score) and m.score >= self.config.threshold)

    def card_status(self, card: Match) -> str | None:
        """Return claimed state, 'available', or None (unreadable/unsafe).

        OCR is scoped to the card status inset; claimed template detection is
        scoped to the card. OCR failure or no status evidence is unsafe. A
        clearly readable non-claimed OCR line is the only 'available' evidence.
        """
        card_box = card.box
        if not self.config.chat_region.contains(card_box):
            return None
        for match in self._find(self.config.claimed_templates, card_box):
            if card_box.contains(match.box):
                return "已领取"  # claimed template may depict any claimed state
        inset = self.config.card_status_roi
        if inset is None:
            return None
        x, y, width, height = inset
        status_box = Region(card_box.left + x, card_box.top + y, width, height)
        if not card_box.contains(status_box):
            return None
        lines = _lines(self.ocr, self.backend.screenshot(status_box))
        texts = [line.text.strip() for line in lines if line.text.strip() and
                 Region(0, 0, width, height).contains(line.box)]
        if any(_claimed(text) for text in texts):
            return next(state for state in CLAIMED_STATES if
                        any(state in unicodedata.normalize("NFKC", text).replace(" ", "") for text in texts))
        normalized = tuple(unicodedata.normalize("NFKC", text).replace(" ", "") for text in texts)
        return "available" if any(unicodedata.normalize("NFKC", phrase).replace(" ", "") in text
                                  for text in normalized for phrase in self.config.available_texts) else None

    def _card_identity(self, group: str, card: Match) -> tuple[tuple[str, Region, tuple[str, ...]], tuple[str, ...]]:
        lines = _lines(self.ocr, self.backend.screenshot(card.box))
        texts = tuple(line.text for line in lines
                      if Region(0, 0, card.box.width, card.box.height).contains(line.box))
        return (group, card.box, texts), texts

    def scan_once(self, *, direction: str = "up",
                  seen_cards: set[tuple[str, Region, tuple[str, ...]]] | None = None) -> Match | None:
        """Find an unseen unclaimed card in current chat; optionally scroll boundedly.

        ``direction`` is 'up' (older) or 'down' (newer). Each screenshot and
        scroll is preceded by fresh selected-header verification. ``seen_cards``
        is caller-owned; no method silently mutates it. Card identity uses
        group, geometry, and card-local OCR, never the full screenshot.
        """
        if direction not in ("up", "down"):
            raise ValueError("direction must be 'up' or 'down'")
        region = self.config.history_region or self.config.chat_region
        for attempt in range(self.config.max_scroll_attempts + 1):
            if self.verify_group() is None:
                return None
            cards = sorted(self._find(self.config.card_templates, region),
                           key=lambda m: (-m.score, m.box.top, m.box.left))
            for card in cards:
                group = self.verify_group()
                if group is None:
                    return None
                if self.card_status(card) == "available":
                    if seen_cards is not None:
                        key, _ = self._card_identity(group, card)
                        if key in seen_cards:
                            continue
                    return card
            if attempt < self.config.max_scroll_attempts:
                if self.verify_group() is None:
                    return None
                self.backend.scroll(self.config.scroll_step if direction == "up" else
                                    -self.config.scroll_step, *region.center)
        return None

    def process_one(self, *, direction: str = "up",
                    seen_cards: set[tuple[str, Region, tuple[str, ...]]] | None = None) -> ProcessResult | None:
        """Process one eligible card; OCR result before closing its page.

        The caller should inspect ``card_key`` and ``open_clicked`` before
        reporting success. No automatic retry of a card occurs in this method.
        """
        card = self.scan_once(direction=direction, seen_cards=seen_cards)
        group = self.verify_group()
        if card is None or group is None or self.card_status(card) != "available":
            return None
        key, card_text = self._card_identity(group, card)
        if (seen_cards is not None and key in seen_cards) or self.verify_group() != group or self.card_status(card) != "available":
            return None
        self.backend.click(*card.center)
        # Reserve after the click, including when a popup later fails to open.
        if seen_cards is not None:
            seen_cards.add(key)
        result = ProcessResult(group, card, key, card_text, True, False)
        if self.config.card_wait:
            time.sleep(self.config.card_wait)
        popup = self.config.popup_region or self.config.chat_region
        if self.verify_group() != group:
            return result
        # A claimed result in popup prevents clicking a stale/open-like button.
        popup_lines = _lines(self.ocr, self.backend.screenshot(popup))
        if any(_claimed(line.text) for line in popup_lines):
            return ProcessResult(group, card, key, card_text, True, False,
                                 status="claimed_popup")
        buttons = sorted(self._find(self.config.open_templates, popup), key=lambda m: -m.score)
        if not buttons or self.verify_group() != group:
            return result
        self.backend.click(*buttons[0].center)
        if self.config.result_wait:
            time.sleep(self.config.result_wait)
        if self.verify_group() != group:
            return ProcessResult(group, card, key, card_text, True, True)
        result_region = self.config.result_region or popup
        image = self.backend.screenshot(result_region)
        lines = tuple(line for line in _lines(self.ocr, image)
                      if Region(0, 0, result_region.width, result_region.height).contains(line.box))
        result = ProcessResult(group, card, key, card_text, True, True,
                               image, lines, result_region, status="opened")
        if self.verify_group() != group:
            return result
        closes = sorted(self._find(self.config.close_templates, popup), key=lambda m: -m.score)
        if closes and self.verify_group() == group:
            self.backend.click(*closes[0].center)
            result = ProcessResult(group, card, key, card_text, True, True,
                                   image, lines, result_region, True, "opened")
        return result


class PyAutoGUIBackend:
    """Lazy desktop adapter; PyAutoGUI's corner failsafe stays enabled."""
    def __init__(self) -> None:
        import pyautogui
        pyautogui.FAILSAFE = True
        self._gui = pyautogui

    def screenshot(self, region: Region) -> Any:
        return self._gui.screenshot(region=region.as_tuple())

    def click(self, x: int, y: int) -> None:
        self._gui.click(x, y)

    def scroll(self, amount: int, x: int, y: int) -> None:
        self._gui.moveTo(x, y)
        self._gui.scroll(amount)


def _to_rgb(image: Any) -> Any:
    import numpy as np
    if hasattr(image, "convert"):
        return np.asarray(image.convert("RGB"))
    array = np.asarray(image)
    if array.ndim not in (2, 3):
        raise ValueError("image must be a PIL image or numpy array")
    return array


class OpenCVMatcher:
    """OpenCV multitemplate all-occurrence matching with local NMS.

    Templates are captured at the same DPI/scale as the screenshot. Textured
    templates are required: constant-valued images are rejected rather than
    accepting OpenCV's degenerate correlation score.
    """
    def __init__(self, nms_overlap: float = 0.35):
        if not 0 <= nms_overlap < 1:
            raise ValueError("nms_overlap must be in [0, 1)")
        self.nms_overlap = nms_overlap

    def find_all(self, image: Any, templates: Sequence[str | Path], region: Region,
                 threshold: float) -> tuple[Match, ...]:
        import cv2
        import numpy as np
        screenshot = _to_rgb(image)
        gray = cv2.cvtColor(screenshot, cv2.COLOR_RGB2GRAY) if screenshot.ndim == 3 else screenshot
        candidates: list[Match] = []
        for path in templates:
            template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                continue
            h, w = template.shape[:2]
            if h > gray.shape[0] or w > gray.shape[1] or float(np.std(template)) < 1.0:
                continue
            scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(scores >= threshold)
            for y, x in zip(ys, xs):
                score = float(scores[y, x])
                if math.isfinite(score):
                    candidates.append(Match(Region(region.left + int(x), region.top + int(y), w, h),
                                            score, str(path)))
        candidates.sort(key=lambda m: -m.score)
        kept: list[Match] = []
        for item in candidates:
            if all(_intersection_over_union(item.box, prior.box) <= self.nms_overlap for prior in kept):
                kept.append(item)
        return tuple(kept)


def _intersection_over_union(a: Region, b: Region) -> float:
    w = max(0, min(a.left + a.width, b.left + b.width) - max(a.left, b.left))
    h = max(0, min(a.top + a.height, b.top + b.height) - max(a.top, b.top))
    overlap = w * h
    return overlap / (a.width * a.height + b.width * b.height - overlap)


class RapidOCREngine:
    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()

    def read(self, image: Any) -> tuple[OCRLine, ...]:
        output, _ = self._ocr(_to_rgb(image))
        if not output:
            return ()
        lines = []
        for polygon, text, confidence in output:
            if confidence < 0.5:
                continue
            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            left, top, right, bottom = math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))
            if right > left and bottom > top:
                lines.append(OCRLine(str(text), Region(left, top, right - left, bottom - top)))
        return tuple(lines)


class TesseractOCREngine:
    """Requires pytesseract and a separately installed Tesseract with chi_sim."""
    def __init__(self, language: str = "chi_sim+eng", executable: str | None = None):
        import pytesseract
        if executable is not None:
            pytesseract.pytesseract.tesseract_cmd = executable
        self._ocr = pytesseract
        self.language = language

    def read(self, image: Any) -> tuple[OCRLine, ...]:
        from pytesseract import Output
        data: Mapping[str, Sequence[Any]] = self._ocr.image_to_data(
            _to_rgb(image), lang=self.language, output_type=Output.DICT)
        lines = []
        for i, text in enumerate(data["text"]):
            if not str(text).strip() or float(data["conf"][i]) < 50:
                continue
            lines.append(OCRLine(str(text), Region(int(data["left"][i]), int(data["top"][i]),
                                                   int(data["width"][i]), int(data["height"][i]))))
        return tuple(lines)


def auto_ocr() -> OCREngine:
    """Prefer self-contained RapidOCR; no silent OCR bypass on missing deps."""
    try:
        return RapidOCREngine()
    except ImportError:
        try:
            return TesseractOCREngine()
        except ImportError as exc:
            raise RuntimeError("Install rapidocr_onnxruntime, or pytesseract and Tesseract OCR") from exc
