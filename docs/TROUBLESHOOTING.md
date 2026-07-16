# Troubleshooting

## No microphone detected

Run `local-flow devices` to list input devices. If the list is empty:

- Check that your microphone is connected and not muted at the OS level.
- On macOS: System Settings → Privacy & Security → Microphone — ensure Terminal (or your terminal app) is allowed.
- On Linux: check that `pulseaudio` or `pipewire` is running and your user is in the `audio` group.
- Try `local-flow test-mic` to verify levels before starting the dictation loop.

## Model download failure

Run `local-flow models download small.en`. If it fails:

- Check your internet connection.
- Confirm you have write access to the model cache directory (`local-flow config show-effective` shows `download_root`).
- Proxy environments: set `HTTPS_PROXY` / `HTTP_PROXY` environment variables before running the command.
- Hugging Face may rate-limit; wait and retry.

## Offline model missing

If you see "model not available" when starting the app:

```bash
local-flow models download small.en   # or your configured model
```

The model name must match `transcription.model` in your config. Run `local-flow models list-local` to see what's cached.

## Slow CPU transcription

CPU transcription is expected to be slower than GPU. Typical targets (not hard requirements):

- Short utterance (< 5 s audio): under 5 s on a modern CPU.
- Improve latency by using `base.en` instead of `small.en`, or by enabling CUDA if available.

Check `local-flow doctor` for CUDA status. On Apple Silicon, `faster-whisper` runs on CPU; an MLX backend is a planned future enhancement.

## CUDA not detected

`local-flow doctor` reports CUDA status. If not detected:

- Ensure `ctranslate2` was built with CUDA support (check `pip show ctranslate2`).
- Set `transcription.compute_type = "auto"` in your config — the backend will select the best available compute type.
- CUDA requires compatible NVIDIA drivers and a matching version of `ctranslate2`.

## macOS accessibility permission

Global hotkeys require the Accessibility permission on macOS (System Settings → Privacy & Security → Accessibility). Without it:

- `pynput` cannot register global hotkeys, and `local-flow run` will fail to detect the push-to-talk key.
- If you see no response when holding the configured hotkey, check Accessibility permissions for your terminal application.
- You may also need Input Monitoring permission depending on your terminal.

Do not grant Accessibility permission to untrusted applications.

## Hotkey conflicts

If the configured hotkey does not trigger dictation:

- Another application may have registered the same global hotkey. Common conflicts: screenshot tools (`cmd+shift+5`), password managers, window managers.
- Change `hotkeys.push_to_talk` in your config to a less-common combination.
- Run `local-flow doctor` — the `Global hotkeys` check will report if `pynput` is importable.

## Paste shortcut mismatch

If text appears on the clipboard but is not inserted at the cursor:

- The configured paste shortcut may not match the active application. Check `paste.shortcut_macos` / `paste.shortcut_windows` / `paste.shortcut_linux_gui`.
- Some terminal emulators use `ctrl+shift+v` instead of `ctrl+v`. Add an application override:
  ```toml
  [[paste.application_overrides]]
  application = "gnome-terminal"
  shortcut = "ctrl+shift+v"
  ```
- Use `local-flow run --no-paste` to copy only; paste manually to confirm the transcript is correct.

## Wayland limitations

On Wayland compositors, global hotkeys and simulated input may not work because the compositor restricts cross-application input access.

Options:
- Configure a global shortcut in your compositor that invokes `local-flow` directly via the CLI.
- Use X11 (XWayland) mode if your compositor supports it.
- Use `local-flow transcribe FILE` for file-based transcription instead of live dictation.

`local-flow doctor` reports the detected session type.

## Clipboard not restored

The prior clipboard is restored only when the clipboard still holds the inserted transcript after paste. It is intentionally not restored when:

- You copied something new while the dictation was processing.
- The prior clipboard held non-text content (images, files).
- Restoration is disabled (`paste.restore_clipboard = false`).
- A paste failure occurred (the transcript is left on the clipboard instead).

## Ollama unavailable

If you have enabled cleanup (`cleanup.enabled = true`) and `local-flow doctor` reports Ollama unreachable:

```bash
ollama serve                         # start Ollama
ollama pull qwen2.5:3b               # download the configured model
```

Confirm the base URL matches your Ollama instance: `cleanup.ollama.base_url` defaults to `http://127.0.0.1:11434`. Cleanup is fail-open: if Ollama is unreachable, the deterministic transcript is used.

## Cleanup returning unwanted content

If the LLM cleanup stage returns answers or code instead of cleaned prose:

- The validation layer rejects obvious assistant prefaces and answers — if they appear, the raw transcript was used.
- Use `local-flow run --no-cleanup` to disable cleanup for a session.
- Try a different model (`cleanup.ollama.model`): small instruction models can misinterpret technical prompts. `qwen2.5:3b` is the recommended default.
- Switch to `app.mode = "literal"` to bypass cleanup entirely and preserve all technical names exactly.

## Technical names transcribed incorrectly

The transcription model may mishear technical names (e.g. `AppController` → "app controller", `middleware.ts` → "middleware dot t s"). Mitigations:

- Add the terms to `text.custom_vocabulary` — they are passed as transcription hints.
- Use spoken commands for paths: `src slash auth slash middleware dot ts`.
- Use `app.mode = "literal"` to prevent stylistic changes to the transcript.

## Recording-start sound captured by microphone

If you hear feedback or the recording-start sound appears at the start of the transcript:

- Use headphones or a directional microphone.
- Disable `notifications.sound_enabled` to suppress audio feedback.
- Increase `audio.leading_padding_ms` to skip the first milliseconds of recording (captures the start of speech, not the notification sound).
