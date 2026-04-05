"""
Nexus AI — Voice Handler (Improvement #16)
WhatsApp/Telegram voice notes → Whisper transcription → pipeline.
Full multilingual support: Hindi, Kannada, Tamil, Telugu, English.
Optional TTS reply via gTTS.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


# Supported languages with Whisper language codes
SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "kn": "Kannada",
    "ta": "Tamil",
    "te": "Telugu",
    "ml": "Malayalam",
    "mr": "Marathi",
    "gu": "Gujarati",
    "bn": "Bengali",
    "pa": "Punjabi",
}


class VoiceHandler:
    """
    Handles voice input via Whisper (local, free).
    Transcribes to text, then passes to the standard query pipeline.
    Optionally converts text response back to speech.
    """

    def __init__(self) -> None:
        self._model = None          # Whisper model (loaded lazily)
        self._model_size = "base"   # base = fast + good for Indian languages
        self._tts_available = False

    async def initialize(self) -> None:
        """Load Whisper model. Falls back gracefully if not installed."""
        try:
            import whisper
            self._model = await asyncio.to_thread(
                whisper.load_model, self._model_size
            )
            log.info("whisper_loaded", model=self._model_size)
        except ImportError:
            log.warning("whisper_not_installed", hint="pip install openai-whisper")
        except Exception as exc:
            log.warning("whisper_load_failed", error=str(exc))

        # Check TTS availability
        try:
            from gtts import gTTS  # noqa
            self._tts_available = True
            log.info("gtts_available")
        except ImportError:
            log.info("gtts_not_installed", hint="pip install gtts")

    async def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
    ) -> dict:
        """
        Transcribe audio file to text.
        Returns: {text, language, confidence, duration_s}
        """
        if self._model is None:
            await self.initialize()
        if self._model is None:
            return {"text": "", "language": "unknown", "confidence": 0.0,
                    "error": "Whisper not available"}
        try:
            result = await asyncio.to_thread(
                self._model.transcribe,
                audio_path,
                language=language,      # None = auto-detect
                task="transcribe",
                fp16=False,             # CPU-safe
            )
            detected_lang = result.get("language", "unknown")
            text = result.get("text", "").strip()
            # Whisper confidence from segment probabilities
            segments = result.get("segments", [])
            avg_conf = (
                sum(s.get("avg_logprob", -1) for s in segments) / len(segments)
                if segments else -1
            )
            # Convert log-prob to approximate confidence (rough)
            confidence = max(0.0, min(1.0, (avg_conf + 1.0)))

            lang_name = SUPPORTED_LANGUAGES.get(detected_lang, detected_lang)
            log.info("transcription_complete",
                     language=detected_lang, text_length=len(text),
                     confidence=round(confidence, 2))
            return {
                "text": text,
                "language": detected_lang,
                "language_name": lang_name,
                "confidence": round(confidence, 2),
                "segment_count": len(segments),
            }
        except Exception as exc:
            log.error("transcription_error", error=str(exc))
            return {"text": "", "language": "unknown", "confidence": 0.0,
                    "error": str(exc)}

    async def transcribe_bytes(
        self,
        audio_bytes: bytes,
        file_ext: str = ".ogg",
        language: Optional[str] = None,
    ) -> dict:
        """Transcribe audio from raw bytes (e.g. from WhatsApp OGG file)."""
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            return await self.transcribe(tmp_path, language)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def text_to_speech(
        self,
        text: str,
        language: str = "en",
        output_path: Optional[str] = None,
    ) -> Optional[str]:
        """
        Convert text to speech using gTTS.
        Returns path to MP3 file, or None if TTS unavailable.
        """
        if not self._tts_available:
            return None
        try:
            from gtts import gTTS
            # Map Whisper language codes to gTTS codes
            gtts_lang_map = {"kn": "kn", "ta": "ta", "te": "te",
                             "hi": "hi", "ml": "ml", "en": "en"}
            gtts_lang = gtts_lang_map.get(language, "en")

            if not output_path:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    output_path = tmp.name

            tts = gTTS(text=text[:500], lang=gtts_lang, slow=False)
            await asyncio.to_thread(tts.save, output_path)
            log.info("tts_generated", language=gtts_lang, length=len(text))
            return output_path
        except Exception as exc:
            log.warning("tts_error", error=str(exc))
            return None

    async def handle_whatsapp_voice(
        self,
        audio_bytes: bytes,
        user_id: str,
        chat_id: str,
    ) -> dict:
        """
        Full WhatsApp voice note handler.
        1. Transcribe (auto-detect language)
        2. Return text for pipeline processing
        3. Optionally generate TTS response
        """
        log.info("whatsapp_voice_received", user_id=user_id[:8]+"…")
        result = await self.transcribe_bytes(audio_bytes, file_ext=".ogg")
        if not result.get("text"):
            return {"success": False, "error": "Transcription failed",
                    "text": "", "language": "unknown"}
        log.info("voice_transcribed", text=result["text"][:60],
                 lang=result["language"], conf=result["confidence"])
        return {
            "success": True,
            "text": result["text"],
            "language": result["language"],
            "language_name": result.get("language_name", ""),
            "confidence": result["confidence"],
            "ready_for_pipeline": True,
        }


voice_handler = VoiceHandler()
