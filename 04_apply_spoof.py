#!/usr/bin/env python3
"""Apply the generated identity and built firmware to one libvirt domain.

The script selects a domain interactively, backs up its current XML, validates
the two artifacts, and defines the updated XML.  ``--dry-run`` performs the
same validation and transformation in memory, then prints a unified diff.
Storage bus/controller information is preserved from the profile rather than
forced to SATA or NVMe.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from smbios_memory import validate_record as validate_memory_record, xml_memory_bytes

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
PROFILE_PATH = ARTIFACTS / "identity-hardware.json"
BUILD = ROOT / "build"
BACKUPS = ROOT / "backups"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
RUNTIME_ROOT = Path("/opt/ovo-spoof/profiles")
ET.register_namespace("qemu", QEMU_NS)

I226_NETWORK_IDENTITY = {
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
I226_EXTENDED_CAPABILITIES = ["aer-v2", "dsn", "ltr", "l1-pm-substates", "ptm"]


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "command failed: " + " ".join(command))
    return result


def run_visible(command: list[str]) -> None:
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(command))


def available_host_cpus() -> set[int]:
    """Return logical CPUs available to this process/libvirt launcher."""
    try:
        return set(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return set(range(os.cpu_count() or 1))


def parse_cpu_set(value: str, available: set[int]) -> set[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"CPU 集合无效: {item}")
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"CPU 范围无效: {item}")
            result.update(range(start, end + 1))
        elif item.isdigit():
            result.add(int(item))
        else:
            raise ValueError(f"CPU 编号无效: {item}")
    if not result or not result.issubset(available):
        raise ValueError("CPU 集合包含不可用或空的 CPU")
    return result


def format_cpu_set(cpus: set[int]) -> str:
    values = sorted(cpus)
    if not values:
        return ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def vcpu_count(root: ET.Element) -> int:
    node = root.find("vcpu")
    if node is None or not (node.text and node.text.strip().isdigit()):
        raise ValueError("VM XML 缺少数字形式的 <vcpu>")
    count = int(node.text.strip())
    if int(node.get("current", str(count)), 0) != count or root.find("vcpus") is not None:
        raise ValueError("Fixed SMBIOS CPU topology does not support partial/hotplug vCPUs")
    if count < 1:
        raise ValueError("VM XML 的 vCPU 数量必须大于零")
    return count


def fit_vcpu_count(root: ET.Element, available: set[int]) -> int:
    requested = vcpu_count(root)
    if len(available) < 2:
        raise ValueError("至少需要 2 个可用逻辑 CPU 才能进行 vCPU/emulator 绑定")
    if requested > len(available) - 1:
        raise ValueError(
            f"VM 需要 {requested} 个 vCPU 并为 emulator 保留 CPU，但当前仅有 "
            f"{len(available)} 个可用逻辑 CPU；请调整 XML 后重新运行 01_generate_identity.py"
        )
    return requested


def validate_hardware_baseline(root: ET.Element, profile: dict) -> None:
    """Reject XML drift that would contradict the generated SMBIOS profile."""
    actual_memory = xml_memory_bytes(root)
    expected_memory = int(profile["vm_baseline"]["memory_bytes"])
    if actual_memory != expected_memory:
        raise ValueError("当前 XML 的内存容量已改变，请重新运行 01_generate_identity.py")

    expected = profile["vm_baseline"]["topology"]
    topology = root.find("cpu/topology")
    actual = {
        "vcpus": vcpu_count(root),
        "sockets": int(topology.get("sockets", "1"), 0) if topology is not None else 1,
        "dies": int(topology.get("dies", "1"), 0) if topology is not None else 1,
        "clusters": int(topology.get("clusters", "1"), 0) if topology is not None else 1,
        "cores": int(topology.get("cores", str(vcpu_count(root))), 0) if topology is not None else vcpu_count(root),
        "threads": int(topology.get("threads", "1"), 0) if topology is not None else 1,
    }
    if any(int(expected.get(name, 1)) != value for name, value in actual.items()):
        raise ValueError("当前 XML 的 vCPU 数量或拓扑已改变，请重新运行 01_generate_identity.py")


def configured_cpu_bindings(root: ET.Element, available: set[int]) -> tuple[set[int], set[int]]:
    count = vcpu_count(root)
    ordered = sorted(available)
    cputune = root.find("cputune")
    selected: set[int] | None = None
    if cputune is not None:
        try:
            pins: dict[int, int] = {}
            for pin in cputune.findall("vcpupin"):
                index = int(pin.get("vcpu", ""))
                values = parse_cpu_set(pin.get("cpuset", ""), available)
                if len(values) != 1 or index in pins:
                    raise ValueError("vcpupin 必须一一对应单个 CPU")
                pins[index] = next(iter(values))
            if set(pins) == set(range(count)) and len(set(pins.values())) == count:
                selected = set(pins.values())
        except (TypeError, ValueError):
            selected = None
    if selected is None:
        if len(ordered) < count:
            raise ValueError(f"VM 需要 {count} 个 vCPU，但当前只有 {len(ordered)} 个可用 CPU")
        selected = set(ordered[:count])

    emulator: set[int] | None = None
    if cputune is not None and cputune.find("emulatorpin") is not None:
        try:
            candidate = parse_cpu_set(cputune.find("emulatorpin").get("cpuset", ""), available)
            if not candidate & selected:
                emulator = candidate
        except ValueError:
            pass
    if emulator is None:
        emulator = available - selected
    if not emulator:
        raise ValueError("没有剩余逻辑 CPU 可分配给 emulatorpin")
    return selected, emulator


def set_vcpu_pinning(root: ET.Element, vcpu_cpus: set[int], emulator_cpus: set[int]) -> None:
    cputune = root.find("cputune")
    if cputune is None:
        cputune = ET.Element("cputune")
        vcpu = root.find("vcpu")
        insert_at = list(root).index(vcpu) + 1 if vcpu is not None else 0
        root.insert(insert_at, cputune)
    for child in list(cputune):
        if child.tag in {"vcpupin", "emulatorpin"}:
            cputune.remove(child)
    for index, cpu in enumerate(sorted(vcpu_cpus)):
        ET.SubElement(cputune, "vcpupin", {"vcpu": str(index), "cpuset": str(cpu)})
    ET.SubElement(cputune, "emulatorpin", {"cpuset": format_cpu_set(emulator_cpus)})


def virsh_prefix() -> list[str]:
    probe = run(["virsh", "--connect", "qemu:///system", "list", "--all", "--name"], check=False)
    return ["virsh", "--connect", "qemu:///system"] if probe.returncode == 0 else ["virsh"]


def select_domain(prefix: list[str]) -> str:
    result = run(prefix + ["list", "--all", "--name"])
    names = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    if not names:
        raise RuntimeError("没有找到 libvirt 虚拟机")
    print("可用虚拟机：")
    for index, name in enumerate(names, 1):
        print(f"  {index}) {name}")
    while True:
        answer = input("请选择要应用的虚拟机编号：").strip()
        try:
            return names[int(answer) - 1]
        except (ValueError, IndexError):
            print("请输入有效编号。")


def validate_inherited_platform(profile: dict) -> None:
    platform = profile.get("hardware", {}).get("platform", {})
    dmi = profile.get("host", {}).get("dmi_non_unique", {})
    mappings = {
        "vendor": "sys_vendor", "product": "product_name", "version": "product_version",
        "family": "product_family", "sku": "product_sku", "board_vendor": "board_vendor",
        "board": "board_name", "board_version": "board_version",
        "chassis_vendor": "chassis_vendor", "chassis_version": "chassis_version",
        "bios_vendor": "bios_vendor", "bios_version": "bios_version", "bios_date": "bios_date",
    }
    mismatches = [target for target, source in mappings.items() if platform.get(target) != dmi.get(source)]
    if mismatches:
        raise ValueError("平台非唯一字段没有严格继承宿主: " + ", ".join(mismatches))
    canonical = dict(platform)
    canonical.pop("platform_id", None)
    canonical.pop("platform_source_version", None)
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    expected_id = "host-" + hashlib.sha256(encoded).hexdigest()[:20]
    if platform.get("platform_id") != expected_id:
        raise ValueError("宿主平台 ID 与身份文件内容不一致")
    memory = profile.get("hardware", {}).get("memory", {})
    observed_memory = profile.get("host", {}).get("smbios_non_unique", {}).get("memory", {})
    if memory.get("type") != observed_memory.get("type") or memory.get("speed_mt") != observed_memory.get("speed_mt"):
        raise ValueError("内存类型或速度没有继承宿主平台能力")


def load_artifacts() -> tuple[dict, bytes]:
    profile_path = ARTIFACTS / "identity-hardware.json"
    smbios_path = ARTIFACTS / "smbios.bin"
    if not profile_path.exists() or not smbios_path.exists():
        raise FileNotFoundError("缺少 identity-hardware.json 或 smbios.bin，请先运行 01_generate_identity.py")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    validate_memory_record(profile)
    if (
        profile.get("meta", {}).get("platform_source") != "host-non-unique"
        or int(profile.get("meta", {}).get("platform_source_version", 0)) != 3
        or not profile.get("meta", {}).get("platform_id")
    ):
        raise ValueError("身份文件缺少宿主非唯一平台来源信息")
    if profile.get("ovmf_policy", {}).get("secure_boot") is not False:
        raise ValueError("当前身份文件未明确关闭 Secure Boot，请重新运行 01_generate_identity.py")
    smbios = smbios_path.read_bytes()
    expected = profile.get("artifacts", {}).get("smbios_sha256")
    actual = hashlib.sha256(smbios).hexdigest()
    if expected != actual:
        raise ValueError("smbios.bin 与 identity-hardware.json 不匹配")
    validate_memory_record(profile, smbios)
    if profile.get("source", {}).get("domain") is None:
        raise ValueError("身份文件缺少源虚拟机名称")
    if profile.get("hardware", {}).get("audio", {}).get("xml_policy") != "onboard-hda" or profile.get("xml_policy", {}).get("audio") != "onboard-hda":
        raise ValueError("身份文件未声明板载 HDA 策略")
    validate_inherited_platform(profile)
    primary = profile.get("devices", {}).get("pci_identities", {})
    required_primary = {"host_bridge", "lpc", "smbus", "sata", "root_port", "vga", "hda"}
    if not required_primary.issubset(primary):
        raise ValueError("身份文件缺少南桥/Root Port/VGA 主 PCI 身份，请重新运行 01_generate_identity.py")
    if not primary.get("root_ports"):
        raise ValueError("身份文件缺少 Root Port 实例映射")
    power = profile.get("hardware", {}).get("power", {})
    if power.get("source") != "host-observed" or not {
        "preferred_pm_profile", "c2_latency_us", "c3_latency_us",
        "suspend_to_mem", "suspend_to_disk",
    }.issubset(power):
        raise ValueError("身份文件缺少宿主 FADT/休眠能力")
    vga = primary.get("vga") or {}
    if vga.get("source") != "qemu-stdvga-temporary" or str(vga.get("vendor_id", "")).lower() != "1234" or str(vga.get("device_id", "")).lower() != "1111":
        raise ValueError("临时 VGA 必须保持 QEMU 1234:1111，请重新运行 01_generate_identity.py")
    acpi_nodes = profile.get("devices", {}).get("acpi_nodes", {})
    required_acpi = {"pci_root", "lpc", "smbus", "usb", "sata"}
    if not required_acpi.issubset(acpi_nodes):
        raise ValueError("身份文件缺少宿主 ACPI 节点，请重新运行 01_generate_identity.py")
    acpi_name_re = re.compile(r"\A[A-Z_][A-Z0-9_]{3}\Z")
    for role in ("lpc", "smbus", "usb"):
        node = acpi_nodes.get(role) or {}
        if node.get("source") != "host-observed" or acpi_name_re.fullmatch(str(node.get("name", ""))) is None:
            raise ValueError(f"身份文件中的 {role} ACPI 节点无效，请重新运行 01_generate_identity.py")
    if not re.fullmatch(r"[0-9a-fA-F]{4}", str(primary.get("host_bridge", {}).get("device_id", ""))):
        raise ValueError("身份文件缺少有效的 Host Bridge DID")
    occupied: dict[tuple[int, int], str] = {}
    for role in ("lpc", "smbus", "sata"):
        item = primary.get(role) or {}
        address = item.get("pci_address") or {}
        try:
            bus = int(address["bus"], 0)
            slot = int(address["slot"], 0)
            function = int(address["function"], 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"身份文件缺少 {role} PCI 槽位") from exc
        if bus != 0 or not (0 <= slot <= 31 and 0 <= function <= 7):
            raise ValueError(f"{role} PCI 槽位超出 Q35 根总线范围")
        if role in {"lpc", "smbus"} and item.get("slot_source") != "host-observed":
            raise ValueError(f"{role} PCI 槽位必须来自宿主观察")
        key = (slot, function)
        if key in occupied:
            raise ValueError(f"{role} 与 {occupied[key]} 占用同一 PCI 地址")
        occupied[key] = role
    usb_address = profile.get("hardware", {}).get("usb_controller", {}).get("pci_address") or {}
    try:
        usb_key = (int(usb_address["slot"], 0), int(usb_address["function"], 0))
    except (KeyError, TypeError, ValueError):
        usb_key = None
    if usb_key in occupied:
        raise ValueError(f"xHCI 与 {occupied[usb_key]} 占用同一 PCI 地址")
    validate_network_identity(profile)
    validate_usb_identity(profile)
    validate_audio_identity(profile)
    validate_cpu_platform_identity(profile)
    return profile, smbios


def validate_usb_identity(profile: dict) -> None:
    usb = profile.get("hardware", {}).get("usb_controller", {})
    hex_fields = {
        "pci_vendor_id": 4,
        "pci_device_id": 4,
        "revision_id": 2,
        "subsystem_vendor_id": 4,
        "subsystem_device_id": 4,
        "serial_bus_release": 2,
    }
    for key, width in hex_fields.items():
        if not re.fullmatch(rf"[0-9a-fA-F]{{{width}}}", str(usb.get(key, ""))):
            raise ValueError(f"xHCI 身份字段无效: hardware.usb_controller.{key}")
    if usb.get("qemu_model") != "qemu-xhci" or usb.get("interrupt_mode") not in {"msi", "msix"}:
        raise ValueError("xHCI 后端模型或中断模式不一致")
    if usb.get("capability_profile") not in {"intel-pch-integrated", "amd-pcie-xhci"}:
        raise ValueError("xHCI capability profile 不受支持")
    usb2_ports = int(usb.get("usb2_ports", 0))
    usb3_ports = int(usb.get("usb3_ports", 0))
    slots = int(usb.get("slots", 0))
    interrupts = int(usb.get("interrupts", 0))
    if not (1 <= usb2_ports <= 15 and 1 <= usb3_ports <= 15):
        raise ValueError("xHCI USB2/USB3 端口数无效")
    if not (1 <= slots <= 64 and 1 <= interrupts <= 16 and interrupts & (interrupts - 1) == 0):
        raise ValueError("xHCI slot/interrupter 数量无效")
    if int(usb.get("bar_size_bytes", 0)) != 64 * 1024:
        raise ValueError("xHCI BAR 大小不是 64 KiB")
    address = usb.get("pci_address", {})
    for key, width in (("domain", 4), ("bus", 2), ("slot", 2), ("function", 1)):
        if not re.fullmatch(rf"0x[0-9a-fA-F]{{{width}}}", str(address.get(key, ""))):
            raise ValueError(f"xHCI PCI 地址字段无效: {key}")
    subsystem = profile.get("devices", {}).get("pci_subsystems", {}).get("usb", {})
    if (
        str(subsystem.get("vendor_id", "")).lower() != str(usb["subsystem_vendor_id"]).lower()
        or str(subsystem.get("device_id", "")).lower() != str(usb["subsystem_device_id"]).lower()
    ):
        raise ValueError("xHCI 与 PCI subsystem 记录不一致")
    integrated = usb["capability_profile"] == "intel-pch-integrated"
    if integrated != (not bool(usb.get("pcie_endpoint"))):
        raise ValueError("xHCI 集成/PCIe capability 记录相互矛盾")
    expected_interrupt = "msi" if integrated else "msix"
    if usb["interrupt_mode"] != expected_interrupt:
        raise ValueError("xHCI 中断模式与平台 capability profile 不一致")


def validate_cpu_platform_identity(profile: dict) -> None:
    try:
        banks = int(profile.get("hardware", {}).get("mce_banks"))
        hotplug = int(str(profile.get("hardware", {}).get("cpu_hotplug_io_base", "")), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("身份文件缺少 MCE bank 或 CPU 热插拔 IO 基址") from exc
    if banks != profile.get("host", {}).get("mce_banks_observed"):
        raise ValueError("MCE bank 数量没有继承宿主")
    if not (1 <= banks <= 32):
        raise ValueError(f"MCE bank 数量超出范围: {banks}")
    if not (0x4000 <= hotplug <= 0x7FF0) or hotplug & 0xF or hotplug == 0x0CD8:
        raise ValueError(f"CPU 热插拔 IO 基址无效: {hotplug:#x}")
    observed = profile.get("host", {}).get("smbios_non_unique") or {}
    smbios = profile.get("smbios_profile") or {}
    if smbios.get("connectors") != observed.get("connectors"):
        raise ValueError("SMBIOS Type 8 没有继承宿主")
    if smbios.get("slots") != observed.get("slots"):
        raise ValueError("SMBIOS Type 9 没有继承宿主")


def validate_audio_identity(profile: dict) -> None:
    audio = profile.get("hardware", {}).get("audio", {})
    if audio.get("implementation") != "ich9-hda" or audio.get("patch_if_present") is not True:
        raise ValueError("HDA 后端身份策略无效")
    if not str(audio.get("controller_product", "")).strip() or not str(audio.get("codec_name", "")).strip():
        raise ValueError("HDA controller/codec 名称缺失")
    for key, width in (
        ("controller_vendor_id", 4), ("controller_device_id", 4),
        ("controller_revision_id", 2), ("controller_subsystem_vendor_id", 4),
        ("controller_subsystem_device_id", 4), ("codec_vendor_device_id", 8),
        ("codec_subsystem_id", 8), ("codec_revision_id", 8),
    ):
        if not re.fullmatch(rf"[0-9a-fA-F]{{{width}}}", str(audio.get(key, ""))):
            raise ValueError(f"HDA 身份字段无效: hardware.audio.{key}")
    subsystem = profile.get("devices", {}).get("pci_subsystems", {}).get("audio", {})
    if (
        str(subsystem.get("vendor_id", "")).lower() != str(audio["controller_subsystem_vendor_id"]).lower()
        or str(subsystem.get("device_id", "")).lower() != str(audio["controller_subsystem_device_id"]).lower()
    ):
        raise ValueError("HDA 与 PCI subsystem 记录不一致")
    pins = audio.get("codec_pins") or []
    if not isinstance(pins, list) or not any(pin.get("direction") == "playback" for pin in pins):
        raise ValueError("身份文件缺少板载 HDA 播放针脚")
    if audio.get("libvirt_codec") not in {"duplex", "output"}:
        raise ValueError("板载 HDA codec 类型无效")
    address = audio.get("pci_address") or {}
    hda = profile.get("devices", {}).get("pci_identities", {}).get("hda") or {}
    if hda.get("pci_address") != address or hda.get("source") != "host-observed-onboard":
        raise ValueError("板载 HDA PCI 槽位与身份记录不一致")
    try:
        if int(address.get("bus", "1"), 0) != 0:
            raise ValueError("板载 HDA 必须在 PCI 总线 0")
    except (TypeError, ValueError) as exc:
        raise ValueError("板载 HDA PCI 地址无效") from exc


def validate_network_identity(profile: dict) -> None:
    network = profile.get("hardware", {}).get("network", {})
    for key, expected in I226_NETWORK_IDENTITY.items():
        if str(network.get(key, "")).lower() != expected.lower():
            raise ValueError(f"网卡身份字段不一致: hardware.network.{key}")
    if network.get("extended_capabilities") != I226_EXTENDED_CAPABILITIES:
        raise ValueError("网卡 PCIe 扩展能力链与身份文件不一致")

    interfaces = profile.get("devices", {}).get("interfaces", [])
    mac_addresses = profile.get("identity", {}).get("mac_addresses", [])
    adapters = profile.get("identity", {}).get("network_adapters", [])
    if len(interfaces) != len(mac_addresses) or len(interfaces) != len(adapters):
        raise ValueError("网卡数量、MAC 与 PCIe 身份记录数量不一致")

    seen_macs: set[str] = set()
    seen_dsns: set[str] = set()
    seen_subnets: list[ipaddress.IPv4Network] = []
    seen_network_names: set[str] = set()
    seen_bridge_names: set[str] = set()
    for index, (interface, mac, adapter) in enumerate(zip(interfaces, mac_addresses, adapters)):
        normalized_mac = str(mac).lower()
        if interface.get("model_target") != "i226-v":
            raise ValueError(f"第 {index + 1} 块网卡未绑定 i226-v")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized_mac):
            raise ValueError(f"第 {index + 1} 块网卡 MAC 格式无效")
        first_octet = int(normalized_mac[:2], 16)
        if first_octet & 0x03:
            raise ValueError(f"第 {index + 1} 块网卡 MAC 不是全局唯一单播地址")
        expected_dsn = "-".join((*normalized_mac.split(":")[:3], "ff", "ff", *normalized_mac.split(":")[3:]))
        if str(adapter.get("mac", "")).lower() != normalized_mac:
            raise ValueError(f"第 {index + 1} 块网卡 MAC 记录不一致")
        if str(adapter.get("permanent_mac", "")).lower() != normalized_mac:
            raise ValueError(f"第 {index + 1} 块网卡永久 MAC 不一致")
        dsn = str(adapter.get("pcie_device_serial_number", "")).lower()
        if dsn != expected_dsn:
            raise ValueError(f"第 {index + 1} 块网卡 PCIe DSN 与 MAC 不一致")
        if normalized_mac in seen_macs or dsn in seen_dsns:
            raise ValueError("多块网卡之间出现重复 MAC 或 PCIe DSN")
        seen_macs.add(normalized_mac)
        seen_dsns.add(dsn)

        ipv4 = adapter.get("ipv4", {})
        gateway = adapter.get("gateway", {})
        managed = adapter.get("libvirt_network", {})
        try:
            subnet = ipaddress.ip_network(str(ipv4.get("subnet", "")), strict=True)
            guest_ip = ipaddress.ip_address(str(ipv4.get("address", "")))
            dhcp_start = ipaddress.ip_address(str(ipv4.get("dhcp_start", "")))
            dhcp_end = ipaddress.ip_address(str(ipv4.get("dhcp_end", "")))
            gateway_ip = ipaddress.ip_address(str(gateway.get("ipv4_address", "")))
        except ValueError as exc:
            raise ValueError(f"第 {index + 1} 块网卡的 IPv4 身份无效") from exc
        if (
            subnet.version != 4
            or subnet.prefixlen != 24
            or guest_ip not in subnet
            or dhcp_start not in subnet
            or dhcp_end not in subnet
            or not dhcp_start <= guest_ip <= dhcp_end
            or dhcp_start > dhcp_end
            or gateway_ip not in subnet
            or guest_ip in {subnet.network_address, subnet.broadcast_address, gateway_ip}
            or gateway_ip in {subnet.network_address, subnet.broadcast_address}
        ):
            raise ValueError(f"第 {index + 1} 块网卡的 IPv4/网关组合不合理")
        if ipv4.get("assignment") != "dhcp-reservation":
            raise ValueError(f"第 {index + 1} 块网卡未使用 DHCP 固定租约")
        if str(ipv4.get("netmask", "")) != str(subnet.netmask):
            raise ValueError(f"第 {index + 1} 块网卡的子网掩码不一致")
        if any(subnet.overlaps(other) for other in seen_subnets):
            raise ValueError("多块网卡之间出现重叠 IPv4 网段")
        seen_subnets.append(subnet)

        gateway_mac = str(gateway.get("mac", "")).lower()
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", gateway_mac):
            raise ValueError(f"第 {index + 1} 块网卡的网关 MAC 格式无效")
        if int(gateway_mac[:2], 16) & 0x03 or gateway_mac in seen_macs:
            raise ValueError(f"第 {index + 1} 块网卡的网关 MAC 不是独立的全局单播地址")
        seen_macs.add(gateway_mac)

        network_name = str(managed.get("name", ""))
        bridge_name = str(managed.get("bridge_name", ""))
        try:
            uuid.UUID(str(managed.get("uuid", "")))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"第 {index + 1} 块网卡的 libvirt 网络 UUID 无效") from exc
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", network_name):
            raise ValueError(f"第 {index + 1} 块网卡的 libvirt 网络名无效")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", bridge_name):
            raise ValueError(f"第 {index + 1} 块网卡的桥接名无效")
        if managed.get("forward_mode") != "nat":
            raise ValueError(f"第 {index + 1} 块网卡的网络不是 NAT 模式")
        if network_name in seen_network_names or bridge_name in seen_bridge_names:
            raise ValueError("libvirt 网络名或桥接名重复")
        if interface.get("managed_network") != network_name:
            raise ValueError(f"第 {index + 1} 块网卡的设备/网络记录不一致")
        seen_network_names.add(network_name)
        seen_bridge_names.add(bridge_name)


def validate_build_outputs(profile: dict) -> None:
    expected_id = profile["meta"]["profile_id"]
    expected_hash = hashlib.sha256((ARTIFACTS / "identity-hardware.json").read_bytes()).hexdigest()
    builds = {
        "qemu": {
            "info": BUILD / "qemu" / "build-info.json",
            "minimum_revision": 34,
            "products": ((BUILD / "qemu" / "bin" / "qemu-system-x86_64-ovo", "binary_sha256"),),
        },
        "ovmf": {
            "info": BUILD / "ovmf" / "build-info.json",
            "minimum_revision": 15,
            "products": (
                (BUILD / "ovmf" / "OVMF_CODE_4M.patched.qcow2", "code_sha256"),
                (BUILD / "ovmf" / "OVMF_VARS_4M.patched.qcow2", "vars_sha256"),
            ),
        },
    }
    for name, spec in builds.items():
        info_path = spec["info"]
        if not info_path.is_file():
            raise FileNotFoundError(f"缺少 {name} 构建信息，请重新运行对应构建脚本: {info_path}")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if info.get("profile_id") != expected_id or info.get("profile_sha256") != expected_hash:
            raise ValueError(f"构建产物与当前 identity-hardware.json 不匹配: {info_path}")
        if (
            info.get("platform_source") != profile["meta"]["platform_source"]
            or info.get("platform_source_version") != profile["meta"]["platform_source_version"]
            or info.get("platform_id") != profile["meta"]["platform_id"]
        ):
            raise ValueError(f"{name} 构建产物与当前宿主平台身份不匹配: {info_path}")
        if name == "qemu":
            expected_prefix = str(RUNTIME_ROOT / expected_id / "qemu")
            if info.get("runtime_prefix") != expected_prefix:
                raise ValueError("QEMU 构建产物仍绑定旧的工作区路径，请重新运行 02_patch_qemu.py")
        if name == "ovmf" and info.get("secure_boot") is not False:
            raise ValueError("OVMF 构建产物未明确关闭 Secure Boot，请重新运行 03_patch_ovmf.py")
        if name == "ovmf" and info.get("flash_size_bytes") != profile["ovmf_policy"]["flash_size_bytes"]:
            raise ValueError("OVMF flash size does not match the SMBIOS profile")
        if int(info.get("patch_revision", 0)) < spec["minimum_revision"]:
            raise ValueError(f"{name} 构建产物版本过旧，请重新构建")
        for product, hash_key in spec["products"]:
            if not product.is_file():
                raise FileNotFoundError(f"缺少构建产物: {product}")
            if hashlib.sha256(product.read_bytes()).hexdigest() != info.get(hash_key):
                raise ValueError(f"构建产物校验失败: {product}")
        if name == "qemu" and profile["hardware"].get("battery", {}).get("enabled"):
            battery = BUILD / "qemu/generated/battery.aml"
            if not battery.is_file():
                raise FileNotFoundError(f"缺少电池 ACPI 产物: {battery}")
            if hashlib.sha256(battery.read_bytes()).hexdigest() != info.get("battery_aml_sha256"):
                raise ValueError(f"电池 ACPI 产物校验失败: {battery}")


def deploy_runtime(profile: dict) -> Path:
    runtime = RUNTIME_ROOT / profile["meta"]["profile_id"]
    qemu_prefix = BUILD / "qemu" / "prefix"
    qemu_binary = BUILD / "qemu" / "bin" / "qemu-system-x86_64-ovo"
    code = BUILD / "ovmf" / "OVMF_CODE_4M.patched.qcow2"
    vars_file = BUILD / "ovmf" / "OVMF_VARS_4M.patched.qcow2"
    smbios = ARTIFACTS / "smbios.bin"
    if not qemu_prefix.is_dir():
        raise FileNotFoundError(f"缺少 QEMU install prefix: {qemu_prefix}")

    privilege = [] if os.geteuid() == 0 else ["sudo"]
    if privilege:
        if shutil.which("sudo") is None:
            raise RuntimeError("需要 sudo 将运行时产物部署到 /opt/ovo-spoof")
        print("需要管理员权限部署 libvirt 可读的运行时产物。")
        run_visible(["sudo", "-v"])

    for directory in (runtime / "qemu", runtime / "ovmf", runtime / "identity", runtime / "metadata"):
        run_visible(privilege + ["install", "-d", "-m", "0755", str(directory)])
    run_visible(privilege + ["cp", "-a", str(qemu_prefix) + "/.", str(runtime / "qemu")])
    run_visible(privilege + ["install", "-m", "0755", str(qemu_binary), str(runtime / "qemu/bin/qemu-system-x86_64-ovo")])
    for source, target in (
        (code, runtime / "ovmf/OVMF_CODE_4M.patched.qcow2"),
        (vars_file, runtime / "ovmf/OVMF_VARS_4M.patched.qcow2"),
        (smbios, runtime / "identity/smbios.bin"),
        (PROFILE_PATH, runtime / "metadata/identity-hardware.json"),
        (BUILD / "qemu/build-info.json", runtime / "metadata/qemu-build-info.json"),
        (BUILD / "ovmf/build-info.json", runtime / "metadata/ovmf-build-info.json"),
    ):
        run_visible(privilege + ["install", "-m", "0644", str(source), str(target)])
    battery = BUILD / "qemu/generated/battery.aml"
    if profile["hardware"].get("battery", {}).get("enabled"):
        run_visible(privilege + ["install", "-m", "0644", str(battery), str(runtime / "identity/battery.aml")])

    checks = (
        (qemu_binary, runtime / "qemu/bin/qemu-system-x86_64-ovo"),
        (code, runtime / "ovmf/OVMF_CODE_4M.patched.qcow2"),
        (vars_file, runtime / "ovmf/OVMF_VARS_4M.patched.qcow2"),
        (smbios, runtime / "identity/smbios.bin"),
    )
    deployed_checks = list(checks)
    if profile["hardware"].get("battery", {}).get("enabled"):
        deployed_checks.append((battery, runtime / "identity/battery.aml"))
    for source, target in deployed_checks:
        if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
            raise RuntimeError(f"部署后校验失败: {target}")
    return runtime


def managed_network_xml(adapter: dict) -> str:
    managed = adapter["libvirt_network"]
    ipv4 = adapter["ipv4"]
    gateway = adapter["gateway"]
    network = ET.Element("network")
    ET.SubElement(network, "name").text = managed["name"]
    ET.SubElement(network, "uuid").text = managed["uuid"]
    ET.SubElement(network, "forward", {"mode": "nat"})
    ET.SubElement(network, "bridge", {
        "name": managed["bridge_name"],
        "stp": "on",
        "delay": "0",
    })
    # This is the Ethernet identity Windows resolves for its default gateway.
    ET.SubElement(network, "mac", {"address": gateway["mac"]})
    ET.SubElement(network, "dns", {"enable": "yes", "forwardPlainNames": "yes"})
    ip_node = ET.SubElement(network, "ip", {
        "address": gateway["ipv4_address"],
        "netmask": ipv4["netmask"],
    })
    dhcp = ET.SubElement(ip_node, "dhcp")
    ET.SubElement(dhcp, "range", {
        "start": ipv4["dhcp_start"],
        "end": ipv4["dhcp_end"],
    })
    ET.SubElement(dhcp, "host", {
        "mac": adapter["mac"],
        "ip": ipv4["address"],
    })
    return ET.tostring(network, encoding="unicode")


def ensure_managed_networks(prefix: list[str], profile: dict) -> list[str]:
    adapters = profile["identity"]["network_adapters"]
    all_networks = {
        line.strip()
        for line in run(prefix + ["net-list", "--all", "--name"]).stdout.splitlines()
        if line.strip()
    }
    active_networks = {
        line.strip()
        for line in run(prefix + ["net-list", "--name"]).stdout.splitlines()
        if line.strip()
    }
    applied: list[str] = []
    for adapter in adapters:
        name = adapter["libvirt_network"]["name"]
        if name in active_networks:
            run(prefix + ["net-destroy", name])
        if name in all_networks:
            run(prefix + ["net-undefine", name])
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".xml", delete=False
            ) as temporary:
                temporary.write(managed_network_xml(adapter))
                temporary_path = Path(temporary.name)
            run(prefix + ["net-define", str(temporary_path)])
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        run(prefix + ["net-autostart", name])
        run(prefix + ["net-start", name])
        applied.append(name)
    return applied


def profile_networks_in_domain(root: ET.Element) -> set[str]:
    names: set[str] = set()
    for source in root.findall("./devices/interface/source"):
        name = source.get("network", "")
        if re.fullmatch(r"lan-[0-9a-f]{10}-[1-9][0-9]*", name):
            names.add(name)
    return names


def remove_obsolete_profile_networks(prefix: list[str], names: set[str]) -> None:
    if not names:
        return
    all_networks = {
        line.strip()
        for line in run(prefix + ["net-list", "--all", "--name"]).stdout.splitlines()
        if line.strip()
    }
    active_networks = {
        line.strip()
        for line in run(prefix + ["net-list", "--name"]).stdout.splitlines()
        if line.strip()
    }
    for name in sorted(names & all_networks):
        if name in active_networks:
            run(prefix + ["net-destroy", name])
        run(prefix + ["net-undefine", name])


def domain_state(prefix: list[str], name: str) -> str:
    return run(prefix + ["domstate", name]).stdout.strip().lower()


def replace_or_add(parent: ET.Element, tag: str, text: str) -> ET.Element:
    node = parent.find(tag)
    if node is None:
        node = ET.SubElement(parent, tag)
    node.text = text
    return node


def set_features(root: ET.Element, profile: dict) -> None:
    old = root.find("features")
    if old is not None:
        root.remove(old)
    features = ET.Element("features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    kvm = ET.SubElement(features, "kvm")
    ET.SubElement(kvm, "hidden", {"state": "on"})
    hyperv = ET.SubElement(features, "hyperv", {"mode": "custom"})
    for name in (
        "relaxed", "vapic", "spinlocks", "vpindex", "runtime", "synic",
        "stimer", "reset", "vendor_id", "frequencies", "reenlightenment",
        "tlbflush", "ipi", "evmcs", "avic",
    ):
        ET.SubElement(hyperv, name, {"state": "off"})
    ET.SubElement(features, "pmu", {"state": "on"})
    ET.SubElement(features, "vmport", {"state": "off"})
    ET.SubElement(features, "ps2", {"state": "off"})
    ET.SubElement(features, "smm", {"state": "on"})
    ET.SubElement(features, "ioapic", {"driver": "kvm"})
    ET.SubElement(features, "msrs", {"unknown": "fault"})
    insert_at = list(root).index(root.find("cpu")) if root.find("cpu") is not None else len(root)
    root.insert(insert_at, features)


def set_cpu_and_clock(root: ET.Element, profile: dict, effective_vcpus: int) -> None:
    old_cpu = root.find("cpu")
    if old_cpu is not None:
        root.remove(old_cpu)
    topology = profile["vm_baseline"]["topology"]
    cpu = ET.Element("cpu", {"mode": "host-passthrough", "check": "none", "migratable": "off"})
    sockets = int(topology["sockets"])
    dies = int(topology.get("dies", 1))
    clusters = int(topology.get("clusters", 1))
    threads = int(topology["threads"])
    topology_vcpus = sockets * dies * clusters * int(topology["cores"]) * threads
    if topology_vcpus != effective_vcpus:
        sockets, dies, clusters, threads = 1, 1, 1, 1
        cores = effective_vcpus
    else:
        cores = int(topology["cores"])
    ET.SubElement(cpu, "topology", {
        "sockets": str(sockets),
        "dies": str(dies),
        "clusters": str(clusters),
        "cores": str(cores),
        "threads": str(threads),
    })
    ET.SubElement(cpu, "cache", {"mode": "passthrough"})
    ET.SubElement(cpu, "feature", {"policy": "disable", "name": "hypervisor"})
    host_flags = set(profile["host"]["cpu"].get("flags", ()))
    for host_name, libvirt_name in (
        ("x2apic", "x2apic"),
        ("stibp", "stibp"),
        ("ssbd", "ssbd"),
    ):
        if host_name in host_flags:
            ET.SubElement(cpu, "feature", {"policy": "require", "name": libvirt_name})
    if host_flags & {"spec_ctrl", "ibrs", "ibrs_enhanced"}:
        ET.SubElement(cpu, "feature", {"policy": "require", "name": "spec-ctrl"})
    if profile["host"]["cpu"]["vendor"] == "AuthenticAMD":
        ET.SubElement(cpu, "feature", {"policy": "require", "name": "svm"})
        ET.SubElement(cpu, "feature", {"policy": "require", "name": "topoext"})
    else:
        ET.SubElement(cpu, "feature", {"policy": "require", "name": "vmx"})
    cpu_index = list(root).index(root.find("features")) + 1 if root.find("features") is not None else 0
    root.insert(cpu_index, cpu)

    old_clock = root.find("clock")
    if old_clock is not None:
        root.remove(old_clock)
    clock = ET.Element("clock", {"offset": "localtime"})
    ET.SubElement(clock, "timer", {"name": "tsc", "present": "yes", "tickpolicy": "discard", "mode": "native"})
    ET.SubElement(clock, "timer", {"name": "hpet", "present": "yes"})
    ET.SubElement(clock, "timer", {"name": "rtc", "present": "yes"})
    ET.SubElement(clock, "timer", {"name": "pit", "present": "yes"})
    ET.SubElement(clock, "timer", {"name": "kvmclock", "present": "no"})
    ET.SubElement(clock, "timer", {"name": "hypervclock", "present": "no"})
    root.insert(cpu_index + 1, clock)


def set_power_management(root: ET.Element, profile: dict) -> None:
    power = profile["hardware"]["power"]
    old = root.find("pm")
    pm = ET.Element("pm")
    ET.SubElement(pm, "suspend-to-mem", {"enabled": "yes" if power["suspend_to_mem"] else "no"})
    ET.SubElement(pm, "suspend-to-disk", {"enabled": "yes" if power["suspend_to_disk"] else "no"})
    if old is not None:
        root.remove(old)
    devices = root.find("devices")
    root.insert(list(root).index(devices) if devices is not None else len(root), pm)


def set_os(root: ET.Element, profile: dict, domain: str, runtime: Path) -> None:
    os_node = root.find("os")
    if os_node is None:
        os_node = ET.SubElement(root, "os")
    os_node.attrib.pop("firmware", None)
    automatic = os_node.find("firmware")
    if automatic is not None:
        os_node.remove(automatic)
    type_node = os_node.find("type")
    if type_node is None:
        type_node = ET.SubElement(os_node, "type")
    type_node.text = "hvm"
    type_node.set("arch", "x86_64")
    machine = profile.get("hardware", {}).get("machine", {}).get("implementation")
    if machine != "pc-q35-11.0":
        raise ValueError(f"不受支持的 QEMU machine implementation: {machine}")
    type_node.set("machine", machine)
    old_smbios = os_node.find("smbios")
    if old_smbios is not None:
        os_node.remove(old_smbios)
    firmware = root.find("os/loader")
    built_code = runtime / "ovmf" / "OVMF_CODE_4M.patched.qcow2"
    if firmware is None:
        firmware = ET.SubElement(os_node, "loader")
    firmware.text = str(built_code)
    firmware.set("readonly", "yes")
    firmware.set("secure", "no")
    firmware.set("type", "pflash")
    firmware.set("format", "qcow2")
    nvram = root.find("os/nvram")
    built_vars = runtime / "ovmf" / "OVMF_VARS_4M.patched.qcow2"
    if nvram is None:
        nvram = ET.SubElement(os_node, "nvram")
    safe_domain = re.sub(r"[^A-Za-z0-9_.-]", "_", domain)
    nvram_name = f"{safe_domain}_{profile['identity']['nvram_id'][:8]}_VARS.qcow2"
    nvram.text = str(Path("/var/lib/libvirt/qemu/nvram") / nvram_name)
    nvram.attrib.clear()
    nvram.set("template", str(built_vars))
    nvram.set("templateFormat", "qcow2")
    nvram.set("format", "qcow2")
    bootmenu = os_node.find("bootmenu")
    if bootmenu is None:
        bootmenu = ET.SubElement(os_node, "bootmenu")
    bootmenu.set("enable", "yes")


def set_emulator(root: ET.Element, runtime: Path) -> None:
    devices = root.find("devices")
    if devices is None:
        devices = ET.SubElement(root, "devices")
    emulator = devices.find("emulator")
    if emulator is None:
        emulator = ET.Element("emulator")
        devices.insert(0, emulator)
    emulator.text = str(runtime / "qemu/bin/qemu-system-x86_64-ovo")


def pci_bus_number(node: ET.Element) -> int | None:
    address = node.find("address")
    if address is None or address.get("type") != "pci" or not address.get("bus"):
        return None
    try:
        return int(address.get("bus", ""), 0)
    except ValueError:
        return None


def normalize_pcie_root_ports(devices: ET.Element) -> None:
    used_buses = {
        bus
        for node in devices
        if node.tag != "controller"
        for bus in (pci_bus_number(node),)
        if bus is not None
    }
    # Libvirt requires controller indexes to be contiguous.  First discard an
    # empty conventional bridge so it cannot force libvirt to recreate an
    # otherwise unused filler Root Port after define.
    for controller in list(devices.findall("controller")):
        if controller.get("type") != "pci" or controller.get("model") not in {
            "pcie-to-pci-bridge", "pci-bridge",
        }:
            continue
        try:
            index = int(controller.get("index", "0"), 0)
        except ValueError as exc:
            raise ValueError("PCI bridge controller index 无效") from exc
        if index not in used_buses:
            devices.remove(controller)
    non_root_port_indexes: list[int] = []
    for controller in devices.findall("controller"):
        if controller.get("type") != "pci" or controller.get("model") in {"pcie-root", "pcie-root-port"}:
            continue
        try:
            non_root_port_indexes.append(int(controller.get("index", "0"), 0))
        except ValueError as exc:
            raise ValueError("PCI controller index 无效") from exc
    filler_limit = max(non_root_port_indexes, default=0)
    for controller in list(devices.findall("controller")):
        if controller.get("type") != "pci" or controller.get("model") != "pcie-root-port":
            continue
        try:
            index = int(controller.get("index", "0"), 0)
        except ValueError:
            index = 0
        if index >= 5 and index not in used_buses and index >= filler_limit:
            devices.remove(controller)
        else:
            # Keep libvirt's controller model while selecting QEMU's real
            # Intel IOH backend through the nested model element.  These ports
            # represent fixed internal devices, so do not let Windows treat
            # their children (notably the xHCI controller) as ejectable PCIe
            # hardware.
            controller.set("model", "pcie-root-port")
            model = controller.find("model")
            if model is None:
                model = ET.SubElement(controller, "model")
            model.set("name", "ioh3420")
            target = controller.find("target")
            if target is None:
                target = ET.SubElement(controller, "target")
            target.set("hotplug", "off")


def validate_profiled_root_ports(devices: ET.Element, profile: dict) -> None:
    profiled = {
        int(item["guest_target_port"])
        for item in profile["devices"]["pci_identities"]["root_ports"]
    }
    actual: set[int] = set()
    for controller in devices.findall("controller"):
        if controller.get("type") != "pci" or controller.get("model") != "pcie-root-port":
            continue
        target = controller.find("target")
        try:
            actual.add(int(target.get("port", ""), 0) if target is not None else -1)
        except ValueError as exc:
            raise ValueError("PCIe Root Port target port 无效") from exc
    missing = sorted(actual - profiled)
    if missing:
        values = ", ".join(f"0x{port:02x}" for port in missing)
        raise ValueError(f"最终 XML 包含未建档的 PCIe Root Port: {values}")


def normalize_onboard_hda(devices: ET.Element, profile: dict) -> None:
    """Drop libvirt <sound> so Q35's reserved 1f.3 SMBus slot is not collided.

    The actual ich9-intel-hda device is attached later via qemu:commandline at
    the host-observed BDF.  Spice/none audio backends stay as <audio> nodes.
    """
    audio = profile["hardware"]["audio"]
    wanted = audio["pci_address"]
    wanted_bus = int(wanted["bus"], 0)
    wanted_slot = int(wanted["slot"], 0)
    wanted_function = int(wanted["function"], 0)
    sounds = devices.findall("sound")
    if len(sounds) > 1:
        raise ValueError("当前 XML 存在多块虚拟声卡，请先只保留板载 ich9-hda")
    if sounds and sounds[0].get("model") not in {None, "ich9"}:
        raise ValueError(f"当前声卡型号是 {sounds[0].get('model')!r}，板载 HDA 后端只承载 ich9")
    for node in list(devices):
        if node.tag == "sound":
            continue
        address = node.find("address")
        if address is None or address.get("type") != "pci":
            continue
        try:
            occupied = (
                int(address.get("bus", "0"), 0),
                int(address.get("slot", "0"), 0),
                int(address.get("function", "0"), 0),
            )
        except ValueError:
            continue
        if occupied == (wanted_bus, wanted_slot, wanted_function):
            raise ValueError(
                f"PCI 00:{wanted_slot:02x}.{wanted_function} 已被其他设备占用，无法放置板载 HDA"
            )
    for sound in sounds:
        devices.remove(sound)


def pci_hostdev_bdfs(devices: ET.Element) -> list[str]:
    found: list[str] = []
    for hostdev in devices.findall("hostdev"):
        if hostdev.get("type") != "pci":
            continue
        address = hostdev.find("source/address")
        if address is None:
            continue
        try:
            found.append(
                f"{int(address.get('domain', '0'), 0):04x}:"
                f"{int(address.get('bus', '0'), 0):02x}:"
                f"{int(address.get('slot', '0'), 0):02x}."
                f"{int(address.get('function', '0'), 0)}"
            )
        except ValueError:
            continue
    return found


def hostdev_is_usb_controller(bdf: str) -> bool:
    path = Path(f"/sys/bus/pci/devices/{bdf}/class")
    if not path.is_file():
        return False
    try:
        class_code = int(path.read_text(encoding="utf-8").strip(), 16)
    except ValueError:
        return False
    return (class_code >> 8) & 0xFFFF == 0x0C03


def disable_emulated_usb_controller(devices: ET.Element) -> None:
    """Keep libvirt from auto-inserting qemu-xhci when it is not required."""
    for node in list(devices.findall("controller")):
        if node.get("type") == "usb":
            devices.remove(node)
    controller = ET.SubElement(devices, "controller")
    controller.set("type", "usb")
    controller.set("index", "0")
    controller.set("model", "none")


def strip_emulated_usb_hid(devices: ET.Element) -> None:
    """Drop QEMU usb-kbd/usb-mouse/tablet/ps2.  Keep user USB/PCI hostdevs."""
    for node in list(devices.findall("input")):
        bus = node.get("bus")
        kind = node.get("type")
        if bus in {"usb", "ps2"} or kind in {"tablet", "mouse", "keyboard"}:
            devices.remove(node)


def has_usb_device_hostdev(devices: ET.Element) -> bool:
    return any(node.get("type") == "usb" for node in devices.findall("hostdev"))


def normalize_usb_controller(devices: ET.Element, profile: dict) -> None:
    pci_xhci = any(hostdev_is_usb_controller(bdf) for bdf in pci_hostdev_bdfs(devices))
    if pci_xhci or not has_usb_device_hostdev(devices):
        disable_emulated_usb_controller(devices)
        return

    usb = profile["hardware"]["usb_controller"]
    controllers = devices.findall("controller[@type='usb']")
    controller = controllers[0] if controllers else ET.SubElement(devices, "controller")
    for extra in controllers[1:]:
        devices.remove(extra)

    wanted_address = usb["pci_address"]
    wanted_bus = int(wanted_address["bus"], 0)
    wanted_slot = int(wanted_address["slot"], 0)
    wanted_function = int(wanted_address["function"], 0)
    for node in devices:
        if node is controller:
            continue
        address = node.find("address")
        if address is None or address.get("type") != "pci":
            continue
        try:
            occupied = (
                int(address.get("bus", "0"), 0),
                int(address.get("slot", "0"), 0),
                int(address.get("function", "0"), 0),
            )
        except ValueError:
            continue
        if occupied == (wanted_bus, wanted_slot, wanted_function):
            raise ValueError(
                "xHCI 目标 PCI 地址已被其他设备占用: "
                f"{wanted_bus:02x}:{wanted_slot:02x}.{wanted_function}"
            )

    controller.attrib.clear()
    controller.set("type", "usb")
    controller.set("index", "0")
    controller.set("model", usb["qemu_model"])
    for child in list(controller):
        controller.remove(child)
    ET.SubElement(controller, "address", {
        "type": "pci",
        "domain": wanted_address["domain"],
        "bus": wanted_address["bus"],
        "slot": wanted_address["slot"],
        "function": wanted_address["function"],
    })


def set_devices(root: ET.Element, profile: dict) -> None:
    devices = root.find("devices")
    if devices is None:
        devices = ET.SubElement(root, "devices")
    for node in list(devices):
        if node.tag in {"serial", "parallel", "redirdev", "rng", "panic", "filesystem", "vsock", "shmem", "crypto", "iommu"}:
            devices.remove(node)
            continue
        if node.tag == "console":
            target = node.find("target")
            if target is None or target.get("type") in {None, "serial", "virtio"}:
                devices.remove(node)
                continue
        if node.tag == "channel":
            target = node.find("target")
            if target is None or target.get("type") == "virtio" or "spice" in target.get("name", ""):
                devices.remove(node)
                continue
        if node.tag == "controller" and node.get("type") == "virtio-serial":
            devices.remove(node)
            continue
        if node.tag == "input" and (node.get("bus") == "ps2" or node.get("type") == "tablet"):
            devices.remove(node)

    strip_emulated_usb_hid(devices)

    balloon = devices.find("memballoon")
    if balloon is None:
        balloon = ET.SubElement(devices, "memballoon")
    balloon.attrib.clear()
    balloon.set("model", "none")
    for child in list(balloon):
        balloon.remove(child)

    normalize_usb_controller(devices, profile)
    normalize_onboard_hda(devices, profile)
    normalize_pcie_root_ports(devices)

    if profile.get("devices", {}).get("gpu_passthrough"):
        for node in devices.findall("video") + devices.findall("graphics"):
            devices.remove(node)
    else:
        for video in devices.findall("video"):
            model = video.find("model")
            if model is None:
                model = ET.SubElement(video, "model")
            if model.get("type") in {None, "qxl", "virtio", "bochs"}:
                model.attrib.clear()
                model.set("type", "vga")

    storage = profile["storage"]["devices"]
    disks = [node for node in root.findall("./devices/disk") if node.get("device") in {"disk", "cdrom", "floppy"}]
    configured = {
        (
            source["device"],
            source["bus"],
            source.get("target", {}).get("dev", ""),
        ): source
        for source in storage
    }
    matched: set[tuple[str, str, str]] = set()
    for node in disks:
        target = node.find("target")
        current_bus = (target.get("bus") if target is not None else "") or "unknown"
        current_dev = (target.get("dev") if target is not None else "") or ""
        device = node.get("device", "disk")
        key = (device, current_bus, current_dev)
        source = configured.get(key)
        if source is None:
            if device in {"cdrom", "floppy"}:
                continue
            raise ValueError("当前 XML 的持久存储布局已改变，请重新运行 01_generate_identity.py")
        matched.add(key)
        identity = source["identity"]
        if source.get("serial"):
            replace_or_add(node, "serial", source["serial"])
        # libvirt only accepts <wwn> for IDE and SCSI disks. SATA, VirtIO,
        # and NVMe identities use serial/vendor/product without a WWN node.
        if source.get("wwn") and current_bus in {"ide", "scsi"}:
            replace_or_add(node, "wwn", source["wwn"])
        else:
            old_wwn = node.find("wwn")
            if old_wwn is not None:
                node.remove(old_wwn)
        # libvirt exposes vendor/product only on SCSI disks.  Keeping these
        # elements on SATA (the current VM layout) makes `virsh define`
        # reject the complete domain XML.
        if current_bus == "scsi":
            replace_or_add(node, "vendor", identity["vendor"][:8])
            replace_or_add(node, "product", identity["product"][:40])
        else:
            for tag in ("vendor", "product"):
                old_identity = node.find(tag)
                if old_identity is not None:
                    node.remove(old_identity)
    missing_disks = [
        key for key, source in configured.items()
        if source["device"] == "disk" and key not in matched
    ]
    if missing_disks:
        raise ValueError("当前 XML 缺少身份文件中的持久磁盘，请重新运行 01_generate_identity.py")
    interfaces = root.findall("./devices/interface")
    configured = profile.get("devices", {}).get("interfaces", [])
    if len(interfaces) != len(configured):
        raise ValueError("当前 XML 的网卡数量已改变，请重新运行 01_generate_identity.py")
    for index, interface in enumerate(interfaces):
        addresses = profile["identity"].get("mac_addresses", [])
        adapters = profile["identity"].get("network_adapters", [])
        if index < len(addresses):
            mac = interface.find("mac")
            if mac is None:
                mac = ET.SubElement(interface, "mac")
            mac.set("address", addresses[index])
        if index < len(configured):
            interface.set("type", "network")
            source = interface.find("source")
            if source is None:
                source = ET.SubElement(interface, "source")
            source.attrib.clear()
            source.set("network", adapters[index]["libvirt_network"]["name"])
            for tag in ("target", "virtualport", "vlan", "backend"):
                old = interface.find(tag)
                if old is not None:
                    interface.remove(old)
            model = interface.find("model")
            if model is None:
                model = ET.SubElement(interface, "model")
            model.set("type", configured[index].get("model_target") or "i226-v")
            # libvirt only honors interface driver queues for selected backends
            # such as virtio.  I226-V exposes its descriptor queues in-device;
            # adding queues here is silently discarded and would misstate the
            # actual single physical-link/TAP topology.
            driver = interface.find("driver")
            if driver is not None:
                interface.remove(driver)

    current_hostdevs: list[str] = []
    for hostdev in root.findall("./devices/hostdev[@type='pci']"):
        address = hostdev.find("source/address")
        if address is not None:
            try:
                current_hostdevs.append(
                    f"{int(address.get('domain', '0'), 0):04x}:"
                    f"{int(address.get('bus', '0'), 0):02x}:"
                    f"{int(address.get('slot', '0'), 0):02x}."
                    f"{int(address.get('function', '0'), 0):x}"
                )
            except ValueError:
                current_hostdevs.append("invalid")
    expected_hostdevs = sorted(
        item.get("address", "")
        for item in profile.get("devices", {}).get("hostdevs", [])
        if item.get("type") == "pci"
    )
    current_set = set(current_hostdevs)
    expected_set = set(expected_hostdevs)
    missing = expected_set - current_set
    blocking_missing = {bdf for bdf in missing if not hostdev_is_usb_controller(bdf)}
    if blocking_missing:
        raise ValueError("当前 XML 缺少身份文件中的 PCI 直通设备，请重新运行 01_generate_identity.py")
    usb_passed = [bdf for bdf in current_hostdevs if hostdev_is_usb_controller(bdf)]
    extra = current_set - expected_set
    for bdf in extra:
        same_slot = any(bdf.rsplit(".", 1)[0] == usb.rsplit(".", 1)[0] for usb in usb_passed)
        if not (hostdev_is_usb_controller(bdf) or same_slot):
            raise ValueError("当前 XML 的 PCI 直通设备已改变，请重新运行 01_generate_identity.py")


def set_qemu_commandline(
    root: ET.Element, profile: dict, runtime: Path, *, validate_runtime: bool = True,
) -> None:
    tag = f"{{{QEMU_NS}}}commandline"
    arg_tag = f"{{{QEMU_NS}}}arg"
    commandline = root.find(tag)
    if commandline is None:
        commandline = ET.SubElement(root, tag)
    children = list(commandline)
    kept: list[ET.Element] = []
    index = 0
    while index < len(children):
        node = children[index]
        value = node.get("value", "") if node.tag == arg_tag else ""
        generated_acpi = value == "-acpitable"
        generated_hda = value == "-device" and index + 1 < len(children) and children[index + 1].get("value", "").startswith(("ich9-intel-hda,id=sound0", "hda-duplex,id=sound0-codec0", "hda-output,id=sound0-codec0"))
        if value == "-smbios" or generated_acpi or generated_hda:
            index += 2
            continue
        kept.append(node)
        index += 1
    for node in children:
        commandline.remove(node)
    for node in kept:
        commandline.append(node)
    ET.SubElement(commandline, arg_tag, {"value": "-smbios"})
    ET.SubElement(commandline, arg_tag, {"value": f"full-file={runtime / 'identity/smbios.bin'}"})

    battery_enabled = bool(profile["hardware"].get("battery", {}).get("enabled"))
    battery_aml = runtime / "identity" / "battery.aml"
    if battery_enabled:
        if validate_runtime and not battery_aml.is_file():
            raise FileNotFoundError("该 profile 是移动/电池平台，但 QEMU 构建缺少 battery.aml")
        ET.SubElement(commandline, arg_tag, {"value": "-acpitable"})
        ET.SubElement(commandline, arg_tag, {"value": f"file={battery_aml}"})
    attach_onboard_hda_commandline(commandline, root, profile, arg_tag)


def attach_onboard_hda_commandline(commandline: ET.Element, root: ET.Element, profile: dict, arg_tag: str) -> None:
    audio = profile["hardware"]["audio"]
    address = audio["pci_address"]
    bus = int(address["bus"], 0)
    slot = int(address["slot"], 0)
    function = int(address["function"], 0)
    if bus != 0:
        raise ValueError("板载 HDA 必须在 PCI 总线 0")
    codec = {"duplex": "hda-duplex", "output": "hda-output"}.get(audio.get("libvirt_codec"))
    if not codec:
        raise ValueError("板载 HDA codec 类型无效")
    cad = int(audio.get("codec_cad") or 0)
    audio_id = None
    devices = root.find("devices")
    if devices is not None:
        backend = devices.find("audio")
        if backend is not None and backend.get("id"):
            audio_id = backend.get("id")
    ET.SubElement(commandline, arg_tag, {"value": "-device"})
    ET.SubElement(commandline, arg_tag, {"value": f"ich9-intel-hda,id=sound0,addr=0x{slot:02x}.{function}"})
    codec_value = f"{codec},id=sound0-codec0,bus=sound0.0,cad={cad}"
    if audio_id is not None:
        codec_value += f",audiodev=audio{audio_id}"
    ET.SubElement(commandline, arg_tag, {"value": "-device"})
    ET.SubElement(commandline, arg_tag, {"value": codec_value})


def apply_profile(
    root: ET.Element, profile: dict, domain: str, runtime: Path,
    *, validate_runtime: bool = True,
) -> None:
    devices_before = root.find("devices")
    audio_before = [ET.tostring(node, encoding="unicode") for node in (
        ([] if devices_before is None else devices_before.findall("audio"))
    )]
    for sysinfo in root.findall("sysinfo"):
        root.remove(sysinfo)
    for genid in root.findall("genid"):
        root.remove(genid)
    validate_hardware_baseline(root, profile)
    available = available_host_cpus()
    effective_vcpus = fit_vcpu_count(root, available)
    vcpu_cpus, emulator_cpus = configured_cpu_bindings(root, available)
    set_features(root, profile)
    set_cpu_and_clock(root, profile, effective_vcpus)
    set_power_management(root, profile)
    set_vcpu_pinning(root, vcpu_cpus, emulator_cpus)
    set_os(root, profile, domain, runtime)
    set_emulator(root, runtime)
    set_devices(root, profile)
    devices = root.find("devices")
    if devices is None:
        raise RuntimeError("域 XML 缺少 devices")
    validate_profiled_root_ports(devices, profile)
    set_qemu_commandline(root, profile, runtime, validate_runtime=validate_runtime)
    devices_after = root.find("devices")
    audio_after = [ET.tostring(node, encoding="unicode") for node in (
        ([] if devices_after is None else devices_after.findall("audio"))
    )]
    if audio_after != audio_before:
        raise RuntimeError("内部错误：应用过程改变了用户的 audio 后端 XML")


def main() -> int:
    try:
        parser = argparse.ArgumentParser(description="Apply one spoof_v2 profile to its libvirt domain")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="validate and print the domain XML diff without deploying or defining anything",
        )
        args = parser.parse_args()
        prefix = virsh_prefix()
        domain = select_domain(prefix)
        profile, _ = load_artifacts()
        validate_build_outputs(profile)
        source_domain = profile["source"]["domain"]
        if source_domain != domain:
            raise ValueError(f"身份文件属于 {source_domain}，当前选择的是 {domain}。请先重新运行 01_generate_identity.py。")
        xml_text = run(prefix + ["dumpxml", domain]).stdout
        root = ET.fromstring(xml_text)
        runtime = RUNTIME_ROOT / profile["meta"]["profile_id"]
        if args.dry_run:
            apply_profile(root, profile, domain, runtime, validate_runtime=False)
            result = ET.tostring(root, encoding="unicode")
            diff = difflib.unified_diff(
                xml_text.splitlines(), result.splitlines(),
                fromfile=f"{domain}.current.xml", tofile=f"{domain}.spoof-v2.xml", lineterm="",
            )
            print("\n".join(diff) or "XML 无变化")
            return 0
        state = domain_state(prefix, domain)
        if "shut" not in state and "关闭" not in state:
            raise RuntimeError("虚拟机必须先关机，再应用 spoof 配置")
        runtime = deploy_runtime(profile)
        BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS / f"{domain}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xml"
        backup.write_text(xml_text, encoding="utf-8")
        old_profile_networks = profile_networks_in_domain(root)
        apply_profile(root, profile, domain, runtime)
        networks = ensure_managed_networks(prefix, profile)
        result = ET.tostring(root, encoding="unicode")
        temporary = backup.with_suffix(".new.xml")
        try:
            temporary.write_text(result, encoding="utf-8")
            run(prefix + ["define", str(temporary)])
            remove_obsolete_profile_networks(
                prefix, old_profile_networks - set(networks)
            )
        finally:
            # A failed `virsh define` must not leave a misleading deployable
            # XML beside the immutable backup.
            temporary.unlink(missing_ok=True)
        print(f"已应用到虚拟机：{domain}")
        print(f"原 XML 备份：{backup}")
        print(f"运行时目录：{runtime}")
        print(f"当前存储总线：{', '.join(item['bus'] for item in profile['storage']['devices']) or 'none'}")
        for index, adapter in enumerate(profile["identity"]["network_adapters"], 1):
            print(
                f"网络{index}：MAC {adapter['mac']}，"
                f"IP {adapter['ipv4']['address']}/{adapter['ipv4']['prefix_length']} "
                f"via {adapter['gateway']['ipv4_address']} "
                f"({adapter['gateway']['mac']})"
            )
        print(f"已启用身份专属 NAT 网络：{', '.join(networks)}")
        print("SMBIOS UUID、MAC 和磁盘 serial 已从 identity-hardware.json 注入；libvirt 域 UUID 保持不变。")
        return 0
    except (OSError, ValueError, RuntimeError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"应用失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
