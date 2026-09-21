#!/usr/bin/env python3
"""Reconocedor OpenFace con cámara real, cinco gestos y dos modos.

Python 3.9+, sólo biblioteca estándar; FeatureExtraction debe estar compilado.
Ejemplo desde ~/airwheel, con el entorno mamba activo:
  python openface-gestures/openface.py --root ~/airwheel --preview --debug

stdout: eventos JSONL; stderr: estado y diagnóstico. Ctrl+C termina el motor.
No envía serial ni invoca Alexa: integrar mediante GestureController.emit.
El consumidor de movimiento necesita watchdog propio y confirmación física de
parada antes de habilitar interacción. STOP aquí es una petición, no telemetría.
Los CSV de algunas compilaciones llegan por lotes: este puente no garantiza
latencia de control ni sustituye un paro físico. No conectar directamente a
motores sin verificar latencia, neutro y protocolo del controlador.

Gestos: cejas sostenidas = modo; mirada arriba/abajo/izquierda/derecha.
Arranca en INTERACCION. Cada orden exige confirmación temporal y volver al
centro. En MOVIMIENTO mantiene una intención mientras se sostiene la mirada;
al soltar, perder seguimiento o datos, emite STOP. HEARTBEAT renueva intención.
Umbrales iniciales heurísticos: ajustar por persona. --invert-x / --invert-y
permiten corregir signos observados; --signal head usa pose en vez de mirada.
MediaPipe puede reutilizar GestureController si entrega x/y en radianes y
brow/blink en escala compatible: sus blendshapes NO son AUs intercambiables.

Documentación:
https://github.com/TadasBaltrusaitis/OpenFace/wiki/Command-line-arguments
https://github.com/TadasBaltrusaitis/OpenFace/wiki/Output-Format
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def log(message):
    print(message, file=sys.stderr, flush=True)


class GestureController:
    def __init__(self, args, emit=None):
        self.a = args
        self.emit = emit or self.print_event
        self.mode = 'INTERACCION'
        self.active = None
        self.armed = False
        self.candidate = None
        self.since = 0.0
        self.last = None
        self.last_heartbeat = 0.0
        self.last_fault = None
        self.samples = []
        self.baseline = None
        self.debug_at = 0.0

    @staticmethod
    def print_event(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def event(self, kind, **fields):
        self.emit(dict(type=kind, mode=self.mode, monotonic=time.monotonic(), **fields))

    def stop(self, reason, force=False):
        if self.active is not None or force:
            self.event('STOP', reason=reason)
        self.active = None

    def fault(self, reason):
        self.stop(reason, force=self.last_fault != reason)
        self.armed = False
        self.candidate = None
        if self.baseline is None:
            self.samples.clear()
        self.last_fault = reason

    def tick(self, now):
        if self.last is not None and now - self.last > self.a.stale:
            self.fault('sin_datos_recientes')

    def feed(self, x, y, brow, blink, valid, now):
        self.tick(now)
        self.last = now
        if not valid or not all(math.isfinite(v) for v in (x, y, brow, blink)):
            self.fault('rostro_no_confiable')
            return
        self.last_fault = None
        if blink > self.a.blink:
            self.fault('ojos_cerrados')
            return
        if self.baseline is None:
            self.samples.append((now, x, y, brow))
            if now - self.samples[0][0] < self.a.calibration or len(self.samples) < 15:
                return
            # Reject unstable calibration instead of learning a moving face.
            xs, ys = [s[1] for s in self.samples], [s[2] for s in self.samples]
            if max(statistics.pstdev(xs), statistics.pstdev(ys)) > self.a.threshold / 3:
                self.samples.clear()
                log('Calibración inestable: mira al centro y relaja la cara.')
                return
            self.baseline = (statistics.median(xs), statistics.median(ys),
                             statistics.median(s[3] for s in self.samples))
            self.samples.clear()
            self.event('CALIBRATED', baseline=self.baseline)
            log('Calibrado. Mantén el centro para habilitar los gestos.')
            return
        x = (x - self.baseline[0]) * (-1 if self.a.invert_x else 1)
        y = (y - self.baseline[1]) * (-1 if self.a.invert_y else 1)
        brow -= self.baseline[2]
        if self.a.debug and now - self.debug_at >= .25:
            log(f'modo={self.mode} x={x:+.3f} y={y:+.3f} cejas={brow:.2f} armado={self.armed}')
            self.debug_at = now
        if brow >= self.a.brow:
            gesture = 'MODO'
        elif abs(x) < self.a.release and abs(y) < self.a.release:
            gesture = 'CENTRO'
        elif abs(x) >= self.a.threshold and abs(x) > 1.3 * abs(y):
            gesture = 'DERECHA' if x > 0 else 'IZQUIERDA'
        elif abs(y) >= self.a.threshold and abs(y) > 1.3 * abs(x):
            gesture = 'ATRAS' if y > 0 else 'ADELANTE'
        else:
            gesture = 'AMBIGUO'
        if self.active is not None and gesture != self.active:
            self.stop('gesto_liberado_o_cambiado')
        if gesture != self.candidate:
            self.candidate, self.since = gesture, now
        elapsed = now - self.since
        if gesture == 'CENTRO':
            if elapsed >= self.a.center_hold:
                self.armed = True
            return
        if gesture == 'AMBIGUO':
            return
        if self.active == gesture:
            if now - self.last_heartbeat >= .1:
                self.event('HEARTBEAT', direction=self.active)
                self.last_heartbeat = now
            return
        required = self.a.mode_hold if gesture == 'MODO' else self.a.hold
        if not self.armed or elapsed < required:
            return
        self.armed = False
        if gesture == 'MODO':
            self.stop('cambio_de_modo', force=True)
            self.mode = 'MOVIMIENTO' if self.mode == 'INTERACCION' else 'INTERACCION'
            self.event('MODE', gesture=gesture)
        elif self.mode == 'MOVIMIENTO':
            self.active = gesture
            self.last_heartbeat = now
            self.event('MOVE', direction=gesture)
        else:
            self.event('INTERACT', direction=gesture)


class CsvTail:
    """Conserva líneas incompletas. Usa sólo el último frame de cada lectura."""
    def __init__(self, path, required):
        self.file = path.open('r', encoding='utf-8-sig')
        self.pending = ''
        self.headers = None
        self.required = set(required)

    def latest(self):
        self.pending += self.file.read(1024 * 1024)
        lines = self.pending.split('\n')
        self.pending = lines.pop()
        latest = None
        for line in lines:
            if not line.strip():
                continue
            values = next(csv.reader([line], skipinitialspace=True))
            if self.headers is None:
                self.headers = [s.strip() for s in values]
                missing = self.required - set(self.headers)
                if missing:
                    raise RuntimeError(f'Faltan columnas en CSV: {sorted(missing)}')
            else:
                latest = dict(zip(self.headers, values)) if len(values) == len(self.headers) else {}
        return latest

    def close(self):
        self.file.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', type=Path, default=Path.home() / 'sillodromo-chair-control')
    p.add_argument('--openface-bin', type=Path)
    p.add_argument('--output-dir', type=Path)
    p.add_argument('--device', type=int, default=0)
    p.add_argument('--preview', action='store_true')
    p.add_argument('--debug', action='store_true')
    p.add_argument('--signal', choices=['gaze', 'head'], default='gaze')
    p.add_argument('--invert-x', action='store_true')
    p.add_argument('--invert-y', action='store_true')
    p.add_argument('--threshold', type=float, default=.22, help='umbral direccional, radianes')
    p.add_argument('--release', type=float, default=.12, help='zona central, radianes')
    p.add_argument('--brow', type=float, default=1.5, help='AU01_r sobre nivel basal')
    p.add_argument('--blink', type=float, default=2.5, help='AU45_r que inhibe órdenes')
    p.add_argument('--hold', type=float, default=.45)
    p.add_argument('--mode-hold', type=float, default=1.2)
    p.add_argument('--center-hold', type=float, default=.3)
    p.add_argument('--calibration', type=float, default=2.0)
    p.add_argument('--confidence', type=float, default=.85)
    p.add_argument('--stale', type=float, default=.5, help='timeout de datos, segundos')
    p.add_argument('--startup-timeout', type=float, default=45)
    return p


def main():
    p = parser()
    a = p.parse_args()
    positives = ('threshold', 'release', 'brow', 'blink', 'hold', 'mode_hold',
                 'center_hold', 'calibration', 'stale', 'startup_timeout')
    if any(not math.isfinite(getattr(a, k)) or getattr(a, k) <= 0 for k in positives):
        p.error('Los umbrales y tiempos deben ser positivos y finitos.')
    if not 0 <= a.confidence <= 1 or a.release >= a.threshold:
        p.error('Requiere confidence entre 0 y 1 y release < threshold.')
    root = a.root.expanduser().resolve()
    binary = (a.openface_bin or root / 'OpenFace/build/bin/FeatureExtraction').expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        p.error(f'No existe un ejecutable en {binary}; especifica --openface-bin.')
    output = (a.output_dir or root / 'openface_output').expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix='live_', dir=output))
    csv_path = session / 'live_tracking.csv'
    logfile = session / 'openface.log'
    command = [str(binary), '-device', str(a.device), '-out_dir', str(session),
               '-of', 'live_tracking', '-gaze', '-pose', '-aus']
    if a.preview:
        command.append('-vis-track')
    xkey, ykey = ('gaze_angle_x', 'gaze_angle_y') if a.signal == 'gaze' else ('pose_Ry', 'pose_Rx')
    required = ['timestamp', 'success', 'confidence', xkey, ykey, 'AU01_r', 'AU45_r']
    controller = GestureController(a)
    process = tail = None
    last_timestamp = None
    first_data_deadline = None
    try:
        with logfile.open('w') as engine_log:
            log(f'Python: {sys.executable}\nMotor: {binary}\nDiagnóstico: {logfile}')
            controller.event('STOP', reason='inicio')
            with subprocess.Popen(command, cwd=binary.parent, stdout=engine_log,
                                  stderr=subprocess.STDOUT) as process:
                start = time.monotonic()
                try:
                    while not csv_path.exists():
                        if process.poll() is not None:
                            raise RuntimeError(f'OpenFace terminó con código {process.returncode}. Ver {logfile}')
                        if time.monotonic() - start > a.startup_timeout:
                            raise RuntimeError(f'OpenFace no creó el CSV. Ver {logfile}')
                        time.sleep(.05)
                    tail = CsvTail(csv_path, required)
                    first_data_deadline = time.monotonic() + a.startup_timeout
                    log('Mira al centro con cejas relajadas durante la calibración. Ctrl+C para salir.')
                    while True:
                        now = time.monotonic()
                        if process.poll() is not None:
                            raise RuntimeError(f'OpenFace terminó con código {process.returncode}. Ver {logfile}')
                        controller.tick(now)
                        row = tail.latest()
                        if row is not None:
                            try:
                                stamp = float(row['timestamp'])
                                vals = [float(row[k]) for k in (xkey, ykey, 'AU01_r', 'AU45_r')]
                                valid = float(row['success']) == 1 and float(row['confidence']) >= a.confidence
                                if not math.isfinite(stamp) or (last_timestamp is not None and stamp <= last_timestamp):
                                    raise ValueError('timestamp no creciente')
                                last_timestamp = stamp
                            except (KeyError, ValueError, OverflowError):
                                controller.fault('fila_invalida')
                            else:
                                controller.feed(*vals, valid, now)
                        if controller.last is None and now > first_data_deadline:
                            raise RuntimeError(f'No llegan frames válidos: revisa {logfile}; posible buffer de CSV.')
                        time.sleep(.01)
                finally:
                    controller.stop('cierre', force=True)
                    if tail is not None:
                        tail.close()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
    except KeyboardInterrupt:
        log('Cámara liberada.')
    except (OSError, RuntimeError) as exc:
        log(f'ERROR: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
