import tkinter as tk
import serial
from pynput import keyboard

# --- CÓDIGO DE LOS ORGANIZADORES (SIN ALTERAR) ---
PUERTO_SERIAL = '/dev/ttyUSB0'
BAUD_RATE = 9600
POSICION_NEUTRA = 128
VALOR_MAXIMO = 255
VALOR_MINIMO = 0
PASO_MOVIMIENTO = 15

class ControladorArduino:
    def __init__(self, puerto, baudrate):
        self.ver_duty = POSICION_NEUTRA
        self.hor_duty = POSICION_NEUTRA
        self.teclas_presionadas = set()
        self.arduino = None
        try:
            self.arduino = serial.Serial(puerto, baudrate, timeout=1)
            print(f"Arduino conectado en {puerto}")
        except serial.SerialException as e:
            print(f"Error: No se pudo abrir el puerto {puerto}. Detalles: {e}")

    def enviar_datos(self):
        if self.arduino and self.arduino.is_open:
            try:
                ver_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.ver_duty)))
                hor_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.hor_duty)))
                self.arduino.write(bytes([ver_enviar, hor_enviar]))
                print(f"Enviando -> Vertical: {ver_enviar}, Horizontal: {hor_enviar}")
            except serial.SerialException as e:
                print(f"Error al escribir al puerto serial: {e}")
        else:
            print("Arduino no conectado o puerto cerrado.")

    def actualizar_movimiento(self):
        mov_vertical = 0
        mov_horizontal = 0
        if 'w' in self.teclas_presionadas: mov_vertical += 1
        if 's' in self.teclas_presionadas: mov_vertical -= 1
        if 'a' in self.teclas_presionadas: mov_horizontal -= 1
        if 'd' in self.teclas_presionadas: mov_horizontal += 1
        self.ver_duty = POSICION_NEUTRA + mov_vertical * PASO_MOVIMIENTO * 8
        self.hor_duty = POSICION_NEUTRA + mov_horizontal * PASO_MOVIMIENTO * 8
        self.enviar_datos()

    def al_presionar(self, key):
        pass # Omitido para interfaz gráfica

    def al_liberar(self, key):
        pass

    def cerrar_conexion(self):
        if self.arduino and self.arduino.is_open:
            self.arduino.write(bytes([POSICION_NEUTRA, POSICION_NEUTRA]))
            self.arduino.close()
            print("Conexión con Arduino cerrada.")

# --- INTERFAZ GRÁFICA PASTEL ---
BG = '#FDF1F4'         # Rosa pastel muy suave
INK = '#4A4E69'        # Ciruela oscuro (legible y suave)
MUTED = '#9A8C98'      # Malva oscuro para textos secundarios
ACTIVE_BG = '#B5E4CA'  # Verde menta para botones activos
HEADER_BG = '#E2D4F0'  # Lila pastel
BTN_BG = '#FFFFFF'     # Botones blancos por defecto

TECLA_A_WASD = {'w': 'w', 'up': 'w', 's': 's', 'down': 's', 'a': 'a', 'left': 'a', 'd': 'd', 'right': 'd'}
DIR_A_WASD = {'arriba': 'w', 'abajo': 's', 'izquierda': 'a', 'derecha': 'd'}

class PanelSillaPastel:
    def __init__(self, root):
        self.root = root
        self.controlador = ControladorArduino(PUERTO_SERIAL, BAUD_RATE)
        self.timers = {}
        self.bloqueadas = set()
        self.botones = {}
        self.tecla_raton = None

        root.title('MOVA | Diseño Pastel')
        root.geometry('900x650')
        root.minsize(800, 600)
        root.configure(bg=BG)

        cabecera = tk.Frame(root, bg=HEADER_BG, padx=32, pady=15)
        cabecera.pack(fill='x')
        self.label(cabecera, '✨ MOVA ✨', 22, INK, HEADER_BG, True).pack(side='left')
        
        conectado = self.controlador.arduino and self.controlador.arduino.is_open
        estado_conexion = f"🌸 CONECTADO" if conectado else "☁️ SIN CONEXIÓN USB"
        color_conexion = '#4A4E69' if conectado else '#D5B4B4'
        self.label(cabecera, estado_conexion, 12, color_conexion, HEADER_BG, True).pack(side='right')

        titulo = tk.Frame(root, bg=BG)
        titulo.pack(fill='x', padx=32, pady=(20, 10))
        self.label(titulo, 'Panel de Control Principal', 24, INK, bold=True).pack(anchor='w')
        self.label(titulo, 'Usa el teclado o botones para enviar órdenes suavemente.', 12, MUTED).pack(anchor='w')

        contenido = tk.Frame(root, bg=BG)
        contenido.pack(fill='both', expand=True, padx=32, pady=10)
        contenido.columnconfigure(0, weight=1)
        contenido.columnconfigure(1, weight=1)

        izquierda = tk.Frame(contenido, bg='white', padx=24, pady=20, highlightbackground=HEADER_BG, highlightthickness=2)
        izquierda.grid(row=0, column=0, sticky='nsew', padx=(0, 15))
        self.label(izquierda, '🌸 PANEL DE ENTRADA', 10, MUTED, 'white', True).pack(anchor='w')

        cruceta = tk.Frame(izquierda, bg='white')
        cruceta.pack(expand=True, pady=10)

        for direccion, letra, flecha, fila, columna in [('arriba', 'W', '↑', 0, 1), ('izquierda', 'A', '←', 1, 0), ('abajo', 'S', '↓', 1, 1), ('derecha', 'D', '→', 1, 2)]:
            boton = tk.Button(cruceta, text=f'{flecha}\n{letra}', width=5, height=2, font=('Arial', 16, 'bold'), fg=INK, cursor='hand2')
            boton.grid(row=fila, column=columna, padx=4, pady=4)
            boton.bind('<ButtonPress-1>', lambda e, d=direccion: self.clic(d))
            boton.bind('<ButtonRelease-1>', self.soltar_raton)
            boton.bind('<Leave>', self.soltar_raton)
            self.botones[direccion] = boton

        derecha = tk.Frame(contenido, bg='white', padx=24, pady=20, highlightbackground=HEADER_BG, highlightthickness=2)
        derecha.grid(row=0, column=1, sticky='nsew')
        self.label(derecha, '☁️ ESTADO DE MOTORES', 10, MUTED, 'white', True).pack(anchor='w')
        
        self.lbl_estado = self.label(derecha, 'DETENIDOS', 22, MUTED, 'white', True)
        self.lbl_estado.pack(anchor='w', pady=(15, 5))

        self.lbl_val_vert = self.label(derecha, 'Vertical (Duty): 128', 12, INK, 'white')
        self.lbl_val_vert.pack(anchor='w')
        self.lbl_val_hor = self.label(derecha, 'Horizontal (Duty): 128', 12, INK, 'white')
        self.lbl_val_hor.pack(anchor='w', pady=(0, 15))

        # Texto adaptativo (wraplength dinámico)
        ayuda = tk.Message(derecha, text='1. Haz clic en la ventana para enfocarla.\n2. Mantén WASD para mover.\n3. Suelta para detener.', font=('Arial', 11), fg=MUTED, bg='white', width=300)
        ayuda.pack(anchor='w', fill='x', pady=10)

        # Botón corregido para Mac (fg oscuro para que se lea siempre)
        tk.Button(derecha, text='Detener motores ↺', command=self.limpiar, font=('Arial', 12, 'bold'), fg=INK, highlightbackground=HEADER_BG).pack(side='bottom', fill='x', pady=(10, 0))

        root.bind('<KeyPress>', self.presionar)
        root.bind('<KeyRelease>', self.soltar)
        root.bind('<FocusOut>', lambda e: self.limpiar())
        root.protocol('WM_DELETE_WINDOW', self.cerrar)
        root.after_idle(root.focus_set)

    def label(self, padre, texto, size, fg=INK, bg=BG, bold=False):
        return tk.Label(padre, text=texto, font=('Arial', size, 'bold' if bold else 'normal'), fg=fg, bg=bg)

    def presionar(self, evento):
        t = evento.keysym.lower()
        if t in TECLA_A_WASD:
            if t in self.timers: self.root.after_cancel(self.timers.pop(t))
            if t not in self.bloqueadas:
                self.controlador.teclas_presionadas.add(TECLA_A_WASD[t])
                self.controlador.actualizar_movimiento()
                self.refrescar()

    def soltar(self, evento):
        t = evento.keysym.lower()
        if t in TECLA_A_WASD:
            if t in self.timers: self.root.after_cancel(self.timers.pop(t))
            self.timers[t] = self.root.after(50, lambda: self.confirmar_soltado(t))

    def confirmar_soltado(self, tecla):
        self.timers.pop(tecla, None)
        self.bloqueadas.discard(tecla)
        self.controlador.teclas_presionadas.discard(TECLA_A_WASD[tecla])
        self.controlador.actualizar_movimiento()
        self.refrescar()

    def clic(self, direccion):
        self.root.focus_set()
        self.tecla_raton = DIR_A_WASD[direccion]
        self.controlador.teclas_presionadas.add(self.tecla_raton)
        self.controlador.actualizar_movimiento()
        self.refrescar()

    def soltar_raton(self, evento=None):
        if self.tecla_raton:
            self.controlador.teclas_presionadas.discard(self.tecla_raton)
            self.tecla_raton = None
            self.controlador.actualizar_movimiento()
            self.refrescar()

    def limpiar(self):
        self.controlador.teclas_presionadas.clear()
        self.tecla_raton = None
        self.controlador.actualizar_movimiento()
        self.refrescar()

    def refrescar(self):
        v, h = self.controlador.ver_duty, self.controlador.hor_duty
        if v == POSICION_NEUTRA and h == POSICION_NEUTRA:
            self.lbl_estado.configure(text='DETENIDOS', fg=MUTED)
        else:
            self.lbl_estado.configure(text='EN MOVIMIENTO ✨', fg='#2D6A4F') # Verde oscuro bonito

        self.lbl_val_vert.configure(text=f'Vertical (Duty): {v}')
        self.lbl_val_hor.configure(text=f'Horizontal (Duty): {h}')

    def cerrar(self):
        self.controlador.cerrar_conexion()
        self.root.destroy()

if __name__ == '__main__':
    ventana = tk.Tk()
    PanelSillaPastel(ventana)
    ventana.mainloop()