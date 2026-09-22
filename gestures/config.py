"""Configuración validada, sin dependencias de cámara ni de interfaz."""
import json
import math
import os
from pathlib import Path
import sys
import tempfile

CONFIG_PATH = Path(__file__).resolve().parents[1] / 'gesture_config.json'
DEFAULTS = dict(threshold_x=.22, threshold_y=.16, release=.12,
                diagonal_ratio=1.0, sustain_ratio=.65,
                hold=.45, mode_hold=1.2, center_hold=.3, calibration=2.0,
                brow=.9, blink=1.8, mouth_open=.35, mouth_close=.20,
                mouth_hold=.45, mouth_close_hold=.20,
                invert_x=False, invert_y=False)


def defaults(backend='mediapipe'):
    result = DEFAULTS.copy()
    if backend == 'openface':
        result.update(brow=1.5, blink=2.5, mouth_open=2.0, mouth_close=1.0)
    return result


def validate(values, backend='mediapipe'):
    if not isinstance(values, dict):
        raise ValueError('La configuración debe ser un objeto JSON.')
    unknown = values.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError('Parámetros desconocidos: ' + ', '.join(sorted(unknown)))
    result = defaults(backend)
    result.update(values)
    for key, value in result.items():
        if key.startswith('invert_'):
            if type(value) is not bool:
                raise ValueError(f'{key} debe ser booleano.')
        elif (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError(f'{key} debe ser positivo y finito.')
    if result['release'] >= min(result['threshold_x'], result['threshold_y']):
        raise ValueError('Centro debe ser menor que ambos umbrales direccionales.')
    if result['mouth_close'] >= result['mouth_open']:
        raise ValueError('Cierre de boca debe ser menor que apertura.')
    if not 0 < result['sustain_ratio'] <= result['diagonal_ratio'] or result['diagonal_ratio'] < 1:
        raise ValueError('Se requiere 0 < tolerancia sostenida ≤ dominancia inicial y dominancia inicial ≥ 1.')
    return result


def read_document(path):
    path = Path(path)
    if not path.exists():
        return {'version': 1, 'profiles': {}}
    with path.open(encoding='utf-8') as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
        raise ValueError('Formato de configuración inválido; se requiere version=1 y profiles.')
    for backend, values in data['profiles'].items():
        if backend not in ('mediapipe', 'openface'):
            raise ValueError(f'Detector desconocido: {backend}')
        validate(values, backend)
    return data


def load_config(path=CONFIG_PATH, backend='mediapipe'):
    return validate(read_document(path)['profiles'].get(backend, {}), backend)


def save_config(values, path=CONFIG_PATH, backend='mediapipe'):
    values = validate(values, backend)
    path = Path(path)
    data = read_document(path)
    data['profiles'][backend] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            name = stream.name
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def add_config_arguments(parser, backend):
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    existing = {action.dest for action in parser._actions}
    for key, value in defaults(backend).items():
        if key not in existing:
            parser.add_argument('--' + key.replace('_', '-'), type=float, default=value)


def parse_settings(parser, backend, argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    first = parser.parse_args(argv)
    try:
        parser.set_defaults(**load_config(first.config, backend))
        args = parser.parse_args(argv)
        # Compatibilidad: --threshold explícito controla ambos ejes.
        supplied = {item.split('=')[0] for item in argv}
        if '--threshold' in supplied:
            for axis in ('x', 'y'):
                if '--threshold-' + axis not in supplied:
                    setattr(args, 'threshold_' + axis, args.threshold)
        values = validate({key: getattr(args, key) for key in DEFAULTS}, backend)
        for key, value in values.items():
            setattr(args, key, value)
        return args
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
