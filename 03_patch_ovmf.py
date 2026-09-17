#!/usr/bin/env python3
"""Build profile-bound OVMF firmware from untouched upstream EDK2."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from smbios_memory import validate_record as validate_memory_record
from smbios_contract import FIRMWARE_SIZE

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "artifacts" / "identity-hardware.json"
RESOURCES = ROOT / "resources"
SOURCE = RESOURCES / "ovmfbackup"
OUT = ROOT / "build" / "ovmf"
EDK2_URL = "https://github.com/tianocore/edk2.git"
EDK2_REF = "edk2-stable202602"
PATCH_REVISION = 15
HOST_BGRT_IMAGE = Path("/sys/firmware/acpi/bgrt/image")

TOOL_PACKAGES = {
    "pacman": {
        "git": "git", "make": "make", "gcc": "gcc", "g++": "gcc", "nasm": "nasm",
        "python3": "python", "iasl": "acpica", "qemu-img": "qemu-img",
    },
    "apt-get": {
        "git": "git", "make": "build-essential", "gcc": "build-essential",
        "g++": "build-essential", "nasm": "nasm", "python3": "python3",
        "iasl": "acpica-tools", "qemu-img": "qemu-utils",
    },
    "dnf": {
        "git": "git", "make": "make", "gcc": "gcc", "g++": "gcc-c++", "nasm": "nasm",
        "python3": "python3", "iasl": "acpica-tools", "qemu-img": "qemu-img",
    },
}


def run(command: list[str], cwd: Path | None = None, *, capture: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def install_missing_tools(missing: list[str]) -> None:
    manager = next((name for name in TOOL_PACKAGES if shutil.which(name)), None)
    if manager is None:
        raise RuntimeError("缺少 OVMF 构建工具且未找到受支持的包管理器: " + ", ".join(missing))
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
            raise RuntimeError("自动安装后仍缺少 OVMF 构建工具: " + name)
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
    if (SOURCE / "edksetup.sh").is_file():
        return SOURCE
    if SOURCE.exists():
        raise RuntimeError(f"源码目录存在但不是有效 EDK2 源码: {SOURCE}")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("缺少 git，无法自动下载 EDK2 源码")
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    print(f"未找到本地 OVMF 原版源码，正在下载 {EDK2_REF} ...")
    run([
        git, "clone", "--depth", "1", "--branch", EDK2_REF,
        "--recurse-submodules", "--shallow-submodules", EDK2_URL, str(SOURCE),
    ])
    return SOURCE


def validate_pristine_source(source: Path) -> None:
    dec = (source / "MdeModulePkg/MdeModulePkg.dec").read_text(encoding="utf-8")
    video = (source / "OvmfPkg/QemuVideoDxe/ComponentName.c").read_text(encoding="utf-8")
    smbios = (source / "OvmfPkg/SmbiosPlatformDxe/SmbiosPlatformDxe.c").read_text(encoding="utf-8")
    expected = (
        'PcdFirmwareVendor|L"EDK II"',
        'PcdAcpiDefaultOemId|"INTEL "',
        "PcdAcpiDefaultOemTableId|0x20202020324B4445",
    )
    if not all(item in dec for item in expected):
        raise RuntimeError("resources/ovmfbackup 的固件/ACPI PCD 已被修改，不是原版源码")
    if 'L"QEMU Video Driver"' not in video or "0x1C // SystemReserved" not in smbios:
        raise RuntimeError("resources/ovmfbackup 已包含旧 OVMF 补丁，请换回原版源码")
    status = run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
        capture=True,
    ).stdout.strip()
    if status:
        raise RuntimeError("resources/ovmfbackup 或其子模块不是干净的固定版本源码")


def source_revision(source: Path) -> str:
    if not (source / ".git").exists() or not shutil.which("git"):
        return EDK2_REF
    try:
        return run(["git", "-c", f"safe.directory={source}", "-C", str(source), "rev-parse", "HEAD"], capture=True).stdout.strip()
    except subprocess.CalledProcessError:
        return EDK2_REF


def synchronize_locked_source(source: Path) -> None:
    if not (source / ".git").exists():
        raise RuntimeError("resources/ovmfbackup 缺少 Git 元数据，无法验证和锁定 EDK2 源码")
    git_prefix = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    expected = run(git_prefix + ["rev-list", "-n", "1", EDK2_REF], capture=True).stdout.strip()
    current = run(git_prefix + ["rev-parse", "HEAD"], capture=True).stdout.strip()
    if not expected or current != expected:
        raise RuntimeError(f"resources/ovmfbackup 未锁定到 {EDK2_REF}")

    top_status = run(
        git_prefix + ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
        capture=True,
    ).stdout.strip()
    if top_status:
        raise RuntimeError("resources/ovmfbackup 顶层源码存在修改，拒绝自动覆盖")

    print("同步并锁定 EDK2 递归子模块 ...")
    run(git_prefix + ["submodule", "sync", "--recursive"])
    run(git_prefix + [
        "submodule", "update", "--init", "--recursive", "--force", "--checkout", "--depth", "1",
    ])

    manifest = submodule_manifest(source)
    unlocked = [line for line in manifest if line[:1] in {"+", "-", "U"}]
    if unlocked:
        raise RuntimeError("EDK2 子模块未锁定:\n" + "\n".join(unlocked))

    required = (
        source / "CryptoPkg/Library/OpensslLib/openssl/Configure",
        source / "MdeModulePkg/Library/BrotliCustomDecompressLib/brotli/c/common/platform.c",
    )
    missing = [str(path.relative_to(source)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("EDK2 锁定子模块仍不完整: " + ", ".join(missing))
    validate_pristine_source(source)


def submodule_manifest(source: Path) -> list[str]:
    if not (source / ".git").exists() or not shutil.which("git"):
        return []
    result = run(
        ["git", "-c", f"safe.directory={source}", "-C", str(source), "submodule", "status", "--recursive"],
        capture=True,
    )
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def copy_source(source: Path, target: Path) -> None:
    def ignored(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in {".git", "__pycache__"} or name.endswith(".pyc")}
        if Path(directory) == source:
            skipped.update(name for name in names if name in {"Build", "Conf"})
        return skipped

    shutil.copytree(source, target, symlinks=True, ignore=ignored)
    # BaseTools writes generated configuration here but does not create it.
    (target / "Conf").mkdir()


def install_host_logo(source: Path) -> dict[str, object]:
    """Embed the host's ACPI BGRT BMP as OVMF's static boot logo.

    OVMF's LogoDxe consumes a single BMP image; it does not support animated
    GIF/video assets.  BGRT is therefore the closest firmware-native source
    and matches the behavior of the legacy ovmfpatch.sh script.
    """
    target = source / "MdeModulePkg/Logo/Logo.bmp"
    if not HOST_BGRT_IMAGE.is_file():
        print("未找到宿主 BGRT Logo，保留 EDK2 默认启动 Logo。")
        return {"applied": False, "source": str(HOST_BGRT_IMAGE), "sha256": None}
    data = HOST_BGRT_IMAGE.read_bytes()
    if len(data) < 2 or data[:2] != b"BM":
        raise RuntimeError(f"宿主 BGRT Logo 不是有效 BMP: {HOST_BGRT_IMAGE}")
    target.write_bytes(data)
    print(f"已提取宿主 BGRT Logo: {HOST_BGRT_IMAGE} -> {target}")
    return {
        "applied": True,
        "source": str(HOST_BGRT_IMAGE),
        "target": "MdeModulePkg/Logo/Logo.bmp",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def replace_once(path: Path, pattern: str, replacement: str, label: str, *, flags: int = re.MULTILINE) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, lambda _match: replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match in {path}, got {count}")
    path.write_text(updated, encoding="utf-8")


def patch_source(source: Path, profile: dict) -> None:
    firmware = profile["hardware"]["firmware"]
    vendor = firmware["vendor"]
    version = firmware["version"]
    date = firmware["date"]
    oem_id = profile["ovmf_policy"]["acpi_oem_id"][:6].ljust(6)
    table_id = profile["ovmf_policy"]["acpi_table_id"][:8].ljust(8)
    table_id_number = int.from_bytes(table_id.encode("ascii"), "little")
    creator_id = profile["ovmf_policy"]["acpi_creator_id"][:4].ljust(4)
    creator_id_number = int.from_bytes(creator_id.encode("ascii"), "little")
    creator_revision = int(profile["ovmf_policy"]["acpi_creator_revision"])

    dec = source / "MdeModulePkg/MdeModulePkg.dec"
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdFirmwareVendor\|L"[^"]*"', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdFirmwareVendor|L"{vendor}"', "firmware vendor")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdFirmwareVersionString\|L"[^"]*"', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdFirmwareVersionString|L"{version}"', "firmware version")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdFirmwareReleaseDateString\|L"[^"]*"', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdFirmwareReleaseDateString|L"{date}"', "firmware date")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdAcpiDefaultOemId\|"[^"]*"', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdAcpiDefaultOemId|"{oem_id}"', "ACPI OEM ID")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdAcpiDefaultOemTableId\|0x[0-9A-Fa-f]+', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdAcpiDefaultOemTableId|0x{table_id_number:016X}', "ACPI table ID")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdAcpiDefaultCreatorId\|0x[0-9A-Fa-f]+', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdAcpiDefaultCreatorId|0x{creator_id_number:08X}', "ACPI creator ID")
    replace_once(dec, r'^\s*gEfiMdeModulePkgTokenSpaceGuid\.PcdAcpiDefaultCreatorRevision\|0x[0-9A-Fa-f]+', f'  gEfiMdeModulePkgTokenSpaceGuid.PcdAcpiDefaultCreatorRevision|0x{creator_revision:08X}', "ACPI creator revision")

    smbios = source / "OvmfPkg/SmbiosPlatformDxe/SmbiosPlatformDxe.c"
    replace_once(smbios, r'0xE800, // UINT16\s+ BiosSegment', '0,', "SMBIOS BIOS segment")
    replace_once(smbios, r'^\s*0,\s*// UINT8\s+ BiosSize', '  0x3F,', "SMBIOS BIOS size")
    replace_once(
        smbios,
        r'^\s*0x1C // SystemReserved = VirtualMachineSupported \|\n\s*//\s+UefiSpecificationSupported \|\n\s*//\s+TargetContentDistributionEnabled',
        '    0x18',
        "SMBIOS VM characteristic",
    )
    replace_once(smbios, r'^\s*0,\s*// UINT8\s+ SystemBiosMajorRelease', '  0xFF,  // UINT8                     SystemBiosMajorRelease', "SMBIOS BIOS major")
    replace_once(smbios, r'^\s*0,\s*// UINT8\s+ SystemBiosMinorRelease', '  0xFF,  // UINT8                     SystemBiosMinorRelease', "SMBIOS BIOS minor")
    replace_once(
        smbios,
        r'^\s*0xFF\s+// UINT8\s+ EmbeddedControllerFirmwareMinorRelease$',
        '  0xFF,\n  { 0, 0 }',
        "SMBIOS extended BIOS size",
    )

    shell = source / "ShellPkg/ShellPkg.dec"
    replace_once(shell, r'PcdShellSupplier\|L"[^"]*"', f'PcdShellSupplier|L"{vendor}"', "UEFI shell supplier")

    component = source / "OvmfPkg/QemuVideoDxe/ComponentName.c"
    replace_once(component, r'L"QEMU Video Driver"', f'L"{vendor} Graphics Output Driver"', "video driver name")
    replace_once(component, r'L"QEMU Video PCI Adapter"', 'L"PCI Graphics Adapter"', "video controller name")

    video_driver = source / "OvmfPkg/QemuVideoDxe/Driver.c"
    for old, new in (
        ('L"QEMU Standard VGA"', 'L"PCI Graphics Adapter"'),
        ('L"QEMU Standard VGA (secondary)"', 'L"PCI Graphics Adapter (secondary)"'),
        ('L"QEMU QXL VGA"', 'L"PCI Graphics Adapter"'),
        ('L"QEMU VirtIO VGA"', 'L"PCI Graphics Adapter"'),
        ('L"QEMU VMWare SVGA"', 'L"PCI Graphics Adapter"'),
    ):
        replace_once(video_driver, re.escape(old), new, f"video card label {old}")

    sio = source / "OvmfPkg/SioBusDxe/ComponentName.c"
    replace_once(sio, r'L"OVMF Sio Bus Driver"', f'L"{vendor} SIO Bus Driver"', "SIO driver name")
    for name in ("QemuQ35.c", "QemuPC.c"):
        hsti = source / "OvmfPkg/VirtHstiDxe" / name
        replace_once(hsti, r'L"OVMF \(Qemu (?:Q35|PC)\)"', f'L"{vendor} Platform Security"', f"HSTI publisher {name}")

    boot_order = source / "OvmfPkg/Library/QemuBootOrderLib/QemuBootOrderLib.c"
    replace_once(boot_order, r'L"VMMBootOrder%04x"', 'L"PlatformBoot%04x"', "private boot-order variable")

    cache = source / "OvmfPkg/Library/QemuFwCfgLib/QemuFwCfgCacheInit.c"
    replace_once(cache, r'#define EV_POSTCODE_INFO_QEMU_FW_CFG_DATA\s+"QEMU FW CFG"', '#define EV_POSTCODE_INFO_QEMU_FW_CFG_DATA  "Firmware Config"', "fw_cfg TPM event label")

    host_bridge = profile.get("devices", {}).get("pci_identities", {}).get("host_bridge", {})
    try:
        host_bridge_did = int(host_bridge["device_id"], 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 Host Bridge DID，OVMF 无法与 QEMU 对齐") from exc
    if host_bridge_did in {0, 0xFFFF}:
        raise RuntimeError(f"Host Bridge DID 无效: {host_bridge_did:#06x}")
    q35 = source / "OvmfPkg/Include/IndustryStandard/Q35MchIch9.h"
    replace_once(
        q35,
        r"^#define INTEL_Q35_MCH_DEVICE_ID\s+0x29C0",
        f"#define INTEL_Q35_MCH_DEVICE_ID  0x{host_bridge_did:04X}",
        "Q35 host bridge DID",
    )
    if f"#define INTEL_Q35_MCH_DEVICE_ID  0x{host_bridge_did:04X}" not in q35.read_text(encoding="utf-8"):
        raise RuntimeError("OVMF Host Bridge DID 没有写入 Q35MchIch9.h")
    try:
        hotplug = int(str(profile.get("hardware", {}).get("cpu_hotplug_io_base", "")), 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 CPU 热插拔 IO 基址，OVMF 无法与 QEMU 对齐") from exc
    if not (0x4000 <= hotplug <= 0x7FF0) or hotplug & 0xF or hotplug == 0x0CD8:
        raise RuntimeError(f"CPU 热插拔 IO 基址无效或仍是 QEMU 默认口: {hotplug:#x}")
    replace_once(
        q35,
        r"^#define ICH9_CPU_HOTPLUG_BASE\s+0x0CD8",
        f"#define ICH9_CPU_HOTPLUG_BASE  0x{hotplug:04X}",
        "ICH9 CPU hotplug IO base",
    )
    q35_hotplug = q35.read_text(encoding="utf-8")
    if f"#define ICH9_CPU_HOTPLUG_BASE  0x{hotplug:04X}" not in q35_hotplug:
        raise RuntimeError("OVMF CPU 热插拔 IO 基址没有写入 Q35MchIch9.h")
    if re.search(r"^#define ICH9_CPU_HOTPLUG_BASE\s+0x0CD8", q35_hotplug, re.MULTILINE):
        raise RuntimeError("OVMF 仍使用默认 CPU 热插拔口 0x0CD8")

    lpc = profile.get("devices", {}).get("pci_identities", {}).get("lpc") or {}
    address = lpc.get("pci_address") or {}
    try:
        lpc_bus = int(address["bus"], 0)
        lpc_slot = int(address["slot"], 0)
        lpc_func = int(address["function"], 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("身份文件缺少 LPC PCI 槽位，OVMF 无法对齐 PMBASE") from exc
    if lpc.get("slot_source") != "host-observed" or lpc_bus != 0 or not (0 <= lpc_slot <= 31 and 0 <= lpc_func <= 7):
        raise RuntimeError("LPC PCI 槽位无效，OVMF 无法对齐南桥")
    replace_once(
        q35,
        r"^#define POWER_MGMT_REGISTER_Q35\(Offset\) \\\n  PCI_LIB_ADDRESS \(0, 0x1f, 0, \(Offset\)\)",
        "#define POWER_MGMT_REGISTER_Q35(Offset) \\\n"
        f"  PCI_LIB_ADDRESS (0, 0x{lpc_slot:x}, {lpc_func}, (Offset))",
        "Q35 LPC PMBASE address",
    )
    replace_once(
        q35,
        r"^#define POWER_MGMT_REGISTER_Q35_EFI_PCI_ADDRESS\(Offset\) \\\n  EFI_PCI_ADDRESS \(0, 0x1f, 0, \(Offset\)\)",
        "#define POWER_MGMT_REGISTER_Q35_EFI_PCI_ADDRESS(Offset) \\\n"
        f"  EFI_PCI_ADDRESS (0, 0x{lpc_slot:x}, {lpc_func}, (Offset))",
        "Q35 LPC EFI PMBASE address",
    )
    q35_text = q35.read_text(encoding="utf-8")
    if f"PCI_LIB_ADDRESS (0, 0x{lpc_slot:x}, {lpc_func}, (Offset))" not in q35_text:
        raise RuntimeError("OVMF LPC PMBASE 地址没有写入 Q35MchIch9.h")
    for rel in (
        "OvmfPkg/Library/PlatformBootManagerLib/BdsPlatform.c",
        "OvmfPkg/Library/PlatformBootManagerLibBhyve/BdsPlatform.c",
    ):
        path = source / rel
        if not path.is_file():
            continue
        old = "PCI_LIB_ADDRESS (0, 0x1f, 0,"
        new = f"PCI_LIB_ADDRESS (0, 0x{lpc_slot:x}, {lpc_func},"
        if old == new:
            continue
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count < 1:
            raise RuntimeError(f"{rel} 缺少 LPC PIRQ 访问点")
        path.write_text(text.replace(old, new), encoding="utf-8")
    for rel in (
        "OvmfPkg/Library/PlatformBootManagerLib/BdsPlatform.h",
        "OvmfPkg/Library/PlatformBootManagerLibBhyve/BdsPlatform.h",
    ):
        path = source / rel
        if not path.is_file():
            continue
        old = "PCI_DEVICE_PATH_NODE(0, 0x1f)"
        new = f"PCI_DEVICE_PATH_NODE({lpc_func}, 0x{lpc_slot:x})"
        if old == new:
            continue
        replace_once(path, re.escape(old), new, f"{rel} ISA bridge path")


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
    if profile.get("ovmf_policy", {}).get("secure_boot") is not False:
        raise RuntimeError("当前管理策略要求明确关闭 Secure Boot")

    tools = require_tools(("git", "make", "gcc", "g++", "nasm", "python3", "iasl", "qemu-img"))
    source = ensure_source()
    synchronize_locked_source(source)
    revision = source_revision(source)
    submodules = submodule_manifest(source)

    prepare_output_directory(OUT)
    OUT.mkdir(parents=True)
    source_copy = OUT / "source"
    print(f"复制原版 OVMF 源码: {source}")
    copy_source(source, source_copy)
    logo = install_host_logo(source_copy)
    patch_source(source_copy, profile)

    jobs = build_jobs()
    version = profile["hardware"]["firmware"]["version"]
    # EDK2's VfrCompile makefile declares several generated files as independent
    # targets for one Antlr recipe. Parallel BaseTools builds can therefore run
    # that generator more than once and compile a half-written C++ file.
    run(["make", "-C", "BaseTools", "-j1"], source_copy)
    command = (
        "set -Eeo pipefail; "
        "source edksetup.sh; "
        "build -D FD_SIZE_4MB -D SMM_REQUIRE -D TPM1_ENABLE -D TPM2_ENABLE "
        '-D FIRMWARE_VER="$OVO_FIRMWARE_VERSION" '
        f"-a X64 -p OvmfPkg/OvmfPkgX64.dsc -b RELEASE -t GCC5 -n {jobs} -s -q"
    )
    run(
        ["bash", "-lc", command],
        source_copy,
        env={**os.environ, "JOBS": jobs, "OVO_FIRMWARE_VERSION": version},
    )
    code = source_copy / "Build/OvmfX64/RELEASE_GCC5/FV/OVMF_CODE.fd"
    vars_file = source_copy / "Build/OvmfX64/RELEASE_GCC5/FV/OVMF_VARS.fd"
    for path in (code, vars_file):
        if not path.is_file():
            raise RuntimeError(f"OVMF 构建缺少 {path}")

    code_out = OUT / "OVMF_CODE_4M.patched.qcow2"
    if code.stat().st_size + vars_file.stat().st_size != FIRMWARE_SIZE:
        raise RuntimeError("OVMF CODE plus VARS size differs from the SMBIOS flash contract")
    vars_out = OUT / "OVMF_VARS_4M.patched.qcow2"
    run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(code), str(code_out)])
    run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(vars_file), str(vars_out)])
    info = {
        "profile_id": profile["meta"]["profile_id"],
        "profile_sha256": hashlib.sha256(PROFILE_PATH.read_bytes()).hexdigest(),
        "patch_revision": PATCH_REVISION,
        "platform_source": profile["meta"]["platform_source"],
        "platform_source_version": profile["meta"]["platform_source_version"],
        "platform_id": profile["meta"]["platform_id"],
        "source": {"path": str(source), "ref": EDK2_REF, "revision": revision},
        "source_submodules": submodules,
        "secure_boot": False,
        "flash_size_bytes": FIRMWARE_SIZE,
        "patches": ["profile-firmware-pcd", "profile-acpi-pcd", "profile-acpi-creator", "profile-q35-host-bridge-did", "profile-lpc-pmbase-devfn", "profile-cpu-hotplug-io", "secure-boot-disabled", "smbios-vm-bit-clear", "neutral-video-component", "neutral-hsti-publisher", "neutral-sio-component", "neutral-boot-variable", "neutral-fw-cfg-event"],
        "logo": logo,
        "tools": tools,
        "code": code_out.name,
        "vars_template": vars_out.name,
        "code_sha256": hashlib.sha256(code_out.read_bytes()).hexdigest(),
        "vars_sha256": hashlib.sha256(vars_out.read_bytes()).hexdigest(),
    }
    (OUT / "build-info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OVMF 产物已生成: {code_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"OVMF 构建失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
