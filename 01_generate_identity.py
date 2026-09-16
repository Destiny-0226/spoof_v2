#!/usr/bin/env python3
"""Generate the single source of truth for the new spoof stack.

The script is intentionally interactive: it lists libvirt domains, reads the
selected domain XML and host facts, creates one coherent hardware profile and
new stable identity values, then writes only two artifacts:

    artifacts/identity-hardware.json
    artifacts/smbios.bin

Running it again deliberately creates a new identity, as requested.  The
other scripts consume the JSON and never create their own random identifiers.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
SCHEMA_VERSION = 23
PLATFORM_SOURCE_VERSION = 3
Q35_DERIVED_SATA_ADDRESS = {
    "domain": "0x0000",
    "bus": "0x00",
    "slot": "0x1f",
    "function": "0x2",
}
ACPI_NAMESEG_RE = re.compile(r"\A[A-Z_][A-Z0-9_]{3}\Z")
RESERVED_ACPI_DEVICE_NAMESEGS = {
    "PCI0", "PIRQ", "APIC", "TIMR", "HPET", "GPE0", "GPEC", "SMI0", "SMID",
    "PRTP", "PRTA", "PCIR", "PHPR", "MCHC", "DRAC", "FWCF",
}

NETWORK_MODEL = "i226-v"
NETWORK_TEMPLATE = {
    "manufacturer": "Intel Corporation",
    "product": "Ethernet Controller I226-V",
    "pci_vendor_id": "8086",
    "pci_device_id": "125c",
    "revision_id": "04",
    # Intel's reference subsystem pair is deliberately non-OEM-specific.
    "subsystem_vendor_id": "8086",
    "subsystem_device_id": "0000",
    "pcie_spec_version": "3.1",
    "pcie_max_link_speed_gtps": 5.0,
    "pcie_link_width": 1,
    "link_speed_mbps": 2500,
    "rx_queues": 4,
    "tx_queues": 4,
    "msix_vectors": 5,
    "option_rom_size_kib": 1024,
    "phy_id": "67c9dc01",
    "firmware_version": "2017:888d",
    "extended_capabilities": ["aer-v2", "dsn", "ltr", "l1-pm-substates", "ptm"],
}

ROUTER_OUIS = (
    ("TP-Link Technologies", (0x50, 0xC7, 0xBF)),
    ("TP-Link Technologies", (0xD8, 0x07, 0xB6)),
    ("ASUSTek Computer", (0x2C, 0xFD, 0xA1)),
    ("ASUSTek Computer", (0x04, 0xD9, 0xF5)),
    ("NETGEAR", (0xA0, 0x04, 0x60)),
    ("NETGEAR", (0x9C, 0x3D, 0xCF)),
    ("Xiaomi Communications", (0x64, 0xCC, 0x2E)),
    ("Huawei Technologies", (0x48, 0x46, 0xFB)),
)

MEMORY_CATALOG: dict[tuple[str, str], dict[int, tuple[tuple[str, str], ...]]] = {
    ("ddr5", "laptop"): {
        4096: (("Samsung", "M425R512GB4-CQK"),),
        8192: (("Samsung", "M425R1GB4BB0-CQK"), ("Micron", "MTC4C10163S1SC48BA1"), ("SK hynix", "HMCG66MEBSA095N")),
        16384: (("Samsung", "M425R2GA3BB0-CQK"), ("Micron", "MTC8C1084S1SC48BA1"), ("SK hynix", "HMCG78MEBSA095N")),
        32768: (("Samsung", "M425R4GA3BB0-CQK"), ("SK hynix", "HMCG88MEBSA092N")),
    },
    ("ddr5", "desktop"): {
        4096: (("Samsung", "M323R512GB4-CQK"),),
        8192: (("Samsung", "M323R1GB4BB0-CQK"), ("Crucial", "CT8G48C40U5"), ("Kingston", "KF548C38BB-8")),
        16384: (("Samsung", "M323R2GA3BB0-CQK"), ("Crucial", "CT16G48C40U5"), ("Kingston", "KF552C40BB-16")),
        32768: (("Samsung", "M323R4GA3BB0-CQK"), ("Crucial", "CT32G48C40U5"), ("Kingston", "KF560C36BBE-32")),
        65536: (("Crucial", "CT64G48C40U5"), ("Kingston", "KF560C32RSA-64")),
    },
    ("ddr4", "laptop"): {
        4096: (("Samsung", "M471A5244CB0-CWE"), ("SK hynix", "HMA851S6CJR6N-XN")),
        8192: (("Samsung", "M471A1K43EB1-CWE"), ("Crucial", "CT8G4SFRA32A"), ("SK hynix", "HMA81GS6DJR8N-XN")),
        16384: (("Samsung", "M471A2K43EB1-CWE"), ("Crucial", "CT16G4SFRA32A"), ("SK hynix", "HMA82GS6DJR8N-XN")),
        32768: (("Samsung", "M471A4G43AB1-CWE"), ("Crucial", "CT32G4SFD832A")),
    },
    ("ddr4", "desktop"): {
        4096: (("Samsung", "M378A5244CB0-CWE"), ("Kingston", "KVR32N22S6/4")),
        8192: (("Samsung", "M378A1K43EB1-CWE"), ("Crucial", "CT8G4DFRA32A"), ("Kingston", "KVR32N22S8/8")),
        16384: (("Samsung", "M378A2K43EB1-CWE"), ("Crucial", "CT16G4DFRA32A"), ("Kingston", "KVR32N22D8/16")),
        32768: (("Samsung", "M378A4G43AB2-CWE"), ("Crucial", "CT32G4DFD832A"), ("Kingston", "KVR32N22D8/32")),
    },
}

STORAGE_CATALOG: dict[str, tuple[dict[str, str], ...]] = {
    "sata": (
        {"vendor": "ATA", "product": "Samsung SSD 870 EVO", "model": "Samsung SSD 870 EVO", "firmware": "SVT02B6Q", "serial_prefix": "S6P", "wwn_prefix": "5002538"},
        {"vendor": "ATA", "product": "Crucial MX500", "model": "CT1000MX500SSD1", "firmware": "M3CR046", "serial_prefix": "23", "wwn_prefix": "500a075"},
        {"vendor": "ATA", "product": "WDC WD10EZEX", "model": "WDC WD10EZEX-08WN4A0", "firmware": "01.01A01", "serial_prefix": "WD-WCC6", "wwn_prefix": "50014ee"},
        {"vendor": "ATA", "product": "KINGSTON SA400S37", "model": "KINGSTON SA400S37", "firmware": "SBFK71E0", "serial_prefix": "50026B", "wwn_prefix": "50026b7"},
    ),
    "nvme": (
        {"vendor": "SAMSUNG", "product": "SSD 980 PRO", "model": "Samsung SSD 980 PRO", "firmware": "5B2QGXA7", "serial_prefix": "S5GX", "wwn_prefix": "002538"},
        {"vendor": "SK hynix", "product": "PC801 NVMe", "model": "SK hynix PC801", "firmware": "51003141", "serial_prefix": "AJA", "wwn_prefix": "000000"},
        {"vendor": "WD", "product": "WD_BLACK SN850X", "model": "WD_BLACK SN850X", "firmware": "620331WD", "serial_prefix": "222", "wwn_prefix": "001b44"},
        {"vendor": "Crucial", "product": "CT1000P5PSSD8", "model": "CT1000P5PSSD8", "firmware": "P7CR403", "serial_prefix": "224", "wwn_prefix": "00a075"},
    ),
    "scsi": (
        {"vendor": "SAMSUNG", "product": "MZ7L3480HCHQ", "model": "MZ7L3480HCHQ", "firmware": "GDC5", "serial_prefix": "S4G7", "wwn_prefix": "5002538"},
        {"vendor": "SEAGATE", "product": "ST1200MM0129", "model": "ST1200MM0129", "firmware": "C005", "serial_prefix": "WFK", "wwn_prefix": "5000c50"},
    ),
    "cdrom": (
        {"vendor": "HL-DT-ST", "product": "DVDRAM GUD1N", "model": "HL-DT-ST DVDRAM GUD1N", "firmware": "1.00"},
        {"vendor": "PLDS", "product": "DVD+-RW DU-8A5LH", "model": "PLDS DVD+-RW DU-8A5LH", "firmware": "6D1M"},
        {"vendor": "MATSHITA", "product": "DVD-RAM UJ8E2", "model": "MATSHITA DVD-RAM UJ8E2", "firmware": "1.00"},
    ),
}

HID_CATALOG = (
    {
        "manufacturer": "Logitech", "mouse_product": "USB Optical Mouse", "mouse_vendor_id": "046d", "mouse_product_id": "c077",
        "keyboard_product": "USB Keyboard", "keyboard_vendor_id": "046d", "keyboard_product_id": "c31c",
    },
    {
        "manufacturer": "Dell", "mouse_product": "Dell MS116 USB Optical Mouse", "mouse_vendor_id": "413c", "mouse_product_id": "301a",
        "keyboard_product": "Dell KB216 Wired Keyboard", "keyboard_vendor_id": "413c", "keyboard_product_id": "2113",
    },
    {
        "manufacturer": "Lenovo", "mouse_product": "Lenovo USB Optical Mouse", "mouse_vendor_id": "17ef", "mouse_product_id": "608d",
        "keyboard_product": "Lenovo Traditional USB Keyboard", "keyboard_vendor_id": "17ef", "keyboard_product_id": "6099",
    },
)


def cache_size_kib(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([KMG])?\s*", value, re.I)
    if not match:
        return 0
    scale = {None: 1, "K": 1, "M": 1024, "G": 1024 * 1024}
    suffix = match.group(2).upper() if match.group(2) else None
    return int(match.group(1)) * scale[suffix]


def ascii_clean(value: Any, fallback: str = "") -> str:
    text = str(value or "").encode("ascii", "ignore").decode("ascii")
    text = "".join(char for char in text if " " <= char <= "~" and char not in {'"', "\\", "|"})
    return " ".join(text.split()) or fallback


def host_mce_banks() -> int:
    """Count architectural MCE banks from host sysfs.

    QEMU's MCE_BANKS_DEF is both the CPUID/MCG_CAP count and the emulator
    array size.  Inventing 32 on a CPU that only implements 20 is as much a
    tell as leaving QEMU's default 10.
    """
    root = Path("/sys/devices/system/machinecheck/machinecheck0")
    if not root.is_dir():
        raise RuntimeError("宿主没有 machinecheck sysfs，无法采集 MCE bank")
    indexes = set()
    for path in root.iterdir():
        match = re.fullmatch(r"bank(\d+)", path.name)
        if match:
            indexes.add(int(match.group(1)))
    if not indexes:
        raise RuntimeError("宿主 MCE bank 列表为空")
    count = max(indexes) + 1
    if indexes != set(range(count)):
        raise RuntimeError(f"宿主 MCE bank 编号不连续: {sorted(indexes)}")
    if not (1 <= count <= 32):
        raise RuntimeError(f"宿主 MCE bank 数量 {count} 超出当前 QEMU 可承载范围 1-32")
    return count


def allocate_cpu_hotplug_io_base() -> int:
    """Pick a 16-byte-aligned ICH9 CPU hotplug IO base that is not QEMU's 0x0CD8."""
    base = 0x4000 + secrets.randbelow(1024) * 16
    if not (0x4000 <= base <= 0x7FF0) or base & 0xF or base == 0x0CD8:
        raise RuntimeError(f"CPU 热插拔 IO 基址无效: {base:#x}")
    return base


def encode_host_smbios_table(kind: int, formatted: bytes, strings: list[str]) -> dict[str, Any]:
    if len(formatted) < 4:
        raise RuntimeError(f"宿主 SMBIOS Type {kind} 长度不足")
    cleaned: list[str] = []
    for value in strings:
        text = ascii_clean(value)
        cleaned.append(text if text else "NA")
    while cleaned and cleaned[-1] == "NA":
        cleaned.pop()
    return {
        "type": kind,
        "body": formatted[4:].hex(),
        "strings": cleaned,
        "source": "host-observed",
    }


def host_cache_sizes() -> dict[str, int]:
    totals: dict[str, dict[str, int]] = {"l1": {}, "l2": {}, "l3": {}}
    try:
        for cpu in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cache/index*"):
            level = cpu.joinpath("level").read_text().strip()
            kind = cpu.joinpath("type").read_text().strip().lower()
            size = cache_size_kib(cpu.joinpath("size").read_text())
            shared = cpu.joinpath("shared_cpu_list").read_text().strip()
            key = f"{level}:{kind}:{shared}:{size}"
            if level in {"1", "2", "3"}:
                totals[f"l{level}"][key] = size
    except OSError:
        pass
    result = {name: sum(items.values()) for name, items in totals.items()}
    missing = [name.upper() for name, size in result.items() if size <= 0]
    if missing:
        raise RuntimeError("无法读取宿主缓存层级，不允许使用 fallback: " + ", ".join(missing))
    return result


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "command failed: " + " ".join(command))
    return result


def read_privileged_bytes(path: Path, minimum_size: int = 1) -> bytes:
    """Read a host firmware table without substituting synthetic data."""
    try:
        data = path.read_bytes()
        if len(data) >= minimum_size:
            return data
    except PermissionError:
        pass
    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError(f"读取宿主硬件表需要 root 或 sudo: {path}")
    result = subprocess.run([sudo, "cat", str(path)], stdout=subprocess.PIPE)
    if result.returncode != 0 or len(result.stdout) < minimum_size:
        raise RuntimeError(f"无法完整读取宿主硬件表，不允许使用 fallback: {path}")
    return result.stdout


def read_privileged_text(path: Path) -> str:
    return ascii_clean(read_privileged_bytes(path).decode(errors="ignore"))


def virsh_prefix() -> list[str]:
    system = run(["virsh", "--connect", "qemu:///system", "list", "--all", "--name"], check=False)
    return ["virsh", "--connect", "qemu:///system"] if system.returncode == 0 else ["virsh"]


def list_domains(prefix: list[str]) -> list[str]:
    result = run(prefix + ["list", "--all", "--name"])
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def choose_domain(names: list[str]) -> str:
    print("可用虚拟机：")
    for index, name in enumerate(names, 1):
        print(f"  {index}) {name}")
    while True:
        answer = input("请选择虚拟机编号：").strip()
        try:
            selected = names[int(answer) - 1]
        except (ValueError, IndexError):
            print("请输入有效编号。")
            continue
        return selected


def read_domain_xml(prefix: list[str], name: str) -> str:
    return run(prefix + ["dumpxml", name]).stdout


def unit_multiplier(unit: str) -> int:
    return {
        "b": 1,
        "bytes": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
    }.get(unit.lower(), 1)


def memory_bytes(root: ET.Element) -> int:
    node = root.find("memory")
    if node is None or not (node.text or "").strip():
        return 8 * 1024**3
    value = int(node.text.strip(), 0)
    return max(256 * 1024**2, value * unit_multiplier(node.get("unit", "KiB")))


def cpu_topology(root: ET.Element) -> dict[str, int]:
    vcpu_node = root.find("vcpu")
    vcpu = int((vcpu_node.text or "1").strip(), 0) if vcpu_node is not None else 1
    topo = root.find("cpu/topology")
    if topo is None:
        return {"vcpus": vcpu, "sockets": 1, "dies": 1, "clusters": 1, "cores": vcpu, "threads": 1}
    values = {
        "vcpus": vcpu,
        "sockets": int(topo.get("sockets", "1"), 0),
        "dies": int(topo.get("dies", "1"), 0),
        "clusters": int(topo.get("clusters", "1"), 0),
        "cores": int(topo.get("cores", str(vcpu)), 0),
        "threads": int(topo.get("threads", "1"), 0),
    }
    product = values["sockets"] * values["dies"] * values["clusters"] * values["cores"] * values["threads"]
    if product != vcpu:
        raise ValueError(f"XML CPU topology ({product}) does not match vCPU count ({vcpu})")
    return values


def parse_storage(root: ET.Element) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") not in {"disk", "cdrom", "floppy"}:
            continue
        target = disk.find("target")
        source = disk.find("source")
        driver = disk.find("driver")
        devices.append(
            {
                "device": disk.get("device", "disk"),
                "type": disk.get("type", "file"),
                "target": dict(target.attrib) if target is not None else {},
                "source": dict(source.attrib) if source is not None else {},
                "driver": dict(driver.attrib) if driver is not None else {},
                "bus": (target.get("bus") if target is not None else "") or "unknown",
            }
        )
    return devices


def parse_interfaces(root: ET.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for interface in root.findall("./devices/interface"):
        mac = interface.find("mac")
        model = interface.find("model")
        result.append(
            {
                "type": interface.get("type", "network"),
                "source": dict((interface.find("source").attrib if interface.find("source") is not None else {})),
                "model": model.get("type") if model is not None else "",
                "model_target": NETWORK_MODEL,
            }
        )
    return result


def parse_hostdevs(root: ET.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for hostdev in root.findall("./devices/hostdev"):
        entry: dict[str, Any] = {
            "type": hostdev.get("type", ""),
            "mode": hostdev.get("mode", ""),
            "managed": hostdev.get("managed", ""),
            "address": "",
            "class_code": "",
            "is_display": False,
            "is_audio": False,
        }
        address = hostdev.find("source/address")
        if entry["type"] == "pci" and address is not None:
            try:
                domain = int(address.get("domain", "0"), 0)
                bus = int(address.get("bus", "0"), 0)
                slot = int(address.get("slot", "0"), 0)
                function = int(address.get("function", "0"), 0)
                bdf = f"{domain:04x}:{bus:02x}:{slot:02x}.{function:x}"
                entry["address"] = bdf
                class_path = Path("/sys/bus/pci/devices") / bdf / "class"
                class_code = read_int(class_path)
                entry["class_code"] = f"0x{class_code:06x}" if class_code else ""
                entry["is_display"] = ((class_code >> 16) & 0xFF) == 0x03
                entry["is_audio"] = ((class_code >> 8) & 0xFFFF) == 0x0403
            except (TypeError, ValueError):
                pass
        result.append(entry)
    return result


def host_cpu() -> dict[str, Any]:
    text = Path("/proc/cpuinfo").read_text(errors="ignore")
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"vendor_id", "model name", "cpu family", "model", "stepping"}:
            values.setdefault(key.strip(), value.strip())
    vendor = values.get("vendor_id", "")
    try:
        family = int(values.get("cpu family", "6"))
        model = int(values.get("model", "0"))
        stepping = int(values.get("stepping", "0"))
    except ValueError:
        raise RuntimeError("无法解析宿主 CPU family/model/stepping")
    max_khz = read_int(Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"))
    base_khz = read_int(Path("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency"))
    current_mhz = 0
    for line in text.splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                current_mhz = int(float(line.split(":", 1)[1].strip()))
            except (ValueError, IndexError):
                pass
            break
    flags: set[str] = set()
    for line in text.splitlines():
        if line.lower().startswith(("flags", "features")):
            flags.update(line.partition(":")[2].split())
            break

    # CPUID.1:EAX stores the high model nibble separately for family 6/15.
    base_family = min(family, 0xF)
    extended_family = family - 0xF if family >= 0xF else 0
    base_model = model & 0xF
    extended_model = (model >> 4) & 0xF if family in {6, 15} else 0
    signature = (
        (stepping & 0xF)
        | (base_model << 4)
        | (base_family << 8)
        | (extended_model << 16)
        | ((extended_family & 0xFF) << 20)
    )
    edx_names = {
        0: "fpu", 1: "vme", 2: "de", 3: "pse", 4: "tsc", 5: "msr",
        6: "pae", 7: "mce", 8: "cx8", 9: "apic", 11: "sep",
        12: "mtrr", 13: "pge", 14: "mca", 15: "cmov", 16: "pat",
        17: "pse36", 18: "psn", 19: "clflush", 21: "ds", 22: "acpi",
        23: "mmx", 24: "fxsr", 25: "sse", 26: "sse2", 27: "ss",
        28: "ht", 29: "tm", 31: "pbe",
    }
    features_edx = sum(1 << bit for bit, name in edx_names.items() if name in flags)
    topology: set[tuple[str, str]] = set()
    for cpu in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
        try:
            if cpu.joinpath("online").exists() and cpu.joinpath("online").read_text().strip() == "0":
                continue
            package = cpu.joinpath("topology/physical_package_id").read_text().strip()
            core = cpu.joinpath("topology/core_id").read_text().strip()
            topology.add((package, core))
        except OSError:
            continue
    if not topology:
        raise RuntimeError("无法读取宿主 CPU 物理核心拓扑")
    return {
        "vendor": vendor,
        "vendor_name": "AuthenticAMD" if vendor == "AuthenticAMD" else "GenuineIntel",
        "model_name": ascii_clean(values.get("model name")),
        "family": family,
        "model": model,
        "stepping": stepping,
        "signature": signature,
        "features_edx": features_edx,
        "flags": sorted(flags),
        "physical_cores": len(topology),
        "logical_cpus": os.cpu_count() or 1,
        "max_mhz": max_khz // 1000 if max_khz else 0,
        "base_mhz": base_khz // 1000 if base_khz else 0,
        "current_mhz": current_mhz,
    }


def host_memory_kib() -> int:
    for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return 0


def host_dmi() -> dict[str, str]:
    root = Path("/sys/class/dmi/id")
    result: dict[str, str] = {}
    for name in (
        "sys_vendor", "product_name", "product_version", "product_family",
        "product_sku",
        "board_vendor", "board_name", "board_version", "bios_vendor", "bios_version",
        "bios_date", "bios_release", "ec_firmware_release",
        "chassis_vendor", "chassis_version", "chassis_type",
    ):
        try:
            result[name] = ascii_clean(root.joinpath(name).read_text(errors="ignore"))
        except OSError:
            result[name] = ""
    dmidecode_fields = {
        "sys_vendor": "system-manufacturer", "product_name": "system-product-name",
        "product_version": "system-version", "product_sku": "system-sku-number",
        "board_vendor": "baseboard-manufacturer", "board_name": "baseboard-product-name",
        "board_version": "baseboard-version", "bios_vendor": "bios-vendor",
        "bios_version": "bios-version", "bios_date": "bios-release-date",
        "chassis_vendor": "chassis-manufacturer", "chassis_version": "chassis-version",
        "chassis_type": "chassis-type",
        "bios_release": "bios-revision",
    }
    if shutil.which("dmidecode"):
        for name, selector in dmidecode_fields.items():
            if result.get(name):
                continue
            probe = run(["dmidecode", "--string", selector], check=False)
            if probe.returncode == 0:
                result[name] = ascii_clean(probe.stdout)
    return result


def host_unique_identifiers() -> dict[str, str]:
    """Read unique DMI values transiently so generated values cannot copy them."""
    root = Path("/sys/class/dmi/id")
    result: dict[str, str] = {}
    for name in ("product_serial", "board_serial", "chassis_serial", "product_uuid"):
        value = read_privileged_text(root / name)
        if not value or value.lower() in {
            "default string", "to be filled by o.e.m.", "not specified", "unknown", "none",
        }:
            raise RuntimeError(f"宿主 DMI {name} 缺失，无法生成同格式的新身份")
        result[name] = value
    return result


def iter_smbios_structures(data: bytes) -> Iterable[tuple[int, bytes, list[str]]]:
    offset = 0
    while offset + 4 <= len(data):
        kind, length = data[offset], data[offset + 1]
        if length < 4 or offset + length > len(data):
            raise RuntimeError("宿主 SMBIOS structure stream 损坏")
        formatted = data[offset:offset + length]
        strings_start = offset + length
        strings_end = strings_start
        while strings_end + 1 < len(data) and data[strings_end:strings_end + 2] != b"\0\0":
            strings_end += 1
        if strings_end + 1 >= len(data):
            raise RuntimeError("宿主 SMBIOS structure 缺少字符串终止符")
        raw_strings = data[strings_start:strings_end]
        strings = [ascii_clean(value.decode(errors="ignore")) for value in raw_strings.split(b"\0")] if raw_strings else []
        yield kind, formatted, strings
        offset = strings_end + 2
        if kind == 127:
            return
    raise RuntimeError("宿主 SMBIOS 缺少 Type 127 终止结构")


def smbios_index(strings: list[str], index: int, field: str) -> str:
    if index < 1 or index > len(strings) or not strings[index - 1]:
        raise RuntimeError(f"宿主 SMBIOS {field} 缺失，不允许使用 fallback")
    return strings[index - 1]


def normalized_identifier_variants(values: Iterable[str]) -> set[str]:
    variants: set[str] = set()
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned:
            continue
        variants.add(cleaned)
        variants.add(re.sub(r"[^a-z0-9]", "", cleaned))
    return variants


def safe_oem_string(value: str, unique_variants: set[str]) -> bool:
    cleaned = ascii_clean(value).strip()
    compact = re.sub(r"[^a-z0-9]", "", cleaned.lower())
    if (
        not cleaned
        or cleaned.lower() in unique_variants
        or compact in unique_variants
        or any(len(identifier) >= 6 and identifier in compact for identifier in unique_variants)
    ):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{16,}", compact) or re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", cleaned
    ):
        return False
    return True


def host_smbios_facts(unique_identifiers: dict[str, str]) -> dict[str, Any]:
    data = read_privileged_bytes(Path("/sys/firmware/dmi/tables/DMI"))
    processor: dict[str, Any] | None = None
    modules: list[dict[str, Any]] = []
    memory_array: dict[str, Any] | None = None
    connectors: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    oem_strings: list[str] = []
    supplemental: list[dict[str, Any]] = []
    type_histogram: dict[str, int] = {}
    unique_variants = normalized_identifier_variants(unique_identifiers.values())
    memory_types = {0x1A: "ddr4", 0x22: "ddr5"}
    for kind, formatted, strings in iter_smbios_structures(data):
        type_histogram[str(kind)] = type_histogram.get(str(kind), 0) + 1
        if kind == 8:
            connectors.append(encode_host_smbios_table(8, formatted, strings))
        elif kind == 9:
            slots.append(encode_host_smbios_table(9, formatted, strings))
        elif kind == 4 and processor is None:
            if len(formatted) < 42:
                raise RuntimeError("宿主 SMBIOS Type 4 长度不足")
            processor = {
                "socket": smbios_index(strings, formatted[4], "processor socket"),
                "upgrade": formatted[25],
                "voltage": formatted[17],
                "external_clock_mhz": struct.unpack_from("<H", formatted, 18)[0],
                "characteristics": struct.unpack_from("<H", formatted, 38)[0],
            }
        elif kind == 11:
            oem_strings.extend(value for value in strings if safe_oem_string(value, unique_variants))
        elif kind in {12, 13, 26, 28, 29}:
            supplemental.append(encode_host_smbios_table(kind, formatted, strings))
        elif kind == 16 and memory_array is None:
            if len(formatted) < 15:
                raise RuntimeError("宿主 SMBIOS Type 16 长度不足")
            maximum_kib = struct.unpack_from("<I", formatted, 7)[0]
            if maximum_kib == 0x80000000:
                if len(formatted) < 23:
                    raise RuntimeError("宿主 SMBIOS Type 16 缺少 Extended Maximum Capacity")
                maximum_bytes = struct.unpack_from("<Q", formatted, 15)[0]
            else:
                maximum_bytes = maximum_kib * 1024
            memory_array = {
                "location": formatted[4],
                "use": formatted[5],
                "error_correction": formatted[6],
                "maximum_capacity_bytes": maximum_bytes,
                "device_slots": struct.unpack_from("<H", formatted, 13)[0],
            }
        elif kind == 17:
            if len(formatted) < 27:
                raise RuntimeError("宿主 SMBIOS Type 17 长度不足")
            size_field = struct.unpack_from("<H", formatted, 12)[0]
            installed = size_field not in {0, 0xFFFF}
            memory_type_code = formatted[18]
            memory_type = memory_types.get(memory_type_code) if installed else None
            if installed and memory_type is None:
                raise RuntimeError(
                    f"宿主内存类型 0x{memory_type_code:02x} 尚未实现，"
                    "不允许降级为 DDR4/DDR5 fallback"
                )
            speed = struct.unpack_from("<H", formatted, 32)[0] if installed and len(formatted) >= 34 else 0
            if installed and speed in {0, 0xFFFF}:
                speed = struct.unpack_from("<H", formatted, 21)[0]
            if installed and speed in {0, 0xFFFF}:
                raise RuntimeError("宿主 SMBIOS 没有有效的内存工作速度")
            modules.append({
                "installed": installed,
                "type": memory_type,
                "type_code": memory_type_code,
                "form_factor": formatted[14],
                "speed_mt": speed,
                "device_locator": smbios_index(strings, formatted[16], "memory device locator"),
                "bank_locator": strings[formatted[17] - 1] if formatted[17] and formatted[17] <= len(strings) else "",
            })
    if processor is None:
        raise RuntimeError("宿主 SMBIOS 缺少 Type 4 processor 信息")
    installed_modules = [item for item in modules if item["installed"]]
    if memory_array is None or not installed_modules:
        raise RuntimeError("宿主 SMBIOS 缺少 Type 16 或已安装的 Type 17")
    if memory_array["device_slots"] != len(modules):
        raise RuntimeError("宿主 SMBIOS Type 16 插槽数与 Type 17 数量不一致")
    types = {item["type"] for item in installed_modules}
    forms = {item["form_factor"] for item in installed_modules}
    speeds = {item["speed_mt"] for item in installed_modules}
    if len(types) != 1 or len(forms) != 1 or len(speeds) != 1:
        raise RuntimeError("宿主内存模块的类型、形态或工作速度不一致，无法生成单一可信配置")
    if len(connectors) > 255 or len(slots) > 255:
        raise RuntimeError("宿主 SMBIOS Type 8/9 数量超出当前可编码范围")
    return {
        "processor": processor,
        "type_histogram": type_histogram,
        "oem_strings": list(dict.fromkeys(oem_strings)),
        "supplemental": supplemental,
        "connectors": connectors,
        "slots": slots,
        "memory": {
            "type": installed_modules[0]["type"],
            "type_code": installed_modules[0]["type_code"],
            "form_factor": installed_modules[0]["form_factor"],
            "speed_mt": installed_modules[0]["speed_mt"],
            "installed_devices": len(installed_modules),
            "array": memory_array,
            "slots": modules,
        },
    }


def host_acpi_identity() -> dict[str, Any]:
    data = read_privileged_bytes(Path("/sys/firmware/acpi/tables/DSDT"))
    if len(data) < 36 or data[:4] != b"DSDT":
        raise RuntimeError("宿主 DSDT 表头无效")
    length = struct.unpack_from("<I", data, 4)[0]
    if length < 36 or length > len(data) or sum(data[:length]) & 0xFF:
        raise RuntimeError("宿主 DSDT 长度或校验和无效")
    fields = {
        "acpi_oem_id": data[10:16].decode("ascii", "strict"),
        "acpi_table_id": data[16:24].decode("ascii", "strict"),
        "acpi_creator_id": data[28:32].decode("ascii", "strict"),
        "acpi_creator_revision": struct.unpack_from("<I", data, 32)[0],
    }
    if any(any(ord(char) < 0x20 or ord(char) > 0x7E for char in str(value)) for key, value in fields.items() if key != "acpi_creator_revision"):
        raise RuntimeError("宿主 DSDT OEM/Creator 字段包含无效字符")
    return fields


def host_power_profile() -> dict[str, Any]:
    data = read_privileged_bytes(Path("/sys/firmware/acpi/tables/FACP"))
    if len(data) < 116 or data[:4] != b"FACP":
        raise RuntimeError("宿主 FADT 表头或长度无效")
    length = struct.unpack_from("<I", data, 4)[0]
    if length < 116 or length > len(data) or sum(data[:length]) & 0xFF:
        raise RuntimeError("宿主 FADT 长度或校验和无效")
    states = set((read_privileged_text(Path("/sys/power/state")) or "").split())
    mem_sleep = set(re.sub(r"[\[\]]", "", read_privileged_text(Path("/sys/power/mem_sleep")) or "").split())
    return {
        "preferred_pm_profile": data[45],
        "c2_latency_us": struct.unpack_from("<H", data, 96)[0],
        "c3_latency_us": struct.unpack_from("<H", data, 98)[0],
        "flags": struct.unpack_from("<I", data, 112)[0],
        "suspend_to_mem": "mem" in states and bool(mem_sleep & {"s2idle", "deep"}),
        "suspend_to_disk": "disk" in states,
        "mem_sleep_modes": sorted(mem_sleep),
        "source": "host-observed",
    }


def host_pci_subsystems() -> dict[str, dict[str, str]]:
    """Capture non-unique OEM subsystem pairs for matching device classes."""
    wanted = {
        "host_bridge": (0xFFFFFF, 0x060000),
        "root_port": (0xFFFFFF, 0x060400),
        "usb": (0xFFFFFF, 0x0C0330),
        "lpc": (0xFFFFFF, 0x060100),
        "smbus": (0xFFFFFF, 0x0C0500),
        "sata": (0xFFFFFF, 0x010601),
        "audio": (0xFFFF00, 0x040300),
        "display": (0xFF0000, 0x030000),
    }
    found: dict[str, dict[str, str]] = {}
    for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
        class_code = read_int(device / "class")
        for name, (mask, wanted_class) in wanted.items():
            if name in found or (class_code & mask) != wanted_class:
                continue
            subvendor = read_int(device / "subsystem_vendor")
            subdevice = read_int(device / "subsystem_device")
            if subvendor not in {0, 0xFFFF} and subdevice not in {0, 0xFFFF}:
                found[name] = {
                    "vendor_id": f"{subvendor:04x}",
                    "device_id": f"{subdevice:04x}",
                    "source": "host-observed",
                }
    # Q35 always exposes an AHCI function. On an NVMe-only host there is no
    # SATA function to copy, so bind that virtual function to the host's
    # observed platform/LPC OEM pair instead of falling back to QEMU IDs.
    if "sata" not in found and "lpc" in found:
        found["sata"] = dict(found["lpc"])
        found["sata"]["source"] = "derived-from-host-lpc-for-q35"
    return found


def parse_pci_bdf(bdf: str) -> dict[str, str]:
    match = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])", bdf, re.I)
    if match is None:
        raise RuntimeError(f"PCI BDF 无效: {bdf}")
    domain, bus, slot, function = (int(match.group(index), 16) for index in range(1, 5))
    if domain != 0 or bus != 0:
        raise RuntimeError(f"南桥设备必须在 PCI 0000:00，当前 Q35 后端无法承载: {bdf}")
    if slot > 31:
        raise RuntimeError(f"PCI slot 超出 Q35 根总线范围: {bdf}")
    return {
        "domain": f"0x{domain:04x}",
        "bus": f"0x{bus:02x}",
        "slot": f"0x{slot:02x}",
        "function": f"0x{function:x}",
    }


def pci_address_key(address: dict[str, str]) -> tuple[int, int]:
    return int(address["slot"], 0), int(address["function"], 0)


def parse_firmware_release(text: str, field: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", (text or "").strip())
    if match is None:
        raise RuntimeError(f"宿主 {field} 格式无效，不允许使用 255 占位: {text!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if not (0 <= major <= 254 and 0 <= minor <= 254):
        raise RuntimeError(f"宿主 {field} 超出 SMBIOS 范围: {text!r}")
    return major, minor


def pci_device_record(device: Path, name: str, default_desc: str) -> dict[str, Any]:
    vendor = read_int(device / "vendor")
    product = read_int(device / "device")
    revision = read_int(device / "revision", 0xFF)
    subvendor = read_int(device / "subsystem_vendor")
    subdevice = read_int(device / "subsystem_device")
    if vendor in {0, 0xFFFF} or product in {0, 0xFFFF} or revision in {0xFF}:
        raise RuntimeError(f"宿主 {name} PCI 主身份无效: {device.name}")
    description = ""
    lspci = shutil.which("lspci")
    if lspci:
        probe = subprocess.run(
            [lspci, "-s", device.name, "-nn"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if ": " in probe:
            description = probe.split(": ", 1)[1].split(" [", 1)[0].strip()
    return {
        "vendor_id": f"{vendor:04x}",
        "device_id": f"{product:04x}",
        "revision_id": f"{revision:02x}",
        "subsystem_vendor_id": f"{subvendor:04x}",
        "subsystem_device_id": f"{subdevice:04x}",
        "description": ascii_clean(description, default_desc),
        "source": "host-observed",
        "bdf": device.name,
    }


def parse_pcie_link_speed(value: str, field: str) -> str:
    match = re.search(r"(2\.5|5(?:\.0)?|8(?:\.0)?|16(?:\.0)?|32(?:\.0)?|64(?:\.0)?)\s*GT/s", value)
    if match is None:
        raise RuntimeError(f"宿主 PCIe {field} 无效: {value!r}")
    return f"{float(match.group(1)):g}"


def host_root_port_identities() -> list[dict[str, Any]]:
    """Capture every usable root port, preferring PCH ports in stable order."""
    found: list[tuple[int, Path, dict[str, Any]]] = []
    for device in sorted(Path("/sys/bus/pci/devices").glob("0000:00:*")):
        if read_int(device / "class") != 0x060400:
            continue
        vendor = read_int(device / "vendor")
        if vendor not in {0x8086, 0x1022}:
            continue
        slot = int(device.name.split(":")[2].split(".")[0], 16)
        function = int(device.name.rsplit(".", 1)[1], 16)
        if slot == 0x07:
            continue
        if slot in {0x1c, 0x1d}:
            score = 100 - function
        elif slot >= 0x1c:
            score = 50
        else:
            score = 10
        current_width = read_int(device / "current_link_width")
        record = pci_device_record(device, "root_port", "PCI Express Root Port")
        record.update({
            "max_link_speed_gtps": parse_pcie_link_speed(
                read_privileged_text(device / "max_link_speed") or "", "max_link_speed"
            ),
            "max_link_width": read_int(device / "max_link_width"),
            "current_link_speed_gtps": parse_pcie_link_speed(
                read_privileged_text(device / "current_link_speed") or "", "current_link_speed"
            ),
            "current_link_width": current_width,
        })
        if record["max_link_width"] <= 0:
            raise RuntimeError(f"宿主 Root Port {device.name} 链路宽度无效")
        # Connected ports are preferred for Guest ports with devices.  An
        # unconnected physical port is still a valid identity/capability
        # source for a libvirt topology filler with no downstream endpoint.
        if current_width > 0:
            score += 1000
        found.append((-score, device, record))
    if not found:
        raise RuntimeError("宿主缺少可用的 PCI Express Root Port 身份")
    return [record for _, _, record in sorted(found, key=lambda item: (item[0], item[1].name))]


def guest_root_port_profiles(root: ET.Element, host_ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used_buses: set[int] = set()
    devices = root.find("devices")
    if devices is not None:
        for node in devices:
            if node.tag == "controller":
                continue
            address = node.find("address")
            if address is None or address.get("type") != "pci" or not address.get("bus"):
                continue
            try:
                used_buses.add(int(address.get("bus", ""), 0))
            except ValueError as exc:
                raise RuntimeError("Guest 设备 PCI bus 地址无效") from exc
    non_root_port_indexes: list[int] = []
    for controller in root.findall("./devices/controller[@type='pci']"):
        if controller.get("model") in {"pcie-root", "pcie-root-port"}:
            continue
        try:
            non_root_port_indexes.append(int(controller.get("index", "0"), 0))
        except ValueError as exc:
            raise RuntimeError("Guest PCI controller index 无效") from exc
    filler_limit = max(non_root_port_indexes, default=0)
    targets: list[int] = []
    for controller in root.findall("./devices/controller[@type='pci']"):
        if controller.get("model") != "pcie-root-port":
            continue
        try:
            index = int(controller.get("index", "0"), 0)
        except ValueError as exc:
            raise RuntimeError("Guest PCIe Root Port index 无效") from exc
        if index >= 5 and index not in used_buses and index >= filler_limit:
            continue
        target = controller.find("target")
        if target is None or not target.get("port"):
            raise RuntimeError("Guest PCIe Root Port 缺少 target port，无法稳定映射")
        try:
            port = int(target.get("port", ""), 0)
        except ValueError as exc:
            raise RuntimeError("Guest PCIe Root Port target port 无效") from exc
        if port in targets:
            raise RuntimeError(f"Guest PCIe Root Port target port 重复: {port:#x}")
        targets.append(port)
    if len(targets) > len(host_ports):
        raise RuntimeError(
            f"Guest 需要 {len(targets)} 个 Root Port，但宿主只有 {len(host_ports)} 个正在工作的可用端口"
        )
    result: list[dict[str, Any]] = []
    for target, observed in zip(sorted(targets), host_ports):
        item = deepcopy(observed)
        item["guest_target_port"] = target
        result.append(item)
    return result


def host_vga_identity(display_sub: dict[str, str]) -> dict[str, Any]:
    """Temporary QEMU stdvga identity until GPU passthrough.

    Real iGPU/dGPU IDs are never copied onto stdvga: Windows binds the
    vendor's GPU driver and the device fails (Code 43).  8086:1111 is also
    not a real Intel GPU, and Basic Display cannot start it, which leaves
    SPICE/local viewers at "display not initialized".  Keep QEMU 1234:1111
    so Microsoft Basic Display can light the framebuffer.  OEM subsystem
    still comes from the host display device.
    """
    if display_sub.get("vendor_id") in {"0000", "ffff"} or display_sub.get("device_id") in {"0000", "ffff"}:
        raise RuntimeError("宿主显示 subsystem 无效，无法绑定临时 QEMU VGA")
    return {
        "vendor_id": "1234",
        "device_id": "1111",
        "revision_id": "02",
        "subsystem_vendor_id": display_sub["vendor_id"],
        "subsystem_device_id": display_sub["device_id"],
        "description": "VGA compatible controller",
        "source": "qemu-stdvga-temporary",
    }


def bind_southbridge_slots(pci_ids: dict[str, dict[str, Any]], xhci: dict[str, Any]) -> None:
    """Place Q35-backed southbridge functions on host-observed DEVFNs.

    Derived AHCI keeps the conventional Q35 1f.2 slot when the host has no
    SATA controller.  Occupied DEVFNs fail instead of silently shifting.
    """
    for role in ("lpc", "smbus"):
        item = pci_ids[role]
        bdf = item.get("bdf")
        if not bdf:
            raise RuntimeError(f"宿主 {role} 缺少 PCI 地址")
        item["pci_address"] = parse_pci_bdf(bdf)
        item["slot_source"] = "host-observed"
    sata = pci_ids["sata"]
    if sata.get("source") == "host-observed" and sata.get("bdf"):
        sata["pci_address"] = parse_pci_bdf(sata["bdf"])
        sata["slot_source"] = "host-observed"
    else:
        sata["pci_address"] = dict(Q35_DERIVED_SATA_ADDRESS)
        sata["slot_source"] = "q35-default-for-derived-ahci"
        sata.pop("bdf", None)
    occupied: dict[tuple[int, int], str] = {}
    for role in ("lpc", "smbus", "sata", "hda"):
        if role not in pci_ids:
            if role == "hda":
                raise RuntimeError("南桥槽位绑定缺少板载 HDA 地址")
            continue
        key = pci_address_key(pci_ids[role]["pci_address"])
        if key in occupied:
            slot, function = key
            raise RuntimeError(
                f"{role} 与 {occupied[key]} 占用同一 PCI 地址 "
                f"00:{slot:02x}.{function}"
            )
        occupied[key] = role
    usb_address = xhci.get("pci_address")
    if usb_address:
        key = pci_address_key({
            "slot": usb_address["slot"],
            "function": usb_address["function"],
        })
        if key in occupied:
            slot, function = key
            raise RuntimeError(
                f"xHCI 与 {occupied[key]} 占用同一 PCI 地址 "
                f"00:{slot:02x}.{function}"
            )


def host_pci_identities() -> dict[str, dict[str, Any]]:
    """Capture the host's primary PCI identity for the southbridge classes.

    Subsystem IDs alone are insufficient: Windows also exposes the primary
    vendor/device/revision tuple.  Keep the QEMU implementation model, but
    bind its guest-visible tuple to the corresponding host platform device.
    """
    wanted = {
        "host_bridge": (0xFFFFFF, 0x060000),
        "lpc": (0xFFFFFF, 0x060100),
        "smbus": (0xFFFFFF, 0x0C0500),
        "sata": (0xFFFFFF, 0x010601),
    }
    result: dict[str, dict[str, str]] = {}
    for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
        class_code = read_int(device / "class")
        for name, (mask, expected_class) in wanted.items():
            if name in result or (class_code & mask) != expected_class:
                continue
            vendor = read_int(device / "vendor")
            product = read_int(device / "device")
            revision = read_int(device / "revision", 0xFF)
            subvendor = read_int(device / "subsystem_vendor")
            subdevice = read_int(device / "subsystem_device")
            if vendor in {0, 0xFFFF} or product in {0, 0xFFFF} or revision in {0xFF}:
                continue
            bdf = device.name
            description = ""
            lspci = shutil.which("lspci")
            if lspci:
                probe = subprocess.run(
                    [lspci, "-s", bdf, "-nn"],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                if ": " in probe:
                    description = probe.split(": ", 1)[1].split(" [", 1)[0].strip()
            if not description:
                description = {
                    "host_bridge": "Host bridge",
                    "lpc": "LPC bridge",
                    "smbus": "SMBus controller",
                    "sata": "SATA AHCI controller",
                }[name]
            result[name] = {
                "vendor_id": f"{vendor:04x}",
                "device_id": f"{product:04x}",
                "revision_id": f"{revision:02x}",
                "subsystem_vendor_id": f"{subvendor:04x}",
                "subsystem_device_id": f"{subdevice:04x}",
                "description": ascii_clean(description, name),
                "source": "host-observed",
                "bdf": bdf,
            }
    if "sata" not in result and "lpc" in result:
        # NVMe-only hosts still need a coherent AHCI function because the
        # Q35 machine exposes one.  Use a modern platform AHCI ID rather than
        # duplicating the LPC tuple.
        lpc = result["lpc"]
        if lpc["vendor_id"] == "1022":
            sata_device = "7901"
            description = "AMD FCH SATA Controller (AHCI)"
        elif lpc["vendor_id"] == "8086":
            sata_device = "51d3"
            description = "Intel Alder Lake SATA AHCI Controller"
        else:
            raise RuntimeError("宿主缺少 SATA 控制器且平台厂商没有受支持的派生 AHCI 身份")
        result["sata"] = {
            **lpc,
            "device_id": sata_device,
            "description": description,
            "source": "derived-modern-ahci-for-q35",
        }
        result["sata"].pop("bdf", None)
    return result


def acpi_path_namesegs(path: str) -> list[str]:
    text = path.strip()
    if text.startswith("\\"):
        text = text[1:]
    parts = [part for part in text.split(".") if part]
    if len(parts) < 2:
        raise RuntimeError(f"宿主 ACPI 路径过短，无法得到设备 NameSeg: {path}")
    namesegs: list[str] = []
    for part in parts:
        if not 1 <= len(part) <= 4:
            raise RuntimeError(f"宿主 ACPI 路径包含非法 NameSeg: {part} ({path})")
        nameseg = part.ljust(4, "_")
        if ACPI_NAMESEG_RE.fullmatch(nameseg) is None:
            raise RuntimeError(f"宿主 ACPI NameSeg 不是合法 ASL 名称: {nameseg} ({path})")
        namesegs.append(nameseg)
    return namesegs


def host_pci_firmware_path(bdf: str) -> str | None:
    path = Path("/sys/bus/pci/devices") / bdf / "firmware_node" / "path"
    if not path.exists():
        return None
    raw = read_privileged_bytes(path).decode("ascii", "strict").strip()
    return raw or None


def host_acpi_node_from_bdf(bdf: str, role: str) -> dict[str, str]:
    raw = host_pci_firmware_path(bdf)
    if raw is None:
        raise RuntimeError(f"宿主 {role} ({bdf}) 没有 ACPI firmware_node，不允许使用 fallback")
    namesegs = acpi_path_namesegs(raw)
    name = namesegs[-1]
    if name in RESERVED_ACPI_DEVICE_NAMESEGS or re.fullmatch(r"S[0-9A-F]{2}_", name):
        raise RuntimeError(f"宿主 {role} ACPI 名称与 Q35 DSDT 保留名冲突: {name}")
    return {
        "name": name,
        "path": raw,
        "source": "host-observed",
        "bdf": bdf,
    }


def host_acpi_nodes(pci_ids: dict[str, dict[str, str]], xhci: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Capture guest-visible ACPI NameSegs for Q35-backed southbridge devices.

    QEMU still implements the Q35 topology (PCI0, LPC at 00:1f.0).  The
    NameSegs themselves come from the host DSDT via sysfs firmware_node, so
    a Lenovo LPCB, AMD SBRG, or other OEM node is inherited rather than
    hardcoded.
    """
    lpc_bdf = pci_ids["lpc"]["bdf"]
    smbus_bdf = pci_ids["smbus"]["bdf"]
    xhci_bdf = xhci.get("bdf")
    if not xhci_bdf:
        raise RuntimeError("宿主 xHCI 缺少 PCI 地址，无法采集 ACPI 节点")
    lpc = host_acpi_node_from_bdf(lpc_bdf, "lpc")
    namesegs = acpi_path_namesegs(lpc["path"])
    nodes = {
        "pci_root": {
            "name": namesegs[-2],
            "path": "\\" + ".".join(namesegs[:-1]),
            "source": "host-observed-from-lpc",
        },
        "lpc": lpc,
        "smbus": host_acpi_node_from_bdf(smbus_bdf, "smbus"),
        "usb": host_acpi_node_from_bdf(xhci_bdf, "usb"),
    }
    sata = pci_ids["sata"]
    if sata.get("source") == "host-observed":
        sata_bdf = sata.get("bdf")
        if not sata_bdf:
            raise RuntimeError("宿主 SATA 身份缺少 PCI 地址，无法采集 ACPI 节点")
        nodes["sata"] = host_acpi_node_from_bdf(sata_bdf, "sata")
    else:
        nodes["sata"] = {"source": str(sata.get("source") or "absent-on-host")}
    return nodes


def host_xhci_controller() -> dict[str, Any]:
    """Capture the host's integrated xHCI shape for a platform-coherent model."""
    candidates: list[tuple[int, Path, int, int]] = []
    for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
        if read_int(device / "class") != 0x0C0330:
            continue
        usb2_ports = 0
        usb3_ports = 0
        for root_hub in device.glob("usb[0-9]*"):
            try:
                version = root_hub.joinpath("version").read_text().strip()
                ports = int(root_hub.joinpath("maxchild").read_text().strip())
            except (OSError, ValueError):
                continue
            if version.startswith("2"):
                usb2_ports += ports
            elif version.startswith("3"):
                usb3_ports += ports
        # Prefer the PCH controller over a small Thunderbolt/USB4 controller.
        integrated_bonus = 100 if device.name.endswith(":00:14.0") else 0
        candidates.append((integrated_bonus + usb2_ports + usb3_ports,
                           device, usb2_ports, usb3_ports))

    if not candidates:
        raise RuntimeError("宿主没有可用的 xHCI 控制器身份")
    _, device, usb2_ports, usb3_ports = max(candidates, key=lambda item: item[0])
    vendor_id = read_int(device / "vendor")
    device_id = read_int(device / "device")
    revision_id = read_int(device / "revision")
    subsystem_vendor_id = read_int(device / "subsystem_vendor")
    subsystem_device_id = read_int(device / "subsystem_device")
    match = re.fullmatch(r"([0-9a-f]{4}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])",
                         device.name, re.I)
    if match is None:
        raise RuntimeError(f"xHCI PCI 地址无效: {device.name}")

    bar_size = 0
    try:
        first_resource = device.joinpath("resource").read_text().splitlines()[0].split()
        start, end = int(first_resource[0], 16), int(first_resource[1], 16)
        if start and end >= start:
            bar_size = end - start + 1
    except (OSError, IndexError, ValueError):
        pass
    try:
        msi_vectors = len(list(device.joinpath("msi_irqs").iterdir()))
    except OSError:
        msi_vectors = 0

    invalid_ids = {0, 0xFFFF}
    if (
        vendor_id in invalid_ids or device_id in invalid_ids
        or subsystem_vendor_id in invalid_ids or subsystem_device_id in invalid_ids
        or revision_id > 0xFF or usb2_ports < 1 or usb3_ports < 1 or bar_size < 1
    ):
        raise RuntimeError(f"宿主 xHCI 信息不完整，不允许使用 fallback: {device}")

    manufacturers = {
        0x8086: "Intel Corporation",
        0x1022: "Advanced Micro Devices, Inc.",
    }
    manufacturer = manufacturers.get(vendor_id)
    if manufacturer is None:
        raise RuntimeError(f"宿主 xHCI 厂商不在受支持的平台范围内: {vendor_id:04x}")
    product = ""
    if shutil.which("lspci"):
        probe = run(["lspci", "-Dnn", "-s", device.name], check=False)
        if probe.returncode == 0:
            label = re.search(r"USB controller[^:]*:\s*(.+?)\s*\[[0-9a-f]{4}:[0-9a-f]{4}\](?:\s*\(rev [0-9a-f]+\))?$", probe.stdout.strip(), re.I)
            if label:
                product = ascii_clean(label.group(1))
                if product.startswith(manufacturer):
                    product = product[len(manufacturer):].strip()
    if not product:
        raise RuntimeError(f"无法从 PCI 数据库解析宿主 xHCI 名称，不允许使用通用 fallback: {device.name}")
    config = read_privileged_bytes(device / "config", 0x61)
    if len(config) <= 0x60 or config[0x60] not in range(0x20, 0x40):
        raise RuntimeError(f"宿主 xHCI Serial Bus Release Number 无效: {device.name}")
    return {
        "manufacturer": manufacturer,
        "product": product,
        "pci_vendor_id": f"{vendor_id:04x}",
        "pci_device_id": f"{device_id:04x}",
        "revision_id": f"{revision_id:02x}",
        "subsystem_vendor_id": f"{subsystem_vendor_id:04x}",
        "subsystem_device_id": f"{subsystem_device_id:04x}",
        "pci_address": {
            "domain": f"0x{int(match.group(1), 16):04x}",
            "bus": f"0x{int(match.group(2), 16):02x}",
            "slot": f"0x{int(match.group(3), 16):02x}",
            "function": f"0x{int(match.group(4), 16):x}",
        },
        "usb2_ports": usb2_ports,
        "usb3_ports": usb3_ports,
        "slots": 64,
        "interrupts_observed": msi_vectors,
        "bar_size_bytes": bar_size,
        "serial_bus_release": f"{config[0x60]:02x}",
        "pcie_endpoint": (device / "current_link_speed").exists(),
        "bdf": device.name,
    }


ONBOARD_HDA_VENDORS = {0x8086, 0x1022}
HDA_PIN_PORT_NONE = 1
HDA_PIN_PLAYBACK_DEVICES = {
    0x0: "line-out",
    0x1: "speaker",
    0x2: "hp-out",
    0x4: "spdif-out",
    0x5: "digital-out",
}
HDA_PIN_CAPTURE_DEVICES = {
    0x8: "line-in",
    0x9: "aux",
    0xA: "mic",
    0xC: "spdif-in",
    0xD: "digital-in",
}


def is_gpu_function_audio(device: Path) -> bool:
    """True when this HDA function sits on the same PCI device as a display."""
    base = device.name.rsplit(".", 1)[0]
    for sibling in device.parent.glob(base + ".*"):
        if (read_int(sibling / "class") >> 16) == 0x03:
            return True
    return False


def pci_hda_card_indexes(device: Path) -> list[int]:
    cards: list[int] = []
    sound = device / "sound"
    if sound.is_dir():
        for child in sorted(sound.iterdir()):
            match = re.fullmatch(r"card(\d+)", child.name)
            if match:
                cards.append(int(match.group(1)))
    if cards:
        return cards
    resolved = device.resolve()
    class_root = Path("/sys/class/sound")
    if not class_root.is_dir():
        return []
    for card in sorted(class_root.glob("card*")):
        match = re.fullmatch(r"card(\d+)", card.name)
        if match is None:
            continue
        target = card / "device"
        try:
            linked = target.resolve()
        except OSError:
            continue
        if linked == resolved or resolved in linked.parents or linked in resolved.parents:
            cards.append(int(match.group(1)))
    return cards


def parse_hda_pin_widget(nid: int, block: str) -> dict[str, Any] | None:
    if "[Pin Complex]" not in block.split("\n", 1)[0]:
        return None
    cap = re.search(r"^\s+Pincap 0x([0-9a-fA-F]+)", block, re.MULTILINE)
    default = re.search(r"^\s+Pin Default 0x([0-9a-fA-F]+):", block, re.MULTILINE)
    if default is None:
        return None
    config = int(default.group(1), 16)
    connectivity = (config >> 30) & 0x3
    if connectivity == HDA_PIN_PORT_NONE:
        return None
    device_id = (config >> 20) & 0xF
    association = (config >> 4) & 0xF
    if association == 0xF:
        return None
    if device_id in HDA_PIN_PLAYBACK_DEVICES:
        direction = "playback"
        device_name = HDA_PIN_PLAYBACK_DEVICES[device_id]
    elif device_id in HDA_PIN_CAPTURE_DEVICES:
        direction = "capture"
        device_name = HDA_PIN_CAPTURE_DEVICES[device_id]
    else:
        return None
    return {
        "nid": f"{nid:02x}",
        "direction": direction,
        "device": device_name,
        "config": f"{config:08x}",
        "pincap": f"{int(cap.group(1), 16):08x}" if cap else "00000000",
        "association": association,
        "sequence": config & 0xF,
        "connectivity": {0: "jack", 2: "fixed", 3: "both"}[connectivity],
        "source": "host-observed-onboard-codec",
    }


def parse_onboard_hda_codec_text(text: str) -> dict[str, Any] | None:
    codec = re.search(r"^Codec:\s*(.+)$", text, re.MULTILINE)
    vendor = re.search(r"^Vendor Id:\s*0x([0-9a-fA-F]{1,8})$", text, re.MULTILINE)
    subsystem = re.search(r"^Subsystem Id:\s*0x([0-9a-fA-F]{1,8})$", text, re.MULTILINE)
    revision = re.search(r"^Revision Id:\s*0x([0-9a-fA-F]{1,8})$", text, re.MULTILINE)
    address = re.search(r"^Address:\s*(\d+)$", text, re.MULTILINE)
    if not (codec and vendor and subsystem):
        return None
    name = ascii_clean(codec.group(1), "High Definition Audio")
    if any(token in name.upper() for token in ("HDMI", "DISPLAYPORT", "GPU", "NVIDIA", "RADEON")):
        return None
    pins: list[dict[str, Any]] = []
    chunks = re.split(r"(?=^Node 0x)", text, flags=re.MULTILINE)
    for chunk in chunks:
        node = re.match(r"Node 0x([0-9a-fA-F]+)\s+\[", chunk)
        if node is None:
            continue
        pin = parse_hda_pin_widget(int(node.group(1), 16), chunk)
        if pin is not None:
            pins.append(pin)
    pins.sort(key=lambda item: (item["direction"] != "playback", item["association"], item["sequence"], int(item["nid"], 16)))
    playback = [item for item in pins if item["direction"] == "playback"][:8]
    capture = [item for item in pins if item["direction"] == "capture"][:8]
    selected = playback + capture
    if not playback:
        return None
    return {
        "name": name,
        "vendor_device_id": vendor.group(1).lower().zfill(8),
        "subsystem_id": subsystem.group(1).lower().zfill(8),
        "revision_id": revision.group(1).lower().zfill(8) if revision else "00100101",
        "cad": int(address.group(1)) if address else 0,
        "pins": selected,
        "source": "host-observed-onboard-analog",
    }


def onboard_hda_identity() -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind QEMU HDA to the chipset analog codec only.

    USB sound cards and discrete PCIe audio are not identity sources; those
    devices stay available for passthrough.  GPU HDMI/DP function audio is
    also ignored here.
    """
    analog: list[tuple[Path, dict[str, Any]]] = []
    for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
        class_code = read_int(device / "class")
        if (class_code >> 8) & 0xFFFF != 0x0403:
            continue
        if read_int(device / "vendor") not in ONBOARD_HDA_VENDORS:
            continue
        if is_gpu_function_audio(device):
            continue
        try:
            parse_pci_bdf(device.name)
        except RuntimeError:
            continue
        matched: dict[str, Any] | None = None
        for card in pci_hda_card_indexes(device):
            for path in sorted(Path("/proc/asound").glob(f"card{card}/codec#*")):
                try:
                    text = path.read_text(errors="ignore")
                except OSError:
                    continue
                parsed = parse_onboard_hda_codec_text(text)
                if parsed is None:
                    continue
                if matched is not None:
                    raise RuntimeError(f"板载 HDA {device.name} 上存在多个 analog codec，无法唯一绑定")
                matched = parsed
        if matched is not None:
            analog.append((device, matched))
    if not analog:
        raise RuntimeError("宿主没有板载 Intel/AMD analog HDA。USB 声卡和独立 PCIe 声卡不采集")
    if len(analog) > 1:
        names = ", ".join(device.name for device, _ in analog)
        raise RuntimeError(f"宿主存在多块板载 analog HDA，无法唯一绑定: {names}")
    device, codec = analog[0]
    values = {
        "vendor_id": read_int(device / "vendor"),
        "device_id": read_int(device / "device"),
        "revision_id": read_int(device / "revision"),
        "subsystem_vendor_id": read_int(device / "subsystem_vendor"),
        "subsystem_device_id": read_int(device / "subsystem_device"),
    }
    if any(value in {0, 0xFFFF} for key, value in values.items() if key != "revision_id") or values["revision_id"] > 0xFF:
        raise RuntimeError(f"宿主板载 HDA controller 信息不完整: {device.name}")
    product = ""
    if shutil.which("lspci"):
        probe = run(["lspci", "-Dnn", "-s", device.name], check=False)
        if probe.returncode == 0:
            label = re.search(r"Audio device[^:]*:\s*(.+?)\s*\[[0-9a-f]{4}:[0-9a-f]{4}\](?:\s*\(rev [0-9a-f]+\))?$", probe.stdout.strip(), re.I)
            if label:
                product = ascii_clean(label.group(1))
                for manufacturer in ("Intel Corporation", "Advanced Micro Devices, Inc."):
                    if product.startswith(manufacturer):
                        product = product[len(manufacturer):].strip()
                        break
    if not product:
        raise RuntimeError(f"无法从 PCI 数据库解析板载 HDA 名称: {device.name}")
    controller = {
        "product": product,
        "vendor_id": f"{values['vendor_id']:04x}",
        "device_id": f"{values['device_id']:04x}",
        "revision_id": f"{values['revision_id']:02x}",
        "subsystem_vendor_id": f"{values['subsystem_vendor_id']:04x}",
        "subsystem_device_id": f"{values['subsystem_device_id']:04x}",
        "bdf": device.name,
        "pci_address": parse_pci_bdf(device.name),
        "source": "host-observed-onboard",
    }
    return controller, codec


def read_int(path: Path, default: int = 0) -> int:
    try:
        value = path.read_text().strip()
        return int(value, 16 if value.lower().startswith("0x") else 10)
    except (OSError, ValueError):
        return default


def host_system_battery() -> dict[str, Any] | None:
    """Return the built-in system battery, excluding mouse/headset batteries."""
    root = Path("/sys/class/power_supply")
    for item in sorted(root.glob("*")):
        try:
            if item.joinpath("type").read_text().strip() != "Battery":
                continue
            scope = item.joinpath("scope").read_text().strip() if item.joinpath("scope").exists() else "System"
            if scope.lower() == "device" or item.name.lower().startswith(("hid", "mouse", "headset")):
                continue
            if read_int(item / "present", 1) == 0:
                continue
            def text(name: str) -> str:
                try:
                    return item.joinpath(name).read_text(errors="ignore").strip()
                except OSError:
                    return ""
            design_voltage_uv = read_int(item / "voltage_min_design")
            voltage_uv = read_int(item / "voltage_now") or design_voltage_uv
            energy_uwh = read_int(item / "energy_full_design")
            if not energy_uwh:
                charge_uah = read_int(item / "charge_full_design")
                energy_uwh = charge_uah * design_voltage_uv // 1_000_000
            full_uwh = read_int(item / "energy_full")
            if not full_uwh:
                charge_full_uah = read_int(item / "charge_full")
                full_uwh = charge_full_uah * voltage_uv // 1_000_000
            energy_now_uwh = read_int(item / "energy_now")
            if not energy_now_uwh:
                charge_now_uah = read_int(item / "charge_now")
                energy_now_uwh = charge_now_uah * voltage_uv // 1_000_000
            power_uw = read_int(item / "power_now")
            if not power_uw:
                current_ua = read_int(item / "current_now")
                power_uw = current_ua * voltage_uv // 1_000_000
            ac_online = any(
                read_int(supply / "online") == 1
                for supply in root.glob("*")
                if supply != item
                and supply.joinpath("type").exists()
                and supply.joinpath("type").read_text(errors="ignore").strip() in {"Mains", "USB", "USB_C"}
            )
            return {
                "name": item.name,
                "manufacturer": ascii_clean(text("manufacturer")),
                "model": ascii_clean(text("model_name")),
                "design_capacity_mwh": energy_uwh // 1000 if energy_uwh else 0,
                "last_full_capacity_mwh": full_uwh // 1000 if full_uwh else 0,
                "design_voltage_mv": design_voltage_uv // 1000,
                "remaining_capacity_mwh": energy_now_uwh // 1000 if energy_now_uwh else 0,
                "present_rate_mw": power_uw // 1000 if power_uw else 0,
                "present_voltage_mv": voltage_uv // 1000 if voltage_uv else 0,
                "capacity_percent": read_int(item / "capacity"),
                "status": ascii_clean(text("status"), "Unknown"),
                "ac_online": ac_online,
            }
        except OSError:
            continue
    return None


def random_alnum(length: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_hex(length: int) -> str:
    return "".join(secrets.choice("0123456789ABCDEF") for _ in range(length))


def storage_serial(identity: dict[str, str]) -> str:
    prefix = identity["serial_prefix"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    length = max(12, int(identity.get("serial_length", "15")))
    if len(prefix) >= length:
        raise ValueError(f"存储型号 {identity['model']} 的序列号规则无效")
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length - len(prefix)))


def storage_wwn(identity: dict[str, str]) -> str:
    prefix = identity["wwn_prefix"].lower()
    if not re.fullmatch(r"[0-9a-f]{6,15}", prefix):
        raise ValueError(f"存储型号 {identity['model']} 的 WWN 前缀无效")
    return (prefix + random_hex(16 - len(prefix))).upper()


def platform_serials(platform: dict[str, Any]) -> dict[str, str]:
    """Generate identifiers in formats commonly used by the selected OEM."""
    vendor = platform["vendor"].upper()
    if "LENOVO" in vendor:
        system = "PF" + random_alnum(6)
        board = "L1" + random_alnum(14)
    elif "DELL" in vendor:
        # Dell Service Tags use seven base-36 characters; board serials use a
        # manufacturing form rather than a generic UUID.
        system = random_alnum(7)
        board = "CN-0" + random_alnum(5) + "-" + random_alnum(5) + "-" + random_alnum(3) + "-" + random_alnum(4)
    elif "ASUS" in vendor:
        system = random_alnum(15)
        board = random_alnum(12)
    elif "MSI" in vendor or "MICRO-STAR" in vendor:
        system = random_alnum(14)
        board = random_alnum(14)
    else:
        system = random_alnum(12)
        board = random_alnum(16)
    return {"system": system, "board": board, "chassis": system}


def processor_family(model_name: str, amd: bool) -> tuple[int, int]:
    if amd:
        return 0x6B, 0x006B  # AMD Zen
    upper = model_name.upper()
    if "CORE(TM) ULTRA 9" in upper or "CORE ULTRA 9" in upper:
        return 0xFE, 0x0307
    if "CORE(TM) ULTRA 7" in upper or "CORE ULTRA 7" in upper:
        return 0xFE, 0x0306
    if "CORE(TM) ULTRA 5" in upper or "CORE ULTRA 5" in upper:
        return 0xFE, 0x0305
    if "CORE(TM) ULTRA 3" in upper or "CORE ULTRA 3" in upper:
        return 0xFE, 0x0304
    return (
        0xCF if re.search(r"\bI9[- ]", upper) else
        0xC6 if re.search(r"\bI7[- ]", upper) else
        0xCD if re.search(r"\bI5[- ]", upper) else
        0xCE if re.search(r"\bI3[- ]", upper) else
        0x16,
        0,
    )


def random_mac(model: str = NETWORK_MODEL) -> str:
    # Use a vendor-consistent unicast OUI instead of the conspicuous 52:54:00
    # or a locally-administered prefix. The NIC suffix remains random.
    prefixes = {
        "i226-v": ((0x3C, 0xFD, 0xFE), (0xA8, 0xB8, 0xE0)),
        "e1000e": ((0x3C, 0xFD, 0xFE), (0x00, 0x1B, 0x21)),
        "rtl8139": ((0x00, 0xE0, 0x4C),),
    }
    prefix = secrets.choice(prefixes.get(model, prefixes[NETWORK_MODEL]))
    values = [*prefix, secrets.randbelow(256), secrets.randbelow(256), secrets.randbelow(256)]
    return ":".join(f"{value:02x}" for value in values)


def random_oui_mac(oui: tuple[int, int, int], excluded: set[str]) -> str:
    while True:
        values = [*oui, *(secrets.randbelow(256) for _ in range(3))]
        mac = ":".join(f"{value:02x}" for value in values)
        if mac not in excluded:
            return mac


def routed_ipv4_networks(prefix: list[str]) -> list[ipaddress.IPv4Network]:
    """Collect host and defined libvirt networks to avoid address conflicts."""
    result = run(["ip", "-j", "-4", "route", "show"], check=False)
    if result.returncode != 0:
        return []
    try:
        routes = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    networks: list[ipaddress.IPv4Network] = []
    for route in routes:
        destination = route.get("dst", "")
        if not destination or destination == "default":
            continue
        try:
            networks.append(ipaddress.ip_network(destination, strict=False))
        except ValueError:
            continue
    listed = run(prefix + ["net-list", "--all", "--name"], check=False)
    if listed.returncode != 0:
        return networks
    for name in (line.strip() for line in listed.stdout.splitlines()):
        if not name:
            continue
        dumped = run(prefix + ["net-dumpxml", name], check=False)
        if dumped.returncode != 0:
            continue
        try:
            root = ET.fromstring(dumped.stdout)
        except ET.ParseError:
            continue
        for node in root.findall("ip"):
            address = node.get("address", "")
            netmask = node.get("netmask", "")
            prefix_length = node.get("prefix", "")
            if not address or (not netmask and not prefix_length):
                continue
            try:
                suffix = prefix_length or netmask
                network = ipaddress.ip_network(f"{address}/{suffix}", strict=False)
            except ValueError:
                continue
            if network.version == 4:
                networks.append(network)
    return networks


def generate_lan_profiles(
    nic_macs: list[str], profile_id: str, prefix: list[str]
) -> list[dict[str, Any]]:
    occupied = routed_ipv4_networks(prefix)
    reserved: list[ipaddress.IPv4Network] = []
    profiles: list[dict[str, Any]] = []
    used_macs = {mac.lower() for mac in nic_macs}

    for index, nic_mac in enumerate(nic_macs):
        candidates = list(range(256))
        while candidates:
            third_octet = secrets.choice(candidates)
            candidates.remove(third_octet)
            subnet = ipaddress.ip_network(f"192.168.{third_octet}.0/24")
            if not any(subnet.overlaps(other) for other in (*occupied, *reserved)):
                break
        else:
            raise RuntimeError("无法生成与宿主路由不冲突的私有 IPv4 网段")
        reserved.append(subnet)

        gateway_host = secrets.choice((1, 254))
        guest_candidates = [value for value in range(20, 240) if value != gateway_host]
        guest_host = secrets.choice(guest_candidates)
        gateway_ip = str(subnet.network_address + gateway_host)
        guest_ip = str(subnet.network_address + guest_host)
        router_vendor, router_oui = secrets.choice(ROUTER_OUIS)
        gateway_mac = random_oui_mac(router_oui, used_macs)
        used_macs.add(gateway_mac)
        token = uuid.UUID(profile_id).hex

        profiles.append({
            "mac": nic_mac,
            "permanent_mac": nic_mac,
            "pcie_device_serial_number": pcie_dsn_from_mac(nic_mac),
            "ipv4": {
                "address": guest_ip,
                "assignment": "dhcp-reservation",
                "dhcp_start": str(subnet.network_address + 20),
                "dhcp_end": str(subnet.network_address + 239),
                "prefix_length": subnet.prefixlen,
                "netmask": str(subnet.netmask),
                "subnet": str(subnet),
            },
            "gateway": {
                "ipv4_address": gateway_ip,
                "mac": gateway_mac,
                "manufacturer": router_vendor,
            },
            "libvirt_network": {
                "name": f"lan-{token[:10]}-{index + 1}",
                "uuid": str(uuid.uuid4()),
                "bridge_name": f"br{token[:10]}{index:x}",
                "forward_mode": "nat",
            },
        })
    return profiles


def pcie_dsn_from_mac(mac: str) -> str:
    """Use Intel's MAC-derived PCIe serial layout used by this controller."""
    octets = [int(value, 16) for value in mac.split(":")]
    if len(octets) != 6:
        raise ValueError(f"MAC 地址格式无效: {mac}")
    return "-".join(f"{value:02x}" for value in (*octets[:3], 0xFF, 0xFF, *octets[3:]))


def validate_host_facts(host: dict[str, Any], dmi: dict[str, str]) -> None:
    missing: list[str] = []
    if host.get("vendor") not in {"GenuineIntel", "AuthenticAMD"}:
        missing.append("受支持的 CPU vendor_id")
    if not host.get("model_name"):
        missing.append("CPU model name")
    if int(host.get("max_mhz", 0)) <= 0:
        missing.append("CPU maximum frequency")
    if int(host.get("base_mhz", 0)) <= 0 and int(host.get("current_mhz", 0)) <= 0:
        missing.append("CPU base/current frequency")
    for field in (
        "sys_vendor", "product_name", "product_version", "product_family", "product_sku",
        "board_vendor", "board_name", "board_version", "bios_vendor", "bios_version",
        "bios_date", "chassis_vendor", "chassis_version", "chassis_type",
    ):
        value = dmi.get(field, "").strip()
        if not value or value.lower() in {"default string", "to be filled by o.e.m.", "not specified", "unknown"}:
            missing.append(f"DMI {field}")
    if missing:
        raise RuntimeError("宿主硬件信息不完整，不允许使用 fallback:\n- " + "\n- ".join(missing))


def host_platform_profile(
    host: dict[str, Any], dmi: dict[str, str], smbios: dict[str, Any],
    acpi: dict[str, Any], battery_present: bool,
) -> dict[str, Any]:
    """Build a platform identity solely from non-unique host facts."""
    validate_host_facts(host, dmi)
    try:
        chassis_type = int(dmi["chassis_type"], 0)
        datetime.strptime(dmi["bios_date"], "%m/%d/%Y")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("宿主 chassis type 或 BIOS 日期无效") from exc
    if not 1 <= chassis_type <= 0x24:
        raise RuntimeError(f"宿主 chassis type 超出 SMBIOS 范围: {chassis_type}")
    mobile_chassis_types = {8, 9, 10, 14, 30, 31, 32}
    platform_class = "laptop" if chassis_type in mobile_chassis_types else "desktop"
    if battery_present and platform_class != "laptop":
        raise RuntimeError("宿主存在系统电池，但 DMI chassis type 不是移动平台")

    memory = smbios["memory"]
    processor = smbios["processor"]
    platform = {
        "identity_source": "host-non-unique",
        "vendor": dmi["sys_vendor"],
        "product": dmi["product_name"],
        "version": dmi["product_version"],
        "family": dmi["product_family"],
        "sku": dmi["product_sku"],
        "board_vendor": dmi["board_vendor"],
        "board": dmi["board_name"],
        "board_version": dmi["board_version"],
        "chassis_vendor": dmi["chassis_vendor"],
        "chassis_version": dmi["chassis_version"],
        "chassis_type": chassis_type,
        "socket": processor["socket"],
        "cpu_upgrade": processor["upgrade"],
        "memory_type": memory["type"],
        "memory_type_code": memory["type_code"],
        "memory_form_factor": memory["form_factor"],
        "memory_speed": memory["speed_mt"],
        "bios_vendor": dmi["bios_vendor"],
        "bios_version": dmi["bios_version"],
        "bios_date": dmi["bios_date"],
        "bios_major_release": parse_firmware_release(dmi.get("bios_release", ""), "bios_release")[0],
        "bios_minor_release": parse_firmware_release(dmi.get("bios_release", ""), "bios_release")[1],
        "ec_major_release": parse_firmware_release(dmi.get("ec_firmware_release", ""), "ec_firmware_release")[0],
        "ec_minor_release": parse_firmware_release(dmi.get("ec_firmware_release", ""), "ec_firmware_release")[1],
        **acpi,
        "class": platform_class,
        "has_battery": battery_present,
    }
    stable = json.dumps(platform, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    platform["platform_id"] = "host-" + hashlib.sha256(stable).hexdigest()[:20]
    platform["platform_source_version"] = PLATFORM_SOURCE_VERSION
    return platform


def dimm_layout(total_bytes: int, memory_type: str, platform_class: str) -> list[int]:
    if total_bytes % (1024 * 1024):
        raise RuntimeError("Guest XML 内存容量必须是整 MiB")
    total_mib = total_bytes // (1024 * 1024)
    if total_mib < 4096 or total_mib % 4096:
        raise RuntimeError("Guest 内存必须是 4 GiB 的整数倍，且单条不小于 4 GiB")
    catalog = MEMORY_CATALOG.get((memory_type, platform_class))
    if not catalog:
        raise RuntimeError(f"没有 {platform_class} {memory_type} 内存配件池")
    common = tuple(sorted((size for size in catalog if size >= 4096), reverse=True))
    if 4096 not in common:
        raise RuntimeError(f"{platform_class} {memory_type} 配件池缺少最小 4 GiB 规格")
    remaining = total_mib
    layout: list[int] = []
    for size in common:
        while remaining >= size:
            layout.append(size)
            remaining -= size
    if remaining or not layout:
        raise RuntimeError(f"无法用常见内存条规格表示 Guest 内存: {total_mib} MiB")
    return layout


def memory_identity(memory_type: str, platform_class: str, sizes_mib: list[int]) -> tuple[str, list[str]]:
    pool = MEMORY_CATALOG.get((memory_type, platform_class))
    if pool is None:
        raise RuntimeError(f"内置硬件目录不支持 {platform_class} {memory_type} 内存")
    missing = sorted({size for size in sizes_mib if size not in pool})
    if missing:
        raise RuntimeError(f"内置硬件目录没有这些 DIMM 容量: {missing} MiB")
    common_vendors = set(vendor for vendor, _ in pool[sizes_mib[0]])
    for size in sizes_mib[1:]:
        common_vendors &= {vendor for vendor, _ in pool[size]}
    if not common_vendors:
        raise RuntimeError("没有同一厂商可覆盖当前 DIMM 容量组合")
    vendor = secrets.choice(sorted(common_vendors))
    parts = [secrets.choice([part for item_vendor, part in pool[size] if item_vendor == vendor]) for size in sizes_mib]
    return vendor, parts


def storage_identity(device: str, bus: str) -> dict[str, str]:
    key = "cdrom" if device == "cdrom" else ({"ide": "sata", "sata": "sata", "scsi": "scsi", "sas": "scsi", "nvme": "nvme"}.get(bus.lower()))
    if key is None or key not in STORAGE_CATALOG:
        raise RuntimeError(f"没有可安全隐藏的 {bus!r} 存储设备目录；请使用 SATA、SCSI/SAS 或 NVMe")
    return deepcopy(secrets.choice(STORAGE_CATALOG[key]))


def firmware_info(platform: dict[str, Any]) -> dict[str, Any]:
    date = platform["bios_date"]
    try:
        datetime.strptime(date, "%m/%d/%Y")
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"宿主 BIOS 日期无效: {date!r}") from exc
    return {
        "vendor": platform["bios_vendor"],
        "version": platform["bios_version"],
        "date": date,
        "bios_major_release": int(platform["bios_major_release"]),
        "bios_minor_release": int(platform["bios_minor_release"]),
        "ec_major_release": int(platform["ec_major_release"]),
        "ec_minor_release": int(platform["ec_minor_release"]),
        "acpi_oem_id": platform["acpi_oem_id"],
        "acpi_table_id": platform["acpi_table_id"],
        "acpi_creator_id": platform["acpi_creator_id"],
        "acpi_creator_revision": platform["acpi_creator_revision"],
    }


def battery_profile(host_battery: dict[str, Any] | None, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    if host_battery is None:
        raise RuntimeError("电池 profile 已启用但宿主没有系统电池")
    required = ("manufacturer", "model", "design_capacity_mwh", "design_voltage_mv")
    missing = [name for name in required if not host_battery.get(name)]
    if missing:
        raise RuntimeError("宿主电池非唯一规格不完整，不允许使用 fallback: " + ", ".join(missing))
    design = int(host_battery["design_capacity_mwh"])
    voltage = int(host_battery["design_voltage_mv"])
    host_full = int(host_battery.get("last_full_capacity_mwh", 0))
    full = max(design // 2, min(design, host_full)) if 0 < host_full <= design else design * (94 + secrets.randbelow(5)) // 100
    percent = int((host_battery or {}).get("capacity_percent", 0))
    if not 5 <= percent <= 100:
        percent = secrets.randbelow(36) + 60
    remaining = full * percent // 100
    host_design_voltage = int((host_battery or {}).get("design_voltage_mv", 0))
    host_present_voltage = int((host_battery or {}).get("present_voltage_mv", 0))
    if host_design_voltage > 0 and host_present_voltage > 0:
        present_voltage = max(voltage * 80 // 100, min(voltage * 110 // 100, voltage * host_present_voltage // host_design_voltage))
    else:
        present_voltage = voltage * (95 + secrets.randbelow(7)) // 100
    status = str((host_battery or {}).get("status", "Unknown")).lower()
    ac_online = bool((host_battery or {}).get("ac_online", status != "discharging"))
    state = 0x02 if "charg" in status and "discharg" not in status else (0x01 if "discharg" in status else 0)
    present_rate = int((host_battery or {}).get("present_rate_mw", 0))
    if state and not 100 <= present_rate <= 100000:
        present_rate = secrets.randbelow(22001) + 8000
    if not state:
        present_rate = 0
    manufacture_year = secrets.choice((2021, 2022, 2023))
    manufacture_month = secrets.randbelow(12) + 1
    manufacture_day = secrets.randbelow(28) + 1
    current_temperature = 2732 + (secrets.randbelow(15) + 38) * 10
    return {
        "enabled": True,
        "manufacturer": ascii_clean(host_battery["manufacturer"]),
        "model": ascii_clean(host_battery["model"]),
        "serial": "".join(secrets.choice("0123456789") for _ in range(8)),
        "sbds_serial": secrets.randbelow(0xFFFE) + 1,
        "manufacture_date": f"{manufacture_month:02d}/{manufacture_day:02d}/{manufacture_year}",
        "sbds_manufacture_date": ((manufacture_year - 1980) << 9) | (manufacture_month << 5) | manufacture_day,
        "chemistry": "LION",
        "design_capacity_mwh": design,
        "last_full_capacity_mwh": full,
        "remaining_capacity_mwh": remaining,
        "design_voltage_mv": voltage,
        "present_voltage_mv": present_voltage,
        "present_rate_mw": present_rate,
        "state": state,
        "ac_online": ac_online,
        "warning_capacity_mwh": design // 10,
        "low_capacity_mwh": design // 20,
        "granularity_mwh": max(1, design // 1000),
        "temperature_current_dk": current_temperature,
        "temperature_active_dk": 3432,
        "temperature_passive_dk": 3632,
        "temperature_critical_dk": 3782,
    }


def smbios_uuid(value: str) -> bytes:
    raw = uuid.UUID(value).bytes
    return raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:]


def smbios_structure(kind: int, handle: int, body: bytes, strings: Iterable[str] = ()) -> bytes:
    values = [str(item) for item in strings]
    if any("\0" in item for item in values):
        raise ValueError("SMBIOS strings cannot contain NUL")
    area = b"\0\0" if not values else b"".join(item.encode("ascii", "strict") + b"\0" for item in values) + b"\0"
    return struct.pack("<BBH", kind, len(body) + 4, handle) + body + area


def smbios_cache_size(size_kib: int) -> tuple[int, int]:
    """Encode SMBIOS Type 7 legacy and extended cache-size fields."""
    size = max(1, int(size_kib))
    if size <= 0x7FFF:
        legacy = size
    elif (size + 63) // 64 <= 0x7FFF:
        legacy = 0x8000 | ((size + 63) // 64)
    else:
        legacy = 0xFFFF

    if size <= 0x7FFFFFFF:
        extended = size
    elif (size + 63) // 64 <= 0x7FFFFFFF:
        extended = 0x80000000 | ((size + 63) // 64)
    else:
        extended = 0xFFFFFFFF
    return legacy, extended


def validate_smbios_memory_topology(data: bytes, sizes_mib: list[int]) -> None:
    tables = list(iter_smbios_structures(data))
    type16 = [formatted for kind, formatted, _ in tables if kind == 16]
    type17 = [formatted for kind, formatted, _ in tables if kind == 17]
    type19 = [formatted for kind, formatted, _ in tables if kind == 19]
    type20 = [formatted for kind, formatted, _ in tables if kind == 20]
    if len(type16) != 1 or len(type19) != 1:
        raise RuntimeError("SMBIOS 内存拓扑必须各有一条 Type 16/19")
    if len(type17) != len(sizes_mib) or len(type20) != len(sizes_mib):
        raise RuntimeError("SMBIOS Type 17/20 数量与 Guest DIMM 拆分不一致")
    array_handle = struct.unpack_from("<H", type16[0], 2)[0]
    if struct.unpack_from("<H", type16[0], 13)[0] != len(sizes_mib):
        raise RuntimeError("SMBIOS Type 16 memory device 数量错误")
    dimm_handles = {struct.unpack_from("<H", item, 2)[0] for item in type17}
    if any(struct.unpack_from("<H", item, 4)[0] != array_handle for item in type17):
        raise RuntimeError("SMBIOS Type 17 没有引用 Guest Type 16")
    mapped_handle = struct.unpack_from("<H", type19[0], 2)[0]
    if struct.unpack_from("<H", type19[0], 12)[0] != array_handle:
        raise RuntimeError("SMBIOS Type 19 没有引用 Guest Type 16")
    for item in type20:
        if struct.unpack_from("<H", item, 12)[0] not in dimm_handles:
            raise RuntimeError("SMBIOS Type 20 引用了不存在的 Type 17")
        if struct.unpack_from("<H", item, 14)[0] != mapped_handle:
            raise RuntimeError("SMBIOS Type 20 没有引用 Guest Type 19")


def build_smbios_stream(profile: dict, platform: dict, identity: dict) -> bytes:
    """Build a complete SMBIOS structure stream owned by spoof_v2."""
    out: list[bytes] = []
    vendor = profile["vendor"]
    body = struct.pack(
        "<BBHBBQ2B4BH",
        1, 2, 0xE000, 3, 0xFF, 0x0000000000099A80, 0x03, 0x0D,
        int(platform["bios_major_release"]), int(platform["bios_minor_release"]),
        int(platform["ec_major_release"]), int(platform["ec_minor_release"]),
        16,
    )
    out.append(smbios_structure(0, 0x0000, body, [profile["bios_vendor"], profile["bios_version"], profile["bios_date"]]))

    body = struct.pack("<4B", 1, 2, 3, 4) + smbios_uuid(profile["system_uuid"]) + struct.pack("<BBB", 0x06, 5, 6)
    out.append(smbios_structure(1, 0x0100, body, [vendor, profile["product_name"], profile["product_version"], profile["system_serial"], profile["product_sku"], profile["product_family"]]))

    body = struct.pack("<7B H 2B", 1, 2, 3, 4, 5, 0x09, 6, 0x0300, 0x0A, 0)
    out.append(smbios_structure(2, 0x0200, body, [platform["board_vendor"], profile["board_name"], profile["board_version"], profile["board_serial"], identity["board_asset"], "Main Board"]))

    chassis_type = int(platform["chassis_type"])
    body = struct.pack("<9B I 4B B", 1, chassis_type, 2, 3, 4, 3, 3, 3, 2, 0, 0, 1, 0, 0, 5)
    out.append(smbios_structure(3, 0x0300, body, [platform["chassis_vendor"], profile["chassis_version"], profile["chassis_serial"], identity["chassis_asset"], identity["chassis_sku"]]))

    for socket_index in range(profile["sockets"]):
        body = bytearray(44)
        family_code = profile["cpu_family"]
        family2_code = profile["cpu_family2"]
        body[0:4] = bytes((1, 3, family_code, 2))
        struct.pack_into("<II", body, 4, profile["cpu_signature"], profile["cpu_features_edx"])
        body[12:14] = bytes((3, profile["cpu_voltage"]))
        struct.pack_into("<HHH", body, 14, profile["cpu_external_clock_mhz"], profile["cpu_max_mhz"], min(profile["cpu_current_mhz"], profile["cpu_max_mhz"]))
        body[20:22] = bytes((0x41, profile["cpu_upgrade"]))
        struct.pack_into("<HHH", body, 22, 0x0701, 0x0702, 0x0703)
        body[28:31] = bytes((4, 5, 6))
        body[31:34] = bytes((min(profile["cores"], 255), min(profile["enabled_cores"], 255), min(profile["threads"], 255)))
        struct.pack_into("<HHHH", body, 34, profile["cpu_characteristics"], family2_code, profile["cores"], profile["enabled_cores"])
        struct.pack_into("<H", body, 42, profile["threads"])
        out.append(smbios_structure(4, 0x0400 + socket_index, bytes(body), [f"{profile['cpu_socket']} {socket_index + 1}", profile["cpu_vendor"], profile["cpu_version"], profile["cpu_serial"], identity["cpu_asset"], profile["cpu_part"]]))

    for handle, label, level, size, kind in (
        (0x0701, "L1 Cache", 0, profile["cache_l1_kib"], 0x05),
        (0x0702, "L2 Cache", 1, profile["cache_l2_kib"], 0x05),
        (0x0703, "L3 Cache", 2, profile["cache_l3_kib"], 0x05),
    ):
        legacy_size, extended = smbios_cache_size(int(size))
        body = struct.pack("<BHHHHHBBBBII", 1, 0x0180 | level, legacy_size, legacy_size, 0x0003, 0x0003, 0, 0x02, kind, 0x02, extended, extended)
        out.append(smbios_structure(7, handle, body, [label]))

    for index, item in enumerate(profile.get("connectors") or []):
        try:
            body = bytes.fromhex(str(item["body"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("SMBIOS Type 8 记录损坏") from exc
        out.append(smbios_structure(8, 0x0800 + index, body, item.get("strings") or ()))
    for index, item in enumerate(profile.get("slots") or []):
        try:
            body = bytes.fromhex(str(item["body"]))
        except (KeyError, TypeError, ValueError) as extra:
            raise RuntimeError("SMBIOS Type 9 记录损坏") from extra
        out.append(smbios_structure(9, 0x0900 + index, body, item.get("strings") or ()))

    oem_strings = list(profile.get("oem_strings") or [])
    if oem_strings:
        if len(oem_strings) > 255:
            raise RuntimeError("SMBIOS Type 11 OEM string 数量超限")
        out.append(smbios_structure(11, 0x0B00, bytes((len(oem_strings),)), oem_strings))
    for index, item in enumerate(profile.get("supplemental_tables") or []):
        try:
            kind = int(item["type"])
            body = bytes.fromhex(str(item["body"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("宿主 SMBIOS 补充表损坏") from exc
        if kind not in {12, 13, 26, 28, 29}:
            raise RuntimeError(f"不允许复制的 SMBIOS 补充表: Type {kind}")
        out.append(smbios_structure(kind, 0x3000 + index, body, item.get("strings") or ()))

    sizes = [int(value) for value in profile["dimm_sizes_mb"]]
    total_mb = sum(sizes)
    capacity_bytes = total_mb * 1024 * 1024
    capacity_kib = capacity_bytes // 1024
    max_field = capacity_kib if capacity_kib < 0x80000000 else 0x80000000
    extended_capacity = 0 if capacity_kib < 0x80000000 else capacity_bytes
    array_template = profile["memory_array"]
    out.append(smbios_structure(16, 0x1000, struct.pack(
        "<BBBIHHQ", int(array_template["location"]), int(array_template["use"]),
        int(array_template["error_correction"]), max_field, 0xFFFE, len(sizes), extended_capacity,
    )))

    address = 0
    ranges: list[tuple[int, int, int]] = []
    memory_type = int(platform["memory_type_code"])
    slot_templates = list(profile.get("memory_slots") or [])
    form_factor = int(platform["memory_form_factor"])
    voltage = 1100 if profile["memory_type"] == "ddr5" else 1200
    for index, size_mb in enumerate(sizes):
        template = slot_templates[index] if index < len(slot_templates) else {}
        device_locator = str(template.get("device_locator") or f"DIMM_{chr(65 + index)}")
        bank_locator = str(template.get("bank_locator") or f"BANK_{index // 2}")
        size_field, extended_size = (size_mb, 0) if size_mb < 0x7FFF else (0x7FFF, size_mb)
        body = struct.pack("<5H5B2H5BI4H", 0x1000, 0xFFFE, 64, 64, size_field, form_factor, 0, 1, 2, memory_type, 0x80, profile["memory_speed"], 3, 4, 5, 6, 1, extended_size, profile["memory_speed"], voltage, voltage, voltage)
        out.append(smbios_structure(17, 0x1100 + index, body, [device_locator, bank_locator, profile["memory_vendor"], profile["dimm_serials"][index], identity["dimm_assets"][index], profile["dimm_parts"][index]]))
        end = address + size_mb * 1024 * 1024 - 1
        ranges.append((address, end, 0x1100 + index))
        address = end + 1

    end_kib = max(0, total_mb * 1024 - 1)
    if end_kib <= 0xFFFFFFFF:
        mapped = struct.pack("<IIH B Q Q", 0, end_kib, 0x1000, len(sizes), 0, 0)
    else:
        mapped = struct.pack("<IIH B Q Q", 0xFFFFFFFF, 0xFFFFFFFF, 0x1000, len(sizes), 0, capacity_bytes - 1)
    out.append(smbios_structure(19, 0x1300, mapped))
    for index, (start, end, dimm_handle) in enumerate(ranges):
        body = struct.pack("<IIHHB B B Q Q", start // 1024, end // 1024, dimm_handle, 0x1300, index + 1, 0, 1, 0, 0)
        out.append(smbios_structure(20, 0x1400 + index, body))

    battery = profile.get("battery", {"enabled": False})
    if battery.get("enabled"):
        multiplier = max(1, (int(battery["design_capacity_mwh"]) + 0xFFFE) // 0xFFFF)
        capacity = min(0xFFFF, int(battery["design_capacity_mwh"]) // multiplier)
        body = struct.pack(
            "<6BHHBBHHBBI",
            1, 2, 3, 4, 5, 0x06, capacity, int(battery["design_voltage_mv"]),
            6, 0xFF, int(battery["sbds_serial"]), int(battery["sbds_manufacture_date"]),
            7, multiplier, 0,
        )
        out.append(smbios_structure(22, 0x1600, body, [
            "Internal Battery", battery["manufacturer"], battery["manufacture_date"],
            battery["serial"], battery["model"], "03.0", battery["chemistry"],
        ]))

    out.append(smbios_structure(32, 0x2000, bytes(7)))
    out.append(smbios_structure(127, 0x7F00, b""))
    data = b"".join(out)
    if not data.endswith(struct.pack("<BBH", 127, 4, 0x7F00) + b"\0\0"):
        raise ValueError("SMBIOS stream has no valid Type 127 terminator")
    validate_smbios_memory_topology(data, sizes)
    return data


def validate_record_coherence(record: dict[str, Any]) -> None:
    """Prove inherited platform fields still equal their captured host source."""
    platform = record["hardware"]["platform"]
    dmi = record["host"]["dmi_non_unique"]
    mappings = {
        "vendor": "sys_vendor", "product": "product_name", "version": "product_version",
        "family": "product_family", "sku": "product_sku", "board_vendor": "board_vendor",
        "board": "board_name", "board_version": "board_version",
        "chassis_vendor": "chassis_vendor", "chassis_version": "chassis_version",
        "bios_vendor": "bios_vendor", "bios_version": "bios_version", "bios_date": "bios_date",
    }
    mismatches = [target for target, source in mappings.items() if platform.get(target) != dmi.get(source)]
    if mismatches:
        raise RuntimeError("平台非唯一字段没有严格继承宿主: " + ", ".join(mismatches))
    canonical = dict(platform)
    canonical.pop("platform_id", None)
    canonical.pop("platform_source_version", None)
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    expected_id = "host-" + hashlib.sha256(encoded).hexdigest()[:20]
    if platform.get("platform_id") != expected_id:
        raise RuntimeError("宿主平台 ID 与继承字段不一致")
    if record["hardware"]["memory"]["type"] != record["host"]["smbios_non_unique"]["memory"]["type"]:
        raise RuntimeError("虚拟内存类型没有继承宿主平台能力")
    usb = record["hardware"]["usb_controller"]
    observed_usb = record["host"]["xhci_observed"]
    for field in (
        "pci_vendor_id", "pci_device_id", "revision_id", "subsystem_vendor_id",
        "subsystem_device_id", "bar_size_bytes", "serial_bus_release", "pcie_endpoint",
    ):
        if usb.get(field) != observed_usb.get(field):
            raise RuntimeError(f"xHCI 字段没有严格继承受支持的宿主设备: {field}")
    observed_pci = record.get("host", {}).get("pci_identities_profiled", {})
    profiled_pci = record.get("devices", {}).get("pci_identities", {})
    for name in ("host_bridge", "lpc", "smbus", "sata", "root_port", "vga", "hda"):
        if profiled_pci.get(name) != observed_pci.get(name):
            raise RuntimeError(f"{name} PCI identity does not match host observation")
    audio = record["hardware"]["audio"]
    if audio.get("xml_policy") != "onboard-hda":
        raise RuntimeError("板载 HDA 必须使用 onboard-hda XML 策略")
    if audio.get("bdf") != record["host"]["hda_controller_observed"].get("bdf"):
        raise RuntimeError("HDA PCI 地址没有继承板载控制器")
    pins = audio.get("codec_pins") or []
    if not any(pin.get("direction") == "playback" for pin in pins):
        raise RuntimeError("板载 analog codec 没有可继承的播放针脚")
    if any(pin.get("source") != "host-observed-onboard-codec" for pin in pins):
        raise RuntimeError("HDA 针脚必须来自板载 analog codec")
    vga = profiled_pci.get("vga") or {}
    if vga.get("source") != "qemu-stdvga-temporary" or vga.get("vendor_id") != "1234" or vga.get("device_id") != "1111":
        raise RuntimeError("临时 VGA 必须保持 QEMU 1234:1111，不能套用真实 GPU ID")
    if record["hardware"].get("mce_banks") != record["host"].get("mce_banks_observed"):
        raise RuntimeError("MCE bank 数量没有继承宿主")
    try:
        hotplug = int(str(record["hardware"].get("cpu_hotplug_io_base", "")), 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CPU 热插拔 IO 基址无效") from exc
    if not (0x4000 <= hotplug <= 0x7FF0) or hotplug & 0xF or hotplug == 0x0CD8:
        raise RuntimeError(f"CPU 热插拔 IO 基址不能使用 QEMU 默认口: {hotplug:#x}")
    observed_smbios = record["host"].get("smbios_non_unique") or {}
    if (record.get("smbios_profile") or {}).get("connectors") != observed_smbios.get("connectors"):
        raise RuntimeError("SMBIOS Type 8 没有继承宿主")
    if (record.get("smbios_profile") or {}).get("slots") != observed_smbios.get("slots"):
        raise RuntimeError("SMBIOS Type 9 没有继承宿主")
    for role in ("lpc", "smbus", "hda"):
        item = profiled_pci.get(role) or {}
        if item.get("slot_source") != "host-observed" or "pci_address" not in item:
            raise RuntimeError(f"{role} 缺少宿主 PCI 槽位，无法对齐 Q35 南桥")
    sata_slots = profiled_pci.get("sata") or {}
    if "pci_address" not in sata_slots or sata_slots.get("slot_source") not in {
        "host-observed", "q35-default-for-derived-ahci",
    }:
        raise RuntimeError("SATA 缺少可部署的 PCI 槽位")
    observed_acpi = record.get("host", {}).get("acpi_nodes_profiled", {})
    profiled_acpi = record.get("devices", {}).get("acpi_nodes", {})
    if observed_acpi != profiled_acpi:
        raise RuntimeError("ACPI 节点身份没有严格继承宿主观察结果")
    required_acpi = {"pci_root", "lpc", "smbus", "usb", "sata"}
    missing_acpi = sorted(required_acpi - set(profiled_acpi))
    if missing_acpi:
        raise RuntimeError("身份文件缺少宿主 ACPI 节点: " + ", ".join(missing_acpi))
    for role in ("lpc", "smbus", "usb"):
        name = profiled_acpi.get(role, {}).get("name")
        if not name or ACPI_NAMESEG_RE.fullmatch(str(name)) is None:
            raise RuntimeError(f"宿主 {role} ACPI NameSeg 无效")
    if profiled_acpi.get("lpc", {}).get("source") != "host-observed":
        raise RuntimeError("LPC ACPI 节点必须直接继承宿主 firmware_node")


def build_record(
    domain: str, root: ET.Element, xml_text: str, prefix: list[str]
) -> tuple[dict[str, Any], bytes]:
    host = host_cpu()
    detected_battery = host_system_battery()
    dmi = host_dmi()
    physical_identifiers = host_unique_identifiers()
    smbios_facts = host_smbios_facts(physical_identifiers)
    host_mce_count = host_mce_banks()
    cpu_hotplug_io_base = allocate_cpu_hotplug_io_base()
    acpi_identity = host_acpi_identity()
    power = host_power_profile()
    platform = host_platform_profile(host, dmi, smbios_facts, acpi_identity, detected_battery is not None)
    battery = battery_profile(detected_battery, bool(platform["has_battery"]))
    topology = cpu_topology(root)
    total_memory = memory_bytes(root)
    sizes = dimm_layout(total_memory, platform["memory_type"], platform["class"])
    memory_vendor, dimm_parts = memory_identity(platform["memory_type"], platform["class"], sizes)
    cache = host_cache_sizes()
    storage = parse_storage(root)
    interfaces = parse_interfaces(root)
    hostdevs = parse_hostdevs(root)
    host_pci = host_pci_subsystems()
    host_pci_id = host_pci_identities()
    root_ports = host_root_port_identities()
    profiled_root_ports = guest_root_port_profiles(root, root_ports)
    if not profiled_root_ports:
        raise RuntimeError("Guest XML 没有 PCIe Root Port")
    host_pci_id["root_port"] = deepcopy(profiled_root_ports[0])
    host_pci_id["root_ports"] = deepcopy(profiled_root_ports)
    required_primary = {"host_bridge", "lpc", "smbus", "sata", "root_port"}
    missing_primary = sorted(required_primary - set(host_pci_id))
    if missing_primary:
        raise RuntimeError("宿主缺少当前南桥身份所需的 PCI 主设备: " + ", ".join(missing_primary))
    host_xhci = host_xhci_controller()
    host_audio, host_codec = onboard_hda_identity()
    host_pci_id["hda"] = {
        "vendor_id": host_audio["vendor_id"],
        "device_id": host_audio["device_id"],
        "revision_id": host_audio["revision_id"],
        "subsystem_vendor_id": host_audio["subsystem_vendor_id"],
        "subsystem_device_id": host_audio["subsystem_device_id"],
        "description": host_audio["product"],
        "source": "host-observed-onboard",
        "bdf": host_audio["bdf"],
        "pci_address": deepcopy(host_audio["pci_address"]),
        "slot_source": "host-observed",
    }
    bind_southbridge_slots(host_pci_id, host_xhci)
    host_acpi = host_acpi_nodes(host_pci_id, host_xhci)
    interrupts = int(host_xhci["interrupts_observed"])
    if not (1 <= interrupts <= 16 and interrupts & (interrupts - 1) == 0):
        raise RuntimeError(f"宿主 xHCI interrupter 数量不受当前后端支持: {interrupts}")
    if int(host_xhci["bar_size_bytes"]) != 64 * 1024:
        raise RuntimeError("宿主 xHCI BAR 不是当前后端严格支持的 64 KiB")
    if host_xhci["pci_vendor_id"] == "8086" and not host_xhci["pcie_endpoint"]:
        capability_profile, interrupt_mode = "intel-pch-integrated", "msi"
    elif host_xhci["pci_vendor_id"] == "1022" and host_xhci["pcie_endpoint"]:
        capability_profile, interrupt_mode = "amd-pcie-xhci", "msix"
    else:
        raise RuntimeError("宿主 xHCI 集成形态不受当前 QEMU 后端支持，不允许使用 fallback")
    usb_controller = {
        **deepcopy(host_xhci),
        "qemu_model": "qemu-xhci",
        "capability_profile": capability_profile,
        "interrupt_mode": interrupt_mode,
        "interrupts": interrupts,
    }
    audio = {
        "controller_product": host_audio["product"],
        "controller_vendor_id": host_audio["vendor_id"],
        "controller_device_id": host_audio["device_id"],
        "controller_revision_id": host_audio["revision_id"],
        "controller_subsystem_vendor_id": host_audio["subsystem_vendor_id"],
        "controller_subsystem_device_id": host_audio["subsystem_device_id"],
        "codec_name": host_codec["name"],
        "codec_vendor_device_id": host_codec["vendor_device_id"],
        "codec_subsystem_id": host_codec["subsystem_id"],
        "codec_revision_id": host_codec["revision_id"],
        "codec_cad": host_codec["cad"],
        "codec_pins": deepcopy(host_codec["pins"]),
        "libvirt_codec": "duplex" if any(pin["direction"] == "capture" for pin in host_codec["pins"]) else "output",
        "bdf": host_audio["bdf"],
        "pci_address": deepcopy(host_audio["pci_address"]),
        "implementation": "ich9-hda",
        "xml_policy": "onboard-hda",
        "patch_if_present": True,
    }
    host_pci["usb"] = {
        "vendor_id": host_xhci["subsystem_vendor_id"],
        "device_id": host_xhci["subsystem_device_id"],
        "source": "host-observed-selected-xhci",
    }
    host_pci["audio"] = {
        "vendor_id": host_audio["subsystem_vendor_id"],
        "device_id": host_audio["subsystem_device_id"],
        "source": "host-observed-selected-hda",
    }
    required_pci = {"host_bridge", "root_port", "usb", "lpc", "smbus", "sata", "audio", "display"}
    missing_pci = sorted(required_pci - set(host_pci))
    if missing_pci:
        raise RuntimeError("宿主缺少当前 Q35 后端必需的 PCI subsystem 身份: " + ", ".join(missing_pci))
    host_pci_id["vga"] = host_vga_identity(host_pci["display"])
    pci_subsystems = deepcopy(host_pci)
    normalized_storage = []
    shared_scsi_identity: dict[str, str] | None = None
    for item in storage:
        entry = dict(item)
        identity_record = storage_identity(item["device"], item["bus"])
        if item["device"] != "cdrom" and item["bus"].lower() in {"scsi", "sas"}:
            if shared_scsi_identity is None:
                shared_scsi_identity = identity_record
            else:
                identity_record = deepcopy(shared_scsi_identity)
        entry["identity"] = identity_record
        normalized_storage.append(entry)
    legacy_ata_disks = [
        item for item in normalized_storage
        if item["device"] != "cdrom" and item["bus"] in {"ide", "sata"}
    ]
    if len(legacy_ata_disks) > 1:
        raise RuntimeError(
            "当前 QEMU/libvirt 后端无法为多块 IDE/SATA 磁盘逐设备传递 model/firmware；"
            "请保留一块 SATA 盘，其余使用 SCSI/NVMe"
        )
    generated_uuid = str(uuid.uuid4())
    profile_id = str(uuid.uuid4())
    serials = platform_serials(platform)
    mac_addresses = [random_mac(item["model_target"]) for item in interfaces]
    network_adapters = generate_lan_profiles(mac_addresses, profile_id, prefix)
    for interface, adapter in zip(interfaces, network_adapters):
        interface["managed_network"] = adapter["libvirt_network"]["name"]
    identity = {
        "system_uuid": generated_uuid,
        "system_serial": serials["system"],
        "board_serial": serials["board"],
        "chassis_serial": serials["chassis"],
        "bios_serial": random_hex(16),
        "cpu_serial": random_hex(16),
        "cpu_asset": random_alnum(10),
        "board_asset": random_alnum(10),
        "chassis_asset": random_alnum(10),
        "chassis_sku": platform["sku"],
        "dimm_serials": [random_hex(8) for _ in sizes],
        "dimm_assets": [random_alnum(10) for _ in sizes],
        "disk_serials": [storage_serial(item["identity"]) for item in normalized_storage if item["device"] != "cdrom"],
        "disk_wwns": [storage_wwn(item["identity"]) for item in normalized_storage if item["device"] != "cdrom"],
        "mac_addresses": mac_addresses,
        "network_adapters": network_adapters,
        "usb_mouse_serial": "".join(secrets.choice("0123456789") for _ in range(12)),
        "usb_keyboard_serial": "".join(secrets.choice("0123456789") for _ in range(12)),
        "nvram_id": str(uuid.uuid4()),
    }
    copied = [
        name for name, generated, physical in (
            ("system_uuid", identity["system_uuid"], physical_identifiers["product_uuid"]),
            ("system_serial", identity["system_serial"], physical_identifiers["product_serial"]),
            ("board_serial", identity["board_serial"], physical_identifiers["board_serial"]),
            ("chassis_serial", identity["chassis_serial"], physical_identifiers["chassis_serial"]),
        )
        if generated.strip().lower() == physical.strip().lower()
    ]
    if copied:
        raise RuntimeError("生成身份意外复制了宿主唯一标识: " + ", ".join(copied))
    fw = firmware_info(platform)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    serial_index = 0
    for entry in normalized_storage:
        if entry["device"] != "cdrom":
            entry["serial"] = identity["disk_serials"][serial_index]
            entry["wwn"] = identity["disk_wwns"][serial_index]
            serial_index += 1
        else:
            entry["serial"] = ""
            entry["wwn"] = ""

    legacy = {
        "schema": SCHEMA_VERSION,
        "vm_name": domain,
        "system_uuid": generated_uuid,
        "platform": "amd" if host["vendor"] == "AuthenticAMD" else "intel",
        "vendor": platform["vendor"],
        "product_name": platform["product"],
        "product_family": platform["family"],
        "product_version": platform.get("version", "1.0"),
        "product_sku": platform["sku"],
        "board_name": platform["board"],
        "board_version": platform["board_version"],
        "chassis_version": platform["chassis_version"],
        "bios_vendor": fw["vendor"],
        "bios_version": fw["version"],
        "bios_date": fw["date"],
        "system_serial": identity["system_serial"],
        "disk_serial": next((item["serial"] for item in normalized_storage if item["serial"]), ""),
        "board_serial": identity["board_serial"],
        "chassis_serial": identity["chassis_serial"],
        "cpu_serial": identity["cpu_serial"],
        "cpu_version": host["model_name"],
        "cpu_features_edx": host["features_edx"],
        "cpu_socket": platform["socket"],
        "cpu_vendor": "Advanced Micro Devices, Inc." if host["vendor"] == "AuthenticAMD" else "GenuineIntel",
        "cpu_part": host["model_name"],
        "cache_l1_kib": cache["l1"],
        "cache_l2_kib": cache["l2"],
        "cache_l3_kib": cache["l3"],
        "cpu_max_mhz": host["max_mhz"],
        "cpu_current_mhz": min(host["base_mhz"] or host["current_mhz"], host["max_mhz"]),
        "memory_type": platform["memory_type"],
        "memory_vendor": memory_vendor,
        "memory_part": dimm_parts[0],
        "dimm_parts": dimm_parts,
        "memory_speed": platform["memory_speed"],
        "dimm_sizes_mb": sizes,
        "dimm_serials": identity["dimm_serials"],
        "dimm_count": len(sizes),
        "sockets": topology["sockets"],
        # The CPU brand/signature comes from the host, but SMBIOS topology must
        # agree with the CPUs that ACPI and CPUID actually expose to the guest.
        "cores": topology["dies"] * topology["clusters"] * topology["cores"],
        "enabled_cores": topology["dies"] * topology["clusters"] * topology["cores"],
        "threads": topology["vcpus"] // topology["sockets"],
        "pci_slots": max(2, min(16, len(root.findall("./devices/controller[@type='pci']")) or 2)),
        "cpu_signature": host["signature"],
        "cpu_family": processor_family(host["model_name"], host["vendor"] == "AuthenticAMD")[0],
        "cpu_family2": processor_family(host["model_name"], host["vendor"] == "AuthenticAMD")[1],
        "cpu_upgrade": int(platform["cpu_upgrade"]),
        "cpu_voltage": int(smbios_facts["processor"]["voltage"]),
        "cpu_external_clock_mhz": int(smbios_facts["processor"]["external_clock_mhz"]),
        "cpu_characteristics": int(smbios_facts["processor"]["characteristics"]),
        "battery": battery,
        "oem_strings": deepcopy(smbios_facts.get("oem_strings") or []),
        "supplemental_tables": deepcopy(smbios_facts.get("supplemental") or []),
        "memory_array": deepcopy(smbios_facts["memory"]["array"]),
        "memory_slots": deepcopy(smbios_facts["memory"]["slots"]),
        "connectors": deepcopy(smbios_facts.get("connectors") or []),
        "slots": deepcopy(smbios_facts.get("slots") or []),
        "created_at": now,
    }
    smbios = build_smbios_stream(legacy, platform, identity)
    xml_sha = hashlib.sha256(xml_text.encode()).hexdigest()
    record: dict[str, Any] = {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "platform_source": "host-non-unique",
            "platform_source_version": PLATFORM_SOURCE_VERSION,
            "platform_id": platform["platform_id"],
            "generated_at": now,
            "generator": "spoof_v2/01_generate_identity.py",
            "profile_id": profile_id,
            "identity_locked": True,
        },
        "source": {
            "domain": domain,
            "libvirt_connection": "qemu:///system",
            "domain_xml_sha256": xml_sha,
        },
        "vm_baseline": {
            "machine": (root.find("os/type").get("machine") if root.find("os/type") is not None else ""),
            "memory_bytes": total_memory,
            "topology": topology,
            "loader": dict((root.find("os/loader").attrib if root.find("os/loader") is not None else {})),
            "nvram": dict((root.find("os/nvram").attrib if root.find("os/nvram") is not None else {})),
        },
        "host": {
            "cpu": host,
            "memory_kib": host_memory_kib(),
            "cache_kib": cache,
            "system_battery": detected_battery,
            "dmi_non_unique": dmi,
            "smbios_non_unique": smbios_facts,
            "acpi_non_unique": acpi_identity,
            "power_non_unique": power,
            "pci_subsystems_profiled": host_pci,
            "pci_identities_profiled": host_pci_id,
            "acpi_nodes_profiled": host_acpi,
            "xhci_observed": host_xhci,
            "hda_controller_observed": host_audio,
            "hda_codec_observed": host_codec,
            "mce_banks_observed": host_mce_count,
        },
        "hardware": {
            "platform": platform,
            "machine": {
                "implementation": "pc-q35-11.0",
                "description": f"{platform['vendor']} {platform['product']}",
            },
            "cpu_model": host["model_name"],
            "cpu_signature": host["signature"],
            "mce_banks": host_mce_count,
            "cpu_hotplug_io_base": f"0x{cpu_hotplug_io_base:04x}",
            "memory": {
                "type": platform["memory_type"],
                "speed_mt": platform["memory_speed"],
                "sizes_mib": sizes,
                "vendor": legacy["memory_vendor"],
                "part": legacy["memory_part"],
                "parts": dimm_parts,
                "array": deepcopy(smbios_facts["memory"]["array"]),
                "slots": deepcopy(smbios_facts["memory"]["slots"]),
            },
            "power": power,
            "storage_controller": {
                "sata_generation": 3,
                "ata_major_version": 8,
                "udma_mode": 6,
                "ncq": True,
                "source": "modern-ahci-profile",
            },
            "cache_kib": cache,
            "firmware": fw,
            "battery": battery,
            "audio": audio,
            "usb_controller": usb_controller,
            "network": {
                **NETWORK_TEMPLATE,
                "model": NETWORK_MODEL,
                "nvm": {
                    "word_count": 1024,
                    "checksum_word": "003f",
                    "checksum_sum": "baba",
                    "permanent_mac_source": "identity.network_adapters[].permanent_mac",
                },
            },
        },
        "storage": {
            "devices": normalized_storage,
            "preserve_current_bus": True,
        },
        "devices": {
            "interfaces": interfaces,
            "hostdevs": hostdevs,
            "gpu_passthrough": any(item["is_display"] for item in hostdevs),
            "pci_subsystems": pci_subsystems,
            "pci_identities": deepcopy(host_pci_id),
            "acpi_nodes": deepcopy(host_acpi),
        },
        "identity": identity,
        "qemu_policy": {
            "hide_hypervisor_leaf": True,
            "hide_kvm": True,
            "host_passthrough": True,
            "migratable": False,
            "cache_passthrough": True,
            "battery_acpi": bool(battery["enabled"]),
            "smbios_entry_point": {
                "major": 3,
                "minor": 5 if platform["memory_type"] == "ddr5" else 2,
                "docrev": 0,
            },
            "usb_hid": {
                **deepcopy(secrets.choice(HID_CATALOG)),
                "mouse_serial": identity["usb_mouse_serial"],
                "keyboard_serial": identity["usb_keyboard_serial"],
            },
        },
        "ovmf_policy": {
            "firmware_vendor": fw["vendor"],
            "firmware_version": fw["version"],
            "firmware_date": fw["date"],
            "acpi_oem_id": fw["acpi_oem_id"],
            "acpi_table_id": fw["acpi_table_id"],
            "acpi_creator_id": fw["acpi_creator_id"],
            "acpi_creator_revision": fw["acpi_creator_revision"],
            "secure_boot": False,
        },
        "xml_policy": {
            "preserve_domain_uuid": True,
            "smbios_system_uuid": generated_uuid,
            "inject_smbios_full_file": True,
            "preserve_storage_bus": True,
            "remove_obvious_virtio_console": True,
            "kvm_hidden": True,
            "vmport": False,
            "audio": "onboard-hda",
        },
        "smbios_profile": legacy,
    }
    validate_record_coherence(record)
    record["artifacts"] = {"smbios_sha256": hashlib.sha256(smbios).hexdigest()}
    return record, smbios


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def main() -> int:
    try:
        prefix = virsh_prefix()
        names = list_domains(prefix)
        if not names:
            print("没有找到 libvirt 虚拟机。", file=sys.stderr)
            return 1
        domain = choose_domain(names)
        xml_text = read_domain_xml(prefix, domain)
        root = ET.fromstring(xml_text)
        record, smbios = build_record(domain, root, xml_text, prefix)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        atomic_write(ARTIFACTS / "smbios.bin", smbios)
        atomic_write(
            ARTIFACTS / "identity-hardware.json",
            (json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(),
        )
        print("已生成新的硬件身份：")
        print(f"  虚拟机: {domain}")
        print(f"  JSON:   {ARTIFACTS / 'identity-hardware.json'}")
        print(f"  SMBIOS: {ARTIFACTS / 'smbios.bin'}")
        print(f"  CPU:    {record['host']['cpu']['model_name']}")
        selected_platform = record["hardware"]["platform"]
        print(
            f"  平台:   {selected_platform['vendor']} {selected_platform['product']} "
            f"[{selected_platform['platform_id']}] "
            f"(电池: {'yes' if record['hardware']['battery']['enabled'] else 'no'})"
        )
        print(f"  存储:   {', '.join(item['bus'] for item in record['storage']['devices']) or 'none'}")
        usb = record["hardware"]["usb_controller"]
        print(
            f"  USB:    {usb['manufacturer']} {usb['product']} "
            f"({usb['pci_vendor_id']}:{usb['pci_device_id']}, "
            f"USB2 {usb['usb2_ports']} + USB3 {usb['usb3_ports']})"
        )
        audio = record["hardware"]["audio"]
        pin_desc = ", ".join(f"{pin['device']}:{pin['config']}" for pin in audio["codec_pins"])
        print(
            f"  音频:   {audio['controller_product']} {audio['bdf']} "
            f"({audio['controller_vendor_id']}:{audio['controller_device_id']}) "
            f"{audio['codec_name']} [{pin_desc}]"
        )
        for index, adapter in enumerate(record["identity"]["network_adapters"], 1):
            print(
                f"  网络{index}: MAC {adapter['mac']}, "
                f"IP {adapter['ipv4']['address']}/{adapter['ipv4']['prefix_length']} "
                f"via {adapter['gateway']['ipv4_address']} "
                f"({adapter['gateway']['mac']})"
            )
        print(f"  UUID:   {record['identity']['system_uuid']}")
        print(
            f"  MCE:    {record['hardware']['mce_banks']} banks, "
            f"CPU hotplug IO {record['hardware']['cpu_hotplug_io_base']}"
        )
        print(
            f"  SMBIOS: Type8 x{len(record['smbios_profile'].get('connectors') or [])}, "
            f"Type9 x{len(record['smbios_profile'].get('slots') or [])}"
        )
        return 0
    except (OSError, ValueError, RuntimeError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
