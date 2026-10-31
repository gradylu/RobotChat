#!/usr/bin/env bash
# 启动自检（T-7.5 / C-123、C-168、C-171、C-174）
#
# 优先调用运行中服务的 POST /api/diagnostics/selfcheck（18 项）；
# 服务不可达时回退到容器内 `python -m robotchat.cli doctor`。
#
# 用法：
#   ./scripts/selfcheck.sh            # 人读格式，WARN 不阻断
#   ./scripts/selfcheck.sh --json     # 输出 JSON（便于回传）
#   ./scripts/selfcheck.sh --strict   # 存在 FAIL 时退出码 1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

AS_JSON=0
STRICT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            AS_JSON=1
            shift
            ;;
        --strict)
            STRICT=1
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
TOKEN="${ROBOTCHAT_API_TOKEN:-}"

TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT

if ! curl -fsS -X POST -H "X-API-Token: ${TOKEN}" \
    "${API_URL}/api/diagnostics/selfcheck" -o "${TMP_FILE}" 2>/dev/null; then
    warn "HTTP 自检不可用（服务未启动或未提供写操作口令），回退容器内 cli doctor"
    compose exec -T robotchat python3 -m robotchat.cli doctor --json >"${TMP_FILE}"
fi

if command -v python3 >/dev/null 2>&1; then
    overall="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["overall"])' "${TMP_FILE}")"
    if [[ "${AS_JSON}" -eq 1 ]]; then
        python3 -m json.tool "${TMP_FILE}"
    else
        python3 - "${TMP_FILE}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
for item in data.get("items", []):
    print(f'[{item["status"]}] {item["name"]}  {item["detail"]}')
    for hint in item.get("hints", []):
        print(f'       hint: {hint}')
print("-" * 60)
counts = data.get("counts", {})
print(
    "RESULT: {} (PASS {} / WARN {} / FAIL {})".format(
        data.get("overall"),
        counts.get("PASS", 0),
        counts.get("WARN", 0),
        counts.get("FAIL", 0),
    )
)
PY
    fi
else
    cat "${TMP_FILE}"
    overall="$(grep -o '"overall": *"[A-Z]*"' "${TMP_FILE}" | head -1 | sed 's/.*"\([A-Z]*\)"/\1/' || echo UNKNOWN)"
fi

if [[ "${overall}" == "FAIL" ]]; then
    warn "自检存在 FAIL 项（服务默认仍以降级方式运行）"
    [[ "${STRICT}" -eq 1 ]] && exit 1
fi
exit 0
