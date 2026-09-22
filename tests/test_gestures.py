from pathlib import Path
import tempfile
import unittest

from gestures.config import defaults, load_config, save_config, validate, parse_settings
from gestures.openface import GestureController, parser


class ControllerTests(unittest.TestCase):
    def setUp(self):
        args = parser().parse_args([])
        for k, v in defaults().items():
            setattr(args, k, v)
        self.events = []
        self.c = GestureController(args, self.events.append)
        self.c.baseline = (0., 0., 0.)
        self.c.mode = 'MOVIMIENTO'
        self.now = 0.

    def feed(self, x=0, y=0, mouth=0, blink=0, valid=True, duration=.55, brow=0):
        for _ in range(round(duration / .05)):
            self.now += .05
            self.c.feed(x, y, brow, blink, valid, self.now, mouth=mouth)

    def events_of(self, name):
        return [event for event in self.events if event['type'] == name]

    def test_independent_thresholds(self):
        self.feed()
        self.feed(x=.18)
        self.assertIsNone(self.c.active)
        self.feed(y=-.18)
        self.assertEqual(self.c.active, 'ADELANTE')

    def test_hysteresis_and_wider_sustained_cone(self):
        self.feed()
        self.feed(x=.24, y=.18)
        self.assertEqual(self.c.active, 'DERECHA')
        self.feed(x=.18, y=.22)
        self.assertEqual(self.c.active, 'DERECHA')
        self.feed(x=.11, duration=.05)
        self.assertIsNone(self.c.active)
        self.feed(x=.24)
        self.assertIsNone(self.c.active)  # requiere centro de nuevo
        self.feed()
        self.feed(x=.24)
        self.assertEqual(self.c.active, 'DERECHA')

    def test_reverse_stops_without_immediate_reversal(self):
        self.feed()
        self.feed(x=.25)
        self.feed(x=-.25)
        self.assertIsNone(self.c.active)

    def test_mouth_once_and_confirmed_close(self):
        self.feed()
        self.feed(mouth=.5, duration=2)
        self.assertEqual(len(self.events_of('MOUTH_OPEN')), 1)
        self.feed(mouth=.1, duration=.05)
        self.feed(mouth=.5)
        self.assertEqual(len(self.events_of('MOUTH_OPEN')), 1)
        self.feed(mouth=.1)
        self.feed(mouth=.5)
        self.assertEqual(len(self.events_of('MOUTH_OPEN')), 2)

    def test_mouth_hold_rejects_brief_opening(self):
        self.feed()
        self.feed(mouth=.5, duration=.2)
        self.feed(mouth=.25, duration=.05)
        self.feed(mouth=.5, duration=.2)
        self.assertFalse(self.events_of('MOUTH_OPEN'))

    def test_open_mouth_at_start_needs_close(self):
        self.feed(mouth=.5, duration=2)
        self.assertFalse(self.events_of('MOUTH_OPEN'))

    def test_fault_does_not_rearm_open_mouth(self):
        self.feed()
        self.feed(mouth=.5)
        self.feed(valid=False, duration=.05)
        self.feed(mouth=.5, duration=2)
        self.assertEqual(len(self.events_of('MOUTH_OPEN')), 1)

    def test_mouth_cancels_motion_and_requires_center(self):
        self.feed()
        self.feed(x=.25)
        self.feed(x=.25, mouth=.5)
        self.assertIsNone(self.c.active)
        self.assertEqual(len(self.events_of('MOUTH_OPEN')), 1)
        self.feed(x=.25)
        self.assertIsNone(self.c.active)

    def test_blink_face_loss_and_timeout_stop(self):
        for case in ('blink', 'invalid', 'timeout'):
            with self.subTest(case=case):
                self.feed()
                self.feed(x=.25)
                self.assertEqual(self.c.active, 'DERECHA')
                if case == 'blink':
                    self.feed(blink=3, duration=.05)
                elif case == 'invalid':
                    self.feed(valid=False, duration=.05)
                else:
                    self.now += .6
                    self.c.tick(self.now)
                self.assertIsNone(self.c.active)
                self.assertFalse(self.c.armed)

    def test_calibration_uses_medians_and_rejects_motion(self):
        self.c.baseline = None
        self.feed(x=.1, y=-.03, duration=2.2)
        self.assertEqual(self.c.baseline, (.1, -.03, 0))
        self.c.baseline = None
        for i in range(45):
            self.feed(x=.3 if i % 2 else -.3, duration=.05)
        self.assertIsNone(self.c.baseline)

    def test_brows_switch_once_until_center(self):
        self.feed()
        self.feed(brow=2, duration=3)
        self.assertEqual(len(self.events_of('MODE')), 1)


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_separate_detector_scales(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            settings = defaults()
            settings['threshold_y'] = .18
            save_config(settings, path)
            save_config(defaults('openface'), path, 'openface')
            self.assertEqual(load_config(path), settings)
            self.assertEqual(load_config(path, 'openface')['mouth_open'], 2.)
            self.assertEqual(list(Path(d).glob('*.tmp')), [])

    def test_invalid_values(self):
        for values in ({'threshold_y': .1}, {'release': .22}, {'mouth_close': .4},
                       {'hold': float('nan')}, {'hold': True}, {'invert_x': 1},
                       {'diagonal_ratio': .8}, {'sustain_ratio': 2}, {'unknown': 1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate(values)

    def test_corrupt_file_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                save_config(defaults(), path)
            self.assertEqual(path.read_text(), '{broken')

    def test_cli_precedence_and_legacy_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'config.json'
            save_config(dict(defaults('openface'), threshold_y=.19), path, 'openface')
            a = parse_settings(parser(), 'openface', ['--config', str(path), '--threshold-x', '.25'])
            self.assertEqual((a.threshold_x, a.threshold_y), (.25, .19))
            a = parse_settings(parser(), 'openface', ['--config', str(path), '--threshold', '.3', '--threshold-y', '.18'])
            self.assertEqual((a.threshold_x, a.threshold_y), (.3, .18))


if __name__ == '__main__':
    unittest.main()
