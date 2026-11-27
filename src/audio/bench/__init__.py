"""Speech-to-text benchmark: ``python -m src.audio.bench`` (docs/asr-benchmark.md).

Runs each installed candidate engine on the same 16 kHz clips, each in a
disposable process on a free loopback port, and writes one Markdown table.
A candidate that is not installed gets a ``not installed`` row with no
numbers. Nothing here starts, stops or signals the hub's own backends.
"""
