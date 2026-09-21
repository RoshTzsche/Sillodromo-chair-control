import sys
import os
import shutil
import threading
import time # Añadido para optimización del CameraThread
import traceback
import pyttsx3

# Módulos externos
try:
    import pyttsx3
except ImportError:
    pyttsx3 = None
    print("ADVERTENCIA: pyttsx3 no encontrado. La función TTS no estara disponible.")

try:
    import serial
except ImportError:
    serial = None
    print("ADVERTENCIA: pyserial no encontrado. La conexión con Arduino no estara disponible.")

try:
    import cv2
    from ultralytics import YOLO
except ImportError:
    cv2 = None
    YOLO = None
    print("ADVERTENCIA: OpenCV o Ultralytics (YOLO) no encontrados. La detección de camara no estara disponible.")

# Módulos PyQt5 
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QVBoxLayout, QHBoxLayout, 
    QStackedWidget, QGridLayout, QDialog, QMessageBox
)
from PyQt5.QtGui import QFont, QPalette, QLinearGradient, QColor, QBrush, QImage, QPixmap
from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal, QObject

# CONFIGURACIÓN GLOBAL SILLA
PUERTO_SERIAL = 'COM5'
BAUD_RATE = 9600
POSICION_NEUTRA = 128 # Valor central para el servo (asumiendo 0-255)
VALOR_MAXIMO = 255
VALOR_MINIMO = 0
PASO_MOVIMIENTO = 15 # Incremento base para el control


class ControladorArduino:
    """Clase para manejar la comunicación serial y el estado de movimiento."""
    def __init__(self, puerto, baudrate):
        self.ver_duty = POSICION_NEUTRA
        self.hor_duty = POSICION_NEUTRA
        self.teclas_presionadas = set()
        self.arduino = None
        self.conectado = False

        if serial:
            try:
                self.arduino = serial.Serial(puerto, baudrate, timeout=1)
                self.conectado = True
                print(f"Arduino conectado en {puerto}")
                # Esperar un momento para la inicialización del Arduino
                time.sleep(2) 
                self.enviar_datos() # Enviar posición neutra inicial
            except serial.SerialException as e:
                print(f"ERROR: No se pudo abrir el puerto {puerto}. Detalles: {e}")
        else:
            print("INFO: Módulo serial no disponible. El control de movimiento no funcionará.")

    def enviar_datos(self):
        """Envía los valores de duty cycle actuales al Arduino."""
        if self.conectado and self.arduino.is_open:
            try:
                # Asegurar que los valores estén dentro del rango 0-255
                ver_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.ver_duty)))
                hor_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.hor_duty)))

                # El protocolo espera dos bytes: vertical y horizontal
                self.arduino.write(bytes([ver_enviar, hor_enviar]))
                # print(f"Enviando -> Vertical: {ver_enviar}, Horizontal: {hor_enviar}") # Descomentar para debug
            except serial.SerialException as e:
                print(f"Error al escribir al puerto serial: {e}")
            except Exception as e:
                print(f"Error inesperado al enviar datos: {e}")
        # else:
            # print("Arduino no conectado o puerto cerrado.")

    def actualizar_movimiento(self):
        """Calcula los nuevos duty cycles basados en las teclas presionadas."""
        mov_vertical = 0
        mov_horizontal = 0

        # Mapeo de teclas a dirección
        if 'w' in self.teclas_presionadas:
            mov_vertical += 1 # Arriba/Adelante
        if 's' in self.teclas_presionadas:
            mov_vertical -= 1 # Abajo/Atrás
        if 'a' in self.teclas_presionadas:
            mov_horizontal += 1 # Izquierda
        if 'd' in self.teclas_presionadas:
            mov_horizontal -= 1 # Derecha

        # Se calcula la posición objetivo (Full Speed o Neutro)
        # 128 (Neutral) +/- 120 (15 * 8) = 8 o 248 (Casi el rango completo)
        self.ver_duty = POSICION_NEUTRA + mov_vertical * PASO_MOVIMIENTO * 8
        self.hor_duty = POSICION_NEUTRA + mov_horizontal * PASO_MOVIMIENTO * 8

        self.enviar_datos()

    def presionar(self, tecla):
        """Registra la pulsación de una tecla y actualiza el movimiento."""
        if tecla not in self.teclas_presionadas:
            self.teclas_presionadas.add(tecla)
            self.actualizar_movimiento()

    def soltar(self, tecla):
        """Registra la liberación de una tecla y actualiza el movimiento."""
        if tecla in self.teclas_presionadas:
            self.teclas_presionadas.remove(tecla)
            self.actualizar_movimiento()

    def cerrar_conexion(self):
        """Cierra la conexión serial y envía la posición neutra."""
        if self.conectado and self.arduino and self.arduino.is_open:
            try:
                # Enviar posición neutra final para detener el movimiento
                self.arduino.write(bytes([POSICION_NEUTRA, POSICION_NEUTRA]))
                time.sleep(0.1)
                self.arduino.close()
                print("Conexión con Arduino cerrada.")
            except Exception as e:
                print(f"Error al cerrar la conexión serial: {e}")

class CameraThread(QObject):
    """Hilo para capturar video, ejecutar YOLO y emitir el frame procesado."""
    changePixmap = pyqtSignal(cv2.Mat) # Señal que envia el frame de OpenCV

    def __init__(self):
        super().__init__()
        self.cap = None
        self.model = None
        self.running = False
        
        if cv2 and YOLO:
            # Cargar YOLO 
            try:
                self.model = YOLO("yolov8n.pt")
                print("Modelo YOLOv8n cargado exitosamente.")
            except Exception as e:
                print(f"Error al cargar modelo YOLO: {e}")
                self.model = None
                return

            # Intentar abrir la camara
            for i in range(2): 
                try:
                    test = cv2.VideoCapture(i)
                    if test.isOpened():
                        self.cap = test
                        print(f"Cámara detectada en índice {i}")
                        break
                    else:
                        test.release()
                except Exception:
                    pass

            if self.cap is None:
                print("ERROR: No se pudo abrir ninguna cámara. Verifique si otra aplicación la está usando.")
            
            # Detectar solo personas
            self.objetos_a_detectar = ["person"]
            self.clases_a_detectar_ids = [
                k for k, v in self.model.names.items()
                if v in self.objetos_a_detectar
            ]
        else:
            print("INFO: Módulos de cámara (cv2/YOLO) no disponibles.")

    def start(self):
        """Inicia la captura de video en un hilo separado."""
        if not self.running and self.cap and self.model:
            self.running = True
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def stop(self):
        """Detiene la captura de video y libera los recursos."""
        self.running = False
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass

    def run(self):
        """Bucle principal de captura y procesamiento."""
        if self.cap is None:
            return
        
        while self.running and self.cap.isOpened():
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.01) # Pequeña pausa si falla la lectura
                continue
            
            alto, ancho, _ = frame.shape
            area_frame = alto * ancho

            # Ejecutar deteccion. Se añade 'conf=0.4' para reducir el umbral de confianza y detectar objetos mas facil.
            # Puedes ajustar este valor entre 0.0 y 1.0 (ej. 0.7 es alto, 0.2 es bajo).
            results = self.model(frame, classes=self.clases_a_detectar_ids, conf=0.4, verbose=False)

            for r in results:
                # Iterar sobre las detecciones de personas
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    w = x2 - x1
                    h = y2 - y1
                    area_box = w * h
                    porcentaje = (area_box / area_frame) * 100

                    # Nivel de peligro basado en el tamaño de la persona en el frame
                    if porcentaje >= 50:
                        mensaje = "ADVERTENCIA: CONTACTO INMINENTE"
                        color = (0, 0, 255) # Rojo
                    elif porcentaje >= 30:
                        mensaje = "PELIGRO: OBJETO MUY CERCA"
                        color = (0, 165, 255) # Naranja
                    elif porcentaje >= 15:
                        mensaje = "ATENCIÓN: OBJETO CERCA"
                        color = (0, 255, 255) # Amarillo
                    else:
                        mensaje = "Objeto detectado"
                        color = (0, 255, 0) # Verde

                    # Dibujar bounding box y texto
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, mensaje, (x1, y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            # Emitir el frame procesado a la GUI
            self.changePixmap.emit(frame)
            
            # Pausa para reducir la carga de CPU
            time.sleep(0.03)


class VentanaControl(QWidget):
    """Ventana para el control de movimiento manual con cámara y detección."""
    def __init__(self, controlador, parent):
        super().__init__()
        self.controlador = controlador
        self.parent = parent
        self.camera_thread = None
        self.setFocusPolicy(Qt.StrongFocus) # Permite recibir eventos de teclado

        self.setWindowTitle("Control de Silla de Ruedas")
        # El tamaño fijo facilita el diseño, pero es recomendable usar layouts flexibles
        # self.setFixedSize(900, 820) 
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # Título
        title = QLabel("Control de Movimiento")
        title.setFont(QFont("Arial", 28, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #1565C0; margin-bottom: 10px;")
        main_layout.addWidget(title)

        
        # CÁMARA CENTRADA
        
        self.camera_label = QLabel("Cargando Cámara...")
        self.camera_label.setMinimumSize(600, 400) # Tamaño minimo flexible
        self.camera_label.setAlignment(Qt.AlignCenter)
        self.camera_label.setStyleSheet("""
            QLabel {
                border: 4px solid #1565C0;
                border-radius: 15px;
                background-color: black;
                color: white;
                font-size: 20px;
            }
        """)

        main_layout.addWidget(self.camera_label, alignment=Qt.AlignCenter)
        main_layout.addSpacing(30)

        
        # BOTONES DE CONTROL (W-A-S-D)
        
        control_group_layout = QHBoxLayout()
        control_group_layout.setAlignment(Qt.AlignCenter)

        grid = QGridLayout()
        grid.setSpacing(15)
        
        # Etiqueta de INFO para el control
        info_label = QLabel("Comandos")
        info_label.setFont(QFont("Arial", 16, QFont.Bold))
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setStyleSheet("color: #333; padding-bottom: 15px;")
        grid.addWidget(info_label, 1, 1, alignment=Qt.AlignCenter)


        botones = {
            "btn_up":    ("▲", 0, 1, "w"),
            "btn_left":  ("◄", 1, 0, "a"),
            "btn_right": ("►", 1, 2, "d"),
            "btn_down":  ("▼", 2, 1, "s")
        }

        button_style = """
            QPushButton {
                font-size: 40px;
                font-weight: bold;
                background: #E3F2FD;
                color: #1565C0;
                border-radius: 15px;
                border: 3px solid #1565C0;
            }
            QPushButton:pressed {
                background: #BBDEFB;
                border: 3px solid #0D47A1;
            }
        """

        for name, (text, r, c, key) in botones.items():
            btn = QPushButton(text)
            btn.setFixedSize(90, 90)
            btn.setStyleSheet(button_style)
            grid.addWidget(btn, r, c, alignment=Qt.AlignCenter)

            btn.pressed.connect(lambda k=key: self.controlador.presionar(k))
            btn.released.connect(lambda k=key: self.controlador.soltar(k))

        control_group_layout.addLayout(grid)
        main_layout.addLayout(control_group_layout)

        
        # BOTÓN VOLVER
        
        back_btn = QPushButton("Volver al Menú")
        back_btn.setFixedSize(280, 50)
        back_btn.setFont(QFont("Arial", 18, QFont.Bold))
        back_btn.setStyleSheet("""
            QPushButton {
                background: #FF5252;
                color: white;
                border-radius: 10px;
                padding: 8px;
            }
            QPushButton:hover {
                background: #F44336;
            }
        """)
        back_btn.clicked.connect(self.volver_menu)

        main_layout.addWidget(back_btn, alignment=Qt.AlignCenter)
        main_layout.addStretch(1) # Relleno para centrado vertical

        self.setLayout(main_layout)

    # Eventos de Teclado para control W-A-S-D 
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_W:
            self.controlador.presionar('w')
        elif key == Qt.Key_S:
            self.controlador.presionar('s')
        elif key == Qt.Key_A:
            self.controlador.presionar('a')
        elif key == Qt.Key_D:
            self.controlador.presionar('d')
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        if key == Qt.Key_W:
            self.controlador.soltar('w')
        elif key == Qt.Key_S:
            self.controlador.soltar('s')
        elif key == Qt.Key_A:
            self.controlador.soltar('a')
        elif key == Qt.Key_D:
            self.controlador.soltar('d')
        super().keyReleaseEvent(event)

    # Control de Camara 
    def showEvent(self, event):
        """Se ejecuta cuando la ventana se hace visible."""
        # Se requiere cv2, YOLO y Arduino conectado para iniciar el modulo de movimiento asistido.
        if cv2 and YOLO and self.controlador.conectado:
            self.iniciar_camara()
        else:
            if not self.controlador.conectado:
                 self.camera_label.setText("ERROR: Arduino no conectado. Verifique puerto serial.")
            elif not (cv2 and YOLO):
                 self.camera_label.setText("ERROR: Módulos de cámara (cv2/YOLO) no disponibles.")
            else:
                 self.camera_label.setText("Cámara/YOLO o Arduino no disponibles.")
        super().showEvent(event)

    def hideEvent(self, event):
        """Se ejecuta cuando la ventana se oculta."""
        self.detener_camara()
        super().hideEvent(event)

    def iniciar_camara(self):
        """Inicializa el hilo de la cámara solo si no está activo."""
        if self.camera_thread is None:
            self.camera_thread = CameraThread()
            self.camera_thread.changePixmap.connect(self.update_camera_frame)
            self.camera_thread.start()
            self.camera_label.setText("Cámara Activa (Esperando Detección)")

    def detener_camara(self):
        """Detiene el hilo de la cámara y libera recursos."""
        if self.camera_thread:
            self.camera_thread.stop()
            # Esperar a que el hilo termine si es posible, aunque daemon=True ayuda
            # self.camera_thread.thread.join() 
            self.camera_thread = None
            self.camera_label.setText("Cámara Detenida")

    def update_camera_frame(self, frame):
        """Convierte el frame de OpenCV a QPixmap y lo muestra."""
        if frame is None:
            return

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_line = ch * w
        
        # Crear QImage
        img = QImage(frame_rgb.data, w, h, bytes_line, QImage.Format_RGB888)
        
        # Escalar la imagen para ajustarse al QLabel
        img = img.scaled(self.camera_label.size(),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.camera_label.setPixmap(QPixmap.fromImage(img))

    def volver_menu(self):
        self.controlador.soltar('w')
        self.controlador.soltar('s')
        self.controlador.soltar('a')
        self.controlador.soltar('d')
        self.detener_camara()
        self.parent.stack.setCurrentWidget(self.parent.menu_widget)


class ComunicacionWindow(QWidget):
    """Ventana para la comunicación basada en bloques y TTS."""
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.selected_text = ""
        
        # Inicializacion segura de TTS
        self.engine = None
        if pyttsx3:
            try:
                self.engine = pyttsx3.init()
            except Exception as e:
                print(f"Error al inicializar TTS: {e}")

        # Banco de palabras
        self.word_bank = [
            "hola", "adiós", "gracias", "por favor", "sí", "no",
            "ayuda", "comer", "beber", "feliz", "triste", "mamá",
            "papá", "quiero", "dormir", "caminar", "jugar",
            "baño", "hablar", "escuchar", "ver", "ir", "venir",
            "buenos", "días", "tarde", "noche", "cómo", "estás",
            "médico", "duele", "agua", "sentarme"
        ]

        self.suggestion_labels = []
        self.init_ui()
        self.update_suggestions() # Inicializar sugerencias

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignCenter)
        main_layout.setContentsMargins(50, 30, 50, 30)
        main_layout.setSpacing(25)

        # Titulo
        title = QLabel("Tablero de Comunicación")
        title.setFont(QFont("Arial", 28, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #0D47A1;")
        main_layout.addWidget(title)

        # Area de texto
        self.display = QLabel("")
        self.display.setFont(QFont("Arial", 28))
        self.display.setWordWrap(True)
        self.display.setStyleSheet("""
            QLabel {
                background-color: white;
                border: 4px solid #0D47A1;
                border-radius: 15px;
                padding: 15px;
                min-height: 80px;
                max-height: 100px;
            }
        """)
        self.display.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        main_layout.addWidget(self.display)

        # Sugerencias (con fila de 3 botones)
        self.suggestion_area = QHBoxLayout()
        self.suggestion_area.setAlignment(Qt.AlignCenter)
        for i in range(3):
            sug = QPushButton("")
            sug.setFont(QFont("Arial", 20, QFont.Bold))
            sug.setFixedSize(QSize(250, 60))
            sug.setCursor(Qt.PointingHandCursor)
            sug.setStyleSheet("""
                QPushButton {
                    background-color: #B3E5FC;
                    color: #0D47A1;
                    border-radius: 12px;
                    border: 2px solid #0D47A1;
                }
                QPushButton:hover {
                    background-color: #81D4FA;
                }
            """)
            sug.clicked.connect(lambda _, idx=i: self.use_suggestion(idx))
            self.suggestion_area.addWidget(sug)
            self.suggestion_labels.append(sug)
        main_layout.addLayout(self.suggestion_area)

        # Cuadricula de bloques de letras (3x2)
        grid_layout = QGridLayout()
        grid_layout.setSpacing(20)

        # A-D, E-H, I-L, M-P, Q-T, U-Z (6 bloques)
        blocks = [
            ["A", "B", "C", "D"], ["E", "F", "G", "H"],
            ["I", "J", "K", "L"], ["M", "N", "O", "P"],
            ["Q", "R", "S", "T"], ["U", "V", "W", "X", "Y", "Z"]
        ]
        colors = ["#42A5F5", "#66BB6A", "#FFCA28", "#AB47BC", "#EF5350", "#8D6E63"]
        positions = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]

        block_style = """
            QPushButton {
                font-size: 22px;
                font-weight: bold;
                color: white;
                border-radius: 20px;
                border: 4px solid white;
            }
            QPushButton:hover {
                filter: brightness(1.2);
            }
        """

        for i, group in enumerate(blocks):
            block_btn = QPushButton("\n".join(group))
            block_btn.setFixedSize(QSize(200, 200)) # Ajustado para 3x2
            block_btn.setCursor(Qt.PointingHandCursor)
            
            # Aplicar color individualmente
            block_btn.setStyleSheet(
                block_style + f"QPushButton {{background-color: {colors[i]};}}"
            )
            
            # Al hacer clic, abre el selector de letras
            block_btn.clicked.connect(lambda _, g=group: self.open_letter_selector(g))
            grid_layout.addWidget(block_btn, *positions[i])

        main_layout.addLayout(grid_layout)

        # Botones inferiores (Espacio, Borrar, Hablar, Menú)
        bottom_layout = QHBoxLayout()
        bottom_layout.setAlignment(Qt.AlignCenter)
        bottom_layout.setSpacing(30)
        
        # Botones de accion
        speak_btn = self._make_bottom_button("Hablar", "#26A69A", self.speak_current_text)
        space_btn = self._make_bottom_button("Espacio", "#29B6F6", lambda: self.add_letter(" "))
        clear_btn = self._make_bottom_button("Borrar", "#EC407A", self.clear_text)
        
        # Boton Volver
        back_btn = self._make_bottom_button("Volver", "#1E88E5", 
                                            lambda: self.parent.stack.setCurrentWidget(self.parent.menu_widget))

        for btn in [speak_btn, space_btn, clear_btn, back_btn]:
            bottom_layout.addWidget(btn)

        main_layout.addLayout(bottom_layout)
        self.setLayout(main_layout)

    def _make_bottom_button(self, text, color, action):
        """Helper para crear botones inferiores estandarizados."""
        btn = QPushButton(text)
        btn.setFont(QFont("Arial", 18, QFont.Bold))
        btn.setFixedSize(QSize(180, 60))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border-radius: 15px;
                border: 3px solid white;
            }}
            QPushButton:hover {{
                filter: brightness(0.9);
            }}
        """)
        btn.clicked.connect(lambda: self.animate_click(btn, action))
        return btn

    # Funcionalidad de Audio y Efectos

    def play_click_sound(self):
        """Intenta reproducir un sonido de click (plataforma dependiente)."""
        # Funcion simple para feedback de click, mantenida por compatibilidad
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.Beep(800, 80)
            else:
                sys.stdout.write("\a"); sys.stdout.flush()
        except:
            pass

    def speak_blocking(self, text):
        """Función de TTS que bloquea el hilo."""
        if self.engine:
            try:
                self.engine.stop()
                self.engine.say(text)
                self.engine.runAndWait()
            except Exception as e:
                print(f"Error TTS blocking: {e}")

    def speak_async(self, text):
        """Inicia el TTS en un hilo separado para no bloquear la GUI."""
        if not text or not text.strip() or not self.engine:
            return
        try:
            thread = threading.Thread(target=self.speak_blocking, args=(text,), daemon=True)
            thread.start()
        except Exception as e:
            print(f"Error al iniciar hilo TTS: {e}")

    def speak_current_text(self):
        """Lee el texto completo actualmente en pantalla."""
        text_to_speak = self.selected_text.strip()
        if text_to_speak:
            self.speak_async(text_to_speak)
        else:
            self.speak_async("No hay mensaje para leer")

    # Funcionalidad de Interfaz 

    def animate_click(self, button, action):
        """Aplica animación de click y ejecuta la acción."""
        self.play_click_sound()
        try:
            original_style = button.styleSheet()
            # Animacion simple de fondo amarillo
            temp_style = original_style + "QPushButton:!hover {background-color: #FFFF00;}"
            button.setStyleSheet(temp_style)
            QTimer.singleShot(150, lambda: button.setStyleSheet(original_style))
        except:
            pass
        
        try:
            action()
        except Exception as e:
            print(f"Error en acción del botón: {e}")

    def open_letter_selector(self, group):
        """Abre un QDialog para seleccionar una letra dentro del grupo."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Selecciona una letra ({group[0]} - {group[-1]})")
        dialog.setModal(True)
        dialog.setFixedSize(QSize(400, 300))
        layout = QGridLayout()
        layout.setSpacing(15)
        
        letter_style = """
            QPushButton {
                background-color: #90CAF9;
                color: #1565C0;
                font-size: 32px;
                font-weight: bold;
                border-radius: 15px;
                border: 2px solid white;
            }
            QPushButton:hover {
                background-color: #1565C0;
                color: white;
            }
        """

        for i, letter in enumerate(group):
            btn = QPushButton(letter)
            btn.setFixedSize(QSize(80, 80))
            btn.setStyleSheet(letter_style)
            
            # Conectar la accion y cerrar el dialogo
            btn.clicked.connect(lambda _, l=letter, d=dialog: self.add_letter_and_close(l, d))
            
            layout.addWidget(btn, i // 4, i % 4) # 4 columnas por fila

        dialog.setLayout(layout)
        dialog.exec_()

    def add_letter_and_close(self, letter, dialog):
        """Añade la letra y cierra el diálogo."""
        self.play_click_sound()
        self.add_letter(letter)
        dialog.accept()

    def add_letter(self, letter):
        """Añade una letra o espacio al texto seleccionado y da feedback TTS."""
        
        # Logica para manejar la palabra actual antes de añadir el nuevo caracter
        words = self.selected_text.strip().split()
        current_word = words[-1] if words else ""
        
        self.selected_text += letter
        self.display.setText(self.selected_text)
        
        self.update_suggestions()
        
        # Feedback de voz:
        if letter == " ":
            # Si se añade un espacio, lee la palabra que acaba de completar
            if current_word:
                self.speak_async(current_word)
        elif len(current_word + letter) == 3:
             # Lee la palabra en progreso si alcanza cierta longitud
             self.speak_async(current_word + letter)

    def clear_text(self):
        """Borra todo el texto, resetea sugerencias y habla un mensaje de confirmación."""
        self.selected_text = ""
        self.display.setText("")
        self.update_suggestions()
        self.play_click_sound()
        self.speak_async("mensaje borrado")

    def update_suggestions(self):
        """Actualiza los botones de sugerencias según el prefijo de la última palabra."""
        words = self.selected_text.strip().split()
        prefix = words[-1].lower() if words and self.selected_text.strip()[-1] != ' ' else ""
        
        # Buscar palabras que empiecen con el prefijo y que no sean el prefijo mismo
        suggestions = [w for w in self.word_bank if w.startswith(prefix) and w != prefix][:3]
        
        for i, btn in enumerate(self.suggestion_labels):
            if i < len(suggestions):
                btn.setText(suggestions[i])
                btn.setVisible(True)
            else:
                btn.setText("")
                btn.setVisible(False)

    def use_suggestion(self, index):
        """Usa la sugerencia seleccionada para completar la palabra actual."""
        suggestion = self.suggestion_labels[index].text()
        if not suggestion:
            return

        words = self.selected_text.strip().split()
        
        if not words or self.selected_text.endswith(" "):
            # Si no hay palabras o el ultimo caracter es un espacio, se añade como una palabra nueva
            self.selected_text += suggestion + " "
        else:
            # Reemplaza la palabra incompleta por la sugerencia
            words[-1] = suggestion
            self.selected_text = " ".join(words) + " "
            
        self.display.setText(self.selected_text)
        self.update_suggestions()
        self.play_click_sound()
        self.speak_async(suggestion) # Lee la palabra completa


class AtajosWindow(QWidget):
    """Módulo de Atajos"""
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        
        self.frases = [
            ("Tengo sed", "#4FC3F7"),
            ("Tengo hambre", "#FFB74D"),
            ("Me duele algo", "#E57373"),
            ("Quiero ir al baño", "#66BB6A"),
            ("Estoy feliz", "#FFEE58"),
            ("Emergencia", "#AB47BC"),
        ]
        
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignCenter)
        main_layout.setContentsMargins(50, 50, 50, 50)
        main_layout.setSpacing(30)

        title = QLabel("Atajos")
        title.setFont(QFont("Arial", 30, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #FFA726; margin-bottom: 20px;")
        main_layout.addWidget(title)

        grid_layout = QGridLayout()
        grid_layout.setSpacing(25)
        
        tts_engine = None
        if pyttsx3:
             try:
                tts_engine = pyttsx3.init()
             except:
                pass

        for i, (frase, color) in enumerate(self.frases):
            btn = QPushButton(frase)
            btn.setFont(QFont("Arial", 22, QFont.Bold))
            btn.setFixedSize(QSize(300, 150))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color};
                    color: #333;
                    border-radius: 20px;
                    border: 4px solid white;
                    text-align: center;
                }}
                QPushButton:hover {{
                    filter: brightness(0.9);
                }}
            """)
            
            # Conexion para leer la frase
            if tts_engine:
                btn.clicked.connect(lambda _, text=frase: self.speak_phrase_async(text))
            else:
                btn.clicked.connect(lambda: print("TTS no disponible para leer la frase."))

            grid_layout.addWidget(btn, i // 3, i % 3)

        main_layout.addLayout(grid_layout)
        
        main_layout.addSpacing(50)

        back_btn = QPushButton("Volver al Menú")
        back_btn.setFont(QFont("Arial", 24, QFont.Bold))
        back_btn.setFixedSize(QSize(400, 80))
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setStyleSheet("""
            QPushButton {
                background-color: #1E88E5;
                color: white;
                border-radius: 15px;
                border: 4px solid white;
            }
            QPushButton:hover {
                background-color: #1565C0;
            }
        """)
        back_btn.clicked.connect(lambda: self.parent.stack.setCurrentWidget(self.parent.menu_widget))

        main_layout.addWidget(back_btn, alignment=Qt.AlignCenter)
        main_layout.addStretch(1)

        self.setLayout(main_layout)

    def speak_phrase_async(self, text):
        """Inicia el TTS para la frase en un hilo (similar a ComunicacionWindow)."""
        if pyttsx3:
            try:
                # Reinicializar el motor para el hilo si es necesario, o usar una instancia global
                def speak_blocking(t):
                    try:
                        engine = pyttsx3.init()
                        engine.say(t)
                        engine.runAndWait()
                    except Exception as e:
                        print(f"Error TTS en atajo: {e}")
                
                thread = threading.Thread(target=speak_blocking, args=(text,), daemon=True)
                thread.start()
            except Exception as e:
                print(f"Error al iniciar hilo TTS en atajos: {e}")


class MainWindow(QWidget):
    """Ventana principal que gestiona el menú y la navegación entre módulos."""
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Sistema de Control Asistido")
        # Iniciar en pantalla completa para experiencia de usuario asistida
        self.showFullScreen() 
        self.setAutoFillBackground(True)

        # Fondo degradado
        palette = QPalette()
        gradient = QLinearGradient(0, 0, 1920, 1080)
        gradient.setColorAt(0.0, QColor("#E3F2FD"))
        gradient.setColorAt(1.0, QColor("#BBDEFB"))
        palette.setBrush(QPalette.Window, QBrush(gradient))
        self.setPalette(palette)

        # 1. Controlador de Movimiento
        self.controlador = ControladorArduino(PUERTO_SERIAL, BAUD_RATE)
        
        # 2. Contenedor de vistas (QStackedWidget)
        self.stack = QStackedWidget()
        
        # 3. Widgets de las vistas
        self.menu_widget = QWidget()
        self.comunicacion_window = ComunicacionWindow(self)
        self.atajos_window = AtajosWindow(self)
        self.movimiento_window = VentanaControl(self.controlador, self)

        self.init_menu()

        # AGREGAR AL STACK
        self.stack.addWidget(self.menu_widget)
        self.stack.addWidget(self.comunicacion_window)
        self.stack.addWidget(self.atajos_window)
        self.stack.addWidget(self.movimiento_window)

        main_layout = QVBoxLayout()
        main_layout.addWidget(self.stack)
        self.setLayout(main_layout)

    def init_menu(self):
        """Configura el widget del menú principal."""
        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignCenter)
        main_layout.setContentsMargins(100, 80, 100, 80)
        main_layout.setSpacing(60)

        # Titulo centrado
        title = QLabel("Bienvenido")
        title.setFont(QFont("Arial", 36, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #0D47A1; margin-bottom: 60px;")
        main_layout.addWidget(title)

        # BOTONES DE MODULOS
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignCenter)
        btn_layout.setSpacing(40)
        
        button_info = [
            ("Comunicación", self.comunicacion_window, "#66BB6A", "#43A047"),
            ("Movimiento", self.movimiento_window, "#1E88E5", "#1565C0"),
            ("Atajos", self.atajos_window, "#FFA726", "#FB8C00"),
        ]

        for text, target_widget, bg_color, hover_color in button_info:
            btn = QPushButton(text)
            btn.setFont(QFont("Arial", 28, QFont.Bold))
            btn.setFixedSize(QSize(400, 360))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {bg_color};
                    color: white;
                    border-radius: 45px;
                    border: 6px solid white;
                }}
                QPushButton:hover {{
                    background-color: {hover_color};
                }}
            """)
            btn.clicked.connect(lambda _, w=target_widget: self.stack.setCurrentWidget(w))
            btn_layout.addWidget(btn)

        main_layout.addLayout(btn_layout)

        # BOTON SALIR DEL PROGRAMA
        exit_btn = QPushButton("Salir del Programa")
        exit_btn.setFont(QFont("Arial", 28, QFont.Bold))
        exit_btn.setFixedSize(QSize(600, 100))
        exit_btn.setCursor(Qt.PointingHandCursor)
        exit_btn.setStyleSheet("""
            QPushButton {
                background-color: #D32F2F;
                color: white;
                border-radius: 25px;
                border: 6px solid white;
            }
            QPushButton:hover {
                background-color: #B71C1C;
            }
        """)
        exit_btn.clicked.connect(lambda: QApplication.quit())

        main_layout.addSpacing(80)
        main_layout.addWidget(exit_btn, alignment=Qt.AlignCenter)
        main_layout.addStretch(1) # Asegura el centrado vertical
        self.menu_widget.setLayout(main_layout)

    def closeEvent(self, event):
        """Garantiza el cierre seguro de la conexión serial al salir."""
        if hasattr(self, 'controlador'):
            self.controlador.cerrar_conexion()
        event.accept()

# Ejecucion
if __name__ == "__main__":
    if not QApplication.instance():
        app = QApplication(sys.argv)
    else:
        app = QApplication.instance()

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


import pyautogui
import time
import os

class MonitorPuntero:
    def __init__(self, tiempo_espera):
        """
        Inicializa la clase con el tiempo de espera antes de realizar un clic.
        """
        self.tiempo_espera = tiempo_espera
        self.posicion_inicial = None
        self.tiempo_inicio = None


    def obtener_posicion_puntero(self):
        """
        Obtiene y devuelve la posición actual del puntero del ratón.
        """
        return pyautogui.position()

    def tiempo_de_permanencia(self):
        """
        Monitorea la posición del puntero y realiza un clic si el puntero
        permanece en la misma posición durante 'tiempo_espera' segundos.
        """
        self.posicion_inicial = self.obtener_posicion_puntero()
        self.tiempo_inicio = time.time()

        while True:
            posicion_actual = self.obtener_posicion_puntero()
            tiempo_actual = time.time()

            # Imprimir la posición actual del puntero
            print(f'Posición actual del puntero: {posicion_actual}', end='\r')

            # Si la posición no ha cambiado y ha pasado el tiempo de espera, haz clic
            if posicion_actual == self.posicion_inicial and (tiempo_actual - self.tiempo_inicio) >= self.tiempo_espera:
                pyautogui.click()
                print(f"\nClic realizado en la posición: {posicion_actual}")
                # Después del clic, reiniciar la posición y el tiempo de inicio
                self.posicion_inicial = self.obtener_posicion_puntero()
                self.tiempo_inicio = time.time()

            # Si la posición del puntero cambia, reinicia el temporizador
            elif posicion_actual != self.posicion_inicial:
                self.posicion_inicial = posicion_actual
                self.tiempo_inicio = time.time()

            # Pausar un poco antes de la siguiente comprobación
            time.sleep(0.1)

    def iniciar(self):
        """
        Inicia el monitoreo continuo del puntero del ratón.
        """
        try:
            while True:
                self.tiempo_de_permanencia()
        except KeyboardInterrupt:
            print("\nPrograma terminado.")

# Ejemplo de uso
monitor = MonitorPuntero(tiempo_espera=5)  # Establece el tiempo de espera en 2 segundos
monitor.iniciar()
