# Using Seda with Claude Code

Seda is optimized for dictating prompts into Claude Code and similar terminal-based AI assistants. This guide covers setup, prompt styles, and safe usage patterns.

## Setup

1. Install and configure Seda (see README).
2. Start Claude Code in your terminal.
3. Click inside the Claude Code input to focus it.
4. Start the dictation loop in a separate terminal:
   ```bash
   seda run
   ```
5. Hold the push-to-talk hotkey, dictate your prompt, release.

The transcript appears at the cursor, **editable before you submit**. Seda never presses Enter. Review and submit when ready.

## Prompt styles

### Normal prose prompt

Dictate naturally — Seda cleans up punctuation and removes filler words in standard mode.

**Dictation:**
> "Review the authentication middleware and explain why refresh tokens are rejected. Do not modify files yet."

**Result:**
> Review the authentication middleware and explain why refresh tokens are rejected. Do not modify files yet.

### Technical prompt with paths and identifiers

Use spoken commands for file paths, operators, and symbols. They are replaced deterministically — no LLM involved.

**Dictation:**
> "Literal mode inspect src slash auth slash middleware dot t s and compare refresh token max age with config slash auth dot toml"

**Result:**
> Inspect src/auth/middleware.ts and compare refresh token max age with config/auth.toml

The `literal mode` prefix at the start bypasses all stylistic cleanup, preserving technical terms exactly.

### Structured multi-step prompt

Use `new line` and `new paragraph` for structure.

**Dictation:**
> "Polished mode first inspect the failing tests new line second identify the root cause new line third propose the smallest fix new paragraph do not edit files until I approve"

**Result:**
> 1. Inspect the failing tests.
> 2. Identify the root cause.
> 3. Propose the smallest fix.
>
> Do not edit files until I approve.

> **Note:** Numbered list formatting is produced by the LLM cleanup stage (polished mode), not by spoken commands. Without cleanup enabled, the spoken `new line` / `new paragraph` commands produce plain line breaks.

## Processing modes

| Mode | Use for |
|---|---|
| `literal` | Technical paths, identifiers, code snippets — no stylistic changes |
| `standard` | Normal prompts — conservative punctuation cleanup, no filler injection |
| `polished` | Longer prose — paragraph breaks, readability improvements, filler removed |

Set the mode per-dictation by speaking it at the start: `"Literal mode ..."` or `"Polished mode ..."`. Or set a default in config: `app.mode = "standard"`.

## Safety with Claude Code

Because Claude Code can execute commands, keep these behaviours in mind:

- **Seda never presses Enter.** Dictated text appears at the cursor, editable. You control submission.
- **Use literal mode for file paths.** Standard and polished modes may alter whitespace or punctuation in technical content.
- **Review before submitting.** Multi-step instructions with `new line` produce literal newlines — Claude Code interprets these as structured prompts, not multiple commands.
- **Cancel a dictation** by speaking `"scratch that"` or `"cancel dictation"` at the start of your utterance.

## Multiline input

Claude Code's terminal handles multiline paste as a single input block (not separate commands). Seda sets `paste.multiline_policy = "preserve"` by default, which keeps newlines intact. If you experience unexpected execution, switch to `"flatten"`:

```toml
[paste]
multiline_policy = "flatten"  # converts newlines to spaces
```

Or use `"copy_only"` to paste manually after reviewing.

## Custom vocabulary for Claude Code

Add common terms to `text.custom_vocabulary` so the transcription model recognizes them:

```toml
[text]
custom_vocabulary = [
  "Claude Code",
  "TypeScript",
  "middleware.ts",
  "PostgreSQL",
  "RLHF",
]
```

Vocabulary terms are passed as transcription hints only — they are not sent to any network service.

## Known limitations with Claude Code

- **Bracketed paste**: some terminal configurations interpret multiline paste differently. If lines are submitted individually rather than as a block, set `multiline_policy = "flatten"` or configure your terminal's bracketed-paste setting.
- **Long prompts**: very long dictations (> 30 s audio) may take several seconds to transcribe on CPU. Consider breaking long prompts into shorter dictations.
- **Wayland**: global hotkeys may not work on Wayland compositors. See `docs/TROUBLESHOOTING.md`.
