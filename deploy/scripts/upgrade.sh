#!/usr/bin/env bash
# 升级（T-7.11 / C-129，deployment §11.1）
#
# 流程：备份 → 记录当前版本 → 更新 .env → 加载新镜像 → 重建容器 → 等待就绪 → 自检
# 就绪失败时保留回滚状态文件，执行 scripts/rollback.sh 即可恢复上一版本。
#
# 用法：
#   ./scripts/upgrade.sh --version 0.2.0
#   ./scripts/upgrade.sh --version 0.2.0 --image robotchat-0.2.0-humble.tar
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

VERSION=""
IMAGE_TAR=""
DISTRO=""
READY_TIMEOUT=300
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --image)
            IMAGE_TAR="$2"
            shift 2
            ;;
        --distro)
            DISTRO="$2"
            shift 2
            ;;
        --ready-timeout)
            READY_TIMEOUT="$2"
            shift 2
            ;;
        -h | --help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            die "未知参数：$1"
            ;;
    esac
done

[[ -n "${VERSION}" ]] || die "必须指定 --version（生产环境禁止 latest，deployment §11.3）"
require_docker
load_env

PREV_VERSION="${ROBOTCHAT_VERSION:-unknown}"
PREV_IMAGE="$(image_name)"

log "升级前备份（配置 + 数据卷）"
"${SCRIPT_DIR}/backup.sh"

{
    printf 'PREV_VERSION=%s\n' "${PREV_VERSION}"
    printf 'PREV_IMAGE=%s\n' "${PREV_IMAGE}"
    printf 'UPGRADED_AT=%s\n' "$(date -Iseconds)"
} >"${STATE_FILE}"
chmod 600 "${STATE_FILE}"

NEW_IMAGE="robotchat:${VERSION}-${DISTRO:-${ROS_DISTRO:-humble}}"
if [[ -n "${IMAGE_TAR}" ]]; then
    [[ -f "${IMAGE_TAR}" ]] || die "镜像包不存在：${IMAGE_TAR}"
    log "加载离线镜像 ${IMAGE_TAR}"
    docker load -i "${IMAGE_TAR}"
fi
docker image inspect "${NEW_IMAGE}" >/dev/null 2>&1 ||
    die "镜像 ${NEW_IMAGE} 不存在：请先 docker load 或 docker compose pull"

set_env_value ROBOTCHAT_VERSION "${VERSION}" "${ENV_FILE}"
if [[ -n "${DISTRO}" ]]; then
    set_env_value ROS_DISTRO "${DISTRO}" "${ENV_FILE}"
fi
load_env

log "重建并启动 ${NEW_IMAGE}"
compose up -d

if ! wait_ready "${READY_TIMEOUT}"; then
    compose logs --tail=100 robotchat || true
    warn "升级后未就绪：执行 scripts/rollback.sh 回滚到 ${PREV_VERSION}"
    exit 3
fi

set +e
"${SCRIPT_DIR}/selfcheck.sh"
SELFCHECK_CODE=$?
set -e
[[ "${SELFCHECK_CODE}" -eq 0 ]] || warn "升级完成但自检存在 FAIL 项，请按 hint 处理"

log "升级完成：${PREV_VERSION} → ${VERSION}（回滚命令：scripts/rollback.sh）"
