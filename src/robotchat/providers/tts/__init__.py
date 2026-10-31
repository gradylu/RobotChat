"""TTS 驱动包：本地 Qwen3-TTS 与云端 DashScope（T-2.4、T-2.5）。"""

from __future__ import annotations

from robotchat.providers.tts.dashscope_tts import DashScopeTTS
from robotchat.providers.tts.qwen3_tts import Qwen3TTSLocal, TTSUnavailableError

__all__ = ["DashScopeTTS", "Qwen3TTSLocal", "TTSUnavailableError"]
