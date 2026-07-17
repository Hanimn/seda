# Wispr Flow: Real-Time Transcription & Text-Insertion Behavior

Research date: 2026-07-17. Sources are overwhelmingly from Wispr Flow's own Help Center
(`docs.wisprflow.ai`), Security & Compliance FAQ, and marketing site — i.e. primary sources.
Where a claim rests on inference from those docs rather than an explicit statement, it is marked
`[inferred]`. Explicit statements are `[sourced]`. Open questions are `[uncertain]`.

> Note on terminology: the product is **Wispr Flow** by **Wispr AI, Inc.** (a distinct company;
> not to be confused with OpenAI's *Whisper* model, though people often mistype it "Whisper Flow").

---

## Summary for our design (most relevant bullets)

- **Wispr Flow does NOT type live/incrementally into the target app.** On desktop it records the
  whole utterance, sends the audio to the cloud, transcribes + AI-formats it, and then inserts the
  finished text **once** when processing completes. The on-screen "Flow Bar" is a **status/waveform
  bubble**, not a live text preview. This means Wispr sidesteps the "revise-words-live" tension
  entirely by **not showing words live in the app at all** — it is effectively a
  **batch-per-utterance** design with a "type-once-when-final" insertion model. `[sourced/inferred]`
- **Insertion mechanism differs by platform:** desktop (Mac/Windows) = **clipboard paste** (then it
  restores your old clipboard); iOS = a **custom keyboard** that inserts directly; Android = an
  **accessibility-API** direct insertion (the "Flow Bubble") with clipboard fallback. All desktop
  insertion requires the OS **Accessibility** permission. `[sourced]`
- **Self-corrections ("2… actually 3") are resolved in the transcript before insertion**, using the
  *full utterance* as context — not by backspacing already-typed text in the app. So relative to
  the tool's A1/A2/A3 options, Wispr is closest to **A3 in spirit** (type the polished/final version
  once) but implemented as **"never type until final"** — it never has to correct in-app text because it
  never inserts provisional text. `[sourced]`
- **A cloud LLM "polish" pass is core, on by default** ("Smart Formatting" / "Auto Cleanup"):
  removes filler/disfluencies, fixes punctuation/capitalization, builds lists, applies tone. The
  **polished** version is what gets inserted; the raw transcript is recoverable via "Undo AI edit".
  `[sourced]`
- **It's cloud-only and requires internet** — "Flow requires an internet connection for voice
  transcription"; audio is streamed to a US cloud backend. No on-device/offline mode. `[sourced]`
- **No evidence it auto-presses Enter / auto-submits.** "Press enter" appears to be an explicit
  *spoken command* (there was a bug where saying only "press enter" left a stray period). Inserted
  text is normal editable text in the field. For our safety property, Wispr is a good precedent:
  it commits text, it does not submit forms. `[sourced/inferred]`

---

## 1. Live display model

**Words do NOT appear live/incrementally in the target app as you speak.** On desktop, Flow records
the full utterance while you hold the hotkey, uploads the audio, transcribes + formats server-side,
and then pastes the finished text into the focused field in one shot when processing finishes.

- Marketing copy says words appear "in real time" and "as you speak" with "no noticeable delay,"
  but this is a *latency* claim, not an incremental-token-streaming-into-your-editor claim. The
  Help Center's operational description is: "press the hotkey, speak … your words appear as text
  wherever your cursor is." `[sourced]` — https://docs.wisprflow.ai/articles/2772472373-what-is-flow
- The desktop UI element is the **Flow Bar**: "the small bubble that shows your dictation status …
  The waveform, progress indicator, pickers, and tooltips …" — i.e. a **status HUD with a waveform
  and a progress/processing indicator**, not a live transcript preview. `[sourced]`
  https://docs.wisprflow.ai/articles/1790396454-move-and-dock-the-flow-bar-on-desktop
- The "Taking longer than usual" notification "dismisses itself as soon as your text is pasted,"
  and appears "during the Initializing, Stopping, or Processing states" — confirming text is pasted
  at the *end* of a processing step, not streamed word-by-word. `[sourced]`
  https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors

**Answer to (a) vs (b):** There is **no live text preview in the target app**, and the desktop
overlay is a **status bubble (b-style), not a text preview**. Final text goes **directly into the
target application** (a) only once it is finalized. `[inferred from the above]`

## 2. Insertion mechanism

**Platform-dependent, and explicitly documented:**

- **Mac & Windows — clipboard paste.** "On Mac and Windows, Flow temporarily uses your clipboard to
  paste text and restores your previous contents after a successful paste. If paste fails, Flow
  leaves your dictated text on the clipboard." Requires the **Accessibility** permission
  (System Settings → Privacy & Security → Accessibility). It pastes "into whichever app is focused
  when transcription finishes." `[sourced]`
  https://docs.wisprflow.ai/articles/7971211038-fix-text-not-pasting-after-dictation
- **iOS — custom keyboard, direct insertion.** "On iOS, Flow works as a keyboard. Text appears
  directly in the text field without using the clipboard." Requires "Allow Full Access." `[sourced]`
  (same article)
- **Android — accessibility API, direct insertion with clipboard fallback.** "On Android, Flow uses
  the Flow Bubble to deliver transcribed text. Flow inserts text directly in most … apps … If
  insertion can't be verified, the Flow Bubble shows a copy button." Uses "overlay or accessibility
  permissions." `[sourced]` (same article)
- **OS vs. own injection:** It uses its **own injection** layered on OS permissions (clipboard +
  Accessibility on desktop; a third-party keyboard extension on iOS; the Accessibility service on
  Android). It does **not** appear to use Apple/Windows built-in *dictation* APIs — the whole audio
  path is Wispr's own cloud ASR. `[sourced/inferred]`
- Corroborating quote: "Users speak, and formatted text is inserted into whatever application is
  active." `[sourced]` https://docs.wisprflow.ai/articles/3467817258-security-and-compliance-faq

## 3. Revision / correction behavior

**Wispr does not rewrite already-inserted text in the app.** Because it inserts only the final,
already-cleaned transcript (see Q1), there is no in-app backspacing/retyping of provisional words.
Self-corrections are resolved *in the transcript* before insertion, using the whole utterance:

- **"Backtrack"** examples: You say "Let's do coffee at 2 actually 3." → Flow writes "Let's do
  coffee at 3." And "I wanted to buy a record as a gift… as a present." → "…as a present." `[sourced]`
  https://docs.wisprflow.ai/articles/5373093536-how-do-i-use-smart-formatting-and-backtrack
- "Flow uses your **full dictation as context** and removes only clear self-corrections." `[sourced]`
  (same article) — this is utterance-level (batch) reasoning, applied once.
- Smart Formatting "does not attempt to correct misheard words"; it handles "grammar, punctuation,
  capitalization, and disfluency cleanup." `[sourced]` (same article)
- The classic streaming ambiguity (e.g. "to" → "two") is therefore never surfaced to the user as
  flickering in-app text, because the app only ever sees the finalized string. `[inferred]`

## 4. Streaming vs. batch transcription

**Evidence points to batch-per-utterance cloud transcription, not incrementally-displayed streaming
ASR.**

- Audio is **uploaded to the cloud** and processed there: the "Taking longer than usual"
  notification exists precisely because "Flow's servers need extra time to process your audio";
  disabling VPN/firewall and having a working internet connection are troubleshooting steps.
  `[sourced]`
  https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors
- Sessions are submitted as a unit: "At 20 minutes, the session automatically stops and your
  transcription is submitted." And offline: "Flow tries to transcribe the audio it already
  captured." `[sourced]` (same article)
- Audio is **streamed to the backend** during recording but the *result* is delivered per session:
  "Audio recordings (audio is streamed to the backend and not persisted locally)." So there may be
  streaming *upload*, but the user-visible output is a single finalized insertion. `[sourced]`
  https://docs.wisprflow.ai/articles/3467817258-security-and-compliance-faq
- **Architecture / model:** "Wispr Flow runs entirely in the cloud and is delivered as multi-tenant
  SaaS, hosted with a major US cloud provider. There is no on-premise deployment." It is **cloud,
  not on-device**, and requires internet: "No. Flow requires an internet connection for voice
  transcription." `[sourced]`
  https://docs.wisprflow.ai/articles/2772472373-what-is-flow
- **Exact ASR model is not disclosed.** Public docs never name Whisper or a specific streaming
  model. Wispr uses its **own models** ("used to evaluate, train, and improve Wispr's models") and
  a separate AI **formatting/rewrite** layer. Whether the ASR is Whisper-derived, a proprietary
  streaming model, or a third-party API is **not stated**. `[uncertain]` (model identity) /
  `[sourced]` (that they run/train their own models — Security FAQ, "Do you use customer data to
  train AI models?")

## 5. The "final polish" step

**Yes — a cloud AI cleanup/rewrite pass is central and on by default.**

- **Smart Formatting / Auto Cleanup** "removes filler words by design," fixes punctuation and
  capitalization, and builds lists; "To get raw transcription, turn off Smart Formatting." `[sourced]`
  https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors
- Marketing: "AI Auto Edits … Rambled thoughts become clear, perfectly formatted text, without the
  filler words or typos." `[sourced]` https://wisprflow.ai
- The pipeline is explicitly staged in the Security FAQ: "the captured audio, any screen context …,
  the **speech-to-text output**, the **formatted result**, and any downstream **AI-rewritten
  variants**." So: ASR → formatting → optional rewrite. `[sourced]`
  https://docs.wisprflow.ai/articles/3467817258-security-and-compliance-faq
- **Does polish replace live text?** There is no live text to replace — the **polished version is
  the thing that gets inserted** (single insertion). The **raw** transcript is preserved and
  recoverable: "Your original dictation is never lost … recover it … using Undo AI edit." So it is
  *replace-at-source-before-insert*, not *insert-then-replace-in-app*. `[sourced]`
  https://docs.wisprflow.ai/articles/5373093536-how-do-i-use-smart-formatting-and-backtrack
- Additional layers exist: **Command Mode** (spoken commands rewrite highlighted text in place),
  **Polish Shortcuts / Transforms**, **Writing/Personalization Styles** (tone per app category),
  and **Context Awareness** (reads the app's accessibility tree / optional screen OCR to improve
  formatting). `[sourced]`
  https://docs.wisprflow.ai/articles/4816967992-how-to-use-command-mode ,
  https://docs.wisprflow.ai/articles/4678293671-feature-context-awareness

## 6. Endpointing / when it commits

**Push-to-talk (hold a key) is the core model, with a tap/toggle option; text commits after the
recording ends and cloud processing completes.**

- Default hotkey: "On Mac, press the **fn** key. On Windows, press **Ctrl + Win**." `[sourced]`
  https://docs.wisprflow.ai/articles/2772472373-what-is-flow
- Both modes exist: docs distinguish **"hold-to-talk"** from tapping a **shortcut** (e.g. fn+space),
  and note "Hold-to-talk keeps working while shortcuts are blocked." `[sourced]`
  https://docs.wisprflow.ai/articles/8841649969-fix-flow-shortcuts-blocked-by-macos-secure-keyboard-entry-secure-event-input
- Command Mode is explicitly PTT: "Press and hold the shortcut, speak your command, then release."
  `[sourced]` https://docs.wisprflow.ai/articles/4816967992-how-to-use-command-mode
- The commit trigger is **releasing the hotkey / stopping the session**, then server processing;
  the Android bubble shows "a Cancel button … shortly after you release the hotkey," and a loading
  spinner "during push-to-talk processing." Insertion happens when processing finishes. `[sourced]`
  https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors
- So endpointing is primarily **hotkey-release**, not silence detection, though a hard 20-minute cap
  auto-stops long sessions. `[sourced]` (same article)

## 7. The "never submit" concern

**No evidence Wispr auto-presses Enter or auto-submits.** Inserted text lands as ordinary editable
text in the focused field; the user submits manually.

- Strong tell: a bug fix reads "A stray period appears when saying only 'press enter' … No extra
  text is pasted when 'press enter' is the entire dictation." This implies **"press enter" is an
  explicit spoken command**, i.e. Enter is only sent when the user *asks* for it — it is not an
  automatic behavior after every dictation. `[sourced/inferred]`
  https://docs.wisprflow.ai/articles/7971211038-fix-text-not-pasting-after-dictation
- Everything about the insertion flow (clipboard paste, "paste last transcript," manual Cmd+V
  fallback, editable field required) describes depositing editable text, never a submit action.
  `[sourced]` (same article)
- No official doc claims an auto-send/auto-submit feature. `[uncertain — absence of evidence]`

## 8. The fundamental tension (live words vs. a batch model wanting to revise)

**Wispr avoids the tension by not showing words live in the app.** It records the full utterance,
does batch (per-utterance) cloud ASR + AI formatting, and inserts the finished string once. Because
the target application never receives provisional tokens, there is nothing to flicker or rewrite in
place; self-corrections and "to→two"-style ambiguities are resolved server-side using full-utterance
context before a single insertion. `[sourced/inferred, from Q1/Q3/Q4/Q5]`

- There is **no public evidence that Wispr uses a low-latency streaming ASR that emits stable
  partial tokens into your editor.** The visible UX is "record → process → insert once." The only
  "live" element is the **status/waveform bubble**, plus AI-command overlays. `[inferred]`
- Consequence for our tool: Wispr is a precedent for **"don't type until final"** rather than
  "type-then-correct." If we want *visible* live words during speech, Wispr does not demonstrate a
  solved approach for that — it chose to hide the transcript until it's stable. `[inferred]`

### Mapping to our A1/A2/A3 decision

- **A1 (type-only-never-correct):** Not Wispr's model; Wispr would produce a wrong first draft if it
  typed live and never corrected, which is exactly why it waits.
- **A2 (type-and-backspace-correct):** No evidence Wispr does in-app backspacing of provisional
  text. It does not appear to correct already-inserted text.
- **A3 (type-live-then-replace-with-polished):** Closest in *outcome* (the inserted text is the
  polished/final version), but Wispr's implementation is really **"A0: never type until final"** —
  it never shows a provisional draft in the app, then inserts the polished result once. This is the
  cleanest way to dodge the revise-live problem, at the cost of no live in-app feedback (only a
  status HUD). `[inferred]`

---

## Sources

- Wispr Flow marketing site — https://wisprflow.ai
- Help Center: "What is Flow?" — https://docs.wisprflow.ai/articles/2772472373-what-is-flow
- Help Center: "Fix text not pasting after dictation" (insertion mechanism, clipboard/keyboard/accessibility, "press enter") — https://docs.wisprflow.ai/articles/7971211038-fix-text-not-pasting-after-dictation
- Help Center: "Fix 'Taking longer than usual' and transcription errors" (cloud processing, submit-on-stop, Smart Formatting removes fillers, PTT processing) — https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors
- Help Center: "How do I use Smart Formatting & Backtrack" (self-correction, full-utterance context, Undo AI edit) — https://docs.wisprflow.ai/articles/5373093536-how-do-i-use-smart-formatting-and-backtrack
- Help Center: "Move and Dock the Flow Bar on Desktop" (status/waveform bubble, not a text preview) — https://docs.wisprflow.ai/articles/1790396454-move-and-dock-the-flow-bar-on-desktop
- Help Center: "Fix Flow shortcuts blocked by macOS Secure Keyboard Entry" (hold-to-talk vs. shortcut modes) — https://docs.wisprflow.ai/articles/8841649969-fix-flow-shortcuts-blocked-by-macos-secure-keyboard-entry-secure-event-input
- Help Center: "How to use Command Mode" (PTT, replace-selected-text) — https://docs.wisprflow.ai/articles/4816967992-how-to-use-command-mode
- Help Center: "Context Awareness" (reads accessibility tree / optional screen OCR to improve formatting) — https://docs.wisprflow.ai/articles/4678293671-feature-context-awareness
- Security & Compliance FAQ (cloud-only SaaS, audio streamed to backend, staged STT→formatted→AI-rewritten pipeline, own models/training) — https://docs.wisprflow.ai/articles/3467817258-security-and-compliance-faq
- Understanding Privacy Mode & Cloud Sync — https://docs.wisprflow.ai/articles/4709791908-understanding-privacy-mode-and-cloud-sync
