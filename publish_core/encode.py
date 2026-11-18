"""IMAGE batch -> one MP4 with ffmpeg (raw frames piped in, nothing but the file written). No ffmpeg: refuse, say so."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


class EncodeRefused(Exception):
    pass


def find_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:                                              # some ComfyUI installs bundle one through imageio-ffmpeg
        import imageio_ffmpeg                         # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def frames_of(images):
    """An IMAGE tensor [N,H,W,C] float 0..1 -> uint8 numpy [N,H,W,3] (alpha dropped)."""
    import numpy as np
    arr = images.cpu().numpy() if hasattr(images, "cpu") else np.asarray(images)
    arr = np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)
    if arr.ndim != 4 or arr.shape[0] < 1 or arr.shape[3] < 3:
        raise EncodeRefused("The image input is empty or not an image batch")
    return arr[..., :3]


def encode_mp4(frames, fps: float, out: Path) -> Path:
    exe = find_ffmpeg()
    if not exe:
        raise EncodeRefused("ffmpeg was not found: install it and make it available on PATH, then run again")
    n, h, w, _ = frames.shape
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)                      # a leftover .part is never used: always start clean
    cmd = [exe, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "-",
           "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "medium", "-crf", "12", "-pix_fmt", "yuv420p",
           "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", "-f", "mp4", str(part)]
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd, input=frames.tobytes(), capture_output=True)
    if p.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise EncodeRefused("ffmpeg could not encode the frames: " + p.stderr.decode(errors="replace")[-300:].strip())
    os.replace(part, out)                             # the .mp4 only ever appears whole
    return out
