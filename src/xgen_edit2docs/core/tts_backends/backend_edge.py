"""edge-tts backend for narration audio generation."""

from __future__ import annotations

import re
from pathlib import Path


COMMON_VOICES = [
    # Korean (G10 — Korean voices promoted to first-class with Korean defaults)
    ("ko-KR", "ko-KR-SunHiNeural", "female, Korean, natural and composed (default for ko-KR)"),
    ("ko-KR", "ko-KR-InJoonNeural", "male, Korean, trustworthy presenter tone"),
    ("ko-KR", "ko-KR-HyunsuNeural", "male, Korean, younger / friendly"),
    ("ko-KR", "ko-KR-YuJinNeural", "female, Korean, energetic"),
    # Chinese
    ("zh-CN", "zh-CN-XiaoxiaoNeural", "female, Mandarin, clear and natural (default for zh-CN)"),
    ("zh-CN", "zh-CN-XiaoyiNeural", "female, Mandarin, bright"),
    ("zh-CN", "zh-CN-YunjianNeural", "male, Mandarin, steady"),
    ("zh-CN", "zh-CN-YunxiNeural", "male, Mandarin, youthful"),
    ("zh-CN", "zh-CN-YunxiaNeural", "male, Mandarin, boyish"),
    ("zh-CN", "zh-CN-YunyangNeural", "male, Mandarin, broadcasting tone"),
    ("zh-HK", "zh-HK-HiuGaaiNeural", "female, Cantonese"),
    ("zh-HK", "zh-HK-WanLungNeural", "male, Cantonese"),
    ("zh-TW", "zh-TW-HsiaoChenNeural", "female, Mandarin (Taiwan)"),
    ("zh-TW", "zh-TW-YunJheNeural", "male, Mandarin (Taiwan)"),
    # Japanese
    ("ja-JP", "ja-JP-NanamiNeural", "female, Japanese"),
    ("ja-JP", "ja-JP-KeitaNeural", "male, Japanese"),
    # English
    ("en-US", "en-US-JennyNeural", "female, American English"),
    ("en-US", "en-US-GuyNeural", "male, American English"),
    ("en-GB", "en-GB-SoniaNeural", "female, British English"),
    ("en-GB", "en-GB-RyanNeural", "male, British English"),
]


# Default voice per locale (used when caller doesn't specify --voice).
DEFAULT_VOICE_PER_LOCALE = {
    "ko-KR": "ko-KR-SunHiNeural",
    "en-US": "en-US-JennyNeural",
    "en-GB": "en-GB-SoniaNeural",
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh-TW": "zh-TW-HsiaoChenNeural",
    "ja-JP": "ja-JP-NanamiNeural",
}


def default_voice_for_locale(locale: str) -> str | None:
    """Return the recommended voice for *locale* (or None if unknown)."""
    if locale in DEFAULT_VOICE_PER_LOCALE:
        return DEFAULT_VOICE_PER_LOCALE[locale]
    prefix = locale.split("-")[0] if locale else ""
    for code, voice in DEFAULT_VOICE_PER_LOCALE.items():
        if code.startswith(prefix + "-"):
            return voice
    return None


def edge_output_extension() -> str:
    return ".mp3"


def normalize_rate(rate: str) -> str:
    """Normalize a user-provided rate into edge-tts format."""
    value = rate.strip()
    if not value:
        return "+0%"
    if value.endswith("%"):
        if value[0] not in "+-":
            return f"+{value}"
        return value
    if re.fullmatch(r"[+-]?\d+", value):
        number = int(value)
        return f"{number:+d}%"
    return value


async def generate(text: str, output_path: Path, *, voice: str, rate: str) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `edge-tts`. Install it with: "
            "python3 -m pip install edge-tts"
        ) from exc

    communicate = edge_tts.Communicate(text, voice=voice, rate=normalize_rate(rate))
    await communicate.save(str(output_path))


def print_common_voices() -> None:
    print("Common edge-tts voices:")
    print("Locale   Voice                         Notes")
    print("------   ----------------------------  ----------------")
    for locale, voice, notes in COMMON_VOICES:
        print(f"{locale:<8} {voice:<29} {notes}")


async def print_voices(locale: str | None = None) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `edge-tts`. Install it with: "
            "python3 -m pip install edge-tts"
        ) from exc

    manager = await edge_tts.VoicesManager.create()
    voices = manager.voices
    if locale:
        voices = [voice for voice in voices if voice.get("Locale") == locale]
    for voice in sorted(voices, key=lambda item: (item.get("Locale", ""), item.get("ShortName", ""))):
        short_name = voice.get("ShortName", "")
        voice_locale = voice.get("Locale", "")
        gender = voice.get("Gender", "")
        friendly = voice.get("FriendlyName", "")
        print(f"{voice_locale:<8} {short_name:<34} {gender:<8} {friendly}")

