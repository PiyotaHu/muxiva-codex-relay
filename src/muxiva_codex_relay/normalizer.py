from __future__ import annotations

from dataclasses import dataclass
import json
import re
import urllib.request


S1_SYSTEM_PROMPT = (
    "You are a text normalizer for speech-to-text transcripts. The input begins "
    "with a control line specifying the styling, structure, and context settings; "
    "clean the transcript to match those settings and output only the cleaned text."
)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def should_use_s1_mini(text: str) -> bool:
    """S1-mini v1 is English-only; never send Chinese text to it."""
    return not _CJK_RE.search(text) and len(_LATIN_RE.findall(text)) >= 3


@dataclass(slots=True)
class NormalizationResult:
    text: str
    engine: str


class TranscriptNormalizer:
    def __init__(self, base_url: str | None, model: str, timeout_seconds: int = 20):
        self.base_url = base_url
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def normalize(self, transcript: str) -> NormalizationResult:
        transcript = transcript.strip()
        if not transcript:
            return NormalizationResult("", "empty")
        if not self.base_url or not should_use_s1_mini(transcript):
            return NormalizationResult(transcript, "asr-original")

        control = "[Styling: semi-formal] [Structure: prose] [Context: general]"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": S1_SYSTEM_PROMPT},
                {"role": "user", "content": f"{control}\n{transcript}"},
            ],
            "temperature": 0,
            "max_tokens": max(64, int(len(transcript.split()) * 1.3) + 32),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
            cleaned = payload["choices"][0]["message"]["content"].strip()
            # Filler-only input legitimately normalizes to an empty string.
            return NormalizationResult(cleaned, "S1-mini by Superwhisper")
        except Exception:
            # Dictation must remain usable if the optional local model is down.
            return NormalizationResult(transcript, "asr-original-fallback")
