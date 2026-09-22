"""MOVA: control manual y por gestos con salida Arduino de dos bytes."""
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from gestures.config import defaults, load_config, save_config, validate
from arduino_output import ArduinoOutput

BG, INK, MUTED, LILA, MENTA = '#FDF1F4', '#4A4E69', '#81717D', '#E2D4F0', '#B5E4CA'
KEYS = {'w': 'w', 'up': 'w', 's': 's', 'down': 's', 'a': 'a', 'left': 'a', 'd': 'd', 'right': 'd'}
DIRECTIONS = {'ADELANTE': 'w', 'ATRAS': 's', 'IZQUIERDA': 'a', 'DERECHA': 'd'}


class Intent:
    """Estado independiente de Tk, cámara y hardware; parada enclavada."""
    def __init__(self):
        self.enabled = False
        self.source = 'Manual'
        self.inputs = set()
        self.direction = None
        self.last = 0.

    def stop(self):
        self.enabled = False
        self.inputs.clear()
        self.direction = None

    def values(self, now):
        if not self.enabled:
            return 128, 128
        if self.source == 'Gestos':
            keys = {DIRECTIONS.get(self.direction)} if now - self.last <= .5 else set()
        else:
            keys = {KEYS.get(k, k) for k in self.inputs}
        return (128 + 120 * (('w' in keys) - ('s' in keys)),
                128 + 120 * (('d' in keys) - ('a' in keys)))


class Camera(threading.Thread):
    """Solo publica el frame más reciente; jamás accede a widgets Tk."""
    def __init__(self):
        super().__init__(daemon=True)
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.snapshot = None
        self.quit = threading.Event()

    def publish(self, data):
        with self.lock:
            self.snapshot = data

    def read(self):
        with self.lock:
            return self.snapshot

    def run(self):
        cap = detector = None
        try:
            from gestures import mediapipe_gestures as m
            a = m.parser().parse_args([])
            a.models_dir = Path(__file__).resolve().parent / 'models'
            self.publish({'status': 'Preparando modelo de cámara…'})
            m.ensure_model(a.models_dir / 'face_landmarker.task')
            detector = m.mp.tasks.vision.FaceLandmarker.create_from_options(
                m.mp.tasks.vision.FaceLandmarkerOptions(
                    base_options=m.mp.tasks.BaseOptions(model_asset_path=str(a.models_dir / 'face_landmarker.task')),
                    running_mode=m.mp.tasks.vision.RunningMode.VIDEO, num_faces=1,
                    min_face_detection_confidence=a.min_face_confidence,
                    min_tracking_confidence=a.min_tracking_confidence))
            cap = m.open_camera(0)
            if not cap.isOpened():
                raise RuntimeError('No se pudo abrir la cámara. Revisa los permisos de macOS.')
            generation = 0
            controller = m.GestureController(a, emit=lambda event: self.events.put((generation, event)) if event["type"] == "MOUTH_OPEN" else None)
            start, stamp = time.monotonic(), -1
            while not self.quit.is_set():
                try:
                    while True:
                        generation, settings = self.commands.get_nowait()
                        for key, value in validate(settings).items():
                            setattr(a, key, value)
                        controller = m.GestureController(a, emit=lambda event: self.events.put((generation, event)) if event["type"] == "MOUTH_OPEN" else None)
                except queue.Empty:
                    pass
                ok, frame = cap.read()
                now = time.monotonic()
                if not ok:
                    controller.fault('lectura_camara')
                    self.publish({'status': 'No llegan imágenes de la cámara', 'generation': generation,
                                  'at': now, 'direction': None})
                    self.quit.wait(.05)
                    continue
                frame = m.cv2.flip(frame, 1)
                # Reducir la carga de procesamiento y mantener proporciones.
                h, w = frame.shape[:2]
                if w > 640:
                    frame = m.cv2.resize(frame, (640, round(h * 640 / w)))
                h, w = frame.shape[:2]
                rgb = m.cv2.cvtColor(frame, m.cv2.COLOR_BGR2RGB)
                stamp = max(stamp + 1, int((now - start) * 1000))
                result = detector.detect_for_video(m.mp.Image(image_format=m.mp.ImageFormat.SRGB, data=rgb), stamp)
                # El tiempo corresponde a la captura, no a la finalización de inferencia.
                dx = dy = db = blink = None
                if result.face_landmarks:
                    face = result.face_landmarks[0]
                    pose = m.head_pose(face, w, h)
                    if pose is None:
                        controller.fault('rostro_no_confiable')
                    else:
                        x, y = pose
                        brow, blink = m.brow_metric(face, w, h), m.blink_metric(face, w, h)
                        controller.feed(x, y, brow, blink, True, now, mouth=m.mouth_metric(face, w, h))
                        if controller.baseline is not None:
                            bx, by, bb = controller.baseline
                            dx = (x - bx) * (-1 if a.invert_x else 1)
                            dy = (y - by) * (-1 if a.invert_y else 1)
                            db = brow - bb
                    m.draw_landmarks(frame, face, w, h)
                else:
                    # En esta integración, un frame sin rostro ya cancela la intención.
                    controller.fault('rostro_no_detectado')
                if controller.last_fault:
                    status = {'rostro_no_detectado': 'Rostro no detectado', 'ojos_cerrados': 'Ojos cerrados: vuelve al centro'}.get(controller.last_fault, 'Seguimiento no confiable')
                elif controller.baseline is None:
                    status = 'Calibrando: mira al centro con cejas relajadas'
                elif controller.candidate == 'BOCA':
                    status = 'Sostén la apertura' if controller.mouth_ready else 'Boca registrada o bloqueada: ciérrala para rearmar'
                elif controller.active:
                    status = 'Gesto confirmado: ' + controller.active.lower()
                elif controller.armed:
                    status = 'Listo: dirección, cejas o apertura de boca'
                else:
                    status = 'Vuelve al centro para habilitar el siguiente gesto'
                required = a.mouth_hold if controller.candidate == 'BOCA' else a.center_hold if controller.candidate == 'CENTRO' else a.mode_hold if controller.candidate == 'MODO' else a.hold
                progress = min(100, max(0, (now - controller.since) / required * 100)) if controller.candidate not in (None, 'AMBIGUO') else 0
                m.draw_hud(frame, controller, a, dx, dy, db, blink, now, w, h)
                self.publish(dict(status=status, generation=generation, at=now, direction=controller.active,
                                  mode=controller.mode, progress=progress,
                                  image=m.cv2.cvtColor(frame, m.cv2.COLOR_BGR2RGB)))
        except Exception as exc:
            self.publish({'status': f'Cámara: {type(exc).__name__}: {exc}', 'error': True})
        finally:
            if cap is not None:
                cap.release()
            if detector is not None:
                detector.close()


class Panel:
    def __init__(self, root):
        self.root, self.intent = root, Intent()
        self.worker = None
        self.serial_output = None
        self.generation = 0
        self.timers, self.held, self.blocked = {}, set(), set()
        self.mouse = None
        self.last_image = None
        self.settings_window = None
        self.mouth_count = 0
        config_error = None
        try:
            self.settings = load_config()
        except (OSError, ValueError) as exc:
            self.settings = defaults()
            config_error = str(exc)
        root.title('MOVA | Manual y gestos · Arduino')
        root.geometry('1080x840')
        root.minsize(960, 780)
        root.configure(bg=BG)
        header = tk.Frame(root, bg=LILA, padx=24, pady=16)
        header.pack(fill='x')
        self.label(header, 'MOVA', 25, bg=LILA).pack(side='left')
        self.label(header, 'CONTROL ARDUINO · SALIDA USB', 11, bg=LILA).pack(side='right')
        usb = tk.Frame(root, bg=BG, padx=24, pady=8)
        usb.pack(fill='x')
        self.label(usb, 'Puerto USB:', 11).pack(side='left')
        self.port = tk.StringVar(value='/dev/cu.usbmodem14101')
        self.ports = ttk.Combobox(usb, textvariable=self.port, width=32)
        self.ports.pack(side='left', padx=8)
        self.button(usb, 'Buscar puertos', self.find_ports).pack(side='left', padx=4)
        self.button(usb, 'Conectar', self.connect_usb).pack(side='left', padx=4)
        self.button(usb, 'Desconectar', self.disconnect_usb).pack(side='left', padx=4)
        self.usb_status = self.label(root, 'USB desconectado · no se envían órdenes', 11)
        self.usb_status.pack(fill='x')
        controls = tk.Frame(root, bg=BG, padx=24, pady=14)
        controls.pack(fill='x')
        self.source = tk.StringVar(value='Manual')
        for source in ('Manual', 'Gestos'):
            tk.Radiobutton(controls, text=source, value=source, variable=self.source, command=self.change_source, bg=BG, fg=INK).pack(side='left', padx=8)
        self.button(controls, 'Iniciar cámara', self.start_camera).pack(side='right', padx=6)
        self.button(controls, 'Calibrar', self.calibrate).pack(side='right', padx=6)
        self.button(controls, 'Ajustes de gestos', self.open_settings).pack(side='right', padx=6)
        body = tk.Frame(root, bg=BG)
        body.pack(fill='both', expand=True, padx=24)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg='white', padx=12, pady=12)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
        self.video = self.label(left, 'Inicia la cámara para probar los gestos', 13, bg='white')
        self.video.pack(fill='both', expand=True)
        self.camera_status = self.label(left, 'Cámara apagada', 12, bg='white')
        self.camera_status.configure(wraplength=520)
        self.camera_status.pack(fill='x', pady=8)
        self.progress = ttk.Progressbar(left, maximum=100)
        self.progress.pack(fill='x')
        self.invert_x = tk.BooleanVar(value=self.settings['invert_x'])
        self.invert_y = tk.BooleanVar(value=self.settings['invert_y'])
        for title, var in [('Invertir izquierda / derecha', self.invert_x), ('Invertir adelante / atrás', self.invert_y)]:
            tk.Checkbutton(left, text=title, variable=var, command=self.calibrate, bg='white', fg=INK).pack(anchor='w')
        right = tk.Frame(body, bg='white', padx=20, pady=16)
        right.grid(row=0, column=1, sticky='nsew')
        self.label(right, 'ORDEN SOLICITADA', 12, bg='white').pack()
        self.order = self.label(right, 'NEUTRO', 23, bg='white')
        self.order.pack(pady=12)
        self.mode = self.label(right, 'Control manual', 12, bg='white')
        self.mode.pack()
        self.mouth_status = self.label(right, 'Boca: sin eventos · acción pendiente', 10, bg='white')
        self.mouth_status.pack()
        pad = tk.Frame(right, bg='white')
        pad.pack(pady=18)
        self.buttons = {}
        for key, title, r, c in [('w', '↑\nW', 0, 1), ('a', '←\nA', 1, 0), ('s', '↓\nS', 1, 1), ('d', '→\nD', 1, 2)]:
            b = tk.Label(pad, text=title, font=('Arial', 19, 'bold'), bg=BG, fg=INK, width=4, height=2, relief='ridge', cursor='hand2')
            b.grid(row=r, column=c, padx=3, pady=3)
            b.bind('<ButtonPress-1>', lambda e, k=key: self.mouse_down(k))
            b.bind('<Leave>', self.mouse_up)
            self.buttons[key] = b
        self.duty = self.label(right, 'Vertical: 128\nHorizontal: 128', 13, bg='white')
        self.duty.pack(pady=10)
        self.help = self.label(right, 'Manual: mantén WASD, flechas o botones.\n\nGestos: calibra, vuelve al centro y levanta las cejas 1,2 s para cambiar a Movimiento.', 12, bg='white')
        self.help.configure(wraplength=300, justify='left')
        self.help.pack(pady=10)
        footer = tk.Frame(root, bg=BG, padx=24, pady=16)
        footer.pack(fill='x')
        self.enable_button = self.button(footer, 'Activar control', self.enable)
        self.enable_button.pack(side='left')
        self.state = self.label(footer, 'Pausado', 12)
        self.state.pack(side='left', padx=16)
        stop = self.button(footer, 'DETENER · Esc', self.stop)
        stop.configure(font=('Arial', 17, 'bold'))
        stop.pack(side='right')
        root.bind('<KeyPress>', self.key_down)
        root.bind('<KeyRelease>', self.key_up)
        root.bind('<ButtonRelease-1>', self.mouse_up)
        root.bind('<FocusOut>', lambda e: root.after_idle(self.check_focus))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()
        if config_error:
            root.after_idle(lambda: messagebox.showerror('Configuración no cargada',
                config_error + '\nSe usan los valores predeterminados; el archivo no fue reemplazado.'))

    def open_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        self.stop()
        win = self.settings_window = tk.Toplevel(self.root)
        win.title('Ajustes de gestos · MediaPipe')
        win.transient(self.root)
        win.grab_set()  # El teclado de ajustes no controla el movimiento.
        box = ttk.Frame(win, padding=16)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='Menor umbral = más sensibilidad. Tab cambia de campo; flechas ajustan.').grid(
            row=0, column=0, columnspan=4, sticky='w', pady=(0, 12))
        fields = [
            ('threshold_x', 'Horizontal (rad)', .01), ('threshold_y', 'Vertical (rad)', .01),
            ('release', 'Zona central (rad)', .01), ('diagonal_ratio', 'Dominancia inicial (menor = más ancho)', .05),
            ('sustain_ratio', 'Dominancia sostenida (menor = más tolerancia)', .05),
            ('hold', 'Confirmar dirección (s)', .05), ('center_hold', 'Confirmar centro (s)', .05),
            ('brow', 'Umbral de cejas', .05), ('mode_hold', 'Confirmar cejas (s)', .1),
            ('blink', 'Umbral de ojos cerrados', .1), ('calibration', 'Calibración (s)', .5),
            ('mouth_open', 'Boca: apertura / ancho', .01), ('mouth_close', 'Boca: cierre / ancho', .01),
            ('mouth_hold', 'Confirmar apertura (s)', .05), ('mouth_close_hold', 'Confirmar cierre (s)', .05),
        ]
        variables = {}
        for row, (key, label, step) in enumerate(fields, 1):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky='w', padx=(0, 15), pady=3)
            var = variables[key] = tk.StringVar(value=str(self.settings[key]))
            ttk.Spinbox(box, textvariable=var, from_=step, to=100, increment=step, width=12).grid(
                row=row, column=1, sticky='ew')
        row = len(fields) + 1
        for key, label in [('invert_x', 'Invertir horizontal'), ('invert_y', 'Invertir vertical')]:
            var = variables[key] = tk.BooleanVar(value=getattr(self, key).get())
            ttk.Checkbutton(box, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky='w')
            row += 1
        feedback = ttk.Label(box, text='Aplicar pausa el control y vuelve a calibrar.')
        feedback.grid(row=row, column=0, columnspan=2, pady=8)

        def apply(persist=False):
            try:
                values = validate({key: var.get() if key.startswith('invert_') else float(var.get())
                                   for key, var in variables.items()})
                if persist:
                    save_config(values)
                self.settings = values
                self.invert_x.set(values['invert_x'])
                self.invert_y.set(values['invert_y'])
                self.stop()
                self.reset_camera()
                feedback.configure(text='Guardado y aplicado.' if persist else 'Aplicado; aún no guardado.')
            except (ValueError, OSError, tk.TclError) as exc:
                messagebox.showerror('Ajustes inválidos', str(exc), parent=win)

        def reload_values():
            try:
                values = load_config()
                for key, var in variables.items():
                    var.set(values[key])
                feedback.configure(text='Archivo cargado en los campos; pulsa Aplicar.')
            except (ValueError, OSError) as exc:
                messagebox.showerror('No se pudo cargar', str(exc), parent=win)

        buttons = ttk.Frame(box)
        buttons.grid(row=row+1, column=0, columnspan=2, pady=8)
        for label, command in [('Aplicar', apply), ('Aplicar y guardar', lambda: apply(True)),
                               ('Recargar archivo', reload_values), ('Cerrar', win.destroy)]:
            ttk.Button(buttons, text=label, command=command).pack(side='left', padx=3)
        win.bind('<Escape>', lambda event: win.destroy())

    def label(self, parent, text, size, bg=BG):
        return tk.Label(parent, text=text, font=('Arial', size), bg=bg, fg=INK)

    def button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, fg=INK, highlightbackground=LILA, padx=10, pady=7)

    def stop(self):
        self.blocked.update(self.held)
        self.intent.stop()
        if self.serial_output:
            self.serial_output.stop()
        self.mouse = None
        self.state.configure(text='Pausado · pulsa Activar control')
        self.enable_button.configure(state='normal')

    def enable(self):
        if not self.serial_output or not self.serial_output.arm():
            self.state.configure(text='Conecta el Arduino primero')
            return
        self.blocked.update(self.held)
        self.intent.inputs.clear()
        self.intent.direction = None
        if self.source.get() == 'Gestos':
            self.start_camera()
            self.reset_camera()
        self.intent.enabled = True
        self.state.configure(text='Control activo · salida USB')
        self.enable_button.configure(state='disabled')
        self.root.focus_set()

    def change_source(self):
        self.stop()
        self.intent.source = self.source.get()
        if self.intent.source == 'Gestos':
            self.start_camera()

    def start_camera(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = Camera()
        self.reset_camera()
        self.worker.start()

    def reset_camera(self):
        self.generation += 1
        if self.worker:
            self.settings.update(invert_x=self.invert_x.get(), invert_y=self.invert_y.get())
            self.worker.commands.put((self.generation, self.settings.copy()))

    def calibrate(self):
        self.stop()
        self.start_camera()
        self.reset_camera()

    def key_down(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)):
            if event.keysym.lower() == 'escape':
                self.stop()
            return
        key = event.keysym.lower()
        if key == 'escape':
            self.stop()
        if key not in KEYS:
            return
        if key in self.timers:
            self.root.after_cancel(self.timers.pop(key))
        self.held.add(key)
        if self.intent.enabled and self.intent.source == 'Manual' and key not in self.blocked:
            self.intent.inputs.add(key)

    def key_up(self, event):
        key = event.keysym.lower()
        if key in KEYS:
            if key in self.timers:
                self.root.after_cancel(self.timers.pop(key))
            self.timers[key] = self.root.after(50, lambda: self.release_key(key))

    def release_key(self, key):
        self.timers.pop(key, None)
        self.held.discard(key)
        self.blocked.discard(key)
        self.intent.inputs.discard(key)

    def mouse_down(self, key):
        self.root.focus_set()
        if self.intent.enabled and self.intent.source == 'Manual':
            self.mouse = key

    def mouse_up(self, event=None):
        self.mouse = None

    def check_focus(self):
        if self.root.focus_displayof() is None:
            self.stop()
            self.held.clear()
            self.blocked.clear()
            for timer in self.timers.values():
                self.root.after_cancel(timer)
            self.timers.clear()

    def refresh(self):
        now = time.monotonic()
        if self.serial_output:
            ready, enabled, status, sent = self.serial_output.info()
            suffix = f' · Último envío: {sent[0]}, {sent[1]}' if sent else ''
            self.usb_status.configure(text=status + suffix)
            if self.intent.enabled and (not ready or not enabled):
                self.stop()
        if self.worker:
            try:
                while True:
                    generation, event = self.worker.events.get_nowait()
                    if generation == self.generation and now - event['monotonic'] <= .5:
                        self.mouth_count += 1
                        self.mouth_status.configure(text=f'Boca: {self.mouth_count} apertura(s) · acción pendiente')
            except queue.Empty:
                pass
        data = self.worker.read() if self.worker else None
        if data:
            self.camera_status.configure(text=data['status'])
            fresh = now - data.get('at', 0) <= .5 and data.get('generation') == self.generation
            self.progress['value'] = data.get('progress', 0) if fresh else 0
            if self.intent.source == 'Gestos':
                self.mode.configure(text='Gestos · ' + data.get('mode', 'PREPARANDO'))
                self.intent.direction = data.get('direction') if fresh else None
                self.intent.last = data.get('at', 0)
                if not fresh and data.get('at'):
                    self.camera_status.configure(text='Esperando datos recientes · orden neutra')
            if data.get('image') is not None and data['image'] is not self.last_image:
                try:
                    from PIL import Image, ImageTk
                    img = Image.fromarray(data['image'])
                    img.thumbnail((max(100, self.video.winfo_width()), max(100, self.video.winfo_height())))
                    self.photo = ImageTk.PhotoImage(img)
                    self.video.configure(image=self.photo, text='')
                    self.last_image = data['image']
                except ImportError:
                    self.camera_status.configure(text='Falta Pillow: instala requirements.txt')
        if self.intent.source == 'Manual':
            self.mode.configure(text='Control manual')
        # Ratón y teclado permanecen independientes incluso en la misma dirección.
        original = self.intent.inputs
        self.intent.inputs = original | ({self.mouse} if self.mouse else set())
        v, h = self.intent.values(now)
        self.intent.inputs = original
        if self.serial_output and self.intent.enabled:
            deadline = now + .5
            if self.intent.source == 'Gestos' and (v, h) != (128, 128):
                deadline = min(deadline, self.intent.last + .5)
            self.serial_output.submit(v, h, deadline)
        names = []
        if v != 128:
            names.append('ADELANTE' if v > 128 else 'ATRÁS')
        if h != 128:
            names.append('DERECHA' if h > 128 else 'IZQUIERDA')
        self.order.configure(text=' + '.join(names) or 'NEUTRO', font=('Arial', 16 if len(names) > 1 else 23))
        self.duty.configure(text=f'Vertical: {v}\nHorizontal: {h}')
        active = {'w': v > 128, 's': v < 128, 'a': h < 128, 'd': h > 128}
        for key, button in self.buttons.items():
            button.configure(bg=MENTA if active[key] else BG)
        self.root.after(40, self.refresh)

    def find_ports(self):
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
            self.ports['values'] = ports
            if ports and self.port.get() not in ports:
                self.port.set(ports[0])
            if not ports:
                self.state.configure(text='No se encontraron puertos USB')
        except Exception as exc:
            self.state.configure(text=f'Puertos: {exc}')

    def connect_usb(self):
        self.stop()
        if self.serial_output and self.serial_output.is_alive():
            self.state.configure(text='Desconecta el puerto actual primero')
            return
        port = self.port.get().strip()
        if not port:
            self.state.configure(text='Selecciona un puerto USB')
            return
        self.serial_output = ArduinoOutput(port)
        self.serial_output.start()

    def disconnect_usb(self):
        self.stop()
        if self.serial_output:
            self.serial_output.close()

    def close(self):
        self.stop()
        if self.serial_output:
            self.serial_output.close()
            self.serial_output.join(timeout=.8)
        if self.worker:
            self.worker.quit.set()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    Panel(root)
    root.mainloop()
