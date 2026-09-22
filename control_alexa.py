import copy
import queue
import threading
import pyttsx3

class AlexaDispatcher:
    def __init__(self, config):
        self.commands = copy.deepcopy(config["alexa_commands"])
        self.queue = queue.Queue()
        self.results = queue.Queue()
        self._closed = threading.Event()

        self.thread = threading.Thread(
            target=self._run,
            name="alexa-audio",
            daemon=True,
        )
        self.thread.start()

    @property
    def page_count(self):
        return max(1, (len(self.commands) + 3) // 4)

    def get_commands_for_page(self, page_index):
        page_index %= self.page_count
        start = page_index * 4
        items = copy.deepcopy(self.commands[start:start + 4])
        return items + [None] * (4 - len(items))

    def execute(self, command_dict):
        if command_dict is None or self._closed.is_set():
            return
        self.queue.put(copy.deepcopy(command_dict))

    def _run(self):
        engine = None

        try:
            while True:
                command = self.queue.get()

                try:
                    if command is None or self._closed.is_set():
                        return

                    if engine is None:
                        import pyttsx3

                        engine = pyttsx3.init()

                        voices = engine.getProperty("voices")
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
                            engine.setProperty("voice", spanish.id)

                    engine.say(command["phrase"])
                    engine.runAndWait()

                    self.results.put({
                        "id": command["id"],
                        "ok": True,
                        "message": command["phrase"],
                    })

                except Exception as exc:
                    self.results.put({
                        "id": command.get("id", ""),
                        "ok": False,
                        "message": str(exc),
                    })

                    if engine is not None:
                        try:
                            engine.stop()
                        except Exception:
                            pass
                        engine = None

                finally:
                    self.queue.task_done()

        finally:
            if engine is not None:
                engine.stop()

    def close(self):
        # No hace join() ni espera a runAndWait() desde Tkinter.
        self._closed.set()
        self.queue.put(None)