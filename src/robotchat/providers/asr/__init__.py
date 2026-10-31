"""ASR 驱动包：本地 FunASR、外置方言模型与云端 DashScope（T-2.1、T-2.2、T-2A.3）。"""

from __future__ import annotations

from robotchat.providers.asr.dashscope_stream import DashScopeASR
from robotchat.providers.asr.funasr_dialect import FunASRDialectASR
from robotchat.providers.asr.funasr_local import FunASRLocalASR

__all__ = ["DashScopeASR", "FunASRDialectASR", "FunASRLocalASR"]
