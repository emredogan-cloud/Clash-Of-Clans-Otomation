#!/usr/bin/env bash
#
# Idempotent bootstrap for the Android UI Automation Framework — Phase 1.
#
# Validates the operator environment, checks the connected device's USB
# link speed (frozen Phase-0.5 requirement), creates the venv, installs
# pinned dependencies, and creates the runtime directory tree.
#
# Exit codes:
#   0  success
#   2  python or adb prerequisite missing / too old
#   3  no connected device, or device not in state `device`
#   4  USB link speed below 480 Mbps
#   5  USB link speed unverifiable AND --strict-usb passed
#   6  venv creation or dependency installation failed
#
# Usage:
#   bash scripts/bootstrap.sh [--strict-usb] [--skip-deps] [--no-color]
#
set -Eeuo pipefail

# -------------------- script-level constants --------------------------

# Resolve the repo root regardless of where the script is invoked from.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
cd "${REPO_ROOT}"

VENV_DIR="${REPO_ROOT}/.venv"
LOCK_FILE="${REPO_ROOT}/requirements-lock.txt"

MIN_PY_MAJOR=3
MIN_PY_MINOR=11
MIN_ADB_MAJOR=34
MIN_USB_SPEED_MBPS=480

# -------------------- flags --------------------------------------------

STRICT_USB=0
SKIP_DEPS=0
NO_COLOR=0
for arg in "$@"; do
    case "${arg}" in
        --strict-usb) STRICT_USB=1 ;;
        --skip-deps)  SKIP_DEPS=1 ;;
        --no-color)   NO_COLOR=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: ${arg}" >&2
            echo "usage: bash scripts/bootstrap.sh [--strict-usb] [--skip-deps] [--no-color]" >&2
            exit 64
            ;;
    esac
done

# -------------------- output helpers ----------------------------------

if [[ "${NO_COLOR}" -eq 0 && -t 1 ]]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

# Summary checklist printed at the end. Format: "key:status:detail".
SUMMARY_LINES=()
summary_add()  { SUMMARY_LINES+=("$1:$2:${3:-}"); }
log_info()    { printf '%s[INFO]%s  %s\n' "${C_BLUE}" "${C_RESET}" "$*" >&2; }
log_ok()      { printf '%s[OK]%s    %s\n' "${C_GREEN}" "${C_RESET}" "$*" >&2; }
log_warn()    { printf '%s[WARN]%s  %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
log_error()   { printf '%s[ERROR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }
die() { log_error "$1"; exit "${2:-1}"; }

trap 'log_error "bootstrap aborted at line ${LINENO}"' ERR

# -------------------- checks ------------------------------------------

check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        summary_add python FAIL "python3 not on PATH"
        die "python3 not found on PATH. Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ and re-run." 2
    fi
    local raw major minor
    raw=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
    major=$(echo "${raw}" | cut -d. -f1)
    minor=$(echo "${raw}" | cut -d. -f2)
    if (( major < MIN_PY_MAJOR )) || (( major == MIN_PY_MAJOR && minor < MIN_PY_MINOR )); then
        summary_add python FAIL "found ${raw}, need ≥${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
        die "Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ required, found ${raw}" 2
    fi
    log_ok "python ${raw}"
    summary_add python OK "${raw}"
}

check_adb() {
    if ! command -v adb >/dev/null 2>&1; then
        summary_add adb FAIL "adb not on PATH"
        die "adb not found on PATH. Install platform-tools ≥ ${MIN_ADB_MAJOR}.0 and re-run.
On Debian/Ubuntu:  sudo apt install android-tools-adb
On Fedora:         sudo dnf install android-tools" 2
    fi
    local version_line major
    version_line=$(adb version 2>/dev/null | grep -m1 '^Version' || true)
    if [[ -z "${version_line}" ]]; then
        summary_add adb FAIL "could not parse version"
        die "could not determine adb version: \`adb version\` returned no 'Version' line" 2
    fi
    major=$(echo "${version_line}" | awk '{print $2}' | cut -d. -f1)
    if (( major < MIN_ADB_MAJOR )); then
        local found
        found=$(echo "${version_line}" | awk '{print $2}')
        summary_add adb FAIL "found ${found}, need ≥${MIN_ADB_MAJOR}.0"
        die "adb (platform-tools) ${MIN_ADB_MAJOR}.0+ required, found ${found}" 2
    fi
    local found
    found=$(echo "${version_line}" | awk '{print $2}')
    log_ok "adb ${found}"
    summary_add adb OK "${found}"
}

check_device() {
    # Ensure the adb server is up before probing so the "daemon not running;
    # starting now" stderr messages do not race the state check.
    adb start-server >/dev/null 2>&1 || true
    local state stderr_file
    stderr_file=$(mktemp)
    if ! state=$(adb get-state 2>"${stderr_file}"); then
        local err
        err=$(cat "${stderr_file}")
        rm -f "${stderr_file}"
        summary_add device FAIL "no connected device"
        die "no Android device detected. Connect the phone over USB with USB debugging enabled.
adb error: ${err}
\`adb devices\` output:
$(adb devices 2>&1 | sed 's/^/  /')" 3
    fi
    rm -f "${stderr_file}"
    # Trim whitespace; `adb get-state` writes "device\n".
    state=$(echo "${state}" | tr -d '[:space:]')
    if [[ "${state}" != "device" ]]; then
        summary_add device FAIL "state=${state}"
        die "device is in state '${state}'; expected 'device'.
For 'unauthorized', accept the USB-debugging prompt on the phone.
For 'no permissions', install udev rules and re-plug." 3
    fi
    local serial
    serial=$(adb get-serialno 2>/dev/null || echo unknown)
    log_ok "device ${serial} (state=device)"
    summary_add device OK "${serial}"
}

# Resolve the sysfs USB device path for the connected device serial.
# Echoes the path on stdout (without trailing slash) or empty if unresolved.
resolve_usb_path() {
    local serial="$1"
    local base="/sys/bus/usb/devices"
    if [[ ! -d "${base}" ]]; then
        return 0
    fi
    local dev
    for dev in "${base}"/*; do
        [[ -f "${dev}/serial" ]] || continue
        local s
        s=$(tr -d '[:space:]' < "${dev}/serial" 2>/dev/null || true)
        if [[ "${s}" == "${serial}" ]]; then
            echo "${dev}"
            return 0
        fi
    done
}

check_usb_link_speed() {
    local serial dev_path speed
    serial=$(adb get-serialno 2>/dev/null || true)
    if [[ -z "${serial}" ]]; then
        summary_add usb WARN "could not read adb serial"
        log_warn "cannot read adb serialno; skipping USB link-speed validation"
        return 0
    fi
    dev_path=$(resolve_usb_path "${serial}" || true)
    if [[ -z "${dev_path}" ]]; then
        if (( STRICT_USB == 1 )); then
            summary_add usb FAIL "sysfs path not resolvable for ${serial}"
            die "cannot resolve /sys/bus/usb/devices path for serial ${serial} and --strict-usb was passed" 5
        fi
        summary_add usb WARN "sysfs path not resolvable for ${serial}"
        log_warn "cannot resolve USB sysfs path for serial ${serial}; skipping speed check"
        return 0
    fi
    if [[ ! -f "${dev_path}/speed" ]]; then
        if (( STRICT_USB == 1 )); then
            summary_add usb FAIL "no speed file at ${dev_path}"
            die "${dev_path}/speed missing and --strict-usb was passed" 5
        fi
        summary_add usb WARN "no speed file at ${dev_path}"
        log_warn "${dev_path}/speed missing; skipping speed check"
        return 0
    fi
    speed=$(tr -d '[:space:]' < "${dev_path}/speed")
    if [[ -z "${speed}" ]]; then
        summary_add usb WARN "speed file empty"
        log_warn "${dev_path}/speed is empty; skipping speed check"
        return 0
    fi
    if (( speed >= MIN_USB_SPEED_MBPS )); then
        log_ok "usb ${speed} Mbps (path: ${dev_path})"
        summary_add usb OK "${speed} Mbps"
        return 0
    fi
    summary_add usb FAIL "${speed} Mbps (< ${MIN_USB_SPEED_MBPS})"
    die "USB link speed is ${speed} Mbps; minimum required is ${MIN_USB_SPEED_MBPS} Mbps.
Almost always caused by an intermediate USB hub (keyboard, monitor, dock) downgrading
the link to USB 1.1 Full Speed. Replug the cable directly into a USB 2.0 high-speed
(or USB 3.x) port on the host, then re-run this script." 4
}

ensure_venv() {
    if [[ -d "${VENV_DIR}" ]]; then
        log_ok "venv ${VENV_DIR} (present)"
        summary_add venv OK "${VENV_DIR}"
        return 0
    fi
    log_info "creating venv at ${VENV_DIR}"
    if ! python3 -m venv "${VENV_DIR}"; then
        summary_add venv FAIL "python3 -m venv failed"
        die "could not create venv at ${VENV_DIR}" 6
    fi
    log_ok "venv ${VENV_DIR} (created)"
    summary_add venv OK "${VENV_DIR}"
}

install_deps() {
    if (( SKIP_DEPS == 1 )); then
        log_info "--skip-deps passed; skipping pip install"
        summary_add deps SKIP "--skip-deps"
        return 0
    fi
    if [[ ! -f "${LOCK_FILE}" ]]; then
        summary_add deps FAIL "lockfile missing: ${LOCK_FILE}"
        die "lockfile not found at ${LOCK_FILE}" 6
    fi
    log_info "installing locked dependencies from ${LOCK_FILE}"
    # --quiet keeps the log readable; --require-virtualenv is redundant (we're already in one)
    # but cheap insurance against accidental system-pip installs.
    if ! "${VENV_DIR}/bin/pip" install --quiet --upgrade pip; then
        summary_add deps FAIL "pip upgrade failed"
        die "failed to upgrade pip inside venv" 6
    fi
    if ! "${VENV_DIR}/bin/pip" install --quiet --no-deps -r "${LOCK_FILE}"; then
        summary_add deps FAIL "pip install failed"
        die "pip install -r ${LOCK_FILE} failed" 6
    fi
    log_ok "dependencies installed (numpy, opencv-python-headless)"
    summary_add deps OK "$(wc -l < "${LOCK_FILE}") locked"
}

ensure_runtime_dirs() {
    local d
    for d in var/logs var/metrics var/artifacts var/tmp; do
        mkdir -p "${REPO_ROOT}/${d}"
    done
    log_ok "runtime dirs (var/logs, var/metrics, var/artifacts, var/tmp)"
    summary_add runtime_dirs OK "var/{logs,metrics,artifacts,tmp}"
}

# -------------------- summary -----------------------------------------

print_summary() {
    echo ""
    printf '%sBootstrap summary%s\n' "${C_BOLD}" "${C_RESET}"
    echo "================="
    local line key status detail mark
    for line in "${SUMMARY_LINES[@]}"; do
        key="${line%%:*}"
        line="${line#*:}"
        status="${line%%:*}"
        detail="${line#*:}"
        case "${status}" in
            OK)   mark="${C_GREEN}✓${C_RESET}" ;;
            WARN) mark="${C_YELLOW}!${C_RESET}" ;;
            FAIL) mark="${C_RED}✗${C_RESET}" ;;
            SKIP) mark="${C_BLUE}~${C_RESET}" ;;
            *)    mark="?" ;;
        esac
        if [[ -n "${detail}" ]]; then
            printf '  %s %-15s %s\n' "${mark}" "${key}" "${detail}"
        else
            printf '  %s %-15s\n' "${mark}" "${key}"
        fi
    done
    echo ""
    echo "Next: source .venv/bin/activate && python -m automation.bootstrap"
}

# -------------------- main --------------------------------------------

main() {
    log_info "Phase 1 bootstrap starting (repo: ${REPO_ROOT})"
    check_python
    check_adb
    check_device
    check_usb_link_speed
    ensure_venv
    install_deps
    ensure_runtime_dirs
    print_summary
}

main "$@"
