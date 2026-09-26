#!/bin/bash

# ============================ 配置参数 ============================
# 源目录
SOURCE_DIR="/home/apyyc/ass-Computational/DEBIAN13/Debian13_project/data_warehouse/podman/"

# 目的目录列表（支持多个），每行一个，格式：用户名@主机:SSH端口:远程目录
# 需要新增/删除目的目录时，直接在此追加或注释对应行即可；以 # 开头的行会被跳过
REMOTE_DESTINATIONS=(
    # "root@103.236.99.177:50488:/docker/czdt-staging"
    # "root@120.26.29.177:22:/srv/docker/czdt-production"
)

# 传输带宽限制，单位 KB/s；0 表示不限速，即以最大速度传输
BWLIMIT=0
# ==================================================================

# 当前目录下生成带时间戳的日志文件
LOG_DIR="$(pwd)/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/rsync_czdt_$(date +%Y%m%d_%H%M%S).log"

echo "开始同步，日志文件: ${LOG_FILE}"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "源目录: ${SOURCE_DIR}"
echo "带宽限制: ${BWLIMIT} KB/s（0 表示不限速/最大速度）"
echo "目的目录数量: ${#REMOTE_DESTINATIONS[@]}"
echo "----------------------------------------"

TOTAL_OK=0
TOTAL_FAIL=0

for DEST in "${REMOTE_DESTINATIONS[@]}"; do
    # 跳过空行和注释行
    [[ -z "${DEST}" || "${DEST}" == \#* ]] && continue

    # 解析 user@host:port:dir
    REMOTE_USER="${DEST%%@*}"
    REST="${DEST#*@}"
    REMOTE_HOST="${REST%%:*}"
    REMOTE_PORT="${REST#*:}"
    REMOTE_PORT="${REMOTE_PORT%%:*}"
    REMOTE_DIR="${REST#*:*:}"

    echo ""
    echo ">>> 同步到 ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PORT}:${REMOTE_DIR}"

    # BWLIMIT 大于 0 时加 --bwlimit 限速；等于 0 时不加参数，即最大速度
    BWARG=""
    if [ "${BWLIMIT}" -gt 0 ] 2>/dev/null; then
        BWARG="--bwlimit=${BWLIMIT}"
    fi

    # 执行 rsync，输出同时写入终端和日志文件
    rsync -avzP ${BWARG} -e "ssh -p ${REMOTE_PORT} -o Compression=no" \
        "${SOURCE_DIR}" \
        "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}" 2>&1 | tee -a "${LOG_FILE}"

    # 获取 rsync 的退出码（在管道中需要使用 PIPESTATUS）
    EXIT_CODE=${PIPESTATUS[0]}

    if [ ${EXIT_CODE} -eq 0 ]; then
        echo "同步成功: ${REMOTE_HOST}:${REMOTE_DIR}" | tee -a "${LOG_FILE}"
        TOTAL_OK=$((TOTAL_OK+1))
    else
        echo "同步失败，错误码: ${EXIT_CODE} → ${REMOTE_HOST}:${REMOTE_DIR}" | tee -a "${LOG_FILE}"
        TOTAL_FAIL=$((TOTAL_FAIL+1))
    fi
done

echo "----------------------------------------"
echo "完成: $(date '+%Y-%m-%d %H:%M:%S')"
echo "结果: 成功 ${TOTAL_OK} 个，失败 ${TOTAL_FAIL} 个"
