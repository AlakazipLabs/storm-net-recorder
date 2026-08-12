#!/usr/bin/env python3
"""Score a set of captures to find the gain that gives the cleanest NBFM audio.

Capture the same signal at several gain settings (tools/capture.sh, one run per
gain, naming the files so you can tell them apart), then run this over the
directory. It reports per capture:

  SNR proxy (dB)  speech-band power (300-3000 Hz) over hiss-band power
                  (4500-10000 Hz). Narrowband FM voice has no legitimate energy
                  in the hiss band, so that band is a clean read of demodulator
                  noise — what an operator hears as "quieting".
  clip%           samples at or near full scale; the front-end overload marker.
  DC, RMS         sanity checks. A capture with DC near zero and low RMS heard
                  nothing at all.

Why this matters: maximum gain is not best gain. Past a point the tuner
amplifies noise faster than signal, and the SNR proxy will show you exactly
where that turn happens on your antenna.

Compare only captures from a single session. Run-to-run drift of a couple of dB
at identical settings is normal, which is enough to invert a ranking built from
captures taken hours apart.

Usage: python3 tools/analyze_gain_ladder.py [captures_dir]
Reads *.raw (16-bit signed LE mono, 24 kHz — rtl_fm's native output).
Offline only: it touches nothing but files already on disk.
"""

import glob
import os
import sys

import numpy as np

RATE = 24000
SPEECH_BAND = (300.0, 3000.0)
HISS_BAND = (4500.0, 10000.0)
CLIP_LEVEL = 32700   # of int16 full scale 32767
FRAME = 4096         # periodogram frame (hann, 50% overlap)


def load_raw(path):
    """int16 LE mono -> float64 array."""
    return np.fromfile(path, dtype="<i2").astype(np.float64)


def avg_psd(x, rate=RATE, frame=FRAME):
    """Averaged windowed periodogram. Returns (freqs, psd)."""
    if len(x) < frame:
        raise ValueError(f"capture too short: {len(x)} samples < {frame}")
    win = np.hanning(frame)
    hop = frame // 2
    n_frames = 1 + (len(x) - frame) // hop
    acc = np.zeros(frame // 2 + 1)
    for i in range(n_frames):
        seg = x[i * hop: i * hop + frame] * win
        acc += np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(frame, d=1.0 / rate)
    return freqs, acc / n_frames


def band_power(freqs, psd, lo, hi):
    sel = (freqs >= lo) & (freqs < hi)
    return float(psd[sel].mean())


def metrics(x, rate=RATE):
    """Per-capture quality metrics. Raises ValueError on empty or short input."""
    if len(x) == 0:
        raise ValueError("empty capture")
    freqs, psd = avg_psd(x, rate)
    speech = band_power(freqs, psd, *SPEECH_BAND)
    hiss = band_power(freqs, psd, *HISS_BAND)
    return {
        "snr_db": 10.0 * np.log10(speech / hiss) if hiss > 0 else float("inf"),
        "clip_pct": 100.0 * float(np.mean(np.abs(x) >= CLIP_LEVEL)),
        "dc": float(x.mean()),
        "rms": float(np.sqrt(np.mean(x ** 2))),
    }


def main(cap_dir="captures"):
    paths = sorted(glob.glob(os.path.join(cap_dir, "*.raw")))
    if not paths:
        print(f"no .raw files in {cap_dir}/", file=sys.stderr)
        return 1
    rows = []
    for p in paths:
        label = os.path.basename(p)[:-4]
        try:
            rows.append((label, metrics(load_raw(p))))
        except ValueError as e:
            print(f"skip {p}: {e}", file=sys.stderr)
    if not rows:
        return 1
    rows.sort(key=lambda r: r[1]["snr_db"], reverse=True)
    print(f"{'capture':>28}  {'SNR dB':>7}  {'clip%':>6}  {'DC':>8}  {'RMS':>8}")
    for label, m in rows:
        print(f"{label:>28}  {m['snr_db']:>7.2f}  {m['clip_pct']:>6.2f}"
              f"  {m['dc']:>8.1f}  {m['rms']:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
