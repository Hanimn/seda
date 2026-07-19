# Seda Brand Specification

> **Seda** (صدا) — Persian for "voice"

## Brand Essence

Local-first voice dictation for developers. Terminal-native, privacy-focused, no cloud required.

## Palette (OKLch)

| Token | Role | OKLch | Hex |
|-------|------|-------|-----|
| `--bg` | Background | `oklch(15% 0.01 250)` | `#0d1117` |
| `--surface` | Cards, containers | `oklch(20% 0.01 250)` | `#161b22` |
| `--fg` | Primary text | `oklch(90% 0 0)` | `#e6edf3` |
| `--muted` | Secondary text | `oklch(60% 0.01 250)` | `#7d8590` |
| `--border` | Borders, dividers | `oklch(30% 0.01 250)` | `#30363d` |
| `--accent` | Voice/active states | `oklch(70% 0.15 190)` | `#2dd4bf` |

### Accent Usage

The teal accent (`--accent`) represents voice activity — use it for:
- Waveform visualization
- Recording/active states
- Primary CTAs
- Logo mark highlight

Budget: **at most twice per screen** outside the logo.

## Typography

| Role | Stack |
|------|-------|
| Display / Headings | `"JetBrains Mono", "SF Mono", "Fira Code", ui-monospace, monospace` |
| Body | `"JetBrains Mono", "SF Mono", ui-monospace, monospace` |
| Code | `"JetBrains Mono", "SF Mono", ui-monospace, monospace` |

All text is monospace to reinforce the terminal aesthetic.

## Logo Mark

The Seda logo is a stylized audio waveform contained within terminal brackets `[ ]`:

```
[ ▁▃▅▇▅▃▁ ]
```

- Waveform bars use the accent teal
- Brackets use foreground color (light on dark, dark on light)
- Minimum clear space: 0.5× the bracket height on all sides

### Logo Files

| File | Use |
|------|-----|
| `logo.svg` | Primary logo (dark background) |
| `logo-light.svg` | Light background variant |
| `icon.svg` | Favicon / small contexts (waveform only) |

## Layout Posture

- **Corner radius:** `2px` — sharp, terminal-native
- **Borders:** `1px solid var(--border)`
- **Spacing:** 8px baseline grid
- **Code blocks:** `var(--surface)` background with `var(--border)` outline

## Voice & Tone

| Do | Don't |
|----|-------|
| "Local-first" | "Privacy-first" (overused) |
| "No cloud required" | "Your data stays safe" |
| "Runs offline" | "Secure and private" |
| Technical, direct | Marketing fluff |

## Badge Style

Badges use the brand palette:
- Background: `--surface` (#161b22)
- Text: `--fg` (#e6edf3)  
- Accent labels: `--accent` (#2dd4bf)
