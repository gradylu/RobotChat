"""ROS2 假机器人音频节点（deploy/docker-compose.sim.yml 配套）。

在没有真实机器人硬件时，用容器扮演“机器人那一侧”：

- 采集：把 16 kHz / 单声道 / 16bit 的 WAV 按 20 ms 分块推到 `/mic/audio_raw`；
- 播放：订阅 `/spk/audio_out`，把应用播出的 TTS 落成 wav（每句一个文件）；
- 停播：响应 `/spk/stop`（std_srvs/Trigger），立即结束当前句；
- 音量：订阅 `/spk/volume`，只记录不发声。

只依赖 ROS2 基础镜像自带的 rclpy / std_msgs / std_srvs，不需要 audio_common_msgs，
对应 `config/devices/sim_virtual_audio.yaml`。
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from array import array
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float32, UInt8MultiArray  # type: ignore[import-not-found]
from std_srvs.srv import Trigger  # type: ignore[import-not-found]

SAMPLE_RATE = 16000
CHUNK_BYTES = 2
REPORT_INTERVAL_S = 5.0
UTTERANCE_IDLE_S = 0.5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ROS2 假机器人音频节点")
    parser.add_argument("--input", default="/sim-audio/demo.wav", help="回放用的 WAV 路径")
    parser.add_argument("--out-dir", default="/out", help="TTS 落盘目录")
    parser.add_argument("--capture-topic", default="/mic/audio_raw")
    parser.add_argument("--playback-topic", default="/spk/audio_out")
    parser.add_argument("--volume-topic", default="/spk/volume")
    parser.add_argument("--stop-service", default="/spk/stop")
    parser.add_argument("--sample-rate", type=int, default=SAMPLE_RATE)
    parser.add_argument("--frame-ms", type=int, default=20, help="采集分块时长")
    parser.add_argument("--gap-ms", type=int, default=1500, help="每轮回放后的静音间隔")
    parser.add_argument("--repeat", type=int, default=0, help="回放轮数，0 = 无限")
    return parser.parse_args(argv)


def read_chunks(path: Path, sample_rate: int, frame_ms: int) -> list[bytes]:
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != CHUNK_BYTES:
            raise ValueError(f"仅支持 16bit WAV：{path}")
        if reader.getnchannels() != 1:
            raise ValueError(f"仅支持单声道 WAV：{path}")
        if reader.getframerate() != sample_rate:
            raise ValueError(f"采样率必须是 {sample_rate} Hz：{path}")
        raw = reader.readframes(reader.getnframes())
    step = int(sample_rate * frame_ms / 1000) * CHUNK_BYTES
    return [raw[index : index + step] for index in range(0, len(raw), step)]


class PlaybackSink:
    """把应用播出的 PCM 分句落盘；静音间隔或停播服务都会结束当前句。"""

    def __init__(self, out_dir: Path, sample_rate: int) -> None:
        self.out_dir = out_dir
        self.sample_rate = sample_rate
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.buffer = bytearray()
        self.last_at = 0.0
        self.index = 0
        self.written = 0

    def append(self, payload: bytes) -> None:
        if not payload:
            return
        self.buffer.extend(payload)
        self.last_at = time.monotonic()

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        path = self.out_dir / f"spk_{self.index:04d}.wav"
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(CHUNK_BYTES)
            writer.setframerate(self.sample_rate)
            writer.writeframes(bytes(self.buffer))
        self.written += 1
        self.index += 1
        self.buffer.clear()
        return path


class FakeRobotNode(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("fake_robot_audio")
        self.args = args
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self.capture_pub = self.create_publisher(
            UInt8MultiArray, args.capture_topic, qos
        )
        self.create_subscription(
            UInt8MultiArray, args.playback_topic, self._on_playback, 10
        )
        self.create_subscription(Float32, args.volume_topic, self._on_volume, 10)
        self.create_service(Trigger, args.stop_service, self._on_stop)
        self.sink = PlaybackSink(Path(args.out_dir), args.sample_rate)
        self.published = 0
        self.received_bytes = 0
        self.volume = 1.0
        self.get_logger().info(
            f"fake robot ready: capture={args.capture_topic} "
            f"playback={args.playback_topic} out={args.out_dir}"
        )

    # ------------------------------------------------------------ 回调

    def _on_playback(self, msg: Any) -> None:
        payload = bytes(msg.data)
        self.received_bytes += len(payload)
        self.sink.append(payload)

    def _on_volume(self, msg: Any) -> None:
        self.volume = float(msg.data)
        self.get_logger().info(f"volume -> {self.volume:.2f}")

    def _on_stop(self, request: Any, response: Any) -> Any:
        del request
        path = self.sink.flush()
        response.success = True
        response.message = f"stopped, flushed {path.name}" if path else "stopped"
        return response

    # ------------------------------------------------------------ 主循环

    def publish_frame(self, payload: bytes) -> None:
        message = UInt8MultiArray()
        message.data = array("B", payload)
        self.capture_pub.publish(message)
        self.published += 1

    def poll_playback(self) -> None:
        if self.sink.buffer and time.monotonic() - self.sink.last_at > UTTERANCE_IDLE_S:
            path = self.sink.flush()
            if path is not None:
                self.get_logger().info(f"utterance saved: {path}")

    def report(self) -> None:
        self.get_logger().info(
            f"published={self.published} chunks, received={self.received_bytes} bytes, "
            f"utterances={self.sink.written}"
        )

    def finish(self) -> None:
        self.poll_playback()
        path = self.sink.flush()
        if path is not None:
            self.get_logger().info(f"final utterance saved: {path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.input)
    chunks: list[bytes] = []
    if source.is_file():
        chunks = read_chunks(source, args.sample_rate, args.frame_ms)
    sample_bytes = int(args.sample_rate * args.frame_ms / 1000) * CHUNK_BYTES
    silence = b"\x00\x00" * (sample_bytes // 2)
    gap = [silence] * max(1, args.gap_ms // max(1, args.frame_ms))

    rclpy.init()
    node = FakeRobotNode(args)
    if chunks:
        node.get_logger().info(f"replaying {source} ({len(chunks)} frames per round)")
    else:
        node.get_logger().warning(
            f"输入 WAV 不存在：{source}；只发送静音帧（仍可验证 DDS 链路）"
        )

    frame_s = args.frame_ms / 1000.0
    rounds = 0
    next_report = time.monotonic() + REPORT_INTERVAL_S
    try:
        while rclpy.ok():
            for payload in chunks + gap:
                if not rclpy.ok():
                    break
                node.publish_frame(payload)
                deadline = time.monotonic() + frame_s
                while time.monotonic() < deadline and rclpy.ok():
                    rclpy.spin_once(node, timeout_sec=0.001)
                node.poll_playback()
                if time.monotonic() >= next_report:
                    node.report()
                    next_report += REPORT_INTERVAL_S
            rounds += 1
            if args.repeat and rounds >= args.repeat:
                break
    except KeyboardInterrupt:
        pass
    finally:
        node.finish()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
