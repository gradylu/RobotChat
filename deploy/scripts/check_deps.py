"""依赖白名单与镜像体积守门（T-0.9、C-111 ~ C-113）。

用法：
    python deploy/scripts/check_deps.py                 # 校验声明依赖 + 当前环境
    python deploy/scripts/check_deps.py --image IMG     # 追加校验镜像体积
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

# design §1.1：应用镜像禁止重型推理框架
FORBIDDEN = {
    "torch": "禁止在应用镜像内引入 PyTorch",
    "torchaudio": "禁止在应用镜像内引入 PyTorch 生态",
    "torchvision": "禁止在应用镜像内引入 PyTorch 生态",
    "transformers": "禁止在应用镜像内引入 transformers",
    "funasr": "ASR 必须作为外部服务（ADR-04）",
    "vllm": "LLM 必须部署在边端 / 云端（CON-12）",
    "tensorflow": "禁止重型推理框架",
    "paddlepaddle": "禁止重型推理框架",
    "onnxruntime-gpu": "VAD / KWS 仅需 CPU 版 onnxruntime",
    "scipy": "软件 AEC 用 NumPy 自实现（ADR-06）",
}


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def read_declared_dependencies(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:  # Python 3.10
        deps: list[str] = []
        for line in text.splitlines():
            stripped = line.strip().strip('",')
            if stripped and not stripped.startswith(("#", "[")):
                deps.append(stripped)
        return deps

    data = tomllib.loads(text)
    project = data.get("project", {})
    declared = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        declared.extend(group)
    return declared


def check_declared(path: Path) -> list[str]:
    failures: list[str] = []
    for spec in read_declared_dependencies(path):
        name = spec.split(";")[0]
        for separator in ("[", "=", "<", ">", "!", "~", " "):
            name = name.split(separator)[0]
        package = _normalize(name)
        if package in FORBIDDEN:
            failures.append(f"pyproject 声明了禁用依赖 {package}：{FORBIDDEN[package]}")
    return failures


def check_installed() -> list[str]:
    failures: list[str] = []
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - 3.10+ 必有
        return failures

    for dist in metadata.distributions():
        name = _normalize(dist.metadata["Name"] or "")
        if name in FORBIDDEN:
            failures.append(f"当前环境已安装禁用依赖 {name}：{FORBIDDEN[name]}")
    return failures


def image_size_mb(image: str) -> float:
    output = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Size}}", image],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(output) / (1024 * 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖白名单与镜像体积守门")
    parser.add_argument("--image", help="可选：待校验的镜像名，例如 robotchat:0.1.0-humble")
    parser.add_argument(
        "--max-image-mb",
        type=float,
        default=1600.0,
        help="方案 A 镜像体积上限（C-111：≤ 1.6 GB）",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    failures.extend(check_declared(PYPROJECT))
    failures.extend(check_installed())

    if args.image:
        try:
            size = image_size_mb(args.image)
        except (subprocess.CalledProcessError, ValueError) as exc:
            failures.append(f"无法读取镜像体积（{args.image}）：{exc}")
        else:
            print(f"镜像体积：{size:.1f} MB（上限 {args.max_image_mb:.0f} MB）")
            if size > args.max_image_mb:
                failures.append(
                    f"镜像体积 {size:.1f} MB 超过上限 {args.max_image_mb:.0f} MB（C-111）"
                )

    if failures:
        print("依赖 / 体积守门失败：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print("依赖 / 体积守门通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
