"""Bounded preview inspection; filenames and upload Content-Type are untrusted."""

import codecs
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

TEXT_LIMIT = 256 * 1024
SIGNATURE_LIMIT = 4096
TEXT_EXTENSIONS = frozenset((
    "", ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".conf", ".py", ".sh", ".c", ".h", ".cpp", ".rs",
    ".go", ".js", ".css", ".sql",
))


@dataclass(frozen=True)
class Preview:
    kind: str = "unsupported"
    media_type: str = "application/octet-stream"
    text: str = ""
    truncated: bool = False


def media_preview(name: str, prefix: bytes) -> Preview:
    """Allow only explicit media extensions with matching container signatures.

    This identifies a container, not codec support or file validity. Browsers
    decode media; we neither parse whole uploads nor trust supplied MIME types.
    Active document formats never enter this allowlist.
    """
    suffix = PurePosixPath(name).suffix.lower()
    matches = (
        (suffix == ".png" and prefix.startswith(b"\x89PNG\r\n\x1a\n"), "image", "image/png"),
        (suffix in (".jpg", ".jpeg") and prefix.startswith(b"\xff\xd8\xff"), "image", "image/jpeg"),
        (suffix == ".gif" and prefix[:6] in (b"GIF87a", b"GIF89a"), "image", "image/gif"),
        (suffix == ".webp" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP", "image", "image/webp"),
        (suffix == ".wav" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE", "audio", "audio/wav"),
        (suffix == ".flac" and prefix.startswith(b"fLaC"), "audio", "audio/flac"),
        (suffix in (".ogg", ".oga") and prefix.startswith(b"OggS\x00"), "audio", "audio/ogg"),
        (suffix == ".ogv" and prefix.startswith(b"OggS\x00"), "video", "video/ogg"),
        (suffix == ".mp3" and (prefix.startswith(b"ID3") or
            (len(prefix) >= 4 and prefix[0] == 255 and prefix[1] & 0xE0 == 0xE0
             and prefix[1] & 6 and prefix[2] >> 4 not in (0, 15)
             and prefix[2] & 12 != 12)), "audio", "audio/mpeg"),
        (suffix == ".webm" and prefix.startswith(b"\x1a\x45\xdf\xa3")
         and b"\x42\x82\x84webm" in prefix, "video", "video/webm"),
    )
    for matches_signature, kind, media_type in matches:
        if matches_signature:
            return Preview(kind, media_type)
    if suffix in (".mp4", ".m4v", ".m4a") and prefix[4:8] == b"ftyp":
        box_size = int.from_bytes(prefix[:4], "big")
        if 16 <= box_size <= len(prefix) and box_size % 4 == 0:
            brands = {prefix[8:12], *(prefix[i:i + 4] for i in range(16, box_size, 4))}
            if brands & {b"isom", b"iso2", b"mp41", b"mp42", b"M4V ", b"M4A ", b"avc1"}:
                return Preview("audio", "audio/mp4") if suffix == ".m4a" else Preview("video", "video/mp4")
    return Preview()


def inspect_preview(file: BinaryIO, name: str, *, include_text: bool) -> Preview:
    file.seek(0)
    prefix = file.read(SIGNATURE_LIMIT)
    media = media_preview(name, prefix)
    if media.kind != "unsupported" or not include_text or PurePosixPath(name).suffix.lower() not in TEXT_EXTENSIONS:
        return media
    file.seek(0)
    data = file.read(TEXT_LIMIT + 1)
    truncated = len(data) > TEXT_LIMIT
    try:
        # Do not replace malformed UTF-8; only defer a split codepoint at the
        # preview boundary. Raw bytes remain available through download.
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(data[:TEXT_LIMIT], final=not truncated)
    except UnicodeDecodeError:
        return Preview()
    if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) < 160 for char in text):
        return Preview()
    return Preview("text", text=text, truncated=truncated)
