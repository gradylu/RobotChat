"""驱动显式注册入口（FR-10.2）：导入驱动模块即完成 `@register_provider` 注册。

新增厂商 = 新驱动文件 + 装饰器 + 配置段，业务代码零改动；此处集中导入，
避免每个调用方各写一遍 import。
"""

from __future__ import annotations


def load_drivers() -> None:
    """导入内置驱动模块，触发注册（幂等）。"""
    from robotchat.audio import file_io, ros2_bridge, ros2_native, vad  # noqa: F401
    from robotchat.providers import asr, knowledge, kws, llm, tts  # noqa: F401
