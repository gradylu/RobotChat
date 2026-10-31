# RobotChat 应用镜像（方案 A：容器内直连 DDS，deployment §3）
# 构建：docker build -f deploy/Dockerfile --build-arg ROS_DISTRO=humble -t robotchat:0.1.0-humble .
# 双发行版（humble / jazzy）由 CI 矩阵并行构建（CON-10、R21）
ARG ROS_DISTRO=humble

# ---------------------------------------------------------------- WebUI 构建
FROM node:20-alpine AS webui
WORKDIR /build
COPY webui/ ./webui/
RUN if [ -f webui/package.json ]; then \
        cd webui && npm ci --no-audit --no-fund && npm run build && cp -r dist /out; \
    else \
        mkdir -p /out; \
    fi

# ---------------------------------------------------------------- 运行时镜像
FROM ros:${ROS_DISTRO}-ros-base

ARG ROS_DISTRO
# 构建期 pip 源（国内直连 pypi.org 常超时，可在 compose / CI 传镜像站）
ARG PIP_INDEX_URL=https://pypi.org/simple
# 镜像内校验发行版与标签一致（deployment §3.1：不一致直接 FAIL）
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="robotchat" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.base="ros:${ROS_DISTRO}-ros-base"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/robotchat \
    ROBOTCHAT_CONFIG_DIR=/app/config \
    ROBOTCHAT_DATA_DIR=/var/lib/robotchat \
    ROBOTCHAT_LOG_DIR=/var/log/robotchat \
    ROBOTCHAT_LOG_RETENTION_DAYS=14 \
    ROBOTCHAT_IMAGE_DISTRO=${ROS_DISTRO}

# 线程数限制（deployment §12.2）：避免 onnxruntime / NumPy 线程风暴
ENV OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2

# Python 版本跟随 ROS2 发行版（Humble=3.10 / Jazzy=3.12，ADR-03 / ADR-15）
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# 先装依赖再拷源码，最大化层缓存（deployment §3.5）
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY config/ ./config/
# VAD / 唤醒词 ONNX 与提示音（deployment §2 资产清单；不含任何大模型权重）
COPY assets/ ./assets/
COPY --from=webui /out ./webui/dist

RUN pip install --upgrade pip --index-url "${PIP_INDEX_URL}" \
    && pip install --index-url "${PIP_INDEX_URL}" . \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && rm -rf /root/.cache /tmp/*

# 非 root 运行（uid 1000，deployment §13）：仅数据卷与日志卷可写
RUN groupadd -g 1000 robotchat \
    && useradd -u 1000 -g robotchat -d /home/robotchat -m -s /usr/sbin/nologin robotchat \
    && mkdir -p /var/lib/robotchat /var/log/robotchat \
    && chown -R robotchat:robotchat /var/lib/robotchat /var/log/robotchat

USER robotchat

EXPOSE 8080
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/ready').read()"

CMD ["python3", "-m", "robotchat.main"]
