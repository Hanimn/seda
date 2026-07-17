"""GUI overlay support (macOS only).

This package hosts the macOS live-recording waveform overlay (epic #15). Per
ADR-0001, on macOS a GUI host owns the main thread and the AppKit run loop and
*drives* the :class:`~local_flow.app.AppController`; on every other platform, or
when AppKit is unavailable, the app falls back to the controller's own blocking
``run()`` with no behavior change (fail-open).
"""
