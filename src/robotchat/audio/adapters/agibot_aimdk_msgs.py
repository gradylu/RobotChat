"""智元灵犀 X2 适配器（AimDK：aimdk_msgs + token_id 续播 + 音频焦点）。

机型差异（相对通用适配器）：
- 播放消息需要 `token_id` / `pkg_name` / `priority`，同一 token_id 续播，换 token_id 清空缓存；
- 停播没有专用服务，靠「换 token_id 并停止送流」实现（FR-16.6 的 topic_msg 策略）；
- 音量是 ROS2 服务 `SetVolume`（0-100），不是话题；
- 播放前必须申请音频焦点，否则与本体自带交互模块抢音频流。

接口契约全部来自 Profile，本文件不出现业务话题常量（FR-16.2 / FR-16.7）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from robotchat.audio.adapters import register_device_adapter
from robotchat.audio.adapters.base_ros_audio import (
    BaseRosAudioAdapter,
    PublishSpec,
    RosAudioBackend,
    ServiceSpec,
)
from robotchat.config.schema import DeviceProfileSchema
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.audio.adapters.agibot_aimdk_msgs")

DEFAULT_PKG_NAME = "robotchat"


@register_device_adapter("agibot_aimdk_msgs")
class AgibotAimdkMsgsAdapter(BaseRosAudioAdapter):
    """灵犀 X2（Pro 探索版 / Ultra 旗舰版）音频适配器。"""

    def __init__(
        self,
        profile: DeviceProfileSchema,
        *,
        backend: RosAudioBackend | None = None,
        pkg_name: str = DEFAULT_PKG_NAME,
        target_sample_rate: int = 16000,
        target_channels: int = 1,
        target_bit_depth: int = 16,
        frame_ms: int = 20,
        capture_timeout_s: float = 2.0,
        queue_frames: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            profile,
            backend=backend,
            target_sample_rate=target_sample_rate,
            target_channels=target_channels,
            target_bit_depth=target_bit_depth,
            frame_ms=frame_ms,
            capture_timeout_s=capture_timeout_s,
            queue_frames=queue_frames,
            clock=clock,
        )
        self.pkg_name = pkg_name
        self._token = 1
        self._focus_held = False

    @property
    def token_id(self) -> int:
        return self._token

    # ------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        await super().open()
        await self._request_focus()

    async def close(self) -> None:
        if self._focus_held:
            await self._abandon_focus()
        await super().close()

    # ------------------------------------------------------------ 播放

    def _playback_spec(self) -> PublishSpec:
        base = super()._playback_spec()
        extra: dict[str, Any] = {
            "pkg_name": self.pkg_name,
            self._token_field(): self._token,
        }
        focus = self.profile.playback.focus
        if focus is not None:
            extra["priority"] = focus.priority
            if focus.weight:
                extra["weight"] = focus.weight
        return PublishSpec(
            topic=base.topic,
            msg_type=base.msg_type,
            field=base.field,
            extra_fields=extra,
        )

    def _token_field(self) -> str:
        return self.profile.playback.fields.get("token", "token_id")

    async def flush(self) -> None:
        """换 token_id + 送空帧：本体侧丢弃旧缓存，实现 300ms 内的立即静音。"""
        started = self._clock()
        self._token += 1
        await self._backend.publish(self._playback_spec(), b"")
        self._mark_flush(started)

    # ------------------------------------------------------------ 音量

    async def set_volume(self, level: float) -> None:
        volume = self.profile.volume
        if volume is None:
            await super().set_volume(level)
            return
        low, high = volume.range
        if not low <= level <= high:
            raise ValueError(f"音量 {level} 超出 Profile 范围 {volume.range}")
        self._volume = float(level)
        if volume.kind == "ros2_service":
            if not volume.msg_type:
                raise RuntimeError("volume.kind=ros2_service 需要声明 volume.msg_type")
            await self._backend.call_service(
                ServiceSpec(
                    service=volume.service,
                    msg_type=volume.msg_type,
                    fields={volume.field: int(round(level))},
                )
            )
            return
        await super().set_volume(level)

    # ------------------------------------------------------------ 音频焦点

    async def _request_focus(self) -> None:
        focus = self.profile.playback.focus
        if focus is None:
            return
        fields: dict[str, Any] = {"pkg_name": self.pkg_name, "priority": focus.priority}
        if focus.weight:
            fields["weight"] = focus.weight
        await self._backend.call_service(
            ServiceSpec(
                service=focus.request_service,
                msg_type=focus.request_msg_type,
                call_timeout_s=1.0,
                fields=fields,
            )
        )
        self._focus_held = True
        logger.info("audio_focus_acquired", profile=self.profile_name, pkg=self.pkg_name)

    async def _abandon_focus(self) -> None:
        focus = self.profile.playback.focus
        if focus is None:
            return
        self._focus_held = False
        try:
            await self._backend.call_service(
                ServiceSpec(
                    service=focus.abandon_service or focus.request_service,
                    msg_type=focus.abandon_msg_type or focus.request_msg_type,
                    call_timeout_s=0.5,
                    fields={"pkg_name": self.pkg_name},
                )
            )
        except Exception:  # 关闭阶段放弃焦点失败不阻塞退出
            logger.warning("audio_focus_abandon_failed", profile=self.profile_name)


__all__ = ["AgibotAimdkMsgsAdapter", "DEFAULT_PKG_NAME"]
