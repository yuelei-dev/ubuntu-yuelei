#!/usr/bin/env python3
"""Generate a short original instrumental WAV for text-led social videos."""

from __future__ import annotations

import argparse
import math
import wave
from pathlib import Path

import numpy as np


SAMPLE_RATE = 48_000


def envelope(t: np.ndarray, attack: float, decay: float) -> np.ndarray:
    return np.minimum(1.0, t / max(attack, 1e-6)) * np.exp(-t / decay)


def add_tone(buf, start, length, freq, amp, kind="sine"):
    i0 = max(0, int(start * SAMPLE_RATE))
    i1 = min(len(buf), int((start + length) * SAMPLE_RATE))
    if i1 <= i0:
        return
    t = np.arange(i1 - i0, dtype=np.float64) / SAMPLE_RATE
    phase = 2.0 * math.pi * freq * t
    signal = np.tanh(1.7 * (np.sin(phase) + 0.22 * np.sin(3 * phase))) if kind == "softsquare" else np.sin(phase) + 0.18 * np.sin(2 * phase)
    buf[i0:i1] += amp * envelope(t, 0.008 if kind == "sine" else 0.08, 0.2 if kind == "sine" else 1.4) * signal


def add_kick(buf, start, amp=0.46):
    i0 = int(start * SAMPLE_RATE)
    i1 = min(len(buf), i0 + int(0.24 * SAMPLE_RATE))
    if i1 <= i0:
        return
    t = np.arange(i1 - i0, dtype=np.float64) / SAMPLE_RATE
    phase = 2 * np.pi * (70 * t - 28 * t * t)
    buf[i0:i1] += amp * np.sin(phase) * np.exp(-t * 18)


def add_clap(buf, start, rng, amp=0.14):
    i0 = int(start * SAMPLE_RATE)
    i1 = min(len(buf), i0 + int(0.18 * SAMPLE_RATE))
    if i1 <= i0:
        return
    t = np.arange(i1 - i0, dtype=np.float64) / SAMPLE_RATE
    noise = rng.normal(0, 1, i1 - i0)
    noise = np.concatenate(([0.0], np.diff(noise)))
    buf[i0:i1] += amp * noise * np.exp(-t * 28)


def generate(output: Path, duration: float, bpm: float, seed: int) -> None:
    beat = 60.0 / bpm
    length = int(SAMPLE_RATE * duration)
    left = np.zeros(length, dtype=np.float64)
    right = np.zeros(length, dtype=np.float64)
    rng = np.random.default_rng(seed)
    chords = [
        (110.00, [220.00, 261.63, 329.63]),
        (87.31, [174.61, 220.00, 261.63]),
        (130.81, [261.63, 329.63, 392.00]),
        (98.00, [196.00, 246.94, 293.66]),
    ]
    chord_span = 2 * beat
    start = 0.0
    chord_index = 0
    while start < duration:
        bass, notes = chords[chord_index % len(chords)]
        add_tone(left, start, chord_span, bass, 0.13, "softsquare")
        add_tone(right, start, chord_span, bass, 0.13, "softsquare")
        for note in notes:
            add_tone(left, start, chord_span, note, 0.08)
            add_tone(right, start, chord_span, note, 0.08)
        start += chord_span
        chord_index += 1
    arpeggio = [523.25, 659.25, 783.99, 659.25]
    for beat_index in range(int(math.ceil(duration / beat))):
        beat_start = beat_index * beat
        add_kick(left, beat_start)
        add_kick(right, beat_start)
        if beat_index % 2:
            add_clap(left, beat_start, rng)
            add_clap(right, beat_start, rng)
        for half in range(2):
            pluck_start = beat_start + half * beat / 2
            frequency = arpeggio[(beat_index * 2 + half) % len(arpeggio)]
            add_tone(left, pluck_start, 0.28, frequency, 0.075)
            add_tone(right, pluck_start, 0.28, frequency * (1.002 if half else 1.0), 0.075)
    fade_in = min(length, int(0.16 * SAMPLE_RATE))
    fade_out = min(length, int(0.35 * SAMPLE_RATE))
    left[:fade_in] *= np.linspace(0, 1, fade_in)
    right[:fade_in] *= np.linspace(0, 1, fade_in)
    left[-fade_out:] *= np.linspace(1, 0, fade_out)
    right[-fade_out:] *= np.linspace(1, 0, fade_out)
    stereo = np.stack([left, right], axis=1)
    stereo *= 0.82 / max(np.max(np.abs(stereo)), 1e-9)
    pcm = np.int16(np.clip(stereo, -1, 1) * 32767)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=8.2)
    parser.add_argument("--bpm", type=float, default=96.0)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()
    if not 3.0 <= args.duration <= 30.0:
        raise SystemExit("duration must be between 3 and 30 seconds")
    if not 60.0 <= args.bpm <= 130.0:
        raise SystemExit("bpm must be between 60 and 130")
    generate(args.output.resolve(), args.duration, args.bpm, args.seed)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
