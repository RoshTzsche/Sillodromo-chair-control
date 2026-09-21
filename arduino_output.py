"""Un único escritor serial, con intención caducable y parada enclavada."""
import threading
import time

NEUTRAL = bytes((128, 128))


class ArduinoOutput(threading.Thread):
    def __init__(self, port, factory=None, startup_delay=2.):
        super().__init__(daemon=True)
        self.port, self.factory, self.startup_delay = port, factory, startup_delay
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.quit = threading.Event()
        self.ready = False
        self.enabled = False
        self.status = 'Conectando…'
        self.packet = NEUTRAL
        self.deadline = 0.
        self.last_sent = None

    def info(self):
        with self.lock:
            return self.ready, self.enabled, self.status, self.last_sent

    def arm(self):
        with self.lock:
            if not self.ready:
                return False
            self.packet = NEUTRAL
            self.deadline = time.monotonic() + .5
            self.enabled = True
            self.status = 'USB conectado · 9600 baudios'
        self.wake.set()
        return True

    def submit(self, vertical, horizontal, deadline):
        packet = bytes(max(0, min(255, int(v))) for v in (vertical, horizontal))
        with self.lock:
            if self.enabled:
                self.packet, self.deadline = packet, deadline
        self.wake.set()

    def stop(self):
        with self.lock:
            self.enabled = False
            self.packet = NEUTRAL
        self.wake.set()

    def close(self):
        self.stop()
        self.quit.set()
        self.wake.set()

    def run(self):
        device = None
        failed = False
        try:
            factory = self.factory
            if factory is None:
                import serial
                factory = serial.Serial
            device = factory(self.port, 9600, timeout=.1, write_timeout=.2)
            if self.quit.wait(self.startup_delay):
                return
            if device.write(NEUTRAL) != 2:
                raise OSError('Escritura serial incompleta')
            with self.lock:
                self.ready = True
                self.last_sent = (128, 128)
                self.status = 'USB conectado · 9600 baudios'
            while not self.quit.is_set():
                self.wake.clear()
                # Serialización: después de stop(), ninguna orden antigua inicia escritura.
                with self.lock:
                    if self.enabled and time.monotonic() > self.deadline:
                        self.enabled = False
                        self.status = 'Orden caducada · vuelve a activar el control'
                    packet = self.packet if self.enabled else NEUTRAL
                    if device.write(packet) != 2:
                        raise OSError('Escritura serial incompleta')
                    self.last_sent = tuple(packet)
                self.wake.wait(.1)
        except Exception as exc:
            failed = True
            with self.lock:
                self.status = f'Error USB: {exc}'
        finally:
            with self.lock:
                self.ready = self.enabled = False
            if device is not None:
                try:
                    device.write(NEUTRAL)
                except Exception:
                    with self.lock:
                        self.status = 'Error USB · no se pudo enviar la parada'
                    failed = True
                finally:
                    try:
                        device.close()
                    except Exception:
                        failed = True
            with self.lock:
                if not failed:
                    self.status = 'USB desconectado'
