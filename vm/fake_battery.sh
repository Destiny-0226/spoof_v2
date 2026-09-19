#!/bin/bash

# ============================================================
# 颜色定义
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

RUNTIME_ROOT="${KVMSPOOF_RUNTIME_DIR:-/tmp/kvmspoof-runtime-standalone}"
mkdir -p "$RUNTIME_ROOT"
KVMSPOOF_STATE_FILE="${KVMSPOOF_STATE_FILE:-$RUNTIME_ROOT/kvmspoof_state.env}"
export KVMSPOOF_RUNTIME_DIR="$RUNTIME_ROOT"
export KVMSPOOF_STATE_FILE

# ============================================================
# 生成临时电池 DSL 文件并编译为 AML
# ============================================================
TMP_DSL=$(mktemp "$RUNTIME_ROOT/battery_XXXXXX.dsl")

cat > "$TMP_DSL" << 'EOF'
/*
 * Intel ACPI Component Architecture
 * AML/ASL+ Disassembler version 20240927 (64-bit version)
 * Copyright (c) 2000 - 2023 Intel Corporation
 * 
 * Disassembling to symbolic ASL+ operators
 *
 * Disassembly of SSDT1.aml
 *
 * Original Table Header:
 *     Signature        "SSDT"
 *     Length           0x000000A1 (161)
 *     Revision         0x01
 *     Checksum         0x80
 *     OEM ID           "BOCHS"
 *     OEM Table ID     "BXPCSSDT"
 *     OEM Revision     0x00000001 (1)
 *     Compiler ID      "INTL"
 *     Compiler Version 0x20240927 (539232551)
 */
DefinitionBlock ("", "SSDT", 1, "BOCHS", "BXPCSSDT", 0x00000001)
{
    External (_SB_.PCI0, DeviceObj)

    Scope (_SB.PCI0)
    {
        Device (BAT0)
        {
            Name (_HID, EisaId ("PNP0C0A") /* Control Method Battery */)  // _HID: Hardware ID
            Name (_UID, Zero)  // _UID: Unique ID
            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                Return (0x1F)
            }

            Method (_BIF, 0, NotSerialized)  // _BIF: Battery Information
            {
                Return (Package (0x0D)
                {
                    One, 
                    0x1770, 
                    0x1770, 
                    One, 
                    0x39D0, 
                    0x0258, 
                    0x012C, 
                    0x3C, 
                    0x3C, 
                    "", 
                    "", 
                    "LION", 
                    ""
                })
            }

            Method (_BST, 0, NotSerialized)  // _BST: Battery Status
            {
                Return (Package (0x04)
                {
                    Zero, 
                    Zero, 
                    0x1770, 
                    0x39D0
                })
            }
        }
    }
}
EOF

echo "已生成 DSL 文件: $TMP_DSL"

if command -v iasl &> /dev/null; then
    echo "正在编译..."
    iasl "$TMP_DSL"
    BASE_NAME="${TMP_DSL%.*}"
    rm -f "$TMP_DSL"
    echo "已删除临时 DSL 文件"

    if [ -f "${BASE_NAME}.aml" ]; then
        sudo cp "${BASE_NAME}.aml" /usr/local/bin/battery.aml
        echo "编译完成！已拷贝至：/usr/local/bin/battery.aml"
        rm -f "${BASE_NAME}.aml"
    else
        echo "编译失败！"
        exit 1
    fi
else
    echo "错误: iasl 未安装，无法编译。"
    echo "Ubuntu/Debian: sudo apt install acpica-tools"
    echo "Arch/Manjaro:  sudo pacman -S acpica"
    exit 1
fi

# ============================================================
# 注入电池 AML 到虚拟机 XML
# ============================================================
VM_NAME="win10"
BATTERY_PATH="/usr/local/bin/battery.aml"

if [ ! -f "$BATTERY_PATH" ]; then
    print_message "错误: battery.aml 文件不存在" "$RED"
    exit 1
fi

if ! command -v virt-xml >/dev/null 2>&1; then
    print_message "错误: virt-xml 未安装" "$RED"
    echo "Ubuntu/Debian: sudo apt install virtinst"
    echo "Arch/Manjaro:  sudo pacman -S virt-install"
    exit 1
fi

print_message "正在操作虚拟机 $VM_NAME 的 XML..." "$BLUE"

TMP_XML="$RUNTIME_ROOT/${VM_NAME}_battery.xml"

# 导出 XML
run_virsh dumpxml "$VM_NAME" > "$TMP_XML"

# 检查是否已存在 qemu:commandline 节点
if ! grep -q '<qemu:commandline' "$TMP_XML"; then
    echo "未找到 qemu:commandline 节点，正在创建并添加电池 acpitable..."
    # 使用 virt-xml 添加
    sudo virt-xml "$VM_NAME" --edit --qemu-commandline="-acpitable" --qemu-commandline="file=$BATTERY_PATH"
    print_message "已通过 virt-xml 添加电池 ACPI 表" "$GREEN"
    rm -f "$TMP_XML"
    exit 0
fi

# 检查是否已有 battery.aml 的 acpitable
if grep -q "value=['\"]file=$BATTERY_PATH" "$TMP_XML"; then
    print_message "电池 ACPI 表已存在，无需重复添加。" "$YELLOW"
    rm -f "$TMP_XML"
    exit 0
fi

# 在 </qemu:commandline> 前插入新的 arg 对
echo "正在添加新的 -acpitable 项..."

# 使用 awk 在 </qemu:commandline> 之前插入两行
awk -v bat_path="$BATTERY_PATH" '
    /<\/qemu:commandline>/ {
        print "    <qemu:arg value=\"-acpitable\"/>"
        print "    <qemu:arg value=\"file=" bat_path "\"/>"
        print "</qemu:commandline>"
        next
    }
    { print }
' "$TMP_XML" > "${TMP_XML}.new" && mv "${TMP_XML}.new" "$TMP_XML"

# 应用配置
if run_virsh define "$TMP_XML" > /dev/null 2>&1; then
    print_message "✅ 电池 ACPI 表已成功添加到虚拟机 $VM_NAME" "$GREEN"
else
    print_message "❌ 应用配置失败" "$RED"
fi

rm -f "$TMP_XML"

print_message "操作完成" "$GREEN"
