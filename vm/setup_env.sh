#!/bin/bash

# ============================================================
# 颜色定义
# ============================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

RUN_ALL=0

RUNTIME_ROOT="${KVMSPOOF_RUNTIME_DIR:-/tmp/kvmspoof-runtime-standalone}"
mkdir -p "$RUNTIME_ROOT"
KVMSPOOF_STATE_FILE="${KVMSPOOF_STATE_FILE:-$RUNTIME_ROOT/kvmspoof_state.env}"
export KVMSPOOF_RUNTIME_DIR="$RUNTIME_ROOT"
export KVMSPOOF_STATE_FILE

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

# ============================================================
# 检测系统发行版
# ============================================================
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
        arch|endeavouros|manjaro|arcolinux|garuda|artix) return 0 ;;
        *) return 1 ;;
    esac
}

is_debian_like() {
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop) return 0 ;;
        *) return 1 ;;
    esac
}

install_packages() {
    if is_arch_like; then
        sudo pacman -S --needed --noconfirm "$@"
    elif is_debian_like; then
        sudo apt-get install -y "$@"
    else
        print_message "未知发行版 $DISTRO_ID，请手动安装依赖: $*" "$RED"
        return 1
    fi
}

print_package_manager_failure_hint() {
    if is_arch_like && [[ -e /var/lib/pacman/db.lck ]]; then
        print_message "检测到 pacman 数据库锁: /var/lib/pacman/db.lck" "$YELLOW"
        print_message "请先确认没有 pacman/yay/paru/pamac 正在运行，再手动处理锁文件。" "$YELLOW"
        print_message "检查命令: ps aux | grep -E 'pacman|pamac|yay|paru' | grep -v grep" "$YELLOW"
        print_message "确认没有进程后再执行: sudo rm -f /var/lib/pacman/db.lck" "$YELLOW"
    elif is_arch_like; then
        print_message "Arch/EndeavourOS 出现依赖版本冲突时，通常是系统处于部分升级状态。" "$YELLOW"
        print_message "脚本不会自动更新系统；请手动同步依赖后再重试。" "$YELLOW"
        print_message "不更新内核可用：" "$YELLOW"
        print_message "sudo pacman -Syu --ignore linux --ignore linux-headers --ignore linux-zen --ignore linux-zen-headers --ignore linux-lts --ignore linux-lts-headers" "$YELLOW"
    fi
}

return_to_menu_after_error() {
    if [ "$RUN_ALL" -eq 0 ]; then
        echo
        read -rp "按 Enter 继续..."
        main_menu
    fi
    return 1
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

service_exists() {
    systemctl cat "$1" >/dev/null 2>&1
}

try_enable_start_service() {
    local unit="$1"
    if service_exists "$unit"; then
        sudo systemctl enable --now "$unit" 2>/dev/null || sudo systemctl start "$unit" 2>/dev/null || true
        return 0
    fi
    return 1
}

try_restart_service() {
    local unit="$1"
    if service_exists "$unit"; then
        sudo systemctl restart "$unit" 2>/dev/null || true
        return 0
    fi
    return 1
}

try_disable_stop_service() {
    local unit="$1"
    if service_exists "$unit"; then
        sudo systemctl disable --now "$unit" 2>/dev/null || true
        return 0
    fi
    return 1
}

cleanup_old_libvirt_refresh_service() {
    sudo systemctl disable --now kvmspoof-libvirt-refresh.service >/dev/null 2>&1 || true
    sudo rm -f /etc/systemd/system/kvmspoof-libvirt-refresh.service
    sudo rm -f /usr/local/sbin/kvmspoof-libvirt-refresh
    sudo systemctl reset-failed kvmspoof-libvirt-refresh.service >/dev/null 2>&1 || true
}

cleanup_old_libvirt_service_ordering() {
    sudo rm -f /etc/systemd/system/libvirtd.service.d/10-kvmspoof-after-udev.conf
    sudo rm -f /etc/systemd/system/virtqemud.service.d/10-kvmspoof-after-udev.conf
    sudo rm -f /etc/systemd/system/virtstoraged.service.d/10-kvmspoof-after-udev.conf
    sudo rm -f /etc/systemd/system/virtnodedevd.service.d/10-kvmspoof-after-udev.conf
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
}

configure_libvirt_services() {
    cleanup_old_libvirt_refresh_service
    cleanup_old_libvirt_service_ordering

    sudo systemctl unmask virtlogd.socket virtlockd.socket >/dev/null 2>&1 || true
    try_enable_start_service virtlogd.socket || true
    try_enable_start_service virtlockd.socket || true

    if is_arch_like; then
        sudo mkdir -p /run/libvirt >/dev/null 2>&1 || true
        sudo systemctl unmask \
            virtqemud.socket virtqemud-ro.socket virtqemud-admin.socket virtqemud.service \
            virtstoraged.socket virtstoraged-ro.socket virtstoraged-admin.socket virtstoraged.service \
            virtnodedevd.socket virtnodedevd-ro.socket virtnodedevd-admin.socket virtnodedevd.service \
            virtnetworkd.socket virtnetworkd-ro.socket virtnetworkd-admin.socket virtnetworkd.service \
            virtsecretd.socket virtsecretd-ro.socket virtsecretd-admin.socket virtsecretd.service \
            virtproxyd.socket virtproxyd-ro.socket virtproxyd-admin.socket >/dev/null 2>&1 || true
        try_disable_stop_service libvirtd.service || true
        try_disable_stop_service libvirtd.socket || true
        try_disable_stop_service libvirtd-ro.socket || true
        try_disable_stop_service libvirtd-admin.socket || true
        try_enable_start_service virtqemud.socket || true
        try_enable_start_service virtqemud-ro.socket || true
        try_enable_start_service virtqemud-admin.socket || true
        try_enable_start_service virtqemud.service || true
        try_enable_start_service virtstoraged.socket || true
        try_enable_start_service virtstoraged-ro.socket || true
        try_enable_start_service virtstoraged-admin.socket || true
        try_enable_start_service virtstoraged.service || true
        try_enable_start_service virtnodedevd.socket || true
        try_enable_start_service virtnodedevd-ro.socket || true
        try_enable_start_service virtnodedevd-admin.socket || true
        try_enable_start_service virtnodedevd.service || true
        try_enable_start_service virtnetworkd.socket || true
        try_enable_start_service virtnetworkd-ro.socket || true
        try_enable_start_service virtnetworkd-admin.socket || true
        try_enable_start_service virtnetworkd.service || true
        try_enable_start_service virtsecretd.socket || true
        try_enable_start_service virtsecretd-ro.socket || true
        try_enable_start_service virtsecretd-admin.socket || true
        try_enable_start_service virtsecretd.service || true
        try_enable_start_service virtproxyd.socket || true
        try_enable_start_service virtproxyd-ro.socket || true
        try_enable_start_service virtproxyd-admin.socket || true
        return 0
    fi

    try_enable_start_service libvirtd.service || true
    try_enable_start_service libvirt.service || true
}

restart_libvirt_services() {
    print_message "正在重启 libvirt/KVM 相关服务..." "$BLUE"
    try_restart_service virtlogd.socket || true
    try_restart_service virtlockd.socket || true

    if is_arch_like; then
        try_restart_service virtqemud.socket || true
        try_restart_service virtqemud-ro.socket || true
        try_restart_service virtqemud-admin.socket || true
        try_restart_service virtqemud.service || true
        try_restart_service virtstoraged.socket || true
        try_restart_service virtstoraged-ro.socket || true
        try_restart_service virtstoraged-admin.socket || true
        try_restart_service virtstoraged.service || true
        try_restart_service virtnodedevd.socket || true
        try_restart_service virtnodedevd-ro.socket || true
        try_restart_service virtnodedevd-admin.socket || true
        try_restart_service virtnodedevd.service || true
        try_restart_service virtnetworkd.socket || true
        try_restart_service virtnetworkd-ro.socket || true
        try_restart_service virtnetworkd-admin.socket || true
        try_restart_service virtnetworkd.service || true
        try_restart_service virtsecretd.socket || true
        try_restart_service virtsecretd-ro.socket || true
        try_restart_service virtsecretd-admin.socket || true
        try_restart_service virtsecretd.service || true
        try_restart_service virtproxyd.socket || true
        try_restart_service virtproxyd-ro.socket || true
        try_restart_service virtproxyd-admin.socket || true
        return 0
    fi

    try_restart_service libvirtd.service || true
    try_restart_service libvirt.service || true
}

refresh_libvirt_inventory() {
    print_message "正在刷新 libvirt 存储池与 PCI/USB 设备列表..." "$BLUE"

    if ! sudo virsh pool-info default >/dev/null 2>&1; then
        sudo mkdir -p /var/lib/libvirt/images
        sudo virsh pool-define-as default dir --target /var/lib/libvirt/images >/dev/null 2>&1 || true
    fi
    sudo virsh pool-start default >/dev/null 2>&1 || true
    sudo virsh pool-autostart default >/dev/null 2>&1 || true
    sudo virsh pool-refresh default >/dev/null 2>&1 || true
    sudo virsh nodedev-list >/dev/null 2>&1 || true

    print_message "libvirt 刷新完成；如 virt-manager 已打开，请关闭后重新打开。" "$GREEN"
}

verify_libvirt_qemu_connection() {
    local attempt

    for attempt in 1 2 3; do
        if sudo virsh -c qemu:///system list --all >/dev/null 2>&1; then
            print_message "libvirt qemu:///system 连接正常。" "$GREEN"
            return 0
        fi

        configure_libvirt_services
        if is_arch_like; then
            sudo systemctl start virtqemud.socket >/dev/null 2>&1 || true
            sudo systemctl start virtqemud.service >/dev/null 2>&1 || true
        fi
        sleep 1
    done

    print_message "libvirt qemu:///system 仍无法连接。" "$RED"
    if is_arch_like; then
        if [[ ! -S /run/libvirt/virtqemud-sock ]]; then
            print_message "未生成 /run/libvirt/virtqemud-sock。" "$RED"
        fi
        print_message "=== virtqemud 服务状态 ===" "$YELLOW"
        sudo systemctl status virtqemud.socket virtqemud.service virtstoraged.socket virtnodedevd.socket virtlogd.socket virtlockd.socket --no-pager || true
        print_message "=== virtqemud 最近日志 ===" "$YELLOW"
        sudo journalctl -u virtqemud.socket -u virtqemud.service -u virtstoraged.socket -u virtstoraged.service -u virtnodedevd.socket -u virtnodedevd.service -n 120 --no-pager || true
    else
        print_message "=== libvirtd 服务状态 ===" "$YELLOW"
        sudo systemctl status libvirtd.service virtlogd.socket virtlockd.socket --no-pager || true
        print_message "=== libvirtd 最近日志 ===" "$YELLOW"
        sudo journalctl -u libvirtd.service -n 80 --no-pager || true
    fi
    return 1
}

restart_and_refresh_libvirt() {
    configure_libvirt_services
    restart_libvirt_services
    refresh_libvirt_inventory
    verify_libvirt_qemu_connection
}

disable_apparmor_if_present() {
    if service_exists apparmor.service; then
        sudo systemctl stop apparmor 2>/dev/null || true
        sudo systemctl disable apparmor 2>/dev/null || true
    fi
}

start_ssh_service() {
    if is_arch_like; then
        try_enable_start_service sshd.service || true
    else
        try_enable_start_service ssh.service || sudo service ssh start 2>/dev/null || true
    fi
}

update_bootloader() {
    case "$DISTRO_ID" in
        arch|endeavouros|manjaro) sudo grub-mkconfig -o /boot/grub/grub.cfg ;;
        ubuntu|debian|linuxmint|void) sudo update-grub ;;
        fedora) sudo grub2-mkconfig -o /boot/efi/EFI/fedora/grub.cfg ;;
        pop) sudo bootctl update ;;
        opensuse) sudo grub2-mkconfig -o /boot/grub2/grub.cfg ;;
        *) print_message "请手动更新引导程序" "$YELLOW" ;;
    esac
}

# ============================================================
# 安装基础软件包
# ============================================================
install_base_packages() {
    print_message "=== 安装基础软件包 ===" "$BLUE"

    if is_arch_like; then
        if ! install_packages git gedit openssh curl jsoncpp screen libxml2 libvirt pciutils xmlstarlet dmidecode python; then
            print_message "基础软件包安装失败，已停止。" "$RED"
            print_package_manager_failure_hint
            return_to_menu_after_error
            return 1
        fi
    elif is_debian_like; then
        if ! install_packages git gedit openssh-server curl libjsoncpp-dev libcurl4-openssl-dev screen libxml2-utils libvirt-clients pciutils xmlstarlet dmidecode python3; then
            print_message "基础软件包安装失败，已停止。" "$RED"
            print_package_manager_failure_hint
            return_to_menu_after_error
            return 1
        fi
    else
        print_message "未知发行版 $DISTRO_ID，请手动安装基础软件包" "$RED"
        return 1
    fi

    start_ssh_service
    disable_apparmor_if_present

    print_message "基础软件包安装完成" "$GREEN"

    if [ "$RUN_ALL" -eq 0 ]; then
        echo
        read -rp "按 Enter 继续..."
        main_menu
    fi
}

# ============================================================
# 安装编译依赖
# ============================================================
install_build_dependencies() {
    print_message "=== 安装编译依赖 ===" "$BLUE"

    if is_arch_like; then
        local BUILD_DEPENDENCIES=(
            acpica
            base-devel
            dtc
            glib2
            pixman
            ninja
            python
            python-virtualenv
            zlib
            gnupg
            python-sphinx
            python-sphinx_rtd_theme
            patch
            curl
            spice
            libusb
            usbredir
            git
            git-lfs
            nasm
            edk2-ovmf
            pciutils
            pkgconf
            dmidecode
            virt-firmware
            seabios
        )
        if ! install_packages "${BUILD_DEPENDENCIES[@]}"; then
            print_message "编译依赖安装失败，已停止。" "$RED"
            print_package_manager_failure_hint
            return_to_menu_after_error
            return 1
        fi
    elif is_debian_like; then
        local BUILD_DEPENDENCIES=(
            acpica-tools
            build-essential
            libfdt-dev
            libglib2.0-dev
            libpixman-1-dev
            ninja-build
            python3-venv
            zlib1g-dev
            gnupg
            python3-sphinx
            python3-sphinx-rtd-theme
            patch
            curl
            libspice-server-dev
            libusb-1.0-0-dev
            libusbredirhost-dev
            libusbredirparser-dev
            uuid-dev
            git
            nasm
            python-is-python3
            python3-virt-firmware
            qemu-utils
            qemu-system-data
            seabios
            pciutils
            pkg-config
            dmidecode
            xmlstarlet
        )
        if ! install_packages "${BUILD_DEPENDENCIES[@]}"; then
            print_message "编译依赖安装失败，已停止。" "$RED"
            print_package_manager_failure_hint
            return_to_menu_after_error
            return 1
        fi
    else
        print_message "未知发行版 $DISTRO_ID，请手动安装编译依赖" "$RED"
        return 1
    fi

    print_message "编译依赖安装完成" "$GREEN"

    if [ "$RUN_ALL" -eq 0 ]; then
        echo
        read -rp "按 Enter 继续..."
        main_menu
    fi
}

# ============================================================
# 安装虚拟化 + IOMMU + libvirt
# ============================================================
install_virtualization_full() {
    print_message "=== 配置 IOMMU 内核参数 ===" "$BLUE"

    case "$DISTRO_ID" in
        "pop")
            local BOOT_CONFIG_FILE="/boot/efi/loader/entries/Pop_OS-current.conf"
            if ! grep -i -q "intel_iommu" "$BOOT_CONFIG_FILE" && ! grep -i -q "amd_iommu" "$BOOT_CONFIG_FILE"; then
                if grep -i -q 'intel' "/proc/cpuinfo"; then
                    sudo sed -i 's/quiet /&intel_iommu=on iommu=pt /' "$BOOT_CONFIG_FILE"
                    print_message "已添加 Intel IOMMU 参数" "$GREEN"
                elif grep -i -q 'amd' "/proc/cpuinfo"; then
                    sudo sed -i 's/quiet /&amd_iommu=on iommu=pt /' "$BOOT_CONFIG_FILE"
                    print_message "已添加 AMD IOMMU 参数" "$GREEN"
                fi
            else
                print_message "IOMMU 参数已存在" "$YELLOW"
            fi
            ;;
        *)
            local BOOT_CONFIG_FILE="/etc/default/grub"
            if ! grep -i -q "intel_iommu" "$BOOT_CONFIG_FILE" && ! grep -i -q "amd_iommu" "$BOOT_CONFIG_FILE"; then
                if grep -i -q 'intel' "/proc/cpuinfo"; then
                    sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/&intel_iommu=on iommu=pt /' "$BOOT_CONFIG_FILE"
                    print_message "已添加 Intel IOMMU 参数" "$GREEN"
                elif grep -i -q 'amd' "/proc/cpuinfo"; then
                    sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/&amd_iommu=on iommu=pt /' "$BOOT_CONFIG_FILE"
                    print_message "已添加 AMD IOMMU 参数" "$GREEN"
                fi
            else
                print_message "IOMMU 参数已存在" "$YELLOW"
            fi
            ;;
    esac

    print_message "=== 安装虚拟化软件包 ===" "$BLUE"
    local install_status=0
    case "$DISTRO_ID" in
        "arch"|"endeavouros"|"manjaro"|"arcolinux"|"garuda"|"artix")
            sudo pacman -S --needed --noconfirm \
                qemu-full libvirt virt-manager virt-viewer dnsmasq edk2-ovmf iptables-nft swtpm || install_status=$?
            ;;
        "ubuntu"|"debian"|"linuxmint"|"pop")
            sudo apt install qemu-kvm libvirt-clients libvirt-daemon-system bridge-utils virt-manager virtinst ovmf -y || install_status=$?
            ;;
        "void")
            sudo xbps-install -y qemu libvirt bridge-utils virt-manager || install_status=$?
            ;;
        "fedora")
            sudo dnf install @virtualization -y || install_status=$?
            ;;
        "opensuse")
            sudo zypper install -y libvirt libvirt-client libvirt-daemon virt-manager virt-install virt-viewer qemu qemu-kvm qemu-ovmf-x86_64 qemu-tools || install_status=$?
            ;;
        *)
            print_message "未知发行版，请手动安装虚拟化软件包" "$RED"
            return_to_menu_after_error
            return 1
            ;;
    esac

    if [[ "$install_status" -ne 0 ]]; then
        print_message "虚拟化软件包安装失败，已停止后续 KVM 配置。" "$RED"
        print_package_manager_failure_hint
        return_to_menu_after_error
        return 1
    fi

    if ! command -v virt-manager >/dev/null 2>&1; then
        print_message "virt-manager 未安装成功，已停止后续 KVM 配置。" "$RED"
        print_message "请先解决软件包安装错误后重新执行 [4] 安装虚拟机 KVM。" "$YELLOW"
        return_to_menu_after_error
        return 1
    fi

    if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
        print_message "qemu-system-x86_64 未安装成功，已停止后续 KVM 配置。" "$RED"
        print_message "Arch/EndeavourOS 请确认 qemu-full 安装成功。" "$YELLOW"
        return_to_menu_after_error
        return 1
    fi

    print_message "=== 配置 libvirt 服务 ===" "$BLUE"
    local LIBVIRTD_PATH="/etc/libvirt/libvirtd.conf"

    if ! grep -q 'unix_sock_group = "libvirt"' "$LIBVIRTD_PATH" 2>/dev/null; then
        echo 'unix_sock_group = "libvirt"' | sudo tee -a "$LIBVIRTD_PATH" > /dev/null
    fi
    if ! grep -q 'unix_sock_rw_perms = "0770"' "$LIBVIRTD_PATH" 2>/dev/null; then
        echo 'unix_sock_rw_perms = "0770"' | sudo tee -a "$LIBVIRTD_PATH" > /dev/null
    fi
    if ! grep -q '^log_filters="1:qemu"$' "$LIBVIRTD_PATH" 2>/dev/null; then
        echo 'log_filters="1:qemu"' | sudo tee -a "$LIBVIRTD_PATH" > /dev/null
    fi
    if ! grep -q '^log_outputs="1:file:/var/log/libvirt/libvirtd.log"$' "$LIBVIRTD_PATH" 2>/dev/null; then
        echo 'log_outputs="1:file:/var/log/libvirt/libvirtd.log"' | sudo tee -a "$LIBVIRTD_PATH" > /dev/null
    fi

    local CURR_USER
    CURR_USER="$(get_target_user)"
    sudo usermod -a -G libvirt "$CURR_USER"
    sudo usermod -a -G kvm "$CURR_USER"
    if getent group input >/dev/null 2>&1; then
        sudo usermod -a -G input "$CURR_USER"
    fi

    configure_libvirt_services

    print_message "=== 更新引导程序 ===" "$BLUE"
    update_bootloader

    if ! restart_and_refresh_libvirt; then
        print_message "KVM/libvirt 服务未正常启动，已停止。" "$RED"
        return_to_menu_after_error
        return 1
    fi
    print_message "虚拟化配置完成" "$GREEN"

    if [ "$RUN_ALL" -eq 0 ]; then
        echo
        read -rp "按 Enter 继续..."
        main_menu
    fi
}

# ============================================================
# 全部执行（调整后的顺序）
# ============================================================
run_all() {
    print_message "=== 开始全部安装 ===" "$CYAN"
    echo

    RUN_ALL=1
    install_base_packages || { RUN_ALL=0; return 1; }
    install_build_dependencies || { RUN_ALL=0; return 1; }
    install_virtualization_full || { RUN_ALL=0; return 1; }
    RUN_ALL=0

    echo
    print_message "=== 基础编译依赖包、KVM虚拟机 已安装完成！请重启计算机生效 ===" "$GREEN"
    echo
    read -rp "按 Enter 继续..."
    main_menu
}

# ============================================================
# 主菜单
# ============================================================
main_menu() {
    clear
    echo -e "${CYAN}=== 环境配置工具 ===${NC}"
    echo
    echo -e "  检测到系统: ${GREEN}$DISTRO_ID${NC}"
    echo
    echo "  [1] 全部执行"
    echo "  [2] 安装编译依赖"
    echo "  [3] 安装基础软件包"
    echo "  [4] 安装虚拟机 KVM"
    echo "  [0] 返回"
    echo
    read_menu_choice "请选择: "
    choice="$MENU_CHOICE"
    echo

    case "$choice" in
        1) run_all ;;
        2) install_build_dependencies ;;
        3) install_base_packages ;;
        4) install_virtualization_full ;;
        0) echo "已退出"; exit 0 ;;
        *) echo -e "${RED}[ERR]${NC} 无效选择"; sleep 1; main_menu ;;
    esac
}

# ============================================================
# 启动
# ============================================================
main_menu
