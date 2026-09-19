"""Offline unit tests for voiceyt.

Run with:  .\.venv\Scripts\python.exe -m unittest discover -s tests -v

Nothing here needs a microphone, a GPU, mpv or model files: the tests cover
normalization, the trigger state machine, configuration validation and journal
replay, which are the parts where a silent regression would otherwise only show
up in a live session.
"""
