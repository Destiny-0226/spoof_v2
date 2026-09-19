#!/usr/bin/env bash
# ============================================================
# libvirt qemu hook 一键管理脚本（NVIDIA/AMD 通用版）
# ============================================================

# ===== 使用传入的 SCRIPT_DIR（由 Spoof.cpp 设置，已废弃） =====
if [ -n "${SCRIPT_DIR:-}" ] && [ -d "$SCRIPT_DIR/hooks/n" ]; then
    SCRIPT_DIR="$SCRIPT_DIR"
else
    SCRIPT_DIR=""
fi

RUNTIME_ROOT="${KVMSPOOF_RUNTIME_DIR:-/tmp/kvmspoof-runtime-standalone}"
mkdir -p "$RUNTIME_ROOT"
KVMSPOOF_STATE_FILE="${KVMSPOOF_STATE_FILE:-$RUNTIME_ROOT/kvmspoof_state.env}"
export KVMSPOOF_RUNTIME_DIR="$RUNTIME_ROOT"
export KVMSPOOF_STATE_FILE

HOOK_FILE="/etc/libvirt/hooks/qemu"
HOOK_DISABLED="/etc/libvirt/hooks/qemu.disabled"

G='\033[1;32m'
Y='\033[1;33m'
R='\033[1;31m'
B='\033[1;34m'
N='\033[0m'

run_virsh() {
    virsh "$@" 2>/dev/null || sudo virsh "$@"
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
# 固定策略：虚拟机 release/关机时，宿主机也自动关机
# ============================================================

detect_vm_name() {
    if run_virsh list --all --name 2>/dev/null | grep -qx "win10"; then
        echo "win10"
    elif run_virsh list --all --name 2>/dev/null | grep -qx "win11"; then
        echo "win11"
    else
        echo ""
    fi
}

VM_NAME=$(detect_vm_name)

detect_status() {
    if [[ -f "$HOOK_FILE" ]]; then
        echo "enabled"
    elif [[ -f "$HOOK_DISABLED" ]]; then
        echo "disabled"
    else
        echo "missing"
    fi
}

print_status() {
    local st
    st=$(detect_status)
    case "$st" in
        enabled)  echo -e "  当前状态: ${G}已启用${N}（VM 启动会触发通用 VFIO hook）" ;;
        disabled) echo -e "  当前状态: ${Y}已禁用${N}（VM 启动不会触发 hook）" ;;
        missing)  echo -e "  当前状态: ${R}未找到 hook 文件${N}（请先执行 [1] 部署 hook 环境）" ;;
    esac
}

service_exists() {
    systemctl list-unit-files "$1" >/dev/null 2>&1
}

try_restart_service() {
    local unit="$1"
    if service_exists "$unit"; then
        sudo systemctl restart "$unit" 2>/dev/null || true
    fi
}

restart_services() {
    try_restart_service virtlogd.socket
    try_restart_service libvirtd.service
    try_restart_service virtqemud.service
    try_restart_service virtqemud.socket
    try_restart_service virtnetworkd.service
    try_restart_service virtnetworkd.socket
    sleep 2
}

require_virt_xml() {
    if ! command -v virt-xml >/dev/null 2>&1; then
        echo -e "${R}[ERR]${N} 未找到 virt-xml"
        echo "  Ubuntu/Debian: sudo apt install virtinst"
        echo "  Arch/Manjaro:  sudo pacman -S virt-install"
        return 1
    fi
    return 0
}

pause_menu() {
    echo
    read -rp "按 Enter 返回主菜单..."
}

# ============================================================
# 禁用/恢复 hook
# ============================================================
disable_hook() {
    local st
    st=$(detect_status)
    case "$st" in
        disabled)
            echo -e "${Y}[SKIP]${N} hook 已经处于禁用状态，无需操作"
            pause_menu
            return 0
            ;;
        missing)
            echo -e "${R}[ERR]${N} hook 文件不存在，请先执行 [1] 部署 hook 环境"
            pause_menu
            return 1
            ;;
    esac

    echo -e "${B}[INFO]${N} 禁用 hook：$HOOK_FILE  →  $HOOK_DISABLED"
    sudo mv "$HOOK_FILE" "$HOOK_DISABLED" || { echo -e "${R}[ERR]${N} 重命名失败"; pause_menu; return 1; }
    restart_services
    echo -e "${G}=== hook 已禁用 ===${N}"
    echo "  VM 启动时不会触发通用 VFIO hook。"
    pause_menu
}

enable_hook() {
    local st
    st=$(detect_status)
    case "$st" in
        enabled)
            echo -e "${Y}[SKIP]${N} hook 已经处于启用状态，无需操作"
            pause_menu
            return 0
            ;;
        missing)
            echo -e "${R}[ERR]${N} hook 文件不存在，无法恢复"
            echo "  请先执行 [1] 部署 hook 环境"
            pause_menu
            return 1
            ;;
    esac

    echo -e "${B}[INFO]${N} 恢复 hook：$HOOK_DISABLED  →  $HOOK_FILE"
    sudo mv "$HOOK_DISABLED" "$HOOK_FILE" || { echo -e "${R}[ERR]${N} 重命名失败"; pause_menu; return 1; }
    sudo chmod +x "$HOOK_FILE" 2>/dev/null
    restart_services
    echo -e "${G}=== hook 已恢复 ===${N}"
    echo "  关机策略保持：虚拟机关闭 → 宿主机自动关机"
    pause_menu
}

# ============================================================
# 虚拟显卡调试/恢复
# ============================================================
_ensure_vm_shutoff() {
    if [[ -z "$VM_NAME" ]]; then
        echo -e "${R}[ERR]${N} 未检测到 win10/win11 虚拟机"
        return 1
    fi
    if ! sudo virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
        echo -e "${R}[ERR]${N} 找不到虚拟机 '$VM_NAME'，请先 define"
        return 1
    fi
    local state
    state=$(sudo virsh domstate "$VM_NAME" 2>/dev/null)
    if [[ "$state" != "shut off" && "$state" != "关闭" ]]; then
        echo -e "${R}[ERR]${N} 虚拟机 '$VM_NAME' 当前状态：$state"
        echo "  请先关机：sudo virsh shutdown $VM_NAME"
        echo "  强制关机：sudo virsh destroy $VM_NAME"
        return 1
    fi
    return 0
}

_has_virtual_display() {
    sudo virsh dumpxml "$VM_NAME" 2>/dev/null | grep -qE '<graphics[[:space:]]+type=|<video[>[:space:]]'
}

add_virtual_display() {
    echo -e "${B}=== 添加虚拟显卡（VGA + VNC + USB tablet） ===${N}"
    echo

    if ! _ensure_vm_shutoff; then
        pause_menu
        return 1
    fi
    if ! require_virt_xml; then
        pause_menu
        return 1
    fi

    local backup_dir="/tmp/spoof"
    mkdir -p "$backup_dir" || { echo -e "${R}[ERR]${N} 无法创建备份目录"; pause_menu; return 1; }
    local backup_file="$backup_dir/${VM_NAME}_passthrough_backup.xml"
    if ! sudo virsh dumpxml "$VM_NAME" > "$backup_file" 2>/dev/null || [[ ! -s "$backup_file" ]]; then
        echo -e "${R}[ERR]${N} 备份直通配置失败"
        pause_menu
        return 1
    fi

    if _has_virtual_display; then
        echo -e "${Y}[SKIP]${N} 虚拟机 '$VM_NAME' 已有虚拟显示设备，跳过添加"
        echo "  直通配置已备份: $backup_file"
        pause_menu
        return 0
    fi

    local tmp_xml="$RUNTIME_ROOT/${VM_NAME}_display_$$.xml"
    sudo virsh dumpxml "$VM_NAME" > "$tmp_xml" || { echo -e "${R}[ERR]${N} 导出 XML 失败"; pause_menu; return 1; }

    sed -i '/<hostdev/,/<\/hostdev>/d' "$tmp_xml"
    sed -i '/<hostdev[[:space:]][^>]*\/>/d' "$tmp_xml"
    sed -i -E '/<graphics[[:space:]][^>]*\/>/d' "$tmp_xml"
    sed -i -E '/<graphics([[:space:]]|>)/,/<\/graphics>/d' "$tmp_xml"
    sed -i -E '/<video[[:space:]][^>]*\/>/d' "$tmp_xml"
    sed -i -E '/<video([[:space:]]|>)/,/<\/video>/d' "$tmp_xml"
    sed -i -E "/<input type=['\"]tablet['\"][^>]*\/>/d" "$tmp_xml"

    if ! sudo virsh define "$tmp_xml" >/dev/null 2>&1; then
        echo -e "${R}[ERR]${N} 清理直通/显示设备失败"
        rm -f "$tmp_xml"
        pause_menu
        return 1
    fi
    rm -f "$tmp_xml"

    sudo virt-xml "$VM_NAME" --add-device --graphics 'vnc,port=-1,listen=0.0.0.0' 2>/dev/null || {
        echo -e "${R}[ERR]${N} 添加 VNC 失败"
        pause_menu
        return 1
    }

    if sudo virt-xml "$VM_NAME" --add-device --video model=vga,vram=16384,heads=1 2>/dev/null; then
        echo "✅ 成功添加 VGA"
    else
        echo "⚠️ 直接添加 VGA 失败，尝试添加默认视频后修改..."
        sudo virt-xml "$VM_NAME" --add-device --video 2>/dev/null || true
        sudo virt-xml "$VM_NAME" --edit --video model=vga,vram=16384,heads=1 2>/dev/null || true
    fi

    if ! sudo virsh dumpxml "$VM_NAME" 2>/dev/null | grep -q "<input type=['\"]tablet['\"]"; then
        sudo virt-xml "$VM_NAME" --add-device --input 'tablet,bus=usb' 2>/dev/null || true
    fi

    echo
    echo -e "${G}=== 操作完成 ===${N}"
    echo "  虚拟机: $VM_NAME"
    echo "  已添加虚拟显卡（VGA + VNC）"
    echo "  直通配置已备份: $backup_file"
    pause_menu
}

remove_virtual_display() {
    echo -e "${B}=== 删除虚拟显卡（恢复直通配置） ===${N}"
    echo

    if ! _ensure_vm_shutoff; then
        pause_menu
        return 1
    fi

    local backup_file="/tmp/spoof/${VM_NAME}_passthrough_backup.xml"
    if [[ ! -f "$backup_file" ]]; then
        echo -e "${R}[ERR]${N} 未找到直通备份文件: $backup_file"
        echo "  请手动恢复直通配置，或重新添加 PCI 直通设备。"
        pause_menu
        return 1
    fi

    if sudo virsh define "$backup_file" >/dev/null 2>&1; then
        echo -e "${G}=== 操作完成 ===${N}"
        echo "  直通配置已恢复，请启动 hook 后再启动虚拟机"
    else
        echo -e "${R}[ERR]${N} 恢复失败，请手动检查: virsh define $backup_file"
    fi

    pause_menu
}

# ============================================================
# 部署通用 hook 环境
# ============================================================
deploy_hook_env() {
    echo -e "${B}=== 部署 libvirt qemu hook 环境（NVIDIA/AMD 通用） ===${N}"
    echo

    sudo mkdir -p /etc/libvirt/hooks

    if [[ -f "$HOOK_FILE" ]]; then
        echo -e "${Y}[WARN]${N} 发现已存在的 hook 文件: $HOOK_FILE"
        read_menu_choice "是否备份并覆盖？ (y/N): "
        confirm="$MENU_CHOICE"
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            echo -e "${Y}[SKIP]${N} 取消部署"
            pause_menu
            return 0
        fi
        sudo mv "$HOOK_FILE" "${HOOK_FILE}.backup_$(date +%Y%m%d_%H%M%S)"
    fi
    if [[ -f "$HOOK_DISABLED" ]]; then
        sudo rm -f "$HOOK_DISABLED"
    fi

    sudo tee /bin/vfio-startup.sh > /dev/null << 'EOF'
#!/bin/bash

DATE=$(date +"%m/%d/%Y %R:%S :")
RUNTIME_ROOT="/tmp/kvmspoof-runtime"
mkdir -p "$RUNTIME_ROOT"

echo "$DATE Beginning of universal VFIO startup!"

stop_display_manager_if_running() {
    local dispmgr=""

    if command -v systemctl >/dev/null 2>&1; then
        if pgrep -l "plasma" | grep -q "plasmashell"; then
            dispmgr="display-manager"
        elif [ -f /etc/systemd/system/display-manager.service ]; then
            dispmgr=$(grep 'ExecStart=' /etc/systemd/system/display-manager.service | awk -F'/' '{print $NF}' | awk '{print $1}')
        else
            dispmgr="display-manager"
        fi

        if systemctl is-active --quiet "$dispmgr.service"; then
            echo "$dispmgr" > "$RUNTIME_ROOT/vfio-store-display-manager"
            echo "$DATE Stopping display manager: $dispmgr"
            systemctl stop "$dispmgr.service" 2>/dev/null || true
            systemctl isolate multi-user.target 2>/dev/null || true
        elif systemctl is-active --quiet display-manager.service; then
            echo "display-manager" > "$RUNTIME_ROOT/vfio-store-display-manager"
            echo "$DATE Stopping display-manager.service"
            systemctl stop display-manager.service 2>/dev/null || true
            systemctl isolate multi-user.target 2>/dev/null || true
        fi
    fi
}

unbind_vtconsoles() {
    rm -f "$RUNTIME_ROOT/vfio-bound-consoles"
    for ((i = 0; i < 16; i++)); do
        if [ -e "/sys/class/vtconsole/vtcon${i}/bind" ] && grep -q "frame buffer" "/sys/class/vtconsole/vtcon${i}/name" 2>/dev/null; then
            echo 0 > "/sys/class/vtconsole/vtcon${i}/bind" 2>/dev/null || true
            echo "$i" >> "$RUNTIME_ROOT/vfio-bound-consoles"
            echo "$DATE Unbinding console $i"
        fi
    done
}

unload_gpu_drivers() {
    if lspci -nn | grep -iE 'VGA|3D|Display' | grep -qi nvidia; then
        echo "$DATE NVIDIA GPU detected, unloading NVIDIA drivers"
        modprobe -r nvidia_uvm 2>/dev/null || true
        modprobe -r nvidia_drm 2>/dev/null || true
        modprobe -r nvidia_modeset 2>/dev/null || true
        modprobe -r nvidia 2>/dev/null || true
        modprobe -r i2c_nvidia_gpu 2>/dev/null || true
    fi

    if lspci -nn | grep -iE 'VGA|3D|Display' | grep -qiE 'amd|ati|advanced micro devices'; then
        echo "$DATE AMD GPU detected, unloading AMD drivers"
        modprobe -r amdgpu 2>/dev/null || true
        modprobe -r radeon 2>/dev/null || true
    fi

    modprobe -r drm_kms_helper 2>/dev/null || true
    modprobe -r drm 2>/dev/null || true
}

stop_display_manager_if_running
sleep 1
unbind_vtconsoles
sleep 1
echo efi-framebuffer.0 > /sys/bus/platform/drivers/efi-framebuffer/unbind 2>/dev/null || true
unload_gpu_drivers

modprobe vfio 2>/dev/null || true
modprobe vfio_pci 2>/dev/null || true
modprobe vfio_iommu_type1 2>/dev/null || true

echo "$DATE End of universal VFIO startup!"
EOF
    sudo chmod +x /bin/vfio-startup.sh

    sudo tee /bin/vfio-teardown.sh > /dev/null << 'EOF'
#!/bin/bash

DATE=$(date +"%m/%d/%Y %R:%S :")
RUNTIME_ROOT="/tmp/kvmspoof-runtime"
mkdir -p "$RUNTIME_ROOT"

echo "$DATE Beginning of universal VFIO teardown!"

echo "$DATE 虚拟机已关闭，宿主机将在 3 秒后关机..."
sleep 3
systemctl poweroff || poweroff

echo "$DATE End of universal VFIO teardown!"
EOF
    sudo chmod +x /bin/vfio-teardown.sh

    sudo tee /etc/libvirt/hooks/qemu > /dev/null << 'EOF'
#!/bin/bash

OBJECT="$1"
OPERATION="$2"

if [[ $OBJECT == "win10" || $OBJECT == "win11" ]]; then
    case "$OPERATION" in
        "prepare")
            systemctl start libvirt-nosleep@"$OBJECT" 2>&1 | tee -a /var/log/libvirt/custom_hooks.log
            /bin/vfio-startup.sh 2>&1 | tee -a /var/log/libvirt/custom_hooks.log
            ;;
        "release")
            systemctl stop libvirt-nosleep@"$OBJECT" 2>&1 | tee -a /var/log/libvirt/custom_hooks.log
            /bin/vfio-teardown.sh 2>&1 | tee -a /var/log/libvirt/custom_hooks.log
            ;;
    esac
fi
EOF
    sudo chmod +x /etc/libvirt/hooks/qemu

    if [ -f "$SCRIPT_DIR/systemd-no-sleep/libvirt-nosleep@.service" ]; then
        sudo cp "$SCRIPT_DIR/systemd-no-sleep/libvirt-nosleep@.service" /etc/systemd/system/
        sudo systemctl daemon-reload
    fi

    restart_services

    echo
    echo -e "${G}=== hook 环境部署完成 ===${N}"
    echo "  脚本类型: NVIDIA/AMD 通用 VFIO hook"
    echo "  关机策略: 虚拟机关闭 → 宿主机自动关机"
    pause_menu
}

main() {
    while true; do
        clear
        echo -e "${B}=== libvirt qemu hook 管理（NVIDIA/AMD 通用） ===${N}"
        echo
        print_status
        echo -e "  关机策略: ${G}虚拟机关闭 → 宿主机自动关机${N}"
        echo
        echo "  [1] 部署 hook 环境（NVIDIA/AMD 通用）"
        echo
        echo "  [2] 禁用 hook（临时关闭直通）"
        echo
        echo "  [3] 恢复 hook（开启直通模式）"
        echo
        echo "  [4] 添加虚拟显卡（VGA画面调试）"
        echo
        echo "  [5] 删除虚拟显卡（恢复直通配置）"
        echo
        echo "  [0] 退出"
        echo
        read_menu_choice "请选择: "
        choice="$MENU_CHOICE"
        echo
        case "$choice" in
            1) deploy_hook_env ;;
            2) disable_hook ;;
            3) enable_hook ;;
            4) add_virtual_display ;;
            5) remove_virtual_display ;;
            0) echo "已退出"; exit 0 ;;
            *) echo -e "${R}[ERR]${N} 无效选择: $choice"; sleep 1 ;;
        esac
    done
}

main
