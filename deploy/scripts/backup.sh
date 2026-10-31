#!/usr/bin/env bash
# 备份（T-7.12 / C-126、deployment §10）
#
# 备份内容：现场配置（config/）+ 数据卷（SQLite / 会话 / 运行时配置）+ 清单
# 不含密钥：.env 含口令与 API Key，不进入备份包（按现场安全策略单独保管）
#
# 用法：
#   ./scripts/backup.sh                         # 备份到 deploy/backups/
#   ./scripts/backup.sh --out /data/backups
#   ./scripts/backup.sh --stop                  # 先停应用再备份（SQLite 一致性最好）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

OUT_DIR="${DEPLOY_DIR}/backups"
STOP_FIRST=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)
            OUT_DIR="$2"
            shift 2
            ;;
        --stop)
            STOP_FIRST=1
            shift
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

require_docker
load_env
mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd "${OUT_DIR}" && pwd)"

STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${OUT_DIR}/robotchat-backup-${STAMP}.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

if [[ "${STOP_FIRST}" -eq 1 ]]; then
    log "停止应用以保证 SQLite 一致性"
    compose stop robotchat
fi

log "打包现场配置"
tar czf "${STAGE}/config.tar.gz" -C "${ROOT_DIR}" config

log "导出数据卷"
if [[ "$(docker inspect -f '{{.State.Running}}' robotchat 2>/dev/null || true)" == "true" ]]; then
    compose exec -T robotchat tar czf - -C /var/lib/robotchat . >"${STAGE}/data.tar.gz"
else
    docker run --rm --volumes-from robotchat -v "${STAGE}:/backup:rw" \
        alpine:3.20 sh -c 'tar czf /backup/data.tar.gz -C /var/lib/robotchat .'
fi

cat >"${STAGE}/manifest.json" <<EOF
{
  "created_at": "$(date -Iseconds)",
  "app_version": "${ROBOTCHAT_VERSION:-unknown}",
  "ros_distro": "${ROS_DISTRO:-unknown}",
  "device_profile": "${ROBOTCHAT_DEVICE_PROFILE:-unknown}",
  "contents": ["config.tar.gz", "data.tar.gz"],
  "excludes": [".env（含口令与密钥，按安全策略单独保管）"],
  "stopped_first": ${STOP_FIRST}
}
EOF

tar czf "${ARCHIVE}" -C "${STAGE}" config.tar.gz data.tar.gz manifest.json
chmod 600 "${ARCHIVE}"

if [[ "${STOP_FIRST}" -eq 1 ]]; then
    log "重新启动应用"
    compose up -d
    wait_ready 180 || warn "服务未就绪，请执行 scripts/selfcheck.sh"
fi

log "备份完成：${ARCHIVE}（$(du -h "${ARCHIVE}" | cut -f1)）"
