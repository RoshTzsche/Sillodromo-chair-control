import time
import unittest
from arduino_output import ArduinoOutput, NEUTRAL


class FakeSerial:
    def __init__(self, *args, **kwargs):
        self.writes = []
        self.closed = False
        self.fail = False
        self.partial = False

    def write(self, data):
        if self.fail:
            raise OSError('USB desconectado')
        self.writes.append(data)
        return 1 if self.partial else len(data)

    def close(self):
        self.closed = True


def wait_until(predicate):
    deadline = time.monotonic() + 1.5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Tiempo de espera excedido')
        time.sleep(.005)


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeSerial()
        self.worker = ArduinoOutput('fake', factory=lambda *a, **kw: self.device, startup_delay=0)
        self.worker.start()
        wait_until(lambda: self.worker.info()[0])

    def tearDown(self):
        self.worker.close()
        self.worker.join(1)
        self.assertFalse(self.worker.is_alive())

    def test_protocol_stop_and_close(self):
        self.assertEqual(self.device.writes[0], NEUTRAL)
        self.assertTrue(self.worker.arm())
        self.worker.submit(248, 8, time.monotonic() + .5)
        wait_until(lambda: bytes((248, 8)) in self.device.writes)
        self.worker.stop()
        index = len(self.device.writes)
        self.worker.submit(8, 248, time.monotonic() + .5)
        wait_until(lambda: len(self.device.writes) > index)
        self.assertTrue(all(p == NEUTRAL for p in self.device.writes[index:]))
        self.worker.close()
        self.worker.join(1)
        self.assertEqual(self.device.writes[-1], NEUTRAL)
        self.assertTrue(self.device.closed)

    def test_watchdog_latches_without_ui(self):
        self.worker.arm()
        self.worker.submit(248, 128, time.monotonic() + .06)
        wait_until(lambda: bytes((248, 128)) in self.device.writes)
        wait_until(lambda: not self.worker.info()[1])
        self.assertEqual(self.device.writes[-1], NEUTRAL)
        self.worker.submit(248, 128, time.monotonic() + 1)
        self.assertFalse(self.worker.info()[1])

    def test_usb_failure_disables_and_closes(self):
        self.device.fail = True
        self.worker.arm()
        wait_until(lambda: not self.worker.is_alive())
        self.assertFalse(self.worker.info()[0])
        self.assertFalse(self.worker.arm())
        self.assertTrue(self.device.closed)
        self.assertIn('Error USB', self.worker.info()[2])

    def test_partial_write_disables(self):
        self.device.partial = True
        self.worker.arm()
        wait_until(lambda: not self.worker.is_alive())
        self.assertFalse(self.worker.info()[0])
        self.assertIn('incompleta', self.worker.info()[2])


if __name__ == '__main__':
    unittest.main()
