"""Inspect uploaded or generated audio/video files."""

import json
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


class MediaProbeError(Exception):
    pass


@dataclass(frozen=True)
class MediaProbeResult:
    duration_ms: int
    width: int | None = None
    height: int | None = None


def _seconds_to_milliseconds(value: object) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return round(seconds * 1000)


_MPEG_BITRATES = {
    ("1", "1"): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    ("1", "2"): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    ("1", "3"): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    ("2", "1"): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    ("2", "2"): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    ("2", "3"): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG_SAMPLE_RATES = {
    "1": (44100, 48000, 32000),
    "2": (22050, 24000, 16000),
    "2.5": (11025, 12000, 8000),
}


def _synchsafe_integer(value: bytes) -> int:
    if len(value) != 4 or any(byte & 0x80 for byte in value):
        raise MediaProbeError("Uploaded MP3 contains an invalid ID3 header")
    return sum(byte << shift for byte, shift in zip(value, (21, 14, 7, 0)))


def _inspect_wav(path: Path) -> MediaProbeResult:
    try:
        with wave.open(str(path), "rb") as stream:
            frame_rate = stream.getframerate()
            frame_count = stream.getnframes()
            if frame_rate <= 0 or frame_count <= 0:
                raise MediaProbeError("Uploaded WAV duration is unavailable")
            duration_ms = round(frame_count * 1000 / frame_rate)
    except (EOFError, OSError, wave.Error) as error:
        raise MediaProbeError("Uploaded file is not a valid WAV audio file") from error
    return MediaProbeResult(duration_ms=max(duration_ms, 1))


def _mpeg_frame_details(header: bytes) -> tuple[int, int] | None:
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    sample_rate_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    if version_bits == 0x01 or layer_bits == 0 or bitrate_index in {0, 0x0F} or sample_rate_index == 0x03:
        return None

    version = {0x03: "1", 0x02: "2", 0x00: "2.5"}[version_bits]
    layer = {0x03: "1", 0x02: "2", 0x01: "3"}[layer_bits]
    bitrate_family = "1" if version == "1" else "2"
    bitrate_kbps = _MPEG_BITRATES[(bitrate_family, layer)][bitrate_index]
    sample_rate = _MPEG_SAMPLE_RATES[version][sample_rate_index]
    if layer == "1":
        frame_length = ((12 * bitrate_kbps * 1000 // sample_rate) + padding) * 4
        samples_per_frame = 384
    elif layer == "3" and version != "1":
        frame_length = 72 * bitrate_kbps * 1000 // sample_rate + padding
        samples_per_frame = 576
    else:
        frame_length = 144 * bitrate_kbps * 1000 // sample_rate + padding
        samples_per_frame = 1152
    if frame_length < 4:
        return None
    return frame_length, round(samples_per_frame * 1000 / sample_rate)


def _inspect_mp3(path: Path) -> MediaProbeResult:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise MediaProbeError("Uploaded MP3 could not be read") from error
    offset = 0
    if content.startswith(b"ID3"):
        if len(content) < 10:
            raise MediaProbeError("Uploaded MP3 contains an incomplete ID3 header")
        offset = 10 + _synchsafe_integer(content[6:10])
        if content[5] & 0x10:
            offset += 10

    frame_count = 0
    duration_ms = 0
    while offset + 4 <= len(content):
        details = _mpeg_frame_details(content[offset : offset + 4])
        if details is None:
            if frame_count:
                break
            offset += 1
            continue
        frame_length, frame_duration_ms = details
        if offset + frame_length > len(content):
            break
        frame_count += 1
        duration_ms += frame_duration_ms
        offset += frame_length
    if frame_count == 0 or duration_ms <= 0:
        raise MediaProbeError("Uploaded file is not a valid MP3 audio file")
    return MediaProbeResult(duration_ms=duration_ms)


def _inspect_audio_file(path: Path) -> MediaProbeResult:
    try:
        signature = path.read_bytes()[:12]
    except OSError as error:
        raise MediaProbeError("Uploaded audio file could not be read") from error
    if signature.startswith(b"RIFF") and signature[8:12] == b"WAVE":
        return _inspect_wav(path)
    if signature.startswith(b"ID3") or signature.startswith(b"\xff"):
        return _inspect_mp3(path)
    raise MediaProbeError("Uploaded file must be a valid MP3 or WAV audio file")


def inspect_media_file(path: Path, expected_kind: str) -> MediaProbeResult:
    if expected_kind not in {"video", "audio"}:
        raise MediaProbeError("Unsupported media probe kind")
    if expected_kind == "audio":
        return _inspect_audio_file(path)
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaProbeError("Media inspection service is unavailable") from error
    if completed.returncode != 0:
        raise MediaProbeError("Uploaded file is not a valid media file")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise MediaProbeError("Media inspection returned invalid metadata") from error
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaProbeError("Uploaded file contains no readable media stream")
    matching_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == expected_kind
    ]
    if not matching_streams:
        raise MediaProbeError(f"Uploaded file does not contain a {expected_kind} stream")

    duration_candidates = [
        _seconds_to_milliseconds((payload.get("format") or {}).get("duration"))
        if isinstance(payload.get("format"), dict)
        else None,
        *(_seconds_to_milliseconds(stream.get("duration")) for stream in matching_streams),
    ]
    durations = [duration for duration in duration_candidates if duration is not None]
    if not durations:
        raise MediaProbeError("Media duration is unavailable")

    width = height = None
    if expected_kind == "video":
        video_stream = matching_streams[0]
        try:
            width = int(video_stream.get("width"))
            height = int(video_stream.get("height"))
        except (TypeError, ValueError) as error:
            raise MediaProbeError("Video dimensions are unavailable") from error
        if width <= 0 or height <= 0:
            raise MediaProbeError("Video dimensions are invalid")

    return MediaProbeResult(duration_ms=max(durations), width=width, height=height)
