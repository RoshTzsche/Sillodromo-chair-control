#movement.py is a library to move the wheel chair
import serial
from pynput import keyboard

# --- Constantes de configuración ---
PUERTO_SERIAL = '/dev/ttyUSB0'
BAUD_RATE = 9600
POSICION_NEUTRA = 128
VALOR_MAXIMO = 255
VALOR_MINIMO = 0
PASO_MOVIMIENTO = 15 # Incremento en cada PASO

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
                # Aseguramos que los valores estén en el rango 0-255
                ver_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.ver_duty)))
                hor_enviar = max(VALOR_MINIMO, min(VALOR_MAXIMO, int(self.hor_duty)))

                self.arduino.write(bytes([ver_enviar, hor_enviar]))
                print(f"Enviando -> Vertical: {ver_enviar}, Horizontal: {hor_enviar}")
            except serial.SerialException as e:
                print(f"Error al escribir al puerto serial: {e}")
        else:
            print("Arduino no conectado o puerto cerrado.")

    def actualizar_movimiento(self):
        """Calcula el movimiento basado en las teclas presionadas."""
        mov_vertical = 0
        mov_horizontal = 0

        if 'w' in self.teclas_presionadas:
            mov_vertical += 1
        if 's' in self.teclas_presionadas:
            mov_vertical -= 1
        if 'a' in self.teclas_presionadas:
            # En muchos sistemas, 'a' es izquierda (menor valor) y 'd' derecha (mayor valor)
            mov_horizontal -= 1
        if 'd' in self.teclas_presionadas:
            mov_horizontal += 1
            
        # Actualizar valores de duty. Si no hay teclas, vuelven a la posición neutra.
        self.ver_duty = POSICION_NEUTRA + mov_vertical * PASO_MOVIMIENTO * 8 # Mayor rango de movimiento vertical
        self.hor_duty = POSICION_NEUTRA + mov_horizontal * PASO_MOVIMIENTO * 8

        self.enviar_datos()

    def al_presionar(self, key):
        """Callback para cuando se presiona una tecla."""
        try:
            # Si se presiona 'esc', detenemos el listener.
            if key == keyboard.Key.esc:
                print("Tecla 'esc' presionada. Saliendo...")
                return False  # Esto detiene el listener

            k = key.char.lower()
            if k in 'wasd':
                self.teclas_presionadas.add(k)
                self.actualizar_movimiento()
        except AttributeError:
            # Ignora teclas especiales que no tienen '.char' (como Shift, Ctrl, etc.)
            pass

    def al_liberar(self, key):
        """Callback para cuando se suelta una tecla."""
        try:
            k = key.char.lower()
            if k in 'wasd':
                self.teclas_presionadas.discard(k)
                self.actualizar_movimiento()
        except AttributeError:
            pass
            
    def cerrar_conexion(self):
        """Cierra la conexión serial de forma segura."""
        if self.arduino and self.arduino.is_open:
            # Regresar a la posición neutral antes de cerrar
            self.arduino.write(bytes([POSICION_NEUTRA, POSICION_NEUTRA]))
            self.arduino.close()
            print("Conexión con Arduino cerrada.")

# --- Bucle Principal ---
if __name__ == "__main__":
    controlador = ControladorArduino(PUERTO_SERIAL, BAUD_RATE)

    # Inicia el listener de teclado en un bloque 'with' para asegurar que se limpie correctamente
    with keyboard.Listener(on_press=controlador.al_presionar, on_release=controlador.al_liberar) as listener:
        print("Control activado. Usa 'w', 'a', 's', 'd' para mover. Presiona 'esc' para salir.")
        listener.join()

    # Cuando el listener se detiene (al presionar 'esc'), el código continúa aquí.
    controlador.cerrar_conexion()
    print("Programa finalizado.")


class Chettos:
    def __init__(self, sabor, color,forma):
        self.sabor = "rojos"
        self.color = "nacho"
        self.forma = "torciditos"