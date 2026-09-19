#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# 主入口脚本：直接执行 QEMU + EDK2 编译安装脚本。
# 可通过环境变量指定脚本位置：
#   QEMU_PATCH_SCRIPT="/path/to/qemu9.2patch.sh" bash nothing.sh
#   EDK2_PATCH_SCRIPT="/path/to/edk2patch202505.sh" bash nothing.sh
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() { echo -e "${BLUE}$1${NC}"; }
print_ok() { echo -e "${GREEN}$1${NC}"; }
print_warn() { echo -e "${YELLOW}$1${NC}"; }
print_err() { echo -e "${RED}$1${NC}"; }

get_distro_id() {
    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
        echo "${ID,,}"
    else
        echo "unknown"
    fi
}

is_arch_like() {
    case "$(get_distro_id)" in
        arch|endeavouros|manjaro) return 0 ;;
        *) return 1 ;;
    esac
}

is_debian_like() {
    case "$(get_distro_id)" in
        ubuntu|debian|linuxmint|pop) return 0 ;;
        *) return 1 ;;
    esac
}

install_build_dependencies() {
    local missing_packages=()

    if [[ "${KVMSPOOF_SKIP_DEP_INSTALL:-0}" == "1" ]]; then
        print_warn "已跳过依赖安装。"
        return 0
    fi

    print_info "正在安装 QEMU/EDK2 编译依赖..."
    if is_arch_like; then
        sudo pacman -S --needed --noconfirm \
            acpica base-devel dtc glib2 pixman ninja python python-virtualenv \
            zlib gnupg python-sphinx python-sphinx_rtd_theme patch curl spice \
            libusb usbredir git git-lfs nasm edk2-ovmf pciutils pkgconf dmidecode virt-firmware seabios
    elif is_debian_like; then
        local packages=(
            acpica-tools build-essential libfdt-dev libglib2.0-dev libpixman-1-dev
            ninja-build python3-venv zlib1g-dev gnupg python3-sphinx
            python3-sphinx-rtd-theme patch curl libspice-server-dev
            libusb-1.0-0-dev libusbredirhost-dev libusbredirparser-dev uuid-dev
            git git-lfs nasm python-is-python3 python3-virt-firmware qemu-utils qemu-system-data seabios pciutils pkg-config dmidecode xmlstarlet
        )

        local package=""
        for package in "${packages[@]}"; do
            if ! dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx "installed"; then
                missing_packages+=("$package")
            fi
        done

        if [[ "${#missing_packages[@]}" -eq 0 ]]; then
            print_info "QEMU/EDK2 编译依赖已安装，跳过 apt 更新。"
            export KVMSPOOF_SKIP_DEP_INSTALL=1
            return 0
        fi

        export DEBIAN_FRONTEND=noninteractive
        print_warn "缺少编译依赖: ${missing_packages[*]}"
        sudo apt-get install -y "${missing_packages[@]}" || {
            print_err "QEMU/EDK2 编译依赖安装失败。"
            print_err "未自动执行 apt update；如软件源缓存过旧，请手动更新软件源后重试。"
            return 1
        }
    else
        print_err "不支持的发行版: $(get_distro_id)，请手动安装 QEMU/EDK2 编译依赖。"
        return 1
    fi

    export KVMSPOOF_SKIP_DEP_INSTALL=1
}

read_menu_choice() {
    local prompt="$1"
    MENU_CHOICE=""
    printf "%s" "$prompt"
    IFS= read -r -s -n 1 MENU_CHOICE
    while IFS= read -r -s -n 1 -t 0.01 _discard; do :; done
    printf "%s\n" "$MENU_CHOICE"
}

# ============================================================
# 新增的辅助函数：统一完成提示的边框和等待按键
# ============================================================
print_success_header() {
    echo -e "${GREEN}========================================${NC}"
}

print_success_footer() {
    echo -e "${GREEN}========================================${NC}"
    read -p "按回车键返回主菜单..."
}

prompt_positive_int() {
    local prompt="$1"
    local value=""

    while true; do
        echo -n "$prompt" >&2
        read -r value
        if [[ "$value" =~ ^[0-9]+$ && "$value" -gt 0 ]]; then
            echo "$value"
            return 0
        fi
        print_err "输入无效，请输入正整数。" >&2
    done
}

normalize_capacity_to_gb() {
    local capacity="${1//\"/}"
    capacity="${capacity// /}"

    case "$capacity" in
        "") return 1 ;;
        *[Tt][Bb])
            local tb="${capacity%[Tt][Bb]}"
            [[ "$tb" =~ ^[0-9]+$ ]] || return 1
            echo "$((tb * 1000))"
            ;;
        *[Tt])
            local tb="${capacity%[Tt]}"
            [[ "$tb" =~ ^[0-9]+$ ]] || return 1
            echo "$((tb * 1000))"
            ;;
        *[Gg][Bb])
            local gb="${capacity%[Gg][Bb]}"
            [[ "$gb" =~ ^[0-9]+$ ]] || return 1
            echo "$gb"
            ;;
        *[Gg])
            local gb="${capacity%[Gg]}"
            [[ "$gb" =~ ^[0-9]+$ ]] || return 1
            echo "$gb"
            ;;
        *)
            [[ "$capacity" =~ ^[0-9]+$ ]] || return 1
            echo "$capacity"
            ;;
    esac
}

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
SCRIPT_ROOT="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd || pwd)"
RUN_ROOT="$(pwd)"

# 当脚本由 spoof 释放到临时目录运行时，可通过以下变量指定释放目录：
RELEASE_ROOT="${KVMSPOOF_RELEASE_DIR:-${KVMSPOOF_RESOURCE_DIR:-$SCRIPT_ROOT}}"
if [[ "$SCRIPT_SOURCE" == /proc/self/fd/* && -z "${KVMSPOOF_RELEASE_DIR:-}" && -z "${KVMSPOOF_RESOURCE_DIR:-}" ]]; then
    RELEASE_ROOT="$RUN_ROOT"
fi

RUNTIME_ROOT="${KVMSPOOF_RUNTIME_DIR:-/tmp/spoof}"
mkdir -p "$RUNTIME_ROOT"
export KVMSPOOF_RUNTIME_DIR="$RUNTIME_ROOT"

# 状态文件统一为 vars.sh（位于 VARS_ROOT）
VARS_ROOT="${KVMSPOOF_VARS_DIR:-$RUNTIME_ROOT}"
mkdir -p "$VARS_ROOT"
export KVMSPOOF_VARS_DIR="$VARS_ROOT"
VARS_FILE="$VARS_ROOT/vars.sh"

# 旧的 state_file（用于迁移）
STATE_FILE_OLD="${KVMSPOOF_STATE_FILE:-$RUNTIME_ROOT/kvmspoof_state.env}"

WORK_ROOT_OVERRIDE="${KVMSPOOF_WORK_DIR:-}"
WORK_ROOT=""

# ============================================================
# 查找 QEMU+EDK2 编译安装脚本
# ============================================================
find_script() {
    local env_path="$1"
    local script_name="$2"
    local candidates=()

    if [[ -n "$env_path" ]]; then
        candidates+=("$env_path")
    fi

    candidates+=(
        "$RELEASE_ROOT/$script_name"
        "$RUN_ROOT/$script_name"
        "$SCRIPT_ROOT/$script_name"
        "$(dirname "$RUN_ROOT")/$script_name"
        "$HOME/Downloads/$script_name"
    )

    local candidate
    for candidate in "${candidates[@]}"; do
        if [[ -f "$candidate" ]]; then
            realpath "$candidate" 2>/dev/null || echo "$candidate"
            return 0
        fi
    done

    return 1
}

# ============================================================
# 直接选择 QEMU + EDK2 编译安装脚本
# ============================================================
select_build_profile() {
    local qemu_script=""
    local edk2_script=""
    local qemu_env_path="${QEMU_PATCH_SCRIPT:-}"
    local edk2_env_path="${EDK2_PATCH_SCRIPT:-}"
    local qemu_script_name="qemu9.2patch.sh"
    local edk2_script_name="edk2patch202505.sh"
    local expected_qemu_branch="stable-9.2"
    local expected_qemu_version="9.2"
    local build_profile="${KVMSPOOF_BUILD_PROFILE:-}"

    if [[ -z "$qemu_env_path" && -z "$edk2_env_path" && -z "$build_profile" ]]; then
        print_info "请选择 QEMU/EDK2 编译版本："
        echo "1 - QEMU 9.2 + EDK2"
        echo "2 - QEMU 11.0 + EDK2"
        echo "0 - 退出"
        read_menu_choice "请输入选择: "
        build_profile="$MENU_CHOICE"
    fi

    case "$build_profile" in
        ""|1|9.2|qemu9.2)
            qemu_script_name="qemu9.2patch.sh"
            edk2_script_name="edk2patch202505.sh"
            expected_qemu_branch="stable-9.2"
            expected_qemu_version="9.2"
            ;;
        2|11.0|qemu11.0)
            qemu_script_name="qemu11.0patch.sh"
            edk2_script_name="edk2patch202602.sh"
            expected_qemu_branch="stable-11.0"
            expected_qemu_version="11.0"
            ;;
        0|exit|quit)
            return 1
            ;;
        *)
            print_err "无效的 QEMU/EDK2 编译版本: $build_profile"
            return 1
            ;;
    esac

    if ! qemu_script="$(find_script "$qemu_env_path" "$qemu_script_name")"; then
        print_err "找不到 QEMU 编译脚本 $qemu_script_name。"
        print_err "请把 $qemu_script_name 放到当前目录/上级目录，或使用 QEMU_PATCH_SCRIPT=\"/path/to/$qemu_script_name\" 指定。"
        return 1
    fi
    if ! edk2_script="$(find_script "$edk2_env_path" "$edk2_script_name")"; then
        print_err "找不到 EDK2 编译脚本 $edk2_script_name。"
        print_err "请把 $edk2_script_name 放到当前目录/上级目录，或使用 EDK2_PATCH_SCRIPT=\"/path/to/$edk2_script_name\" 指定。"
        return 1
    fi

    if ! grep -Fq -- "QEMU_SOURCE_BRANCH=\"$expected_qemu_branch\"" "$qemu_script"; then
        print_err "所选 QEMU 脚本与构建配置不匹配：期望 $expected_qemu_branch"
        print_err "脚本: $qemu_script"
        return 1
    fi

    local default_qemu_version="$expected_qemu_version"
    local default_edk2_version="edk2-stable202505"

    case "$(basename "$edk2_script")" in
        edk2patch202602.sh) default_edk2_version="edk2-stable202602" ;;
    esac

    SELECTED_BUILD_TYPE="qemu"
    if [[ -n "${KVMSPOOF_QEMU_VERSION:-}" && "$KVMSPOOF_QEMU_VERSION" != "$default_qemu_version" ]]; then
        print_err "KVMSPOOF_QEMU_VERSION=$KVMSPOOF_QEMU_VERSION 与所选脚本版本 $default_qemu_version 不一致"
        return 1
    fi
    SELECTED_QEMU_VERSION="$default_qemu_version"
    SELECTED_EDK2_VERSION="${KVMSPOOF_EDK2_VERSION:-$default_edk2_version}"
    SELECTED_QEMU_SCRIPT="$qemu_script"
    SELECTED_EDK2_SCRIPT="$edk2_script"
    SELECTED_BUILD_LABEL="${KVMSPOOF_BUILD_LABEL:-QEMU $SELECTED_QEMU_VERSION + EDK2}"
    return 0
}

prepare_selected_work_root() {

    if [[ -n "$WORK_ROOT_OVERRIDE" ]]; then
        WORK_ROOT="${WORK_ROOT_OVERRIDE%/}"
    else
        local work_dir_name="${KVMSPOOF_BUILD_WORK_NAME:-qemu${SELECTED_QEMU_VERSION}-edk2}"
        work_dir_name="${work_dir_name//\//-}"
        WORK_ROOT="${RELEASE_ROOT%/}/$work_dir_name"
    fi

    mkdir -p "$WORK_ROOT"
    WORK_ROOT="$(cd "$WORK_ROOT" && pwd)"
    export KVMSPOOF_WORK_DIR="$WORK_ROOT"
}

stage_work_resources() {
    local resource=""
    local work_vars="$WORK_ROOT/vars.sh"
    local resources=(
        "splash.bmp"
    )

    # 只在 vars.sh 存在时才复制
    if [[ -f "$VARS_FILE" && "$VARS_FILE" != "$work_vars" ]]; then
        cp -f "$VARS_FILE" "$work_vars"
    fi

    for resource in "${resources[@]}"; do
        if [[ -f "$RELEASE_ROOT/$resource" ]]; then
            cp -f "$RELEASE_ROOT/$resource" "$WORK_ROOT/$resource"
        elif [[ -f "$SCRIPT_ROOT/$resource" ]]; then
            cp -f "$SCRIPT_ROOT/$resource" "$WORK_ROOT/$resource"
        fi
    done
}

# ============================================================
# 执行补丁脚本
# ============================================================
run_patch_script() {
    local label="$1"
    local script_path="$2"

    print_info "========================================"
    print_info "开始执行 $label: $script_path"
    print_info "========================================"

    chmod +x "$script_path" 2>/dev/null || true

    local script_dir
    script_dir="$(cd "$(dirname "$script_path")" && pwd)"

    if [[ -d "$RELEASE_ROOT" ]]; then
        export KVMSPOOF_RELEASE_DIR="$RELEASE_ROOT"
        export KVMSPOOF_RESOURCE_DIR="$RELEASE_ROOT"
    fi
    export KVMSPOOF_SCRIPT_DIR="$script_dir"
    export KVMSPOOF_WORK_DIR="$WORK_ROOT"
    export KVMSPOOF_VARS_DIR="$VARS_ROOT"
    export KVMSPOOF_STATE_FILE="$VARS_FILE"

    if (cd "$WORK_ROOT" && bash "$script_path"); then
        print_ok "$label 执行完成。"
        return 0
    else
        local code=$?
        print_err "$label 执行失败，退出码: $code"
        return "$code"
    fi
}

# ============================================================
# 检查 QEMU 是否安装成功
# ============================================================
check_qemu_installed() {
    local qemu_bin="${QEMU_BIN:-/usr/local/bin/qemu-system-x86_64}"
    local version_line=""

    if [[ ! -x "$qemu_bin" ]]; then
        print_err "未检测到 QEMU 可执行文件: $qemu_bin"
        print_err "QEMU 编译安装脚本可能没有安装完成。"
        return 1
    fi

    version_line="$($qemu_bin --version | head -n 1 || true)"
    QEMU_BIN_PATH="$qemu_bin"
    QEMU_VERSION_LINE="$version_line"

    if [[ -n "${SELECTED_QEMU_VERSION:-}" && "$version_line" != *"$SELECTED_QEMU_VERSION"* ]]; then
        print_err "QEMU 版本不匹配：当前输出为 '$version_line'，期望包含 $SELECTED_QEMU_VERSION。"
        print_err "不会更新固件 JSON，请先确认 qemu-system-x86_64 已被正确替换。"
        return 1
    fi

    return 0
}

# ============================================================
# 在 /usr/local/bin 查找 OVMF 固件文件
# ============================================================
find_ovmf_files() {
    # 优先使用环境变量
    local code_file="${OVMF_CODE:-}"
    local vars_file="${OVMF_VARS:-}"
    local search_dir="/usr/local/bin"
    local code_candidates=()
    local vars_candidates=()

    if [[ "${SELECTED_QEMU_VERSION:-}" == "11.0" || "${SELECTED_EDK2_VERSION:-}" == "edk2-stable202602" ]]; then
        code_candidates=(
            "OVMF_CODE_4M.patched.qcow2"
            "OVMF_CODE_4M.qcow2"
        )
        vars_candidates=(
            "OVMF_VARS_4M.patched.qcow2"
            "OVMF_VARS_4M.qcow2"
        )
    else
        code_candidates=(
            "OVMF_CODE.qcow2"
            "OVMF_CODE.patched.qcow2"
        )
        vars_candidates=(
            "OVMF_VARS.qcow2"
            "OVMF_VARS.patched.qcow2"
        )
    fi

    # 如果环境变量未设置或文件不存在，则搜索 /usr/local/bin
    if [[ -z "$code_file" || ! -f "$code_file" ]]; then
        for candidate in "${code_candidates[@]}"; do
            local full="$search_dir/$candidate"
            if [[ -f "$full" ]]; then
                code_file="$full"
                break
            fi
        done
    fi

    if [[ -z "$vars_file" || ! -f "$vars_file" ]]; then
        for candidate in "${vars_candidates[@]}"; do
            local full="$search_dir/$candidate"
            if [[ -f "$full" ]]; then
                vars_file="$full"
                break
            fi
        done
    fi

    # 检查是否找到
    if [[ -z "$code_file" || ! -f "$code_file" ]]; then
        print_err "在 /usr/local/bin 未找到 OVMF CODE 文件。"
        print_err "请通过环境变量 OVMF_CODE 指定路径，或确保已正确安装 EDK2 固件。"
        return 1
    fi
    if [[ -z "$vars_file" || ! -f "$vars_file" ]]; then
        print_err "在 /usr/local/bin 未找到 OVMF VARS 文件。"
        print_err "请通过环境变量 OVMF_VARS 指定路径，或确保已正确安装 EDK2 固件。"
        return 1
    fi

    # 将找到的路径保存到 vars.sh 以便下次使用
    if [[ -f "$VARS_FILE" ]]; then
        sed -i "/^OVMF_CODE_PATH=/d" "$VARS_FILE" 2>/dev/null
        sed -i "/^OVMF_VARS_PATH=/d" "$VARS_FILE" 2>/dev/null
        echo "OVMF_CODE_PATH=\"$code_file\"" >> "$VARS_FILE"
        echo "OVMF_VARS_PATH=\"$vars_file\"" >> "$VARS_FILE"
    fi

    echo "$code_file|$vars_file"
    return 0
}

# ============================================================
# 检查 EDK2 输出固件
# ============================================================
check_edk2_outputs() {
    if OVMF_PATHS=$(find_ovmf_files); then
        OVMF_CODE_PATH="${OVMF_PATHS%%|*}"
        OVMF_VARS_PATH="${OVMF_PATHS##*|}"
        return 0
    else
        print_warn "未自动检测到 OVMF 固件文件，请手动设置环境变量 OVMF_CODE 和 OVMF_VARS。"
        return 1
    fi
}

# ============================================================
# Update existing inactive Q35 libvirt domains to the selected
# QEMU/OVMF version. Keep a separate NVRAM file per QEMU version.
# ============================================================
run_virsh() {
    virsh "$@" 2>/dev/null || sudo virsh "$@"
}

xml_set_or_add_attr() {
    local xml_file="$1"
    local element_path="$2"
    local attr_name="$3"
    local attr_value="$4"
    local attr_count=""

    attr_count=$(xmlstarlet sel -t -v "count($element_path/@$attr_name)" "$xml_file" 2>/dev/null)
    if [[ "$attr_count" == "0" ]]; then
        xmlstarlet ed -L -i "$element_path" -t attr -n "$attr_name" -v "$attr_value" "$xml_file"
    else
        xmlstarlet ed -L -u "$element_path/@$attr_name" -v "$attr_value" "$xml_file"
    fi
}

xml_ensure_firmware_feature() {
    local xml_file="$1"
    local feature_name="$2"
    local feature_count=""

    feature_count=$(xmlstarlet sel -t -v \
        "count(/domain/os/firmware/feature[@name='$feature_name'])" "$xml_file" 2>/dev/null)
    if [[ "$feature_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/os/firmware" -t elem -n "feature" -v "" "$xml_file"
        xml_set_or_add_attr "$xml_file" "/domain/os/firmware/feature[last()]" "enabled" "yes"
        xml_set_or_add_attr "$xml_file" "/domain/os/firmware/feature[last()]" "name" "$feature_name"
    else
        xml_set_or_add_attr "$xml_file" "/domain/os/firmware/feature[@name='$feature_name']" "enabled" "yes"
    fi
}

ensure_qemu_display_model() {
    local xml_file="$1"
    local video_count=""
    local model_count=""
    local graphics_count=""
    local listen_count=""
    local target_model="${KVMSPOOF_VIDEO_MODEL:-vga}"

    if [[ "${KVMSPOOF_FORCE_VIDEO_MODEL:-1}" != "1" ]]; then
        return 0
    fi

    # QEMU 11 修改 VirtIO/PCI ID 后，virtio 显示在 OVMF 阶段容易无法初始化。
    # vga 是安装/OVMF 阶段最保守的显示设备；可用 KVMSPOOF_VIDEO_MODEL=qxl 覆盖。
    video_count=$(xmlstarlet sel -t -v "count(/domain/devices/video)" "$xml_file" 2>/dev/null)
    if [[ "$video_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/devices" -t elem -n "video" -v "" "$xml_file"
    fi

    model_count=$(xmlstarlet sel -t -v "count(/domain/devices/video[1]/model)" "$xml_file" 2>/dev/null)
    if [[ "$model_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/devices/video[1]" -t elem -n "model" -v "" "$xml_file"
    fi

    xmlstarlet ed -L -d "/domain/devices/video/model/@primary" "$xml_file" 2>/dev/null || true
    xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "type" "$target_model"
    xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "heads" "1"
    xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "primary" "yes"

    if [[ "$target_model" == "qxl" ]]; then
        xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "ram" "65536"
        xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "vram" "65536"
        xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "vgamem" "16384"
    else
        xmlstarlet ed -L \
            -d "/domain/devices/video[1]/model/@ram" \
            -d "/domain/devices/video[1]/model/@vgamem" \
            "$xml_file" 2>/dev/null || true
        xml_set_or_add_attr "$xml_file" "/domain/devices/video[1]/model" "vram" "16384"
    fi

    graphics_count=$(xmlstarlet sel -t -v "count(/domain/devices/graphics)" "$xml_file" 2>/dev/null)
    if [[ "$graphics_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/devices" -t elem -n "graphics" -v "" "$xml_file"
    fi
    xml_set_or_add_attr "$xml_file" "/domain/devices/graphics[1]" "type" "spice"
    xml_set_or_add_attr "$xml_file" "/domain/devices/graphics[1]" "autoport" "yes"
    xmlstarlet ed -L -d "/domain/devices/graphics[1]/gl" "$xml_file" 2>/dev/null || true

    listen_count=$(xmlstarlet sel -t -v "count(/domain/devices/graphics[1]/listen)" "$xml_file" 2>/dev/null)
    if [[ "$listen_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/devices/graphics[1]" -t elem -n "listen" -v "" "$xml_file"
    fi
    xml_set_or_add_attr "$xml_file" "/domain/devices/graphics[1]/listen[1]" "type" "address"
}

get_qcow2_virtual_size() {
    local image_path="$1"
    local qemu_img=""

    qemu_img="$(command -v qemu-img 2>/dev/null || true)"
    [[ -n "$qemu_img" && -f "$image_path" ]] || return 1

    "$qemu_img" info --output=json "$image_path" 2>/dev/null |
        python3 -c 'import json, sys; print(json.load(sys.stdin).get("virtual-size", ""))' 2>/dev/null
}

prepare_versioned_nvram() {
    local vm_name="$1"
    local current_nvram="$2"
    local version_tag="${SELECTED_QEMU_VERSION//./_}"
    local nvram_dir="/var/lib/libvirt/qemu/nvram"
    local target_nvram=""
    local source_nvram="$OVMF_VARS_PATH"
    local current_size=""
    local template_size=""

    if [[ -n "$current_nvram" ]]; then
        nvram_dir="$(dirname "$current_nvram")"
    fi
    target_nvram="$nvram_dir/${vm_name}_qemu${version_tag}_VARS.qcow2"

    if [[ -f "$target_nvram" ]]; then
        echo "$target_nvram"
        return 0
    fi

    if [[ -f "$current_nvram" ]]; then
        current_size="$(get_qcow2_virtual_size "$current_nvram" || true)"
        template_size="$(get_qcow2_virtual_size "$OVMF_VARS_PATH" || true)"
        if [[ -n "$current_size" && "$current_size" == "$template_size" ]]; then
            source_nvram="$current_nvram"
            print_info "保留 $vm_name 的现有 NVRAM 状态以供 QEMU $SELECTED_QEMU_VERSION 使用。" >&2
        else
            print_warn "$vm_name 的现有 NVRAM 与目标 OVMF 模板容量不同，将创建新的版本专用 NVRAM。" >&2
        fi
    fi

    sudo install -d -m 0755 "$nvram_dir" || return 1
    sudo install -m 0644 "$source_nvram" "$target_nvram" || return 1
    if id -u libvirt-qemu >/dev/null 2>&1; then
        sudo chown libvirt-qemu:kvm "$target_nvram" 2>/dev/null || true
    elif id -u qemu >/dev/null 2>&1; then
        sudo chown qemu:qemu "$target_nvram" 2>/dev/null || true
    fi
    command -v restorecon >/dev/null 2>&1 && sudo restorecon "$target_nvram" 2>/dev/null || true

    echo "$target_nvram"
}

update_q35_domain_firmware() {
    local vm_name="$1"
    local xml_file="$RUNTIME_ROOT/${vm_name}_qemu${SELECTED_QEMU_VERSION}.xml"
    local current_machine=""
    local current_nvram=""
    local target_nvram=""
    local firmware_count=""
    local loader_count=""
    local nvram_count=""
    local emulator_count=""
    local target_machine="pc-q35-${SELECTED_QEMU_VERSION}"

    run_virsh dumpxml "$vm_name" > "$xml_file" || {
        print_warn "无法导出虚拟机 XML，跳过: $vm_name"
        return 0
    }

    current_machine=$(xmlstarlet sel -t -v "/domain/os/type/@machine" "$xml_file" 2>/dev/null)
    if [[ "$current_machine" != pc-q35-* ]]; then
        print_info "跳过 $vm_name：当前机器类型不是 Q35 (${current_machine:-未设置})。"
        rm -f "$xml_file"
        return 0
    fi

    current_nvram=$(xmlstarlet sel -t -v "normalize-space(/domain/os/nvram)" "$xml_file" 2>/dev/null)
    target_nvram=$(prepare_versioned_nvram "$vm_name" "$current_nvram") || {
        print_warn "无法准备 $vm_name 的版本专用 NVRAM，跳过 XML 更新。"
        rm -f "$xml_file"
        return 0
    }

    xml_set_or_add_attr "$xml_file" "/domain/os" "firmware" "efi"
    xmlstarlet ed -L -u "/domain/os/type" -v "hvm" "$xml_file"
    xml_set_or_add_attr "$xml_file" "/domain/os/type" "arch" "x86_64"
    xml_set_or_add_attr "$xml_file" "/domain/os/type" "machine" "$target_machine"

    firmware_count=$(xmlstarlet sel -t -v "count(/domain/os/firmware)" "$xml_file" 2>/dev/null)
    if [[ "$firmware_count" == "0" ]]; then
        xmlstarlet ed -L -a "/domain/os/type" -t elem -n "firmware" -v "" "$xml_file"
    fi
    xml_ensure_firmware_feature "$xml_file" "enrolled-keys"
    xml_ensure_firmware_feature "$xml_file" "secure-boot"

    loader_count=$(xmlstarlet sel -t -v "count(/domain/os/loader)" "$xml_file" 2>/dev/null)
    if [[ "$loader_count" == "0" ]]; then
        xmlstarlet ed -L -a "/domain/os/firmware" -t elem -n "loader" -v "" "$xml_file"
    fi
    xmlstarlet ed -L -u "/domain/os/loader" -v "$OVMF_CODE_PATH" "$xml_file"
    xml_set_or_add_attr "$xml_file" "/domain/os/loader" "readonly" "yes"
    xml_set_or_add_attr "$xml_file" "/domain/os/loader" "secure" "yes"
    xml_set_or_add_attr "$xml_file" "/domain/os/loader" "type" "pflash"
    xml_set_or_add_attr "$xml_file" "/domain/os/loader" "format" "qcow2"

    nvram_count=$(xmlstarlet sel -t -v "count(/domain/os/nvram)" "$xml_file" 2>/dev/null)
    if [[ "$nvram_count" == "0" ]]; then
        xmlstarlet ed -L -a "/domain/os/loader" -t elem -n "nvram" -v "" "$xml_file"
    fi
    xmlstarlet ed -L -u "/domain/os/nvram" -v "$target_nvram" "$xml_file"
    xml_set_or_add_attr "$xml_file" "/domain/os/nvram" "template" "$OVMF_VARS_PATH"
    xml_set_or_add_attr "$xml_file" "/domain/os/nvram" "format" "qcow2"

    if [[ "${SELECTED_QEMU_VERSION:-}" == "11.0" ]]; then
        ensure_qemu_display_model "$xml_file"
    fi

    emulator_count=$(xmlstarlet sel -t -v "count(/domain/devices/emulator)" "$xml_file" 2>/dev/null)
    if [[ "$emulator_count" == "0" ]]; then
        xmlstarlet ed -L -s "/domain/devices" -t elem -n "emulator" -v "$QEMU_BIN_PATH" "$xml_file"
    else
        xmlstarlet ed -L -u "/domain/devices/emulator" -v "$QEMU_BIN_PATH" "$xml_file"
    fi

    if ! xmlstarlet val -q "$xml_file"; then
        print_warn "$vm_name 的更新后 XML 无效，保留文件供排查: $xml_file"
        return 0
    fi

    if ! run_virsh define "$xml_file" >/dev/null; then
        print_warn "无法写回 $vm_name 的 XML，保留文件供排查: $xml_file"
        return 0
    fi

    print_ok "已将 $vm_name 切换为 $target_machine，并使用 $OVMF_CODE_PATH。"
    rm -f "$xml_file"
}

update_existing_q35_domains() {
    local target_machine="pc-q35-${SELECTED_QEMU_VERSION}"
    local domain_name=""
    local domain_state=""
    local configured_names="${KVMSPOOF_VM_NAMES:-win10 win11}"

    if [[ "${KVMSPOOF_UPDATE_VM_XML:-1}" != "1" ]]; then
        print_info "已跳过现有虚拟机 XML 更新。"
        return 0
    fi
    if ! command -v virsh >/dev/null 2>&1 || ! command -v xmlstarlet >/dev/null 2>&1; then
        print_warn "未找到 virsh 或 xmlstarlet，跳过现有虚拟机 XML 更新。"
        return 0
    fi
    if [[ ! -x "$QEMU_BIN_PATH" || ! -f "$OVMF_CODE_PATH" || ! -f "$OVMF_VARS_PATH" ]]; then
        print_warn "QEMU 或 OVMF 文件未就绪，跳过现有虚拟机 XML 更新。"
        return 0
    fi
    if ! "$QEMU_BIN_PATH" -machine help 2>/dev/null | grep -q "^${target_machine}[[:space:]]"; then
        print_warn "当前 QEMU 不支持机器类型 $target_machine，跳过 XML 更新。"
        return 0
    fi

    for domain_name in $configured_names; do
        if ! run_virsh list --all --name 2>/dev/null | grep -qx "$domain_name"; then
            continue
        fi
        domain_state=$(run_virsh domstate "$domain_name" 2>/dev/null || true)
        if [[ "$domain_state" != "shut off" ]]; then
            print_warn "跳过正在运行或暂停的虚拟机 $domain_name（状态: ${domain_state:-未知}）。"
            continue
        fi
        update_q35_domain_firmware "$domain_name"
    done
}

# ============================================================
# 安装固定名称的自定义 OVMF 固件 JSON，后安装的版本覆盖前一次。
# ============================================================
install_custom_ovmf_firmware_json() {
    local ovmf_json_name="${KVMSPOOF_OVMF_JSON_NAME:-00-ovmf-x64.json}"
    local ovmf_json_dest="/usr/share/qemu/firmware/$ovmf_json_name"
    local tmp_json="$RUNTIME_ROOT/$ovmf_json_name"
    local firmware_desc="Custom UEFI-SecureBoot Firmware (${SELECTED_BUILD_LABEL:-QEMU+EDK2}) with Enrolled Keys"

    # 获取固件文件路径
    local code_file=""
    local vars_file=""
    if OVMF_PATHS=$(find_ovmf_files); then
        code_file="${OVMF_PATHS%%|*}"
        vars_file="${OVMF_PATHS##*|}"
    else
        # 如果查找失败，尝试从 vars.sh 中读取上次保存的路径
        if [[ -f "$VARS_FILE" ]]; then
            source "$VARS_FILE"
            if [[ -n "${OVMF_CODE_PATH:-}" && -f "${OVMF_CODE_PATH:-}" ]]; then
                code_file="$OVMF_CODE_PATH"
            fi
            if [[ -n "${OVMF_VARS_PATH:-}" && -f "${OVMF_VARS_PATH:-}" ]]; then
                vars_file="$OVMF_VARS_PATH"
            fi
        fi
    fi

    if [[ -z "$code_file" || ! -f "$code_file" ]]; then
        print_err "❌ 未找到 OVMF CODE 文件。请通过环境变量 OVMF_CODE 指定。"
        return 1
    fi
    if [[ -z "$vars_file" || ! -f "$vars_file" ]]; then
        print_err "❌ 未找到 OVMF VARS 文件。请通过环境变量 OVMF_VARS 指定。"
        return 1
    fi

    cat > "$tmp_json" << EOF
{
    "description": "$firmware_desc",
    "interface-types": [
        "uefi"
    ],
    "mapping": {
        "device": "flash",
        "executable": {
            "format": "qcow2",
            "filename": "$code_file"
        },
        "nvram-template": {
            "format": "qcow2",
            "filename": "$vars_file"
        }
    },
    "targets": [
        {
            "architecture": "x86_64",
            "machines": [
                "pc-q35-*",
                "pc-i440fx-*"
            ]
        }
    ],
    "features": [
        "acpi-s3",
        "acpi-s4",
        "requires-smm",
        "secure-boot",
        "enrolled-keys"
    ]
}
EOF

    sudo mkdir -p "$(dirname "$ovmf_json_dest")"
    if sudo cp -f "$tmp_json" "$ovmf_json_dest"; then
        OVMF_CODE_PATH="$code_file"
        OVMF_VARS_PATH="$vars_file"
        OVMF_JSON_PATH="$ovmf_json_dest"
    else
        print_err "❌ 复制 JSON 失败，请检查权限"
        rm -f "$tmp_json"
        return 1
    fi

    rm -f "$tmp_json"
}

# ============================================================
# 硬盘厂商列表与容量格式化
# ============================================================
HDD_VENDORS=(
    "wd:Western Digital"
    "seagate:Seagate"
    "samsung:Samsung"
    "toshiba:Toshiba"
    "sandisk:SanDisk"
    "micron:Micron"
    "kingston:Kingston"
    "intel:Intel"
    "corsair:Corsair"
    "crucial:Crucial"
    "adata:ADATA"
    "hitachi:Hitachi"
    "hynix:SK Hynix"
    "plextor:Plextor"
    "transcend:Transcend"
    "pny:PNY"
    "patriot:Patriot"
    "sabrent:Sabrent"
    "siliconpower:Silicon Power"
    "teamgroup:Team Group"
)

format_disk_capacity() {
    local gb="$1"

    if [[ "$gb" -ge 1000 && $((gb % 1000)) -eq 0 ]]; then
        echo "$((gb / 1000))TB"
    else
        echo "${gb}GB"
    fi
}

safe_source_vars() {
    local vars_file="$1"

    set +e
    # shellcheck disable=SC1090
    source "$vars_file" 2>/dev/null
    local source_code=$?
    set -e

    if [[ "$source_code" -ne 0 ]]; then
        print_warn "配置文件 $vars_file 格式异常，将重新生成。"
        return 1
    fi

    return 0
}

# ============================================================
# 设置硬盘厂商（轮换随机：未用完前不重复）
# ============================================================
setup_hdd_vendor_random() {
    local vendor_info=""
    local vendor_id=""
    local available=()
    local used_count=0

    HDD_VENDOR_USED_IDS="${HDD_VENDOR_USED_IDS:-}"

    for vendor_info in "${HDD_VENDORS[@]}"; do
        vendor_id="${vendor_info%%:*}"
        if [[ " $HDD_VENDOR_USED_IDS " == *" $vendor_id "* ]]; then
            ((used_count+=1))
        else
            available+=("$vendor_info")
        fi
    done

    if [[ "${#available[@]}" -eq 0 || "$used_count" -ge "${#HDD_VENDORS[@]}" ]]; then
        HDD_VENDOR_USED_IDS=""
        available=("${HDD_VENDORS[@]}")
        print_info "硬盘厂商已全部轮换使用过，开始新一轮随机。"
    fi

    vendor_info="${available[$((RANDOM % ${#available[@]}))]}"
    HDD_VENDOR_ID="${vendor_info%%:*}"
    HDD_VENDOR_NAME="${vendor_info##*:}"
    HDD_VENDOR_CHOICE="${HDD_VENDOR_ID}:${HDD_VENDOR_NAME}"
    HDD_VENDOR_USED_IDS="${HDD_VENDOR_USED_IDS:+$HDD_VENDOR_USED_IDS }$HDD_VENDOR_ID"

    export HDD_VENDOR_ID HDD_VENDOR_NAME HDD_VENDOR_CHOICE HDD_VENDOR_USED_IDS

    print_ok "硬盘厂商已随机设置为：$HDD_VENDOR_NAME ($HDD_VENDOR_ID)"
    print_info "本轮已使用硬盘厂商：$HDD_VENDOR_USED_IDS"
}

# ============================================================
# 写入用户配置（容量+内存） + 刷新所有随机项（硬件ID + 厂商）
# ============================================================
write_vm_defaults() {
    local defaults_file="$1"

    # ---------- 强制清理变量中的多余引号 ----------
    USER_CAPACITY="${USER_CAPACITY//\"/}"
    DISK_CAPACITY="${DISK_CAPACITY//\"/}"
    USER_MEM_SLOT="${USER_MEM_SLOT//\"/}"
    VM_MEMORY_MB="${VM_MEMORY_MB//\"/}"
    VM_MEMORY_GB="${VM_MEMORY_GB//\"/}"
    # 生成新的硬件 ID（每次都随机刷新）
    local device=$(( ($(date +"%-d") + $(date +"%-m"))*100 + $(date +"%-d") * $(date +"%-m") ))
    local vendor=$(( device + ((RANDOM%768)+256) ))
    local xhci=$(( 49152 - device - ((RANDOM%768)+256) ))
    local virtio=$(( 49152 + device ))

    # 随机生成 edk2bridge 值（0x0000-0xFFFF）
    local edk2bridge_1022=$(printf "%04X" $((RANDOM % 65536)))
    local edk2bridge_8086=$(printf "%04X" $((RANDOM % 65536)))

    # 每次编译都轮换随机选择硬盘厂商；同一轮未用完前不重复
    setup_hdd_vendor_random

    # 写入整个文件
    cat > "$defaults_file" << EOF
# Generated by nothing.sh
device="$device"
vendor="$vendor"
xhci="$xhci"
virtio="$virtio"
edk2bridge_1022="$edk2bridge_1022"
edk2bridge_8086="$edk2bridge_8086"
USER_CAPACITY="$USER_CAPACITY"
DISK_CAPACITY="$DISK_CAPACITY"
USER_MEM_SLOT="$USER_MEM_SLOT"
VM_MEMORY_GB="$VM_MEMORY_GB"
VM_MEMORY_MB="$VM_MEMORY_MB"
HDD_VENDOR_ID="$HDD_VENDOR_ID"
HDD_VENDOR_NAME="$HDD_VENDOR_NAME"
HDD_VENDOR_CHOICE="$HDD_VENDOR_CHOICE"
HDD_VENDOR_USED_IDS="$HDD_VENDOR_USED_IDS"
EOF

    print_ok "硬件 ID 和硬盘厂商已刷新，容量/内存配置已写入 $defaults_file"
}

# ============================================================
# 加载配置：优先从 vars.sh，若不存在则从旧的 state_file 迁移
# ============================================================
load_vm_defaults() {
    if [[ -f "$VARS_FILE" ]]; then
        if safe_source_vars "$VARS_FILE"; then
            echo "已加载配置：硬盘 ${DISK_CAPACITY}，内存 ${VM_MEMORY_MB}MB，厂商 ${HDD_VENDOR_NAME}"
            return 0
        fi
        return 1
    fi

    # 尝试从旧的 state_file 迁移
    if [[ -f "$STATE_FILE_OLD" ]]; then
        print_warn "检测到旧的 kvmspoof_state.env，正在迁移到 vars.sh..."
        safe_source_vars "$STATE_FILE_OLD" || true
        # 将旧配置写入 vars.sh（保留原有硬件 ID 占位）
        if [[ ! -f "$VARS_FILE" ]]; then
            {
                echo "# Generated by nothing.sh (migrated from kvmspoof_state.env)"
                echo "device=\"1111\""
                echo "vendor=\"8086\""
                echo "xhci=\"49152\""
                echo "virtio=\"49153\""
                echo "edk2bridge_1022=\"29C0\""
                echo "edk2bridge_8086=\"29C0\""
            } > "$VARS_FILE"
        fi
        write_vm_defaults "$VARS_FILE"
        rm -f "$STATE_FILE_OLD"
        echo "配置已迁移并删除旧文件。"
        echo "已加载配置：硬盘 ${DISK_CAPACITY}，内存 ${VM_MEMORY_MB}MB，厂商 ${HDD_VENDOR_NAME}"
        return 0
    fi

    echo "警告：未找到配置文件，将使用默认值。"
    return 1
}

# ============================================================
# 交互式设置虚拟机容量与内存（仅设置容量和内存，厂商由随机生成）
# ============================================================
setup_vm_defaults() {
    print_info "========================================"
    print_info "虚拟机硬盘与内存容量配置"
    print_info "========================================"

    USER_CAPACITY="$(prompt_positive_int "请输入您希望虚拟机使用的硬盘容量（单位：GB，例如 128/256/512/1000）：")"
    DISK_CAPACITY="$(format_disk_capacity "$USER_CAPACITY")"
    print_ok "硬盘容量已设置为：$DISK_CAPACITY"

    USER_MEM_SLOT="$(prompt_positive_int "请输入您希望虚拟机单个内存设备容量（单位：GB，例如 4/8/16）：")"
    VM_MEMORY_MB=$((USER_MEM_SLOT * 1024))
    VM_MEMORY_GB="$USER_MEM_SLOT"
    print_ok "内存设备容量已设置为：${USER_MEM_SLOT} GB (${VM_MEMORY_MB} MB)"

    export USER_CAPACITY DISK_CAPACITY USER_MEM_SLOT VM_MEMORY_MB VM_MEMORY_GB
}

# ============================================================
# 加载或设置配置（交互式）
# ============================================================
load_or_setup_vm_defaults() {
    local use_previous=""

    # 如果 vars.sh 已存在且包含必要变量，询问是否使用上次的容量和内存
    if [[ -f "$VARS_FILE" ]]; then
        if ! safe_source_vars "$VARS_FILE"; then
            print_warn "上次状态文件无法加载，将重新输入硬盘容量和内存大小。"
            setup_vm_defaults
            return
        fi
        if [[ -n "${DISK_CAPACITY:-}" && -n "${VM_MEMORY_MB:-}" ]]; then
            print_info "检测到上次设置的参数："
            print_info "  硬盘容量: $DISK_CAPACITY"
            print_info "  内存大小: ${VM_MEMORY_MB} MB (${USER_MEM_SLOT:-${VM_MEMORY_GB:-?}} GB)"
            echo -n "是否启用上次设置的硬盘容量和内存大小？(Y/n): "
            read_menu_choice ""
            use_previous="$MENU_CHOICE"

            if [[ -z "$use_previous" || "$use_previous" =~ ^[Yy]$ ]]; then
                # 确保变量一致性，并清理可能的引号
                DISK_CAPACITY="${DISK_CAPACITY//\"/}"
                VM_MEMORY_MB="${VM_MEMORY_MB//\"/}"
                USER_CAPACITY="${USER_CAPACITY:-}"
                USER_CAPACITY="${USER_CAPACITY//\"/}"
                USER_MEM_SLOT="${USER_MEM_SLOT:-}"
                USER_MEM_SLOT="${USER_MEM_SLOT//\"/}"

                if [[ -z "$USER_CAPACITY" ]]; then
                    if ! USER_CAPACITY="$(normalize_capacity_to_gb "$DISK_CAPACITY")"; then
                        print_warn "无法解析上次硬盘容量：$DISK_CAPACITY，将重新输入硬盘容量和内存大小。"
                        setup_vm_defaults
                        return
                    fi
                fi

                if [[ -z "$USER_MEM_SLOT" ]]; then
                    if [[ "$VM_MEMORY_MB" =~ ^[0-9]+$ && "$VM_MEMORY_MB" -gt 0 ]]; then
                        USER_MEM_SLOT="$(((VM_MEMORY_MB + 1023) / 1024))"
                    elif [[ "${VM_MEMORY_GB:-}" =~ ^[0-9]+$ && "${VM_MEMORY_GB:-}" -gt 0 ]]; then
                        USER_MEM_SLOT="$VM_MEMORY_GB"
                    else
                        print_warn "无法解析上次内存大小：${VM_MEMORY_MB:-未设置}，将重新输入硬盘容量和内存大小。"
                        setup_vm_defaults
                        return
                    fi
                fi

                VM_MEMORY_GB="$USER_MEM_SLOT"
                VM_MEMORY_MB="$((USER_MEM_SLOT * 1024))"
                export USER_CAPACITY DISK_CAPACITY USER_MEM_SLOT VM_MEMORY_MB VM_MEMORY_GB
                print_ok "已启用上次设置。"
                return
            fi
            print_info "将重新输入硬盘容量和内存大小。"
        else
            print_warn "上次状态文件不完整，将重新输入硬盘容量和内存大小。"
        fi
    else
        print_info "未找到配置文件，将进行初始设置。"
    fi

    setup_vm_defaults
}

# ============================================================
# 确保 vars.sh 存在（如果不存在则创建临时的）
# ============================================================
ensure_vars_file() {
    if [[ ! -f "$VARS_FILE" ]]; then
        print_info "未找到 $VARS_FILE，创建空的占位文件..."
        mkdir -p "$VARS_ROOT"
        touch "$VARS_FILE"
        echo "# 临时 vars.sh 占位文件（由 nothing.sh 自动生成）" > "$VARS_FILE"
        print_ok "已创建空的 vars.sh: $VARS_FILE"
    fi
}

# ============================================================
# 主流程（已优化，去除了重复边框和等待按键）
# ============================================================
main() {
    if ! select_build_profile; then
        exit 1
    fi

    prepare_selected_work_root

    # 确保 vars.sh 存在
    ensure_vars_file

    print_info "释放/资源目录: $RELEASE_ROOT"
    print_info "构建/克隆目录: $WORK_ROOT"
    print_info "已选择: $SELECTED_BUILD_LABEL"

    # ===== QEMU + EDK2 编译流程 =====
    print_info "本次硬件 ID 状态文件目录: $VARS_ROOT"
    print_info "QEMU 编译脚本: $SELECTED_QEMU_SCRIPT"
    if [[ -n "${SELECTED_EDK2_SCRIPT:-}" ]]; then
        print_info "EDK2 编译脚本: $SELECTED_EDK2_SCRIPT"
    fi

    # 加载或设置配置（会处理 vars.sh 和旧的 state_file），仅设置容量和内存
    load_or_setup_vm_defaults

    # 不删除 vars.sh：每次仅重写随机项，容量和内存按上面的询问结果保留或重新设置
    # 每次运行都刷新所有随机项（硬件 ID、厂商），同时保留容量和内存
    write_vm_defaults "$VARS_FILE"
    stage_work_resources
    install_build_dependencies

    if [[ -n "${SELECTED_EDK2_SCRIPT:-}" ]]; then
        # 先编译 QEMU，再编译 EDK2（OVMF）
        run_patch_script "QEMU ${SELECTED_QEMU_VERSION} 安装/构建脚本" "$SELECTED_QEMU_SCRIPT" || {
            print_err "QEMU 编译失败，停止执行。"
            exit 1
        }

        run_patch_script "EDK2 安装/构建脚本" "$SELECTED_EDK2_SCRIPT" || {
            print_err "EDK2 编译失败，停止执行。"
            exit 1
        }
    else
        run_patch_script "$SELECTED_BUILD_LABEL 安装/构建脚本" "$SELECTED_QEMU_SCRIPT"
    fi

    check_qemu_installed || exit 1
    check_edk2_outputs || exit 1
    install_custom_ovmf_firmware_json || exit 1
    update_existing_q35_domains

    # ===== 最终完成提示 =====
    print_success_header
    echo -e "${GREEN}  ✅ QEMU / OVMF 安装完成${NC}"
    [[ -n "${QEMU_VERSION_LINE:-}" ]] && echo -e "${GREEN}  版本: $QEMU_VERSION_LINE${NC}"
    echo -e "${GREEN}  QEMU: ${QEMU_BIN_PATH:-/usr/local/bin/qemu-system-x86_64}${NC}"
    echo -e "${GREEN}  OVMF CODE: ${OVMF_CODE_PATH:-未检测到}${NC}"
    echo -e "${GREEN}  OVMF VARS: ${OVMF_VARS_PATH:-未检测到}${NC}"
    print_success_footer
}

main "$@"
