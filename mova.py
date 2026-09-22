import copy
import math
import queue
import textwrap
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw, ImageFont, ImageTk

from arduino_output import ArduinoOutput
from control_alexa import AlexaDispatcher
from gestures import mediapipe_gestures as vision
from gestures.config import (
    DEFAULTS,
    parse_settings,
    save_config,
    validate,
)
from gestures.gesture_controller import GestureController


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"

# Orden de los cuatro sectores, en sentido horario.
DIRECTIONS = ("ADELANTE", "DERECHA", "ATRAS", "IZQUIERDA")
KEYS = {
    "w": "ADELANTE", "up": "ADELANTE",
    "d": "DERECHA", "right": "DERECHA",
    "s": "ATRAS", "down": "ATRAS",
    "a": "IZQUIERDA", "left": "IZQUIERDA",
}

# Colores BGR de OpenCV.
DARK = (128, 145, 0)
TEAL = (172, 181, 24)
LIGHT = (222, 231, 166)
WHITE = (255, 255, 255)
INK = (65, 65, 35)


class ArduinoAdapter:
    """Adapta la API existente sin modificar arduino_output.py."""

    def __init__(self, state_getter):
        self.output = None
        self.state_getter = state_getter

    def connect(self, port):
        if self.output is not None and self.output.is_alive():
            raise RuntimeError("Desconecta el puerto actual primero.")

        self.output = ArduinoOutput(port)
        self.output.start()

    def send_move(self, vertical, horizontal, deadline):
        if self.state_getter() != "DRIVE" or self.output is None:
            return

        ready, enabled, _, _ = self.output.info()
        if not ready:
            return
        if not enabled and not self.output.arm():
            return

        self.output.submit(vertical, horizontal, deadline)

    def send_stop(self):
        if self.output is not None:
            self.output.stop()

    def disconnect(self):
        if self.output is not None:
            self.output.close()

    def status(self):
        if self.output is None:
            return "USB desconectado"
        return self.output.info()[2]


class VideoSource(threading.Thread):
    """Un hilo por cámara; publica únicamente el frame más reciente."""

    def __init__(self, index):
        super().__init__(daemon=True, name=f"camera-{index}")
        self.index = index
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self.latest = (0, 0.0, None)
        self.error = ""

    def read(self):
        with self.lock:
            return self.latest

    def close(self):
        self.quit.set()

    def run(self):
        capture = None
        sequence = 0

        try:
            capture = vision.open_camera(self.index)
            if not capture.isOpened():
                raise RuntimeError(
                    f"No se pudo abrir la cámara {self.index}"
                )

            while not self.quit.is_set():
                ok, frame = capture.read()
                sequence += 1

                with self.lock:
                    self.latest = (
                        sequence,
                        time.monotonic(),
                        frame if ok else None,
                    )

                if not ok:
                    self.error = f"Sin imagen de cámara {self.index}"
                    self.quit.wait(.03)
                else:
                    self.error = ""

        except Exception as exc:
            self.error = str(exc)

        finally:
            if capture is not None:
                capture.release()


class FaceWorker(threading.Thread):
    """Procesa exclusivamente la cámara frontal; no accede a Tkinter."""

    def __init__(self, source, args, generation):
        super().__init__(daemon=True, name="face-processing")
        self.source = source
        self.args = copy.deepcopy(args)
        self.generation = generation
        self.state = "MENU"

        self.quit = threading.Event()
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.latest = {}
        self.error = ""

    def set_state(self, generation, state):
        self.commands.put((generation, state))

    def read(self):
        with self.lock:
            return self.latest.copy()

    def close(self):
        self.quit.set()

    def _emit(self, event):
        self.events.put((self.generation, event))

    def run(self):
        detector = None

        try:
            a = self.args
            model = Path(a.models_dir)
            if not model.is_absolute():
                model = ROOT / model
            model = model / "face_landmarker.task"
            vision.ensure_model(model)

            base = vision.mp.tasks.BaseOptions
            delegate = (
                base.Delegate.GPU if a.gpu else base.Delegate.CPU
            )

            try:
                detector = vision._create_face_landmarker(
                    model, a, delegate
                )
            except Exception:
                if not a.gpu:
                    raise
                detector = vision._create_face_landmarker(
                    model, a, base.Delegate.CPU
                )

            controller = GestureController(a, emit=self._emit)
            smoother = vision.EMASmoother(a.smooth_alpha)

            previous_sequence = -1
            timestamp = -1

            while not self.quit.is_set():
                try:
                    while True:
                        self.generation, self.state = (
                            self.commands.get_nowait()
                        )

                        # Reiniciar la selección al cambiar de pantalla.
                        # Se conserva la calibración facial.
                        controller.active = None
                        controller.candidate = None
                        controller.armed = False
                        controller.since = time.monotonic()

                except queue.Empty:
                    pass

                # La UI gobierna el destino del gesto.
                # El algoritmo y a.hold permanecen intactos.
                controller.mode = (
                    "MOVIMIENTO"
                    if self.state == "DRIVE"
                    else "INTERACCION"
                )
                a.mode_hold = (
                    a.interact_page_hold
                    if self.state == "INTERACT"
                    else self.args.mode_hold
                )

                sequence, captured_at, frame = self.source.read()
                if sequence == previous_sequence:
                    self.quit.wait(.005)
                    continue

                previous_sequence = sequence
                now = time.monotonic()
                valid = False
                image = None
                gaze = None

                if frame is None:
                    controller.fault("lectura_camara")
                    smoother.reset()

                else:
                    image = cv2.flip(frame, 1)
                    h, w = image.shape[:2]
                    if w > 640:
                        image = cv2.resize(
                            image, (640, round(h * 640 / w))
                        )
                    h, w = image.shape[:2]

                    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    timestamp = max(
                        timestamp + 1, int(captured_at * 1000)
                    )
                    result = detector.detect_for_video(
                        vision.mp.Image(
                            image_format=vision.mp.ImageFormat.SRGB,
                            data=rgb,
                        ),
                        timestamp,
                    )

                    if not result.face_landmarks:
                        controller.fault("rostro_no_detectado")
                        smoother.reset()
                    else:
                        face = result.face_landmarks[0]
                        pose = vision.head_pose(face, w, h)

                        if pose is None:
                            controller.fault("rostro_no_confiable")
                            smoother.reset()
                        else:
                            x = smoother.apply("x", pose[0])
                            y = smoother.apply("y", pose[1])
                            brow = smoother.apply(
                                "brow", vision.brow_metric(face, w, h)
                            )
                            blink = smoother.apply(
                                "blink", vision.blink_metric(face, w, h)
                            )
                            controller.feed(
                                x, y, brow, blink, True, now,
                                mouth=vision.mouth_metric(face, w, h),
                            )
                            valid = controller.last_fault is None
                            if valid and controller.baseline is not None:
                                bx, by, _ = controller.baseline
                                gaze = (
                                    (x - bx) * (-1 if a.invert_x else 1),
                                    (y - by) * (-1 if a.invert_y else 1),
                                )

                        vision.draw_landmarks(image, face, w, h)

                with self.lock:
                    self.latest = {
                        "generation": self.generation,
                        "at": captured_at,
                        "frame": image,
                        "gaze": gaze,
                        "valid": valid,
                        "calibrated": controller.baseline is not None,
                        "candidate": controller.candidate,
                        "active": controller.active,
                    }

        except Exception as exc:
            self.error = str(exc)
            self._emit({
                "type": "STOP",
                "monotonic": time.monotonic(),
                "reason": str(exc),
            })

        finally:
            if detector is not None:
                detector.close()

class Panel:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.config = validate({
            key: getattr(args, key) for key in DEFAULTS
        })

        self.state = "MENU"
        self.generation = 0
        self.page_index = 0
        self.direction = None
        self.gaze = None
        self.last_move = 0.0
        self.menu_candidate = None
        self.menu_since = None
        self.menu_progress = 0.0
        self.keys = set()
        self.release_jobs = {}
        self.closing = False
        self.restarting = False
        self.editor = None
        self.message = ""

        self.fonts = {}
        self.icons = {}
        self.threads = []
        self.worker = None
        self.front = None
        self.rear = None

        self.arduino_output = ArduinoAdapter(lambda: self.state)
        self.alexa = AlexaDispatcher(self.config)

        root.title("MOVA · Pulse")
        root.resizable(False, False)

        self.video = tk.Label(root, borderwidth=0)
        self.video.pack()
        self.video.bind("<Button-1>", self._image_click)

        self.manual_controls = ttk.Frame(root, padding=6)
        self.manual_controls.pack(fill="x")

        for title, state in (
            ("Menú", "MENU"),
            ("Conducción", "DRIVE"),
            ("Interacción", "INTERACT"),
        ):
            ttk.Button(
                self.manual_controls,
                text=title,
                command=lambda s=state: self.change_state(s),
            ).pack(side="left", padx=3)

        for symbol, direction in zip(
            ("↑", "→", "↓", "←"), DIRECTIONS
        ):
            button = ttk.Button(self.manual_controls, text=symbol)
            button.pack(side="left", padx=3)
            button.bind(
                "<ButtonPress-1>",
                lambda event, d=direction: self._manual_press(d),
            )

        if not args.manual:
            self.manual_controls.pack_forget()
        else:
            root.bind("<KeyPress>", self._key_down)
            root.bind("<KeyRelease>", self._key_up)
            root.bind("<ButtonRelease-1>", self._mouse_release)
            root.bind("<Escape>", lambda event: self.change_state("MENU"))

        self._build_settings()
        self._start_workers()

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _start_workers(self):
        for key, value in self.config.items():
            setattr(self.args, key, copy.deepcopy(value))

        self.front = VideoSource(self.config["cam_front_index"])

        if self.config["cam_rear_index"] == self.config["cam_front_index"]:
            self.rear = self.front
        else:
            self.rear = VideoSource(self.config["cam_rear_index"])

        self.worker = FaceWorker(
            self.front, self.args, self.generation
        )
        self.worker.set_state(self.generation, self.state)

        self.threads = list(dict.fromkeys(
            [self.front, self.rear, self.worker]
        ))
        for thread in self.threads:
            thread.start()

    def _restart_workers(self):
        if self.restarting:
            return

        self.restarting = True
        self.generation += 1
        self._stop_motion()
        self.worker = None

        old_threads = list(self.threads)
        for thread in old_threads:
            thread.close()

        def finish():
            if self.closing:
                return
            if any(thread.is_alive() for thread in old_threads):
                self.root.after(30, finish)
                return

            self._start_workers()
            self.restarting = False

        finish()

    def _stop_motion(self):
        self.direction = None
        self.last_move = 0.0
        self.keys.clear()
        self.arduino_output.send_stop()

    def change_state(self, state):
        if state not in ("MENU", "DRIVE", "INTERACT", "SETTINGS"):
            raise ValueError(f"Estado desconocido: {state}")

        self._stop_motion()
        self.state = state
        self.generation += 1

        self.menu_candidate = None
        self.menu_since = None
        self.menu_progress = 0.0

        if self.worker is not None:
            self.worker.set_state(self.generation, state)

        if self.editor is not None and self.editor.winfo_exists():
            self.editor.destroy()
        self.editor = None

        if state == "SETTINGS":
            self.settings_panel.place(x=100, y=150, width=824, height=350)
        else:
            self.settings_panel.place_forget()

    def _handle_event(self, event):
        kind = event["type"]

        if kind == "MOUTH_OPEN":
            self.change_state("MENU")  # Incluye send_stop().
            return

        # Nombre normalizado en la capa de UI.
        if kind == "MODE":
            kind = "MODE_SWITCH"

        if self.state == "DRIVE":
            if kind == "STOP":
                self.direction = None
                self.arduino_output.send_stop()

            elif kind in ("MOVE", "HEARTBEAT") and not self.keys:
                direction = event.get("direction")
                if direction in DIRECTIONS:
                    self.direction = direction
                    self.last_move = event["monotonic"]

        elif self.state == "INTERACT":
            if kind == "MODE_SWITCH":
                self.page_index = (
                    self.page_index + 1
                ) % self.alexa.page_count

            elif kind == "INTERACT":
                self._execute_direction(event.get("direction"))

    def _execute_direction(self, direction):
        if self.state != "INTERACT" or direction not in DIRECTIONS:
            return

        commands = self.alexa.get_commands_for_page(self.page_index)
        command = commands[DIRECTIONS.index(direction)]
        self.alexa.execute(command)

        if command is not None:
            self.message = "En cola: " + command["phrase"]

    def _update_menu(self, data, fresh):
        candidate = data.get("candidate")
        eligible = (
            fresh
            and data.get("valid")
            and data.get("calibrated")
            and candidate in ("ADELANTE", "ATRAS")
        )

        if not eligible:
            self.menu_candidate = None
            self.menu_since = None
            self.menu_progress = 0.0
            return

        if candidate != self.menu_candidate:
            self.menu_candidate = candidate
            self.menu_since = data["at"]

        # Usar el tiempo de las imágenes evita avanzar el progreso
        # cuando todavía no ha llegado otro frame.
        elapsed = max(0.0, data["at"] - self.menu_since)
        self.menu_progress = min(
            1.0, elapsed / self.config["menu_hold"]
        )

        if self.menu_progress >= 1.0:
            self.change_state(
                "DRIVE" if candidate == "ADELANTE" else "INTERACT"
            )

    def refresh(self):
        if self.closing:
            return

        now = time.monotonic()
        data = self.worker.read() if self.worker is not None else {}

        events = []
        if self.worker is not None:
            try:
                while True:
                    generation, event = self.worker.events.get_nowait()
                    if (
                        generation == self.generation
                        and now - event["monotonic"] <= self.args.stale
                    ):
                        events.append(event)
            except queue.Empty:
                pass

        # La boca tiene prioridad sobre los demás eventos del lote.
        mouth = next(
            (e for e in events if e["type"] == "MOUTH_OPEN"), None
        )
        if mouth is not None:
            self._handle_event(mouth)
        else:
            for event in events:
                self._handle_event(event)

        fresh = (
            data.get("generation") == self.generation
            and now - data.get("at", 0.0) <= self.args.stale
        )

        if self.state == "MENU":
            self._update_menu(data, fresh)

        # Único lugar de la UI que llama a send_move().
        if self.state == "DRIVE":
            if self.keys:
                selected = set(self.keys)
                deadline = now + .5
            elif (
                fresh
                and data.get("valid")
                and self.direction is not None
                and now - self.last_move <= .5
            ):
                selected = {self.direction}
                deadline = min(now + .5, self.last_move + .5)
            else:
                selected = set()
                deadline = now + .5
                self.direction = None

            if selected:
                # Misma conversión que Intent.values() del MOVA original.
                vertical = 128 + 120 * (
                    ("ADELANTE" in selected) - ("ATRAS" in selected)
                )
                horizontal = 128 + 120 * (
                    ("DERECHA" in selected) - ("IZQUIERDA" in selected)
                )
                self.arduino_output.send_move(
                    vertical, horizontal, deadline
                )
            else:
                self.arduino_output.send_stop()

        try:
            while True:
                result = self.alexa.results.get_nowait()
                self.message = (
                    "Reproducido: " if result["ok"] else "Audio: "
                ) + result["message"]
        except queue.Empty:
            pass
        self.gaze = (
            data.get("gaze")
            if (
                data.get("generation") == self.generation
                and now - data.get("at", 0.0) <= self.args.stale
                and data.get("valid")
            )
            else None
        )
        face = data.get("frame")
        candidate = data.get("candidate") if fresh else None

        if self.state == "MENU":
            canvas = self._render_menu(face)
        elif self.state == "DRIVE":
            reversing = (
                "ATRAS" in self.keys
                if self.keys
                else self.direction == "ATRAS"
            )
            canvas = self._render_drive(face, reversing, candidate)
        elif self.state == "INTERACT":
            canvas = self._render_interact(face, candidate)
        else:
            canvas = self._base("Ajustes")

        errors = [
            source.error
            for source in (self.front, self.rear, self.worker)
            if source is not None and source.error
        ]
        status = errors[0] if errors else self.message
        footer = f"{self.arduino_output.status()} · {status}"
        self._text(canvas, footer[:110], (185, 557), size=13)

        self.photo = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        )
        self.video.configure(image=self.photo)
        self.root.after(40, self.refresh)

    def _manual_press(self, direction):
        if not self.args.manual:
            return
        if self.state == "DRIVE":
            self.keys.add(direction)
        elif self.state == "MENU":
            if direction in ("ADELANTE", "ATRAS"):
                self.change_state(
                    "DRIVE" if direction == "ADELANTE" else "INTERACT"
                )
        elif self.state == "INTERACT":
            self._execute_direction(direction)

    def _key_down(self, event):
        if isinstance(
            event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)
        ):
            return

        key = event.keysym.lower()
        if key not in KEYS:
            return

        pending = self.release_jobs.pop(key, None)
        if pending is not None:
            self.root.after_cancel(pending)

        # El autorepeat no vuelve a ejecutar una interacción.
        held = getattr(self, "_held_keys", set())
        if key not in held:
            held.add(key)
            self._held_keys = held
            self._manual_press(KEYS[key])

    def _key_up(self, event):
        key = event.keysym.lower()
        if key not in KEYS:
            return

        pending = self.release_jobs.pop(key, None)
        if pending is not None:
            self.root.after_cancel(pending)

        def release():
            self.release_jobs.pop(key, None)
            held = getattr(self, "_held_keys", set())
            held.discard(key)

            direction = KEYS[key]
            if not any(KEYS.get(k) == direction for k in held):
                self.keys.discard(direction)

        self.release_jobs[key] = self.root.after(50, release)

    def _mouse_release(self, event):
        # Mantener las direcciones que todavía provengan del teclado.
        held = getattr(self, "_held_keys", set())
        self.keys = {KEYS[k] for k in held if k in KEYS}

    def _image_click(self, event):
        x, y = event.x, event.y

        if self.state == "MENU" and 850 <= x <= 1000 and 425 <= y <= 525:
            self.change_state("SETTINGS")
            return

        if not self.args.manual:
            return

        dx, dy = x - 512, y - 325
        distance = math.hypot(dx, dy)
        if not 125 <= distance <= 235:
            return

        if self.state == "MENU":
            self.change_state("DRIVE" if dy < 0 else "INTERACT")

        elif self.state == "INTERACT":
            if abs(dx) > abs(dy):
                direction = "DERECHA" if dx > 0 else "IZQUIERDA"
            else:
                direction = "ATRAS" if dy > 0 else "ADELANTE"
            self._execute_direction(direction)

    def _build_settings(self):
        self.settings_panel = ttk.Frame(self.video, padding=20)
        panel = self.settings_panel
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Puerto USB").grid(
            row=0, column=0, sticky="w", padx=8, pady=12
        )
        self.port = tk.StringVar()
        self.ports = ttk.Combobox(panel, textvariable=self.port)
        self.ports.grid(row=0, column=1, sticky="ew", padx=8)

        actions = ttk.Frame(panel)
        actions.grid(row=1, column=0, columnspan=2, pady=12)

        for title, callback in (
            ("Buscar puertos", self.find_ports),
            ("Conectar", self.connect_usb),
            ("Desconectar", self.arduino_output.disconnect),
            ("Ajuste de gestos", self.open_gesture_settings),
        ):
            ttk.Button(
                actions, text=title, command=callback
            ).pack(side="left", padx=5)

        self.camera_variables = {}
        for row, (key, title) in enumerate((
            ("cam_front_index", "Cámara frontal"),
            ("cam_rear_index", "Cámara trasera"),
        ), start=2):
            ttk.Label(panel, text=title).grid(
                row=row, column=0, sticky="w", padx=8, pady=10
            )
            variable = tk.StringVar(value=str(self.config[key]))
            self.camera_variables[key] = variable
            ttk.Entry(panel, textvariable=variable).grid(
                row=row, column=1, sticky="ew", padx=8
            )

        ttk.Button(
            panel,
            text="Aplicar y guardar cámaras",
            command=self.apply_cameras,
        ).grid(row=4, column=0, columnspan=2, pady=12)

        ttk.Button(
            panel,
            text="Regresar",
            command=lambda: self.change_state("MENU"),
        ).grid(row=5, column=0, columnspan=2, pady=12)

    def find_ports(self):
        try:
            from serial.tools import list_ports

            ports = [port.device for port in list_ports.comports()]
            self.ports["values"] = ports
            if ports and self.port.get() not in ports:
                self.port.set(ports[0])
        except Exception as exc:
            messagebox.showerror("Puertos", str(exc), parent=self.root)

    def connect_usb(self):
        try:
            port = self.port.get().strip()
            if not port:
                raise ValueError("Selecciona un puerto USB.")
            self.arduino_output.connect(port)
        except Exception as exc:
            messagebox.showerror("USB", str(exc), parent=self.root)

    def _apply_config(self, values):
        if self.restarting:
            raise ValueError("Las cámaras todavía se están reiniciando.")

        values = validate(values)
        save_config(values, path=self.args.config)
        self.config = values
        self.message = "Configuración guardada"
        self._restart_workers()

    def apply_cameras(self):
        try:
            values = copy.deepcopy(self.config)
            for key, variable in self.camera_variables.items():
                values[key] = int(variable.get())
            self._apply_config(values)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cámaras", str(exc), parent=self.root)

    def open_gesture_settings(self):
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.lift()
            return

        self.editor = tk.Toplevel(self.root)
        self.editor.title("Ajuste de gestos")
        self.editor.transient(self.root)

        variables = {}
        excluded = {
            "alexa_commands", "cam_front_index", "cam_rear_index", "hold"
        }

        for i, (key, value) in enumerate(
            (item for item in self.config.items() if item[0] not in excluded)
        ):
            row, column = i % 9, (i // 9) * 2
            ttk.Label(self.editor, text=key).grid(
                row=row, column=column, padx=8, pady=6, sticky="w"
            )

            if isinstance(value, bool):
                variable = tk.BooleanVar(value=value)
                widget = ttk.Checkbutton(
                    self.editor, variable=variable
                )
            else:
                variable = tk.StringVar(value=str(value))
                widget = ttk.Entry(
                    self.editor, textvariable=variable, width=10
                )

            variables[key] = variable
            widget.grid(
                row=row, column=column + 1, padx=8, pady=6
            )

        ttk.Label(
            self.editor,
            text=f"Confirmación de gestos: hold = {self.config['hold']} s",
        ).grid(row=10, column=0, columnspan=6, pady=10)

        def apply():
            try:
                values = copy.deepcopy(self.config)
                for key, variable in variables.items():
                    values[key] = (
                        variable.get()
                        if isinstance(self.config[key], bool)
                        else float(variable.get())
                    )
                self._apply_config(values)
                self.editor.destroy()
                self.editor = None
            except (ValueError, OSError, tk.TclError) as exc:
                messagebox.showerror(
                    "Ajustes", str(exc), parent=self.editor
                )

        ttk.Button(
            self.editor, text="Aplicar y guardar", command=apply
        ).grid(row=11, column=0, columnspan=6, pady=12)

    def close(self):
        self.closing = True
        self._stop_motion()
        self.arduino_output.disconnect()
        self.alexa.close()
        for thread in self.threads:
            thread.close()
        self.root.destroy()
    def _text(
        self, canvas, text, position, size=22, color=INK, center=False
    ):
        if size not in self.fonts:
            try:
                self.fonts[size] = ImageFont.truetype(
                    "DejaVuSans.ttf", size
                )
            except OSError:
                self.fonts[size] = ImageFont.load_default()

        image = Image.fromarray(
            cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        )
        draw = ImageDraw.Draw(image)
        font = self.fonts[size]
        x, y = position

        if center:
            box = draw.textbbox((0, 0), text, font=font)
            x -= (box[2] - box[0]) / 2

        draw.text(
            (x, y), text, font=font, fill=tuple(reversed(color))
        )
        canvas[:] = cv2.cvtColor(
            np.asarray(image), cv2.COLOR_RGB2BGR
        )

    def _paragraph(
        self, canvas, text, position, width=18, size=20, color=DARK
    ):
        x, y = position
        for i, line in enumerate(textwrap.wrap(text, width=width)):
            self._text(
                canvas, line, (x, y + i * (size + 5)),
                size=size, color=color, center=True,
            )

    def _icon(self, canvas, filename, box, fallback):
        x, y, width, height = box
        filename = filename or ""

        if filename not in self.icons:
            path = ASSETS / filename
            self.icons[filename] = (
                cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                if filename and path.is_file()
                else None
            )

        icon = self.icons[filename]
        if icon is None:
            cv2.rectangle(
                canvas, (x, y), (x + width, y + height), TEAL, 2
            )
            self._text(
                canvas, fallback, (x + width // 2, y + height // 2 - 9),
                size=16, color=DARK, center=True,
            )
            return

        if icon.ndim == 2:
            icon = cv2.cvtColor(icon, cv2.COLOR_GRAY2BGR)

        scale = min(width / icon.shape[1], height / icon.shape[0])
        resized = cv2.resize(
            icon,
            (
                max(1, round(icon.shape[1] * scale)),
                max(1, round(icon.shape[0] * scale)),
            ),
        )
        h, w = resized.shape[:2]
        left = x + (width - w) // 2
        top = y + (height - h) // 2
        region = canvas[top:top + h, left:left + w]

        if resized.shape[2] == 4:
            alpha = resized[:, :, 3:4].astype(np.float32) / 255
            region[:] = (
                resized[:, :, :3] * alpha + region * (1 - alpha)
            ).astype(np.uint8)
        else:
            region[:] = resized[:, :, :3]

    def _base(self, title):
        canvas = np.full((580, 1024, 3), 255, dtype=np.uint8)
        cv2.rectangle(canvas, (0, 0), (1023, 75), DARK, -1)
        self._text(canvas, title, (35, 23), size=26, color=WHITE)
        self._text(canvas, "Pulse", (810, 20), size=31, color=WHITE)

        ecg = np.array([
            [905, 40], [925, 40], [931, 32], [938, 51],
            [946, 13], [953, 64], [960, 28], [967, 40],
            [974, 35], [980, 40], [1023, 40],
        ], dtype=np.int32)
        cv2.polylines(canvas, [ecg], False, WHITE, 2, cv2.LINE_AA)

        self._text(canvas, "MOVA", (28, 548), size=21, color=INK)
        return canvas

    def _fit(self, frame, width, height):
        h, w = frame.shape[:2]
        scale = max(width / w, height / h)
        resized = cv2.resize(
            frame,
            (
                max(width, math.ceil(w * scale)),
                max(height, math.ceil(h * scale)),
            ),
        )
        x = (resized.shape[1] - width) // 2
        y = (resized.shape[0] - height) // 2
        return resized[y:y + height, x:x + width]

    def _face_circle(self, canvas, frame, center, radius):
        cx, cy = center
        diameter = radius * 2
        x, y = cx - radius, cy - radius

        if frame is None:
            cv2.circle(canvas, center, radius, LIGHT, -1)
            self._text(
                canvas, "Sin imagen", (cx, cy - 10),
                size=20, color=DARK, center=True,
            )
        else:
            crop = self._fit(frame, diameter, diameter)
            mask = np.zeros((diameter, diameter), np.uint8)
            cv2.circle(mask, (radius, radius), radius - 1, 255, -1)
            region = canvas[y:y + diameter, x:x + diameter]
            region[mask > 0] = crop[mask > 0]

        cv2.circle(canvas, center, radius, TEAL, 3, cv2.LINE_AA)
        self._draw_gaze(canvas, center, radius)

    def _ring(self, canvas, center, radius, selected=None):
        # Límites: -135, -45, 45, 135 y 225 grados.
        # Equivalen a cortes en 45°, 135°, 225° y 315°.
        for direction, start in zip(
            DIRECTIONS, (-135, -45, 45, 135)
        ):
            color = DARK if direction == selected else LIGHT
            cv2.ellipse(
                canvas, center, (radius, radius),
                0, start, start + 90, color, -1,
            )

        for angle in (45, 135, 225, 315):
            radians = math.radians(angle)
            point = (
                round(center[0] + radius * math.cos(radians)),
                round(center[1] + radius * math.sin(radians)),
            )
            cv2.line(canvas, center, point, TEAL, 3, cv2.LINE_AA)

        cv2.circle(canvas, center, radius, TEAL, 3, cv2.LINE_AA)
    def _draw_gaze(self, canvas, center, radius):
        if self.gaze is None:
            return

        dx, dy = self.gaze
        if not (math.isfinite(dx) and math.isfinite(dy)):
            return

        cx, cy = center
        limit = radius * 0.80
        threshold_x = self.config["threshold_x"]
        threshold_y = self.config["threshold_y"]

        # Misma escala para ambos ejes: conserva la dirección visual.
        scale = limit / (1.5 * max(threshold_x, threshold_y))
        px, py = dx * scale, dy * scale

        # Mantener el punto dentro del círculo sin cambiar su dirección.
        distance = math.hypot(px, py)
        if distance > limit:
            px *= limit / distance
            py *= limit / distance

        tip = (round(cx + px), round(cy + py))

        centered = (
            abs(dx) < self.config["release"]
            and abs(dy) < self.config["release"]
        )
        color = (80, 230, 80) if centered else (0, 190, 255)

        # Marcas de referencia de los umbrales de cada eje.
        tx = round(threshold_x * scale)
        ty = round(threshold_y * scale)

        for x, y, horizontal in (
            (cx - tx, cy, False),
            (cx + tx, cy, False),
            (cx, cy - ty, True),
            (cx, cy + ty, True),
        ):
            p1 = (x - 4, y) if horizontal else (x, y - 4)
            p2 = (x + 4, y) if horizontal else (x, y + 4)

            cv2.line(canvas, p1, p2, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.line(canvas, p1, p2, WHITE, 2, cv2.LINE_AA)

        # Radio móvil con contorno para contrastar sobre la cámara.
        cv2.line(canvas, center, tip, (0, 0, 0), 5, cv2.LINE_AA)
        cv2.line(canvas, center, tip, color, 2, cv2.LINE_AA)

        # Centro fijo.
        for stroke, thickness in (((0, 0, 0), 4), (WHITE, 2)):
            cv2.drawMarker(
                canvas,
                center,
                stroke,
                cv2.MARKER_CROSS,
                markerSize=14,
                thickness=thickness,
                line_type=cv2.LINE_AA,
            )

        # Posición actual de la cabeza.
        cv2.circle(canvas, tip, 7, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(canvas, tip, 5, color, -1, cv2.LINE_AA)
    def _render_menu(self, face):
        canvas = self._base("Menú Principal")
        center = (512, 325)
        radius = 225

        cv2.ellipse(
            canvas, center, (radius, radius), 0, 180, 360, DARK, -1
        )
        cv2.ellipse(
            canvas, center, (radius, radius), 0, 0, 180, LIGHT, -1
        )
        cv2.circle(canvas, center, radius, TEAL, 3, cv2.LINE_AA)

        self._icon(
            canvas, "steering.png", (465, 120, 94, 70), "Conducir"
        )
        self._icon(
            canvas, "speaker.png", (465, 465, 94, 65), "Interactuar"
        )
        self._face_circle(canvas, face, center, 128)

        if self.menu_progress > 0:
            cv2.ellipse(
                canvas, center, (radius + 5, radius + 5),
                -90, 0, 360 * self.menu_progress,
                TEAL, 7, cv2.LINE_AA,
            )

        cv2.rectangle(canvas, (18, 160), (230, 355), TEAL, -1)
        cv2.fillPoly(
            canvas,
            [np.array([[48, 355], [48, 382], [80, 355]])],
            TEAL,
        )
        seconds = f"{self.config['menu_hold']:g}"
        self._paragraph(
            canvas,
            "Mueve tu cabeza hacia la opción que deseas "
            f"y mantén {seconds} segundos",
            (124, 178), width=17, size=20, color=WHITE,
        )

        self._icon(
            canvas, "stem_udlap.png",
            (855, 150, 130, 170), "STEM UDLAP",
        )
        cv2.rectangle(canvas, (850, 425), (1000, 525), LIGHT, -1)
        self._icon(
            canvas, "settings.png",
            (870, 437, 110, 75), "Ajustes",
        )

        return canvas

    def _render_drive(self, face, reversing, candidate):
        canvas = self._base(
            "Cámara trasera" if reversing else "Cámara delantera"
        )
        center = (265, 325)
        radius = 195

        selected = (
            next(iter(self.keys), None) if self.keys else self.direction
        )
        self._ring(canvas, center, radius, selected or candidate)

        vectors = ((0, -1), (1, 0), (0, 1), (-1, 0))
        for direction, (dx, dy) in zip(DIRECTIONS, vectors):
            start = (265 + dx * 123, 325 + dy * 123)
            end = (265 + dx * 182, 325 + dy * 182)
            color = WHITE if direction == selected else TEAL
            cv2.arrowedLine(
                canvas, start, end, color,
                thickness=18, line_type=cv2.LINE_AA, tipLength=.48,
            )

        self._face_circle(canvas, face, center, 108)

        x, y, width, height = 532, 76, 492, 468
        cv2.rectangle(
            canvas, (x, y), (x + width - 1, y + height - 1), LIGHT, -1
        )

        if reversing and self.rear is not None:
            _, captured_at, rear_frame = self.rear.read()
            if (
                rear_frame is not None
                and time.monotonic() - captured_at <= self.args.stale
            ):
                canvas[y:y + height, x:x + width] = self._fit(
                    rear_frame, width, height
                )
            else:
                self._text(
                    canvas, "Esperando cámara trasera",
                    (778, 290), size=22, center=True,
                )
        else:
            self._paragraph(
                canvas,
                "La cámara trasera aparece al retroceder",
                (778, 285), width=25, size=23,
            )

        return canvas

    def _render_interact(self, face, candidate):
        canvas = self._base(
            f"Colección de interacciones {self.page_index + 1}"
        )
        center = (512, 325)

        self._ring(canvas, center, 210, candidate)
        self._face_circle(canvas, face, center, 105)

        # Correspondencia exacta:
        # arriba -> 0, derecha -> 1, abajo -> 2, izquierda -> 3.
        positions = (
            (512, 155), (835, 285), (512, 465), (185, 285)
        )
        commands = self.alexa.get_commands_for_page(self.page_index)

        for command, (x, y) in zip(commands, positions):
            if command is None:
                self._text(
                    canvas, "—", (x, y), size=24, center=True
                )
                continue

            self._icon(
                canvas, command["icon"],
                (x - 20, y - 43, 40, 35), "Audio",
            )
            self._paragraph(
                canvas, command["phrase"],
                (x, y), width=22, size=19,
            )

        return canvas


if __name__ == "__main__":
    parser = vision.parser()
    args = parse_settings(parser, "mediapipe")

    root = tk.Tk()
    Panel(root, args)
    root.mainloop()