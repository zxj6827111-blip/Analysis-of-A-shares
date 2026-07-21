#!/usr/bin/env bash
# 在阿里云 / 任意 Linux 服务器上先跑本脚本，再决定如何安装 AStock 服务。
# 用法:
#   bash deploy/probe_server.sh
#   bash deploy/probe_server.sh --port 8765 --app-root /opt/wtpy/wtpy-master
# 输出 JSON 摘要到 deploy/.probe_result.json（若可写）

set -euo pipefail

PORT=8765
APP_ROOT=""
REPORT_JSON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-8765}"; shift 2 ;;
    --app-root) APP_ROOT="${2:-}"; shift 2 ;;
    --json-out) REPORT_JSON="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--port 8765] [--app-root /path/to/wtpy-master] [--json-out path]"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# 若未指定，尝试脚本相对路径定位仓库根
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$APP_ROOT" ]]; then
  CAND="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [[ -f "$CAND/wtpy/apps/astock/__main__.py" ]]; then
    APP_ROOT="$CAND"
  fi
fi
if [[ -z "$REPORT_JSON" ]]; then
  if [[ -n "$APP_ROOT" && -w "$APP_ROOT/deploy" ]]; then
    REPORT_JSON="$APP_ROOT/deploy/.probe_result.json"
  else
    REPORT_JSON="/tmp/astock_probe_result.json"
  fi
fi

hr() { printf '\n======== %s ========\n' "$1"; }
ok() { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; }
info() { printf '  [INFO] %s\n' "$1"; }

HAVE_PM2=0
HAVE_DOCKER=0
HAVE_SYSTEMD=0
HAVE_NGINX=0
HAVE_NODE=0
PORT_FREE=1
PYTHON_OK=0
VENV_OK=0
STORAGE_OK=0
DISK_OK=0
MEM_OK=0
RECOMMEND="systemd"
NOTES=()

hr "1. 主机与资源"
if command -v hostnamectl >/dev/null 2>&1; then
  hostnamectl 2>/dev/null | sed 's/^/  /' || true
else
  info "hostname: $(hostname 2>/dev/null || echo unknown)"
  info "uname: $(uname -a)"
fi
info "arch: $(uname -m)"
info "user: $(whoami)  uid=$(id -u)"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "os: ${PRETTY_NAME:-$ID $VERSION_ID}"
fi

if command -v free >/dev/null 2>&1; then
  free -h | sed 's/^/  /'
  MEM_MB=$(free -m | awk '/Mem:/{print $2}')
  if [[ "${MEM_MB:-0}" -lt 3500 ]]; then
    warn "内存约 ${MEM_MB}MB，全市场回测可能吃紧，建议 ≥8GB"
    MEM_OK=0
    NOTES+=("memory_low")
  else
    ok "内存约 ${MEM_MB}MB"
    MEM_OK=1
  fi
fi

if command -v df >/dev/null 2>&1; then
  df -h / | sed 's/^/  /'
  AVAIL_GB=$(df -BG / | awk 'NR==2{gsub(/G/,"",$4); print $4}')
  if [[ "${AVAIL_GB:-0}" -lt 20 ]]; then
    warn "根分区可用约 ${AVAIL_GB}G，storage 约需 1G+ 且回测会继续写盘"
    DISK_OK=0
    NOTES+=("disk_low")
  else
    ok "根分区可用约 ${AVAIL_GB}G"
    DISK_OK=1
  fi
fi

hr "2. 进程守护 / 容器栈（决定安装方式）"
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  HAVE_SYSTEMD=1
  ok "systemd 可用"
else
  warn "systemd 不可用或非 systemd 环境"
fi

if command -v pm2 >/dev/null 2>&1; then
  HAVE_PM2=1
  ok "PM2 已安装: $(pm2 -v 2>/dev/null || echo unknown)"
  pm2 list 2>/dev/null | sed 's/^/  /' || true
  if pm2 list 2>/dev/null | grep -qE 'online|stopped'; then
    info "PM2 上已有其他应用，适合复用同一套 PM2 管理本服务"
    NOTES+=("pm2_has_apps")
  fi
else
  info "未检测到 PM2（若你确定装过，检查 PATH / nvm）"
fi

if command -v node >/dev/null 2>&1; then
  HAVE_NODE=1
  ok "Node: $(node -v)  npm: $(npm -v 2>/dev/null || echo n/a)"
else
  info "未检测到 Node.js"
fi

if command -v docker >/dev/null 2>&1; then
  HAVE_DOCKER=1
  ok "Docker: $(docker --version 2>/dev/null || true)"
  docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null | sed 's/^/  /' || true
else
  info "未检测到 Docker"
fi

if command -v nginx >/dev/null 2>&1; then
  HAVE_NGINX=1
  ok "Nginx: $(nginx -v 2>&1 || true)"
else
  info "未检测到 Nginx（公网反代可选）"
fi

hr "3. 端口占用（默认 $PORT）"
check_port() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lntu 2>/dev/null | awk '{print $5}' | grep -E "[:.]${p}\$" && return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -lntu 2>/dev/null | grep -E "[:.]${p}[[:space:]]" && return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$p" -sTCP:LISTEN 2>/dev/null && return 0
  fi
  return 1
}

if check_port "$PORT" >/tmp/astock_port_check.txt 2>/dev/null; then
  PORT_FREE=0
  bad "端口 $PORT 已被占用："
  cat /tmp/astock_port_check.txt | sed 's/^/  /'
  NOTES+=("port_${PORT}_busy")
  info "可改用 --port 8766 或释放占用进程"
else
  ok "端口 $PORT 当前空闲（本机监听视角）"
  PORT_FREE=1
fi

# 常见 Web 端口一览
info "常见端口监听摘要:"
if command -v ss >/dev/null 2>&1; then
  ss -lnt 2>/dev/null | awk 'NR==1 || /:(80|443|3000|8000|8080|8765|9000|5173)\s/' | sed 's/^/  /' || true
fi

hr "4. Python 环境"
pick_python() {
  local c
  for c in python3.10 python3.9 python3.11 python3 python3.12 python3.8; do
    if command -v "$c" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY_BIN="$(pick_python || true)"
if [[ -z "${PY_BIN:-}" ]]; then
  bad "未找到 python3"
  NOTES+=("no_python")
else
  PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])')"
  info "选用: $PY_BIN  ($PY_VER)"
  MAJOR_MINOR="$("$PY_BIN" -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
  case "$MAJOR_MINOR" in
    3.8|3.9|3.10|3.11)
      ok "Python $MAJOR_MINOR 适合本项目（pandas==1.3.5 友好区间偏 3.8–3.10）"
      PYTHON_OK=1
      ;;
    3.12|3.13|3.14)
      warn "Python $MAJOR_MINOR 较新，pandas==1.3.5 可能装不上，建议另装 3.9/3.10 venv"
      PYTHON_OK=0
      NOTES+=("python_too_new")
      ;;
    *)
      warn "Python $MAJOR_MINOR 未验证"
      PYTHON_OK=0
      NOTES+=("python_unverified")
      ;;
  esac
  if ! "$PY_BIN" -c 'import venv' 2>/dev/null; then
    warn "venv 模块不可用，请安装 python3-venv"
    NOTES+=("no_venv_module")
  fi
fi

hr "5. 应用目录与数据"
if [[ -n "$APP_ROOT" && -d "$APP_ROOT" ]]; then
  ok "APP_ROOT=$APP_ROOT"
  if [[ -f "$APP_ROOT/wtpy/apps/astock/__main__.py" ]]; then
    ok "检测到 wtpy.apps.astock 入口"
  else
    bad "未找到 wtpy/apps/astock/__main__.py，代码可能未上传完整"
    NOTES+=("code_incomplete")
  fi
  if [[ -f "$APP_ROOT/requirements.txt" ]]; then
    ok "requirements.txt 存在"
  else
    warn "缺少 requirements.txt"
  fi
  if [[ -x "$APP_ROOT/.venv/bin/python" ]]; then
    VENV_OK=1
    ok "已有虚拟环境: $APP_ROOT/.venv"
    "$APP_ROOT/.venv/bin/python" -c 'import sys; print("  venv python", sys.version)' 2>/dev/null || true
    for mod in fastapi uvicorn numpy pandas pydantic openpyxl; do
      if "$APP_ROOT/.venv/bin/python" -c "import $mod" 2>/dev/null; then
        ok "import $mod"
      else
        warn "venv 中缺少: $mod"
        NOTES+=("missing_$mod")
      fi
    done
  else
    info "尚未创建 .venv（安装脚本会创建）"
  fi

  SR="$APP_ROOT/storage/astock"
  if [[ -d "$SR" ]]; then
    ok "storage/astock 存在"
    for f in universe.json calendar.json; do
      if [[ -f "$SR/$f" ]]; then
        ok "  $f"
      else
        warn "  缺少 $f"
        NOTES+=("missing_$f")
      fi
    done
    NPZ_N=$(find "$SR/npz" -type f 2>/dev/null | wc -l | tr -d ' ')
    CSV_N=$(find "$SR/csv" -type f 2>/dev/null | wc -l | tr -d ' ')
    info "npz 文件数≈$NPZ_N  csv 文件数≈$CSV_N"
    if [[ "${NPZ_N:-0}" -gt 100 || "${CSV_N:-0}" -gt 100 ]]; then
      STORAGE_OK=1
      ok "行情缓存看起来已有数据"
    else
      STORAGE_OK=0
      warn "行情数据很少，回测前需同步 storage/astock 或导入通达信日线"
      NOTES+=("storage_sparse")
    fi
    if command -v du >/dev/null 2>&1; then
      info "storage/astock 体积: $(du -sh "$SR" 2>/dev/null | awk '{print $1}')"
    fi
  else
    STORAGE_OK=0
    warn "无 storage/astock，需从本机同步数据"
    NOTES+=("no_storage")
  fi

  if [[ -d "$APP_ROOT/指标" ]]; then
    ok "指标/ 目录存在"
  else
    info "无 指标/ 目录（导入 TN6 时需要）"
  fi
else
  warn "未定位到 APP_ROOT。请用 --app-root 指定代码目录，或把仓库放到可探测位置"
  NOTES+=("no_app_root")
fi

hr "6. 云安全组提示（需在控制台人工核对）"
info "本脚本无法读取阿里云安全组。请确认："
info "  - SSH 22 仅限可信 IP"
info "  - 若走 Nginx：开放 80/443，不要对公网开放 $PORT"
info "  - 若临时直连调试：可临时开 $PORT，验证后关闭"

hr "7. 推荐安装方式（自动判断）"
# 决策树：已有 PM2 且多应用 -> PM2；否则 systemd；都没有 -> 引导装 systemd 或 PM2
if [[ $HAVE_PM2 -eq 1 ]]; then
  RECOMMEND="pm2"
  ok "推荐: PM2（服务器已有 PM2，与现有应用统一管理）"
elif [[ $HAVE_SYSTEMD -eq 1 ]]; then
  RECOMMEND="systemd"
  ok "推荐: systemd（系统原生守护，无需 Node）"
elif [[ $HAVE_DOCKER -eq 1 ]]; then
  RECOMMEND="docker"
  warn "推荐: 可考虑 Docker（仓库默认无 Dockerfile，需自建；或改装 PM2/systemd）"
  NOTES+=("docker_only_stack")
else
  RECOMMEND="systemd_or_pm2_install"
  warn "推荐: 安装 systemd 用户服务不可用时，可装 Node+PM2，或请管理员启用 systemd"
fi

if [[ $PORT_FREE -eq 0 ]]; then
  warn "安装时请换端口，例如 ASTOCK_PORT=8766"
fi
if [[ $PYTHON_OK -eq 0 ]]; then
  warn "请先解决 Python 版本（建议 3.9 或 3.10）再 pip install"
fi
if [[ $STORAGE_OK -eq 0 ]]; then
  warn "请先同步 storage/astock（本机约 1GB 级：csv+npz+复权等）"
fi

hr "8. 下一步命令"
case "$RECOMMEND" in
  pm2)
    cat <<EOF
  cd ${APP_ROOT:-/path/to/wtpy-master}
  bash deploy/install_astock.sh --mode pm2 --port $PORT
EOF
    ;;
  systemd)
    cat <<EOF
  cd ${APP_ROOT:-/path/to/wtpy-master}
  bash deploy/install_astock.sh --mode systemd --port $PORT
EOF
    ;;
  *)
    cat <<EOF
  cd ${APP_ROOT:-/path/to/wtpy-master}
  bash deploy/install_astock.sh --mode auto --port $PORT
EOF
    ;;
esac

# 写 JSON 摘要
NOTE_JSON=$(printf '%s\n' "${NOTES[@]+"${NOTES[@]}"}" | "$PY_BIN" -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))' 2>/dev/null || echo '[]')
cat >"$REPORT_JSON" <<EOF
{
  "recommend": "$RECOMMEND",
  "port": $PORT,
  "port_free": $PORT_FREE,
  "have_pm2": $HAVE_PM2,
  "have_docker": $HAVE_DOCKER,
  "have_systemd": $HAVE_SYSTEMD,
  "have_nginx": $HAVE_NGINX,
  "have_node": $HAVE_NODE,
  "python_ok": $PYTHON_OK,
  "python_bin": "${PY_BIN:-}",
  "venv_ok": $VENV_OK,
  "storage_ok": $STORAGE_OK,
  "disk_ok": $DISK_OK,
  "mem_ok": $MEM_OK,
  "app_root": "${APP_ROOT:-}",
  "notes": $NOTE_JSON
}
EOF
info "探测结果已写入: $REPORT_JSON"
echo
ok "探测完成。把本输出贴回来，可据此定最终安装方案（无需改业务代码）。"
