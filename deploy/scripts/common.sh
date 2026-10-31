#!/usr/bin/env bash
# RobotChat 部署脚本公共函数（install / selfcheck / backup / restore / upgrade / rollback 复用）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${DEPLOY_DIR}/.." && pwd)"
ENV_FILE="${DEPLOY_DIR}/.env"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
MODELS_FILE="${DEPLOY_DIR}/docker-compose.models.yml"
API_URL="${ROBOTCHAT_API_URL:-http://127.0.0.1:8080}"
STATE_FILE="${DEPLOY_DIR}/.upgrade-state"

log() { printf '[robotchat] %s\n' "$*"; }
warn() { printf '[robotchat][WARN] %s\n' "$*" >&2; }
die() {
    printf '[robotchat][FAIL] %s\n' "$*" >&2
    exit 1
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "未安装 docker（deployment §1）"
    docker compose version >/dev/null 2>&1 || die "未安装 docker compose v2（D1）"
    command -v curl >/dev/null 2>&1 || die "缺少 curl，无法进行就绪探测"
}

load_env() {
    [[ -f "${ENV_FILE}" ]] || die "缺少 ${ENV_FILE}，请先执行 scripts/install.sh"
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
}

compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

compose_models() {
    docker compose --env-file "${ENV_FILE}" -f "${MODELS_FILE}" "$@"
}

detect_ros_distro() {
    if [[ -n "${ROS_DISTRO:-}" ]]; then
        printf '%s' "${ROS_DISTRO}"
        return 0
    fi
    if [[ -d /opt/ros/jazzy ]]; then
        printf 'jazzy'
    elif [[ -d /opt/ros/humble ]]; then
        printf 'humble'
    else
        printf 'humble'
    fi
}

set_env_value() {
    local key="$1" value="$2" file="${3:-${ENV_FILE}}"
    if grep -qE "^${key}=" "${file}"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "${file}"
    else
        printf '%s=%s\n' "${key}" "${value}" >>"${file}"
    fi
}

image_name() {
    printf 'robotchat:%s-%s' "${ROBOTCHAT_VERSION:-0.1.0}" "${ROS_DISTRO:-humble}"
}

wait_ready() {
    local timeout="${1:-300}" elapsed=0
    while ((elapsed < timeout)); do
        if curl -fsS "${API_URL}/api/ready" >/dev/null 2>&1; then
            log "服务已就绪（${elapsed}s）"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    warn "等待就绪超时（${timeout}s），当前 /api/ready："
    curl -sS "${API_URL}/api/ready" || true
    printf '\n'
    return 1
}
