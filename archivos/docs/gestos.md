# Gestos y calibración de MOVA

Ejecuta desde la raíz del repositorio:

```bash
./.venv/bin/python mova.py
```

Abre **Ajustes de gestos**. Puedes escribir valores, usar Tab y las flechas del
teclado o pulsar las flechas de cada campo con el mouse. **Aplicar** cambia la
sesión; **Aplicar y guardar** escribe `gesture_config.json` junto a `mova.py`.
**Recargar archivo** llena los campos: pulsa Aplicar para enviarlos a la cámara.
Al reiniciar MOVA, el JSON se carga automáticamente. No depende de la carpeta
desde donde ejecutes Python. Si falta el archivo se usan los valores iniciales;
si es inválido se muestra un error y no se sobrescribe silenciosamente.

Abrir ajustes pausa el control. Aplicar los cambios crea un controlador nuevo,
recalibra la postura y exige volver al centro. El teclado de los campos no envía
órdenes de movimiento. Para reanudar cierra ajustes y activa el control de nuevo;
como antes, activar control reinicia la calibración y el modo INTERACCION.
Los ajustes numéricos quedan aplicados aunque vuelvas a calibrar. La postura
neutral NO se guarda porque depende de la cámara y de cómo te sientes.

## Quién hace qué

- `gestures/mediapipe_gestures.py` mide cabeza, cejas, párpados y boca.
- `GestureController`, en `gestures/openface.py`, interpreta las mediciones.
  Importar esa clase no ejecuta el motor OpenFace.
- `gestures/config.py` valida, carga y guarda la configuración.
- `mova.py` muestra la cámara, modifica ajustes y recibe eventos.

OpenFace sigue siendo un detector alternativo, ejecutado por separado. Lee
`AU26_r` para detectar descenso de mandíbula. El JSON tiene perfiles separados
`mediapipe` y `openface`: sus métricas de boca, cejas y ojos tienen escalas distintas.
La ventana de MOVA edita solo el perfil MediaPipe y conserva el de OpenFace.
Referencia de AUs: https://github.com/TadasBaltrusaitis/OpenFace/wiki/Action-Units

## Activar y mantener son decisiones diferentes

`x` e `y` son ángulos en radianes relativos a tu postura neutral. La calibración
usa medianas de al menos 15 muestras durante el tiempo configurado.

| Ajuste | Inicial | Significado |
|---|---:|---|
| `threshold_x` | 0.22 | Giro horizontal para activar |
| `threshold_y` | 0.16 | Inclinación vertical para activar |
| `release` | 0.12 | Límite de retorno; se conserva el centro anterior |
| `diagonal_ratio` | 1.0 | Dominancia de un eje para iniciar |
| `sustain_ratio` | 0.65 | Dominancia para mantener la dirección ya activa |
| `hold` | 0.45 s | Confirmación de dirección |
| `center_hold` | 0.30 s | Confirmación de centro |
| `mode_hold` | 1.20 s | Cejas sostenidas para cambiar modo |

Menor umbral = mayor sensibilidad. `release` debe ser menor que ambos umbrales.
El centro sigue siendo `abs(x) < release AND abs(y) < release`.

Para iniciar derecha: `x >= threshold_x AND x > diagonal_ratio * abs(y)`.
Para mantener derecha: `x > release AND x > sustain_ratio * abs(y)`.
Izquierda y los ejes verticales usan las mismas reglas con el signo correspondiente.
Una diagonal exacta no inicia ninguna dirección con el valor inicial de dominancia.
Una vez activo un eje tiene prioridad dentro del cono sostenido; fuera de él se
solicita STOP y hace falta volver al centro antes de iniciar otra dirección.
Cejas, apertura de boca, ojos cerrados o pérdida de rostro interrumpen movimiento.

Ejemplo: después de activar derecha con `x=0.24`, puedes bajar a `x=0.18` y
subir a `y=0.22` sin corte: `0.18 > 0.12` y `0.18 > 0.65*0.22`.
**Predice:** ¿qué pasa si ahora x baja a 0.11? ¿Bastaría volver a 0.24 sin
permanecer antes en el centro? Compruébalo con la cámara y el control pausado.

El dibujo usa escala angular fija. El recuadro interior muestra la misma zona
central; las marcas exteriores muestran los umbrales por eje. También se sigue
comprobando la dominancia diagonal: estar fuera del recuadro no basta por sí solo.

## Boca: una acción por apertura

MediaPipe calcula distancia entre labios interiores (13 y 14) dividida entre
ancho de boca (61 y 291). Son proporciones, no píxeles ni unidades de acción.

1. Observar boca cerrada durante `mouth_close_hold` rearma el gesto.
2. Superar `mouth_open` continuamente durante `mouth_hold` emite `MOUTH_OPEN`.
3. Mantenerla abierta no repite el evento.
4. Para otra apertura debe bajar a `mouth_close` durante el tiempo de cierre.

Valores iniciales MediaPipe: apertura 0.35, cierre 0.20, confirmación de apertura
0.45 s y cierre 0.20 s. OpenFace usa AU26_r con umbrales 2.0 / 1.0. Son puntos de
partida para calibrar, no valores universales. El HUD muestra la medida de boca
con su umbral y `--debug` también imprime su valor.

La boca no exige armar con el centro: tiene su propio rearme mediante cierre.
Al detectar apertura por encima del umbral se cancela la dirección, incluso
antes de confirmar la acción, y se exige centro para volver a mover. Una pérdida
de seguimiento no cuenta como cierre; si reapareces con boca abierta no se
emite una acción nueva. Tiene prioridad sobre las cejas si ocurren a la vez.

MOVA muestra un contador de eventos de boca. **No ejecuta ninguna acción externa**;
la acción está pendiente de definir. Los eventos tienen una cola independiente
de las imágenes para que un frame nuevo no borre el evento anterior. Los eventos
de una calibración anterior o con más de 0.5 s se descartan en la UI.

## Uso sin la UI

```bash
./.venv/bin/python gestures/mediapipe_gestures.py --preview --debug
./.venv/bin/python gestures/mediapipe_gestures.py --threshold-x .22 --threshold-y .16
./.venv/bin/python gestures/openface.py --root "$PWD" --signal head --preview --debug
```

Se carga el perfil del JSON y las opciones explícitas de terminal tienen prioridad.
`--config /ruta/perfil.json` permite elegir otro archivo. Las opciones de terminal
no se guardan automáticamente. `--threshold .22` sigue disponible como atajo para
ambos ejes; `--threshold-x` y `--threshold-y` explícitos tienen prioridad sobre él.
`--no-invert-x` y `--no-invert-y` permiten desactivar inversiones guardadas.

## Verificación

```bash
python -m unittest discover -s tests -v
```

Las pruebas usan mediciones sintéticas, sin cámara ni puertos seriales. Cubren
histéresis, tolerancia diagonal, centro obligatorio, pérdida de seguimiento,
parpadeo, boca única, cierre confirmado, calibración y persistencia/validación.
No validan precisión en caras reales, latencia, interfaz gráfica real ni hardware.
