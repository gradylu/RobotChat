"""配置 schema：启动时强校验，非法配置直接拒绝启动（FR-10.9、FR-16）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------- 应用与接入


class AppInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "robotchat"
    env: Literal["dev", "sim", "robot"] = "robot"


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    auth_token_env: str = "ROBOTCHAT_API_TOKEN"
    protocol_version: str = "v1"


# ---------------------------------------------------------------- 音频链路


class CaptureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_sample_rate: int = Field(default=16000, gt=0)
    target_channels: int = Field(default=1, ge=1)
    target_bit_depth: int = Field(default=16, gt=0)
    jitter_buffer_ms: int = Field(default=60, ge=0)


class PlaybackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_ms: int = Field(default=300, gt=0)
    flush_budget_ms: int = Field(default=100, gt=0)


class AecSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "hardware", "software", "none"] = "auto"
    fallback_notice: bool = True


class AudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["ros2_native", "ros2_bridge", "file"] = "ros2_native"
    device_profile: str = ""
    auto_detect_profile: bool = False
    fallback_profile: str = ""
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    playback: PlaybackSettings = Field(default_factory=PlaybackSettings)
    aec: AecSettings = Field(default_factory=AecSettings)

    @model_validator(mode="after")
    def _require_profile(self) -> AudioSettings:
        # FR-16.9 / FR-16.11：无显式 Profile、未开启自动匹配、也无兜底 Profile 时拒绝启动
        if not self.device_profile and not self.auto_detect_profile and not self.fallback_profile:
            raise ValueError(
                "audio.device_profile 为空，且 auto_detect_profile=false、fallback_profile 为空；"
                "必须指定机型 Profile 或允许自动匹配（FR-16.9 / FR-16.11）"
            )
        return self


class VadSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 模型后端：silero（默认，进程内 ONNX）| energy（无模型环境的诊断后端，ADR-05）
    backend: Literal["silero", "energy"] = "silero"
    model_path: str = "assets/silero_vad.onnx"
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    start_frames: int = Field(default=3, ge=1)
    silence_ms: int = Field(default=700, ge=0)  # 尾点判定，独立计量（NFR-1）
    max_utterance_ms: int = Field(default=30000, gt=0)


class BargeInSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["enabled", "disabled"] = "enabled"
    # 留空表示按 AEC 模式自适应：hardware 100 / software 200 / none 300
    guard_ms: int | None = Field(default=None, ge=0)
    threshold_boost_db: float = 6.0
    confirm_frames: int = Field(default=3, ge=1)
    semantic_check: bool = True
    echo_similarity_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


# ---------------------------------------------------------------- 状态机与运行时


class StateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 键为状态名，值为超时毫秒数；IDLE / ACTIVE_IDLE 为常驻等待，由全局心跳兜底（FR-7.2）
    timeouts_ms: dict[str, int] = Field(
        default_factory=lambda: {
            "BOOTING": 15000,
            "WAKING": 5000,
            "LISTENING": 15000,
            "THINKING": 30000,
            "SPEAKING": 60000,
            "INTERRUPTING": 1000,
            "EXECUTING": 120000,
            "DEGRADED": 30000,
            "ERROR": 60000,
        }
    )
    heartbeat_ms: int = Field(default=1000, gt=0)
    resident_states: list[str] = Field(default_factory=lambda: ["IDLE", "ACTIVE_IDLE"])

    @model_validator(mode="after")
    def _validate_timeouts(self) -> StateSettings:
        for state, timeout in self.timeouts_ms.items():
            if timeout <= 0:
                raise ValueError(f"state.timeouts_ms.{state} 必须为正数")
        overlap = set(self.resident_states) & set(self.timeouts_ms)
        if overlap:
            raise ValueError(
                f"常驻等待状态不得配置状态超时（FR-7.2）：{sorted(overlap)}"
            )
        return self


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "redis"] = "sqlite"
    sqlite_path: str = "/var/lib/robotchat/robotchat.sqlite3"
    max_turns: int = Field(default=20, ge=1)
    summary_trigger_turns: int = Field(default=10, ge=1)
    # 上下文预算 = LLM 上下文窗口 × 该比例（design §8：默认 70%）
    context_budget_ratio: float = Field(default=0.7, gt=0.0, le=1.0)
    # 滚动摘要保留的最大字符数（T-5.3 / C-73）
    summary_max_chars: int = Field(default=800, ge=100)


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    metrics_buffer_per_stage: int = Field(default=2000, ge=100)


class ResilienceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1)
    failure_window_seconds: int = Field(default=30, ge=1)
    cooldown_seconds: int = Field(default=30, ge=1)
    health_probe_interval: int = Field(default=30, ge=1)
    warmup_on_start: bool = True
    warmup_timeout_ms: int = Field(default=5000, gt=0)
    half_open_probe_timeout_ms: int = Field(default=2000, gt=0)
    # 离线检测（T-2.10）：启动探测公网，不可达时锁定 cloud=true 的驱动
    offline_probe_on_start: bool = True
    offline_probe_url: str = "https://dashscope.aliyuncs.com"
    offline_probe_timeout_ms: int = Field(default=1500, gt=0)


# ---------------------------------------------------------------- 语言


class LanguageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixed", "auto"] = "auto"
    fixed: str = "zh"
    supported: list[str] = Field(default_factory=lambda: ["zh", "en"])
    fallback: str = "zh"
    switch_confirm_turns: int = Field(default=2, ge=1)
    min_chars: int = Field(default=6, ge=0)
    asr_routing: dict[str, str] = Field(default_factory=dict)
    tts_voices: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_languages(self) -> LanguageSettings:
        supported = [item for item in self.supported if item]
        if not supported:
            raise ValueError("language.supported 不能为空")
        if self.fixed not in supported:
            raise ValueError(
                f"language.fixed={self.fixed!r} 不在 language.supported={supported} 中"
            )
        if self.fallback not in supported:
            raise ValueError(
                f"language.fallback={self.fallback!r} 不在 language.supported={supported} 中"
            )
        return self


# ---------------------------------------------------------------- Provider


class ProviderKindSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: str = ""
    primary: str | None = None
    fallback: str | None = None
    drivers: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def _validate_active(self, kind: str, allow_none: bool = False) -> None:
        if allow_none and self.active == "none":
            return
        if not self.active:
            raise ValueError(f"providers.{kind}.active 不能为空")
        if self.active not in self.drivers:
            raise ValueError(
                f"providers.{kind}.active={self.active!r} 未在 drivers 中定义；"
                f"可用驱动：{sorted(self.drivers)}"
            )
        if self.fallback and self.fallback not in self.drivers:
            raise ValueError(
                f"providers.{kind}.fallback={self.fallback!r} 未在 drivers 中定义"
            )


class ASRSettings(ProviderKindSettings):
    @model_validator(mode="after")
    def _check(self) -> ASRSettings:
        self._validate_active("asr")
        return self


class LLMSettings(ProviderKindSettings):
    @model_validator(mode="after")
    def _check(self) -> LLMSettings:
        self._validate_active("llm")
        return self


class TTSSettings(ProviderKindSettings):
    @model_validator(mode="after")
    def _check(self) -> TTSSettings:
        self._validate_active("tts")
        return self


class KnowledgeSettings(ProviderKindSettings):
    @model_validator(mode="after")
    def _check(self) -> KnowledgeSettings:
        # base_url 留空 = 关闭知识库增强（等价 active=none，启动 WARN 不阻断）
        self._validate_active("knowledge", allow_none=True)
        return self


class StandbySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    announce: bool = False
    announce_beep: bool = True


class OpenEarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 关闭唤醒词时默认本地 ASR；改用云端 ASR 须显式确认（FR-14.8）
    cloud_asr_ack: bool = False


class WakeFeedbackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["tts", "beep", "none"] = "tts"
    text: str = "我在"
    beep_path: str = "assets/sfx/wake.wav"


class WakewordSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    active: str = "openwakeword"
    active_window_ms: int = Field(default=60000, gt=0)
    preopen_asr: bool = True
    confirm_twice: bool = False
    drivers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    standby: StandbySettings = Field(default_factory=StandbySettings)
    open_ear: OpenEarSettings = Field(default_factory=OpenEarSettings)
    feedback: WakeFeedbackSettings = Field(default_factory=WakeFeedbackSettings)

    @model_validator(mode="after")
    def _check(self) -> WakewordSettings:
        if self.enabled and self.active not in self.drivers:
            raise ValueError(
                f"providers.wakeword.active={self.active!r} 未在 drivers 中定义；"
                f"可用驱动：{sorted(self.drivers)}"
            )
        return self


class ProvidersSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr: ASRSettings
    llm: LLMSettings
    tts: TTSSettings
    knowledge: KnowledgeSettings
    wakeword: WakewordSettings


# ---------------------------------------------------------------- 工具


class ToolPolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=30, ge=1)
    high_risk_requires_confirm: bool = True
    # 二次确认等待上限；超时或无法判定按取消处理，不默认执行（FR-9.10）
    confirm_timeout_ms: int = Field(default=10000, gt=0)


class ToolsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: ToolPolicySettings = Field(default_factory=ToolPolicySettings)
    definitions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 执行时长超过该阈值的工具转为异步后台任务，避免阻塞对话（T-5.9 / C-83）
    async_threshold_s: float = Field(default=3.0, ge=0.0)
    # 单轮最多允许的「工具执行 → 结果回灌 LLM」轮数，防止无限循环
    max_rounds: int = Field(default=2, ge=1, le=5)


# ---------------------------------------------------------------- 根配置


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppInfo = Field(default_factory=AppInfo)
    api: ApiSettings = Field(default_factory=ApiSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    vad: VadSettings = Field(default_factory=VadSettings)
    barge_in: BargeInSettings = Field(default_factory=BargeInSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)
    language: LanguageSettings = Field(default_factory=LanguageSettings)
    providers: ProvidersSettings
    tools: ToolsSettings = Field(default_factory=ToolsSettings)

    @model_validator(mode="after")
    def _check_open_ear(self) -> Settings:
        """关闭唤醒词时须保证本地 ASR；改用云端 ASR 须显式确认（FR-14.8）。"""
        if self.providers.wakeword.enabled:
            return self
        asr = self.providers.asr
        driver = asr.drivers.get(asr.active, {})
        is_cloud = bool(driver.get("cloud", False)) or bool(
            asr.fallback and asr.drivers.get(asr.fallback, {}).get("cloud", False)
        )
        if is_cloud and not self.providers.wakeword.open_ear.cloud_asr_ack:
            raise ValueError(
                "关闭唤醒词（常开聆听）时启用云端 ASR 须显式置 "
                "providers.wakeword.open_ear.cloud_asr_ack=true（默认要求本地 ASR，FR-14.8）"
            )
        return self

    @model_validator(mode="after")
    def _check_language_routing(self) -> Settings:
        """语言路由与音色映射必须引用已定义的驱动（T-2A.2 / T-2A.6）。"""
        asr_drivers = self.providers.asr.drivers
        for lang, name in self.language.asr_routing.items():
            if name not in asr_drivers:
                raise ValueError(
                    f"language.asr_routing.{lang}={name!r} 未在 providers.asr.drivers 中定义；"
                    f"可用驱动：{sorted(asr_drivers)}"
                )
        tts_drivers = self.providers.tts.drivers
        for lang, cfg in self.language.tts_voices.items():
            name = str(cfg.get("driver", "") or "")
            if name and name not in tts_drivers:
                raise ValueError(
                    f"language.tts_voices.{lang}.driver={name!r} 未在 providers.tts.drivers "
                    f"中定义；可用驱动：{sorted(tts_drivers)}"
                )
        return self


def build_settings(raw: dict[str, Any]) -> Settings:
    """由合并后的原始 dict 构造并校验配置。"""
    return Settings.model_validate(raw)


# ---------------------------------------------------------------- 机型 Profile（T-1A.2）


class ProfileTimestamp(BaseModel):
    """采集时间戳语义（design §3.6.4）。"""

    model_config = ConfigDict(extra="forbid")

    source: Literal["header_stamp", "arrival"] = "arrival"
    unit: Literal["ns", "us", "ms", "s"] = "s"
    monotonic: bool = True


class ProfileQos(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reliability: Literal["best_effort", "reliable"] = "best_effort"
    depth: int = Field(default=10, ge=1)


class ProfileCapture(BaseModel):
    """采集接口：ROS2 话题，或脱离 ROS2 的组播 / 原生 SDK 音频流（D12）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["topic", "udp_multicast"] = "topic"
    topic: str = ""
    msg_type: str = ""
    # 载荷形态：标准 ROS 消息 / 内嵌 protobuf（A2、A3 的 RosMsgWrapper）/ 裸 PCM（宇树 G1）
    payload: Literal["ros_msg", "protobuf", "raw_pcm"] = "ros_msg"
    payload_type: str = ""
    multicast_address: str = ""
    multicast_port: int = Field(default=0, ge=0)
    # 字段映射：data / stream / vad / ref 对应各自消息里的字段名
    fields: dict[str, str] = Field(default_factory=lambda: {"data": "data"})
    sample_rate: int = Field(gt=0)
    channels: int = Field(default=1, ge=1)
    bit_depth: int = 16
    frame_ms: int = Field(default=20, gt=0)
    qos: ProfileQos = Field(default_factory=ProfileQos)
    timestamp: ProfileTimestamp = Field(default_factory=ProfileTimestamp)

    @model_validator(mode="after")
    def _check_format(self) -> ProfileCapture:
        if self.bit_depth not in (8, 16, 32):
            raise ValueError(f"capture.bit_depth={self.bit_depth} 仅支持 8 / 16 / 32")
        if self.kind == "topic":
            if not self.topic or not self.msg_type:
                raise ValueError("capture.topic / capture.msg_type 不能为空")
            if self.payload == "protobuf" and not self.payload_type:
                raise ValueError("capture.payload=protobuf 时须声明 payload_type")
        else:
            if not self.multicast_address or self.multicast_port <= 0:
                raise ValueError(
                    "capture.kind=udp_multicast 时须声明 multicast_address / multicast_port"
                )
            if self.payload != "raw_pcm":
                raise ValueError("capture.kind=udp_multicast 仅支持 payload=raw_pcm")
        return self


class ProfileFlush(BaseModel):
    """立即停播方式（FR-16.6）：service / topic_msg / stream_stop / volume_to_zero / none。

    `none` 表示接口层无停播能力（A2、A3 的流式 PCM 即如此），
    barge-in 只能退化到音量置零或放弃音频焦点。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["stream_stop", "service", "topic_msg", "volume_to_zero", "none"] = (
        "stream_stop"
    )
    service: str = ""
    topic: str = ""
    msg_type: str = ""
    payload: str = ""

    @model_validator(mode="after")
    def _check_target(self) -> ProfileFlush:
        if self.kind == "service" and not self.service:
            raise ValueError("flush.kind=service 时须声明 flush.service")
        if self.kind == "topic_msg" and not self.topic:
            raise ValueError("flush.kind=topic_msg 时须声明 flush.topic")
        return self


class ProfileFocus(BaseModel):
    """音频焦点：智元机型播放前须申请，否则与本体自带 TTS / 交互抢占音频流（D12）。"""

    model_config = ConfigDict(extra="forbid")

    request_service: str = ""
    abandon_service: str = ""
    request_msg_type: str = ""
    abandon_msg_type: str = ""
    priority: int = Field(default=6, ge=0, le=10)
    weight: str = ""

    @model_validator(mode="after")
    def _check_service(self) -> ProfileFocus:
        if not self.request_service:
            raise ValueError("focus.request_service 不能为空")
        if not self.request_msg_type:
            raise ValueError("focus.request_msg_type 不能为空（服务类型须显式声明）")
        return self


class ProfilePlayback(BaseModel):
    """播放接口：ROS2 话题，或 DDS RPC（宇树 G1 的 AudioClient，不经 ROS2）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["topic", "dds_rpc"] = "topic"
    topic: str = ""
    msg_type: str = ""
    # 非 ROS2 播放：SDK 方法与停播方法（如 AudioClient::PlayStream / PlayStop）
    rpc_api: str = ""
    rpc_stop_api: str = ""
    payload: Literal["ros_msg", "raw_pcm"] = "raw_pcm"
    fields: dict[str, str] = Field(default_factory=lambda: {"data": "data"})
    sample_rate: int = Field(gt=0)
    channels: int = Field(default=1, ge=1)
    bit_depth: int = 16
    latency_ms: int = Field(default=60, ge=0)
    focus: ProfileFocus | None = None
    flush: ProfileFlush = Field(default_factory=ProfileFlush)

    @model_validator(mode="after")
    def _check_format(self) -> ProfilePlayback:
        if self.bit_depth not in (8, 16, 32):
            raise ValueError(f"playback.bit_depth={self.bit_depth} 仅支持 8 / 16 / 32")
        if self.kind == "topic":
            if not self.topic or not self.msg_type:
                raise ValueError("playback.topic / playback.msg_type 不能为空")
        elif not self.rpc_api:
            raise ValueError("playback.kind=dds_rpc 时须声明 rpc_api")
        return self


class ProfileVolume(BaseModel):
    """音量控制入口：ROS2 话题 / ROS2 服务（灵犀 X2）/ HTTP RPC（A2、A3）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["topic", "ros2_service", "http_rpc"] = "topic"
    topic: str = ""
    msg_type: str = "std_msgs/msg/Float32"
    service: str = ""
    endpoint: str = ""
    field: str = "volume"
    mute_field: str = ""
    range: tuple[float, float] = (0.0, 1.0)

    @model_validator(mode="after")
    def _check_range(self) -> ProfileVolume:
        low, high = self.range
        if low >= high:
            raise ValueError(f"volume.range 非法：{self.range}")
        if self.kind == "topic" and not self.topic:
            raise ValueError("volume.kind=topic 时须声明 volume.topic")
        if self.kind == "ros2_service" and not self.service:
            raise ValueError("volume.kind=ros2_service 时须声明 volume.service")
        if self.kind == "http_rpc" and not self.endpoint:
            raise ValueError("volume.kind=http_rpc 时须声明 volume.endpoint")
        return self


class ProfileCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture: bool = True
    playback: bool = True
    echo_reference: bool = True
    hardware_aec: bool = False
    volume_control: bool = True
    playback_flush: Literal[
        "stream_stop", "service", "topic_msg", "volume_to_zero", "none"
    ] = "stream_stop"


class DeviceProfileSchema(BaseModel):
    """机型 Profile：接口细节全部配置化，业务代码不硬编码话题 / 服务（FR-16.2）。"""

    model_config = ConfigDict(extra="forbid")

    profile: str
    display_name: str = ""
    ros_distro: list[str] = Field(default_factory=list)
    ros_domain_id: int | None = Field(default=None, ge=0)
    robot_models: list[str] = Field(default_factory=list)
    adapter: str = "base_ros_audio"
    capabilities: ProfileCapabilities = Field(default_factory=ProfileCapabilities)
    capture: ProfileCapture
    playback: ProfilePlayback
    volume: ProfileVolume | None = None
    # 上线前置操作（关闭本体自带语音系统、切唤醒模式等，D12 采集）
    preflight: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> DeviceProfileSchema:
        """能力声明与实现必须一致（FR-16.4 / C-167），不一致直接拒绝启动。"""
        caps = self.capabilities
        if not caps.capture:
            raise ValueError("capabilities.capture=false 与采集配置矛盾")
        if not caps.playback:
            raise ValueError("capabilities.playback=false 与播放配置矛盾")
        if caps.playback_flush != self.playback.flush.kind:
            raise ValueError(
                f"capabilities.playback_flush={caps.playback_flush!r} 与 "
                f"playback.flush.kind={self.playback.flush.kind!r} 不一致"
            )
        if caps.volume_control and self.volume is None:
            raise ValueError("capabilities.volume_control=true 但缺少 volume 配置")
        if caps.hardware_aec and not caps.echo_reference:
            raise ValueError("capabilities.hardware_aec=true 必须同时 echo_reference=true")
        return self
