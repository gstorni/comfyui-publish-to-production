"""IMAGE batch -> one MP4 with ffmpeg (raw frames piped in, nothing but the file written). No ffmpeg: refuse, say so."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("comfy_publish")
FPS_MIN, FPS_MAX = 1, 240                         # same range as the node's fps widget


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


def audio_of(audio):
    """A ComfyUI AUDIO dict {"waveform": [B,C,S] float -1..1, "sample_rate": int} -> (sample_rate, float32 numpy [C,S]), or None."""
    import numpy as np
    if audio is None:
        return None
    try:
        wave = audio["waveform"]
        sr = int(audio["sample_rate"])
        arr = wave.cpu().numpy() if hasattr(wave, "cpu") else np.asarray(wave)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise EncodeRefused("The video's audio is not in a form this tool can read") from None
    if not 8000 <= sr <= 192000:
        raise EncodeRefused("The video's audio sample rate is outside 8000-192000 Hz")
    if arr.ndim == 3:
        if arr.shape[0] < 1:
            return None                                # an empty batch: same as no audio
        arr = arr[0]
    if arr.ndim != 2 or arr.shape[1] < 1:
        return None                                    # no samples: same as no audio
    arr = np.nan_to_num(arr[:8].astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)     # at most 8 channels
    if arr.shape[0] < 1:
        return None
    return sr, np.ascontiguousarray(np.clip(arr, -1.0, 1.0))


def audio_digest(parts) -> str:
    import hashlib
    sr, arr = parts
    h = hashlib.sha256()
    h.update(f"{sr}:{tuple(arr.shape)}".encode())
    h.update(arr.tobytes())
    return h.hexdigest()


def video_parts(video):
    """A ComfyUI VIDEO -> (IMAGE tensor [N,H,W,C], fps as an exact Fraction (24000/1001 stays exact), audio (sample_rate, [C,S]) or None)."""
    try:
        comp = video.get_components()
        images, rate, audio = comp.images, comp.frame_rate, comp.audio
        fps = rate if isinstance(rate, Fraction) else Fraction(rate).limit_denominator(1_000_000)
    except EncodeRefused:
        raise
    except Exception:  # noqa: BLE001
        raise EncodeRefused("The video input could not be read") from None
    if not FPS_MIN <= fps <= FPS_MAX:
        raise EncodeRefused(f"The video's frame rate must be between {FPS_MIN} and {FPS_MAX} fps")
    frames_of(images)                                  # validates the batch; the publisher converts it again
    return images, fps, audio_of(audio)


def _rate(fps: Union[float, Fraction]) -> str:
    if isinstance(fps, Fraction):
        return f"{fps.numerator}/{fps.denominator}"
    return f"{fps:g}"


def encode_mp4(frames, fps: Union[float, Fraction], out: Path, audio=None) -> Path:
    exe = find_ffmpeg()
    if not exe:
        raise EncodeRefused("ffmpeg was not found: install it and make it available on PATH, then run again")
    n, h, w, _ = frames.shape
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)                      # a leftover .part is never used: always start clean
    audio_file = None
    cmd = [exe, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", _rate(fps), "-i", "-"]
    if audio is not None:
        sr, arr = audio
        sr, ch = int(sr), min(max(int(arr.shape[0]), 1), 8)
        if not 8000 <= sr <= 192000:
            raise EncodeRefused("The video's audio sample rate is outside 8000-192000 Hz")
        audio_file = part.with_name(part.name + ".pcm")
        cmd += ["-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", str(audio_file)]
    cmd += ["-map", "0:v"] + (["-map", "1:a", "-c:a", "aac", "-b:a", "192k"] if audio is not None else [])
    cmd += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "medium", "-crf", "12", "-pix_fmt", "yuv420p",
           "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", "-f", "mp4", str(part)]
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if audio_file is not None:
            audio_file.write_bytes(audio[1][:ch].astype("<f4").T.tobytes())       # interleaved little-endian float32
        p = subprocess.run(cmd, input=frames.tobytes(), capture_output=True)
    finally:
        if audio_file is not None:
            audio_file.unlink(missing_ok=True)
    if p.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        log.warning("ffmpeg failed: %s", p.stderr.decode(errors="replace")[-300:].strip())     # detail stays in the ComfyUI log
        raise EncodeRefused("ffmpeg could not encode the frames (details in the ComfyUI log)")
    os.replace(part, out)                             # the .mp4 only ever appears whole
    return out
