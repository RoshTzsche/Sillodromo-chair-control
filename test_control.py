import unittest
from types import SimpleNamespace
from mova import Intent
from openface import GestureController


class ControlTests(unittest.TestCase):
    def test_stop_stays_latched(self):
        i = Intent()
        i.enabled = True
        i.inputs.add('w')
        self.assertEqual(i.values(1), (248, 128))
        i.stop()
        i.inputs.add('w')
        self.assertEqual(i.values(1), (128, 128))

    def test_alias_release_and_opposites(self):
        i = Intent()
        i.enabled = True
        i.inputs.update(['w', 'up'])
        i.inputs.remove('w')
        self.assertEqual(i.values(1), (248, 128))
        i.inputs.add('s')
        self.assertEqual(i.values(1), (128, 128))

    def test_stale_and_source_isolation(self):
        i = Intent()
        i.enabled, i.source, i.direction, i.last = True, 'Gestos', 'DERECHA', 10
        i.inputs.add('w')
        self.assertEqual(i.values(10.2), (128, 248))
        self.assertEqual(i.values(10.6), (128, 128))
        i.stop()
        i.direction, i.last = 'ADELANTE', 11
        self.assertEqual(i.values(11), (128, 128))

    def test_gesture_hold_center_and_fault(self):
        args = SimpleNamespace(stale=.5, blink=1.8, calibration=2., threshold=.22,
                               release=.12, debug=False, invert_x=False, invert_y=False,
                               brow=.9, center_hold=.3, mode_hold=1.2, hold=.45)
        events = []
        c = GestureController(args, emit=events.append)
        c.baseline, c.mode = (0, 0, 0), 'MOVIMIENTO'
        c.feed(0, 0, 0, 0, True, 0)
        c.feed(0, 0, 0, 0, True, .31)
        c.feed(.3, 0, 0, 0, True, .4)
        c.feed(.3, 0, 0, 0, True, .86)
        self.assertEqual(events[-1]['type'], 'MOVE')
        c.fault('rostro_no_detectado')
        self.assertEqual(events[-1]['type'], 'STOP')
        c.feed(.3, 0, 0, 0, True, .9)
        c.feed(.3, 0, 0, 0, True, 1.2)
        c.feed(.3, 0, 0, 0, True, 1.5)
        self.assertIsNone(c.active)
        self.assertFalse(c.armed)


if __name__ == '__main__':
    unittest.main()
