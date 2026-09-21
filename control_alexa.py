"""Instalar: python -m pip install pyttsx3 pygame
Ejecutar: python control_alexa.py
Grabaciones: 1.wav ... 6.wav (también .ogg), con la frase completa.
"""
import os
import sys
from pathlib import Path

FRASES = {
    '1': 'Alexa, enciende enchufe uno',
    '2': 'Alexa, enciende enchufe dos',
    '3': 'Alexa, enciende enchufe tres',
    '4': 'Alexa, apaga enchufe uno',
    '5': 'Alexa, apaga enchufe dos',
    '6': 'Alexa, apaga enchufe tres',
}


def hablar():
    import pyttsx3
    motor = pyttsx3.init()
    try:
        if sys.argv[3]:
            motor.setProperty('voice', sys.argv[3])
        motor.setProperty('rate', int(sys.argv[4]))
        motor.setProperty('volume', float(sys.argv[5]))
        motor.say(sys.argv[2])
        motor.runAndWait()
    finally:
        motor.stop()


def interfaz():
    import subprocess
    import time
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
    import pygame

    class Aplicacion:
        def __init__(self, root):
            self.root = root
            self.proceso = None
            self.inicio = 0
            self.sonidos = {}
            self.carpeta = None
            self.pulsadas = set()
            self.liberaciones = {}
            self.voices = []
            self.audio_error = ''
            self.tts_error = ''
            try:
                pygame.mixer.init(buffer=512)
                self.canal = pygame.mixer.Channel(0)
            except Exception as exc:
                self.canal = None
                self.audio_error = str(exc)
            try:
                import pyttsx3
                motor = pyttsx3.init()
                self.voices = motor.getProperty('voices')
                motor.stop()
            except Exception as exc:
                self.tts_error = str(exc)

            root.title('Alexa · Control de enchufes')
            root.minsize(610, 580)
            panel = ttk.Frame(root, padding=20)
            panel.pack(fill='both', expand=True)
            ttk.Label(panel, text='Control de enchufes', font=('', 20, 'bold')).pack(anchor='w')
            ttk.Label(panel, text='Teclas 1–6 o botones · Esc para detener').pack(anchor='w', pady=(4, 15))
            self.modo = tk.StringVar(value='sintesis' if self.voices else 'grabaciones')
            modos = ttk.Frame(panel)
            modos.pack(fill='x')
            for nombre, valor in [('Voz del sistema', 'sintesis'), ('Mis grabaciones', 'grabaciones')]:
                ttk.Radiobutton(modos, text=nombre, variable=self.modo, value=valor,
                                command=self.cambiar_modo).pack(side='left', padx=(0, 20))
            ttk.Label(panel, text='Voz instalada:').pack(anchor='w', pady=(14, 3))
            nombres = [f'{i + 1}. {v.name or v.id}' for i, v in enumerate(self.voices)]
            self.selector = ttk.Combobox(panel, values=nombres, state='readonly', width=62)
            self.selector.pack(fill='x')
            if nombres:
                elegida = next((i for i, v in enumerate(self.voices)
                                if any(s in f'{v.id} {v.name} {v.languages}'.lower()
                                       for s in ('spanish', 'español', 'es-', 'es_'))), 0)
                self.selector.current(elegida)
            self.velocidad = tk.IntVar(value=175)
            self.ritmo = tk.Scale(panel, from_=80, to=300, orient='horizontal',
                                 variable=self.velocidad, label='Velocidad de síntesis (palabras/minuto)')
            self.ritmo.pack(fill='x', pady=8)
            self.volumen = tk.IntVar(value=100)
            tk.Scale(panel, from_=0, to=100, orient='horizontal', variable=self.volumen,
                     label='Volumen (%)').pack(fill='x')
            fila = ttk.Frame(panel)
            fila.pack(fill='x', pady=(12, 4))
            ttk.Button(fila, text='Elegir carpeta…', command=self.elegir_carpeta).pack(side='left')
            ttk.Button(fila, text='Volver a revisar', command=self.revisar).pack(side='left', padx=8)
            self.archivos = tk.StringVar(value='Usa 1.wav a 6.wav, o 1.ogg a 6.ogg. Cada archivo contiene una orden completa.')
            ttk.Label(panel, textvariable=self.archivos, wraplength=570).pack(anchor='w', pady=6)
            botones = ttk.Frame(panel)
            botones.pack(fill='x', pady=10)
            for i, (tecla, frase) in enumerate(FRASES.items()):
                ttk.Button(botones, text=f'{tecla} · {frase.removeprefix("Alexa, ").capitalize()}',
                           command=lambda k=tecla: self.reproducir(k)).grid(
                               row=i % 3, column=i // 3, sticky='ew', padx=4, pady=4)
            botones.columnconfigure((0, 1), weight=1)
            ttk.Button(panel, text='Detener (Esc)', command=self.detener).pack(fill='x')
            self.estado = tk.StringVar(value='Listo. Mantén esta ventana seleccionada para usar las teclas.')
            ttk.Label(panel, textvariable=self.estado, wraplength=570).pack(anchor='w', pady=12)
            if self.tts_error:
                self.estado.set('Síntesis no disponible: ' + self.tts_error)
            root.bind('<KeyPress>', self.presionar)
            root.bind('<KeyRelease>', self.soltar)
            root.bind('<FocusOut>', lambda e: self.pulsadas.clear())
            root.bind('<Escape>', lambda e: self.detener())
            root.protocol('WM_DELETE_WINDOW', self.cerrar)
            self.cambiar_modo()
            root.after(30, self.actualizar)

        def cambiar_modo(self):
            activo = self.modo.get() == 'sintesis'
            self.selector.configure(state='readonly' if activo else 'disabled')
            self.ritmo.configure(state='normal' if activo else 'disabled')

        def elegir_carpeta(self):
            ruta = filedialog.askdirectory(title='Carpeta de órdenes grabadas')
            if ruta:
                self.carpeta = Path(ruta)
                self.revisar()
                self.modo.set('grabaciones')
                self.cambiar_modo()

        def revisar(self):
            if self.carpeta is None:
                return
            if self.canal is None:
                messagebox.showerror('Audio no disponible', self.audio_error)
                return
            self.sonidos.clear()
            errores = []
            try:
                archivos = {p.name.lower(): p for p in self.carpeta.iterdir() if p.is_file()}
                for tecla in FRASES:
                    ruta = next((archivos[tecla + ext] for ext in ('.wav', '.ogg')
                                 if tecla + ext in archivos), None)
                    if ruta:
                        try:
                            self.sonidos[tecla] = pygame.mixer.Sound(str(ruta))
                        except Exception as exc:
                            errores.append(f'{ruta.name}: {exc}')
            except OSError as exc:
                errores.append(str(exc))
            faltan = ', '.join(k for k in FRASES if k not in self.sonidos) or 'ninguna'
            self.archivos.set(f'{self.carpeta}\nCargadas: {len(self.sonidos)}/6 · Faltan: {faltan}')
            if errores:
                messagebox.showwarning('Archivos que no se pudieron cargar', '\n'.join(errores))

        def ocupado(self):
            return self.proceso is not None or (self.canal is not None and self.canal.get_busy())

        def reproducir(self, tecla):
            if self.ocupado():
                self.estado.set('Ya hay una orden en curso. Espera o pulsa Esc para detenerla.')
                return
            try:
                if self.modo.get() == 'grabaciones':
                    if tecla not in self.sonidos:
                        self.estado.set(f'Falta la grabación {tecla}.wav o {tecla}.ogg. Elige una carpeta y revísala.')
                        return
                    self.canal.set_volume(self.volumen.get() / 100)
                    self.canal.play(self.sonidos[tecla])
                else:
                    indice = self.selector.current()
                    if indice < 0:
                        self.estado.set('No hay una voz instalada disponible. Puedes usar tus grabaciones.')
                        return
                    # Un proceso independiente evita bloquear Tkinter y reutilizar
                    # un motor de voz que haya quedado atascado tras la primera frase.
                    self.proceso = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), '--hablar',
                         FRASES[tecla], self.voices[indice].id, str(self.velocidad.get()),
                         str(self.volumen.get() / 100)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    self.inicio = time.monotonic()
                self.estado.set('Reproduciendo: ' + FRASES[tecla])
            except Exception as exc:
                self.estado.set('Error: ' + str(exc))

        def actualizar(self):
            if self.proceso is not None:
                codigo = self.proceso.poll()
                if codigo is not None:
                    _, error = self.proceso.communicate()
                    self.proceso = None
                    self.estado.set('Listo' if codigo == 0 else
                                    'Error de voz: ' + error.decode(errors='replace')[-450:])
                elif time.monotonic() - self.inicio > 30:
                    self.detener()
                    self.estado.set('El motor no respondió en 30 segundos. Prueba otra voz o tus grabaciones.')
            elif self.canal and not self.canal.get_busy() and self.estado.get().startswith(('Reproduciendo:', 'Ya hay')):
                self.estado.set('Listo')
            self.root.after(30, self.actualizar)

        def presionar(self, evento):
            tecla = evento.char
            pendiente = self.liberaciones.pop(tecla, None)
            if pendiente is not None:
                self.root.after_cancel(pendiente)
            if tecla in FRASES and tecla not in self.pulsadas:
                self.pulsadas.add(tecla)
                # No dispara órdenes al buscar una voz con el teclado.
                if evento.widget != self.selector:
                    self.reproducir(tecla)

        def soltar(self, evento):
            tecla = evento.char
            def confirmar():
                self.pulsadas.discard(tecla)
                self.liberaciones.pop(tecla, None)
            anterior = self.liberaciones.pop(tecla, None)
            if anterior is not None:
                self.root.after_cancel(anterior)
            self.liberaciones[tecla] = self.root.after_idle(confirmar)

        def detener(self):
            if self.canal:
                self.canal.stop()
            if self.proceso:
                if self.proceso.poll() is None:
                    self.proceso.kill()
                self.proceso.communicate()
                self.proceso = None
            self.estado.set('Detenido. Listo para otra orden.')

        def cerrar(self):
            self.detener()
            pygame.mixer.quit()
            self.root.destroy()

    root = tk.Tk()
    Aplicacion(root)
    root.mainloop()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--hablar':
        hablar()
    else:
        interfaz()
