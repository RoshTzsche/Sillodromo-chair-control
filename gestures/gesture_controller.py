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

