"""Configuration loading, validation, and privacy-safe serialization.

Configuration is stored as TOML and validated with Pydantic. Validation
failures are surfaced as :class:`ConfigError` with human-readable messages
rather than raw stack traces or Pydantic's verbose multi-error dumps — see
:func:`load_config`.

Safety- and privacy-sensitive invariants enforced here (see
IMPLEMENTATION_PLAN.md §3, §11, §21):

* ``paste.auto_submit`` must remain ``false`` in this release. Automatic
  submission (pressing Enter) is never implemented; a config that sets it to
  ``true`` is rejected outright.
* A non-loopback ``cleanup.ollama.base_url`` is rejected unless the user has
  explicitly allowed remote endpoints, so dictated audio/transcripts cannot be
  sent off-machine by a stray config value.
* Transcript logging and debug-audio retention default to ``false``.
"""

from __future__ import annotations

import os
import tomllib
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from platformdirs import user_config_path
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

APP_NAME = "local-flow"

# Modes the pipeline understands. "literal" bypasses LLM cleanup entirely.
Mode = Literal["literal", "standard", "polished"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigError(Exception):
    """A configuration problem stated in human-readable terms.

    Raised instead of leaking a Pydantic ``ValidationError`` or a TOML parse
    traceback to the user. The message is safe to print directly.
    """


class _Section(BaseModel):
    """Base for config sections: reject unknown keys so typos are caught."""

    model_config = ConfigDict(extra="forbid")


class AppConfig(_Section):
    mode: Mode = "standard"
    log_level: LogLevel = "INFO"
    log_transcripts: bool = False
    retain_debug_audio: bool = False
    debug_audio_directory: str = ""
    notify_on_ready: bool = True

    @model_validator(mode="after")
    def _check_debug_audio_dir(self) -> AppConfig:
        # A retention directory is only meaningful, and only needs to be
        # writable, when retention is actually enabled (§11 "Writable debug
        # directory, if enabled").
        if self.retain_debug_audio and self.debug_audio_directory:
            directory = Path(self.debug_audio_directory).expanduser()
            if directory.exists():
                if not directory.is_dir():
                    raise ValueError(
                        f"app.debug_audio_directory '{self.debug_audio_directory}' "
                        "exists but is not a directory"
                    )
                if not os.access(directory, os.W_OK):
                    raise ValueError(
                        f"app.debug_audio_directory '{self.debug_audio_directory}' is not writable"
                    )
        return self


class HotkeysConfig(_Section):
    push_to_talk: str = "<ctrl>+<alt>+space"
    cancel: str = "<esc>"
    toggle_mode: str = "<ctrl>+<alt>+m"

    @model_validator(mode="after")
    def _check_hotkey_syntax(self) -> HotkeysConfig:
        for field_name in ("push_to_talk", "cancel", "toggle_mode"):
            value = getattr(self, field_name)
            _validate_hotkey(f"hotkeys.{field_name}", value)
        return self


class AudioConfig(_Section):
    device: str = ""
    sample_rate: int = 16000
    channels: int = Field(default=1, ge=1, le=2)
    minimum_duration_ms: int = Field(default=250, ge=0)
    maximum_duration_seconds: int = Field(default=180, gt=0)
    trim_silence: bool = True
    vad_backend: Literal["energy", "silero", "none"] = "energy"
    vad_threshold: float = Field(default=0.015, ge=0.0, le=1.0)
    leading_padding_ms: int = Field(default=150, ge=0)
    trailing_padding_ms: int = Field(default=300, ge=0)

    @model_validator(mode="after")
    def _check_audio(self) -> AudioConfig:
        # Whisper-family models expect one of a small set of sample rates;
        # 16 kHz is the norm. Reject values that will silently degrade quality.
        allowed_rates = {8000, 16000, 22050, 24000, 44100, 48000}
        if self.sample_rate not in allowed_rates:
            allowed = ", ".join(str(r) for r in sorted(allowed_rates))
            raise ValueError(
                f"audio.sample_rate {self.sample_rate} is not supported "
                f"(expected one of: {allowed})"
            )
        if self.minimum_duration_ms >= self.maximum_duration_seconds * 1000:
            raise ValueError(
                "audio.minimum_duration_ms must be less than audio.maximum_duration_seconds"
            )
        return self


class TranscriptionConfig(_Section):
    backend: str = "faster-whisper"
    model: str = "small.en"
    model_path: str = ""
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"
    language: str = "en"
    beam_size: int = Field(default=5, ge=1)
    temperature: float = Field(default=0.0, ge=0.0)
    condition_on_previous_text: bool = False
    initial_prompt: str = ""
    download_root: str = ""

    @model_validator(mode="after")
    def _check_model(self) -> TranscriptionConfig:
        if not self.model and not self.model_path:
            raise ValueError("transcription.model or transcription.model_path must be set")
        return self


class TextConfig(_Section):
    spoken_commands_enabled: bool = True
    remove_fillers_in_standard_mode: bool = False
    custom_vocabulary: list[str] = Field(
        default_factory=lambda: [
            "Claude Code",
            "TypeScript",
            "PostgreSQL",
            "middleware.ts",
        ]
    )


class OllamaConfig(_Section):
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    temperature: float = Field(default=0.0, ge=0.0)
    keep_alive: str = "10m"


class CleanupConfig(_Section):
    enabled: bool = False
    provider: Literal["ollama", "noop"] = "ollama"
    mode: Literal["standard", "polished"] = "standard"
    timeout_seconds: float = Field(default=15.0, gt=0.0)
    fallback_to_raw_transcript: bool = True
    # Opt-in escape hatch: allow a non-loopback cleanup endpoint. Off by
    # default so transcripts stay on-machine unless the user says otherwise.
    allow_remote_endpoint: bool = False
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)

    @model_validator(mode="after")
    def _check_endpoint(self) -> CleanupConfig:
        if self.provider == "ollama":
            _validate_cleanup_url(
                "cleanup.ollama.base_url",
                self.ollama.base_url,
                allow_remote=self.allow_remote_endpoint,
            )
        return self


class PasteConfig(_Section):
    method: Literal["clipboard", "type"] = "clipboard"
    terminal_shortcut: str = "auto"
    restore_clipboard: bool = True
    restore_delay_ms: int = Field(default=750, ge=0)
    paste_delay_ms: int = Field(default=100, ge=0)
    append_space: bool = False
    # SAFETY: automatic submission (pressing Enter) is not implemented in this
    # release. The field exists so the schema is stable, but any true value is
    # rejected in the validator below.
    auto_submit: bool = False

    @model_validator(mode="after")
    def _reject_auto_submit(self) -> PasteConfig:
        if self.auto_submit:
            raise ValueError(
                "paste.auto_submit must be false: automatic submission is "
                "experimental and not implemented in this release"
            )
        return self


class NotificationsConfig(_Section):
    sound_enabled: bool = True
    console_enabled: bool = True
    recording_start_sound: str = ""
    recording_stop_sound: str = ""
    success_sound: str = ""
    error_sound: str = ""


class Config(_Section):
    """The complete, validated Local Flow configuration."""

    app: AppConfig = Field(default_factory=AppConfig)
    hotkeys: HotkeysConfig = Field(default_factory=HotkeysConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    paste: PasteConfig = Field(default_factory=PasteConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    def effective(self) -> dict[str, Any]:
        """Return a privacy-safe view of the effective configuration.

        Fields that could carry user content or machine-identifying detail —
        the custom vocabulary and the debug-audio directory path — are
        redacted so the result is safe to print or log (see §21). Their
        presence is still indicated so the user knows they are set.
        """
        data = self.model_dump(mode="json")
        vocab = data["text"].get("custom_vocabulary") or []
        data["text"]["custom_vocabulary"] = f"<{len(vocab)} term(s) hidden>"
        if data["app"].get("debug_audio_directory"):
            data["app"]["debug_audio_directory"] = "<set; hidden>"
        return data

    def with_mode(self, mode: str) -> Config:
        """Return a copy with ``app.mode`` overridden, re-validated.

        Goes through full validation (unlike ``model_copy(update=...)``), so an
        invalid mode raises :class:`ConfigError` with a readable message rather
        than being silently accepted.
        """
        data = self.model_dump(mode="python")
        data["app"]["mode"] = mode
        return load_config_from_dict(data)


# --- Validation helpers -----------------------------------------------------

# Named special keys accepted as bare tokens in a hotkey (pynput also accepts
# these unbracketed). Not exhaustive; the hotkey backend does final resolution.
_NAMED_KEYS = frozenset(
    {
        "space",
        "tab",
        "enter",
        "return",
        "esc",
        "escape",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "up",
        "down",
        "left",
        "right",
        "page_up",
        "page_down",
    }
)


def _validate_hotkey(field: str, value: str) -> None:
    """Validate pynput-style hotkey syntax without importing pynput.

    Accepts sequences of ``+``-joined tokens where each token is one of:

    * a ``<bracketed>`` modifier or key name (``<ctrl>``, ``<alt>``, ``<f5>``),
    * a bare named special key (``space``, ``tab``, ``enter``, ``esc``, ...), or
    * a single printable character (``a``, ``m``, ``1``).

    This is a lightweight syntactic check; the real key names are resolved by
    the hotkey backend in a later phase.
    """
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty")
    tokens = [t.strip() for t in value.split("+")]
    for token in tokens:
        if not token:
            raise ValueError(f"{field} '{value}' has an empty key between '+' separators")
        bracketed = token.startswith("<") and token.endswith(">")
        single_char = len(token) == 1
        named = token.lower() in _NAMED_KEYS
        if not (bracketed or single_char or named):
            raise ValueError(
                f"{field} '{value}': token '{token}' is not a single character, "
                "a <bracketed> key name, or a known special key"
            )
        if bracketed and len(token) <= 2:
            raise ValueError(f"{field} '{value}': token '{token}' has no key name")


def _validate_cleanup_url(field: str, value: str, *, allow_remote: bool) -> None:
    """Validate a cleanup endpoint URL and enforce the loopback-only default.

    A non-loopback host is rejected unless ``allow_remote`` is set, so a stray
    config value cannot silently ship transcripts to a remote service (§21).
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} '{value}' must be an http(s) URL")
    host = parsed.hostname
    if not host:
        raise ValueError(f"{field} '{value}' has no host")
    if allow_remote:
        return
    if not _is_loopback(host):
        raise ValueError(
            f"{field} '{value}' is not a loopback address; set "
            "cleanup.allow_remote_endpoint = true to allow a remote endpoint"
        )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        # A non-numeric, non-"localhost" host name is treated as remote.
        return False


# --- Paths, loading, saving -------------------------------------------------


def default_config_path() -> Path:
    """Return the platform-appropriate default config file path.

    Uses :func:`platformdirs.user_config_path`, which resolves to
    ``~/Library/Application Support/local-flow`` on macOS,
    ``~/.config/local-flow`` on Linux, and ``%APPDATA%\\local-flow`` on
    Windows (see §11).
    """
    return user_config_path(APP_NAME, appauthor=False) / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load and validate configuration from ``path`` (or the default path).

    A missing file yields the default configuration. Parse and validation
    errors are re-raised as :class:`ConfigError` with a readable message.
    """
    config_path = path or default_config_path()
    if not config_path.exists():
        return Config()

    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"could not read config file {config_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    return _validate(data, source=str(config_path))


def load_config_from_dict(data: dict[str, Any]) -> Config:
    """Validate an in-memory config mapping (used by tests and overrides)."""
    return _validate(data, source="<config>")


def _validate(data: dict[str, Any], *, source: str) -> Config:
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc


def _format_validation_error(exc: ValidationError, source: str) -> str:
    """Turn a Pydantic ``ValidationError`` into readable, safe lines."""
    lines = [f"invalid configuration in {source}:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def render_toml(config: Config) -> str:
    """Render ``config`` as a TOML document.

    Written by hand (the standard library has no TOML *writer*) so we avoid a
    dependency purely for serialization and keep control over key order and
    comments.
    """
    return _render_toml(config.model_dump(mode="python"))


def _render_toml(data: dict[str, Any]) -> str:
    scalars: list[str] = []
    tables: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append(_render_table(key, value))
        else:
            scalars.append(f"{key} = {_toml_value(value)}")

    parts: list[str] = []
    if scalars:
        parts.append("\n".join(scalars))
    parts.extend(tables)
    return "\n\n".join(parts) + "\n"


def _render_table(name: str, table: dict[str, Any]) -> str:
    lines = [f"[{name}]"]
    subtables: list[str] = []
    for key, value in table.items():
        if isinstance(value, dict):
            subtables.append(_render_table(f"{name}.{key}", value))
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    block = "\n".join(lines)
    if subtables:
        block = block + "\n\n" + "\n\n".join(subtables)
    return block


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        items = ", ".join(_toml_value(item) for item in value)
        return f"[{items}]"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML")
