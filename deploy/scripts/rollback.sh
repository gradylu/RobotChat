#!/usr/bin/env bash
# 回滚（T-7.11 / C-130，deployment §11.2）
#
# 读取 upgrade.sh 记录的状态文件，恢复到上一版本镜像；可选同时恢复升级前配置快照。
#
# 用法：
#   ./scripts/rollback.sh
#   ./scripts/rollback.sh --config backups/robotchat-backup-20261030-120000.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

CONFIG_ARCHIVE=""
READY_TIMEOUT=300
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_ARCHIVE="$2"
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

[[ -f "${STATE_FILE}" ]] || die "缺少 ${STATE_FILE}：未记录可回滚版本，请手动设置 ROBOTCHAT_VERSION"
require_docker
load_env
# shellcheck disable=SC1090
source "${STATE_FILE}"

[[ "${PREV_VERSION}" != "unknown" ]] || die "状态文件未记录有效版本"

log "回滚到 ${PREV_IMAGE}（原版本 ${PREV_VERSION}）"
docker image inspect "${PREV_IMAGE}" >/dev/null 2>&1 ||
    die "本地缺少 ${PREV_IMAGE}：docker load 旧镜像后重试"

set_env_value ROBOTCHAT_VERSION "${PREV_VERSION}" "${ENV_FILE}"
load_env
compose up -d

if ! wait_ready "${READY_TIMEOUT}"; then
    compose logs --tail=100 robotchat || true
    die "回滚后服务未就绪，请执行 scripts/selfcheck.sh 现场排查"
fi

if [[ -n "${CONFIG_ARCHIVE}" ]]; then
    log "恢复升级前配置快照"
    "${SCRIPT_DIR}/restore.sh" --from "${CONFIG_ARCHIVE}" --yes --config-only
fi

"${SCRIPT_DIR}/selfcheck.sh" || warn "回滚完成但自检存在 FAIL 项"
log "回滚完成：当前版本 ${PREV_VERSION}"
