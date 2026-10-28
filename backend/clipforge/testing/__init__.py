"""TEST-ONLY helpers. Imported by production code only when FAKE_MODELS=true.

`synth` makes synthetic "speech" (frequency-coded tone bursts) and fixture videos; `fake_asr`
is a speech-recognition test double that understands only that synthetic speech. Results from
these are always labelled as not real.
"""
