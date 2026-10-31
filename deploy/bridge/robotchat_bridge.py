#!/usr/bin/env python3
"""RobotChat 宿主机 ROS2 桥接节点（方案 B，T-1.8）。

在宿主机（或桥接容器）上以 rclpy 订阅麦克风话题、发布播放话题，并通过本地
WebSocket 与 robotchat 容器交换原始 PCM：

- 二进制帧：PCM16LE，采样率由 `--sample-rate` 约定
- 文本帧：`{"cmd": "flush"}` → 发布空音频块（流停止语义）

该脚本不属于应用镜像（应用镜像不含 rclpy 之外的 ROS2 逻辑），由
`deploy/Dockerfile.bridge` 或宿主机 Python 环境运行（ADR-02、ADR-15）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import threading
from array import array
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from websockets.sync.server import serve


def resolve_msg_type(spec: str) -> type:
    package, _, class_name = spec.partition("/msg/")
    if not class_name:
        raise ValueError(f"消息类型格式错误：{spec!r}（期望 pkg/msg/Type）")
    module = importlib.import_module(f"{package}.msg")
    return getattr(module, class_name)


class AudioBridgeNode(Node):
    """订阅麦克风并广播给 WebSocket 客户端；接收客户端 PCM 后发布到播放话题。"""

    def __init__(
        self,
        capture_topic: str,
        playback_topic: str,
        capture_type: str,
        playback_type: str,
    ) -> None:
        super().__init__("robotchat_bridge")
        self._capture_type = resolve_msg_type(capture_type)
        self._playback_type = resolve_msg_type(playback_type)

        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self._publisher = self.create_publisher(self._playback_type, playback_topic, 10)
        self._subscription = self.create_subscription(
            self._capture_type, capture_topic, self._on_audio, qos
        )
        self._clients: set[Any] = set()
        self._lock = threading.Lock()
        self.get_logger().info(
            f"桥接节点启动：capture={capture_topic} playback={playback_topic}"
        )

    # -------------------------------------------------------- ROS2 侧

    def _on_audio(self, msg: Any) -> None:
        payload = bytes(msg.data)
        with self._lock:
            stale: list[Any] = []
            for client in self._clients:
                try:
                    client.send(payload)
                except Exception:
                    stale.append(client)
            for client in stale:
                self._clients.discard(client)

    def publish_pcm(self, payload: bytes) -> None:
        msg = self._playback_type()
        msg.data = array("B", payload)
        self._publisher.publish(msg)

    def flush(self) -> None:
        self.publish_pcm(b"")

    # -------------------------------------------------------- WebSocket 侧

    def attach(self, client: Any) -> None:
        with self._lock:
            self._clients.add(client)

    def detach(self, client: Any) -> None:
        with self._lock:
            self._clients.discard(client)


def main() -> None:
    parser = argparse.ArgumentParser(description="RobotChat ROS2 ↔ WebSocket 桥接节点")
    parser.add_argument("--capture-topic", default="/mic/audio_raw")
    parser.add_argument("--playback-topic", default="/spk/audio_out")
    parser.add_argument(
        "--capture-msg-type", default="std_msgs/msg/UInt8MultiArray"
    )
    parser.add_argument(
        "--playback-msg-type", default="std_msgs/msg/UInt8MultiArray"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    rclpy.init()
    node = AudioBridgeNode(
        args.capture_topic,
        args.playback_topic,
        args.capture_msg_type,
        args.playback_msg_type,
    )

    def handler(client: Any) -> None:
        node.attach(client)
        try:
            for message in client:
                if isinstance(message, (bytes, bytearray)):
                    node.publish_pcm(bytes(message))
                    continue
                try:
                    command = json.loads(message)
                except json.JSONDecodeError:
                    node.get_logger().warning(f"忽略非法控制指令：{message}")
                    continue
                if command.get("cmd") == "flush":
                    node.flush()
        except Exception:
            node.get_logger().warning("WebSocket 客户端异常断开")
        finally:
            node.detach(client)

    spin = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin.start()

    try:
        with serve(handler, args.host, args.port) as server:
            node.get_logger().info(f"WebSocket 桥接监听 ws://{args.host}:{args.port}/audio")
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
