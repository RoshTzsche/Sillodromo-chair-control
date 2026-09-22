"""Audio asíncrono para Alexa: síntesis, WAV/MP4 y pausas."""

import copy
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from gestures.config import validate_alexa_commands


class _Cancelled(Exception):
    pass


class AlexaDispatcher:
    def __init__(self, config):
        self.base_dir = Path(__file__).resolve().parent

        self.queue = queue.Queue()
        self.results = queue.Queue()

        self._closed = threading.Event()
        self._lock = threading.RLock()
        self._engine = None

        self.reload(config)

        self.thread = threading.Thread(
            target=self._run,
            name="alexa-audio",
            daemon=True,
        )
        self.thread.start()

    def reload(self, config):
        """Actualizar comandos sin interrumpir las órdenes ya encoladas."""
        commands = validate_alexa_commands(config["alexa_commands"])

        with self._lock:
            self.commands = commands

    @property
    def page_count(self):
        with self._lock:
            return max(1, (len(self.commands) + 3) // 4)

    def get_commands_for_page(self, page_index):
        with self._lock:
            start = (page_index % self.page_count) * 4
            items = copy.deepcopy(self.commands[start:start + 4])

        return items + [None] * (4 - len(items))

    def execute(self, command_dict):
        """Encolar una copia de los parámetros vigentes."""
        if command_dict is None:
            return

        with self._lock:
            if self._closed.is_set():
                return

            command = validate_alexa_commands([command_dict])[0]
            self.queue.put((command, time.monotonic()))

    def _pause(self, seconds):
        """Esperar únicamente en el hilo de audio."""
        if self._closed.wait(seconds):
            raise _Cancelled()

    def _get_engine(self):
        if self._engine is None:
            import pyttsx3

            self._engine = pyttsx3.init()
            voices = self._engine.getProperty("voices")

            spanish = next(
                (
                    voice
                    for voice in voices
                    if any(
                        marker in (
                            f"{voice.id} "
                            f"{voice.name} "
                            f"{voice.languages}"
                        ).lower()
                        for marker in (
                            "spanish", "español", "es-", "es_"
                        )
                    )
                ),
                None,
            )

            if spanish is not None:
                self._engine.setProperty("voice", spanish.id)

        return self._engine

    def _speak(self, text, command):
        self._pause(0)

        engine = self._get_engine()
        engine.setProperty("rate", command["rate"])
        engine.setProperty("volume", command["volume"])

        engine.say(text)
        engine.runAndWait()

        self._pause(0)

    def _play_file(self, filename, volume):
        player = shutil.which("ffplay")

        if player is None:
            raise RuntimeError(
                "No se encontró ffplay. Instala FFmpeg con ffplay "
                "y comprueba que esté disponible en PATH."
            )

        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        path = path.resolve()

        if not path.is_file():
            raise FileNotFoundError(f"No existe el audio: {path}")

        self._pause(0)

        process = subprocess.Popen(
            [
                player,
                "-nodisp",
                "-autoexit",
                "-vn",
                "-sn",
                "-loglevel",
                "error",
                "-volume",
                str(round(volume * 100)),
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        try:
            while True:
                self._pause(0)

                try:
                    _, error = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    continue

            if process.returncode:
                raise RuntimeError(
                    error.strip()[-800:]
                    or f"ffplay terminó con código {process.returncode}"
                )

        finally:
            if process.poll() is None:
                process.terminate()

                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def _audio_exists(self, filename):
        if not filename:
            return False

        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path

        return path.is_file()

    def _speak_phrase(self, command):
        phrase = command["phrase"].strip()

        split = re.match(
            r"^(Alexa)\b[\s,;:.!?…—-]*(.*)$",
            phrase,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if (
            command["pause_after_wake"] > 0
            and split
            and split.group(2).strip()
        ):
            self._speak(split.group(1), command)
            self._pause(command["pause_after_wake"])
            self._speak(split.group(2).strip(), command)
        else:
            self._speak(phrase, command)

    def _play_command(self, command):
        self._pause(command["pause_before"])

        if not self._audio_exists(command["audio"]):
            # Sin grabación de la orden: sintetizar la frase completa.
            self._speak_phrase(command)

        else:
            if command["wake_audio"]:
                # Tener wake_audio configurado significa que audio
                # contiene solamente la orden, sin la palabra "Alexa".
                if self._audio_exists(command["wake_audio"]):
                    self._play_file(
                        command["wake_audio"],
                        command["volume"],
                    )
                else:
                    self._speak("Alexa", command)

                self._pause(command["pause_after_wake"])

            self._play_file(
                command["audio"],
                command["volume"],
            )

        self._pause(command["pause_after"])
    def _reset_engine(self):
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass

            self._engine = None

    def _run(self):
        try:
            while True:
                item = self.queue.get()

                try:
                    if item is None:
                        return

                    if self._closed.is_set():
                        continue

                    command, queued_at = item
                    started = time.monotonic()
                    queue_wait = started - queued_at

                    try:
                        self._play_command(command)

                    except _Cancelled:
                        continue

                    except Exception as exc:
                        self._reset_engine()

                        self.results.put({
                            "id": command["id"],
                            "ok": False,
                            "message": str(exc),
                        })

                    else:
                        elapsed = time.monotonic() - started

                        self.results.put({
                            "id": command["id"],
                            "ok": True,
                            "elapsed_s": elapsed,
                            "queue_wait_s": queue_wait,
                            "message": (
                                f"{command['phrase']} · {elapsed:.2f} s "
                                f"· cola {queue_wait:.2f} s"
                            ),
                        })

                finally:
                    self.queue.task_done()

        finally:
            self._reset_engine()

    def close(self):
        """Solicitar el cierre sin bloquear Tkinter."""
        with self._lock:
            if not self._closed.is_set():
                self._closed.set()
                self.queue.put(None)