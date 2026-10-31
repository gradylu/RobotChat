#!/usr/bin/env bash
# 一键部署（T-7.4 / C-110、C-161、C-166）
#
# 流程：检测 ROS_DISTRO → 选择镜像标签 → 环境检查 → 生成 .env → 加载/构建镜像
#       → 启动模型与应用 → 等待就绪 → 18 项自检
#
# 用法：
#   ./scripts/install.sh                       # 自动检测发行版与默认机型 Profile
#   ./scripts/install.sh --profile my_robot --distro humble
#   ./scripts/install.sh --image robotchat-0.1.0-humble.tar
#   ./scripts/install.sh --no-models --systemd
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROFILE="generic_ros_audio"
DISTRO=""
IMAGE_TAR=""
WITH_MODELS=1
INSTALL_SYSTEMD=0
READY_TIMEOUT=300

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --distro)
            DISTRO="$2"
            shift 2
            ;;
        --image)
            IMAGE_TAR="$2"
            shift 2
            ;;
        --no-models)
            WITH_MODELS=0
            shift
            ;;
        --systemd)
            INSTALL_SYSTEMD=1
            shift
            ;;
        --ready-timeout)
            READY_TIMEOUT="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1（--help 查看用法）"
            ;;
    esac
done

require_docker

# 1. 发行版检测：显式参数 > 宿主机 ROS_DISTRO > /opt/ros；双发行版镜像标签（CON-10）
if [[ -z "${DISTRO}" ]]; then
    DISTRO="$(detect_ros_distro)"
fi
[[ "${DISTRO}" == "humble" || "${DISTRO}" == "jazzy" ]] ||
    die "ROS_DISTRO 只支持 humble / jazzy，收到：${DISTRO}"
log "ROS_DISTRO=${DISTRO}（架构 $(uname -m)）"
if [[ "$(uname -m)" == "aarch64" ]]; then
    log "ARM64 本体：建议本机原生构建镜像（deployment §3.5）"
fi

# 2. 生成 .env（含 ROS_DISTRO 与机型 Profile），权限 600
if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${DEPLOY_DIR}/.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    log "已生成 ${ENV_FILE}（权限 600，禁止提交版本库）"
else
    log "复用已有 ${ENV_FILE}"
fi
set_env_value ROS_DISTRO "${DISTRO}"
set_env_value ROBOTCHAT_DEVICE_PROFILE "${PROFILE}"
load_env

# 3. 镜像：优先加载离线 tar；本地无镜像时才构建
IMAGE="$(image_name)"
if [[ -n "${IMAGE_TAR}" ]]; then
    [[ -f "${IMAGE_TAR}" ]] || die "镜像包不存在：${IMAGE_TAR}"
    log "加载离线镜像 ${IMAGE_TAR}"
    docker load -i "${IMAGE_TAR}"
elif ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    log "本地无 ${IMAGE}，开始构建（需网络与 Node 构建阶段）"
    docker build -f "${DEPLOY_DIR}/Dockerfile" \
        --build-arg "ROS_DISTRO=${DISTRO}" \
        --build-arg "GIT_SHA=$(cd "${ROOT_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
        -t "${IMAGE}" "${ROOT_DIR}"
fi
docker image inspect "${IMAGE}" >/dev/null 2>&1 || die "镜像 ${IMAGE} 不可用"

# 4. 启动：模型服务（可选）与应用
if [[ "${WITH_MODELS}" -eq 1 ]]; then
    log "启动本体语音模型（拓扑 1）"
    compose_models up -d
fi
log "启动 robotchat 应用"
compose up -d

# 5. 等待就绪
if ! wait_ready "${READY_TIMEOUT}"; then
    compose logs --tail=100 robotchat || true
    die "服务未在 ${READY_TIMEOUT}s 内就绪，请执行 scripts/selfcheck.sh 排查"
fi

# 6. 自检（FAIL 不阻断部署，但给出明确提示）
set +e
"${SCRIPT_DIR}/selfcheck.sh"
SELFCHECK_CODE=$?
set -e
if [[ "${SELFCHECK_CODE}" -ne 0 ]]; then
    warn "自检存在 FAIL 项，服务仍以降级方式运行；按 hint 处理后执行 scripts/selfcheck.sh 复检"
fi

# 7. 可选：安装 systemd 单元（开机自启 / 崩溃恢复，T-7.7）
if [[ "${INSTALL_SYSTEMD}" -eq 1 ]]; then
    UNIT_SRC="${DEPLOY_DIR}/systemd/robotchat.service"
    UNIT_DST="/etc/systemd/system/robotchat.service"
    [[ -f "${UNIT_SRC}" ]] || die "缺少 ${UNIT_SRC}"
    install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
    sed -i "s|/opt/robotchat|${ROOT_DIR}|g" "${UNIT_DST}"
    systemctl daemon-reload
    systemctl enable --now robotchat.service
    log "systemd 单元已安装：${UNIT_DST}"
fi

log "部署完成：WebUI ${API_URL}（镜像 ${IMAGE}，Profile ${PROFILE}）"
