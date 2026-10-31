#!/usr/bin/env bash
# 恢复（T-7.12 / C-126）
#
# 从 backup.sh 产出的 tar.gz 恢复现场配置与数据卷。恢复会覆盖现有 config/ 与数据卷，
# 必须显式传 --yes；升级回滚时建议先自动备份当前状态。
#
# 用法：
#   ./scripts/restore.sh --from backups/robotchat-backup-20261030-120000.tar.gz --yes
#   ./scripts/restore.sh --from <archive> --yes --config-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

FROM=""
CONFIG_ONLY=0
CONFIRMED=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)
            FROM="$2"
            shift 2
            ;;
        --config-only)
            CONFIG_ONLY=1
            shift
            ;;
        --yes)
            CONFIRMED=1
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

[[ -n "${FROM}" ]] || die "必须指定 --from <备份包>"
[[ -f "${FROM}" ]] || die "备份包不存在：${FROM}"
[[ "${CONFIRMED}" -eq 1 ]] || die "恢复会覆盖 config/ 与数据卷，确认请追加 --yes"
require_docker

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
log "解包 ${FROM}"
tar xzf "${FROM}" -C "${STAGE}"
[[ -f "${STAGE}/config.tar.gz" ]] || die "备份包缺少 config.tar.gz"

log "停止应用"
compose stop robotchat

log "恢复现场配置"
tar xzf "${STAGE}/config.tar.gz" -C "${ROOT_DIR}"

if [[ "${CONFIG_ONLY}" -eq 0 ]]; then
    [[ -f "${STAGE}/data.tar.gz" ]] || die "备份包缺少 data.tar.gz"
    log "恢复数据卷（覆盖现有数据）"
    docker run --rm --volumes-from robotchat -v "${STAGE}:/backup:ro" \
        alpine:3.20 sh -c 'rm -rf /var/lib/robotchat/* && tar xzf /backup/data.tar.gz -C /var/lib/robotchat'
fi

log "启动应用"
compose up -d
wait_ready 180 || warn "服务未就绪，请执行 scripts/selfcheck.sh"
log "恢复完成"
