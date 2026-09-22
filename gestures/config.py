"""Configuración de gestos, interfaz y comandos de Alexa."""

import argparse
import copy
import json
import math
import os
import sys
import tempfile
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parents[1] / "gesture_config.json"

ALEXA_DEFAULTS = {
    "audio": "",
    "wake_audio": "",
    "rate": 175,
    "volume": 1.0,
    "pause_before": 0.0,
    "pause_after_wake": 0.0,
    "pause_after": 0.0,
}

DEFAULTS = {
    "threshold_x": 0.22,
    "threshold_y": 0.16,
    "release": 0.12,
    "diagonal_ratio": 1.0,
    "sustain_ratio": 0.65,
    "hold": 0.45,
    "mode_hold": 1.2,
    "center_hold": 0.3,
    "calibration": 2.0,
    "brow": 0.9,
    "blink": 1.8,
    "mouth_open": 0.35,
    "mouth_close": 0.20,
    "mouth_hold": 0.45,
    "mouth_close_hold": 0.20,
    "invert_x": False,
    "invert_y": False,
    "menu_hold": 3.0,
    "interact_page_hold": 1.2,
    "cam_front_index": 2,
    "cam_rear_index": 1,
    "alexa_commands": [
        {
            "id": "on_1",
            "phrase": "Alexa, enciende Cafetera",
            "icon": "switch.png",
        },
        {
            "id": "on_2",
            "phrase": "Alexa, apaga Cafetera",
            "icon": "switch.png",
        },
        {
            "id": "on_3",
            "phrase": "Alexa, enciende Rasuradora",
            "icon": "switch.png",
        },
        {
            "id": "off_1",
            "phrase": "Alexa, apaga Rasuradora",
            "icon": "switch.png",
        },
        {
            "id": "off_2",
            "phrase": "Alexa, enciende Secadora",
            "icon": "switch.png",
        },
        {
            "id": "off_3",
            "phrase": "Alexa, apaga Secadora",
            "icon": "switch.png",
        },
    ],
}


def validate_alexa_commands(commands):
    if not isinstance(commands, list):
        raise ValueError("alexa_commands debe ser una lista.")

    normalized = []
    identifiers = set()
    allowed = {"id", "phrase", "icon"} | ALEXA_DEFAULTS.keys()

    for original in commands:
        if not isinstance(original, dict):
            raise ValueError("Cada comando debe ser un objeto.")

        unknown = original.keys() - allowed
        if unknown:
            raise ValueError(
                "Campos de Alexa desconocidos: "
                + ", ".join(sorted(unknown))
            )

        command = copy.deepcopy(ALEXA_DEFAULTS)
        command.update(copy.deepcopy(original))

        for key in ("id", "phrase", "icon", "audio", "wake_audio"):
            if not isinstance(command.get(key), str):
                raise ValueError(f"{key} debe ser texto.")

        command["id"] = command["id"].strip()

        if not command["id"] or not command["phrase"].strip():
            raise ValueError("id y phrase no pueden estar vacíos.")

        if command["id"] in identifiers:
            raise ValueError(f"ID repetido: {command['id']}")

        identifiers.add(command["id"])

        if type(command["rate"]) is not int or command["rate"] <= 0:
            raise ValueError("rate debe ser un entero positivo.")

        for key in (
            "volume",
            "pause_before",
            "pause_after_wake",
            "pause_after",
        ):
            value = command[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{key} debe ser finito y >= 0.")

        if command["volume"] > 1:
            raise ValueError("volume debe estar entre 0 y 1.")

        for key in ("audio", "wake_audio"):
            command[key] = command[key].strip()

            if command[key]:
                extension = Path(command[key]).suffix.lower()
                if extension not in (".wav", ".mp4", ".aac", ".m4a"):
                    raise ValueError(
                        f"{key}: utiliza WAV, MP4, AAC o M4A."
                    )

        if command["wake_audio"] and not command["audio"]:
            raise ValueError("wake_audio requiere también audio.")

        normalized.append(command)

    return normalized


def defaults(backend="mediapipe"):
    result = copy.deepcopy(DEFAULTS)

    if backend == "openface":
        result.update(
            brow=1.5,
            blink=2.5,
            mouth_open=2.0,
            mouth_close=1.0,
        )

    return result


def validate(values, backend="mediapipe"):
    if not isinstance(values, dict):
        raise ValueError("La configuración debe ser un objeto JSON.")

    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError(
            "Parámetros desconocidos: " + ", ".join(sorted(unknown))
        )

    result = defaults(backend)
    result.update(copy.deepcopy(values))

    for key, value in result.items():
        if key == "alexa_commands":
            result[key] = validate_alexa_commands(value)

        elif key in ("cam_front_index", "cam_rear_index"):
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} debe ser un entero >= 0.")

        elif key.startswith("invert_"):
            if type(value) is not bool:
                raise ValueError(f"{key} debe ser booleano.")

        elif (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} debe ser positivo y finito.")

    if result["release"] >= min(
        result["threshold_x"], result["threshold_y"]
    ):
        raise ValueError(
            "Centro debe ser menor que ambos umbrales direccionales."
        )

    if result["mouth_close"] >= result["mouth_open"]:
        raise ValueError("Cierre de boca debe ser menor que apertura.")

    if not (
        0 < result["sustain_ratio"] <= result["diagonal_ratio"]
        and result["diagonal_ratio"] >= 1
    ):
        raise ValueError(
            "Se requiere 0 < sustain_ratio <= diagonal_ratio "
            "y diagonal_ratio >= 1."
        )

    return result


def read_document(path):
    path = Path(path)

    if not path.exists():
        return {"version": 1, "profiles": {}}

    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)

    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("profiles"), dict)
    ):
        raise ValueError(
            "Formato inválido: se requiere version=1 y profiles."
        )

    for backend, values in data["profiles"].items():
        if backend not in ("mediapipe", "openface"):
            raise ValueError(f"Detector desconocido: {backend}")
        validate(values, backend)

    return data


def load_config(path=CONFIG_PATH, backend="mediapipe"):
    document = read_document(path)
    return validate(document["profiles"].get(backend, {}), backend)


def save_config(values, path=CONFIG_PATH, backend="mediapipe"):
    values = validate(values, backend)
    path = Path(path)

    data = read_document(path)
    data["profiles"][backend] = values

    path.parent.mkdir(parents=True, exist_ok=True)
    name = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            name = stream.name
            json.dump(
                data,
                stream,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(name, path)

    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def add_config_arguments(parser, backend):
    existing = {action.dest for action in parser._actions}

    if "config" not in existing:
        parser.add_argument(
            "--config", type=Path, default=CONFIG_PATH
        )

    if "manual" not in existing:
        parser.add_argument(
            "--manual",
            action="store_true",
            help="Mostrar y habilitar controles de teclado/ratón.",
        )

    for key, value in defaults(backend).items():
        if key in existing:
            continue

        option = "--" + key.replace("_", "-")

        if isinstance(value, bool):
            parser.add_argument(
                option,
                action=argparse.BooleanOptionalAction,
                default=value,
            )

        elif key == "alexa_commands":
            parser.add_argument(
                option,
                type=json.loads,
                default=copy.deepcopy(value),
            )

        elif key in ("cam_front_index", "cam_rear_index"):
            parser.add_argument(option, type=int, default=value)

        else:
            parser.add_argument(option, type=float, default=value)


def parse_settings(parser, backend, argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    first = parser.parse_args(argv)

    try:
        parser.set_defaults(**load_config(first.config, backend))
        args = parser.parse_args(argv)

        supplied = {item.split("=")[0] for item in argv}

        if "--threshold" in supplied:
            for axis in ("x", "y"):
                if "--threshold-" + axis not in supplied:
                    setattr(args, "threshold_" + axis, args.threshold)

        values = validate(
            {key: getattr(args, key) for key in DEFAULTS},
            backend,
        )

        for key, value in values.items():
            setattr(args, key, value)

        return args

    except (ValueError, OSError) as exc:
        parser.error(str(exc))