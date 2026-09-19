"""Vendored third-party code, kept byte-for-byte and clearly attributed.

nemotron_onnx_streaming.py
    Streaming inference engine for the Nemotron 3.5 ASR ONNX export.
    Source: github.com/codavidgarcia/nemotron-3.5-asr-streaming-onnx
            (engine/nemotron_onnx_streaming.py)
    Licence: Apache-2.0 for the code (see LICENSE-nemotron-onnx-engine); the
             model weights it loads stay NVIDIA's under OpenMDW-1.1.

It is imported only by ``voiceyt.asr.nemotron``; none of the other backends
depend on it, so the file can be deleted if the Nemotron backend is not wanted.
"""
