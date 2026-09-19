"""voiceyt - voice-controlled YouTube playback for European Portuguese.

Implementation of plan2.md.  One process, no web server, no LLM:

    microphone -> VAD -> ASR -> normalize -> matcher -> yt-dlp/mpv

Command words, timings, paths and model ids all come from ``config.yaml``.
"""

__version__ = "0.1.0"