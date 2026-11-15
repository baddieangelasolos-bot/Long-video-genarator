#!/usr/bin/env python3
"""
dup_scan.py - find footage that appears twice (in two source files, or twice inside one file) so it is used once.

Input : <src-dir>/hash_<name>.npz  (written by pipeline/fullscan.py: 2 frames per second, 256-bit difference hash + contrast)
Output: JSON list of EXCLUDED ranges [{file,start,end,dup_of,dup_start,dup_end}]. The first copy (lower file name, earlier
        time) is kept; every later copy is listed. A copy needs >= 4 matching samples in a row at a steady time offset.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
MAX_BITS = 22          # of 256: same picture; different shots of one green pitch are far above this
MIN_STD = 8.0          # flat frames (black, pitch-only) are not evidence
MIN_RUN = 4            # matching samples in a row (~2 s)
SAME_FILE_GAP_S = 4.0  # a static scene matches its own neighbour: ignore matches closer than this inside one file


def load(src_dir: Path, names: list[str]):
    hs, sd, ts, fi = [], [], [], []
    for k, n in enumerate(names):
        p = src_dir / f"hash_{n}.npz"
        if not p.exists():
            continue
        z = np.load(p)
        hs.append(z["h"]); sd.append(z["std"]); ts.append(z["t"]); fi.append(np.full(len(z["t"]), k))
    if not hs:
        return None
    return np.concatenate(hs), np.concatenate(sd), np.concatenate(ts), np.concatenate(fi)


def matches(h, sd, ts, fi):
    keep = np.where(sd >= MIN_STD)[0]
    hk = h[keep]
    out = []
    step = max(1, 4_000_000 // max(1, len(keep)))
    for a in range(0, len(keep), step):
        blk = hk[a:a + step]
        dist = POP[np.bitwise_xor(blk[:, None, :], hk[None, :, :])].sum(axis=2)
        ii, jj = np.nonzero(dist <= MAX_BITS)
        for i, j in zip(ii + a, jj):
            gi, gj = keep[i], keep[j]
            if (fi[gi], ts[gi]) >= (fi[gj], ts[gj]):
                continue
            if fi[gi] == fi[gj] and ts[gj] - ts[gi] < SAME_FILE_GAP_S:
                continue
            out.append((int(fi[gi]), int(fi[gj]), float(ts[gi]), float(ts[gj])))
        if len(out) > 400_000:
            break
    return out


def runs(ms):
    """Group matches into runs with a steady offset: [(fa, fb, a0, a1, b0, b1, n)]."""
    ms.sort()
    done, active = [], []
    for fa, fb, ta, tb in ms:
        d = tb - ta
        active = [r for r in active if r["fa"] == fa and r["fb"] == fb and ta - r["ta"] <= 1.1]
        hit = next((r for r in active if abs(d - r["d"]) <= 0.75 and 0 < ta - r["ta"] <= 1.1), None)
        if hit:
            hit.update(ta=ta, tb=tb, n=hit["n"] + 1, d=d)
            hit["a1"], hit["b1"] = ta, tb
        else:
            r = dict(fa=fa, fb=fb, ta=ta, tb=tb, d=d, n=1, a0=ta, a1=ta, b0=tb, b1=tb)
            active.append(r)
            done.append(r)
    return [r for r in done if r["n"] >= MIN_RUN]


def find(src_dir: Path, names: list[str]) -> list[dict]:
    data = load(src_dir, names)
    if data is None:
        return []
    found = runs(matches(*data))
    ex: list[dict] = []
    for r in found:
        ex.append({"file": f"{names[r['fb']]}.mp4", "start": round(max(0.0, r["b0"] - 0.6), 2), "end": round(r["b1"] + 0.6, 2),
                   "dup_of": f"{names[r['fa']]}.mp4", "dup_start": round(max(0.0, r["a0"] - 0.6), 2), "dup_end": round(r["a1"] + 0.6, 2)})
    # one exclusion must never remove the footage that another exclusion keeps: drop an exclusion whose
    # range lies inside an already-excluded source of its own "original"
    ex.sort(key=lambda e: (e["file"], e["start"]))
    merged: list[dict] = []
    for e in ex:
        if merged and merged[-1]["file"] == e["file"] and e["start"] <= merged[-1]["end"] + 0.5:
            merged[-1]["end"] = max(merged[-1]["end"], e["end"])
        else:
            merged.append(dict(e))
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src-dir", default="/home/claude/work/src")
    ap.add_argument("--sources", nargs="+", required=True, help="source stems, e.g. s01 s02")
    ap.add_argument("--out", default="/home/claude/work/analysis/dup_ranges.json")
    a = ap.parse_args(argv)
    ex = find(Path(a.src_dir), a.sources)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(ex, indent=1), encoding="utf-8")
    secs = round(sum(e["end"] - e["start"] for e in ex), 1)
    print("DUPS " + json.dumps({"ranges": len(ex), "seconds": secs, "files": sorted({e["file"] for e in ex})}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
