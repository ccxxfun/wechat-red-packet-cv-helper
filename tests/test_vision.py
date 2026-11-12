"""Deterministic tests: no desktop, native OCR, or OpenCV required."""
import unittest
from dataclasses import replace

from vision import Match, OCRLine, Region, VisionConfig, VisionHelper


HEADER = Region(100, 20, 200, 40)
CHAT = Region(300, 80, 400, 500)
LIST = Region(0, 80, 280, 500)
CARD = Region(350, 160, 120, 65)
STATUS = Region(355, 190, 110, 25)
POPUP = Region(350, 100, 300, 350)
RESULT = Region(400, 140, 200, 250)


class FakeBackend:
    def __init__(self):
        self.screens = []
        self.clicks = []
        self.scrolls = []
        self.on_click = None

    def screenshot(self, region):
        self.screens.append(region)
        return region  # OCR and matcher may dispatch by Region

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(x, y)

    def scroll(self, amount, x, y):
        self.scrolls.append((amount, x, y))


class FakeOCR:
    def __init__(self, lines=None):
        self.lines = lines or {}

    def read(self, image):
        value = self.lines.get(image, ())
        if isinstance(value, Exception):
            raise value
        return value


class FakeMatcher:
    def __init__(self, entries=None):
        self.entries = entries or {}
        self.calls = []

    def find_all(self, image, templates, region, threshold):
        self.calls.append((image, tuple(templates)))
        return self.entries.get((image, tuple(templates)), ())


def line(text, box=Region(3, 3, 100, 20)):
    return OCRLine(text, box)


def config(**kwargs):
    data = dict(allowed_groups=frozenset({'测试群', '另一群'}), header_region=HEADER,
                chat_region=CHAT, card_templates=('card-a', 'card-b'),
                open_templates=('open',), close_templates=('close',),
                claimed_templates=('claimed',), card_status_roi=(5, 30, 110, 25),
                popup_region=POPUP, result_region=RESULT, card_wait=0, result_wait=0)
    data.update(kwargs)
    return VisionConfig(**data)


def helper(*, header='测试群', status='微信红包', entries=None, other=None, cfg=None):
    backend = FakeBackend()
    ocr = FakeOCR({HEADER: (line(header),), STATUS: (line(status),),
                   CARD: (line('小王 发来红包'),), POPUP: (line('开'),),
                   RESULT: (line('红包金额 1.00'),)})
    if other:
        ocr.lines.update(other)
    matches = {(CHAT, ('card-a', 'card-b')): (Match(CARD, .94, 'card-a'),),
               (POPUP, ('open',)): (Match(Region(450, 200, 30, 30), .9, 'open'),),
               (POPUP, ('close',)): (Match(Region(580, 120, 20, 20), .96, 'close'),)}
    matches.update(entries or {})
    matcher = FakeMatcher(matches)
    return VisionHelper(cfg or config(), backend, ocr, matcher), backend, ocr, matcher


class VisionTests(unittest.TestCase):
    def test_header_exact_fail_closed(self):
        for name in ('', '测试群(23)', '测试群中的消息', '不在白名单'):
            with self.subTest(name=name):
                vision, backend, _, matcher = helper(header=name)
                self.assertIsNone(vision.verify_group())
                self.assertIsNone(vision.scan_once())
                self.assertEqual([], backend.clicks)
                self.assertEqual([], matcher.calls)
        vision, _, ocr, _ = helper(other={HEADER: OSError('OCR unavailable')})
        self.assertIsNone(vision.verify_group())
        self.assertIsNone(vision.process_one())
        ocr.lines[HEADER] = (line('测试群'), line('另一群', Region(3, 22, 60, 15)))
        self.assertIsNone(vision.verify_group())

    def test_claimed_and_unreadable_never_click(self):
        for status in ('已领取', '已抢完', '已过期', '任意文字', ''):
            with self.subTest(status=status):
                vision, backend, _, _ = helper(status=status)
                self.assertIsNone(vision.process_one())
                self.assertEqual([], backend.clicks)
        vision, backend, ocr, _ = helper()
        ocr.lines[STATUS] = RuntimeError('OCR failure')
        self.assertIsNone(vision.process_one())
        self.assertEqual([], backend.clicks)

    def test_claimed_template_blocks_even_available_ocr(self):
        claimed = Match(Region(360, 196, 65, 15), .92, 'claimed')
        vision, backend, _, _ = helper(entries={(CARD, ('claimed',)): (claimed,)})
        self.assertIsNone(vision.process_one())
        self.assertEqual([], backend.clicks)

    def test_process_result_capture_precedes_close(self):
        vision, backend, ocr, _ = helper()
        before_close = []
        backend.on_click = lambda x, y: before_close.append(RESULT in backend.screens) if (x, y) == (590, 130) else None
        result = vision.process_one()
        self.assertEqual('测试群', result.group)
        self.assertTrue(result.open_clicked and result.close_clicked)
        self.assertEqual((350, 160), (result.card.box.left, result.card.box.top))
        self.assertEqual(('小王 发来红包',), result.card_text)
        self.assertEqual('红包金额 1.00', result.result_lines[0].text)
        self.assertIs(result.result_image, RESULT)
        self.assertEqual([True], before_close)
        self.assertEqual([(410, 192), (465, 215), (590, 130)], backend.clicks)
        self.assertEqual(('测试群', CARD, ('小王 发来红包',)), result.card_key)

    def test_seen_cards_skips_prior_card_and_reserves_before_click(self):
        vision, backend, _, _ = helper()
        seen = set()
        first = vision.process_one(seen_cards=seen)
        self.assertEqual({first.card_key}, seen)
        self.assertIsNone(vision.scan_once(seen_cards=seen))
        self.assertIsNone(vision.process_one(seen_cards=seen))
        self.assertEqual(3, len(backend.clicks))

    def test_seen_card_reserved_when_open_button_missing(self):
        vision, backend, _, matcher = helper()
        matcher.entries[(POPUP, ('open',))] = ()
        seen = set()
        first = vision.process_one(seen_cards=seen)
        self.assertFalse(first.open_clicked)
        self.assertIn(first.card_key, seen)
        self.assertIsNone(vision.process_one(seen_cards=seen))
        self.assertEqual([CARD.center], backend.clicks)

    def test_popup_claimed_prevents_open(self):
        vision, backend, ocr, _ = helper(other={POPUP: (line('已过期'),)})
        result = vision.process_one()
        self.assertEqual('claimed_popup', result.status)
        self.assertFalse(result.open_clicked)
        self.assertEqual([CARD.center], backend.clicks)

    def test_missing_open_button_not_reported_opened(self):
        vision, backend, _, matcher = helper()
        matcher.entries[(POPUP, ('open',))] = ()
        result = vision.process_one()
        self.assertTrue(result.card_clicked)
        self.assertFalse(result.open_clicked)
        self.assertEqual([CARD.center], backend.clicks)

    def test_bounded_scroll_up_and_down_only_chat_region(self):
        cfg = config(history_region=CHAT, max_scroll_attempts=2, scroll_step=4)
        for direction, sign in [('up', 4), ('down', -4)]:
            vision, backend, _, matcher = helper(cfg=cfg)
            matcher.entries[(CHAT, ('card-a', 'card-b'))] = ()
            self.assertIsNone(vision.scan_once(direction=direction))
            self.assertEqual([(sign, *CHAT.center)] * 2, backend.scrolls)
        with self.assertRaises(ValueError):
            vision.scan_once(direction='sideways')
        with self.assertRaises(ValueError):
            config(max_scroll_attempts=1)

    def test_navigation_exact_with_header_reverify_and_region(self):
        cfg = config(list_region=LIST)
        vision, backend, ocr, _ = helper(cfg=cfg,
              other={LIST: (line('另一群', Region(10, 30, 70, 25)),
                            line('另一群123', Region(10, 80, 90, 25)))})
        backend.on_click = lambda x, y: ocr.lines.__setitem__(HEADER, (line('另一群'),))
        self.assertTrue(vision.navigate_to_group('另一群'))
        self.assertEqual([(45, 122)], backend.clicks)
        self.assertFalse(vision.navigate_to_group('其它群'))
        self.assertEqual(1, len(backend.clicks))
        ocr.lines[LIST] = (line('另一群'), line('另一群', Region(5, 40, 70, 20)))
        self.assertFalse(vision.navigate_to_group('另一群'))
        self.assertEqual(1, len(backend.clicks))

    def test_navigation_requires_authorized_start_and_reverify(self):
        vision, backend, ocr, _ = helper(cfg=config(list_region=LIST),
                                         header='未知', other={LIST: (line('另一群'),)})
        self.assertFalse(vision.navigate_to_group('另一群'))
        self.assertEqual([], backend.clicks)
        ocr.lines[HEADER] = (line('测试群'),)
        self.assertFalse(vision.navigate_to_group('另一群'))  # click but no header switch
        self.assertEqual(1, len(backend.clicks))

    def test_outside_card_match_never_probed(self):
        vision, backend, _, matcher = helper(entries={(CHAT, ('card-a', 'card-b')):
                                               (Match(Region(700, 500, 120, 65), .99),)})
        self.assertIsNone(vision.scan_once())
        self.assertNotIn(Region(700, 500, 120, 65), backend.screens)


if __name__ == '__main__':
    unittest.main()
