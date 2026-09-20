"""Configuration loading, defaults and validation (plan2.md §10).

The YAML file is the single source of truth for command words, timings, paths
and model ids.  ``DEFAULTS`` mirrors ``config.yaml`` so a partial or older file
still runs, and validation failures are reported as :class:`ConfigError` before
any audio device or model is touched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

AEC_BACKENDS = ("echoff", "voiceclean", "speexdsp")
ASR_DEVICES = ("cuda", "cpu")
NEMOTRON_PRECISIONS = ("fp16", "fp32", "int8")


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed or contradictory."""


DEFAULTS: dict[str, Any] = {
    "audio": {"device": None, "sample_rate": 16000},
    "aec": {
        "enabled": False,
        "backend": "echoff",
        "loopback_device": None,
        "frame_ms": 10,
        "stream_delay_ms": 0,
    },
    "asr": {
        "backend": "parakeet",
        "models_dir": "./models",
        "device": "cuda",
        "language": "pt",
        "whisper": {
            "model": "small",
            "compute_type": "float16",
            "beam_size": 1,
            "condition_on_previous_text": False,
        },
        "parakeet": {"model_id": "nemo-parakeet-tdt-0.6b-v3", "quantization": None},
        "nemotron": {
            "model_id": "codavidgarcia/nemotron-3.5-asr-streaming-0.6b-onnx",
            "chunk_ms": 320,
            "precision": "fp16",
            "language": "auto",
        },
    },
    "vad": {
        "enabled": True,
        "model_path": "./models/vad/silero_vad.onnx",
        "threshold": 0.5,
        "min_speech_ms": 150,
        "min_silence_ms": 400,
        "max_utterance_s": 25,
    },
    "trigger": {
        "words": ["youtube", "you tube", "iutube", "u tube", "utube"],
        "trigger_window_ms": 1500,
        "silence_end_ms": 800,
        "query_max_ms": 6000,
    },
    "player": {
        "mpv_path": "mpv",
        "ipc_path": None,
        "audio_only": True,
        "volume_step": 10,
        "volume_min": 0,
        "volume_max": 130,
        "start_timeout_s": 10,
        "extra_args": ["--no-video", "--really-quiet", "--idle=yes"],
    },
    "search": {"results": 5, "cookies_from_browser": None, "socket_timeout_s": 15},
    "behaviour": {
        "log_transcripts": True,
        "log_path": "./logs/transcripts.log",
        "log_level": "INFO",
        "ignore_trigger_ms_after_play": 500,
        "require_same_utterance_while_playing": True,
        "ui": True,
    },
    "commands": [
        {"action": "play", "verbs": ["passa", "toca", "poe", "mete"], "takes_query": True},
        {"action": "next", "verbs": ["proximo", "seguinte", "outra", "salta"], "takes_query": False},
        {"action": "prev", "verbs": ["previous", "anterior", "volta"], "takes_query": False},
        {"action": "stop", "verbs": ["para", "pausa", "chega"], "takes_query": False},
        {"action": "resume", "verbs": ["continue", "continua", "retoma"], "takes_query": False},
        {"action": "volume_up", "verbs": ["mais alto", "aumenta", "sobe o som"], "takes_query": False},
        {"action": "volume_down", "verbs": ["mais baixo", "baixa", "baixa o som"], "takes_query": False},
    ],
}


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioConfig:
    device: int | str | None
    sample_rate: int


@dataclass(frozen=True)
class AecConfig:
    enabled: bool
    backend: str
    loopback_device: int | str | None
    frame_ms: int
    stream_delay_ms: int


@dataclass(frozen=True)
class WhisperConfig:
    model: str
    compute_type: str
    beam_size: int
    condition_on_previous_text: bool


@dataclass(frozen=True)
class ParakeetConfig:
    model_id: str
    quantization: str | None


@dataclass(frozen=True)
class NemotronConfig:
    model_id: str
    chunk_ms: int
    precision: str
    language: str


@dataclass(frozen=True)
class AsrConfig:
    backend: str
    models_dir: Path
    device: str
    language: str
    whisper: WhisperConfig
    parakeet: ParakeetConfig
    nemotron: NemotronConfig


@dataclass(frozen=True)
class VadConfig:
    enabled: bool
    model_path: Path
    threshold: float
    min_speech_ms: int
    min_silence_ms: int
    max_utterance_s: float


@dataclass(frozen=True)
class TriggerConfig:
    words: tuple[str, ...]
    trigger_window_ms: int
    silence_end_ms: int
    query_max_ms: int


@dataclass(frozen=True)
class PlayerConfig:
    mpv_path: str
    ipc_path: str | None
    audio_only: bool
    volume_step: int
    volume_min: int
    volume_max: int
    start_timeout_s: float
    extra_args: tuple[str, ...]


@dataclass(frozen=True)
class SearchConfig:
    results: int
    cookies_from_browser: str | None
    socket_timeout_s: float


@dataclass(frozen=True)
class BehaviourConfig:
    log_transcripts: bool
    log_path: Path
    log_level: str
    ignore_trigger_ms_after_play: int
    require_same_utterance_while_playing: bool
    ui: bool


@dataclass(frozen=True)
class CommandSpec:
    action: str
    verbs: tuple[str, ...]
    takes_query: bool


@dataclass(frozen=True)
class Config:
    """Fully resolved configuration; all paths are absolute."""

    source: Path
    audio: AudioConfig
    aec: AecConfig
    asr: AsrConfig
    vad: VadConfig
    trigger: TriggerConfig
    player: PlayerConfig
    search: SearchConfig
    behaviour: BehaviourConfig
    commands: tuple[CommandSpec, ...] = field(default_factory=tuple)

    def resolve(self, value: str | Path) -> Path:
        """Resolve a config-relative path against the config file's directory."""
        path = Path(value)
        if not path.is_absolute():
            path = self.source.parent / path
        return path.resolve()


# --------------------------------------------------------------------------- #
# loading helpers
# --------------------------------------------------------------------------- #

ASR_BACKENDS = ("whisper", "parakeet", "nemotron")
NEMOTRON_CHUNK_MS = (80, 160, 320, 560, 1120)
REQUIRED_SAMPLE_RATE = 16000


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping, got {type(value).__name__}")
    return value


def _int(section: dict[str, Any], name: str, where: str, minimum: int | None = None) -> int:
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{name} must be a number, got {value!r}")
    if float(value) != int(value):
        raise ConfigError(f"{where}.{name} must be a whole number, got {value!r}")
    result = int(value)
    if minimum is not None and result < minimum:
        raise ConfigError(f"{where}.{name} must be >= {minimum}, got {result}")
    return result


def _float(section: dict[str, Any], name: str, where: str) -> float:
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{name} must be a number, got {value!r}")
    return float(value)


def _bool(section: dict[str, Any], name: str, where: str) -> bool:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{name} must be true or false, got {value!r}")
    return value


def _str(section: dict[str, Any], name: str, where: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{name} must be a non-empty string, got {value!r}")
    return value.strip()


def _optional_str(section: dict[str, Any], name: str, where: str) -> str | None:
    value = section.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{name} must be a string or null, got {value!r}")
    return value.strip() or None


def _optional_device(section: dict[str, Any], name: str, where: str) -> int | str | None:
    value = section.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{where}.{name} must be an index or a name, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value.strip() or None
    raise ConfigError(f"{where}.{name} must be an index or a name, got {value!r}")


def _string_list(section: dict[str, Any], name: str, where: str) -> tuple[str, ...]:
    value = section.get(name)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.{name} must be a non-empty list of strings, got {value!r}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{where}.{name} may only contain non-empty strings, got {item!r}")
        items.append(item.strip())
    return tuple(items)


def default_config_path() -> Path:
    """Locate config.yaml: ``$VOICEYT_CONFIG``, then CWD, then the repo root."""
    env = os.environ.get("VOICEYT_CONFIG")
    if env:
        return Path(env)
    candidates = (
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parent.parent / "config.yaml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


# --------------------------------------------------------------------------- #
# building and validating
# --------------------------------------------------------------------------- #


def _build(raw: dict[str, Any], source: Path) -> Config:
    """Validate the merged mapping and turn it into a :class:`Config`."""
    audio_raw = _section(raw, "audio")
    sample_rate = _int(audio_raw, "sample_rate", "audio", minimum=1)
    if sample_rate != REQUIRED_SAMPLE_RATE:
        raise ConfigError(
            f"audio.sample_rate must be {REQUIRED_SAMPLE_RATE} Hz: the VAD and every ASR "
            f"backend assume 16 kHz mono (got {sample_rate})"
        )
    audio = AudioConfig(
        device=_optional_device(audio_raw, "device", "audio"),
        sample_rate=sample_rate,
    )

    aec_raw = _section(raw, "aec")
    aec = AecConfig(
        enabled=_bool(aec_raw, "enabled", "aec"),
        backend=_str(aec_raw, "backend", "aec").lower(),
        loopback_device=_optional_device(aec_raw, "loopback_device", "aec"),
        frame_ms=_int(aec_raw, "frame_ms", "aec", minimum=1),
        stream_delay_ms=_int(aec_raw, "stream_delay_ms", "aec"),
    )
    if aec.backend not in AEC_BACKENDS:
        raise ConfigError(
            f"aec.backend must be one of {', '.join(AEC_BACKENDS)} (got {aec.backend!r})"
        )

    asr_raw = _section(raw, "asr")
    backend = _str(asr_raw, "backend", "asr").lower()
    if backend not in ASR_BACKENDS:
        raise ConfigError(
            f"asr.backend must be one of {', '.join(ASR_BACKENDS)} (got {backend!r})"
        )
    device = _str(asr_raw, "device", "asr").lower()
    if device not in ASR_DEVICES:
        raise ConfigError(f"asr.device must be one of {', '.join(ASR_DEVICES)} (got {device!r})")

    whisper_raw = _section(asr_raw, "whisper")
    parakeet_raw = _section(asr_raw, "parakeet")
    nemotron_raw = _section(asr_raw, "nemotron")

    nemotron_chunk_ms = _int(nemotron_raw, "chunk_ms", "asr.nemotron", minimum=1)
    if nemotron_chunk_ms not in NEMOTRON_CHUNK_MS:
        raise ConfigError(
            f"asr.nemotron.chunk_ms must be one of {NEMOTRON_CHUNK_MS} "
            f"(got {nemotron_chunk_ms}); other sizes need the encoder re-exported"
        )
    nemotron_precision = _str(nemotron_raw, "precision", "asr.nemotron").lower()
    if nemotron_precision not in NEMOTRON_PRECISIONS:
        raise ConfigError(
            f"asr.nemotron.precision must be one of {NEMOTRON_PRECISIONS} "
            f"(got {nemotron_precision!r})"
        )

    asr = AsrConfig(
        backend=backend,
        models_dir=Path(_str(asr_raw, "models_dir", "asr")),
        device=device,
        language=_str(asr_raw, "language", "asr").lower(),
        whisper=WhisperConfig(
            model=_str(whisper_raw, "model", "asr.whisper"),
            compute_type=_str(whisper_raw, "compute_type", "asr.whisper"),
            beam_size=_int(whisper_raw, "beam_size", "asr.whisper", minimum=1),
            condition_on_previous_text=_bool(
                whisper_raw, "condition_on_previous_text", "asr.whisper"
            ),
        ),
        parakeet=ParakeetConfig(
            model_id=_str(parakeet_raw, "model_id", "asr.parakeet"),
            quantization=_optional_str(parakeet_raw, "quantization", "asr.parakeet"),
        ),
        nemotron=NemotronConfig(
            model_id=_str(nemotron_raw, "model_id", "asr.nemotron"),
            chunk_ms=nemotron_chunk_ms,
            precision=nemotron_precision,
            language=_str(nemotron_raw, "language", "asr.nemotron"),
        ),
    )

    vad_raw = _section(raw, "vad")
    threshold = _float(vad_raw, "threshold", "vad")
    if not 0.0 < threshold < 1.0:
        raise ConfigError(f"vad.threshold must be between 0 and 1 (got {threshold})")
    vad = VadConfig(
        enabled=_bool(vad_raw, "enabled", "vad"),
        model_path=Path(_str(vad_raw, "model_path", "vad")),
        threshold=threshold,
        min_speech_ms=_int(vad_raw, "min_speech_ms", "vad", minimum=1),
        min_silence_ms=_int(vad_raw, "min_silence_ms", "vad", minimum=1),
        max_utterance_s=_float(vad_raw, "max_utterance_s", "vad"),
    )
    if vad.max_utterance_s <= 0:
        raise ConfigError("vad.max_utterance_s must be greater than 0")

    trigger_raw = _section(raw, "trigger")
    trigger = TriggerConfig(
        words=_string_list(trigger_raw, "words", "trigger"),
        trigger_window_ms=_int(trigger_raw, "trigger_window_ms", "trigger", minimum=1),
        silence_end_ms=_int(trigger_raw, "silence_end_ms", "trigger", minimum=1),
        query_max_ms=_int(trigger_raw, "query_max_ms", "trigger", minimum=1),
    )

    player_raw = _section(raw, "player")
    volume_min = _int(player_raw, "volume_min", "player")
    volume_max = _int(player_raw, "volume_max", "player")
    if volume_min >= volume_max:
        raise ConfigError(
            f"player.volume_min ({volume_min}) must be below player.volume_max ({volume_max})"
        )
    extra_args = player_raw.get("extra_args") or []
    if not isinstance(extra_args, list) or any(not isinstance(arg, str) for arg in extra_args):
        raise ConfigError("player.extra_args must be a list of strings")
    player = PlayerConfig(
        mpv_path=_str(player_raw, "mpv_path", "player"),
        ipc_path=_optional_str(player_raw, "ipc_path", "player"),
        audio_only=_bool(player_raw, "audio_only", "player"),
        volume_step=_int(player_raw, "volume_step", "player", minimum=1),
        volume_min=volume_min,
        volume_max=volume_max,
        start_timeout_s=_float(player_raw, "start_timeout_s", "player"),
        extra_args=tuple(extra_args),
    )

    search_raw = _section(raw, "search")
    search = SearchConfig(
        results=_int(search_raw, "results", "search", minimum=1),
        cookies_from_browser=_optional_str(search_raw, "cookies_from_browser", "search"),
        socket_timeout_s=_float(search_raw, "socket_timeout_s", "search"),
    )

    behaviour_raw = _section(raw, "behaviour")
    log_level = _str(behaviour_raw, "log_level", "behaviour").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"behaviour.log_level is not a valid level (got {log_level!r})")
    behaviour = BehaviourConfig(
        log_transcripts=_bool(behaviour_raw, "log_transcripts", "behaviour"),
        log_path=Path(_str(behaviour_raw, "log_path", "behaviour")),
        log_level=log_level,
        ignore_trigger_ms_after_play=_int(
            behaviour_raw, "ignore_trigger_ms_after_play", "behaviour"
        ),
        require_same_utterance_while_playing=_bool(
            behaviour_raw, "require_same_utterance_while_playing", "behaviour"
        ),
        ui=(
            _bool(behaviour_raw, "ui", "behaviour")
            if "ui" in behaviour_raw
            else True
        ),
    )

    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, list) or not commands_raw:
        raise ConfigError("'commands' must be a non-empty list of command definitions")
    commands: list[CommandSpec] = []
    seen_actions: set[str] = set()
    seen_verbs: dict[str, str] = {}
    for index, item in enumerate(commands_raw):
        where = f"commands[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping with action/verbs/takes_query")
        action = str(item.get("action") or "").strip()
        if not action:
            raise ConfigError(f"{where}.action must be a non-empty string")
        if action in seen_actions:
            raise ConfigError(f"{where}.action {action!r} is defined twice")
        seen_actions.add(action)
        verbs = item.get("verbs")
        if not isinstance(verbs, list) or not verbs:
            raise ConfigError(f"{where}.verbs must be a non-empty list of strings")
        clean_verbs: list[str] = []
        for verb in verbs:
            if not isinstance(verb, str) or not verb.strip():
                raise ConfigError(f"{where}.verbs may only contain non-empty strings")
            normalized = verb.strip().lower()
            if normalized in seen_verbs:
                raise ConfigError(
                    f"{where}: verb {verb!r} is already used by action "
                    f"{seen_verbs[normalized]!r}"
                )
            seen_verbs[normalized] = action
            clean_verbs.append(normalized)
        takes_query = item.get("takes_query", False)
        if not isinstance(takes_query, bool):
            raise ConfigError(f"{where}.takes_query must be true or false")
        commands.append(
            CommandSpec(action=action, verbs=tuple(clean_verbs), takes_query=takes_query)
        )

    config = Config(
        source=source,
        audio=audio,
        aec=aec,
        asr=asr,
        vad=vad,
        trigger=trigger,
        player=player,
        search=search,
        behaviour=behaviour,
        commands=tuple(commands),
    )
    return _resolve_paths(config)


def _resolve_paths(config: Config) -> Config:
    """Return a copy of *config* with every relative path made absolute."""
    from dataclasses import replace

    asr = replace(config.asr, models_dir=config.resolve(config.asr.models_dir))
    vad = replace(config.vad, model_path=config.resolve(config.vad.model_path))
    behaviour = replace(config.behaviour, log_path=config.resolve(config.behaviour.log_path))
    return replace(config, asr=asr, vad=vad, behaviour=behaviour)


def load_config(config_path: str | Path | None = None, backend: str | None = None) -> Config:
    """Load, merge with defaults and validate the configuration file.

    ``backend`` implements the ``--backend`` override from plan2.md §3 and is
    applied before validation, so an unknown name fails immediately.
    """
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    raw = _deep_merge(DEFAULTS, loaded)
    if backend:
        raw.setdefault("asr", {})["backend"] = backend
    return _build(raw, path.resolve())
