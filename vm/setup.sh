#!/bin/bash

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印颜色消息的函数
print_message() {
    echo -e "${2}${1}${NC}"
}

read_menu_choice() {
    local prompt="$1"
    MENU_CHOICE=""
    printf "%s" "$prompt"
    IFS= read -r -s -n 1 MENU_CHOICE
    while IFS= read -r -s -n 1 -t 0.01 _discard; do :; done
    printf "%s\n" "$MENU_CHOICE"
}

# 检查是否为root用户（已禁用，允许root和普通用户运行）
check_root() {
    if [[ $EUID -eq 0 ]]; then
        print_message "警告：当前以root用户运行，某些命令可能表现不同" "$YELLOW"
        # 不再退出，允许继续执行
    fi
}

# 检查命令是否存在
check_command() {
    if ! command -v "$1" &> /dev/null; then
        print_message "错误：未找到 $1 命令" "$RED"
        exit 1
    fi
}

get_target_user() {
    if [[ -n "${KVMSPOOF_TARGET_USER:-}" ]]; then
        echo "$KVMSPOOF_TARGET_USER"
    elif [[ ${EUID:-$(id -u)} -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        echo "$SUDO_USER"
    else
        id -un
    fi
}

get_distro_id() {
    if [[ -r /etc/os-release ]]; then
        . /etc/os-release
        echo "${ID,,}"
    else
        echo "unknown"
    fi
}

DISTRO_ID=$(get_distro_id)

is_arch_like() {
    case "$DISTRO_ID" in
        arch|endeavouros|manjaro) return 0 ;;
        *) return 1 ;;
    esac
}

is_debian_like() {
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop) return 0 ;;
        *) return 1 ;;
    esac
}

service_exists() {
    systemctl list-unit-files "$1" >/dev/null 2>&1
}

try_enable_service() {
    local unit="$1"
    if service_exists "$unit"; then
        sudo systemctl enable --now "$unit" 2>/dev/null || sudo systemctl restart "$unit" 2>/dev/null || true
        return 0
    fi
    return 1
}

restart_libvirt_services() {
    try_enable_service virtlogd.socket || true
    try_enable_service libvirtd.service || true
    try_enable_service virtqemud.service || true
    try_enable_service virtqemud.socket || true
    try_enable_service virtnetworkd.service || true
    try_enable_service virtnetworkd.socket || true
}

update_bootloader() {
    if is_arch_like; then
        sudo grub-mkconfig -o /boot/grub/grub.cfg
    elif is_debian_like; then
        sudo update-grub
    else
        print_message "未知发行版 $DISTRO_ID，请手动更新 GRUB。" "$YELLOW"
    fi
}

disable_apparmor_if_present() {
    if service_exists apparmor.service; then
        sudo systemctl stop apparmor 2>/dev/null || true
        sudo systemctl disable apparmor 2>/dev/null || true
    fi
}

# 检查文件是否包含特定内容
contains_line() {
    local file="$1"
    local line="$2"
    
    grep -qFx "$line" "$file" 2>/dev/null
    return $?
}

# 添加内容到文件（如果不存在）
append_if_not_exists() {
    local file="$1"
    local line="$2"
    local description="$3"
    
    if contains_line "$file" "$line"; then
        print_message "✓ 已存在: $description" "$GREEN"
        return 1
    else
        echo "$line" | sudo tee -a "$file" > /dev/null
        print_message "✓ 已添加: $description" "$YELLOW"
        return 0
    fi
}

# 步骤1: 配置用户组和libvirt服务（不再重复安装软件包）
step1_configure_user_and_service() {
    print_message "\n=== 步骤1: 配置用户组和libvirt服务 ===" "$BLUE"
    
    print_message "将当前用户添加到libvirt和kvm组..." "$YELLOW"
    local TARGET_USER
    TARGET_USER="$(get_target_user)"
    sudo usermod -aG libvirt "$TARGET_USER"
    sudo usermod -aG kvm "$TARGET_USER"
    
    print_message "重启 libvirt 服务..." "$YELLOW"
    restart_libvirt_services

    print_message "步骤1完成！" "$GREEN"
}

# 步骤2: 配置/etc/modules
step2_configure_modules() {
    print_message "\n=== 步骤2: 配置/etc/modules文件 ===" "$BLUE"
    
    if [[ -f /etc/modules ]]; then
        if [[ ! -f /etc/modules.backup ]]; then
            sudo cp /etc/modules /etc/modules.backup
            print_message "已创建备份: /etc/modules.backup" "$GREEN"
        fi

        print_message "正在配置 /etc/modules 文件..." "$YELLOW"

        if ! grep -q "^# 添加的VFIO配置" /etc/modules; then
            echo -e "\n# 添加的VFIO配置" | sudo tee -a /etc/modules > /dev/null
        fi

        append_if_not_exists "/etc/modules" "softdep snd_hda_intel pre:vfio vfio_pci" "snd_hda_intel softdep"
        append_if_not_exists "/etc/modules" "softdep amdgpu pre:vfio vfio_pci" "amdgpu softdep"
        append_if_not_exists "/etc/modules" "vfio" "vfio模块"
        append_if_not_exists "/etc/modules" "vfio_iommu_type1" "vfio_iommu_type1模块"
        append_if_not_exists "/etc/modules" "vfio_virqfd" "vfio_virqfd模块"
    fi

    print_message "正在配置 /etc/modules-load.d/vfio.conf 文件..." "$YELLOW"
    printf '%s\n' vfio vfio_pci vfio_iommu_type1 vfio_virqfd | sudo tee /etc/modules-load.d/vfio.conf > /dev/null

    print_message "步骤2完成！" "$GREEN"
}

# 步骤3: 配置blacklist.conf
step3_configure_blacklist() {
    print_message "\n=== 步骤3: 配置显卡驱动黑名单 ===" "$BLUE"
    
    print_message "请选择您的显卡类型：" "$YELLOW"
    echo "1) NVIDIA显卡"
    echo "2) AMD显卡"
    echo "3) 跳过此步骤"
    
    read_menu_choice "请输入选择 (1/2/3): "
    gpu_choice="$MENU_CHOICE"
    
    if [[ ! -f /etc/modprobe.d/blacklist.conf.backup ]]; then
        sudo cp /etc/modprobe.d/blacklist.conf /etc/modprobe.d/blacklist.conf.backup
        print_message "已创建备份: /etc/modprobe.d/blacklist.conf.backup" "$GREEN"
    fi
    
    case $gpu_choice in
        1)
            print_message "正在为NVIDIA显卡配置黑名单..." "$YELLOW"
            if ! grep -q "^# 屏蔽NVIDIA驱动以用于VFIO直通" /etc/modprobe.d/blacklist.conf; then
                echo -e "\n# 屏蔽NVIDIA驱动以用于VFIO直通" | sudo tee -a /etc/modprobe.d/blacklist.conf > /dev/null
            fi
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist nvidia" "NVIDIA驱动黑名单"
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist nouveau" "Nouveau驱动黑名单"
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist snd_hda_intel" "声卡驱动黑名单"
            GPU_TYPE="nvidia"
            ;;
        2)
            print_message "正在为AMD显卡配置黑名单..." "$YELLOW"
            if ! grep -q "^# 屏蔽AMD驱动以用于VFIO直通" /etc/modprobe.d/blacklist.conf; then
                echo -e "\n# 屏蔽AMD驱动以用于VFIO直通" | sudo tee -a /etc/modprobe.d/blacklist.conf > /dev/null
            fi
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist amdgpu" "AMDGPU驱动黑名单"
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist pcieport" "PCIe端口黑名单"
            append_if_not_exists "/etc/modprobe.d/blacklist.conf" "blacklist snd_hda_intel" "声卡驱动黑名单"
            GPU_TYPE="amd"
            ;;
        3)
            print_message "跳过黑名单配置..." "$YELLOW"
            return
            ;;
        *)
            print_message "无效选择，跳过黑名单配置..." "$RED"
            return
            ;;
    esac
    
    print_message "步骤3完成！" "$GREEN"
}

# 步骤4: 自动获取显卡ID和相关设备
step4_get_gpu_ids() {
    print_message "\n=== 步骤4: 自动获取显卡设备ID ===" "$BLUE"
    
    print_message "正在扫描PCI设备..." "$YELLOW"
    echo ""
    
    VGA_DEVICES=()
    while IFS= read -r line; do
        VGA_DEVICES+=("$line")
    done < <(lspci -nn | grep -i "vga compatible controller")
    
    if [[ ${#VGA_DEVICES[@]} -eq 0 ]]; then
        print_message "未找到VGA兼容设备，尝试其他显卡类型..." "$YELLOW"
        while IFS= read -r line; do
            VGA_DEVICES+=("$line")
        done < <(lspci -nn | grep -i "3d controller\|display controller")
    fi
    
    if [[ ${#VGA_DEVICES[@]} -eq 0 ]]; then
        print_message "未找到显卡设备" "$RED"
        GPU_ID=""
        return
    fi
    
    echo "找到以下显卡设备："
    for i in "${!VGA_DEVICES[@]}"; do
        echo "$((i+1)). ${VGA_DEVICES[$i]}"
    done
    
    if [[ ${#VGA_DEVICES[@]} -eq 1 ]]; then
        print_message "检测到1个显卡设备，自动选择..." "$GREEN"
        selected_vga="${VGA_DEVICES[0]}"
        selection=1
    else
        print_message "请选择要直通的显卡设备 (1-${#VGA_DEVICES[@]})：" "$YELLOW"
        if [[ ${#VGA_DEVICES[@]} -le 9 ]]; then
            read_menu_choice "输入选择: "
            selection="$MENU_CHOICE"
        else
            read -p "输入选择: " selection
        fi
        if ! [[ "$selection" =~ ^[0-9]+$ ]] || [[ "$selection" -lt 1 ]] || [[ "$selection" -gt ${#VGA_DEVICES[@]} ]]; then
            print_message "无效选择，使用默认设备1" "$YELLOW"
            selection=1
        fi
        selected_vga="${VGA_DEVICES[$((selection-1))]}"
    fi
    
    print_message "\n已选择显卡: ${selected_vga}" "$GREEN"
    
    bus_address=$(echo "$selected_vga" | grep -o '^[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]\.[0-9a-f]' | head -1)
    if [[ -z "$bus_address" ]]; then
        print_message "无法提取总线地址" "$RED"
        return
    fi
    bus_prefix=$(echo "$bus_address" | cut -d'.' -f1)
    
    vga_id=$(echo "$selected_vga" | grep -o '\[[0-9a-f][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f][0-9a-f][0-9a-f][0-9a-f]\]' | head -1 | tr -d '[]')
    if [[ -z "$vga_id" ]]; then
        print_message "无法提取设备ID" "$RED"
        return
    fi
    
    print_message "主VGA设备ID: $vga_id" "$GREEN"
    
    print_message "\n正在查找同一总线上的相关设备..." "$YELLOW"
    all_device_ids="$vga_id"
    related_count=0
    
    while IFS= read -r device_line; do
        if [[ "$device_line" == "$selected_vga" ]]; then
            continue
        fi
        if echo "$device_line" | grep -q "^${bus_prefix}\."; then
            related_count=$((related_count + 1))
            device_id=$(echo "$device_line" | grep -o '\[[0-9a-f][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f][0-9a-f][0-9a-f][0-9a-f]\]' | head -1 | tr -d '[]')
            if [[ -n "$device_id" ]]; then
                print_message "找到相关设备: ${device_line}" "$GREEN"
                all_device_ids="${all_device_ids},${device_id}"
            fi
        fi
    done < <(lspci -nn)
    
    if [[ $related_count -eq 0 ]]; then
        print_message "未找到同一总线的相关设备" "$YELLOW"
    else
        print_message "共找到 $related_count 个相关设备" "$GREEN"
    fi
    
    print_message "\n所有需要直通的设备ID: $all_device_ids" "$BLUE"
    
    print_message "\n是否使用以上设备ID进行配置？ (Y/n): " "$YELLOW"
    read_menu_choice ""
    confirm_choice="$MENU_CHOICE"
    if [[ "$confirm_choice" =~ ^[Nn]$ ]]; then
        print_message "请手动输入设备ID (用逗号分隔多个ID): " "$YELLOW"
        read -p "" manual_ids
        if [[ -n "$manual_ids" ]]; then
            all_device_ids="$manual_ids"
        else
            print_message "未输入设备ID，将跳过后续的GRUB配置" "$YELLOW"
            GPU_ID=""
            return
        fi
    fi
    
    GPU_ID="$all_device_ids"
    
    if echo "$selected_vga" | grep -qi "nvidia"; then
        GPU_TYPE="nvidia"
        print_message "检测到NVIDIA显卡" "$GREEN"
    elif echo "$selected_vga" | grep -qi "amd\|ati"; then
        GPU_TYPE="amd"
        print_message "检测到AMD显卡" "$GREEN"
    else
        print_message "请确认您的显卡类型：" "$YELLOW"
        echo "1) NVIDIA显卡"
        echo "2) AMD显卡"
        read_menu_choice "请输入选择 (1/2): "
        gpu_type_choice="$MENU_CHOICE"
        case $gpu_type_choice in
            1) GPU_TYPE="nvidia" ;;
            2) GPU_TYPE="amd" ;;
            *)
                print_message "无效选择，默认使用NVIDIA" "$YELLOW"
                GPU_TYPE="nvidia"
                ;;
        esac
    fi
    
    print_message "步骤4完成！" "$GREEN"
}

# 步骤5: 配置GRUB和KVM模块参数（按用户要求精简内核参数）
step5_configure_grub() {
    print_message "\n=== 步骤5: 配置GRUB引导参数和KVM模块参数 ===" "$BLUE"
    
    if [[ -z "$GPU_ID" ]]; then
        print_message "未检测到显卡ID，跳过GRUB配置" "$YELLOW"
        return
    fi
    
    # 询问CPU类型（仅用于KVM模块参数）
    print_message "请选择您的CPU类型：" "$YELLOW"
    echo "1) Intel CPU"
    echo "2) AMD CPU"
    read_menu_choice "请输入选择 (1/2): "
    cpu_choice="$MENU_CHOICE"
    if [[ "$cpu_choice" == "1" ]]; then
        CPU_TYPE="intel"
    elif [[ "$cpu_choice" == "2" ]]; then
        CPU_TYPE="amd"
    else
        print_message "无效选择，默认使用Intel" "$YELLOW"
        CPU_TYPE="intel"
    fi
    
    # ---------- 配置KVM模块参数 ----------
    KVM_CONF="/etc/modprobe.d/kvm.conf"
    if [[ "$CPU_TYPE" == "intel" ]]; then
        KVM_OPTIONS="options kvm_intel enable_apicv=1 nested=0"
    else
        KVM_OPTIONS="options kvm_amd avic=1 nested=0"
    fi
    
    if [[ -f "$KVM_CONF" ]]; then
        sudo cp "$KVM_CONF" "${KVM_CONF}.backup.$(date +%Y%m%d%H%M%S)"
        print_message "已备份 $KVM_CONF" "$GREEN"
    fi
    
    echo "$KVM_OPTIONS" | sudo tee "$KVM_CONF" > /dev/null
    print_message "已配置KVM模块参数: $KVM_OPTIONS" "$GREEN"
    
    # ---------- 配置GRUB ----------
    if [[ ! -f /etc/default/grub.backup ]]; then
        sudo cp /etc/default/grub /etc/default/grub.backup
        print_message "已创建备份: /etc/default/grub.backup" "$GREEN"
    fi
    
    print_message "正在配置GRUB参数..." "$YELLOW"
    
    CURRENT_CMDLINE=$(grep "^GRUB_CMDLINE_LINUX_DEFAULT=" /etc/default/grub | cut -d'"' -f2)
    
    if [[ -n "$CURRENT_CMDLINE" ]]; then
        if echo "$CURRENT_CMDLINE" | grep -q "vfio-pci.ids="; then
            NEW_CMDLINE=$(echo "$CURRENT_CMDLINE" | sed "s/vfio-pci.ids=[^ ]*/vfio-pci.ids=${GPU_ID}/g")
        else
            NEW_CMDLINE="${CURRENT_CMDLINE} vfio-pci.ids=${GPU_ID}"
        fi
    else
        NEW_CMDLINE=""
    fi
    
    # ========== 用户指定的内核参数（精简版） ==========
    REQUIRED_PARAMS="iommu=on pcie_aspm=off vfio_pci.disable_idle_d3=1 kvm.ignore_msrs=1"
    
    for param in $REQUIRED_PARAMS; do
        if ! echo "$NEW_CMDLINE" | grep -q "$param"; then
            NEW_CMDLINE="${NEW_CMDLINE} ${param}"
        fi
    done
    
    if ! echo "$NEW_CMDLINE" | grep -q "vfio-pci.ids="; then
        NEW_CMDLINE="${NEW_CMDLINE} vfio-pci.ids=${GPU_ID}"
    fi
    
    NEW_CMDLINE=$(echo "$NEW_CMDLINE" | sed 's/^[ \t]*//;s/[ \t]*$//;s/  */ /g')
    
    sudo sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"${NEW_CMDLINE}\"/" /etc/default/grub
    
    print_message "更新后的GRUB参数: $NEW_CMDLINE" "$GREEN"
    
    print_message "正在更新GRUB配置..." "$YELLOW"
    update_bootloader

    print_message "步骤5完成！" "$GREEN"
}

# 最终步骤
final_step() {
    print_message "\n=== 配置完成！ ===" "$GREEN"
    echo "重新启动计算机以应用所有更改"
 
    print_message "配置摘要：" "$YELLOW"
    echo "- /etc/modules 已配置（备份在 /etc/modules.backup）"
    echo "- 黑名单配置在 /etc/modprobe.d/blacklist.conf（备份在 blacklist.conf.backup）"
    echo "- GRUB配置在 /etc/default/grub（备份在 grub.backup）"
    echo "- KVM模块参数配置在 /etc/modprobe.d/kvm.conf（如有旧文件已备份）"
    echo ""
    print_message "重要提示：" "$RED"
    echo "- 如果您在步骤3中选择了屏蔽显卡驱动，重启后可能会无法使用图形界面"
    echo "- 请确保您有备用方案（如SSH、TTY或集成显卡）来访问系统"
    echo ""
    print_message "请记得手动重启以应用所有更改！" "$YELLOW"
}

# 主函数
main() {
    print_message "本脚本将帮助您配置显卡直通（仅配置部分，不安装软件包）" "$BLUE"
    print_message "请注意：此操作需要重启计算机" "$YELLOW"
    echo ""
    
    read_menu_choice "是否继续？ (y/N): "
    continue_choice="$MENU_CHOICE"
    if [[ ! "$continue_choice" =~ ^[Yy]$ ]]; then
        print_message "已取消操作" "$RED"
        exit 0
    fi
    
    check_root
    check_command "sudo"
    check_command "lspci"
    
    step1_configure_user_and_service
    step2_configure_modules
    step3_configure_blacklist
    step4_get_gpu_ids
    step5_configure_grub
    disable_apparmor_if_present
    final_step
}

main
