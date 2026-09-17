#!/usr/bin/env python3
"""Build a profile-bound QEMU from an untouched upstream source tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
from pathlib import Path

from smbios_memory import validate_record as validate_memory_record

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "artifacts" / "identity-hardware.json"
RESOURCES = ROOT / "resources"
SOURCE = RESOURCES / "qemu11backup"
OUT = ROOT / "build" / "qemu"
QEMU_URL = "https://gitlab.com/qemu-project/qemu.git"
QEMU_REF = "v11.0.2"
PATCH_REVISION = 34
ACPI_NAMESEG_RE = re.compile(r"\A[A-Z_][A-Z0-9_]{3}\Z")
QEMU_ACPI_TYPE_BY_ROLE = {
    "lpc": "ICH9-LPC",
    "smbus": "ICH9-SMB",
    "sata": "ich9-ahci",
    "usb": "qemu-xhci",
}

TOOL_PACKAGES = {
    "pacman": {
        "git": "git", "make": "make", "python3": "python", "ninja": "ninja",
        "meson": "meson", "pkg-config": "pkgconf", "cc": "gcc",
    },
    "apt-get": {
        "git": "git", "make": "build-essential", "python3": "python3",
        "ninja": "ninja-build", "meson": "meson", "pkg-config": "pkg-config",
        "cc": "build-essential",
    },
    "dnf": {
        "git": "git", "make": "make", "python3": "python3", "ninja": "ninja-build",
        "meson": "meson", "pkg-config": "pkgconf-pkg-config", "cc": "gcc",
    },
}


def run(command: list[str], cwd: Path | None = None, *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def install_missing_tools(missing: list[str]) -> None:
    manager = next((name for name in TOOL_PACKAGES if shutil.which(name)), None)
    if manager is None:
        raise RuntimeError("缺少 QEMU 构建工具且未找到受支持的包管理器: " + ", ".join(missing))
    privilege: list[str] = []
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError("自动安装构建工具需要 root 或 sudo: " + ", ".join(missing))
        privilege = [sudo]
    packages = sorted({TOOL_PACKAGES[manager][name] for name in missing})
    print(f"缺少构建工具，将通过 {manager} 自动安装: {', '.join(packages)}")
    if manager == "pacman":
        run(privilege + [manager, "-S", "--needed", "--noconfirm", *packages])
    elif manager == "apt-get":
        run(privilege + [manager, "update"])
        run(privilege + [manager, "install", "-y", *packages])
    else:
        run(privilege + [manager, "install", "-y", *packages])


def require_tools(names: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        install_missing_tools(missing)
    for name in names:
        path = shutil.which(name)
        if not path:
            raise RuntimeError("自动安装后仍缺少 QEMU 构建工具: " + name)
        found[name] = str(Path(path).resolve())
    return found


def build_jobs() -> str:
    value = os.environ.get("JOBS", str(os.cpu_count() or 1))
    if not value.isdecimal() or int(value) < 1:
        raise RuntimeError("JOBS 必须是正整数")
    return value


def prepare_output_directory(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not os.access(parent, os.W_OK):
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError(f"构建目录不可写且未找到 sudo: {parent}")
        print(f"构建目录归属其他用户，需要管理员权限修复: {parent}")
        run([sudo, "chown", f"{os.getuid()}:{os.getgid()}", str(parent)])
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError(f"旧构建目录不可写且未找到 sudo，无法清理: {path}")
    print(f"旧构建目录归属其他用户，需要管理员权限清理: {path}")
    run([sudo, "rm", "-rf", "--", str(path)])
    if path.exists():
        raise RuntimeError(f"旧构建目录清理失败: {path}")


def ensure_source() -> Path:
    if (SOURCE / "configure").is_file():
        return SOURCE
    if SOURCE.exists():
        raise RuntimeError(f"源码目录存在但不是有效 QEMU 源码: {SOURCE}")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("缺少 git，无法自动下载 QEMU 源码")
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    print(f"未找到本地 QEMU 原版源码，正在下载 {QEMU_REF} ...")
    run([git, "clone", "--depth", "1", "--branch", QEMU_REF, QEMU_URL, str(SOURCE)])
    return SOURCE


def validate_pristine_source(source: Path) -> None:
    version = (source / "VERSION").read_text(encoding="ascii").strip()
    if version != "11.0.2":
        raise RuntimeError(f"QEMU 源码版本应为 11.0.2，当前为 {version}")
    smbios = (source / "hw/smbios/smbios.c").read_text(encoding="utf-8")
    pci = (source / "include/hw/pci/pci.h").read_text(encoding="utf-8")
    pc = (source / "hw/i386/pc.c").read_text(encoding="utf-8")
    if "smbios_full_file" in smbios or "full-file=binary" in (source / "qemu-options.hx").read_text(encoding="utf-8"):
        raise RuntimeError("resources/qemu11backup 已包含旧 SMBIOS 补丁，请换回原版源码")
    if not re.search(r"PCI_VENDOR_ID_QEMU\s+0x1234", pci):
        raise RuntimeError("resources/qemu11backup 的公共 PCI ID 已被修改，不是原版源码")
    if "pcms->sata_enabled = true;" not in pc or "pcms->i8042_enabled = true;" not in pc:
        raise RuntimeError("resources/qemu11backup 的 PC 默认设备已被修改，不是原版源码")
    if not (source / ".git").exists():
        raise RuntimeError("resources/qemu11backup 缺少 Git 元数据，无法验证固定源码版本")
    expected = run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-list", "-n", "1", QEMU_REF],
        capture=True,
    ).stdout.strip()
    current = run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "HEAD"],
        capture=True,
    ).stdout.strip()
    if not expected or current != expected:
        raise RuntimeError(f"resources/qemu11backup 未锁定到 {QEMU_REF}")
    status = run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
        capture=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("resources/qemu11backup 不是干净的固定版本源码")


def verify_required_subprojects(source: Path) -> None:
    """Fetch dependencies that Meson requires even for an x86-only build."""
    required = source / "subprojects/keycodemapdb/data/keymaps.csv"
    if required.is_file():
        return
    meson = shutil.which("meson")
    if not meson:
        raise RuntimeError("QEMU 缺少 keycodemapdb，且未找到 meson 用于自动下载")
    print("QEMU keycodemapdb 不完整，正在按上游 wrap 锁定版本下载 ...")
    run([meson, "subprojects", "download", "keycodemapdb"], source)
    if not required.is_file():
        raise RuntimeError("QEMU keycodemapdb 下载后仍不完整")


def source_revision(source: Path) -> str:
    if not (source / ".git").exists() or not shutil.which("git"):
        return QEMU_REF
    try:
        return run(["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "HEAD"], capture=True).stdout.strip()
    except subprocess.CalledProcessError:
        return QEMU_REF


def copy_source(source: Path, target: Path) -> None:
    ignored = shutil.ignore_patterns(".git", "build", ".venv", "__pycache__", "*.pyc", "packagecache")
    shutil.copytree(source, target, symlinks=True, ignore=ignored)


def replace_once(path: Path, pattern: str, replacement: str, label: str, *, flags: int = re.MULTILINE) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match in {path}, got {count}")
    path.write_text(updated, encoding="utf-8")


def c_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def replace_literal(path: Path, old: str, new: str, label: str, expected: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{label}: expected {expected} matches in {path}, got {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def verify_i226_register_contract(binary: Path) -> None:
    """Exercise the driver-critical I226 register path through qtest."""
    command = [
        str(binary), "-machine", "q35,accel=qtest", "-display", "none",
        "-nodefaults", "-qtest", "stdio",
        "-device", "i226-v,mac=3c:fd:fe:00:00:01",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def request(line: str) -> str:
        process.stdin.write(line + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 5)
        if not ready:
            raise RuntimeError(f"I226-V qtest 超时: {line}")
        response = process.stdout.readline().strip()
        if not response.startswith("OK"):
            raise RuntimeError(f"I226-V qtest 失败: {line}: {response}")
        return response

    def readl(address: int) -> int:
        response = request(f"readl 0x{address:x}")
        try:
            return int(response.split()[1], 16)
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"I226-V qtest 返回值无效: {response}") from exc

    base = 0xc0000000
    mdic = base + 0x20
    mdic_ready = 0x10000000
    mdic_error = 0x40000000

    def mdic_read(phy: int, register: int) -> int:
        request(
            f"writel 0x{mdic:x} "
            f"0x{(0x08000000 | (phy << 21) | (register << 16)):08x}"
        )
        return readl(mdic)

    try:
        request("outl 0xcf8 0x80000810")
        request(f"outl 0xcfc 0x{base:08x}")
        request("outl 0xcf8 0x80000804")
        request("outw 0xcfc 0x0002")

        eecd = readl(base + 0x10)
        required_eecd = 0x00000200 | 0x00080000 | 0x00200000 | 0x04000000
        if eecd & required_eecd != required_eecd:
            raise RuntimeError(f"I226-V EECD 自动读取/闪存状态不完整: 0x{eecd:08x}")

        phy_id1 = mdic_read(0, 2)
        phy_id2 = mdic_read(0, 3)
        if (phy_id1 & 0xffff) != 0x67c9 or (phy_id2 & 0xffff) != 0xdc01:
            raise RuntimeError(
                f"I226-V GPY PHY ID 不一致: 0x{phy_id1:08x}/0x{phy_id2:08x}"
            )
        if (phy_id1 | phy_id2) & mdic_error:
            raise RuntimeError("I226-V GPY PHY 地址 0 被错误拒绝")
        if not ((phy_id1 & mdic_ready) and (phy_id2 & mdic_ready)):
            raise RuntimeError("I226-V GPY PHY 读取未置 READY")
        if not mdic_read(1, 2) & mdic_error:
            raise RuntimeError("I226-V 意外响应非法 PHY 地址 1")

        request(f"writel 0x{base:x} 0x04000000")
        if mdic_read(0, 2) & 0xffff != 0x67c9:
            raise RuntimeError("I226-V 全局复位后 GPY PHY 身份丢失")
        for queue in range(4):
            rxdctl = readl(base + 0xc028 + queue * 0x40)
            if rxdctl & 0x02000000:
                raise RuntimeError(
                    f"I226-V 全局复位后 RX 队列 {queue} 未禁用: "
                    f"0x{rxdctl:08x}"
                )
            txdctl = readl(base + 0xe028 + queue * 0x40)
            if txdctl & 0x02000000:
                raise RuntimeError(
                    f"I226-V 全局复位后 TX 队列 {queue} 未禁用: "
                    f"0x{txdctl:08x}"
                )

        # I226-V has four guest-visible descriptor queues.  They remain four
        # logical queues even when libvirt supplies one TAP for the one
        # physical Ethernet link.
        for queue in range(4):
            for offset in (0xc028 + queue * 0x40, 0xe028 + queue * 0x40):
                value = 0x02010000 | queue
                request(f"writel 0x{base + offset:x} 0x{value:08x}")
                if readl(base + offset) != value:
                    raise RuntimeError(
                        f"I226-V descriptor 队列 {queue} 控制寄存器不可用"
                    )

        for value in (0x040d001e, 0x040e1234, 0x040d401e, 0x040eabcd,
                      0x040d001e, 0x040e1234, 0x040d401e):
            request(f"writel 0x{mdic:x} 0x{value:08x}")
            result = readl(mdic)
            if result & mdic_error or not result & mdic_ready:
                raise RuntimeError(f"I226-V Clause 45 序列失败: 0x{result:08x}")
        mmd_value = mdic_read(0, 14)
        if mmd_value & mdic_error or (mmd_value & 0xffff) != 0xabcd:
            raise RuntimeError(f"I226-V Clause 45 数据不一致: 0x{mmd_value:08x}")

        register_values = {
            0x01a0: 0x00000020,
            0x2508: 0x00000000,
            0x580c: 0x00000000,
            0x5bb0: 0x00008815,
            0x5bb4: 0x00008c2a,
        }
        for offset, value in register_values.items():
            request(f"writel 0x{base + offset:x} 0x{value:08x}")
            actual = readl(base + offset)
            if actual != value:
                raise RuntimeError(
                    f"I226-V 专用寄存器 0x{offset:04x} 读回不一致: "
                    f"0x{actual:08x} != 0x{value:08x}"
                )
        for offset in (0x4038, 0x4104, 0x4118, 0x4148, 0x414c, 0x5814):
            if readl(base + offset) != 0:
                raise RuntimeError(f"I226-V 状态寄存器 0x{offset:04x} 复位值非零")

        rss_registers = {
            0x5000: 0x00002300,
            0x5818: 0x007b0002,
            0x5c00: 0x03020100,
            0x5c80: 0x6d5a56da,
            0x1700: 0x83828180,
        }
        for offset, value in rss_registers.items():
            request(f"writel 0x{base + offset:x} 0x{value:08x}")
            actual = readl(base + offset)
            if actual != value:
                raise RuntimeError(
                    f"I226-V RSS/队列寄存器 0x{offset:04x} 读回不一致: "
                    f"0x{actual:08x} != 0x{value:08x}"
                )
        for vector in range(5):
            offset = 0x1680 + vector * 4
            value = 0x20 + vector
            request(f"writel 0x{base + offset:x} 0x{value:08x}")
            if readl(base + offset) != value:
                raise RuntimeError(f"I226-V EITR{vector} 寄存器不可用")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


I226_SOURCE = r'''/*
 * Intel Ethernet Controller I226-V emulation.
 *
 * The PCIe function, NVM access path and I225/I226-only register facade are
 * implemented here.  The descriptor datapath reuses QEMU's Intel advanced
 * descriptor engine; no 82576 PCI identity or SR-IOV capability is exposed.
 */

#include "qemu/osdep.h"
#include "qemu/units.h"
#include "net/eth.h"
#include "net/net.h"
#include "net/tap.h"
#include "qemu/module.h"
#include "qemu/range.h"
#include "qemu/error-report.h"
#include "system/system.h"
#include "hw/core/hw-error.h"
#include "hw/net/mii.h"
#include "hw/pci/pci.h"
#include "hw/pci/pcie.h"
#include "hw/pci/msi.h"
#include "hw/pci/msix.h"
#include "hw/core/qdev-properties.h"
#include "migration/vmstate.h"

#include "igb_common.h"
#include "igb_core.h"

#include "trace.h"
#include "qapi/error.h"
#include "qom/object.h"

#define TYPE_I226_V "i226-v"
OBJECT_DECLARE_SIMPLE_TYPE(I226VState, I226_V)

#define I226_PCI_DEVICE_ID       0x125c
#define I226_PCI_REVISION        0x04
#define I226_SUBSYSTEM_VENDOR_ID 0x8086
#define I226_SUBSYSTEM_ID        0x0000

#define I226_MMIO_BAR            0
#define I226_MSIX_BAR            3
#define I226_MMIO_SIZE           (1 * MiB)
#define I226_MSIX_SIZE           (16 * KiB)
#define I226_MSIX_VECTORS        5
#define I226_QUEUE_COUNT         4
#define I226_ROM_SIZE            (1 * MiB)
#define I226_AER_OFFSET          0x100
#define I226_DSN_OFFSET          0x140
#define I226_LTR_OFFSET          0x1c0
#define I226_L1SS_OFFSET         0x1e0
#define I226_PTM_OFFSET          0x1f0

#define I226_EERD                0x12014
#define I226_EEWR                0x12018
#define I226_I225_PHPM           0x00e14
#define I226_EEER                0x00e30
#define I226_EEE_SU              0x00e34
#define I226_IPCNFG              0x00e38
#define I226_LTRC                0x001a0
#define I226_DMACR               0x02508
#define I226_SEQEC               0x04038
#define I226_RPTHC               0x04104
#define I226_HGPTC               0x04118
#define I226_TLPIC               0x04148
#define I226_RLPIC               0x0414c
#define I226_WUFC_EXT            0x0580c
#define I226_WUS_EXT             0x05814
#define I226_LTRMINV             0x05bb0
#define I226_LTRMAXV             0x05bb4

#define I226_EECD_SIZE_MASK      0x00007800
#define I226_EECD_SIZE_SHIFT     11
#define I226_EECD_AUTO_RD        0x00000200
#define I226_EECD_FLASH_PRESENT  0x00080000
#define I226_EECD_SHADOW_VALID   0x00200000
#define I226_EECD_FLUDONE        0x04000000
#define I226_STATUS_SPEED_2500   0x00400000
#define I226_PHY_RESET_COMPLETE  0x00000100

#define I226_MMD_ACCESS_CTRL     13
#define I226_MMD_ACCESS_DATA     14
#define I226_MMD_DATA_MODE       0x4000
#define I226_MMD_SLOTS           64

typedef struct I226MmdEntry {
    uint16_t device;
    uint16_t address;
    uint16_t value;
} I226MmdEntry;

struct I226VState {
    PCIDevice parent_obj;
    NICState *nic;
    NICConf conf;
    MemoryRegion mmio;
    MemoryRegion msix;
    IGBCore core;
    bool has_flr;
    uint32_t eewr;
    uint32_t phpm;
    uint32_t eeer;
    uint32_t eee_su;
    uint32_t ipcnfg;
    uint32_t ltrc;
    uint32_t dmacr;
    uint32_t wufc_ext;
    uint32_t wus_ext;
    uint32_t ltrminv;
    uint32_t ltrmaxv;
    uint32_t rpthc;
    uint32_t hgptc;
    uint16_t mmd_control;
    uint16_t mmd_address;
    uint16_t mmd_count;
    I226MmdEntry mmd[I226_MMD_SLOTS];
};

/* The first 64 shadow-RAM words are checksummed to 0xbaba at realize time. */
static const uint16_t i226_nvm_template[64] = {
    0x0000, 0x0000, 0x0000, 0x0420, 0xffff, 0x2017, 0xffff, 0xffff,
    0x2345, 0x0000, 0x0000, 0x125c, 0x8086, 0x125c, 0x0000, 0x8058,
    0x0000, 0x2001, 0x7e7c, 0xffff, 0x1000, 0x00c8, 0x0000, 0x2704,
    0x6cc9, 0x3150, 0x070e, 0x460b, 0x2d84, 0x0100, 0xf004, 0x0706,
    0x6000, 0x0080, 0x0f04, 0x7fff, 0x4f01, 0xc600, 0x0000, 0x20ff,
    0x0028, 0x0003, 0x0000, 0x0000, 0x0000, 0x0003, 0x0000, 0xffff,
    0x0100, 0xc000, 0x221c, 0xc007, 0xffff, 0xffff, 0xffff, 0xffff,
    0xffff, 0xffff, 0xffff, 0xffff, 0x0000, 0x0120, 0xffff, 0x0000,
};

static uint64_t i226_dsn_from_mac(const uint8_t *mac)
{
    return (uint64_t)mac[5] |
           (uint64_t)mac[4] << 8 |
           (uint64_t)mac[3] << 16 |
           (uint64_t)0xffff << 24 |
           (uint64_t)mac[2] << 40 |
           (uint64_t)mac[1] << 48 |
           (uint64_t)mac[0] << 56;
}

static void i226_restore_phy_identity(I226VState *s)
{
    s->core.phy[MII_PHYID1] = 0x67c9;
    s->core.phy[MII_PHYID2] = 0xdc01;
    s->core.phy[0x1e] = 0x888d;
    s->phpm |= I226_PHY_RESET_COMPLETE;
}

static void i226_restore_queue_reset_contract(I226VState *s)
{
    unsigned int i;

    /* Do not inherit the reused 82576 queue-0 enable state across I226 reset. */
    for (i = 0; i < I226_QUEUE_COUNT; i++) {
        s->core.mac[RXDCTL0 + i * 16] &= ~E1000_RXDCTL_QUEUE_ENABLE;
        s->core.mac[TXDCTL0 + i * 16] &= ~E1000_TXDCTL_QUEUE_ENABLE;
    }
}

static void i226_add_host_packets(uint32_t *counter, uint32_t delta)
{
    if (UINT32_MAX - *counter < delta) {
        *counter = UINT32_MAX;
    } else {
        *counter += delta;
    }
}

static uint16_t i226_mmd_read(I226VState *s)
{
    unsigned int i;

    for (i = 0; i < s->mmd_count; i++) {
        if (s->mmd[i].device == (s->mmd_control & 0x1f) &&
            s->mmd[i].address == s->mmd_address) {
            return s->mmd[i].value;
        }
    }
    return 0;
}

static void i226_mmd_write(I226VState *s, uint16_t value)
{
    unsigned int i;

    for (i = 0; i < s->mmd_count; i++) {
        if (s->mmd[i].device == (s->mmd_control & 0x1f) &&
            s->mmd[i].address == s->mmd_address) {
            s->mmd[i].value = value;
            return;
        }
    }
    if (s->mmd_count < I226_MMD_SLOTS) {
        I226MmdEntry *entry = &s->mmd[s->mmd_count++];
        entry->device = s->mmd_control & 0x1f;
        entry->address = s->mmd_address;
        entry->value = value;
    }
}

static uint64_t i226_mmio_read(void *opaque, hwaddr addr, unsigned size)
{
    I226VState *s = opaque;
    uint64_t value;

    switch (addr) {
    case E1000_STATUS:
        value = igb_core_read(&s->core, addr, size);
        if (value & E1000_STATUS_LU) {
            value |= E1000_STATUS_SPEED_1000 | I226_STATUS_SPEED_2500;
        }
        return value;
    case E1000_EECD:
        value = igb_core_read(&s->core, addr, size);
        value &= ~I226_EECD_SIZE_MASK;
        return value | (4U << I226_EECD_SIZE_SHIFT) |
               I226_EECD_AUTO_RD | I226_EECD_FLASH_PRESENT |
               I226_EECD_SHADOW_VALID | I226_EECD_FLUDONE;
    case I226_EERD:
        return igb_core_read(&s->core, E1000_EERD, size);
    case I226_EEWR:
        return s->eewr;
    case I226_I225_PHPM:
        return s->phpm | I226_PHY_RESET_COMPLETE;
    case I226_EEER:
        return s->eeer;
    case I226_EEE_SU:
        return s->eee_su;
    case I226_IPCNFG:
        return s->ipcnfg;
    case I226_LTRC:
        return s->ltrc;
    case I226_DMACR:
        return s->dmacr;
    case I226_WUFC_EXT:
        return s->wufc_ext;
    case I226_WUS_EXT:
        return s->wus_ext;
    case I226_LTRMINV:
        return s->ltrminv;
    case I226_LTRMAXV:
        return s->ltrmaxv;
    case I226_RPTHC:
        value = s->rpthc;
        s->rpthc = 0;
        return value;
    case I226_HGPTC:
        value = s->hgptc;
        s->hgptc = 0;
        return value;
    case I226_SEQEC:
    case I226_TLPIC:
    case I226_RLPIC:
        return 0;
    default:
        return igb_core_read(&s->core, addr, size);
    }
}

static void i226_mdic_write(I226VState *s, uint32_t value)
{
    uint32_t reg = (value & E1000_MDIC_REG_MASK) >> E1000_MDIC_REG_SHIFT;
    uint32_t phy = (value & E1000_MDIC_PHY_MASK) >> E1000_MDIC_PHY_SHIFT;
    uint32_t backend_value;
    uint16_t data = value & E1000_MDIC_DATA_MASK;

    /* I225/I226 integrated GPY PHY is MDIO address 0.  The reused igb
     * datapath models its PHY at address 1, so translate only internally. */
    if (phy != 0) {
        s->core.mac[MDIC] = value | E1000_MDIC_READY | E1000_MDIC_ERROR;
        return;
    }
    backend_value = (value & ~E1000_MDIC_PHY_MASK) |
                    (1U << E1000_MDIC_PHY_SHIFT);
    igb_core_write(&s->core, E1000_MDIC, backend_value,
                   sizeof(backend_value));
    s->core.mac[MDIC] = (s->core.mac[MDIC] & ~E1000_MDIC_PHY_MASK) |
                        (value & E1000_MDIC_PHY_MASK);
    if (value & E1000_MDIC_OP_WRITE) {
        if (reg == I226_MMD_ACCESS_CTRL) {
            s->mmd_control = data;
            s->core.mac[MDIC] = value | E1000_MDIC_READY;
        } else if (reg == I226_MMD_ACCESS_DATA) {
            if (s->mmd_control & I226_MMD_DATA_MODE) {
                i226_mmd_write(s, data);
            } else {
                s->mmd_address = data;
            }
            s->core.mac[MDIC] = value | E1000_MDIC_READY;
        }
    } else if (value & E1000_MDIC_OP_READ) {
        if (reg == I226_MMD_ACCESS_DATA &&
            (s->mmd_control & I226_MMD_DATA_MODE)) {
            s->core.mac[MDIC] = (value & ~E1000_MDIC_DATA_MASK) |
                                i226_mmd_read(s) | E1000_MDIC_READY;
        } else if (reg == 0x1e) {
            s->core.mac[MDIC] = (value & ~E1000_MDIC_DATA_MASK) |
                                0x888d | E1000_MDIC_READY;
        }
    }
}

static void i226_mmio_write(void *opaque, hwaddr addr, uint64_t value,
                            unsigned size)
{
    I226VState *s = opaque;
    uint32_t before;

    switch (addr) {
    case E1000_CTRL:
        igb_core_write(&s->core, addr, value, size);
        if (value & E1000_CTRL_RST) {
            i226_restore_phy_identity(s);
            i226_restore_queue_reset_contract(s);
        }
        return;
    case I226_EERD:
        igb_core_write(&s->core, E1000_EERD, value, size);
        return;
    case I226_EEWR: {
        uint32_t word = (value >> E1000_EERW_ADDR_SHIFT) &
                        E1000_EERW_ADDR_MASK;
        uint16_t data = value >> E1000_EERW_DATA_SHIFT;
        s->eewr = value;
        if ((value & E1000_EERW_START) && word < IGB_EEPROM_SIZE) {
            s->core.eeprom[word] = data;
            s->eewr |= E1000_EERW_DONE;
        }
        return;
    }
    case E1000_MDIC:
        i226_mdic_write(s, value);
        return;
    case I226_I225_PHPM:
        s->phpm = value | I226_PHY_RESET_COMPLETE;
        return;
    case I226_EEER:
        s->eeer = value;
        return;
    case I226_EEE_SU:
        s->eee_su = value;
        return;
    case I226_IPCNFG:
        s->ipcnfg = value;
        return;
    case I226_LTRC:
        s->ltrc = value;
        return;
    case I226_DMACR:
        s->dmacr = value;
        return;
    case I226_WUFC_EXT:
        s->wufc_ext = value;
        return;
    case I226_WUS_EXT:
        s->wus_ext &= ~(uint32_t)value;
        return;
    case I226_LTRMINV:
        s->ltrminv = value;
        return;
    case I226_LTRMAXV:
        s->ltrmaxv = value;
        return;
    default:
        before = s->core.mac[GPTC];
        igb_core_write(&s->core, addr, value, size);
        if (s->core.mac[GPTC] >= before) {
            i226_add_host_packets(&s->hgptc,
                                  s->core.mac[GPTC] - before);
        }
    }
}

static const MemoryRegionOps i226_mmio_ops = {
    .read = i226_mmio_read,
    .write = i226_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl = { .min_access_size = 4, .max_access_size = 4 },
};

static bool i226_can_receive(NetClientState *nc)
{
    I226VState *s = qemu_get_nic_opaque(nc);
    return igb_can_receive(&s->core);
}

static ssize_t i226_receive(NetClientState *nc, const uint8_t *buf,
                            size_t size)
{
    I226VState *s = qemu_get_nic_opaque(nc);
    uint32_t before = s->core.mac[GPRC];
    ssize_t result = igb_receive(&s->core, buf, size);

    if (s->core.mac[GPRC] >= before) {
        i226_add_host_packets(&s->rpthc, s->core.mac[GPRC] - before);
    }
    return result;
}

static ssize_t i226_receive_iov(NetClientState *nc, const struct iovec *iov,
                                int iovcnt)
{
    I226VState *s = qemu_get_nic_opaque(nc);
    uint32_t before = s->core.mac[GPRC];
    ssize_t result = igb_receive_iov(&s->core, iov, iovcnt);

    if (s->core.mac[GPRC] >= before) {
        i226_add_host_packets(&s->rpthc, s->core.mac[GPRC] - before);
    }
    return result;
}

static void i226_link_status_changed(NetClientState *nc)
{
    I226VState *s = qemu_get_nic_opaque(nc);
    igb_core_set_link_status(&s->core);
}

static NetClientInfo i226_net_info = {
    .type = NET_CLIENT_DRIVER_NIC,
    .size = sizeof(NICState),
    .can_receive = i226_can_receive,
    .receive = i226_receive,
    .receive_iov = i226_receive_iov,
    .link_status_changed = i226_link_status_changed,
};

static void i226_write_config(PCIDevice *pdev, uint32_t addr, uint32_t value,
                              int len)
{
    I226VState *s = I226_V(pdev);

    pci_default_write_config(pdev, addr, value, len);
    if (s->has_flr) {
        pcie_cap_flr_write_config(pdev, addr, value, len);
    }
    if (range_covers_byte(addr, len, PCI_COMMAND) &&
        (pdev->config[PCI_COMMAND] & PCI_COMMAND_MASTER)) {
        igb_start_recv(&s->core);
    }
}

static int i226_add_pm_capability(PCIDevice *pdev, uint8_t offset)
{
    Error *local_err = NULL;
    int ret = pci_pm_init(pdev, offset, &local_err);

    if (local_err) {
        error_report_err(local_err);
        return ret;
    }
    pci_set_word(pdev->config + offset + PCI_PM_PMC,
                 PCI_PM_CAP_VER_1_2 | PCI_PM_CAP_DSI |
                 PCI_PM_CAP_PME_D0 | PCI_PM_CAP_PME_D3hot |
                 PCI_PM_CAP_PME_D3cold);
    pci_set_word(pdev->config + offset + PCI_PM_CTRL,
                 PCI_PM_CTRL_NO_SOFT_RESET | 0x2000);
    pci_set_word(pdev->wmask + offset + PCI_PM_CTRL,
                 PCI_PM_CTRL_STATE_MASK | PCI_PM_CTRL_PME_ENABLE |
                 PCI_PM_CTRL_DATA_SEL_MASK);
    pci_set_word(pdev->w1cmask + offset + PCI_PM_CTRL,
                 PCI_PM_CTRL_PME_STATUS);
    return ret;
}

static void i226_set_ext_cap_next(PCIDevice *pdev, uint16_t offset,
                                  uint16_t next)
{
    uint32_t header = pci_get_long(pdev->config + offset);

    header &= 0x000fffff;
    header |= (uint32_t)next << 20;
    pci_set_long(pdev->config + offset, header);
}

static void i226_init_extended_capabilities(PCIDevice *pdev)
{
    uint8_t *config = pdev->config;
    uint8_t *wmask = pdev->wmask;

    pcie_add_capability(pdev, PCI_EXT_CAP_ID_LTR, 1, I226_LTR_OFFSET, 8);
    pci_set_word(config + I226_LTR_OFFSET + PCI_LTR_MAX_SNOOP_LAT, 0);
    pci_set_word(config + I226_LTR_OFFSET + PCI_LTR_MAX_NOSNOOP_LAT, 0);
    pci_set_word(wmask + I226_LTR_OFFSET + PCI_LTR_MAX_SNOOP_LAT, 0x1fff);
    pci_set_word(wmask + I226_LTR_OFFSET + PCI_LTR_MAX_NOSNOOP_LAT, 0x1fff);

    pcie_add_capability(pdev, PCI_EXT_CAP_ID_L1SS, 1, I226_L1SS_OFFSET, 16);
    pci_set_long(config + I226_L1SS_OFFSET + PCI_L1SS_CAP, 0x0039371f);
    pci_set_long(config + I226_L1SS_OFFSET + PCI_L1SS_CTL1, 0x40870000);
    pci_set_long(config + I226_L1SS_OFFSET + PCI_L1SS_CTL2, 0x00000039);
    pci_set_long(wmask + I226_L1SS_OFFSET + PCI_L1SS_CTL1, 0xe3ffff0f);
    pci_set_long(wmask + I226_L1SS_OFFSET + PCI_L1SS_CTL2, 0x000000fb);

    pcie_add_capability(pdev, PCI_EXT_CAP_ID_PTM, 1, I226_PTM_OFFSET, 12);
    pci_set_long(config + I226_PTM_OFFSET + PCI_PTM_CAP,
                 PCI_PTM_CAP_REQ | (4U << 8));
    pci_set_long(config + I226_PTM_OFFSET + PCI_PTM_CTRL, 0);
    pci_set_long(wmask + I226_PTM_OFFSET + PCI_PTM_CTRL,
                 PCI_PTM_CTRL_ENABLE);

    i226_set_ext_cap_next(pdev, I226_AER_OFFSET, I226_DSN_OFFSET);
    i226_set_ext_cap_next(pdev, I226_DSN_OFFSET, I226_LTR_OFFSET);
    i226_set_ext_cap_next(pdev, I226_LTR_OFFSET, I226_PTM_OFFSET);
    i226_set_ext_cap_next(pdev, I226_PTM_OFFSET, I226_L1SS_OFFSET);
    i226_set_ext_cap_next(pdev, I226_L1SS_OFFSET, 0);
}

static void i226_init_pcie_identity(I226VState *s)
{
    PCIDevice *pdev = PCI_DEVICE(s);
    uint8_t *exp = pdev->config + pdev->exp.exp_cap;

    pci_set_long(exp + PCI_EXP_DEVCAP,
                 0x00008cc1 | (s->has_flr ? PCI_EXP_DEVCAP_FLR : 0));
    pci_set_word(exp + PCI_EXP_DEVCTL,
                 PCI_EXP_DEVCTL_RELAX_EN | PCI_EXP_DEVCTL_PAYLOAD_256B |
                 PCI_EXP_DEVCTL_NOSNOOP_EN | PCI_EXP_DEVCTL_READRQ_512B);
    pci_set_word(exp + PCI_EXP_DEVSTA, PCI_EXP_DEVSTA_AUXPD);
    pci_set_long(exp + PCI_EXP_LNKCAP,
                 QEMU_PCI_EXP_LNKCAP_MLS(QEMU_PCI_EXP_LNK_5GT) |
                 QEMU_PCI_EXP_LNKCAP_MLW(QEMU_PCI_EXP_LNK_X1) |
                 PCI_EXP_LNKCAP_ASPM_L1 | (2U << 15) | (1U << 22));
    pci_set_word(exp + PCI_EXP_LNKCTL, PCI_EXP_LNKCTL_CCC);
    pci_set_word(exp + PCI_EXP_LNKSTA,
                 QEMU_PCI_EXP_LNKSTA_CLS(QEMU_PCI_EXP_LNK_5GT) |
                 QEMU_PCI_EXP_LNKSTA_NLW(QEMU_PCI_EXP_LNK_X1) |
                 PCI_EXP_LNKSTA_SLC);
    pci_set_long(exp + PCI_EXP_DEVCAP2,
                 0x0000000f | PCI_EXP_DEVCAP2_COMP_TMOUT_DIS |
                 PCI_EXP_DEVCAP2_LTR);
    pci_set_word(pdev->wmask + pdev->exp.exp_cap + PCI_EXP_DEVCTL2,
                 PCI_EXP_DEVCTL2_COMP_TIMEOUT |
                 PCI_EXP_DEVCTL2_COMP_TMOUT_DIS | PCI_EXP_DEVCTL2_LTR_EN);
}

static void i226_init_unprogrammed_option_rom(PCIDevice *pdev)
{
    if (pdev->romfile && pdev->romfile[0]) {
        return;
    }
    pdev->romsize = I226_ROM_SIZE;
    pdev->has_rom = true;
    memory_region_init_rom(&pdev->rom, OBJECT(pdev), "i226-option-rom",
                           I226_ROM_SIZE, &error_fatal);
    memset(memory_region_get_ram_ptr(&pdev->rom), 0xff, I226_ROM_SIZE);
    pci_register_bar(pdev, PCI_ROM_SLOT, 0, &pdev->rom);
}

static void i226_init_msix(I226VState *s)
{
    int i;
    int ret = msix_init(PCI_DEVICE(s), I226_MSIX_VECTORS,
                        &s->msix, I226_MSIX_BAR, 0,
                        &s->msix, I226_MSIX_BAR, 0x2000,
                        0x70, NULL);

    if (ret < 0) {
        error_report("I226-V MSI-X initialization failed: %d", ret);
        return;
    }
    for (i = 0; i < I226_MSIX_VECTORS; i++) {
        msix_vector_use(PCI_DEVICE(s), i);
    }
}

static void i226_cleanup_msix(I226VState *s)
{
    msix_unuse_all_vectors(PCI_DEVICE(s));
    msix_uninit(PCI_DEVICE(s), &s->msix, &s->msix);
}

static void i226_init_net_peer(I226VState *s, uint8_t *macaddr)
{
    DeviceState *dev = DEVICE(s);
    NetClientState *nc;
    int i;

    s->nic = qemu_new_nic(&i226_net_info, &s->conf,
                          object_get_typename(OBJECT(s)), dev->id,
                          &dev->mem_reentrancy_guard, s);
    /* Descriptor queues are guest hardware.  Net peers are host backends:
     * a single TAP is normal for one physical Ethernet link, so TX queues
     * safely converge there while RX RSS still selects guest descriptor
     * queues through MRQC/RETA inside igb_core. */
    s->core.max_queue_num = MIN(I226_QUEUE_COUNT - 1,
                                s->conf.peers.queues ?
                                s->conf.peers.queues - 1 : 0);
    memcpy(s->core.permanent_mac, macaddr, ETH_ALEN);
    qemu_format_nic_info_str(qemu_get_queue(s->nic), macaddr);

    for (i = 0; i < s->conf.peers.queues; i++) {
        nc = qemu_get_subqueue(s->nic, i);
        if (!nc->peer || !qemu_has_vnet_hdr(nc->peer)) {
            return;
        }
    }
    s->core.has_vnet = true;
    for (i = 0; i < s->conf.peers.queues; i++) {
        nc = qemu_get_subqueue(s->nic, i);
        qemu_set_vnet_hdr_len(nc->peer, sizeof(struct virtio_net_hdr));
    }
}

static void i226_pci_realize(PCIDevice *pdev, Error **errp)
{
    I226VState *s = I226_V(pdev);
    uint8_t *macaddr;
    int ret;

    pdev->config_write = i226_write_config;
    pdev->config[PCI_CACHE_LINE_SIZE] = 0x10;
    pdev->config[PCI_INTERRUPT_PIN] = 1;

    memory_region_init_io(&s->mmio, OBJECT(s), &i226_mmio_ops, s,
                          "i226-mmio", I226_MMIO_SIZE);
    pci_register_bar(pdev, I226_MMIO_BAR, PCI_BASE_ADDRESS_SPACE_MEMORY,
                     &s->mmio);
    memory_region_init(&s->msix, OBJECT(s), "i226-msix", I226_MSIX_SIZE);
    pci_register_bar(pdev, I226_MSIX_BAR, PCI_BASE_ADDRESS_SPACE_MEMORY,
                     &s->msix);

    qemu_macaddr_default_if_unset(&s->conf.macaddr);
    macaddr = s->conf.macaddr.a;

    if (pcie_endpoint_cap_init(pdev, 0xa0) < 0) {
        hw_error("Failed to initialize I226-V PCIe capability");
    }
    pcie_cap_fill_link_ep_usp(pdev, QEMU_PCI_EXP_LNK_X1,
                              QEMU_PCI_EXP_LNK_5GT, false);
    i226_init_msix(s);
    ret = msi_init(pdev, 0x50, 1, true, true, NULL);
    if (ret) {
        error_report("I226-V MSI initialization failed: %d", ret);
    }
    if (i226_add_pm_capability(pdev, 0x40) < 0) {
        hw_error("Failed to initialize I226-V PM capability");
    }
    if (s->has_flr) {
        pcie_cap_flr_init(pdev);
    }
    i226_init_pcie_identity(s);
    if (pcie_aer_init(pdev, 2, I226_AER_OFFSET, 0x40, errp) < 0) {
        i226_cleanup_msix(s);
        return;
    }
    pcie_dev_ser_num_init(pdev, I226_DSN_OFFSET,
                          i226_dsn_from_mac(macaddr));
    i226_init_extended_capabilities(pdev);
    i226_init_unprogrammed_option_rom(pdev);

    i226_init_net_peer(s, macaddr);
    s->core.owner = pdev;
    s->core.owner_nic = s->nic;
    igb_core_pci_realize(&s->core, i226_nvm_template,
                         sizeof(i226_nvm_template), macaddr);
}

static void i226_pci_uninit(PCIDevice *pdev)
{
    I226VState *s = I226_V(pdev);

    igb_core_pci_uninit(&s->core);
    pcie_aer_exit(pdev);
    pcie_cap_exit(pdev);
    qemu_del_nic(s->nic);
    i226_cleanup_msix(s);
    msi_uninit(pdev);
}

static void i226_reset_hold(Object *obj, ResetType type)
{
    I226VState *s = I226_V(obj);

    igb_core_reset(&s->core);
    i226_restore_queue_reset_contract(s);
    s->phpm = I226_PHY_RESET_COMPLETE;
    i226_restore_phy_identity(s);
    s->eewr = E1000_EERW_DONE;
    s->eeer = 0;
    s->eee_su = 0;
    s->ipcnfg = 0;
    s->ltrc = 0;
    s->dmacr = 0;
    s->wufc_ext = 0;
    s->wus_ext = 0;
    s->ltrminv = 0;
    s->ltrmaxv = 0;
    s->rpthc = 0;
    s->hgptc = 0;
    s->mmd_control = 0;
    s->mmd_address = 0;
    s->mmd_count = 0;
    memset(s->mmd, 0, sizeof(s->mmd));
}

static int i226_pre_save(void *opaque)
{
    I226VState *s = opaque;

    igb_core_pre_save(&s->core);
    return 0;
}

static int i226_post_load(void *opaque, int version_id)
{
    I226VState *s = opaque;

    return igb_core_post_load(&s->core);
}

static const VMStateDescription i226_vmstate_tx_ctx = {
    .name = "i226-tx-ctx",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(vlan_macip_lens, struct e1000_adv_tx_context_desc),
        VMSTATE_UINT32(seqnum_seed, struct e1000_adv_tx_context_desc),
        VMSTATE_UINT32(type_tucmd_mlhl, struct e1000_adv_tx_context_desc),
        VMSTATE_UINT32(mss_l4len_idx, struct e1000_adv_tx_context_desc),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription i226_vmstate_tx = {
    .name = "i226-tx",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_STRUCT_ARRAY(ctx, struct igb_tx, 2, 0,
                             i226_vmstate_tx_ctx,
                             struct e1000_adv_tx_context_desc),
        VMSTATE_UINT32(first_cmd_type_len, struct igb_tx),
        VMSTATE_UINT32(first_olinfo_status, struct igb_tx),
        VMSTATE_BOOL(first, struct igb_tx),
        VMSTATE_BOOL(skip_cp, struct igb_tx),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription i226_vmstate_intr_timer = {
    .name = "i226-intr-timer",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_TIMER_PTR(timer, IGBIntrDelayTimer),
        VMSTATE_BOOL(running, IGBIntrDelayTimer),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription i226_vmstate_mmd = {
    .name = "i226-mmd",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT16(device, I226MmdEntry),
        VMSTATE_UINT16(address, I226MmdEntry),
        VMSTATE_UINT16(value, I226MmdEntry),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription i226_vmstate = {
    .name = "i226-v",
    .version_id = 4,
    .minimum_version_id = 1,
    .pre_save = i226_pre_save,
    .post_load = i226_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_PCI_DEVICE(parent_obj, I226VState),
        VMSTATE_MSIX(parent_obj, I226VState),
        VMSTATE_UINT8(core.rx_desc_len, I226VState),
        VMSTATE_UINT16_ARRAY(core.eeprom, I226VState, IGB_EEPROM_SIZE),
        VMSTATE_UINT16_ARRAY(core.phy, I226VState, MAX_PHY_REG_ADDRESS + 1),
        VMSTATE_UINT32_ARRAY(core.mac, I226VState, E1000E_MAC_SIZE),
        VMSTATE_UINT8_ARRAY(core.permanent_mac, I226VState, ETH_ALEN),
        VMSTATE_STRUCT_ARRAY(core.eitr, I226VState, IGB_INTR_NUM, 0,
                             i226_vmstate_intr_timer, IGBIntrDelayTimer),
        VMSTATE_UINT32_ARRAY(core.eitr_guest_value, I226VState, IGB_INTR_NUM),
        VMSTATE_STRUCT_ARRAY(core.tx, I226VState, IGB_NUM_QUEUES, 0,
                             i226_vmstate_tx, struct igb_tx),
        VMSTATE_INT64(core.timadj, I226VState),
        VMSTATE_UINT32(eewr, I226VState),
        VMSTATE_UINT32(phpm, I226VState),
        VMSTATE_UINT32(eeer, I226VState),
        VMSTATE_UINT32(eee_su, I226VState),
        VMSTATE_UINT32(ipcnfg, I226VState),
        VMSTATE_UINT16(mmd_control, I226VState),
        VMSTATE_UINT16(mmd_address, I226VState),
        VMSTATE_UINT16(mmd_count, I226VState),
        VMSTATE_STRUCT_ARRAY(mmd, I226VState, I226_MMD_SLOTS, 0,
                             i226_vmstate_mmd, I226MmdEntry),
        VMSTATE_UINT32_V(ltrc, I226VState, 3),
        VMSTATE_UINT32_V(dmacr, I226VState, 3),
        VMSTATE_UINT32_V(wufc_ext, I226VState, 3),
        VMSTATE_UINT32_V(wus_ext, I226VState, 3),
        VMSTATE_UINT32_V(ltrminv, I226VState, 3),
        VMSTATE_UINT32_V(ltrmaxv, I226VState, 3),
        VMSTATE_UINT32_V(rpthc, I226VState, 4),
        VMSTATE_UINT32_V(hgptc, I226VState, 4),
        VMSTATE_END_OF_LIST()
    }
};

static const Property i226_properties[] = {
    DEFINE_NIC_PROPERTIES(I226VState, conf),
    DEFINE_PROP_BOOL("x-pcie-flr-init", I226VState, has_flr, true),
};

static void i226_class_init(ObjectClass *class, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(class);
    ResettableClass *rc = RESETTABLE_CLASS(class);
    PCIDeviceClass *pc = PCI_DEVICE_CLASS(class);

    pc->realize = i226_pci_realize;
    pc->exit = i226_pci_uninit;
    pc->vendor_id = PCI_VENDOR_ID_INTEL;
    pc->device_id = I226_PCI_DEVICE_ID;
    pc->revision = I226_PCI_REVISION;
    pc->class_id = PCI_CLASS_NETWORK_ETHERNET;
    pc->subsystem_vendor_id = I226_SUBSYSTEM_VENDOR_ID;
    pc->subsystem_id = I226_SUBSYSTEM_ID;
    rc->phases.hold = i226_reset_hold;
    dc->desc = "Intel Ethernet Controller I226-V";
    dc->vmsd = &i226_vmstate;
    device_class_set_props(dc, i226_properties);
    set_bit(DEVICE_CATEGORY_NETWORK, dc->categories);
}

static void i226_instance_init(Object *obj)
{
    I226VState *s = I226_V(obj);
    device_add_bootindex_property(obj, &s->conf.bootindex, "bootindex",
                                  "/ethernet-phy@0", DEVICE(obj));
}

static const TypeInfo i226_info = {
    .name = TYPE_I226_V,
    .parent = TYPE_PCI_DEVICE,
    .instance_size = sizeof(I226VState),
    .class_init = i226_class_init,
    .instance_init = i226_instance_init,
    .interfaces = (const InterfaceInfo[]) {
        { INTERFACE_PCIE_DEVICE },
        { }
    },
};

static void i226_register_types(void)
{
    type_register_static(&i226_info);
}

type_init(i226_register_types)
'''


def patch_i226_device(source: Path, profile: dict) -> None:
    if re.search(r"ovo", I226_SOURCE, re.IGNORECASE):
        raise RuntimeError("I226-V 设备源码中不应出现项目标识")
    network = profile.get("hardware", {}).get("network", {})
    expected = {
        "model": "i226-v",
        "manufacturer": "Intel Corporation",
        "product": "Ethernet Controller I226-V",
        "pci_vendor_id": "8086",
        "pci_device_id": "125c",
        "revision_id": "04",
        "subsystem_vendor_id": "8086",
        "subsystem_device_id": "0000",
        "pcie_spec_version": "3.1",
        "pcie_max_link_speed_gtps": "5.0",
        "pcie_link_width": "1",
        "link_speed_mbps": "2500",
        "rx_queues": "4",
        "tx_queues": "4",
        "msix_vectors": "5",
        "option_rom_size_kib": "1024",
        "phy_id": "67c9dc01",
        "firmware_version": "2017:888d",
    }
    for key, value in expected.items():
        if str(network.get(key, "")).lower() != value.lower():
            raise RuntimeError(f"I226-V 身份字段不一致: hardware.network.{key}")
    expected_caps = ["aer-v2", "dsn", "ltr", "l1-pm-substates", "ptm"]
    if network.get("extended_capabilities") != expected_caps:
        raise RuntimeError("I226-V PCIe 扩展能力链与身份文件不一致")

    net_dir = source / "hw/net"
    target = net_dir / "i226.c"
    if target.exists():
        raise RuntimeError("原版 QEMU 源码中意外存在 hw/net/i226.c")
    target.write_text(I226_SOURCE, encoding="ascii")

    kconfig = net_dir / "Kconfig"
    marker = "config IGB_PCI_EXPRESS\n"
    addition = (
        "config I226_PCI_EXPRESS\n"
        "    bool\n"
        "    default y if PCI_DEVICES || PCIE_DEVICES\n"
        "    depends on PCI_EXPRESS && MSI_NONBROKEN\n"
        "    select IGB_PCI_EXPRESS\n\n"
    )
    text = kconfig.read_text(encoding="utf-8")
    if text.count(marker) != 1:
        raise RuntimeError("QEMU network Kconfig insertion point not found")
    kconfig.write_text(text.replace(marker, addition + marker, 1), encoding="utf-8")

    meson = net_dir / "meson.build"
    marker = "system_ss.add(when: 'CONFIG_IGB_PCI_EXPRESS', if_true: files('net_tx_pkt.c', 'net_rx_pkt.c'))\n"
    addition = "system_ss.add(when: 'CONFIG_I226_PCI_EXPRESS', if_true: files('i226.c'))\n"
    text = meson.read_text(encoding="utf-8")
    if text.count(marker) != 1:
        raise RuntimeError("QEMU network Meson insertion point not found")
    meson.write_text(text.replace(marker, addition + marker, 1), encoding="utf-8")


def patch_device_identity(source: Path, profile: dict) -> None:
    storage = profile["storage"]["devices"]
    disk = next((item["identity"] for item in storage if item["device"] != "cdrom"), {
        "vendor": "ATA", "product": "Samsung SSD 870 EVO", "model": "Samsung SSD 870 EVO", "firmware": "SVT02B6Q",
    })
    optical = next((item["identity"] for item in storage if item["device"] == "cdrom"), {
        "vendor": "HL-DT-ST", "product": "DVDRAM GUD1N", "model": "HL-DT-ST DVDRAM GUD1N", "firmware": "1.00",
    })
    scsi_disk = next((
        item["identity"] for item in storage
        if item["device"] != "cdrom" and str(item["bus"]).lower() in {"scsi", "sas"}
    ), disk)
    disk_model = c_string(disk["model"][:40])
    disk_firmware = c_string(disk["firmware"][:8])
    optical_model = c_string(optical["model"][:40])
    optical_product = c_string(optical["product"][:16])
    optical_vendor = c_string(optical["vendor"][:8])
    optical_firmware = c_string(optical["firmware"][:8])
    scsi_vendor = c_string(scsi_disk["vendor"][:8])
    scsi_product = c_string(scsi_disk["product"][:16])
    scsi_firmware = c_string(scsi_disk["firmware"][:4])

    ide = source / "hw/ide/core.c"
    replace_literal(ide, 'strcpy(s->drive_model_str, "QEMU DVD-ROM");', f'strcpy(s->drive_model_str, "{optical_model}");', "IDE optical model")
    replace_literal(ide, 'strcpy(s->drive_model_str, "QEMU MICRODRIVE");', 'strcpy(s->drive_model_str, "SanDisk SDCFH-008G");', "IDE CF model")
    replace_literal(ide, 'strcpy(s->drive_model_str, "QEMU HARDDISK");', f'strcpy(s->drive_model_str, "{disk_model}");', "IDE disk model")
    replace_literal(
        ide,
        'pstrcpy(s->version, sizeof(s->version), QEMU_HW_VERSION);',
        f'pstrcpy(s->version, sizeof(s->version), kind == IDE_CD ? "{optical_firmware}" : "{disk_firmware}");',
        "IDE firmware revision",
    )

    atapi = source / "hw/ide/atapi.c"
    replace_literal(atapi, 'padstr8(buf + 8, 8, "QEMU");', f'padstr8(buf + 8, 8, "{optical_vendor}");', "ATAPI vendor")
    replace_literal(atapi, 'padstr8(buf + 16, 16, "QEMU DVD-ROM");', f'padstr8(buf + 16, 16, "{optical_product}");', "ATAPI product")

    scsi = source / "hw/scsi/scsi-disk.c"
    replace_literal(
        scsi,
        's->version = g_strdup(QEMU_HW_VERSION);',
        f's->version = g_strdup(dev->type == TYPE_ROM ? "{optical_firmware[:4]}" : "{scsi_firmware}");',
        "SCSI firmware revision",
    )
    replace_literal(scsi, 's->vendor = g_strdup("QEMU");', f's->vendor = g_strdup(dev->type == TYPE_ROM ? "{optical_vendor}" : "{scsi_vendor}");', "SCSI vendor")
    replace_literal(scsi, 's->product = g_strdup("QEMU HARDDISK");', f's->product = g_strdup("{scsi_product}");', "SCSI disk product")
    replace_literal(scsi, 's->product = g_strdup("QEMU CD-ROM");', f's->product = g_strdup("{optical_product}");', "SCSI optical product")

    hid = source / "hw/usb/dev-hid.c"
    policy = profile["qemu_policy"]["usb_hid"]
    hid_text = hid.read_text(encoding="utf-8")
    replacements = {
        '[STR_MANUFACTURER]     = "QEMU",': f'[STR_MANUFACTURER]     = "{c_string(policy["manufacturer"])}",',
        '[STR_PRODUCT_MOUSE]    = "QEMU USB Mouse",': f'[STR_PRODUCT_MOUSE]    = "{c_string(policy["mouse_product"])}",',
        '[STR_PRODUCT_KEYBOARD] = "QEMU USB Keyboard",': f'[STR_PRODUCT_KEYBOARD] = "{c_string(policy["keyboard_product"])}",',
        '[STR_SERIAL_MOUSE]     = "89126",': f'[STR_SERIAL_MOUSE]     = "{c_string(policy["mouse_serial"])}",',
        '[STR_SERIAL_KEYBOARD]  = "68284",': f'[STR_SERIAL_KEYBOARD]  = "{c_string(policy["keyboard_serial"])}",',
        'uc->product_desc   = "QEMU USB Mouse";': f'uc->product_desc   = "{c_string(policy["mouse_product"])}";',
        'uc->product_desc   = "QEMU USB Keyboard";': f'uc->product_desc   = "{c_string(policy["keyboard_product"])}";',
    }
    for old, new in replacements.items():
        if hid_text.count(old) != 1:
            raise RuntimeError(f"USB HID identity marker expected once: {old}")
        hid_text = hid_text.replace(old, new, 1)
    for descriptor, kind in (("desc_mouse", "mouse"), ("desc_mouse2", "mouse"), ("desc_keyboard", "keyboard"), ("desc_keyboard2", "keyboard")):
        pattern = rf'(static const USBDesc {descriptor} = \{{.*?\.idVendor\s*=\s*)0x0627(,.*?\.idProduct\s*=\s*)0x0001(,)'
        replacement = rf'\g<1>0x{policy[f"{kind}_vendor_id"]}\g<2>0x{policy[f"{kind}_product_id"]}\g<3>'
        hid_text, count = re.subn(pattern, replacement, hid_text, count=1, flags=re.DOTALL)
        if count != 1:
            raise RuntimeError(f"USB HID descriptor marker not found: {descriptor}")
    hid.write_text(hid_text, encoding="utf-8")
    patch_usb_serial_identity(source)

    audio = profile["hardware"]["audio"]
    codec_id = int(audio["codec_vendor_device_id"], 16)
    subsystem_id = int(audio["codec_subsystem_id"], 16)
    revision_id = int(audio["codec_revision_id"], 16)
    codec = source / "hw/audio/hda-codec.c"
    replace_literal(
        codec,
        "#define QEMU_HDA_ID_VENDOR  0x1af4",
        f"#define QEMU_HDA_ID_VENDOR  0x{codec_id >> 16:04x}\n"
        f"#define OVO_HDA_CODEC_ID    0x{codec_id:08x}\n"
        f"#define OVO_HDA_SUBSYS_ID   0x{subsystem_id:08x}\n"
        f"#define OVO_HDA_REVISION_ID 0x{revision_id:08x}",
        "HDA codec identity constants",
    )
    common = source / "hw/audio/hda-codec-common.h"
    common_text = common.read_text(encoding="utf-8")
    common_text, codec_count = re.subn(
        r'#define QEMU_HDA_ID_(?:OUTPUT|DUPLEX|MICRO)\s+\(\(QEMU_HDA_ID_VENDOR << 16\) \| 0x(?:11|12|21|22|31|32)\)',
        lambda match: match.group(0).split()[0] + " " + match.group(0).split()[1] + "  OVO_HDA_CODEC_ID",
        common_text,
    )
    if codec_count != 6:
        raise RuntimeError(f"HDA codec ID markers: expected 6 matches, got {codec_count}")
    common_text, subsystem_count = re.subn(
        r'(\.id\s*=\s*AC_PAR_SUBSYSTEM_ID,\n\s*\.val\s*=\s*)QEMU_HDA_ID_(?:OUTPUT|DUPLEX|MICRO)',
        r'\g<1>OVO_HDA_SUBSYS_ID',
        common_text,
    )
    if subsystem_count != 6:
        raise RuntimeError(f"HDA subsystem ID markers: expected 6 matches, got {subsystem_count}")
    common_text, iid_count = re.subn(
        r'(\.iid\s*=\s*)QEMU_HDA_ID_(?:OUTPUT|DUPLEX|MICRO)',
        r'\g<1>OVO_HDA_SUBSYS_ID',
        common_text,
    )
    if iid_count != 3:
        raise RuntimeError(f"HDA codec IID markers: expected 3 matches, got {iid_count}")
    common_text, revision_count = re.subn(
        r'(\.id\s*=\s*AC_PAR_REV_ID,\n\s*\.val\s*=\s*)0x00100101',
        r'\g<1>OVO_HDA_REVISION_ID',
        common_text,
    )
    if revision_count != 3:
        raise RuntimeError(f"HDA revision markers: expected 3 matches, got {revision_count}")
    common.write_text(common_text, encoding="utf-8")
    patch_hda_codec_graph(source, profile)


def patch_sata_capabilities(source: Path, profile: dict) -> None:
    policy = profile.get("hardware", {}).get("storage_controller", {})
    try:
        generation = int(policy["sata_generation"])
        ata_major = int(policy["ata_major_version"])
        udma_mode = int(policy["udma_mode"])
        ncq = bool(policy["ncq"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 SATA/AHCI 能力配置") from exc
    if generation not in {1, 2, 3} or not 3 <= ata_major <= 8 or not 0 <= udma_mode <= 6:
        raise RuntimeError("SATA generation / ATA major / UDMA mode 超出当前后端范围")
    internal = source / "hw/ide/ahci-internal.h"
    text = internal.read_text(encoding="utf-8")
    marker = "#define SATA_SCR_SSTATUS_SPD_GEN1         0x10\n"
    replacement = marker + "#define SATA_SCR_SSTATUS_SPD_PROFILE      " + hex(generation << 4) + "\n"
    if text.count(marker) != 1:
        raise RuntimeError("AHCI SStatus speed marker not found")
    internal.write_text(text.replace(marker, replacement, 1), encoding="utf-8")

    ahci = source / "hw/ide/ahci.c"
    replace_literal(
        ahci, "SATA_SCR_SSTATUS_SPD_GEN1 | SATA_SCR_SSTATUS_IPM_ACTIVE;",
        "SATA_SCR_SSTATUS_SPD_PROFILE | SATA_SCR_SSTATUS_IPM_ACTIVE;",
        "AHCI negotiated SATA speed",
    )
    cap_ncq = "HOST_CAP_NCQ | " if ncq else ""
    replace_literal(
        ahci,
        "(AHCI_SUPPORTED_SPEED_GEN1 << AHCI_SUPPORTED_SPEED) |\n"
        "                          HOST_CAP_NCQ | HOST_CAP_AHCI | HOST_CAP_64;",
        f"({generation} << AHCI_SUPPORTED_SPEED) |\n"
        f"                          {cap_ncq}HOST_CAP_AHCI | HOST_CAP_64;",
        "AHCI CAP interface speed",
    )

    ide = source / "hw/ide/core.c"
    major_mask = sum(1 << version for version in range(3, ata_major + 1))
    udma_supported = (1 << (udma_mode + 1)) - 1
    udma_word = udma_supported | (1 << (8 + udma_mode))
    replace_literal(ide, "put_le16(p + 80, 0xf0); /* ata3 -> ata6 supported */",
                    f"put_le16(p + 80, 0x{major_mask:04x}); /* ATA3 -> ATA{ata_major} supported */",
                    "ATA major version")
    replace_literal(ide, "put_le16(p + 81, 0x16); /* conforms to ata5 */",
                    "put_le16(p + 81, 0x0000); /* ATA minor revision not reported */",
                    "ATA minor revision identity")
    text = ide.read_text(encoding="utf-8")
    old = "put_le16(p + 88, 0x3f | (1 << 13)); /* udma5 set and supported */"
    count = text.count(old)
    if count < 1:
        raise RuntimeError("ATA UDMA capability marker not found")
    ide.write_text(text.replace(old, f"put_le16(p + 88, 0x{udma_word:04x}); /* profiled UDMA mode */"), encoding="utf-8")


def profile_hda_pins(profile: dict) -> tuple[list[dict], list[dict]]:
    audio = profile.get("hardware", {}).get("audio") or {}
    pins = audio.get("codec_pins") or []
    if not isinstance(pins, list) or not pins:
        raise RuntimeError("身份文件缺少板载 HDA 针脚")
    playback = [pin for pin in pins if pin.get("direction") == "playback"]
    capture = [pin for pin in pins if pin.get("direction") == "capture"]
    if not playback:
        raise RuntimeError("板载 HDA 没有播放针脚，当前 ich9-hda 后端无法承载")
    if len(playback) > 8 or len(capture) > 8:
        raise RuntimeError("板载 HDA 针脚数量超出当前 codec 图上限")
    for pin in playback + capture:
        try:
            int(pin["config"], 16)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("板载 HDA 针脚 config 无效") from exc
    return playback, capture


def hda_pin_node_name(pin: dict, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "", str(pin.get("device", "pin")).lower()) or "pin"
    name = base
    index = 0
    while name in used:
        index += 1
        name = f"{base}{index}"
    used.add(name)
    return name


def render_hda_pin_nodes(playback: list[dict], capture: list[dict], *, include_capture: bool) -> tuple[str, int]:
    used = {"root", "func", "dac", "adc", "out", "in"}
    widgets: list[str] = []
    nid = 2
    stream = 0
    last_dac = None
    for index, pin in enumerate(playback):
        if stream < 4:
            last_dac = nid
            widgets.append(
                "    {\n"
                f"        .nid     = {nid},\n"
                f'        .name    = "dac{index}",\n'
                "        .params  = glue(common_params_audio_dac_, PARAM),\n"
                "        .nparams = ARRAY_SIZE(glue(common_params_audio_dac_, PARAM)),\n"
                f"        .stindex = {stream},\n"
                "    }"
            )
            nid += 1
            stream += 1
        name = hda_pin_node_name(pin, used)
        config = int(pin["config"], 16)
        widgets.append(
            "    {\n"
            f"        .nid     = {nid},\n"
            f'        .name    = "{name}",\n'
            "        .params  = glue(common_params_audio_lineout_, PARAM),\n"
            "        .nparams = ARRAY_SIZE(glue(common_params_audio_lineout_, PARAM)),\n"
            f"        .config  = 0x{config:08x},\n"
            "        .pinctl  = AC_PINCTL_OUT_EN,\n"
            f"        .conn    = (uint32_t[]) {{ {last_dac} }},\n"
            "    }"
        )
        nid += 1
    if include_capture:
        last_adc = None
        last_input = None
        for index, pin in enumerate(capture):
            name = hda_pin_node_name(pin, used)
            config = int(pin["config"], 16)
            pin_nid = nid + (1 if stream < 4 else 0)
            if stream < 4:
                last_adc = nid
                last_input = nid + 1
                widgets.append(
                    "    {\n"
                    f"        .nid     = {nid},\n"
                    f'        .name    = "adc{index}",\n'
                    "        .params  = glue(common_params_audio_adc_, PARAM),\n"
                    "        .nparams = ARRAY_SIZE(glue(common_params_audio_adc_, PARAM)),\n"
                    f"        .stindex = {stream},\n"
                    f"        .conn    = (uint32_t[]) {{ {last_input} }},\n"
                    "    }"
                )
                nid += 1
                stream += 1
            else:
                last_input = last_input or nid
            widgets.append(
                "    {\n"
                f"        .nid     = {nid},\n"
                f'        .name    = "{name}",\n'
                "        .params  = glue(common_params_audio_linein_, PARAM),\n"
                "        .nparams = ARRAY_SIZE(glue(common_params_audio_linein_, PARAM)),\n"
                f"        .config  = 0x{config:08x},\n"
                "        .pinctl  = AC_PINCTL_IN_EN,\n"
                "    }"
            )
            nid += 1
            if last_adc is None:
                raise RuntimeError("板载 HDA 采集针脚缺少可用 ADC stream")
    count = nid - 2
    return ",\n".join(widgets), count


def patch_hda_codec_graph(source: Path, profile: dict) -> None:
    """Replace QEMU's desktop Line Out/Line In pins with onboard pin defaults."""
    playback, capture = profile_hda_pins(profile)
    common = source / "hw/audio/hda-codec-common.h"
    text = common.read_text(encoding="utf-8")
    duplex_widgets, duplex_count = render_hda_pin_nodes(playback, capture, include_capture=True)
    output_widgets, output_count = render_hda_pin_nodes(playback, capture, include_capture=False)

    def replace_graph(source_text: str, kind: str, widgets: str, count: int, old_count: str) -> str:
        node_marker = f"/* {kind}: nodes */"
        start = source_text.find(node_marker)
        if start < 0:
            raise RuntimeError(f"HDA {kind} node marker not found")
        dac_start = source_text.find("        .nid     = 2,", start)
        array_end = source_text.find("\n};", dac_start)
        cut = source_text.rfind("},{", start, dac_start)
        if dac_start < 0 or array_end < 0 or cut < 0:
            raise RuntimeError(f"HDA {kind} node array not found")
        rebuilt = source_text[: cut + 1] + ",\n" + widgets + source_text[array_end:]
        count_marker = (
            f"/* {kind}: audio function */\n"
            f"static const desc_param glue({kind}_params_audio_func_, PARAM)[] = {{"
        )
        if count_marker not in rebuilt:
            raise RuntimeError(f"HDA {kind} function marker not found")
        old = f"        .id  = AC_PAR_NODE_COUNT,\n        .val = {old_count},"
        new = f"        .id  = AC_PAR_NODE_COUNT,\n        .val = 0x{((2 << 16) | count):08x},"
        func_start = rebuilt.find(count_marker)
        func_end = rebuilt.find("};", func_start)
        section = rebuilt[func_start:func_end]
        if old not in section:
            raise RuntimeError(f"HDA {kind} NODE_COUNT marker not found")
        section = section.replace(old, new, 1)
        return rebuilt[:func_start] + section + rebuilt[func_end:]

    text = replace_graph(text, "duplex", duplex_widgets, duplex_count, "0x00020004")
    text = replace_graph(text, "output", output_widgets, output_count, "0x00020002")
    if "0x" + playback[0]["config"].lower() not in text.lower() and f"0x{int(playback[0]['config'], 16):08x}" not in text:
        raise RuntimeError("板载 HDA 播放针脚没有写入 codec 图")
    common.write_text(text, encoding="utf-8")


def patch_usb_serial_identity(source: Path) -> None:
    """Stop embedding the xHCI PCI BDF into USB iSerialNumber strings."""
    desc = source / "hw/usb/desc.c"
    replace_literal(
        desc,
        """void usb_desc_create_serial(USBDevice *dev)
{
    DeviceState *hcd = dev->qdev.parent_bus->parent;
    const USBDesc *desc = usb_device_get_usb_desc(dev);
    int index = desc->id.iSerialNumber;
    char *path, *serial;

    if (dev->serial) {
        /* 'serial' usb bus property has priority if present */
        usb_desc_set_string(dev, index, dev->serial);
        return;
    }

    assert(index != 0 && desc->str[index] != NULL);
    path = qdev_get_dev_path(hcd);
    if (path) {
        serial = g_strdup_printf("%s-%s-%s", desc->str[index],
                                 path, dev->port->path);
    } else {
        serial = g_strdup_printf("%s-%s", desc->str[index], dev->port->path);
    }
    usb_desc_set_string(dev, index, serial);
    g_free(path);
    g_free(serial);
}
""",
        """void usb_desc_create_serial(USBDevice *dev)
{
    const USBDesc *desc = usb_device_get_usb_desc(dev);
    int index = desc->id.iSerialNumber;

    if (dev->serial) {
        /* 'serial' usb bus property has priority if present */
        usb_desc_set_string(dev, index, dev->serial);
        return;
    }

    assert(index != 0 && desc->str[index] != NULL);
    /* Device-specific serial only.  Concatenating the host-controller PCI
     * path produces QEMU-unique strings such as "89126-0000:00:14.0-1". */
    usb_desc_set_string(dev, index, desc->str[index]);
}
""",
        "USB serial without PCI path",
    )


def patch_pci_subsystem_identity(source: Path, profile: dict) -> None:
    identities = profile.get("devices", {}).get("pci_subsystems", {})
    primary = profile.get("devices", {}).get("pci_identities", {})

    def pair(name: str) -> tuple[int, int]:
        item = identities.get(name, {})
        try:
            vendor = int(item.get("vendor_id", ""), 16)
            device = int(item.get("device_id", ""), 16)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"硬件组合缺少 {name} PCI subsystem") from exc
        if vendor in {0, 0xFFFF} or device in {0, 0xFFFF}:
            raise RuntimeError(f"硬件组合包含无效的 {name} PCI subsystem")
        return vendor, device

    def primary_tuple(name: str) -> tuple[int, int, int, str]:
        item = primary.get(name, {})
        try:
            vendor = int(item["vendor_id"], 16)
            device = int(item["device_id"], 16)
            revision = int(item["revision_id"], 16)
            description = str(item["description"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"硬件组合缺少 {name} 主 PCI 身份") from exc
        if vendor in {0, 0xFFFF} or device in {0, 0xFFFF} or not (0 <= revision <= 0xFF):
            raise RuntimeError(f"硬件组合包含无效的 {name} 主 PCI 身份")
        if not description or any(ord(ch) < 0x20 or ord(ch) > 0x7e for ch in description):
            raise RuntimeError(f"硬件组合包含无效的 {name} 描述")
        return vendor, device, revision, description

    targets = (
        ("host_bridge", source / "hw/pci-host/q35.c", "    k->vendor_id = PCI_VENDOR_ID_INTEL;"),
        ("lpc", source / "hw/isa/lpc_ich9.c", "    k->vendor_id = PCI_VENDOR_ID_INTEL;"),
        ("sata", source / "hw/ide/ich.c", "    k->vendor_id = PCI_VENDOR_ID_INTEL;"),
        ("smbus", source / "hw/i2c/smbus_ich9.c", "    k->vendor_id = PCI_VENDOR_ID_INTEL;"),
        ("usb", source / "hw/usb/hcd-xhci-pci.c", "    k->vendor_id    = PCI_VENDOR_ID_REDHAT;"),
    )
    for name, path, marker in targets:
        vendor, device = pair(name)
        replacement = (
            marker
            + f"\n    k->subsystem_vendor_id = 0x{vendor:04x};"
            + f"\n    k->subsystem_id = 0x{device:04x};"
        )
        replace_literal(path, marker, replacement, f"{name} PCI subsystem")

    # Replace the primary southbridge tuple as well; subsystem-only spoofing
    # leaves an impossible modern-OEM/ICH9 combination in the guest.
    primary_targets = (
        ("host_bridge", source / "hw/pci-host/q35.c", "PCI_VENDOR_ID_INTEL", "PCI_DEVICE_ID_INTEL_P35_MCH", "MCH_HOST_BRIDGE_REVISION_DEFAULT", "Host bridge"),
        ("lpc", source / "hw/isa/lpc_ich9.c", "PCI_VENDOR_ID_INTEL", "PCI_DEVICE_ID_INTEL_ICH9_8", "ICH9_A2_LPC_REVISION", "ICH9 LPC bridge"),
        ("sata", source / "hw/ide/ich.c", "PCI_VENDOR_ID_INTEL", "PCI_DEVICE_ID_INTEL_82801IR", "0x02", None),
        ("smbus", source / "hw/i2c/smbus_ich9.c", "PCI_VENDOR_ID_INTEL", "PCI_DEVICE_ID_INTEL_ICH9_6", "ICH9_A2_SMB_REVISION", "ICH9 SMBUS Bridge"),
    )
    for name, path, vendor_expr, device_expr, revision_expr, old_desc in primary_targets:
        vendor, device, revision, description = primary_tuple(name)
        text = path.read_text(encoding="utf-8")
        text, count = re.subn(rf"k->vendor_id\s*=\s*[^;]+;", f"k->vendor_id = 0x{vendor:04x};", text, count=1)
        if count != 1:
            raise RuntimeError(f"{name} 主 Vendor ID 标记不存在")
        text, count = re.subn(rf"k->device_id\s*=\s*{re.escape(device_expr)};", f"k->device_id = 0x{device:04x};", text, count=1)
        if count != 1:
            raise RuntimeError(f"{name} 主 Device ID 标记不存在")
        text, count = re.subn(rf"k->revision\s*=\s*{re.escape(revision_expr)};", f"k->revision = 0x{revision:02x};", text, count=1)
        if count != 1:
            raise RuntimeError(f"{name} 主 Revision 标记不存在")
        if old_desc is not None:
            text, count = re.subn(rf'dc->desc\s*=\s*"{re.escape(old_desc)}";', f'dc->desc = "{c_string(description)}";', text, count=1)
            if count != 1:
                raise RuntimeError(f"{name} 设备描述标记不存在")
        elif name == "sata":
            text, count = re.subn(r"(k->class_id\s*=\s*PCI_CLASS_STORAGE_SATA;)" , r'\1\n    dc->desc = "' + c_string(description) + r'";', text, count=1)
            if count != 1:
                raise RuntimeError("sata 设备描述插入点不存在")
        path.write_text(text, encoding="utf-8")

    machine_desc = profile.get("hardware", {}).get("machine", {}).get("description")
    if not machine_desc:
        platform = profile.get("hardware", {}).get("platform", {})
        machine_desc = f"{platform.get('vendor', 'PC')} {platform.get('product', 'Platform')}"
    pc_q35 = source / "hw/i386/pc_q35.c"
    replace_literal(pc_q35, 'm->desc = "Standard PC (Q35 + ICH9, 2009)";', f'm->desc = "{c_string(machine_desc)}";', "Q35 machine description")

    _, host_bridge_device, _, _ = primary_tuple("host_bridge")
    replace_literal(
        source / "include/hw/pci/pci_ids.h",
        "#define PCI_DEVICE_ID_INTEL_P35_MCH      0x29c0",
        f"#define PCI_DEVICE_ID_INTEL_P35_MCH      0x{host_bridge_device:04x}",
        "P35 MCH DID macro",
    )
    patch_southbridge_devfn(source, profile)

    visible_sources = (
        source / "hw/pci-host/q35.c",
        source / "hw/isa/lpc_ich9.c",
        source / "hw/i2c/smbus_ich9.c",
        source / "hw/ide/ich.c",
        source / "hw/i386/pc_q35.c",
        source / "include/hw/pci/pci_ids.h",
    )
    visible_text = "\n".join(path.read_text(encoding="utf-8") for path in visible_sources)
    forbidden = (
        'Standard PC (Q35 + ICH9, 2009)',
        'ICH9 LPC bridge',
        'ICH9 SMBUS Bridge',
        "PCI_DEVICE_ID_INTEL_P35_MCH      0x29c0",
    )
    leaked = [marker for marker in forbidden if marker in visible_text]
    if leaked:
        raise RuntimeError("南桥可见身份仍残留旧 ICH9/Q35 描述: " + ", ".join(leaked))
    if f"k->device_id = 0x{host_bridge_device:04x};" not in (source / "hw/pci-host/q35.c").read_text(encoding="utf-8"):
        raise RuntimeError("QEMU Host Bridge DID 没有写入 q35.c")

    vga_id = primary.get("vga") or {}
    try:
        vga_vendor = int(vga_id["vendor_id"], 16)
        vga_device = int(vga_id["device_id"], 16)
        vga_revision = int(vga_id["revision_id"], 16)
        vga_sub_vendor = int(vga_id["subsystem_vendor_id"], 16)
        vga_sub_device = int(vga_id["subsystem_device_id"], 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 VGA PCI 身份") from exc
    if vga_id.get("source") != "qemu-stdvga-temporary":
        raise RuntimeError("临时 VGA 必须使用 qemu-stdvga-temporary 身份")
    if (vga_vendor, vga_device) != (0x1234, 0x1111):
        raise RuntimeError("临时 QEMU VGA 必须保持 1234:1111，不能套用真实 GPU ID")
    vga = source / "hw/display/vga-pci.c"
    replace_literal(
        vga,
        "    k->vendor_id = PCI_VENDOR_ID_QEMU;\n"
        "    k->device_id = PCI_DEVICE_ID_QEMU_VGA;",
        f"    k->vendor_id = 0x{vga_vendor:04x};\n"
        f"    k->device_id = 0x{vga_device:04x};\n"
        f"    k->revision = 0x{vga_revision:02x};\n"
        f"    k->subsystem_vendor_id = 0x{vga_sub_vendor:04x};\n"
        f"    k->subsystem_id = 0x{vga_sub_device:04x};",
        "VGA PCI identity",
    )

    audio_vendor, audio_device = pair("audio")
    audio = profile["hardware"]["audio"]
    hda = source / "hw/audio/intel-hda.c"
    hda_marker = "    k->vendor_id = PCI_VENDOR_ID_INTEL;"
    replace_literal(
        hda,
        hda_marker,
        f"    k->vendor_id = 0x{int(audio['controller_vendor_id'], 16):04x};"
        + f"\n    k->subsystem_vendor_id = 0x{audio_vendor:04x};"
        + f"\n    k->subsystem_id = 0x{audio_device:04x};",
        "HDA controller PCI subsystem",
    )
    replace_literal(hda, "    k->device_id = 0x293e;", f"    k->device_id = 0x{int(audio['controller_device_id'], 16):04x};", "HDA controller device ID")
    replace_literal(hda, "    k->revision = 3;", f"    k->revision = 0x{int(audio['controller_revision_id'], 16):02x};", "HDA controller revision")
    replace_literal(hda, '    dc->desc = "Intel HD Audio Controller (ich9)";', f'    dc->desc = "{c_string(audio["controller_product"])}";', "HDA controller description")

    # Any optional conventional PCI endpoint without a class-specific
    # subsystem identity must inherit the platform OEM, never QEMU's global
    # 1af4:1100 sentinel.
    default_vendor, default_device = pair("host_bridge")
    pci = source / "hw/pci/pci.c"
    replace_literal(
        pci,
        "static uint16_t pci_default_sub_vendor_id = PCI_SUBVENDOR_ID_REDHAT_QUMRANET;\n"
        "static uint16_t pci_default_sub_device_id = PCI_SUBDEVICE_ID_QEMU;",
        f"static uint16_t pci_default_sub_vendor_id = 0x{default_vendor:04x};\n"
        f"static uint16_t pci_default_sub_device_id = 0x{default_device:04x};",
        "default PCI subsystem identity",
    )

    root_port = primary.get("root_port") or {}
    root_ports = primary.get("root_ports") or []
    try:
        rp_vendor = int(root_port["vendor_id"], 16)
        rp_device = int(root_port["device_id"], 16)
        rp_revision = int(root_port["revision_id"], 16)
        rp_sub_vendor = int(root_port["subsystem_vendor_id"], 16)
        rp_sub_device = int(root_port["subsystem_device_id"], 16)
        rp_desc = str(root_port["description"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 Root Port PCI 身份") from exc
    if not root_ports:
        raise RuntimeError("身份文件缺少按实例映射的 Root Port 列表")
    ioh = source / "hw/pci-bridge/ioh3420.c"
    replace_once(ioh, r'^#define IOH_EP_SSVID_SVID\s+PCI_VENDOR_ID_INTEL', f'#define IOH_EP_SSVID_SVID               0x{rp_sub_vendor:04x}', "root-port subsystem vendor")
    replace_once(ioh, r'^#define IOH_EP_SSVID_SSID\s+0$', f'#define IOH_EP_SSVID_SSID               0x{rp_sub_device:04x}', "root-port subsystem device")
    replace_literal(
        ioh,
        "    k->vendor_id = PCI_VENDOR_ID_INTEL;\n"
        "    k->device_id = PCI_DEVICE_ID_IOH_EPORT;\n"
        "    k->revision = PCI_DEVICE_ID_IOH_REV;\n"
        '    dc->desc = "Intel IOH device id 3420 PCIE Root Port";',
        f"    k->vendor_id = 0x{rp_vendor:04x};\n"
        f"    k->device_id = 0x{rp_device:04x};\n"
        f"    k->revision = 0x{rp_revision:02x};\n"
        f'    dc->desc = "{c_string(rp_desc)}";',
        "root-port PCI identity",
    )
    speed_names = {
        "2.5": "QEMU_PCI_EXP_LNK_2_5GT", "5": "QEMU_PCI_EXP_LNK_5GT",
        "8": "QEMU_PCI_EXP_LNK_8GT", "16": "QEMU_PCI_EXP_LNK_16GT",
        "32": "QEMU_PCI_EXP_LNK_32GT", "64": "QEMU_PCI_EXP_LNK_64GT",
    }
    cases: list[str] = []
    for item in root_ports:
        try:
            target = int(item["guest_target_port"])
            speed = speed_names[str(item["max_link_speed_gtps"])]
            width = int(item["max_link_width"])
            values = tuple(int(item[key], 16) for key in (
                "vendor_id", "device_id", "revision_id", "subsystem_vendor_id", "subsystem_device_id",
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Root Port 实例映射无效") from exc
        if width not in {1, 2, 4, 8, 12, 16, 32}:
            raise RuntimeError(f"Root Port 链路宽度不受支持: x{width}")
        vendor, device, revision, subvendor, subdevice = values
        cases.append(
            f"    case 0x{target:02x}:\n"
            f"        pci_config_set_vendor_id(d->config, 0x{vendor:04x});\n"
            f"        pci_config_set_device_id(d->config, 0x{device:04x});\n"
            f"        pci_config_set_revision(d->config, 0x{revision:02x});\n"
            f"        rp_ssvid = 0x{subvendor:04x}; rp_ssid = 0x{subdevice:04x};\n"
            f"        s->speed = {speed}; s->width = QEMU_PCI_EXP_LNK_X{width};\n"
            "        break;\n"
        )
    rp_source = source / "hw/pci-bridge/pcie_root_port.c"
    replace_literal(
        rp_source,
        "    PCIERootPortClass *rpc = PCIE_ROOT_PORT_GET_CLASS(d);\n    int rc;",
        "    PCIERootPortClass *rpc = PCIE_ROOT_PORT_GET_CLASS(d);\n"
        "    uint16_t rp_ssvid = dc->vendor_id;\n"
        "    uint16_t rp_ssid = rpc->ssid;\n"
        "    int rc;\n\n"
        "    switch (p->port) {\n" + "".join(cases) +
        "    default:\n"
        "        error_setg(errp, \"unprofiled PCIe Root Port number 0x%x\", p->port);\n"
        "        return;\n"
        "    }",
        "per-instance root-port identity",
    )
    replace_literal(
        rp_source,
        "    rc = pci_bridge_ssvid_init(d, rpc->ssvid_offset, dc->vendor_id,\n"
        "                               rpc->ssid, errp);",
        "    rc = pci_bridge_ssvid_init(d, rpc->ssvid_offset, rp_ssvid,\n"
        "                               rp_ssid, errp);",
        "per-instance root-port subsystem identity",
    )
    ioh_text = ioh.read_text(encoding="utf-8")
    if "PCI_DEVICE_ID_IOH_EPORT" in ioh_text.split("ioh3420_class_init", 1)[-1]:
        raise RuntimeError("Root Port 仍使用 IOH 3420 设备 ID")


def patch_xhci_controller(source: Path, profile: dict) -> None:
    usb = profile.get("hardware", {}).get("usb_controller", {})
    required = {
        "pci_vendor_id", "pci_device_id", "revision_id",
        "usb2_ports", "usb3_ports", "slots", "interrupts",
        "bar_size_bytes", "serial_bus_release",
    }
    if not required.issubset(usb):
        raise RuntimeError("hardware.usb_controller 身份不完整")
    if usb.get("qemu_model") != "qemu-xhci":
        raise RuntimeError("xHCI 后端模型或中断模式不受支持")
    capability_profile = usb.get("capability_profile")
    if capability_profile not in {"intel-pch-integrated", "amd-pcie-xhci"}:
        raise RuntimeError("xHCI capability profile 不受支持")
    integrated = capability_profile == "intel-pch-integrated"
    interrupt_mode = usb.get("interrupt_mode")
    if interrupt_mode != ("msi" if integrated else "msix"):
        raise RuntimeError("xHCI 中断模式与平台 capability profile 不一致")

    usb2_ports = int(usb["usb2_ports"])
    usb3_ports = int(usb["usb3_ports"])
    slots = int(usb["slots"])
    interrupts = int(usb["interrupts"])
    bar_size = int(usb["bar_size_bytes"])
    if not (1 <= usb2_ports <= 15 and 1 <= usb3_ports <= 15):
        raise RuntimeError("xHCI USB2/USB3 端口数超出 QEMU 设备模型范围")
    if not (1 <= slots <= 64 and 1 <= interrupts <= 16 and interrupts & (interrupts - 1) == 0):
        raise RuntimeError("xHCI slot/interrupter 数量无效")
    if bar_size != 64 * 1024:
        raise RuntimeError("当前 profile-bound xHCI 实现要求 64 KiB BAR")

    header = source / "include/hw/usb/xhci.h"
    replace_literal(
        header, "#define XHCI_LEN_REGS 0x4000",
        f"#define XHCI_LEN_REGS 0x{bar_size:x}", "xHCI BAR size",
    )

    pci = source / "hw/usb/hcd-xhci-pci.c"
    replacements = {
        "    k->vendor_id    = PCI_VENDOR_ID_REDHAT;":
            f"    k->vendor_id    = 0x{int(usb['pci_vendor_id'], 16):04x};",
        "    k->device_id    = PCI_DEVICE_ID_REDHAT_XHCI;":
            f"    k->device_id    = 0x{int(usb['pci_device_id'], 16):04x};",
        "    k->revision     = 0x01;":
            f"    k->revision     = 0x{int(usb['revision_id'], 16):02x};",
        "    s->msi      = ON_OFF_AUTO_OFF;\n"
        "    s->msix     = ON_OFF_AUTO_AUTO;\n"
        "    xhci->numintrs = XHCI_MAXINTRS;\n"
        "    xhci->numslots = XHCI_MAXSLOTS;":
            f"    s->msi      = {'ON_OFF_AUTO_ON' if integrated else 'ON_OFF_AUTO_OFF'};\n"
            + f"    s->msix     = {'ON_OFF_AUTO_OFF' if integrated else 'ON_OFF_AUTO_ON'};\n"
            + ("    PCI_DEVICE(obj)->cap_present &= ~QEMU_PCI_CAP_EXPRESS;\n" if integrated else "")
            + f"    xhci->numports_2 = {usb2_ports};\n"
            + f"    xhci->numports_3 = {usb3_ports};\n"
            + f"    xhci->numintrs = {interrupts};\n"
            + f"    xhci->numslots = {slots};",
    }
    for old, new in replacements.items():
        replace_literal(pci, old, new, "platform xHCI identity")

    realize_marker = (
        "static void usb_xhci_pci_realize(struct PCIDevice *dev, Error **errp)\n"
        "{\n"
        "    int ret;\n"
        "    Error *err = NULL;\n"
        "    XHCIPciState *s = XHCI_PCI(dev);"
    )
    replace_literal(
        pci, realize_marker,
        realize_marker
        + f"\n    const bool profile_integrated = {'true' if integrated else 'false'};",
        "xHCI integrated-controller discriminator",
    )
    replace_literal(
        pci, "    dev->config[0x60] = 0x30; /* release number */",
        f"    dev->config[0x60] = 0x{int(usb['serial_bus_release'], 16):02x}; /* release number */",
        "xHCI serial bus release",
    )
    pm_setup = (
        "    {\n"
        "        ret = pci_pm_init(dev, 0x70, &err);\n"
        "        if (ret < 0) {\n"
        "            error_propagate(errp, err);\n"
        "            return;\n"
        "        }\n"
        f"        pci_set_word(dev->config + 0x70 + PCI_PM_PMC, 0x{'c1c2' if integrated else 'c1c3'});\n"
        "        pci_set_word(dev->config + 0x70 + PCI_PM_CTRL, 0x0008);\n"
        "        pci_set_word(dev->wmask + 0x70 + PCI_PM_CTRL,\n"
        "                     PCI_PM_CTRL_STATE_MASK | PCI_PM_CTRL_PME_ENABLE);\n"
        "        pci_set_word(dev->w1cmask + 0x70 + PCI_PM_CTRL,\n"
        "                     PCI_PM_CTRL_PME_STATUS);\n"
        "    }\n\n"
    )
    replace_literal(
        pci,
        "    if (s->msi != ON_OFF_AUTO_OFF) {",
        pm_setup + "    if (s->msi != ON_OFF_AUTO_OFF) {",
        "xHCI PM capability",
    )
    replace_literal(
        pci, "        ret = msi_init(dev, 0x70, s->xhci.numintrs, true, false, &err);",
        "        ret = msi_init(dev, 0x80, s->xhci.numintrs, true, false, &err);",
        "xHCI MSI capability offset",
    )
    replace_literal(
        pci,
        "    pci_register_bar(dev, 0,",
        "    if (profile_integrated) {\n"
        "        /* Match the PCH's ascending PM -> MSI capability chain. */\n"
        "        dev->config[PCI_CAPABILITY_LIST] = 0x70;\n"
        "        dev->config[0x70 + PCI_CAP_LIST_NEXT] = 0x80;\n"
        "        dev->config[0x80 + PCI_CAP_LIST_NEXT] = 0;\n"
        "    }\n"
        "    pci_register_bar(dev, 0,",
        "xHCI capability chain",
    )
    replace_literal(
        pci,
        "    if (pci_bus_is_express(pci_get_bus(dev))) {",
        "    if (!profile_integrated && pci_bus_is_express(pci_get_bus(dev))) {",
        "omit PCIe endpoint capability for integrated xHCI",
    )


def profile_southbridge_devfn(profile: dict, role: str) -> tuple[int, int]:
    item = profile.get("devices", {}).get("pci_identities", {}).get(role) or {}
    address = item.get("pci_address") or {}
    try:
        bus = int(address["bus"], 0)
        slot = int(address["slot"], 0)
        function = int(address["function"], 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"身份文件缺少 {role} PCI 槽位") from exc
    if bus != 0 or not (0 <= slot <= 31 and 0 <= function <= 7):
        raise RuntimeError(f"{role} PCI 槽位超出 Q35 根总线范围")
    if role in {"lpc", "smbus"} and item.get("slot_source") != "host-observed":
        raise RuntimeError(f"{role} PCI 槽位必须来自宿主观察")
    return slot, function


def patch_southbridge_devfn(source: Path, profile: dict) -> None:
    lpc_slot, lpc_func = profile_southbridge_devfn(profile, "lpc")
    smb_slot, smb_func = profile_southbridge_devfn(profile, "smbus")
    sata_slot, sata_func = profile_southbridge_devfn(profile, "sata")
    occupied = {
        (lpc_slot, lpc_func): "lpc",
        (smb_slot, smb_func): "smbus",
        (sata_slot, sata_func): "sata",
    }
    hda_item = profile.get("devices", {}).get("pci_identities", {}).get("hda") or {}
    hda_address = hda_item.get("pci_address") or {}
    try:
        hda_slot = int(hda_address["slot"], 0)
        hda_func = int(hda_address["function"], 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少板载 HDA PCI 槽位") from exc
    if (hda_slot, hda_func) in occupied:
        raise RuntimeError("板载 HDA 与南桥功能槽位冲突")
    occupied[(hda_slot, hda_func)] = "hda"
    if len(occupied) != 4:
        raise RuntimeError("南桥 LPC/SMBus/SATA/HDA 槽位冲突")
    usb_address = (profile.get("hardware", {}).get("usb_controller") or {}).get("pci_address") or {}
    try:
        usb_slot = int(usb_address["slot"], 0)
        usb_func = int(usb_address["function"], 0)
    except (KeyError, TypeError, ValueError):
        usb_slot = usb_func = None
    if usb_slot is not None and (usb_slot, usb_func) in occupied:
        raise RuntimeError("xHCI 与板载 HDA/南桥功能槽位冲突")
    header = source / "include/hw/southbridge/ich9.h"
    replacements = (
        ("#define ICH9_LPC_DEV                            31", f"#define ICH9_LPC_DEV                            {lpc_slot}", "LPC slot"),
        ("#define ICH9_LPC_FUNC                           0", f"#define ICH9_LPC_FUNC                           {lpc_func}", "LPC function"),
        ("#define ICH9_SATA1_DEV                          31", f"#define ICH9_SATA1_DEV                          {sata_slot}", "SATA slot"),
        ("#define ICH9_SATA1_FUNC                         2", f"#define ICH9_SATA1_FUNC                         {sata_func}", "SATA function"),
        ("#define ICH9_SMB_DEV                            31", f"#define ICH9_SMB_DEV                            {smb_slot}", "SMBus slot"),
        ("#define ICH9_SMB_FUNC                           3", f"#define ICH9_SMB_FUNC                           {smb_func}", "SMBus function"),
    )
    for old, new, label in replacements:
        if old == new:
            continue
        replace_literal(header, old, new, label)
    text = header.read_text(encoding="utf-8")
    expected = (
        f"#define ICH9_LPC_DEV                            {lpc_slot}",
        f"#define ICH9_LPC_FUNC                           {lpc_func}",
        f"#define ICH9_SATA1_DEV                          {sata_slot}",
        f"#define ICH9_SATA1_FUNC                         {sata_func}",
        f"#define ICH9_SMB_DEV                            {smb_slot}",
        f"#define ICH9_SMB_FUNC                           {smb_func}",
    )
    missing = [marker for marker in expected if marker not in text]
    if missing:
        raise RuntimeError("QEMU 南桥 DEVFN 没有写入 ich9.h: " + ", ".join(missing))


def profile_acpi_nodes(profile: dict) -> dict[str, dict]:
    nodes = profile.get("devices", {}).get("acpi_nodes", {})
    if not isinstance(nodes, dict) or "lpc" not in nodes:
        raise RuntimeError("身份文件缺少宿主 ACPI 节点，请重新运行 01_generate_identity.py")
    return nodes


def profile_acpi_nameseg(nodes: dict[str, dict], role: str, *, required: bool) -> str | None:
    item = nodes.get(role) or {}
    name = item.get("name")
    if not name:
        if required:
            raise RuntimeError(f"身份文件缺少宿主 {role} ACPI NameSeg")
        return None
    if ACPI_NAMESEG_RE.fullmatch(str(name)) is None:
        raise RuntimeError(f"身份文件中的 {role} ACPI NameSeg 无效: {name}")
    if item.get("source") != "host-observed":
        raise RuntimeError(f"{role} ACPI 节点必须来自宿主观察，不能使用派生名")
    return str(name)


def profile_mce_banks(profile: dict) -> int:
    try:
        banks = int(profile.get("hardware", {}).get("mce_banks"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少宿主 MCE bank 数量") from exc
    if not (1 <= banks <= 32):
        raise RuntimeError(f"MCE bank 数量超出 QEMU 可承载范围: {banks}")
    return banks


def profile_cpu_hotplug_io_base(profile: dict) -> int:
    try:
        base = int(str(profile.get("hardware", {}).get("cpu_hotplug_io_base", "")), 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 CPU 热插拔 IO 基址") from exc
    if not (0x4000 <= base <= 0x7FF0) or base & 0xF or base == 0x0CD8:
        raise RuntimeError(f"CPU 热插拔 IO 基址无效或仍是 QEMU 默认口: {base:#x}")
    return base


def patch_mce_and_cpu_hotplug(source: Path, profile: dict) -> None:
    banks = profile_mce_banks(profile)
    hotplug = profile_cpu_hotplug_io_base(profile)
    cpu_header = source / "target/i386/cpu.h"
    if banks != 10:
        replace_literal(
            cpu_header,
            "#define MCE_BANKS_DEF   10",
            f"#define MCE_BANKS_DEF   {banks}",
            "MCE bank count",
        )
    text = cpu_header.read_text(encoding="utf-8")
    if not re.search(rf"^#define MCE_BANKS_DEF\s+{banks}$", text, re.MULTILINE):
        raise RuntimeError("QEMU MCE bank 数量没有写入 cpu.h")
    hotplug_header = source / "include/hw/acpi/pc-hotplug.h"
    replace_literal(
        hotplug_header,
        "#define ICH9_CPU_HOTPLUG_IO_BASE 0x0CD8",
        f"#define ICH9_CPU_HOTPLUG_IO_BASE 0x{hotplug:04X}",
        "ICH9 CPU hotplug IO base",
    )
    if f"#define ICH9_CPU_HOTPLUG_IO_BASE 0x{hotplug:04X}" not in hotplug_header.read_text(encoding="utf-8"):
        raise RuntimeError("QEMU CPU 热插拔 IO 基址没有写入 pc-hotplug.h")
    if "ICH9_CPU_HOTPLUG_IO_BASE 0x0CD8" in hotplug_header.read_text(encoding="utf-8"):
        raise RuntimeError("QEMU 仍使用默认 CPU 热插拔口 0x0CD8")


def patch_southbridge_acpi_nodes(source: Path, profile: dict) -> None:
    """Bind Q35 DSDT node names to host-observed ACPI NameSegs.

    QEMU keeps the PCI0 root.  Device NameSegs and southbridge DEVFNs both
    come from the host profile.
    """
    nodes = profile_acpi_nodes(profile)
    lpc_name = profile_acpi_nameseg(nodes, "lpc", required=True)
    smbus_name = profile_acpi_nameseg(nodes, "smbus", required=True)
    usb_name = profile_acpi_nameseg(nodes, "usb", required=True)
    sata_name = profile_acpi_nameseg(nodes, "sata", required=False)
    named_types = {
        QEMU_ACPI_TYPE_BY_ROLE["lpc"]: lpc_name,
        QEMU_ACPI_TYPE_BY_ROLE["smbus"]: smbus_name,
        QEMU_ACPI_TYPE_BY_ROLE["usb"]: usb_name,
    }
    if sata_name:
        named_types[QEMU_ACPI_TYPE_BY_ROLE["sata"]] = sata_name

    lpc = source / "hw/isa/lpc_ich9.c"
    replace_literal(
        lpc,
        'aml_field("PCI0.SF8.PIRQ"',
        f'aml_field("PCI0.{lpc_name}.PIRQ"',
        "LPC ACPI scope",
    )
    if f'aml_field("PCI0.{lpc_name}.PIRQ"' not in lpc.read_text(encoding="utf-8"):
        raise RuntimeError("LPC ACPI 字段没有绑定到宿主 NameSeg")
    if 'aml_field("PCI0.SF8.PIRQ"' in lpc.read_text(encoding="utf-8"):
        raise RuntimeError("LPC ACPI 字段仍指向 QEMU 默认 SF8 节点")

    comparisons = []
    for typename, nameseg in named_types.items():
        comparisons.append(
            f'        if (!strcmp(type, "{c_string(typename)}")) {{\n'
            f'            memcpy(name, "{c_string(nameseg)}", 5);\n'
            f"            return;\n"
            f"        }}"
        )
    helper = (
        "static void ovo_profile_pci_acpi_name(char *name, PCIBus *bus, int devfn)\n"
        "{\n"
        "    PCIDevice *pdev = bus ? bus->devices[devfn] : NULL;\n"
        "    const char *type = pdev ? object_get_typename(OBJECT(pdev)) : NULL;\n"
        "\n"
        "    if (pdev && bus && pci_bus_is_root(bus) && type) {\n"
        + "\n".join(comparisons) + "\n"
        "    }\n"
        '    snprintf(name, 5, "S%.02X", devfn);\n'
        "}\n\n"
    )
    pcihp = source / "hw/acpi/pcihp.c"
    include_marker = '#include "trace.h"\n'
    replace_literal(
        pcihp,
        include_marker,
        include_marker + '#include "qom/object.h"\n\n' + helper,
        "profile ACPI NameSeg helper",
    )
    replace_literal(
        pcihp,
        "        /* start to compose PCI device descriptor */\n"
        "        dev = aml_device(\"S%.02X\", devfn);\n"
        "        aml_append(dev, aml_name_decl(\"_ADR\", aml_int(adr)));",
        "        /* start to compose PCI device descriptor */\n"
        "        char pci_name[5] = {0};\n"
        "        ovo_profile_pci_acpi_name(pci_name, bus, devfn);\n"
        "        dev = aml_device(\"%s\", pci_name);\n"
        "        aml_append(dev, aml_name_decl(\"_ADR\", aml_int(adr)));",
        "PCI device ACPI NameSeg",
    )
    replace_literal(
        pcihp,
        "        if (bus->devices[devfn]) {\n"
        "            dev = aml_scope(\"S%.02X\", devfn);\n"
        "        } else {\n"
        "            dev = aml_device(\"S%.02X\", devfn);\n"
        "            aml_append(dev, aml_name_decl(\"_ADR\", aml_int(adr)));\n"
        "        }",
        "        char pci_name[5] = {0};\n"
        "        ovo_profile_pci_acpi_name(pci_name, bus, devfn);\n"
        "        if (bus->devices[devfn]) {\n"
        "            dev = aml_scope(\"%s\", pci_name);\n"
        "        } else {\n"
        "            dev = aml_device(\"%s\", pci_name);\n"
        "            aml_append(dev, aml_name_decl(\"_ADR\", aml_int(adr)));\n"
        "        }",
        "PCI hotplug ACPI NameSeg",
    )
    replace_literal(
        pcihp,
        "        Aml *br_scope = aml_scope(\"S%.02X\", sec->parent_dev->devfn);",
        "        char pci_name[5] = {0};\n"
        "        ovo_profile_pci_acpi_name(pci_name, pci_get_bus(sec->parent_dev),\n"
        "                                 sec->parent_dev->devfn);\n"
        "        Aml *br_scope = aml_scope(\"%s\", pci_name);",
        "PCI child-bus ACPI NameSeg",
    )
    replace_literal(
        pcihp,
        "        aml_append(method, aml_name(\"^S%.02X.PCNT\", sec->parent_dev->devfn));",
        "        char pci_name[5] = {0};\n"
        "        ovo_profile_pci_acpi_name(pci_name, pci_get_bus(sec->parent_dev),\n"
        "                                 sec->parent_dev->devfn);\n"
        "        aml_append(method, aml_name(\"^%s.PCNT\", pci_name));",
        "PCI child PCNT ACPI NameSeg",
    )
    replace_literal(
        pcihp,
        "static void build_append_pcihp_notify_entry(Aml *method, int slot)\n"
        "{\n"
        "    Aml *if_ctx;\n"
        "    int32_t devfn = PCI_DEVFN(slot, 0);\n"
        "\n"
        "    if_ctx = aml_if(aml_and(aml_arg(0), aml_int(0x1U << slot), NULL));\n"
        "    aml_append(if_ctx, aml_notify(aml_name(\"S%.02X\", devfn), aml_arg(1)));\n"
        "    aml_append(method, if_ctx);\n"
        "}",
        "static void build_append_pcihp_notify_entry(Aml *method, PCIBus *bus, int slot)\n"
        "{\n"
        "    Aml *if_ctx;\n"
        "    char pci_name[5] = {0};\n"
        "    int32_t devfn = PCI_DEVFN(slot, 0);\n"
        "\n"
        "    ovo_profile_pci_acpi_name(pci_name, bus, devfn);\n"
        "    if_ctx = aml_if(aml_and(aml_arg(0), aml_int(0x1U << slot), NULL));\n"
        "    aml_append(if_ctx, aml_notify(aml_name(\"%s\", pci_name), aml_arg(1)));\n"
        "    aml_append(method, if_ctx);\n"
        "}",
        "PCI notify ACPI NameSeg",
    )
    replace_literal(
        pcihp,
        "        build_append_pcihp_notify_entry(notify_method, slot);",
        "        build_append_pcihp_notify_entry(notify_method, bus, slot);",
        "PCI notify helper call",
    )
    pcihp_text = pcihp.read_text(encoding="utf-8")
    if 'aml_device("S%.02X", devfn)' in pcihp_text or 'aml_scope("S%.02X"' in pcihp_text:
        raise RuntimeError("pcihp ACPI 设备名仍残留 QEMU 默认 Sxx 格式")
    if f'memcpy(name, "{lpc_name}", 5)' not in pcihp_text:
        raise RuntimeError("pcihp 没有绑定宿主 LPC ACPI NameSeg")


def patch_acpi_identity(source: Path, profile: dict) -> None:
    firmware = profile["hardware"]["firmware"]
    vendor = c_string(profile["hardware"]["platform"]["vendor"])
    oem_id = c_string(firmware["acpi_oem_id"][:6].ljust(6))
    table_id = c_string(firmware["acpi_table_id"][:8].ljust(8))
    creator_id = c_string(firmware["acpi_creator_id"][:4].ljust(4))
    creator_revision = int(firmware["acpi_creator_revision"])

    aml_header = source / "include/hw/acpi/aml-build.h"
    replace_once(aml_header, r'^#define ACPI_BUILD_APPNAME6\s+"[^"]*"', f'#define ACPI_BUILD_APPNAME6 "{oem_id}"', "ACPI OEM ID")
    replace_once(aml_header, r'^#define ACPI_BUILD_APPNAME8\s+"[^"]*"', f'#define ACPI_BUILD_APPNAME8 "{table_id}"', "ACPI table ID")
    header_text = aml_header.read_text(encoding="utf-8")
    marker = f'#define ACPI_BUILD_APPNAME8 "{table_id}"\n'
    if header_text.count(marker) != 1:
        raise RuntimeError("ACPI creator insertion marker not found")
    aml_header.write_text(
        header_text.replace(
            marker,
            marker
            + f'#define ACPI_BUILD_CREATOR_ID "{creator_id}"\n'
            + f'#define ACPI_BUILD_CREATOR_REVISION 0x{creator_revision:08X}\n',
            1,
        ),
        encoding="utf-8",
    )

    core = source / "hw/acpi/core.c"
    replace_once(core, r'^\s*"QEMUQEQEMUQEMU\\1\\0\\0\\0"', f'    "{oem_id}{table_id}\\1\\0\\0\\0"', "default ACPI OEM header")
    revision_bytes = creator_revision.to_bytes(4, "little")
    revision_c = "".join(f"\\x{value:02x}" for value in revision_bytes)
    replace_once(core, r'^\s*"QEMU\\1\\0\\0\\0"', f'    "{creator_id}{revision_c}"', "default ACPI creator")

    aml_source = source / "hw/acpi/aml-build.c"
    replace_literal(
        aml_source,
        "g_array_append_vals(array, ACPI_BUILD_APPNAME8, 4); /* Creator ID */",
        "g_array_append_vals(array, ACPI_BUILD_CREATOR_ID, 4); /* Creator ID */",
        "ACPI creator ID",
    )
    replace_literal(
        aml_source,
        "build_append_int_noprefix(array, 1, 4); /* Creator Revision */",
        "build_append_int_noprefix(array, ACPI_BUILD_CREATOR_REVISION, 4); /* Creator Revision */",
        "ACPI creator revision",
    )
    power = profile.get("hardware", {}).get("power", {})
    try:
        preferred_profile = int(power["preferred_pm_profile"])
        c2_latency = int(power["c2_latency_us"])
        c3_latency = int(power["c3_latency_us"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少宿主 FADT 电源参数") from exc
    if not 0 <= preferred_profile <= 8 or not 0 <= c2_latency <= 0xFFFF or not 0 <= c3_latency <= 0xFFFF:
        raise RuntimeError("宿主 FADT 电源参数超出编码范围")
    replace_once(
        aml_source,
        r'build_append_int_noprefix\(tbl, 0 /\* Unspecified \*/, 1\);',
        f'build_append_int_noprefix(tbl, {preferred_profile} /* Platform profile */, 1);',
        "FADT preferred power profile",
    )
    replace_literal(
        aml_source,
        "build_append_int_noprefix(tbl, f->plvl2_lat, 2); /* P_LVL2_LAT */",
        f"build_append_int_noprefix(tbl, {c2_latency}, 2); /* P_LVL2_LAT */",
        "FADT C2 latency",
    )
    replace_literal(
        aml_source,
        "build_append_int_noprefix(tbl, f->plvl3_lat, 2); /* P_LVL3_LAT */",
        f"build_append_int_noprefix(tbl, {c3_latency}, 2); /* P_LVL3_LAT */",
        "FADT C3 latency",
    )
    replace_once(
        aml_source,
        r'build_append_padded_str\(tbl, "QEMU", 8, \'\\0\'\);',
        "build_append_padded_str(tbl, \"\", 8, '\\0');",
        "FADT hypervisor vendor ID",
    )

    fw_cfg = source / "hw/i386/fw_cfg.c"
    replace_once(fw_cfg, r'smbios_set_defaults\("QEMU", mc->desc, mc->name\);', f'smbios_set_defaults("{vendor}", mc->desc, mc->name);', "fallback SMBIOS vendor")

    acpi_build = source / "hw/i386/acpi-build.c"
    pit_builder = '''static void build_pit_aml(Aml *table)
{
    Aml *scope = aml_scope("_SB");
    Aml *dev = aml_device("TIMR");
    Aml *crs = aml_resource_template();

    aml_append(dev, aml_name_decl("_HID", aml_eisaid("PNP0100")));
    aml_append(dev, aml_name_decl("_UID", aml_int(0)));
    aml_append(crs, aml_io(AML_DECODE16, 0x0040, 0x0040, 0x01, 0x04));
    aml_append(crs, aml_irq_no_flags(0));
    aml_append(dev, aml_name_decl("_CRS", crs));
    aml_append(scope, dev);
    aml_append(table, scope);
}

'''
    replace_literal(
        acpi_build,
        "static void build_hpet_aml(Aml *table)\n",
        pit_builder + "static void build_hpet_aml(Aml *table)\n",
        "legacy PIT ACPI device",
    )
    replace_literal(
        acpi_build,
        "    if (misc->has_hpet) {\n        build_hpet_aml(dsdt);\n    }",
        "    build_pit_aml(dsdt);\n\n"
        "    if (misc->has_hpet) {\n        build_hpet_aml(dsdt);\n    }",
        "legacy PIT ACPI publication",
    )
    replace_once(
        acpi_build,
        r'\n\s*/\* create fw_cfg node, unconditionally \*/\n\s*\{\n\s*scope = aml_scope\("\\\\_SB\.PCI0"\);\n\s*fw_cfg_add_acpi_dsdt\(scope, x86ms->fw_cfg\);\n\s*aml_append\(dsdt, scope\);\n\s*\}',
        "\n    /* fw_cfg remains firmware-private and is not advertised to the guest. */",
        "fw_cfg ACPI exposure",
    )
    replace_once(
        acpi_build,
        r'static void build_dbg_aml\(Aml \*table\)\n\{.*?\n\}\n\nstatic Aml \*build_link_dev',
        "static void build_dbg_aml(Aml *table)\n{\n    /* Do not publish the emulator-only debug port in guest AML. */\n    (void)table;\n}\n\nstatic Aml *build_link_dev",
        "ACPI debug port",
        flags=re.MULTILINE | re.DOTALL,
    )
    replace_literal(
        acpi_build,
        "if_ctx = aml_if(aml_lor(aml_equal(period, zero),\n"
        "                            aml_lgreater(period, aml_int(100000000))));",
        "if_ctx = aml_if(aml_equal(period, zero));",
        "ACPI HPET validation",
    )
    routing_replacements = {
        'static const char link_name[][5] = {"LNKD", "LNKA", "LNKB", "LNKC"};': 'static const char link_name[][5] = {"PIRD", "PIRA", "PIRB", "PIRC"};',
        'aml_name_decl("PRTP", build_q35_routing_table("LNK"))': 'aml_name_decl("PIRQ", build_q35_routing_table("PIR"))',
        'aml_name_decl("PRTA", build_q35_routing_table("GSI"))': 'aml_name_decl("APIC", build_q35_routing_table("IRQ"))',
        'aml_return(aml_name("PRTP"))': 'aml_return(aml_name("PIRQ"))',
        'aml_return(aml_name("PRTA"))': 'aml_return(aml_name("APIC"))',
    }
    for suffix in "ABCDEFGH":
        routing_replacements[f'build_link_dev("LNK{suffix}"'] = f'build_link_dev("PIR{suffix}"'
        routing_replacements[f'build_gsi_link_dev("GSI{suffix}"'] = f'build_gsi_link_dev("IRQ{suffix}"'
    for old, new in routing_replacements.items():
        expected = 2 if old.startswith('build_link_dev("LNK') and old[-2] in "ABCD" else 1
        replace_literal(acpi_build, old, new, f"ACPI routing name {old}", expected=expected)
    for old, new, label in (
        ('.plvl2_lat = 0xfff /* C2 state not supported */,', '.plvl2_lat = 0x0099 /* Above the ACPI C2 usability threshold */,', "FADT C2 latency"),
        ('.plvl3_lat = 0xfff /* C3 state not supported */,', '.plvl3_lat = 0x0427 /* Above the ACPI C3 usability threshold */,', "FADT C3 latency"),
        ('aml_device("DRAC")', 'aml_device("MCHC")', "ACPI memory controller name"),
        ('aml_device("PCI0.SMI0")', 'aml_device("PCI0.SMID")', "ACPI SMI device name"),
        ('aml_string("SMI resources")', 'aml_string("System Management")', "ACPI SMI description"),
        ('aml_device("GPE0")', 'aml_device("GPEC")', "ACPI GPE resource name"),
        ('aml_string("GPE0 resources")', 'aml_string("Motherboard resources")', "ACPI GPE description"),
    ):
        replace_literal(acpi_build, old, new, label)
    replace_once(
        acpi_build,
        r'static void\nbuild_waet\(.*?\n\}\n',
        "",
        "WAET builder",
        flags=re.MULTILINE | re.DOTALL,
    )
    replace_literal(
        acpi_build,
        "    acpi_add_table(table_offsets, tables_blob);\n"
        "    build_waet(tables_blob, tables->linker, x86ms->oem_id, x86ms->oem_table_id);",
        "    /* WAET is an emulation-only table and is intentionally omitted. */",
        "WAET publication",
    )

    replace_literal(
        source / "hw/acpi/cpu.c",
        'aml_string("CPU Hotplug resources")',
        'aml_string("Processor resources")',
        "ACPI CPU resource description",
    )
    pcihp = source / "hw/acpi/pcihp.c"
    replace_literal(pcihp, 'aml_device("PHPR")', 'aml_device("PCIR")', "ACPI PCI resource name")
    replace_literal(
        pcihp,
        'aml_string("PCI Hotplug resources")',
        'aml_string("PCI resources")',
        "ACPI PCI resource description",
    )
    replace_once(
        source / "include/hw/i386/x86.h",
        r'^#define ACPI_BUILD_PCI_IRQS .*$',
        '#define ACPI_BUILD_PCI_IRQS (1 << 9)',
        "MADT PCI interrupt override mask",
    )


FULL_SMBIOS_GLOBALS = r'''
static bool smbios_full_file;
static unsigned smbios_full_type4_count;

static bool smbios_full_type_can_repeat(uint8_t type)
{
    switch (type) {
    case 4:
    case 7:
    case 8:
    case 9:
    case 17:
    case 19:
    case 20:
    case 22:
    case 41:
        return true;
    default:
        return false;
    }
}
'''

FULL_SMBIOS_LOADER = r'''
static bool smbios_load_full_file(const char *filename, Error **errp)
{
    static const uint8_t required_types[] = {
        0, 1, 2, 3, 4, 16, 17, 19, 20, 32, 127
    };
    DECLARE_BITMAP(seen, SMBIOS_MAX_TYPE + 1);
    g_autofree uint8_t *data = NULL;
    g_autofree uint16_t *handles = NULL;
    size_t file_size, offset = 0, handle_count = 0;
    size_t structure_count = 0, max_structure_size = 0;
    unsigned type4_count = 0;
    bool saw_end = false;
    unsigned i;

    file_size = get_image_size(filename, NULL);
    if (file_size == (size_t)-1 ||
        file_size < sizeof(struct smbios_structure_header)) {
        error_setg(errp, "Cannot read full SMBIOS file %s", filename);
        return false;
    }
    data = g_malloc(file_size);
    if (load_image_size(filename, data, file_size) != file_size) {
        error_setg(errp, "Failed to load full SMBIOS file %s", filename);
        return false;
    }

    bitmap_zero(seen, SMBIOS_MAX_TYPE + 1);
    while (offset < file_size) {
        const struct smbios_structure_header *header;
        size_t end, structure_size;
        uint16_t handle;

        if (file_size - offset < sizeof(*header)) {
            error_setg(errp, "truncated SMBIOS header at offset %zu", offset);
            return false;
        }
        header = (const struct smbios_structure_header *)(data + offset);
        if (header->length < sizeof(*header) ||
            offset + header->length > file_size) {
            error_setg(errp, "invalid SMBIOS type %u length %u",
                       header->type, header->length);
            return false;
        }
        if (header->type > SMBIOS_MAX_TYPE) {
            error_setg(errp, "SMBIOS type %u is out of range", header->type);
            return false;
        }
        if (test_bit(header->type, seen) &&
            !smbios_full_type_can_repeat(header->type)) {
            error_setg(errp, "duplicate singleton SMBIOS type %u", header->type);
            return false;
        }

        end = offset + header->length;
        while (end + 1 < file_size && (data[end] || data[end + 1])) {
            end++;
        }
        if (end + 1 >= file_size) {
            error_setg(errp, "missing string terminator for SMBIOS type %u",
                       header->type);
            return false;
        }
        structure_size = end + 2 - offset;
        handle = le16_to_cpu(header->handle);
        for (i = 0; i < handle_count; i++) {
            if (handles[i] == handle) {
                error_setg(errp, "duplicate SMBIOS handle 0x%04x", handle);
                return false;
            }
        }
        handles = g_renew(uint16_t, handles, handle_count + 1);
        handles[handle_count++] = handle;
        set_bit(header->type, seen);
        if (header->type == 4) {
            type4_count++;
        }
        max_structure_size = MAX(max_structure_size, structure_size);
        structure_count++;
        offset += structure_size;
        if (header->type == 127) {
            saw_end = true;
            break;
        }
    }
    if (!saw_end || offset != file_size) {
        error_setg(errp, "full SMBIOS file must end at Type 127");
        return false;
    }
    for (i = 0; i < ARRAY_SIZE(required_types); i++) {
        if (!test_bit(required_types[i], seen)) {
            error_setg(errp, "full SMBIOS file is missing type %u",
                       required_types[i]);
            return false;
        }
    }

    g_free(usr_blobs);
    usr_blobs = g_steal_pointer(&data);
    usr_blobs_len = file_size;
    usr_table_max = max_structure_size;
    usr_table_cnt = structure_count;
    smbios_full_type4_count = type4_count;
    bitmap_copy(smbios_have_binfile_bitmap, seen, SMBIOS_MAX_TYPE + 1);
    smbios_full_file = true;
    return true;
}

'''


FULL_SMBIOS_FINALIZE = r'''
static bool smbios_full_finalize_cpuid(Error **errp)
{
    size_t offset = 0;
    uint32_t version = cpu_to_le32(smbios_cpuid_version);
    uint32_t features = cpu_to_le32(smbios_cpuid_features);

    if (!smbios_cpuid_version) {
        error_setg(errp, "guest CPUID is unavailable for SMBIOS");
        return false;
    }
    while (offset < smbios_tables_len) {
        size_t end;
        struct smbios_structure_header *header;

        if (smbios_tables_len - offset < sizeof(*header)) {
            error_setg(errp, "truncated SMBIOS runtime header");
            return false;
        }
        header = (struct smbios_structure_header *)(smbios_tables + offset);
        if (header->length < sizeof(*header) ||
            header->length > smbios_tables_len - offset) {
            error_setg(errp, "invalid SMBIOS runtime structure length");
            return false;
        }
        end = offset + header->length;
        while (end + 1 < smbios_tables_len &&
               (smbios_tables[end] || smbios_tables[end + 1])) {
            end++;
        }
        if (end + 1 >= smbios_tables_len) {
            error_setg(errp, "invalid SMBIOS runtime string terminator");
            return false;
        }
        if (header->type == 4) {
            static const uint8_t placeholder[8];

            if (header->length < 16 ||
                memcmp(smbios_tables + offset + 8, placeholder, 8)) {
                error_setg(errp, "SMBIOS Processor ID must be a runtime placeholder");
                return false;
            }
            memcpy(smbios_tables + offset + 8, &version, sizeof(version));
            memcpy(smbios_tables + offset + 12, &features, sizeof(features));
        }
        offset = end + 2;
    }
    return true;
}

'''


def patch_full_smbios(source: Path, profile: dict) -> None:
    fw_cfg = source / "hw/i386/fw_cfg.c"
    fw_text = fw_cfg.read_text(encoding="utf-8")
    cpuid_marker = "    smbios_set_cpuid(cpu->env.cpuid_version, cpu->env.features[FEAT_1_EDX]);"
    if fw_text.count(cpuid_marker) != 1:
        raise RuntimeError("SMBIOS runtime CPUID source marker not found")
    fw_text = fw_text.replace(cpuid_marker, (
        "    uint32_t smbios_eax, smbios_ebx, smbios_ecx, smbios_edx;\n"
        "    cpu_x86_cpuid(&cpu->env, 1, 0, &smbios_eax, &smbios_ebx,\n"
        "                  &smbios_ecx, &smbios_edx);\n"
        "    smbios_set_cpuid(smbios_eax, smbios_edx);"
    ), 1)
    fw_cfg.write_text(fw_text, encoding="utf-8")
    path = source / "hw/smbios/smbios.c"
    text = path.read_text(encoding="utf-8")
    marker = "static bool smbios_have_defaults;\n"
    if text.count(marker) != 1:
        raise RuntimeError("SMBIOS global marker not found")
    text = text.replace(marker, marker + FULL_SMBIOS_GLOBALS, 1)

    marker = "static void save_opt(const char **dest, QemuOpts *opts, const char *name)\n"
    if text.count(marker) != 1:
        raise RuntimeError("SMBIOS loader insertion point not found")
    text = text.replace(marker, FULL_SMBIOS_LOADER + FULL_SMBIOS_FINALIZE + marker, 1)

    marker = "    val = qemu_opt_get(opts, \"file\");\n"
    replacement = (
        "    val = qemu_opt_get(opts, \"full-file\");\n"
        "    if (val) {\n"
        "        if (smbios_full_file || usr_table_cnt) {\n"
        "            error_setg(errp, \"full SMBIOS file cannot be combined with another SMBIOS source\");\n"
        "            return;\n"
        "        }\n"
        "        smbios_load_full_file(val, errp);\n"
        "        return;\n"
        "    }\n"
        "    if (smbios_full_file) {\n"
        "        error_setg(errp, \"full SMBIOS file cannot be combined with another SMBIOS source\");\n"
        "        return;\n"
        "    }\n"
        + marker
    )
    if text.count(marker) != 1:
        raise RuntimeError("SMBIOS option insertion point not found")
    text = text.replace(marker, replacement, 1)

    marker = "    smbios_table_cnt = usr_table_cnt;\n\n"
    replacement = marker + (
        "    if (smbios_full_file) {\n"
        "        if (!smbios_full_finalize_cpuid(errp)) {\n"
        "            goto err_exit;\n"
        "        }\n"
        "        smbios_type4_count = smbios_full_type4_count;\n"
        "        goto tables_ready;\n"
        "    }\n\n"
    )
    if text.count(marker) != 1:
        raise RuntimeError("SMBIOS table-copy marker not found")
    text = text.replace(marker, replacement, 1)

    marker = "    if (!smbios_check_type4_count(ms->smp.sockets, errp)) {\n"
    if text.count(marker) != 1:
        raise RuntimeError("SMBIOS validation marker not found")
    text = text.replace(marker, "tables_ready:\n" + marker, 1)

    marker = "    switch (ep_type) {\n"
    wrapper_pos = text.find("void smbios_get_tables(")
    switch_pos = text.find(marker, wrapper_pos)
    if switch_pos < 0:
        raise RuntimeError("SMBIOS wrapper entry-point switch not found")
    text = text[:switch_pos] + "    if (smbios_full_file) {\n        ep_type = SMBIOS_ENTRY_POINT_TYPE_64;\n    }\n\n" + text[switch_pos:]

    ep = profile["qemu_policy"]["smbios_entry_point"]
    text, count_major = re.subn(r'(ep\.ep30\.smbios_major_version = )3;', rf'\g<1>{int(ep["major"])};', text, count=1)
    text, count_minor = re.subn(r'(ep\.ep30\.smbios_minor_version = )0;', rf'\g<1>{int(ep["minor"])};', text, count=1)
    text, count_docrev = re.subn(r'(ep\.ep30\.smbios_doc_rev = )0;', rf'\g<1>{int(ep["docrev"])};', text, count=1)
    if (count_major, count_minor, count_docrev) != (1, 1, 1):
        raise RuntimeError("SMBIOS 3.x entry-point version markers not found")
    path.write_text(text, encoding="utf-8")

    help_path = source / "qemu-options.hx"
    help_text = help_path.read_text(encoding="utf-8")
    marker = 'DEF("smbios", HAS_ARG, QEMU_OPTION_smbios,\n'
    replacement = (
        marker
        + '    "-smbios full-file=binary\\n"\n'
        + '    "                load one complete SMBIOS structure stream\\n"\n'
    )
    if help_text.count(marker) != 1:
        raise RuntimeError("SMBIOS help marker not found")
    help_path.write_text(help_text.replace(marker, replacement, 1), encoding="utf-8")


def build_battery_table(profile: dict) -> Path | None:
    battery = profile["hardware"].get("battery", {"enabled": False})
    if not battery.get("enabled"):
        return None
    if shutil.which("iasl") is None:
        raise RuntimeError("移动/电池平台需要 iasl 编译 battery SSDT")
    template_path = RESOURCES / "battery_ssdt.dsl.in"
    if not template_path.is_file():
        raise RuntimeError(f"缺少电池资源模板: {template_path}")
    values = {
        "OEM_ID": profile["ovmf_policy"]["acpi_oem_id"][:6].ljust(6),
        "TABLE_ID": profile["ovmf_policy"]["acpi_table_id"][:8].ljust(8),
        "DESIGN_CAPACITY": battery["design_capacity_mwh"],
        "FULL_CAPACITY": battery["last_full_capacity_mwh"],
        "DESIGN_VOLTAGE": battery["design_voltage_mv"],
        "WARNING_CAPACITY": battery["warning_capacity_mwh"],
        "LOW_CAPACITY": battery["low_capacity_mwh"],
        "GRANULARITY": battery["granularity_mwh"],
        "REMAINING_CAPACITY": battery["remaining_capacity_mwh"],
        "PRESENT_VOLTAGE": battery["present_voltage_mv"],
        "PRESENT_RATE": battery["present_rate_mw"],
        "BATTERY_STATE": battery["state"],
        "AC_ONLINE": 1 if battery["ac_online"] else 0,
        "TEMP_CURRENT": battery["temperature_current_dk"],
        "TEMP_ACTIVE": battery["temperature_active_dk"],
        "TEMP_PASSIVE": battery["temperature_passive_dk"],
        "TEMP_CRITICAL": battery["temperature_critical_dk"],
        "MODEL": battery["model"],
        "SERIAL": battery["serial"],
        "CHEMISTRY": battery["chemistry"],
        "MANUFACTURER": battery["manufacturer"],
    }
    text = template_path.read_text(encoding="ascii")
    for key, value in values.items():
        text = text.replace(f"@{key}@", str(value))
    if re.search(r"@[A-Z_]+@", text):
        raise RuntimeError("battery SSDT contains unresolved template values")
    generated = OUT / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    dsl = generated / "battery.dsl"
    prefix = generated / "battery"
    dsl.write_text(text, encoding="ascii")
    run(["iasl", "-p", str(prefix), str(dsl)])
    aml = generated / "battery.aml"
    if not aml.is_file():
        raise RuntimeError("iasl did not generate battery.aml")
    data = bytearray(aml.read_bytes())
    if len(data) < 36 or data[:4] != b"SSDT":
        raise RuntimeError("iasl generated an invalid battery SSDT")
    firmware = profile["hardware"]["firmware"]
    data[28:32] = firmware["acpi_creator_id"].encode("ascii")[:4].ljust(4, b" ")
    data[32:36] = int(firmware["acpi_creator_revision"]).to_bytes(4, "little")
    data[9] = 0
    data[9] = (-sum(data)) & 0xFF
    aml.write_bytes(data)
    return aml


def main() -> int:
    if not PROFILE_PATH.is_file():
        raise RuntimeError("缺少 artifacts/identity-hardware.json，请先运行 01_generate_identity.py")
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    validate_memory_record(profile, (ROOT / "artifacts/smbios.bin").read_bytes())
    if (
        profile.get("meta", {}).get("platform_source") != "host-non-unique"
        or int(profile.get("meta", {}).get("platform_source_version", 0)) != 3
        or not profile.get("meta", {}).get("platform_id")
    ):
        raise RuntimeError("身份文件缺少宿主非唯一平台来源信息")
    profile_acpi_nodes(profile)
    profile_acpi_nameseg(profile_acpi_nodes(profile), "lpc", required=True)
    profile_southbridge_devfn(profile, "lpc")
    profile_southbridge_devfn(profile, "smbus")
    profile_southbridge_devfn(profile, "sata")
    if profile.get("hardware", {}).get("audio", {}).get("xml_policy") != "onboard-hda":
        raise RuntimeError("身份文件未声明板载 HDA 策略")

    tools = require_tools(("git", "make", "python3", "ninja", "meson", "pkg-config"))
    compiler = shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        install_missing_tools(["cc"])
        compiler = shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise RuntimeError("自动安装后仍缺少 C 编译器 cc/gcc")
    tools["cc"] = str(Path(compiler).resolve())
    source = ensure_source()
    validate_pristine_source(source)
    revision = source_revision(source)

    prepare_output_directory(OUT)
    OUT.mkdir(parents=True)
    source_copy = OUT / "source"
    print(f"复制原版 QEMU 源码: {source}")
    copy_source(source, source_copy)
    verify_required_subprojects(source_copy)
    patch_full_smbios(source_copy, profile)
    patch_acpi_identity(source_copy, profile)
    patch_device_identity(source_copy, profile)
    patch_sata_capabilities(source_copy, profile)
    patch_pci_subsystem_identity(source_copy, profile)
    patch_xhci_controller(source_copy, profile)
    patch_southbridge_acpi_nodes(source_copy, profile)
    patch_mce_and_cpu_hotplug(source_copy, profile)
    patch_i226_device(source_copy, profile)
    battery_aml = build_battery_table(profile)

    # Build against the final libvirt-visible prefix.  The QEMU binary embeds
    # CONFIG_QEMU_DATADIR at configure time; using the workspace path here
    # would make the deployed emulator depend on /home/lx being traversable by
    # the libvirt runtime user.  DESTDIR stages that final prefix locally so
    # only step 04 performs the actual /opt installation.
    runtime_prefix = Path("/opt/ovo-spoof/profiles") / profile["meta"]["profile_id"] / "qemu"
    prefix = OUT / "prefix"
    stage = OUT / "stage"
    jobs = build_jobs()
    run([
        "./configure", "--target-list=x86_64-softmmu", f"--prefix={runtime_prefix}",
        "--disable-werror",
        "--enable-kvm", "--disable-tcg", "--disable-docs", "--disable-tools",
        "--disable-guest-agent", "--disable-debug-info", "--enable-strip",
    ], source_copy)
    run(["make", "-C", "build", "-j", jobs], source_copy)
    run(["make", "-C", "build", "install", f"DESTDIR={stage}"], source_copy)
    staged_prefix = stage / runtime_prefix.relative_to("/")
    if not staged_prefix.is_dir():
        raise RuntimeError(f"QEMU 安装暂存目录缺少最终 prefix: {staged_prefix}")
    shutil.copytree(staged_prefix, prefix)
    binary = prefix / "bin" / "qemu-system-x86_64"
    if not binary.is_file():
        raise RuntimeError(f"QEMU 构建没有生成 {binary}")
    help_text = run([str(binary), "-help"], capture=True).stdout
    if "full-file" not in help_text:
        raise RuntimeError("构建出的 QEMU 不支持 -smbios full-file")
    device_help = run([str(binary), "-device", "help"], capture=True).stdout
    expected_device_line = 'name "i226-v", bus PCI, desc "Intel Ethernet Controller I226-V"'
    if expected_device_line not in device_help:
        raise RuntimeError("构建出的 QEMU 未注册完整的 i226-v PCIe 网卡")
    property_help = run([str(binary), "-device", "i226-v,help"], capture=True).stdout
    if "mac=<str>" not in property_help or "bootindex=<int32>" not in property_help:
        raise RuntimeError("构建出的 i226-v 缺少标准 NIC 属性")
    smoke = subprocess.run(
        [
            str(binary), "-machine", "q35,accel=qtest", "-display", "none",
            "-nodefaults", "-monitor", "stdio",
            "-device", "i226-v,mac=3c:fd:fe:00:00:01",
        ],
        input="info pci\nquit\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if smoke.returncode != 0:
        raise RuntimeError("构建出的 i226-v 无法在 Q35 PCIe 总线上完成实例化:\n" + smoke.stdout)
    smoke_markers = (
        "Ethernet controller: PCI device 8086:125c",
        "PCI subsystem 8086:0000",
        "BAR0: 32 bit memory",
        "BAR3: 32 bit memory",
        "BAR6: 32 bit memory",
    )
    if any(marker not in smoke.stdout for marker in smoke_markers):
        raise RuntimeError("构建出的 i226-v PCI 身份或 BAR 布局不完整:\n" + smoke.stdout)
    verify_i226_register_contract(binary)
    usb = profile["hardware"]["usb_controller"]
    usb_smoke = subprocess.run(
        [
            str(binary), "-machine", "q35,accel=qtest", "-display", "none",
            "-nodefaults", "-monitor", "stdio",
            "-device", f"qemu-xhci,id=profile-xhci,bus=pcie.0,addr={int(usb['pci_address']['slot'], 0):#x}",
        ],
        input=(
            "info pci\n"
            "qom-get /machine/peripheral/profile-xhci msi\n"
            "qom-get /machine/peripheral/profile-xhci msix\n"
            "qom-get /machine/peripheral/profile-xhci p2\n"
            "qom-get /machine/peripheral/profile-xhci p3\n"
            "quit\n"
        ), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=30,
    )
    usb_markers = (
        f"USB controller: PCI device {usb['pci_vendor_id']}:{usb['pci_device_id']}",
        f"PCI subsystem {usb['subsystem_vendor_id']}:{usb['subsystem_device_id']}",
        "BAR0: 64 bit memory",
        '"on"',
        '"off"',
        str(usb["usb2_ports"]),
        str(usb["usb3_ports"]),
    )
    if usb_smoke.returncode != 0 or any(
        marker not in usb_smoke.stdout for marker in usb_markers
    ):
        raise RuntimeError("构建出的 PCH xHCI 身份或 BAR 布局不完整:\n" + usb_smoke.stdout)
    (OUT / "bin").mkdir()
    link = OUT / "bin" / "qemu-system-x86_64-ovo"
    link.symlink_to(Path("../prefix/bin/qemu-system-x86_64"))
    info = {
        "profile_id": profile["meta"]["profile_id"],
        "profile_sha256": hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest(),
        "patch_revision": PATCH_REVISION,
        "source": {"path": str(source), "ref": QEMU_REF, "revision": revision},
        "runtime_prefix": str(runtime_prefix),
        "platform_source": profile["meta"]["platform_source"],
        "platform_source_version": profile["meta"]["platform_source_version"],
        "platform_id": profile["meta"]["platform_id"],
        "patches": ["full-smbios-exclusive", "profile-acpi-identity", "profile-storage-identity", "profile-usb-hid-identity", "usb-serial-without-pci-path", "profile-audio-backend-if-present", "profile-onboard-hda-pins", "profile-pci-subsystem-identity", "profile-host-bridge-did", "profile-southbridge-devfn", "profile-southbridge-acpi-nodes", "profile-mce-banks", "profile-cpu-hotplug-io", "profile-vga-identity", "profile-root-port-identity", f"profile-{usb['capability_profile']}", "qemu-xhci-profile", "xhci-host-port-layout", "xhci-pm-interrupt-capabilities", "xhci-acpi-node", "intel-i226-v-device", "i226-nvm-mac-dsn-coherence", "i226-gpy-mdio-address-zero", "i226-i225-register-contract", "i226-rss-register-contract", "i226-four-descriptor-queues", "i226-2.5gbe-link", "i226-physical-pcie-layout", "i226-migration-state", "hide-fw-cfg-acpi", "clear-fadt-hypervisor-id", "normalize-acpi-topology", "normalize-madt-overrides", "publish-legacy-pit", "normalize-hpet-aml", "normalize-acpi-creator", "omit-acpi-debug-port", "omit-waet", "kvm-only-build", "stripped-runtime"],
        "tools": tools,
        "binary": "bin/qemu-system-x86_64-ovo",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "battery_aml": str(battery_aml.relative_to(OUT)) if battery_aml else None,
        "battery_aml_sha256": hashlib.sha256(battery_aml.read_bytes()).hexdigest() if battery_aml else None,
    }
    (OUT / "build-info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"QEMU 产物已生成: {link}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"QEMU 构建失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
