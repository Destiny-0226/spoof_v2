#!/bin/bash

# ============================================================
#  颜色与消息函数
# ============================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_message() {
    echo -e "${2}${1}${NC}"
}

run_virsh() {
    virsh "$@" 2>/dev/null || sudo virsh "$@"
}

validate_xml() {
    local xml_file="$1"
    local step_name="$2"

    if ! xmlstarlet val -q "$xml_file" 2>/tmp/kvmspoof_xml_error.log; then
        print_message "❌ $step_name 后 XML 格式无效，已停止写入虚拟机。" "$RED"
        cat /tmp/kvmspoof_xml_error.log >&2
        print_message "已保留临时 XML 供排查: $xml_file" "$YELLOW"
        exit 1
    fi
}

# ============================================================
#  运行时目录（仅用于临时文件）
# ============================================================
RUNTIME_ROOT="${KVMSPOOF_RUNTIME_DIR:-/tmp/kvmspoof-runtime-standalone}"
mkdir -p "$RUNTIME_ROOT"
QEMU_EMULATOR="${KVMSPOOF_QEMU_BIN:-/usr/local/bin/qemu-system-x86_64}"
OVMF_CODE_PATH="${OVMF_CODE_PATH:-}"
OVMF_VARS_PATH="${OVMF_VARS_PATH:-}"
QEMU_VERSION_TAG="${KVMSPOOF_QEMU_VERSION:-}"

# ============================================================
#  加载 vars.sh 配置
# ============================================================
load_vm_config() {
    local vars_file="${VARS_FILE:-}"
    local candidate=""
    local candidates=()
    if [[ -z "$vars_file" ]]; then
        local vars_dir="${KVMSPOOF_VARS_DIR:-}"
        if [[ -n "$vars_dir" ]]; then
            vars_file="$vars_dir/vars.sh"
        else
            candidates=(
                "/tmp/spoof/vars.sh"
                "$HOME/vars.sh"
                "$(pwd)/vars.sh"
                "$(pwd)/qemu11.0-edk2/vars.sh"
                "$(pwd)/qemu9.2-edk2/vars.sh"
            )
            for candidate in "${candidates[@]}"; do
                if [[ -f "$candidate" ]]; then
                    vars_file="$candidate"
                    break
                fi
            done
            : "${vars_file:=/tmp/spoof/vars.sh}"
        fi
    fi
    if [[ -f "$vars_file" ]]; then
        # 兼容旧版 vars.sh 中厂商名含空格但未正确转义导致的 source 报错。
        # shellcheck disable=SC1090
        source "$vars_file" 2>/dev/null || true
        HDD_VENDOR_ID="${HDD_VENDOR_ID//\"/}"
        HDD_VENDOR_NAME="${HDD_VENDOR_NAME//\"/}"
        HDD_VENDOR_CHOICE="${HDD_VENDOR_CHOICE//\"/}"
        HDD_VENDOR_USED_IDS="${HDD_VENDOR_USED_IDS//\"/}"
        # qemu9.2patch.sh 编译成功后回写的"实际编译进二进制的厂商"。
        # 这个才是 Windows 里真正看到的厂商名，优先级高于 HDD_VENDOR_ID。
        HDD_VENDOR_COMPILED_ID="${HDD_VENDOR_COMPILED_ID//\"/}"
        HDD_VENDOR_COMPILED_NAME="${HDD_VENDOR_COMPILED_NAME//\"/}"
        HDD_VENDOR_COMPILED_MODEL="${HDD_VENDOR_COMPILED_MODEL//\"/}"
        OVMF_CODE_PATH="${OVMF_CODE_PATH//\"/}"
        OVMF_VARS_PATH="${OVMF_VARS_PATH//\"/}"
        echo "已加载配置：硬盘厂商 ${HDD_VENDOR_NAME:-${HDD_VENDOR_ID:-未设置}}" >&2
        if [[ -n "${HDD_VENDOR_COMPILED_NAME:-}" ]]; then
            echo "已编译进 QEMU 的厂商：$HDD_VENDOR_COMPILED_NAME" >&2
        fi
        return 0
    else
        echo "警告：未找到 $vars_file，将使用随机厂商。" >&2
        return 1
    fi
}

# ============================================================
#  依赖检查：xmlstarlet
# ============================================================
check_xmlstarlet() {
    if ! command -v xmlstarlet &>/dev/null; then
        print_message "xmlstarlet 未安装，尝试自动安装..." "$YELLOW"
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y xmlstarlet
        elif command -v yum &>/dev/null; then
            sudo yum install -y xmlstarlet
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y xmlstarlet
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --needed --noconfirm xmlstarlet
        else
            print_message "无法自动安装 xmlstarlet，请手动安装后重试。" "$RED"
            exit 1
        fi
        if ! command -v xmlstarlet &>/dev/null; then
            print_message "xmlstarlet 安装失败，请手动安装。" "$RED"
            exit 1
        fi
        print_message "xmlstarlet 安装成功。" "$GREEN"
    fi
}

# ============================================================
#  网络 MAC 伪装（虚拟网络 + 虚拟机网卡）
# ============================================================
fix_virtual_network_mac() {
    print_message "=== [功能1] 修复虚拟网络 MAC 地址 ===" "$BLUE"
    local NET_NAME="default"
    local current_mac new_mac selected_oui nic_part verify_mac

    current_mac=$(sudo virsh net-dumpxml "$NET_NAME" 2>/dev/null | grep -oP "mac address='[^']+'" | head -1 | cut -d"'" -f2)
    if [ -z "$current_mac" ]; then
        print_message "错误：未找到 MAC 地址" "$RED"
        return 1
    fi
    print_message "当前 MAC 地址: $current_mac" "$YELLOW"

    local -a oui_list
    oui_list=(00:02:B3 00:03:47 00:04:23 00:0C:F1 00:12:F0
              00:13:20 00:13:E8 00:15:17 00:1B:21 00:1C:C0
              00:22:FA 00:24:D7 00:A0:C9 34:13:E8 3C:FD:FE
              54:EB:AE 68:05:CA 6C:88:14 8C:DC:D4 90:E2:BA
              98:4F:EE 9C:6B:D0 A0:36:9F A0:C5:89 BC:83:A7
              C8:D9:D2 E4:1F:13 F8:F0:05)
    selected_oui=${oui_list[$RANDOM % ${#oui_list[@]}]}
    nic_part=$(printf "%02X:%02X:%02X" $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))
    new_mac="${selected_oui}:${nic_part,,}"
    print_message "生成新 MAC 地址: $new_mac" "$GREEN"

    local tmp_xml="/tmp/net_${NET_NAME}.xml"
    sudo virsh net-dumpxml "$NET_NAME" > "$tmp_xml"
    sudo sed -i "s|mac address='[^']*'|mac address='$new_mac'|" "$tmp_xml"
    sudo virsh net-destroy "$NET_NAME" 2>/dev/null
    sudo virsh net-define "$tmp_xml" >/dev/null 2>&1
    sudo virsh net-start "$NET_NAME" >/dev/null 2>&1
    rm -f "$tmp_xml"

    verify_mac=$(sudo virsh net-dumpxml "$NET_NAME" | grep -oP "mac address='[^']+'" | head -1 | cut -d"'" -f2)
    if [ "$verify_mac" = "$new_mac" ]; then
        print_message "✅ 网络 MAC 地址已更新为 $new_mac" "$GREEN"
    else
        print_message "⚠️  网络 MAC 验证失败，请手动检查" "$RED"
    fi
}

fix_vm_interface_mac() {
    print_message "=== [功能1] 修改虚拟机网卡 MAC 地址 ===" "$BLUE"
    local xml_file="$1"

    local iface_count
    iface_count=$(xmlstarlet sel -t -v "count(/domain/devices/interface)" "$xml_file" 2>/dev/null)
    if [[ -z "$iface_count" || "$iface_count" -eq 0 ]]; then
        print_message "虚拟机无网卡设备，跳过 MAC 修改。" "$YELLOW"
        return 0
    fi

    local current_mac new_mac selected_oui nic_part
    current_mac=$(xmlstarlet sel -t -v "/domain/devices/interface[1]/mac/@address" "$xml_file" 2>/dev/null)
    if [ -z "$current_mac" ]; then
        print_message "警告：未找到网卡 MAC 地址，跳过。" "$RED"
        return 0
    fi
    print_message "当前网卡 MAC: $current_mac" "$YELLOW"

    local -a oui_list
    oui_list=(00:02:B3 00:03:47 00:04:23 00:0C:F1 00:12:F0
              00:13:20 00:13:E8 00:15:17 00:1B:21 00:1C:C0
              00:22:FA 00:24:D7 00:A0:C9 34:13:E8 3C:FD:FE
              54:EB:AE 68:05:CA 6C:88:14 8C:DC:D4 90:E2:BA
              98:4F:EE 9C:6B:D0 A0:36:9F A0:C5:89 BC:83:A7
              C8:D9:D2 E4:1F:13 F8:F0:05)
    selected_oui=${oui_list[$RANDOM % ${#oui_list[@]}]}
    nic_part=$(printf "%02X:%02X:%02X" $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))
    new_mac="${selected_oui}:${nic_part,,}"
    print_message "生成新网卡 MAC: $new_mac" "$GREEN"

    xmlstarlet ed -L -u "/domain/devices/interface[1]/mac/@address" -v "$new_mac" "$xml_file"

    local verify_mac
    verify_mac=$(xmlstarlet sel -t -v "/domain/devices/interface[1]/mac/@address" "$xml_file" 2>/dev/null)
    if [ "$verify_mac" = "$new_mac" ]; then
        print_message "✅ 网卡 MAC 已更新为 $new_mac" "$GREEN"
    else
        print_message "⚠️  网卡 MAC 验证失败" "$RED"
    fi
}

# ============================================================
#  硬盘序列号与厂商伪造（统一格式；同次执行可共享品牌，但序列号彼此不同）
# ============================================================
HDD_VENDORS=(
    "wd:Western Digital" "seagate:Seagate" "samsung:Samsung" "toshiba:Toshiba"
    "sandisk:SanDisk" "micron:Micron" "kingston:Kingston" "intel:Intel"
    "corsair:Corsair" "crucial:Crucial" "adata:ADATA" "hitachi:Hitachi"
    "hynix:SK Hynix" "plextor:Plextor" "transcend:Transcend" "pny:PNY"
    "patriot:Patriot" "sabrent:Sabrent" "siliconpower:Silicon Power" "teamgroup:Team Group"
)

get_vendor_serial_prefix() {
    local vendor="$1"
    case "$vendor" in
        "wd")            echo "WD" ;;
        "seagate")       echo "ST" ;;
        "samsung")       echo "S" ;;
        "toshiba")       echo "T" ;;
        "sandisk")       echo "SD" ;;
        "micron")        echo "MTF" ;;
        "kingston")      echo "K" ;;
        "intel")         echo "BT" ;;
        "corsair")       echo "C" ;;
        "crucial")       echo "CT" ;;
        "adata")         echo "AD" ;;
        "hitachi")       echo "HT" ;;
        "hynix")         echo "HFS" ;;
        "plextor")       echo "PX" ;;
        "transcend")     echo "TS" ;;
        "pny")           echo "PNY" ;;
        "patriot")       echo "PS" ;;
        "sabrent")       echo "SB" ;;
        "siliconpower")  echo "SP" ;;
        "teamgroup")     echo "TG" ;;
        *)                echo "${vendor^^}" | cut -c 1-4 ;;
    esac
}

generate_hdd_serial() {
    local vendor="$1"
    local prefix random_len
    prefix=$(get_vendor_serial_prefix "$vendor")
    random_len=$((10 + RANDOM % 4))
    echo "${prefix}$(tr -dc 'A-Z0-9' </dev/urandom | head -c "$random_len")"
}

get_vendor() {
    if load_vm_config; then
        # 优先用实际编译进 QEMU 二进制的厂商，其次才是 vars.sh 里的当前值。
        # 旧代码这里返回的是 "id:id"（冒号后面本该是厂商名），虽然当前调用方
        # 只取前半段所以没暴露问题，但拿后半段当显示名就会得到 id。
        local id="${HDD_VENDOR_COMPILED_ID:-${HDD_VENDOR_ID:-}}"
        local name="${HDD_VENDOR_COMPILED_NAME:-${HDD_VENDOR_NAME:-}}"
        if [[ -n "$id" ]]; then
            echo "${id}:${name:-$id}"
            return
        fi
    fi
    local vendor_index=$((RANDOM % ${#HDD_VENDORS[@]}))
    echo "${HDD_VENDORS[$vendor_index]}"
}

# ============================================================
#  修改硬盘序列号（统一格式；同次执行尽量保持同品牌）
# ============================================================
apply_hdd_serial() {
    print_message "=== [功能3] 修改硬盘序列号 ===" "$BLUE"
    local xml_file="$1"
    local disk_count
    disk_count=$(xmlstarlet sel -t -v "count(/domain/devices/disk)" "$xml_file" 2>/dev/null)
    if [[ -z "$disk_count" || "$disk_count" -eq 0 ]]; then
        print_message "无磁盘设备，跳过序列号修改。" "$YELLOW"
        return 0
    fi

    # 厂商来源优先级：已编译进二进制的 > vars.sh 期望值 > 随机。
    # 两者不一致时必须跟二进制走：nothing.sh 每次运行都会重新随机厂商
    # （write_vm_defaults -> setup_hdd_vendor_random），若之后编译失败或
    # 没重新编译，vars.sh 是新厂商而二进制还是旧厂商。序列号前缀跟错了，
    # Windows 里看到的磁盘厂商名和序列号前缀就对不上。
    load_vm_config
    local fixed_vendor_prefix=""
    if [[ -n "${HDD_VENDOR_COMPILED_ID:-}" ]]; then
        fixed_vendor_prefix="$HDD_VENDOR_COMPILED_ID"
        if [[ -n "${HDD_VENDOR_ID:-}" && "$HDD_VENDOR_ID" != "$HDD_VENDOR_COMPILED_ID" ]]; then
            print_message "⚠️  vars.sh 期望厂商 ($HDD_VENDOR_ID) 与已编译进 QEMU 的厂商 ($HDD_VENDOR_COMPILED_ID) 不一致。" "$YELLOW"
            print_message "    序列号按已编译的 $HDD_VENDOR_COMPILED_ID 生成；要让 $HDD_VENDOR_ID 生效需重新编译 QEMU。" "$YELLOW"
        fi
        print_message "硬盘厂商: ${HDD_VENDOR_COMPILED_NAME:-$HDD_VENDOR_COMPILED_ID}（来自已编译的 QEMU）" "$GREEN"
    elif [[ -n "${HDD_VENDOR_ID:-}" ]]; then
        fixed_vendor_prefix="$HDD_VENDOR_ID"
        print_message "硬盘厂商: ${HDD_VENDOR_NAME:-$HDD_VENDOR_ID}（来自 vars.sh，无编译记录）" "$YELLOW"
        print_message "    提示: QEMU 若未用该厂商重新编译过，磁盘厂商名仍是编译时的旧值。" "$YELLOW"
    else
        local vendor_info
        vendor_info=$(get_vendor)
        fixed_vendor_prefix="${vendor_info%%:*}"
        print_message "⚠️  未找到厂商配置，随机使用 ${vendor_info##*:}，极可能与 QEMU 二进制不一致。" "$RED"
    fi

    declare -A used_serials=()

    for ((i=1; i<=disk_count; i++)); do
        local SERIAL=""
        while [[ -z "$SERIAL" || -n "${used_serials[$SERIAL]:-}" ]]; do
            SERIAL=$(generate_hdd_serial "$fixed_vendor_prefix")
        done
        used_serials["$SERIAL"]=1

        # 检查该磁盘是否已有 <serial>，有则更新，无则添加
        local has_serial
        has_serial=$(xmlstarlet sel -t -v "count(/domain/devices/disk[$i]/serial)" "$xml_file" 2>/dev/null)
        if [[ "$has_serial" -gt 0 ]]; then
            xmlstarlet ed -L -u "/domain/devices/disk[$i]/serial" -v "$SERIAL" "$xml_file"
        else
            # 在 </disk> 前插入（使用 -a 添加兄弟节点）
            xmlstarlet ed -L -a "/domain/devices/disk[$i]/target" -t elem -n "serial" -v "$SERIAL" "$xml_file"
        fi
        print_message "磁盘 $i 序列号设置为: $SERIAL" "$GREEN"
    done
}

# ============================================================
#  QEMU 命令行底层参数注入（合并追加）
# ============================================================
inject_qemu_commandline() {
    print_message "=== [功能4] 注入 QEMU 命令行参数（不含 -cpu） ===" "$BLUE"
    local xml_file="$1"

    if ! grep -q 'xmlns:qemu=' "$xml_file"; then
        sed -i 's|<domain\(.*\)>|<domain\1 xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">|' "$xml_file"
        print_message "已添加 qemu 命名空间" "$GREEN"
    fi

    local QEMU_ARG_PAIRS=(
        "-acpitable:file=/usr/local/bin/ssdt1.aml"
        "-acpitable:file=/usr/local/bin/ssdt2.aml"
    )

    local cmd_count
    cmd_count=$(xmlstarlet sel -N qemu="http://libvirt.org/schemas/domain/qemu/1.0" \
        -t -v "count(//qemu:commandline)" "$xml_file" 2>/dev/null)

    if [[ -z "$cmd_count" || "$cmd_count" -eq 0 ]]; then
        local COMMANDLINE_CONTENT="<qemu:commandline>"
        for entry in "${QEMU_ARG_PAIRS[@]}"; do
            arg_name="${entry%%:*}"
            arg_value="${entry#*:}"
            COMMANDLINE_CONTENT+=$'\n  <qemu:arg value="'"$arg_name"'"/>'
            COMMANDLINE_CONTENT+=$'\n  <qemu:arg value="'"$arg_value"'"/>'
        done
        COMMANDLINE_CONTENT+=$'\n</qemu:commandline>'

        ex -s "$xml_file" <<EOF
/<\/devices>/ a
$COMMANDLINE_CONTENT
.
wq
EOF
        print_message "✅ QEMU 命令行已创建并注入" "$GREEN"
    else
        for entry in "${QEMU_ARG_PAIRS[@]}"; do
            arg_name="${entry%%:*}"
            arg_value="${entry#*:}"

            local existing
            existing=$(xmlstarlet sel -N qemu="http://libvirt.org/schemas/domain/qemu/1.0" \
                -t -v "count(//qemu:commandline/qemu:arg[@value='$arg_value'])" "$xml_file" 2>/dev/null)

            if [[ -z "$existing" || "$existing" -eq 0 ]]; then
                sed -i "/<\/qemu:commandline>/i\\  <qemu:arg value=\"$arg_name\"/>\n  <qemu:arg value=\"$arg_value\"/>" "$xml_file"
                print_message "✅ 已追加 QEMU 参数: $arg_name $arg_value" "$GREEN"
            else
                print_message "⏩ 参数已存在，跳过: $arg_name $arg_value" "$YELLOW"
            fi
        done
    fi
}

set_qemu_emulator() {
    local xml_file="$1"
    local emulator_count=""

    if [[ ! -x "$QEMU_EMULATOR" ]]; then
        print_message "未找到自编译 QEMU: $QEMU_EMULATOR" "$RED"
        return 1
    fi

    emulator_count=$(xmlstarlet sel -t -v "count(/domain/devices/emulator)" "$xml_file" 2>/dev/null)
    if [[ -n "$emulator_count" && "$emulator_count" -gt 0 ]]; then
        xmlstarlet ed -L -u "/domain/devices/emulator" -v "$QEMU_EMULATOR" "$xml_file"
    else
        xmlstarlet ed -L -s "/domain/devices" -t elem -n "emulator" -v "$QEMU_EMULATOR" "$xml_file"
    fi

    print_message "QEMU 模拟器: $QEMU_EMULATOR" "$GREEN"
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

ensure_firmware_feature() {
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
    local machine_version="$2"
    local video_count=""
    local model_count=""
    local graphics_count=""
    local listen_count=""
    local target_model="${KVMSPOOF_VIDEO_MODEL:-vga}"

    if [[ "${KVMSPOOF_FORCE_VIDEO_MODEL:-1}" != "1" || "$machine_version" != 11.* ]]; then
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

    print_message "显示设备: $target_model" "$GREEN"
}

resolve_ovmf_files() {
    local search_dir="/usr/local/bin"
    local machine_version=""
    local code_candidate=""
    local vars_candidate=""
    local code_candidates=()
    local vars_candidates=()

    machine_version="$(detect_q35_machine_version)"
    if [[ "$machine_version" == 11.* ]]; then
        code_candidates=(
            "${OVMF_CODE:-}"
            "$search_dir/OVMF_CODE_4M.patched.qcow2"
            "$search_dir/OVMF_CODE_4M.qcow2"
            "${OVMF_CODE_PATH:-}"
            "$search_dir/OVMF_CODE.qcow2"
            "$search_dir/OVMF_CODE.patched.qcow2"
        )
        vars_candidates=(
            "${OVMF_VARS:-}"
            "$search_dir/OVMF_VARS_4M.patched.qcow2"
            "$search_dir/OVMF_VARS_4M.qcow2"
            "${OVMF_VARS_PATH:-}"
            "$search_dir/OVMF_VARS.qcow2"
            "$search_dir/OVMF_VARS.patched.qcow2"
        )
    else
        code_candidates=(
            "${OVMF_CODE:-}"
            "$search_dir/OVMF_CODE.qcow2"
            "$search_dir/OVMF_CODE.patched.qcow2"
            "${OVMF_CODE_PATH:-}"
            "$search_dir/OVMF_CODE_4M.patched.qcow2"
            "$search_dir/OVMF_CODE_4M.qcow2"
        )
        vars_candidates=(
            "${OVMF_VARS:-}"
            "$search_dir/OVMF_VARS.qcow2"
            "$search_dir/OVMF_VARS.patched.qcow2"
            "${OVMF_VARS_PATH:-}"
            "$search_dir/OVMF_VARS_4M.patched.qcow2"
            "$search_dir/OVMF_VARS_4M.qcow2"
        )
    fi

    OVMF_CODE_PATH=""
    OVMF_VARS_PATH=""

    for code_candidate in "${code_candidates[@]}"; do
        if [[ -n "$code_candidate" && -f "$code_candidate" ]]; then
            OVMF_CODE_PATH="$code_candidate"
            break
        fi
    done

    for vars_candidate in "${vars_candidates[@]}"; do
        if [[ -n "$vars_candidate" && -f "$vars_candidate" ]]; then
            OVMF_VARS_PATH="$vars_candidate"
            break
        fi
    done

    [[ -n "$OVMF_CODE_PATH" && -n "$OVMF_VARS_PATH" ]]
}

detect_q35_machine_version() {
    local qemu_version=""
    local detected=""

    if [[ -n "$QEMU_VERSION_TAG" ]]; then
        echo "$QEMU_VERSION_TAG"
        return 0
    fi

    if [[ -x "$QEMU_EMULATOR" ]]; then
        qemu_version="$("$QEMU_EMULATOR" --version 2>/dev/null | head -n 1 || true)"
        detected="$(printf '%s\n' "$qemu_version" | sed -nE 's/.*version ([0-9]+\.[0-9]+).*/\1/p')"
        if [[ -n "$detected" ]]; then
            echo "$detected"
            return 0
        fi
    fi

    echo "11.0"
}

prepare_ovmf_nvram() {
    local vm_name="$1"
    local current_nvram="$2"
    local machine_version="$3"
    local version_tag="${machine_version//./_}"
    local nvram_dir="/var/lib/libvirt/qemu/nvram"
    local target_nvram=""

    if [[ -n "$current_nvram" ]]; then
        nvram_dir="$(dirname "$current_nvram")"
    fi
    target_nvram="$nvram_dir/${vm_name}_qemu${version_tag}_VARS.qcow2"

    if [[ ! -f "$target_nvram" ]]; then
        sudo install -d -m 0755 "$nvram_dir" || return 1
        sudo install -m 0644 "$OVMF_VARS_PATH" "$target_nvram" || return 1
        if id -u libvirt-qemu >/dev/null 2>&1; then
            sudo chown libvirt-qemu:kvm "$target_nvram" 2>/dev/null || true
        elif id -u qemu >/dev/null 2>&1; then
            sudo chown qemu:qemu "$target_nvram" 2>/dev/null || true
        fi
        command -v restorecon >/dev/null 2>&1 && sudo restorecon "$target_nvram" 2>/dev/null || true
    fi

    echo "$target_nvram"
}

apply_ovmf_firmware() {
    local xml_file="$1"
    local machine_version=""
    local target_machine=""
    local current_nvram=""
    local target_nvram=""
    local firmware_count=""
    local loader_count=""
    local nvram_count=""

    load_vm_config >/dev/null 2>&1 || true
    if ! resolve_ovmf_files; then
        print_message "未找到 OVMF CODE/VARS，跳过 <os> 固件路径更新。" "$YELLOW"
        return 0
    fi

    machine_version="$(detect_q35_machine_version)"
    target_machine="pc-q35-$machine_version"
    current_nvram=$(xmlstarlet sel -t -v "normalize-space(/domain/os/nvram)" "$xml_file" 2>/dev/null)
    target_nvram=$(prepare_ovmf_nvram "$VM_NAME" "$current_nvram" "$machine_version") || {
        print_message "无法准备 OVMF NVRAM，跳过 <os> 固件路径更新。" "$YELLOW"
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
    ensure_firmware_feature "$xml_file" "enrolled-keys"
    ensure_firmware_feature "$xml_file" "secure-boot"

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
    ensure_qemu_display_model "$xml_file" "$machine_version"

    print_message "OVMF 固件: $OVMF_CODE_PATH" "$GREEN"
}

# ============================================================
#  功能5：虚拟设备去除与电源管理伪装
# ============================================================
apply_memballoon_pm() {
    print_message "=== [功能5] 虚拟设备去除与电源管理伪装 ===" "$BLUE"
    local xml_file="$1"

    echo "检查 memballoon 设备..."
    if xmlstarlet sel -t -v "/domain/devices/memballoon/@model" "$xml_file" 2>/dev/null | grep -q "virtio"; then
        xmlstarlet ed -L -u "/domain/devices/memballoon/@model" -v "none" "$xml_file"
        echo "✅ 已修改 memballoon 的 model 为 none"
    else
        echo "未找到 virtio memballoon，跳过"
    fi

    echo "检查 PM 配置..."
    if xmlstarlet sel -t -v "/domain/pm" "$xml_file" 2>/dev/null | grep -q .; then
        if xmlstarlet sel -t -v "/domain/pm/suspend-to-mem/@enabled" "$xml_file" 2>/dev/null | grep -q .; then
            xmlstarlet ed -L -u "/domain/pm/suspend-to-mem/@enabled" -v "yes" "$xml_file"
        else
            xmlstarlet ed -L -s "/domain/pm" -t elem -n "suspend-to-mem" -v "" \
                -i "/domain/pm/suspend-to-mem" -t attr -n "enabled" -v "yes" "$xml_file"
        fi
        if xmlstarlet sel -t -v "/domain/pm/suspend-to-disk/@enabled" "$xml_file" 2>/dev/null | grep -q .; then
            xmlstarlet ed -L -u "/domain/pm/suspend-to-disk/@enabled" -v "yes" "$xml_file"
        else
            xmlstarlet ed -L -s "/domain/pm" -t elem -n "suspend-to-disk" -v "" \
                -i "/domain/pm/suspend-to-disk" -t attr -n "enabled" -v "yes" "$xml_file"
        fi
        echo "✅ PM 配置已修改"
    else
        echo "未找到 <pm> 标签，将创建并启用..."
        xmlstarlet ed -L -s "/domain" -t elem -n "pm" -v "" \
            -s "/domain/pm" -t elem -n "suspend-to-mem" -v "" \
            -i "/domain/pm/suspend-to-mem" -t attr -n "enabled" -v "yes" \
            -s "/domain/pm" -t elem -n "suspend-to-disk" -v "" \
            -i "/domain/pm/suspend-to-disk" -t attr -n "enabled" -v "yes" "$xml_file"
        echo "✅ 已创建并启用 PM 配置"
    fi
}

# ============================================================
#  【修复】修改 Features、删除并重新插入 CPU / Clock
# ============================================================
apply_anti_eac_mods() {
    print_message "=== [功能6] 应用 anti-EAC 伪装（Features / CPU / Clock） ===" "$BLUE"
    local xml_file="$1"

    TOPOLOGY=$(grep '<topology' "$xml_file" | head -1)

    CPU_VENDOR=$(grep -m1 "vendor_id" /proc/cpuinfo | awk '{print $3}')
    if [[ "$CPU_VENDOR" == "AuthenticAMD" ]]; then
        DISABLE_FEATURE='<feature policy="disable" name="svm"/>'
    else
        DISABLE_FEATURE='<feature policy="disable" name="vmx"/>'
    fi

    NEW_FEATURES='<features>
  <acpi/>
  <apic/>
    <hyperv mode="custom">\
      <relaxed state="off"/>\
      <vapic state="off"/>\
      <spinlocks state="off"/>\
      <vpindex state="off"/>\
      <runtime state="off"/>\
      <synic state="off"/>\
      <stimer state="off"/>\
      <reset state="off"/>\
      <vendor_id state="off"/>\
      <frequencies state="off"/>\
      <reenlightenment state="off"/>\
      <tlbflush state="off"/>\
      <ipi state="off"/>\
      <evmcs state="off"/>\
      <avic state="off"/>\
    </hyperv>\
    <kvm>\
      <hidden state="on"/>\
    </kvm>\
    <pmu state="on"/>\
    <vmport state="off"/>\
    <smm state="on"/>\
    <ioapic driver="kvm"/>\
    <msrs unknown="fault"/>\
</features>'

    NEW_CPU="<cpu mode='host-passthrough' check='none' migratable='off'>
  ${TOPOLOGY}
  <cache mode='passthrough'/>
  ${DISABLE_FEATURE}
  <feature policy='require' name='topoext'/>
  <feature policy='require' name='invtsc'/>
  <feature policy='disable' name='vmx-vnmi'/>
  <feature policy='disable' name='hypervisor'/>
  <feature policy='disable' name='ssbd'/>
  <feature policy='disable' name='virt-ssbd'/>
  <feature policy='disable' name='rdtscp'/>
  <feature policy='disable' name='rdpid'/>
</cpu>"

    NEW_CLOCK="<clock offset='localtime'>
  <timer name='tsc' present='yes' tickpolicy='discard' mode='native'/>
  <timer name='hpet' present='yes'/>
  <timer name='rtc' present='no'/>
  <timer name='pit' present='no'/>
  <timer name='kvmclock' present='no'/>
  <timer name='hypervclock' present='no'/>
</clock>"

    xmlstarlet ed -L -d "/domain/features" "$xml_file" 2>/dev/null
    xmlstarlet ed -L -d "/domain/cpu" "$xml_file" 2>/dev/null
    xmlstarlet ed -L -d "/domain/clock" "$xml_file" 2>/dev/null

    echo "$NEW_FEATURES" > /tmp/features_insert.txt
    ex -s "$xml_file" <<EOF
/<uuid>/a
.
.r /tmp/features_insert.txt
.
wq
EOF
    rm -f /tmp/features_insert.txt

    {
        echo "$NEW_CPU"
        echo "$NEW_CLOCK"
    } > /tmp/cpu_clock_insert.txt
    ex -s "$xml_file" <<EOF
/<devices>/i
.
.r /tmp/cpu_clock_insert.txt
.
wq
EOF
    rm -f /tmp/cpu_clock_insert.txt

    print_message "✅ anti-EAC 伪装（Features / CPU / Clock）已应用" "$GREEN"
}

# ============================================================
#  功能7：注入 qemu:override 伪装硬盘属性
# ============================================================
apply_qemu_override() {
    local xml_file="$1"
    print_message "=== [功能7] 添加 qemu:override 硬盘属性伪装 ===" "$BLUE"

    sed -i '/<qemu:override>/,/<\/qemu:override>/d' "$xml_file"

    local disk_count=$(xmlstarlet sel -t -v "count(/domain/devices/disk)" "$xml_file" 2>/dev/null)
    if [[ -z "$disk_count" || "$disk_count" -eq 0 ]]; then
        print_message "无磁盘设备，跳过 qemu:override。" "$YELLOW"
        return 0
    fi

    local override_block="<qemu:override>"
    local sata_index=0 virtio_index=0 ide_index=0

    for ((i=1; i<=disk_count; i++)); do
        local bus=$(xmlstarlet sel -t -v "/domain/devices/disk[$i]/target/@bus" "$xml_file" 2>/dev/null)
        local alias=""
        case "$bus" in
            "sata")
                alias="sata0-0-${sata_index}"
                ((sata_index++))
                ;;
            "virtio")
                alias="virtio-disk${virtio_index}"
                ((virtio_index++))
                ;;
            "ide")
                alias="ide0-0-${ide_index}"
                ((ide_index++))
                ;;
            *)
                print_message "未知总线 '$bus'，跳过磁盘 $i" "$YELLOW"
                continue
                ;;
        esac

        override_block+=$'\n  <qemu:device alias="'"$alias"'">'
        override_block+=$'\n    <qemu:frontend>'
        override_block+=$'\n      <qemu:property name="rotation_rate" type="unsigned" value="1"/>'
        override_block+=$'\n      <qemu:property name="discard_granularity" type="unsigned" value="0"/>'
        override_block+=$'\n    </qemu:frontend>'
        override_block+=$'\n  </qemu:device>'
    done
    override_block+=$'\n</qemu:override>'

    echo "$override_block" > /tmp/override_insert.txt
    ex -s "$xml_file" <<EOF
/<\/devices>/a
.
.r /tmp/override_insert.txt
.
wq
EOF
    rm -f /tmp/override_insert.txt

    print_message "✅ qemu:override 已添加，共 $disk_count 个磁盘" "$GREEN"
}

# ============================================================
#  主流程
# ============================================================
if run_virsh list --all --name | grep -qx "win10"; then
    VM_NAME="win10"
elif run_virsh list --all --name | grep -qx "win11"; then
    VM_NAME="win11"
else
    print_message "未找到 win10 或 win11 虚拟机，请检查名称。" "$RED"
    exit 1
fi

TMP_XML="$RUNTIME_ROOT/${VM_NAME}_selected.xml"

print_message "目标虚拟机: $VM_NAME" "$GREEN"

check_xmlstarlet

run_virsh dumpxml "$VM_NAME" > "$TMP_XML"
print_message "虚拟机 XML 已导出" "$GREEN"

fix_virtual_network_mac
fix_vm_interface_mac "$TMP_XML"
validate_xml "$TMP_XML" "修改网卡 MAC"
apply_hdd_serial "$TMP_XML"
validate_xml "$TMP_XML" "修改硬盘序列号"
set_qemu_emulator "$TMP_XML"
validate_xml "$TMP_XML" "设置自编译 QEMU"
apply_ovmf_firmware "$TMP_XML"
validate_xml "$TMP_XML" "设置 OVMF 固件"
inject_qemu_commandline "$TMP_XML"
validate_xml "$TMP_XML" "注入 QEMU 命令行参数"
apply_memballoon_pm "$TMP_XML"
validate_xml "$TMP_XML" "修改 PM 配置"
apply_anti_eac_mods "$TMP_XML"
validate_xml "$TMP_XML" "应用 anti-EAC 伪装"
apply_qemu_override "$TMP_XML"
validate_xml "$TMP_XML" "添加 qemu:override"

if run_virsh define "$TMP_XML"; then
    print_message "✅ 虚拟机 XML 已重新定义并写入 libvirt。" "$GREEN"
else
    print_message "❌ virsh define 失败，前面的 XML 修改没有写入虚拟机。" "$RED"
    print_message "已保留临时 XML 供排查: $TMP_XML" "$YELLOW"
    exit 1
fi

verify_emulator=$(run_virsh dumpxml "$VM_NAME" | xmlstarlet sel -t -v "/domain/devices/emulator" 2>/dev/null || true)
if [[ "$verify_emulator" == "$QEMU_EMULATOR" ]]; then
    print_message "libvirt QEMU: $verify_emulator" "$GREEN"
else
    print_message "QEMU emulator path mismatch: ${verify_emulator:-unset}" "$RED"
    exit 1
fi

verify_loader=$(run_virsh dumpxml "$VM_NAME" | xmlstarlet sel -t -v "/domain/os/loader" 2>/dev/null || true)
if [[ -n "${OVMF_CODE_PATH:-}" && "$verify_loader" == "$OVMF_CODE_PATH" ]]; then
    print_message "libvirt OVMF CODE: $verify_loader" "$GREEN"
elif [[ -n "${OVMF_CODE_PATH:-}" ]]; then
    print_message "OVMF loader path mismatch: ${verify_loader:-unset}" "$RED"
    exit 1
fi

verify_serials=$(run_virsh dumpxml "$VM_NAME" | xmlstarlet sel -t -m "/domain/devices/disk[device='disk']" -v "concat(position(), ': ', serial)" -n 2>/dev/null || true)
if [[ -n "$verify_serials" ]]; then
    print_message "当前 libvirt 中的磁盘序列号:" "$BLUE"
    echo "$verify_serials"
else
    print_message "⚠️ 未能从 libvirt 重新读取到磁盘序列号，请手动检查 virsh dumpxml $VM_NAME。" "$YELLOW"
fi

rm -f "$TMP_XML"

echo ""
print_message "=== 所有修改已完成 ===" "$GREEN"
print_message "虚拟机: $VM_NAME" "$GREEN"
print_message "配置已生效（需要重启虚拟机才能完全应用某些改动）" "$YELLOW"
echo ""
print_message "修改摘要：" "$BLUE"
echo "  ✓ 虚拟网络 MAC 地址已更新"
echo "  ✓ 虚拟机网卡 MAC 地址已更新"
echo "  ✓ 硬盘序列号已随机化"
echo "  ✓ QEMU 命令行参数已注入（含 ACPI 表）"
echo "  ✓ 虚拟设备已去除"
echo "  ✓ anti-EAC 伪装已应用"
echo "  ✓ 硬盘属性伪装已添加"
echo ""
print_message "提示：请重启虚拟机使配置生效" "$YELLOW"
echo ""
read -p "按回车键返回主菜单..."
