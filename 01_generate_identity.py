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
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

MIB = 1024**2
GIB = 1024**3
SCHEMA_VERSION = 32
MEMORY_LAYOUT = "q35-fixed-sequential-v1"
ARTIFACT_CONTRACT_VERSION = 1


def q35_ranges(total: int) -> list[list[int]]:
    if total < 4 * GIB or total % (4 * GIB):
        raise ValueError("Guest 内存必须至少 4 GiB 且为 4 GiB 的整数倍")
    if total > 512 * GIB:
        raise ValueError("当前固定 Q35 内存映射仅支持不超过 512 GiB")
    return [[0, 2 * GIB - 1], [4 * GIB, total + 2 * GIB - 1]]


def xml_memory_bytes(root: ET.Element) -> int:
    units = {"b": 1, "bytes": 1, "kb": 1000, "kib": 1024,
             "mb": 1000**2, "mib": MIB, "gb": 1000**3, "gib": GIB}

    def read_size(node: ET.Element | None) -> int:
        if node is None or not (node.text or "").strip():
            raise ValueError("XML 缺少明确的内存容量")
        unit = node.get("unit", "KiB").lower()
        if unit not in units:
            raise ValueError(f"不支持的内存单位: {unit}")
        return int(node.text.strip(), 0) * units[unit]

    total = read_size(root.find("memory"))
    q35_ranges(total)
    current = root.find("currentMemory")
    if current is not None and read_size(current) != total:
        raise ValueError("固定内存模型要求 currentMemory 与 memory 相等")
    if any(root.find(path) is not None for path in ("maxMemory", "./devices/memory", "./cpu/numa", "./devices/controller[@model='pcie-expander-bus']")):
        raise ValueError("当前内存模型不支持热插拔内存、自定义 NUMA 或扩展 PCIe 总线布局")
    namespace = "{http://libvirt.org/schemas/domain/qemu/1.0}"
    arguments = [node.get("value", "") for node in root.findall(f"./{namespace}commandline/{namespace}arg")]
    forbidden = {"-m", "-M", "-machine", "-numa", "-readconfig", "-set", "-incoming"}
    fragments = ("max-ram-below-4g", "sgx-epc", "memory-backend", "pc-dimm", "nvdimm", "virtio-mem", "cxl", "pci-hole64-size")
    if any(value.split("=", 1)[0] in forbidden or any(fragment in value.lower() for fragment in fragments) for value in arguments):
        raise ValueError("QEMU 自定义参数可能改变固定内存映射，当前不支持")
    if root.find(f"./{namespace}override") is not None:
        raise ValueError("固定内存模型暂不接受 QEMU 属性 override")
    if root.find("./cpu/maxphysaddr") is not None:
        raise ValueError("固定内存模型暂不接受自定义 maxphysaddr")
    return total


def validate_array(sizes: list[int], array: dict[str, Any]) -> None:
    maximum = int(array.get("maximum_capacity_bytes", 0))
    slots = int(array.get("device_slots", 0))
    if not 1 <= slots <= 16 or maximum <= 0 or maximum % 1024 or maximum > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("平台内存最大容量或槽位数无效（当前支持 1 到 16 个槽位）")
    if not sizes or any(size < 4096 or size % 4096 for size in sizes):
        raise ValueError("虚拟 DIMM 必须至少 4 GiB 且为 4 GiB 的整数倍")
    if len(sizes) > slots or sum(sizes) * MIB > maximum:
        raise ValueError("Guest DIMM 数或 XML 总容量超过平台 Type 16 能力")


def choose_layout(total: int, catalog: dict, array: dict) -> list[int]:
    q35_ranges(total)
    validate_array([total // MIB], array)
    limit = min(int(array["device_slots"]), total // (4 * GIB))
    candidates: set[tuple[int, ...]] = set()
    vendors = {vendor for entries in catalog.values() for vendor, part in entries}
    for vendor in vendors:
        available = sorted(size for size, entries in catalog.items()
                           if size >= 4096 and size % 4096 == 0 and any(name == vendor for name, part in entries))
        states = {(0, 0): ()}
        for count in range(limit):
            for (used, installed), layout in list(states.items()):
                if installed != count:
                    continue
                for size in available:
                    amount = used + size
                    if amount > total // MIB:
                        continue
                    proposal = tuple(sorted((*layout, size), reverse=True))
                    key = (amount, count + 1)
                    previous = states.get(key)
                    if previous is None or (proposal.count(4096), tuple(-value for value in proposal)) < (previous.count(4096), tuple(-value for value in previous)):
                        states[key] = proposal
        candidates.update(layout for (used, count), layout in states.items() if used == total // MIB)
        half = total // MIB // 2
        if limit >= 2 and half in available:
            candidates.add((half, half))
    if not candidates:
        raise ValueError("配件池无法在平台槽位数内用同厂商模块组成 XML 容量")
    chosen = min(candidates, key=lambda values: (
        values.count(4096), 0 if len(values) == 2 and values[0] == values[1] else 1,
        len(values), tuple(-value for value in values),
    ))
    return list(chosen)


def guest_slots(sizes: list[int], templates: list[dict], array: dict) -> list[dict]:
    validate_array(sizes, array)
    if len(templates) != int(array["device_slots"]):
        raise ValueError("平台 Type 16 槽位数与槽位模板数量不一致")
    result = []
    for index, template in enumerate(templates):
        locator = str(template.get("device_locator") or "")
        if not locator:
            raise ValueError("平台内存槽位缺少 device_locator")
        result.append({
            "device_locator": locator,
            "bank_locator": str(template.get("bank_locator") or f"BANK_{index}"),
            "form_factor": int(template["form_factor"]),
            "installed": index < len(sizes),
            "size_mib": sizes[index] if index < len(sizes) else 0,
        })
    return result


def address_fields(start: int, end: int) -> tuple[int, int, int, int]:
    if start < 0 or end < start or start % 1024 or (end + 1) % 1024 or end > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("SMBIOS 内存地址无效或没有按 KiB 对齐")
    if end // 1024 < 0xFFFFFFFF:
        return start // 1024, end // 1024, 0, 0
    return 0xFFFFFFFF, 0xFFFFFFFF, start, end


def device_ranges(sizes: list[int], ranges: list[list[int]]) -> list[tuple[int, int, int, int]]:
    result = []
    device_offset = 0
    for index, size in enumerate(sizes):
        device_end = device_offset + size * MIB
        range_offset = 0
        for mapped, (start, end) in enumerate(ranges):
            length = end - start + 1
            first, last = max(device_offset, range_offset), min(device_end, range_offset + length)
            if first < last:
                result.append((index, mapped, start + first - range_offset, start + last - range_offset - 1))
            range_offset += length
        device_offset = device_end
    return result


def structure(kind: int, handle: int, body: bytes, strings: list[str] | None = None) -> bytes:
    values = strings or []
    if any(not value or "\0" in value for value in values):
        raise ValueError("SMBIOS 内存字符串不能为空或包含 NUL")
    tail = b"\0".join(value.encode("ascii", "strict") for value in values) + b"\0\0"
    return struct.pack("<BBH", kind, len(body) + 4, handle) + body + tail


def memory_tables(profile: dict, platform: dict, identity: dict) -> list[bytes]:
    sizes = [int(value) for value in profile["dimm_sizes_mb"]]
    array = profile["memory_array"]
    validate_array(sizes, array)
    ranges = q35_ranges(sum(sizes) * MIB)
    slots = guest_slots(sizes, profile["memory_slots"], array)
    for values in (profile["dimm_parts"], profile["dimm_serials"], identity["dimm_assets"]):
        if len(values) != len(sizes):
            raise ValueError("内存身份数量与已安装 DIMM 数不一致")
    maximum = int(array["maximum_capacity_bytes"])
    field = maximum // 1024 if maximum < 0x80000000 * 1024 else 0x80000000
    tables = [structure(16, 0x1000, struct.pack(
        "<BBBIHHQ", int(array["location"]), int(array["use"]), int(array["error_correction"]),
        field, 0xFFFE, len(slots), maximum if field == 0x80000000 else 0,
    ))]
    for index, slot in enumerate(slots):
        installed, size = slot["installed"], slot["size_mib"]
        size_field = size if size < 0x7FFF else 0x7FFF
        speed = int(profile["memory_speed"]) if installed else 0
        rated = int(profile["memory_rated_speed"]) if installed else 0
        if not 0 <= rated <= 0xFFFFFFFF or not 0 <= speed <= 0xFFFFFFFF:
            raise ValueError("Memory speed is outside the SMBIOS range")
        voltages = [int(profile[key]) if installed else 0 for key in
                    ("memory_min_voltage", "memory_max_voltage", "memory_configured_voltage")]
        if installed:
            if rated != speed:
                raise ValueError("Rated memory speed must match configured speed")
            if not all(800 <= value <= 2000 for value in voltages):
                raise ValueError("DIMM voltage is outside the SMBIOS millivolt range")
        elif rated != 0 or any(value != 0 for value in voltages):
            raise ValueError("Empty DIMM slots must not advertise electrical values")
        mfg_id = memory_jedec_id(profile["memory_vendor"]) if installed else 0
        body = struct.pack(
            "<5H5B2H5BI4H", 0x1000, 0xFFFE, 64 if installed else 0xFFFF,
            64 if installed else 0xFFFF, size_field, slot["form_factor"], 0, 1, 2,
            int(platform["memory_type_code"]) if installed else 0x02, 0x80 if installed else 0,
            rated, 3 if installed else 0, 4 if installed else 0, 5 if installed else 0,
            6 if installed else 0, 0, size if size_field == 0x7FFF else 0, min(speed, 0xFFFF), *voltages,
        )
        body += struct.pack(
            "<BHB4H4Q2I", 3 if installed else 2, 8 if installed else 4,
            0, mfg_id, 0, 0, 0, 0, size * MIB, 0, 0, 0,
            speed if speed >= 0xFFFF else 0,
        )
        strings = [slot["device_locator"], slot["bank_locator"]]
        if installed:
            strings.extend([profile["memory_vendor"], profile["dimm_serials"][index], identity["dimm_assets"][index], profile["dimm_parts"][index]])
        tables.append(structure(17, 0x1100 + index, body, strings))
    for index, (start, end) in enumerate(ranges):
        first, last, extended_first, extended_last = address_fields(start, end)
        tables.append(structure(19, 0x1300 + index, struct.pack("<IIHBQQ", first, last, 0x1000, 1, extended_first, extended_last)))
    for index, (device, mapped, start, end) in enumerate(device_ranges(sizes, ranges)):
        first, last, extended_first, extended_last = address_fields(start, end)
        tables.append(structure(20, 0x1400 + index, struct.pack(
            "<IIHHBBBQQ", first, last, 0x1100 + device, 0x1300 + mapped, 1, 0, 0, extended_first, extended_last,
        )))
    return tables


def validate_stream(data: bytes, profile: dict, platform: dict, identity: dict) -> None:
    actual = []
    handles = set()
    offset = 0
    terminated = False
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("SMBIOS 表头截断")
        kind, length, handle = struct.unpack_from("<BBH", data, offset)
        if length < 4 or offset + length > len(data) or handle in handles:
            raise ValueError("SMBIOS 结构长度或 handle 无效")
        handles.add(handle)
        end = data.find(b"\0\0", offset + length)
        if end < 0:
            raise ValueError("SMBIOS 字符串区没有终止符")
        end += 2
        if kind in {16, 17, 19, 20}:
            actual.append(data[offset:end])
        if kind == 127:
            if end != len(data):
                raise ValueError("SMBIOS Type 127 后存在多余数据")
            terminated = True
        offset = end
    if not terminated or actual != memory_tables(profile, platform, identity):
        raise ValueError("SMBIOS 内存表与平台能力、DIMM、Q35 地址或身份不一致")


def validate_memory_record(record: dict, data: bytes | None = None) -> None:
    try:
        if record["meta"]["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"身份文件需要 schema {SCHEMA_VERSION}，请重新运行 01 并重建 02/03")
        profile = record["smbios_profile"]
        memory = record["hardware"]["memory"]
        host_memory = record["host"]["smbios_non_unique"]["memory"]
        sizes = profile["dimm_sizes_mb"]
        array = profile["memory_array"]
        validate_array(sizes, array)
        total = record["vm_baseline"]["memory_bytes"]
        if sum(sizes) * MIB != total or memory["sizes_mib"] != sizes:
            raise ValueError("Guest 安装容量与 XML/profile 不一致")
        if array != host_memory["array"] or memory["array"] != array:
            raise ValueError("Type 16 能力未保持宿主平台来源")
        expected_slots = guest_slots(sizes, host_memory["slots"], array)
        if profile["memory_slots"] != expected_slots or memory["slots"] != expected_slots:
            raise ValueError("Guest 安装/空槽状态与平台槽位不一致")
        if memory["layout"] != MEMORY_LAYOUT or memory["mapped_ranges"] != q35_ranges(total):
            raise ValueError("Guest 内存映射策略不一致")
        if record["hardware"]["machine"]["implementation"] != "pc-q35-11.0":
            raise ValueError("内存映射仅适用于 pc-q35-11.0")
        if profile["dimm_count"] != len(sizes) or memory["parts"] != profile["dimm_parts"]:
            raise ValueError("Guest 内存数量或料号不一致")
        validate_profile(profile)
        topology = record["vm_baseline"]["topology"]
        cores = topology["dies"] * topology["clusters"] * topology["cores"]
        if (profile["sockets"], profile["cores"], profile["threads"]) != (
            topology["sockets"], cores, cores * topology["threads"],
        ) or topology["vcpus"] != topology["sockets"] * cores * topology["threads"]:
            raise ValueError("SMBIOS CPU counts differ from XML baseline")
        if record["qemu_policy"]["smbios_entry_point"] != ENTRY_POINT:
            raise ValueError("SMBIOS entry point must declare the supported 3.5 layout")
        if record["ovmf_policy"].get("flash_size_bytes") != FIRMWARE_SIZE:
            raise ValueError("OVMF flash size is not bound to the SMBIOS contract")
        if memory["type"] != profile["memory_type"] or memory["speed_mt"] != profile["memory_speed"]:
            raise ValueError("Memory type/speed mirrors disagree")
        if memory["vendor"] != profile["memory_vendor"] or record["identity"]["dimm_serials"] != profile["dimm_serials"]:
            raise ValueError("Memory identity mirrors disagree")
        if data is not None:
            validate_stream(data, profile, record["hardware"]["platform"], record["identity"])
            if data != build_smbios_stream(profile, record["hardware"]["platform"], record["identity"]):
                raise ValueError("Complete SMBIOS stream differs from the declared profile")
    except (KeyError, TypeError, OverflowError, struct.error) as error:
        raise ValueError("内存身份结构不完整或无效，请重新运行 01") from error


FIRMWARE_SIZE = 4 * 1024**2
ENTRY_POINT = {"major": 3, "minor": 5, "docrev": 0}
END_OF_TABLE_HANDLE = 0xFEFF


def guest_slot_body(body: bytes) -> bytes:
    if len(body) < 13:
        raise ValueError("Type 9 requires segment, bus and device/function fields")
    result = bytearray(body[:15])
    result[3] = 2
    result[9:13] = b"\xff\xff\xff\xff"
    if len(result) >= 15:
        result[14] = 0
    return bytes(result)


def memory_jedec_id(vendor: str) -> int:
    """SMBIOS Type 17 Module Manufacturer ID: LSB = continuation count, MSB = JEP106 ID."""
    name = vendor.lower()
    mapping = (
        ("samsung", 0xCE00),
        ("hynix", 0xAD00),
        ("micron", 0x2C00),
        ("crucial", 0x2C00),
        ("kingston", 0x9801),
        ("corsair", 0x9E01),
        ("adata", 0xCB01),
    )
    for token, code in mapping:
        if token in name:
            return code
    return 0


def dimm_jedec_voltage_mv(memory_type: str) -> int:
    if "ddr4" in memory_type:
        return 1200
    return 1100


def smbios_cpu_voltage_byte(raw: int) -> int:
    if raw & 0x80 and 5 <= (raw & 0x7F) <= 20:
        return raw
    return 0x8C


def configure_guest_profile(profile: dict) -> dict:
    result = deepcopy(profile)
    speed = int(result["memory_speed"])
    millivolts = dimm_jedec_voltage_mv(str(result.get("memory_type") or ""))
    host_clock = int(result.get("cpu_external_clock_mhz") or 0)
    result.update(
        schema=SCHEMA_VERSION, cache_policy="unknown-until-guest-observed",
        cache_l1_kib=None, cache_l2_kib=None, cache_l3_kib=None,
        memory_rated_speed=speed,
        memory_min_voltage=millivolts,
        memory_max_voltage=millivolts,
        memory_configured_voltage=millivolts,
        cpu_voltage=smbios_cpu_voltage_byte(int(result.get("cpu_voltage") or 0)),
        cpu_external_clock_mhz=host_clock if 10 <= host_clock <= 400 else 100,
        cpu_signature=0, cpu_features_edx=0,
        cpu_id_policy="qemu-runtime-cpuid-leaf1",
    )
    characteristics = 0x04
    if result["cores"] > 1:
        characteristics |= 0x08
    if result["threads"] > result["cores"]:
        characteristics |= 0x10
    result["cpu_characteristics"] = characteristics
    return result


def validate_profile(profile: dict) -> None:
    if profile.get("schema") != SCHEMA_VERSION or profile.get("cache_policy") != "unknown-until-guest-observed":
        raise ValueError(
            f"SMBIOS profile requires schema {SCHEMA_VERSION} and explicit guest cache policy")
    sockets = profile["sockets"]
    cores = profile["cores"]
    enabled = profile["enabled_cores"]
    threads = profile["threads"]
    if not all(type(value) is int and 1 <= value <= 65534 for value in (sockets, cores, enabled, threads)):
        raise ValueError("Invalid SMBIOS CPU counts")
    if sockets > 256 or enabled != cores or threads < enabled or threads % cores:
        raise ValueError("Unsupported CPU socket/core/thread topology")
    if any(profile.get(key) is not None for key in ("cache_l1_kib", "cache_l2_kib", "cache_l3_kib")):
        raise ValueError("Host aggregate caches must not be presented as observed guest caches")
    expected = 4 | (8 if cores > 1 else 0) | (16 if threads > cores else 0)
    if profile["cpu_characteristics"] != expected:
        raise ValueError("Processor characteristics do not match guest topology")
    if not (10 <= int(profile["cpu_external_clock_mhz"]) <= 400):
        raise ValueError("CPU external clock is outside the SMBIOS field range")
    voltage = int(profile["cpu_voltage"])
    if not (voltage & 0x80) or not (5 <= (voltage & 0x7F) <= 20):
        raise ValueError("CPU voltage must use the current-voltage SMBIOS encoding")
    speed = int(profile["memory_speed"])
    if int(profile["memory_rated_speed"]) != speed:
        raise ValueError("Rated memory speed must match configured speed")
    for key in ("memory_min_voltage", "memory_max_voltage", "memory_configured_voltage"):
        if not 800 <= int(profile[key]) <= 2000:
            raise ValueError("DIMM voltage is outside the SMBIOS millivolt range")
    if profile.get("cpu_id_policy") != "qemu-runtime-cpuid-leaf1":
        raise ValueError("Processor ID requires QEMU runtime CPUID finalization")
    if profile["cpu_signature"] != 0 or profile["cpu_features_edx"] != 0:
        raise ValueError("Processor ID template must reserve eight zero bytes for QEMU")
    for key in ("cpu_max_mhz", "cpu_current_mhz"):
        if not 0 < profile[key] <= 65535:
            raise ValueError("CPU speed is outside the SMBIOS field range")
    datetime.strptime(profile["bios_date"], "%m/%d/%Y")


def validate_guest_stream(record: dict, data: bytes, cpuid: tuple[int, int]) -> None:
    if (len(cpuid) != 2 or not all(type(value) is int and 0 <= value <= 0xFFFFFFFF for value in cpuid)
            or cpuid[0] == 0):
        raise ValueError("Independent guest CPUID(1).EAX/EDX evidence is required")
    expected = bytearray(build_smbios_stream(
        record["smbios_profile"], record["hardware"]["platform"], record["identity"],
    ))
    offset = 0
    while offset < len(expected):
        kind, length, handle = struct.unpack_from("<BBH", expected, offset)
        if kind == 4:
            struct.pack_into("<II", expected, offset + 8, *cpuid)
        offset = expected.index(b"\0\0", offset + length) + 2
    validate_tables(data, record["smbios_profile"])
    if data != bytes(expected):
        raise ValueError("Guest SMBIOS differs outside the verified runtime CPUID fields")


def parse_tables(data: bytes) -> list[tuple[int, int, bytes, list[bytes]]]:
    tables = []
    handles = set()
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("Truncated SMBIOS header")
        kind, length, handle = struct.unpack_from("<BBH", data, offset)
        if length < 4 or offset + length > len(data) or handle in handles or handle >= 0xFF00:
            raise ValueError("Invalid SMBIOS length or handle")
        end = data.find(b"\0\0", offset + length)
        if end < 0:
            raise ValueError("Unterminated SMBIOS string area")
        area = data[offset + length:end]
        strings = area.split(b"\0") if area else []
        if any(not value or any(character < 32 or character > 126 for character in value) for value in strings):
            raise ValueError("Invalid SMBIOS printable ASCII string")
        handles.add(handle)
        tables.append((kind, handle, data[offset:offset + length], strings))
        offset = end + 2
        if kind == 127 and (
                length != 4 or handle != END_OF_TABLE_HANDLE or strings or offset != len(data)):
            raise ValueError("Invalid end-of-table structure")
    if not tables or tables[-1][0] != 127:
        raise ValueError("Missing end-of-table structure")
    return tables


def validate_tables(data: bytes, profile: dict) -> None:
    tables = parse_tables(data)
    minimum = {0:26, 1:27, 2:15, 3:22, 4:48, 8:9, 9:17, 11:5,
               12:5, 13:22, 16:23, 17:92, 19:31, 20:35, 22:26, 32:11, 127:4}
    indices = {0:(4,5,8), 1:(4,5,6,7,25,26), 2:(4,5,6,7,8,10),
               3:(4,6,7,8), 4:(4,7,16,32,33,34), 8:(4,6), 9:(4,),
               13:(21,), 17:(16,17,23,24,25,26,43), 22:(4,5,6,7,8,14,20)}
    references = {2:((11,3),), 4:((26,7),(28,7),(30,7)),
                  17:((4,16),), 19:((12,16),), 20:((12,17),(14,19))}
    handles = {handle:kind for kind,handle,body,strings in tables}
    counts = Counter(kind for kind,handle,body,strings in tables)
    for kind in (0,1,2,3,16,32,127):
        if counts[kind] != 1:
            raise ValueError(f"Expected exactly one Type {kind}")
    if counts[4] != profile["sockets"] or counts[17] != profile["memory_array"]["device_slots"]:
        raise ValueError("CPU or DIMM structure count differs from profile")
    if counts[22] != int(bool(profile.get("battery", {}).get("enabled"))):
        raise ValueError("Battery table differs from guest battery policy")
    for kind,handle,body,strings in tables:
        if kind not in minimum or len(body) < minimum[kind]:
            raise ValueError(f"Unsupported or truncated Type {kind}")
        if kind != 9 and len(body) != minimum[kind]:
            raise ValueError(f"Unexpected Type {kind} layout")
        for offset in indices.get(kind, ()):
            if body[offset] > len(strings):
                raise ValueError(f"Type {kind} string index is out of range")
        for offset,target in references.get(kind, ()):
            reference = struct.unpack_from("<H", body, offset)[0]
            if kind == 4 and reference == 0xFFFF:
                continue
            if handles.get(reference) != target:
                raise ValueError(f"Type {kind} references a missing Type {target}")
        if kind in (11,12,13) and body[4] != len(strings):
            raise ValueError(f"Type {kind} string count mismatch")
        if kind == 13 and (body[5] & 0xFE or any(body[6:21])):
            raise ValueError("Type 13 reserved fields must be zero")
        if kind == 2 and (body[9] & 0xE0 or body[14]):
            raise ValueError("Unsupported baseboard flags or contained objects")
        if kind == 3:
            if body[19] or body[20] or body[21] > len(strings):
                raise ValueError("Unsupported chassis contained elements or SKU")
        if kind == 4:
            if body[24] & 0xB8 or body[24] & 7 != 1:
                raise ValueError("Invalid populated/enabled processor status")
            small = tuple(body[35:38])
            wide = struct.unpack_from("<HHH", body, 42)
            expected = (profile["cores"], profile["enabled_cores"], profile["threads"])
            if wide != expected or small != tuple(min(value,255) for value in expected):
                raise ValueError("Processor counts differ from profile")
        if kind == 9 and (body[7] != 2 or body[13:17] != b"\xff\xff\xff\xff"):
            raise ValueError("Host PCI usage/BDF must not leak into guest slot inventory")
        if kind == 9 and (len(body) not in (17,18,19) or len(body) == 19 and body[18]):
            raise ValueError("Unsupported slot peer layout")
        if kind == 32 and any(body[4:]):
            raise ValueError("Invalid reserved bytes or boot status")


def smbios_uuid(value: str) -> bytes:
    raw = uuid.UUID(value).bytes
    return raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:]


def smbios_structure(kind: int, handle: int, body: bytes, strings: Iterable[str] = ()) -> bytes:
    values = [str(item) for item in strings]
    if any(not item or "\0" in item for item in values):
        raise ValueError("SMBIOS strings cannot be empty or contain NUL")
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


def validate_smbios_memory_topology(data: bytes, profile: dict, platform: dict, identity: dict) -> None:
    validate_stream(data, profile, platform, identity)


def build_smbios_stream(profile: dict, platform: dict, identity: dict) -> bytes:
    """Build a complete SMBIOS structure stream owned by spoof_v2."""
    validate_profile(profile)
    out: list[bytes] = []
    vendor = profile["vendor"]
    body = struct.pack(
        "<BBHBBQ2B4BH",
        1, 2, 0, 3, FIRMWARE_SIZE // 65536 - 1, 0x08, 0x01, 0x18,
        int(platform["bios_major_release"]), int(platform["bios_minor_release"]),
        0xFF, 0xFF,
        0,
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
        struct.pack_into("<HHH", body, 22, 0xFFFF, 0xFFFF, 0xFFFF)
        body[28:31] = bytes((4, 5, 6))
        body[31:34] = bytes((min(profile["cores"], 255), min(profile["enabled_cores"], 255), min(profile["threads"], 255)))
        struct.pack_into("<HHHH", body, 34, profile["cpu_characteristics"], family2_code, profile["cores"], profile["enabled_cores"])
        struct.pack_into("<H", body, 42, profile["threads"])
        out.append(smbios_structure(4, 0x0400 + socket_index, bytes(body), [f"{profile['cpu_socket']} {socket_index + 1}", profile["cpu_vendor"], profile["cpu_version"], profile["cpu_serial"], identity["cpu_asset"], profile["cpu_part"]]))

    for index, item in enumerate(profile.get("connectors") or []):
        try:
            body = bytes.fromhex(str(item["body"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("SMBIOS Type 8 记录损坏") from exc
        out.append(smbios_structure(8, 0x0800 + index, body, item.get("strings") or ()))
    for index, item in enumerate(profile.get("slots") or []):
        try:
            body = guest_slot_body(bytes.fromhex(str(item["body"])))
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
        if kind in {26, 28, 29}:
            continue
        if kind not in {12, 13}:
            raise RuntimeError(f"不允许复制的 SMBIOS 补充表: Type {kind}")
        out.append(smbios_structure(kind, 0x3000 + index, body, item.get("strings") or ()))

    out.extend(memory_tables(profile, platform, identity))

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
    out.append(smbios_structure(127, END_OF_TABLE_HANDLE, b""))
    data = b"".join(out)
    if not data.endswith(struct.pack("<BBH", 127, 4, END_OF_TABLE_HANDLE) + b"\0\0"):
        raise ValueError("SMBIOS stream has no valid Type 127 terminator")
    validate_smbios_memory_topology(data, profile, platform, identity)
    validate_tables(data, profile)
    return data


def identity_payload_bytes(record: dict) -> bytes:
    payload = {key: value for key, value in record.items() if key != "artifacts"}
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode()


def build_artifact_contract(record: dict, smbios: bytes) -> dict:
    profile = record["smbios_profile"]
    return {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "identity_payload_sha256": hashlib.sha256(identity_payload_bytes(record)).hexdigest(),
        "smbios": {
            "path": "smbios.bin",
            "sha256": hashlib.sha256(smbios).hexdigest(),
            "size_bytes": len(smbios),
            "entry_point": dict(ENTRY_POINT),
            "end_of_table_handle": END_OF_TABLE_HANDLE,
            "cpu_id_policy": profile["cpu_id_policy"],
        },
    }


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
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
ROUTER_HOSTNAME_PREFIX = {
    "TP-Link Technologies": "tplink",
    "ASUSTek Computer": "asus",
    "NETGEAR": "netgear",
    "Xiaomi Communications": "miwifi",
    "Huawei Technologies": "huawei",
}

MEMORY_CATALOG: dict[tuple[str, str], dict[int, tuple[tuple[str, str], ...]]] = {
    ("ddr5", "laptop"): {
        4096: (("Samsung", "M425R512GB4-CQK"),),
        8192: (
            ("Samsung", "M425R1GB4BB0-CQK"), ("Samsung", "M425R1GB4PB0-CWM"),
            ("Micron", "MTC4C10163S1SC48BA1"), ("SK hynix", "HMCG66MEBSA095N"),
            ("Crucial", "CT8G48C40S5"), ("Kingston", "KF548S38IB-8"),
            ("Corsair", "CMSX8GX5M1A4800C40"),
        ),
        16384: (
            ("Samsung", "M425R2GA3BB0-CQK"), ("Samsung", "M425R2GA3PB0-CWM"),
            ("Micron", "MTC8C1084S1SC48BA1"), ("SK hynix", "HMCG78MEBSA095N"),
            ("Crucial", "CT16G48C40S5"), ("Kingston", "KF548S38IB-16"),
            ("Corsair", "CMSX16GX5M1A4800C40"),
        ),
        32768: (
            ("Samsung", "M425R4GA3BB0-CQK"), ("SK hynix", "HMCG88MEBSA092N"),
            ("Micron", "MTC16C2084S1SC48BA1"), ("Crucial", "CT32G48C40S5"),
            ("Kingston", "KF548S38IB-32"), ("Corsair", "CMSX32GX5M1A4800C40"),
        ),
    },
    ("ddr5", "desktop"): {
        4096: (("Samsung", "M323R512GB4-CQK"),),
        8192: (
            ("Samsung", "M323R1GB4BB0-CQK"), ("Crucial", "CT8G48C40U5"),
            ("Kingston", "KF548C38BB-8"), ("Corsair", "CMK8GX5M1B5200C40"),
            ("ADATA", "AD5U48008G-B"),
        ),
        16384: (
            ("Samsung", "M323R2GA3BB0-CQK"), ("Crucial", "CT16G48C40U5"),
            ("Kingston", "KF552C40BB-16"), ("Corsair", "CMK16GX5M1B5200C40"),
            ("ADATA", "AD5U480016G-B"),
        ),
        32768: (
            ("Samsung", "M323R4GA3BB0-CQK"), ("Crucial", "CT32G48C40U5"),
            ("Kingston", "KF560C36BBE-32"), ("Corsair", "CMK32GX5M1B5200C40"),
            ("ADATA", "AD5U480032G-B"),
        ),
        65536: (
            ("Crucial", "CT64G48C40U5"), ("Kingston", "KF560C32RSA-64"),
            ("Corsair", "CMK64GX5M1B5200C40"),
        ),
    },
    ("ddr4", "laptop"): {
        4096: (
            ("Samsung", "M471A5244CB0-CWE"), ("SK hynix", "HMA851S6CJR6N-XN"),
            ("Kingston", "KVR32S22S6/4"),
        ),
        8192: (
            ("Samsung", "M471A1K43EB1-CWE"), ("Crucial", "CT8G4SFRA32A"),
            ("SK hynix", "HMA81GS6DJR8N-XN"), ("Kingston", "KVR32S22S8/8"),
            ("Micron", "MTA8ATF1G64HZ-3G2E1"),
        ),
        16384: (
            ("Samsung", "M471A2K43EB1-CWE"), ("Crucial", "CT16G4SFRA32A"),
            ("SK hynix", "HMA82GS6DJR8N-XN"), ("Kingston", "KVR32S22D8/16"),
            ("Micron", "MTA16ATF2G64HZ-3G2E1"),
        ),
        32768: (
            ("Samsung", "M471A4G43AB1-CWE"), ("Crucial", "CT32G4SFD832A"),
            ("Kingston", "KVR32S22D8/32"), ("SK hynix", "HMAA4GS6CJR8N-XN"),
        ),
    },
    ("ddr4", "desktop"): {
        4096: (("Samsung", "M378A5244CB0-CWE"), ("Kingston", "KVR32N22S6/4")),
        8192: (
            ("Samsung", "M378A1K43EB1-CWE"), ("Crucial", "CT8G4DFRA32A"),
            ("Kingston", "KVR32N22S8/8"), ("ADATA", "AD4U32008G22-SGN"),
        ),
        16384: (
            ("Samsung", "M378A2K43EB1-CWE"), ("Crucial", "CT16G4DFRA32A"),
            ("Kingston", "KVR32N22D8/16"), ("ADATA", "AD4U320016G22-SGN"),
        ),
        32768: (
            ("Samsung", "M378A4G43AB2-CWE"), ("Crucial", "CT32G4DFD832A"),
            ("Kingston", "KVR32N22D8/32"), ("ADATA", "AD4U320032G22-SGN"),
        ),
    },
}


def _sata_ssd(
    product: str, model: str, firmware: str, style: str, length: int, pattern: str, oui: str,
) -> dict[str, Any]:
    return {
        "vendor": "ATA", "product": product, "model": model, "firmware": firmware,
        "serial_style": style, "serial_length": length, "serial_pattern": pattern,
        "wwn_oui": oui, "interface": "sata", "media_type": "ssd",
        "rotation_rate": 1, "trim": True,
    }


STORAGE_CATALOG: dict[str, tuple[dict[str, Any], ...]] = {
    "sata": (
        _sata_ssd("Samsung SSD 870 EVO", "Samsung SSD 870 EVO", "SVT02B6Q", "samsung", 15, r"S[A-Z]{2}[A-Z0-9]{12}", "002538"),
        _sata_ssd("Samsung SSD 870 QVO", "Samsung SSD 870 QVO", "SVQ02B6Q", "samsung", 15, r"S[A-Z]{2}[A-Z0-9]{12}", "002538"),
        _sata_ssd("Samsung SSD 860 EVO", "Samsung SSD 860 EVO", "RVT04B6Q", "samsung", 15, r"S[A-Z]{2}[A-Z0-9]{12}", "002538"),
        _sata_ssd("Crucial MX500", "Crucial MX500", "M3CR046", "crucial", 15, r"[0-9]{2}[A-Z0-9]{13}", "00a075"),
        _sata_ssd("Crucial BX500", "Crucial BX500", "M6CR056", "crucial", 15, r"[0-9]{2}[A-Z0-9]{13}", "00a075"),
        _sata_ssd("KINGSTON A400", "KINGSTON SA400S37", "SBFK71E0", "kingston", 16, r"[A-Z0-9]{16}", "0026b7"),
        _sata_ssd("KINGSTON KC600", "KINGSTON SKC600", "S4500105", "kingston", 16, r"[A-Z0-9]{16}", "0026b7"),
        _sata_ssd("SanDisk SSD PLUS", "SanDisk SSD PLUS", "UH5100RL", "sandisk", 16, r"[0-9]{4}[A-Z0-9]{12}", "001b44"),
        _sata_ssd("WD Blue SA510", "WD Blue SA510 2.5", "520041WD", "wd", 16, r"[A-Z0-9]{16}", "0014ee"),
        _sata_ssd("WD Green SATA", "WDC WDS SATA", "415000WD", "wd", 16, r"[A-Z0-9]{16}", "0014ee"),
        _sata_ssd("INTEL 545s", "INTEL SSDSC2KW", "LHF002C", "intel", 16, r"[A-Z0-9]{16}", "001de0"),
        _sata_ssd("ADATA SU800", "ADATA SU800", "Q0125A", "adata", 16, r"[A-Z0-9]{16}", "0022b0"),
        _sata_ssd("Seagate BarraCuda SSD", "Seagate BarraCuda SSD", "ST40011P", "seagate", 16, r"[A-Z0-9]{16}", "000c50"),
        _sata_ssd("Transcend SSD230S", "Transcend SSD230S", "R0815B", "transcend", 16, r"[A-Z0-9]{16}", "00d0b0"),
    ),
    "nvme": (
        {"model": "Samsung SSD 980", "firmware": "2B4QFXO7"},
        {"model": "Samsung SSD 990 EVO", "firmware": "0B2QFXO7"},
        {"model": "WD_BLACK SN770", "firmware": "731100WD"},
        {"model": "Crucial P3", "firmware": "P9CR30A"},
        {"model": "KINGSTON SNV2S", "firmware": "SBM02103"},
        {"model": "SK hynix PCIe SSD", "firmware": "HPS1A30Q"},
    ),
    "cdrom": (
        {"vendor": "HL-DT-ST", "product": "DVDRAM GUD1N", "model": "HL-DT-ST DVDRAM GUD1N", "firmware": "1.00"},
        {"vendor": "HL-DT-ST", "product": "DVDRAM GUE1N", "model": "HL-DT-ST DVDRAM GUE1N", "firmware": "1.00"},
        {"vendor": "HL-DT-ST", "product": "DVDRAM GU90N", "model": "HL-DT-ST DVDRAM GU90N", "firmware": "1.02"},
        {"vendor": "PLDS", "product": "DVD+-RW DU-8A5LH", "model": "PLDS DVD+-RW DU-8A5LH", "firmware": "6D1M"},
        {"vendor": "PLDS", "product": "DVD+-RW DU-8A5SH", "model": "PLDS DVD+-RW DU-8A5SH", "firmware": "6D1S"},
        {"vendor": "MATSHITA", "product": "DVD-RAM UJ8E2", "model": "MATSHITA DVD-RAM UJ8E2", "firmware": "1.00"},
        {"vendor": "MATSHITA", "product": "DVD-RAM UJ8G2", "model": "MATSHITA DVD-RAM UJ8G2", "firmware": "1.00"},
        {"vendor": "TSSTcorp", "product": "CDDVDW SU-208GB", "model": "TSSTcorp CDDVDW SU-208GB", "firmware": "D300"},
        {"vendor": "TSSTcorp", "product": "CDDVDW SN-208BB", "model": "TSSTcorp CDDVDW SN-208BB", "firmware": "D300"},
        {"vendor": "ASUS", "product": "SDRW-08U7M-U", "model": "ASUS SDRW-08U7M-U", "firmware": "B101"},
        {"vendor": "PIONEER", "product": "DVD-RW DVR-XD10", "model": "PIONEER DVD-RW DVR-XD10", "firmware": "1.00"},
        {"vendor": "ATAPI", "product": "iHAS124   Y", "model": "ATAPI   iHAS124   Y", "firmware": "AL0M"},
    ),
}

HID_CATALOG = (
    {
        "manufacturer": "Logitech", "mouse_product": "USB Optical Mouse", "mouse_vendor_id": "046d", "mouse_product_id": "c077",
        "keyboard_product": "USB Keyboard", "keyboard_vendor_id": "046d", "keyboard_product_id": "c31c",
    },
    {
        "manufacturer": "Logitech", "mouse_product": "USB Receiver Mouse", "mouse_vendor_id": "046d", "mouse_product_id": "c05a",
        "keyboard_product": "Keyboard K120", "keyboard_vendor_id": "046d", "keyboard_product_id": "c31c",
    },
    {
        "manufacturer": "Dell", "mouse_product": "Dell MS116 USB Optical Mouse", "mouse_vendor_id": "413c", "mouse_product_id": "301a",
        "keyboard_product": "Dell KB216 Wired Keyboard", "keyboard_vendor_id": "413c", "keyboard_product_id": "2113",
    },
    {
        "manufacturer": "Dell", "mouse_product": "Dell MS116t Optical Mouse", "mouse_vendor_id": "413c", "mouse_product_id": "3012",
        "keyboard_product": "Dell KB212-B Keyboard", "keyboard_vendor_id": "413c", "keyboard_product_id": "2003",
    },
    {
        "manufacturer": "Lenovo", "mouse_product": "Lenovo USB Optical Mouse", "mouse_vendor_id": "17ef", "mouse_product_id": "608d",
        "keyboard_product": "Lenovo Traditional USB Keyboard", "keyboard_vendor_id": "17ef", "keyboard_product_id": "6099",
    },
    {
        "manufacturer": "Lenovo", "mouse_product": "Lenovo Optical Mouse", "mouse_vendor_id": "17ef", "mouse_product_id": "6044",
        "keyboard_product": "Lenovo USB Keyboard", "keyboard_vendor_id": "17ef", "keyboard_product_id": "6047",
    },
    {
        "manufacturer": "Microsoft", "mouse_product": "Microsoft Wheel Mouse Optical", "mouse_vendor_id": "045e", "mouse_product_id": "0040",
        "keyboard_product": "Microsoft Wired Keyboard 600", "keyboard_vendor_id": "045e", "keyboard_product_id": "0752",
    },
    {
        "manufacturer": "HP", "mouse_product": "HP USB Optical Mouse", "mouse_vendor_id": "03f0", "mouse_product_id": "094a",
        "keyboard_product": "HP USB Keyboard", "keyboard_vendor_id": "03f0", "keyboard_product_id": "0024",
    },
    {
        "manufacturer": "ASUS", "mouse_product": "ASUS Optical Mouse", "mouse_vendor_id": "0b05", "mouse_product_id": "18f0",
        "keyboard_product": "ASUS Keyboard", "keyboard_vendor_id": "0b05", "keyboard_product_id": "17c0",
    },
    {
        "manufacturer": "Cherry", "mouse_product": "CHERRY USB Mouse", "mouse_vendor_id": "046a", "mouse_product_id": "b090",
        "keyboard_product": "CHERRY Wired Keyboard", "keyboard_vendor_id": "046a", "keyboard_product_id": "0001",
    },
)


def _validate_accessory_catalogs() -> None:
    for item in STORAGE_CATALOG["sata"]:
        re.compile(item["serial_pattern"])
        if not re.fullmatch(r"[0-9a-f]{6}", str(item["wwn_oui"])):
            raise ValueError(f"SATA 配件 WWN OUI 无效: {item['model']}")
        if not 12 <= int(item["serial_length"]) <= 20:
            raise ValueError(f"SATA 配件序列号长度无效: {item['model']}")
        if len(item["firmware"]) > 8 or len(item["model"]) > 40:
            raise ValueError(f"SATA 配件 ATA 字符串超长: {item['model']}")
    for item in STORAGE_CATALOG["cdrom"]:
        if len(item["vendor"]) > 8 or len(item["product"]) > 16 or len(item["firmware"]) > 8 or len(item["model"]) > 40:
            raise ValueError(f"光驱配件 ATA 字符串超长: {item['model']}")
    for item in STORAGE_CATALOG["nvme"]:
        if len(item["model"]) > 40 or len(item["firmware"]) > 8:
            raise ValueError(f"NVMe 潜伏型号字符串超长: {item['model']}")
    for kit in HID_CATALOG:
        for field in ("mouse_vendor_id", "mouse_product_id", "keyboard_vendor_id", "keyboard_product_id"):
            if not re.fullmatch(r"[0-9a-f]{4}", kit[field]):
                raise ValueError(f"HID 配件 PCI/USB ID 无效: {kit['manufacturer']} {field}")


_validate_accessory_catalogs()


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
    return xml_memory_bytes(root)


def cpu_topology(root: ET.Element) -> dict[str, int]:
    vcpu_node = root.find("vcpu")
    vcpu = int((vcpu_node.text or "1").strip(), 0) if vcpu_node is not None else 1
    if not 1 <= vcpu <= 65534:
        raise ValueError("Invalid fixed vCPU count")
    if vcpu_node is not None and int(vcpu_node.get("current", str(vcpu)), 0) != vcpu:
        raise ValueError("Fixed CPU model requires current vCPUs to equal maximum vCPUs")
    if root.find("vcpus") is not None:
        raise ValueError("Per-vCPU hotplug state is not supported by the fixed SMBIOS model")
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
    if any(value <= 0 for value in values.values()):
        raise ValueError("CPU topology dimensions must be positive")
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
    memory_array_handle: int | None = None
    memory_device_arrays: list[int] = []
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
        elif kind == 16:
            if memory_array is not None:
                raise RuntimeError("当前内存模型仅支持单个物理内存阵列")
            if len(formatted) < 15:
                raise RuntimeError("宿主 SMBIOS Type 16 长度不足")
            memory_array_handle = struct.unpack_from("<H", formatted, 2)[0]
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
            memory_device_arrays.append(struct.unpack_from("<H", formatted, 4)[0])
            size_field = struct.unpack_from("<H", formatted, 12)[0]
            if size_field == 0xFFFF:
                raise RuntimeError("宿主 Type 17 安装容量未知，不能视作空槽")
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
    if memory_array["use"] != 3 or any(handle != memory_array_handle for handle in memory_device_arrays):
        raise RuntimeError("宿主 Type 17 未关联到唯一的系统内存阵列")
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


ROOT_PORT_ROLE_ORDER = ("gpu", "nvme", "nic", "usb", "audio", "other", "empty")


def pci_class_role(class_code: int) -> str:
    if (class_code >> 16) & 0xFF == 0x03:
        return "gpu"
    if (class_code >> 8) & 0xFFFF == 0x0108:
        return "nvme"
    if (class_code >> 16) & 0xFF == 0x02:
        return "nic"
    if (class_code >> 8) & 0xFFFF == 0x0c03:
        return "usb"
    if (class_code >> 8) & 0xFFFF == 0x0403:
        return "audio"
    return "other"


def preferred_root_port_role(roles: list[str]) -> str:
    if not roles:
        return "empty"
    return min(roles, key=lambda role: ROOT_PORT_ROLE_ORDER.index(role) if role in ROOT_PORT_ROLE_ORDER else len(ROOT_PORT_ROLE_ORDER))


def host_root_port_children(device: Path) -> list[dict[str, str]]:
    children: list[dict[str, str]] = []
    for child in sorted(path for path in device.iterdir() if path.is_dir() and path.name.count(":") == 2):
        children.append({
            "bdf": child.name,
            "class_code": f"{read_int(child / 'class'):06x}",
            "vendor_id": f"{read_int(child / 'vendor'):04x}",
            "device_id": f"{read_int(child / 'device'):04x}",
        })
    return children


def classify_hostdev_role(node: ET.Element) -> str:
    if node.get("type") != "pci":
        return "usb" if node.get("type") == "usb" else "other"
    source = node.find("source/address")
    if source is None:
        return "other"
    try:
        bdf = (
            f"{int(source.get('domain', '0'), 0):04x}:"
            f"{int(source.get('bus', '0'), 0):02x}:"
            f"{int(source.get('slot', '0'), 0):02x}."
            f"{int(source.get('function', '0'), 0)}"
        )
    except ValueError:
        return "other"
    class_code = read_int(Path("/sys/bus/pci/devices") / bdf / "class")
    if class_code <= 0:
        return "other"
    return pci_class_role(class_code)


def classify_guest_endpoint(node: ET.Element) -> str | None:
    if node.tag == "controller":
        model = node.get("model")
        kind = node.get("type")
        if kind == "pci" or model in {"pcie-root", "pcie-root-port", "pcie-to-pci-bridge", "pci-bridge"}:
            return None
        if kind == "usb":
            return "usb"
        return "other"
    if node.tag == "interface":
        return "nic"
    if node.tag == "video":
        return "gpu"
    if node.tag == "sound":
        return "audio"
    if node.tag == "hostdev":
        return classify_hostdev_role(node)
    if node.tag == "disk":
        target = node.find("target")
        bus = (target.get("bus") if target is not None else "") or ""
        if bus == "nvme" or node.get("type") == "nvme":
            return "nvme"
        return "other"
    address = node.find("address")
    if address is None or address.get("type") != "pci":
        return None
    return "other"


def guest_device_bus(node: ET.Element) -> int | None:
    address = node.find("address")
    if address is None or address.get("type") != "pci" or not address.get("bus"):
        return None
    try:
        return int(address.get("bus", ""), 0)
    except ValueError as exc:
        raise RuntimeError("Guest 设备 PCI bus 地址无效") from exc


def list_kept_guest_root_ports(devices: ET.Element | None) -> list[dict[str, Any]]:
    if devices is None:
        return []
    used_buses: set[int] = set()
    for node in devices:
        if node.tag == "controller" and node.get("type") == "pci":
            continue
        bus = guest_device_bus(node)
        if bus is not None:
            used_buses.add(bus)
    non_root_port_indexes: list[int] = []
    for controller in devices.findall("controller"):
        if controller.get("type") != "pci" or controller.get("model") in {"pcie-root", "pcie-root-port"}:
            continue
        try:
            non_root_port_indexes.append(int(controller.get("index", "0"), 0))
        except ValueError as exc:
            raise RuntimeError("Guest PCI controller index 无效") from exc
    filler_limit = max(non_root_port_indexes, default=0)
    ports: list[dict[str, Any]] = []
    seen: set[int] = set()
    for controller in devices.findall("controller"):
        if controller.get("type") != "pci" or controller.get("model") != "pcie-root-port":
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
        if port in seen:
            raise RuntimeError(f"Guest PCIe Root Port target port 重复: {port:#x}")
        seen.add(port)
        roles = [
            role
            for node in devices
            if guest_device_bus(node) == index
            for role in (classify_guest_endpoint(node),)
            if role
        ]
        ports.append({"index": index, "port": port, "role": preferred_root_port_role(roles)})
    return ports


def host_root_port_rank(guest_role: str, host: dict[str, Any]) -> tuple[Any, ...]:
    host_role = str(host.get("host_role") or "other")
    width = int(host["max_link_width"])
    bdf = str(host["bdf"])
    if guest_role == "gpu":
        if host_role == "gpu":
            bucket = 0
        elif width >= 8:
            bucket = 1
        elif width >= 4:
            bucket = 2
        else:
            bucket = 9
        return (bucket, -width, bdf)
    if guest_role == "nvme":
        if host_role == "nvme":
            bucket = 0
        elif width >= 4:
            bucket = 1
        else:
            bucket = 9
        return (bucket, abs(width - 4), bdf)
    if guest_role == "nic":
        if host_role == "nic":
            bucket = 0
        elif host_role == "empty" and width <= 1:
            bucket = 1
        elif width <= 4:
            bucket = 2
        else:
            bucket = 3
        return (bucket, width, bdf)
    if guest_role == "empty":
        if host_role == "empty":
            bucket = 0
        elif width <= 1:
            bucket = 1
        elif width <= 4:
            bucket = 2
        else:
            bucket = 3
        return (bucket, width, bdf)
    if host_role == guest_role:
        bucket = 0
    elif host_role == "empty":
        bucket = 1
    elif width <= 4:
        bucket = 2
    else:
        bucket = 3
    return (bucket, width, bdf)


def assign_root_port_identities(
    guest_ports: list[dict[str, Any]], host_ports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(guest_ports) > len(host_ports):
        raise RuntimeError(
            f"Guest 需要 {len(guest_ports)} 个 Root Port，但宿主只有 {len(host_ports)} 个可用端口"
        )
    remaining = list(host_ports)
    assigned: dict[int, dict[str, Any]] = {}
    ordered = sorted(
        guest_ports,
        key=lambda item: (ROOT_PORT_ROLE_ORDER.index(item["role"]), item["port"]),
    )
    for guest in ordered:
        ranked = sorted(remaining, key=lambda host: host_root_port_rank(guest["role"], host))
        if not ranked or host_root_port_rank(guest["role"], ranked[0])[0] >= 9:
            raise RuntimeError(
                f"宿主没有适合 Guest {guest['role']} Root Port "
                f"(bus {guest['index']}, port 0x{guest['port']:02x}) 的物理端口"
            )
        chosen = ranked[0]
        remaining = [item for item in remaining if item["bdf"] != chosen["bdf"]]
        item = deepcopy(chosen)
        item["guest_target_port"] = guest["port"]
        item["guest_bus"] = guest["index"]
        item["guest_role"] = guest["role"]
        item["mapping_reason"] = (
            f"guest {guest['role']} bus {guest['index']} port 0x{guest['port']:02x} "
            f"-> host {chosen['host_role']} {chosen['bdf']} x{chosen['max_link_width']}"
        )
        assigned[guest["port"]] = item
    return [assigned[port] for port in sorted(assigned)]


def host_root_port_identities() -> list[dict[str, Any]]:
    """Capture usable root ports with downstream class, not just slot order."""
    found: list[dict[str, Any]] = []
    for device in sorted(Path("/sys/bus/pci/devices").glob("0000:00:*")):
        if read_int(device / "class") != 0x060400:
            continue
        vendor = read_int(device / "vendor")
        if vendor not in {0x8086, 0x1022}:
            continue
        slot = int(device.name.split(":")[2].split(".")[0], 16)
        if slot == 0x07:
            continue
        current_width = read_int(device / "current_link_width")
        record = pci_device_record(device, "root_port", "PCI Express Root Port")
        children = host_root_port_children(device)
        record.update({
            "max_link_speed_gtps": parse_pcie_link_speed(
                read_privileged_text(device / "max_link_speed") or "", "max_link_speed"
            ),
            "max_link_width": read_int(device / "max_link_width"),
            "current_link_speed_gtps": parse_pcie_link_speed(
                read_privileged_text(device / "current_link_speed") or "", "current_link_speed"
            ),
            "current_link_width": current_width,
            "downstream": children,
            "host_role": preferred_root_port_role(
                [pci_class_role(int(child["class_code"], 16)) for child in children]
            ),
        })
        if record["max_link_width"] <= 0:
            raise RuntimeError(f"宿主 Root Port {device.name} 链路宽度无效")
        found.append(record)
    if not found:
        raise RuntimeError("宿主缺少可用的 PCI Express Root Port 身份")
    return sorted(found, key=lambda item: item["bdf"])


def guest_root_port_profiles(root: ET.Element, host_ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    guest_ports = list_kept_guest_root_ports(root.find("devices"))
    return assign_root_port_identities(guest_ports, host_ports)


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


def onboard_hda_identity() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
        return None, None
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


DIGITS = "0123456789"
HEX_DIGITS = "0123456789ABCDEF"
UPPER_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ATA_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
OEM_ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DELL_TAG = "ABCDEFGHJKLMNPRTUVWXYZ0123456789"


def tokens(alphabet: str, length: int) -> str:
    if length < 1:
        raise ValueError("随机标识长度无效")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def tokens_between(alphabet: str, minimum: int, maximum: int) -> str:
    if maximum < minimum:
        raise ValueError("随机标识长度范围无效")
    return tokens(alphabet, minimum + secrets.randbelow(maximum - minimum + 1))


def take_unique(factory, seen: set[str]) -> str:
    for _ in range(64):
        value = factory()
        key = value.lower()
        if key not in seen:
            seen.add(key)
            return value
    raise RuntimeError("无法生成不重复的唯一标识")


def random_alnum(length: int) -> str:
    return tokens(OEM_ALNUM, length)


def random_hex(length: int) -> str:
    return tokens(HEX_DIGITS, length)


def storage_serial(identity: dict[str, Any]) -> str:
    style = str(identity["serial_style"])
    length = int(identity["serial_length"])
    if style == "samsung":
        serial = "S" + tokens(UPPER_LETTERS, 2) + tokens(ATA_ALNUM, length - 3)
    elif style == "crucial":
        serial = f"{21 + secrets.randbelow(6):02d}" + tokens(ATA_ALNUM, length - 2)
    elif style == "sandisk":
        serial = f"{21 + secrets.randbelow(6):02d}{secrets.randbelow(52) + 1:02d}" + tokens(ATA_ALNUM, length - 4)
    elif style in {"kingston", "wd", "intel", "adata", "seagate", "transcend"}:
        serial = tokens(ATA_ALNUM, length)
    else:
        raise ValueError(f"存储型号 {identity['model']} 缺少序列号结构")
    pattern = str(identity["serial_pattern"])
    if len(serial) != length or not re.fullmatch(pattern, serial):
        raise ValueError(f"存储型号 {identity['model']} 的序列号规则无效")
    return serial


def storage_wwn(identity: dict[str, Any]) -> str:
    oui = str(identity["wwn_oui"]).lower()
    if not re.fullmatch(r"[0-9a-f]{6}", oui):
        raise ValueError(f"存储型号 {identity['model']} 的 WWN OUI 无效")
    return ("5" + oui + tokens(HEX_DIGITS, 9)).upper()


def dimm_serial(vendor: str) -> str:
    name = vendor.lower()
    if "samsung" in name:
        return tokens(HEX_DIGITS, secrets.choice((8, 12)))
    if "hynix" in name:
        return tokens(ATA_ALNUM, secrets.choice((8, 10, 12)))
    if "micron" in name or "crucial" in name:
        return f"{21 + secrets.randbelow(6):02d}{secrets.randbelow(52) + 1:02d}{tokens(ATA_ALNUM, 6)}"
    if "kingston" in name:
        return tokens(ATA_ALNUM, secrets.choice((10, 12)))
    if "corsair" in name or "adata" in name:
        return tokens(ATA_ALNUM, 10)
    return tokens(ATA_ALNUM, 10)


def hid_serial(manufacturer: str) -> str:
    name = manufacturer.lower()
    if "logitech" in name:
        return tokens(DIGITS, secrets.choice((10, 12, 13)))
    if "microsoft" in name:
        return tokens(ATA_ALNUM, 12)
    if "dell" in name:
        return tokens(OEM_ALNUM, secrets.choice((10, 12)))
    if "lenovo" in name:
        return tokens(OEM_ALNUM, secrets.choice((8, 10, 12)))
    if "hp" in name:
        return tokens(OEM_ALNUM, secrets.choice((10, 12)))
    if "asus" in name:
        return tokens(ATA_ALNUM, 12)
    if "cherry" in name:
        return tokens(OEM_ALNUM, secrets.choice((8, 10)))
    return tokens_between(OEM_ALNUM, 8, 12)


def accessory_asset() -> str:
    return tokens(OEM_ALNUM, secrets.choice((8, 10, 12)))


def battery_serial(manufacturer: str) -> str:
    name = manufacturer.lower()
    if any(token in name for token in ("lenovo", "smp", "sunwoda", "celxpert", "lg")):
        return tokens(ATA_ALNUM, secrets.choice((8, 10, 11)))
    if any(token in name for token in ("dell", "sanyo", "panasonic", "smp")):
        return tokens(ATA_ALNUM, secrets.choice((7, 11)))
    return tokens(ATA_ALNUM, secrets.choice((8, 10, 12)))


def padded_ascii(value: str, length: int) -> str:
    cleaned = ascii_clean(value)
    if len(cleaned) > length:
        cleaned = cleaned[:length]
    return cleaned.ljust(length)


def firmware_cfg_signature(bios_vendor: str) -> dict[str, str]:
    vendor = bios_vendor.upper()
    if "AMI" in vendor or "MEGATREND" in vendor:
        text = "AMI     "
    elif "INSYDE" in vendor:
        text = "INSYDE  "
    elif "PHOENIX" in vendor:
        text = "PHOENIX "
    else:
        cleaned = re.sub(r"[^A-Z0-9 ]", "", vendor)
        text = (cleaned + " " * 8)[:8]
    raw = text.encode("ascii")
    if len(raw) != 8:
        raise RuntimeError("fw_cfg 签名长度必须为 8 字节")
    return {"text": text, "u64": f"0x{int.from_bytes(raw, 'big'):016X}ULL"}


def acpi_oem_hid(oem_id: str, suffix: str) -> str:
    base = re.sub(r"[^A-Z0-9]", "", ascii_clean(oem_id).upper())[:6].ljust(6, "0")
    extra = re.sub(r"[^A-Z0-9]", "", suffix.upper())[:2].ljust(2, "0")
    return (base + extra)[:8]


def kvm_cpuid_signature(cpu_vendor: str) -> str:
    if cpu_vendor == "AuthenticAMD":
        return "AuthenticAMD"
    if cpu_vendor == "GenuineIntel":
        return "GenuineIntel"
    return padded_ascii(cpu_vendor, 12)


def latent_qemu_identities(
    platform: dict[str, Any],
    hid_kit: dict[str, Any],
    firmware: dict[str, Any],
    cpu_vendor: str,
    seen: set[str],
) -> dict[str, Any]:
    nvme = deepcopy(secrets.choice(STORAGE_CATALOG["nvme"]))
    hid_brand = str(hid_kit["manufacturer"])
    disk_brand = secrets.choice(("SanDisk", "Kingston", "Samsung", "WD", "Toshiba"))
    net_brand = secrets.choice(("Realtek", "ASIX", "Aquantia"))
    signature = firmware_cfg_signature(str(firmware["vendor"]))
    oem_id = str(firmware["acpi_oem_id"])
    return {
        "nvme_model": nvme["model"],
        "nvme_firmware": nvme["firmware"],
        "usb_hid_brand": hid_brand,
        "usb_tablet_product": f"{hid_brand} USB Tablet",
        "usb_tablet_serial": take_unique(lambda: hid_serial(hid_brand), seen),
        "usb_hub_brand": ascii_clean(platform["vendor"]).split()[0] or hid_brand,
        "usb_hub_product": "USB Hub",
        "usb_hub_serial": take_unique(lambda: tokens(DIGITS, 10), seen),
        "usb_msd_brand": disk_brand,
        "usb_msd_product": f"{disk_brand} USB Disk",
        "usb_msd_serial": take_unique(lambda: tokens(ATA_ALNUM, 16), seen),
        "usb_mtp_brand": hid_brand,
        "usb_mtp_product": f"{hid_brand} MTP",
        "usb_audio_brand": hid_brand,
        "usb_audio_product": f"{hid_brand} USB Audio",
        "usb_net_brand": net_brand,
        "usb_net_product": f"{net_brand} USB NIC",
        "usb_serial_brand": hid_brand,
        "usb_serial_product": f"{hid_brand} USB Serial",
        "usb_braille_product": f"{hid_brand} USB Braille",
        "usb_ccid_product": f"{hid_brand} USB CCID",
        "usb_wacom_product": "Wacom PenPartner Tablet",
        "fw_cfg_signature_text": signature["text"],
        "fw_cfg_signature_u64": signature["u64"],
        "fw_cfg_hid": acpi_oem_hid(oem_id, "02"),
        "pvpanic_hid": acpi_oem_hid(oem_id, "01"),
        "kvm_signature": kvm_cpuid_signature(cpu_vendor),
    }


def bios_serial(platform: dict[str, Any], system_serial: str) -> str:
    vendor = platform["vendor"].upper()
    if "LENOVO" in vendor:
        return secrets.choice((system_serial, tokens(HEX_DIGITS, 16)))
    if "DELL" in vendor:
        return system_serial
    if "HP" in vendor or "HEWLETT" in vendor:
        return tokens(OEM_ALNUM, secrets.choice((10, 12)))
    return tokens(HEX_DIGITS, secrets.choice((8, 12, 16)))


def platform_serials(platform: dict[str, Any]) -> dict[str, str]:
    """Generate identifiers in formats commonly used by the selected OEM."""
    vendor = platform["vendor"].upper()
    if "LENOVO" in vendor:
        system = secrets.choice(("PF", "MP", "YB")) + tokens(OEM_ALNUM, secrets.choice((6, 8)))
        board = secrets.choice(("L1", "1S")) + tokens(OEM_ALNUM, secrets.choice((12, 14)))
    elif "DELL" in vendor:
        system = tokens(DELL_TAG, 7)
        board = "CN-0" + tokens(OEM_ALNUM, 5) + "-" + tokens(OEM_ALNUM, 5) + "-" + tokens(OEM_ALNUM, 3) + "-" + tokens(OEM_ALNUM, 4)
    elif "HP" in vendor or "HEWLETT" in vendor:
        system = tokens(UPPER_LETTERS, 3) + tokens(DIGITS, 7)
        board = "PW" + tokens(OEM_ALNUM, secrets.choice((10, 12)))
    elif "ASUS" in vendor:
        system = tokens(OEM_ALNUM, secrets.choice((12, 15)))
        board = tokens(OEM_ALNUM, secrets.choice((10, 12)))
    elif "ACER" in vendor:
        system = "NX" + tokens(OEM_ALNUM, secrets.choice((8, 10)))
        board = tokens(OEM_ALNUM, 12)
    elif "MSI" in vendor or "MICRO-STAR" in vendor:
        system = tokens(OEM_ALNUM, secrets.choice((12, 14)))
        board = tokens(OEM_ALNUM, secrets.choice((12, 14)))
    else:
        system = tokens(OEM_ALNUM, secrets.choice((10, 12)))
        board = tokens(OEM_ALNUM, secrets.choice((12, 16)))
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


def lan_services_identity(
    gateway_ip: str,
    gateway_mac: str,
    manufacturer: str,
    subnet: ipaddress.IPv4Network,
) -> dict[str, Any]:
    prefix = ROUTER_HOSTNAME_PREFIX.get(manufacturer, "router")
    suffix = gateway_mac.replace(":", "")[-4:]
    return {
        "advertise_gateway_as_dns": True,
        "broadcast": str(subnet.broadcast_address),
        "dns_domain": "lan",
        "dns_servers": [gateway_ip],
        "gateway_hostname": f"{prefix}-{suffix}",
        "isolation": "guest-only",
        "lease_seconds": 86400,
    }


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
            "lan_services": lan_services_identity(
                gateway_ip, gateway_mac, router_vendor, subnet,
            ),
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


def dimm_layout(total_bytes: int, memory_type: str, platform_class: str, array: dict) -> list[int]:
    catalog = MEMORY_CATALOG.get((memory_type, platform_class))
    if not catalog:
        raise RuntimeError(f"没有 {platform_class} {memory_type} 内存配件池")
    return choose_layout(total_bytes, catalog, array)


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


def storage_identity(device: str, bus: str) -> dict[str, Any]:
    if device != "cdrom" and bus.lower() != "sata":
        raise RuntimeError(f"持久磁盘必须使用 SATA 总线，当前为 {bus!r}")
    key = "cdrom" if device == "cdrom" else "sata"
    if key is None or key not in STORAGE_CATALOG:
        raise RuntimeError(f"没有可安全使用的 {bus!r} 存储设备目录")
    return deepcopy(secrets.choice(STORAGE_CATALOG[key]))


def validate_storage_layout(storage: list[dict[str, Any]]) -> None:
    disks = [item for item in storage if item["device"] == "disk"]
    if len(disks) != 1:
        raise RuntimeError("当前身份模型要求恰好一块持久 SATA SSD")
    if str(disks[0].get("bus", "")).lower() != "sata":
        raise RuntimeError("持久磁盘必须使用 SATA 总线")
    if any(item["device"] == "floppy" for item in storage):
        raise RuntimeError("当前身份模型不支持软盘设备")


def validate_storage_profile(record: dict[str, Any]) -> None:
    devices = record.get("storage", {}).get("devices") or []
    disks = [item for item in devices if item.get("device") == "disk"]
    if record.get("storage", {}).get("policy") != "single-sata-ssd-v1" or len(disks) != 1:
        raise RuntimeError("存储身份必须使用 single-sata-ssd-v1 策略")
    disk = disks[0]
    identity = disk.get("identity") or {}
    if str(disk.get("bus", "")).lower() != "sata" or identity.get("interface") != "sata":
        raise RuntimeError("持久磁盘及其身份必须同时声明 SATA")
    if identity.get("media_type") != "ssd" or identity.get("rotation_rate") != 1 or identity.get("trim") is not True:
        raise RuntimeError("SATA 磁盘身份必须声明非旋转 SSD 与 TRIM")
    if not all(str(identity.get(field, "")).strip() for field in ("product", "model", "firmware")):
        raise RuntimeError("SATA SSD 缺少型号或固件非唯一信息")
    serial = str(disk.get("serial", ""))
    serial_length = int(identity.get("serial_length", 0))
    pattern = str(identity.get("serial_pattern", ""))
    if not 12 <= serial_length <= 20 or len(serial) != serial_length or not pattern or not re.fullmatch(pattern, serial):
        raise RuntimeError("SATA SSD 序列号格式与配件规则不一致")
    wwn = str(disk.get("wwn", ""))
    oui = str(identity.get("wwn_oui", "")).lower()
    if (
        not re.fullmatch(r"[0-9A-F]{16}", wwn)
        or not re.fullmatch(r"[0-9a-f]{6}", oui)
        or wwn[0] != "5"
        or wwn[1:7].lower() != oui
    ):
        raise RuntimeError("SATA SSD WWN 格式与型号不一致")
    controller = record.get("hardware", {}).get("storage_controller") or {}
    if controller.get("media_policy") != "sata-ssd-only" or controller.get("ncq") is not True:
        raise RuntimeError("AHCI 控制器没有绑定 SATA SSD/NCQ 策略")


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
    manufacture_year = 2021 + secrets.randbelow(6)
    manufacture_month = secrets.randbelow(12) + 1
    manufacture_day = secrets.randbelow(28) + 1
    current_temperature = 2732 + (secrets.randbelow(15) + 38) * 10
    return {
        "enabled": True,
        "manufacturer": ascii_clean(host_battery["manufacturer"]),
        "model": ascii_clean(host_battery["model"]),
        "serial": battery_serial(ascii_clean(host_battery["manufacturer"])),
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
    for name in ("host_bridge", "lpc", "smbus", "sata", "root_port", "vga"):
        if profiled_pci.get(name) != observed_pci.get(name):
            raise RuntimeError(f"{name} PCI identity does not match host observation")
    audio = record["hardware"]["audio"]
    audio_policy = audio.get("xml_policy")
    if record.get("xml_policy", {}).get("audio") != audio_policy:
        raise RuntimeError("音频 XML 策略前后记录不一致")
    if audio_policy == "onboard-hda":
        if profiled_pci.get("hda") != observed_pci.get("hda"):
            raise RuntimeError("hda PCI identity does not match host observation")
        observed_hda = record["host"].get("hda_controller_observed") or {}
        if audio.get("bdf") != observed_hda.get("bdf"):
            raise RuntimeError("HDA PCI 地址没有继承板载控制器")
        pins = audio.get("codec_pins") or []
        if not any(pin.get("direction") == "playback" for pin in pins):
            raise RuntimeError("板载 analog codec 没有可继承的播放针脚")
        if any(pin.get("source") != "host-observed-onboard-codec" for pin in pins):
            raise RuntimeError("HDA 针脚必须来自板载 analog codec")
    elif audio_policy == "none":
        if profiled_pci.get("hda") or observed_pci.get("hda"):
            raise RuntimeError("无板载 analog HDA 时不应写入 HDA PCI 身份")
    else:
        raise RuntimeError("音频 XML 策略必须是芯片组 HDA 或 none")
    vga = profiled_pci.get("vga") or {}
    if vga.get("source") != "qemu-stdvga-temporary" or vga.get("vendor_id") != "1234" or vga.get("device_id") != "1111":
        raise RuntimeError("临时 VGA 必须保持 QEMU 1234:1111，不能套用真实 GPU ID")
    if record["hardware"].get("mce_banks") != record["host"].get("mce_banks_observed"):
        raise RuntimeError("MCE bank 数量没有继承宿主")
    latent = record.get("qemu_policy", {}).get("latent") or {}
    required_latent = {
        "nvme_model", "nvme_firmware", "usb_msd_brand", "usb_msd_product", "usb_msd_serial",
        "usb_hub_brand", "usb_hub_product", "usb_audio_product", "usb_net_product",
        "fw_cfg_signature_u64", "fw_cfg_hid", "pvpanic_hid", "kvm_signature",
        "usb_tablet_product", "usb_tablet_serial",
    }
    if not required_latent.issubset(latent):
        raise RuntimeError("身份文件缺少潜伏 QEMU 设备身份")
    if len(str(latent.get("kvm_signature", ""))) != 12:
        raise RuntimeError("KVM CPUID 签名必须为 12 字节")
    if len(str(latent.get("fw_cfg_hid", ""))) != 8 or len(str(latent.get("pvpanic_hid", ""))) != 8:
        raise RuntimeError("fw_cfg/pvpanic ACPI HID 必须为 8 字符")
    validate_storage_profile(record)
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
    sizes = dimm_layout(total_memory, platform["memory_type"], platform["class"], smbios_facts["memory"]["array"])
    memory_slots = guest_slots(sizes, smbios_facts["memory"]["slots"], smbios_facts["memory"]["array"])
    memory_vendor, dimm_parts = memory_identity(platform["memory_type"], platform["class"], sizes)
    cache = host_cache_sizes()
    storage = parse_storage(root)
    validate_storage_layout(storage)
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
    if host_audio is not None and host_codec is not None:
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
    if host_audio is not None and host_codec is not None:
        audio = {
            "enabled": True,
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
        host_pci["audio"] = {
            "vendor_id": host_audio["subsystem_vendor_id"],
            "device_id": host_audio["subsystem_device_id"],
            "source": "host-observed-selected-hda",
        }
    else:
        audio = {
            "enabled": False,
            "xml_policy": "none",
            "implementation": "none",
            "patch_if_present": False,
        }
    host_pci["usb"] = {
        "vendor_id": host_xhci["subsystem_vendor_id"],
        "device_id": host_xhci["subsystem_device_id"],
        "source": "host-observed-selected-xhci",
    }
    required_pci = {"host_bridge", "root_port", "usb", "lpc", "smbus", "sata", "display"}
    if audio.get("xml_policy") == "onboard-hda":
        required_pci.add("audio")
    missing_pci = sorted(required_pci - set(host_pci))
    if missing_pci:
        raise RuntimeError("宿主缺少当前 Q35 后端必需的 PCI subsystem 身份: " + ", ".join(missing_pci))
    host_pci_id["vga"] = host_vga_identity(host_pci["display"])
    pci_subsystems = deepcopy(host_pci)
    optical_identity = deepcopy(secrets.choice(STORAGE_CATALOG["cdrom"]))
    hid_kit = deepcopy(secrets.choice(HID_CATALOG))
    normalized_storage = []
    for item in storage:
        entry = dict(item)
        if item["device"] == "cdrom":
            entry["identity"] = deepcopy(optical_identity)
        else:
            entry["identity"] = storage_identity(item["device"], item["bus"])
        normalized_storage.append(entry)
    generated_uuid = str(uuid.uuid4())
    profile_id = str(uuid.uuid4())
    serials = platform_serials(platform)
    mac_addresses = [random_mac(item["model_target"]) for item in interfaces]
    network_adapters = generate_lan_profiles(mac_addresses, profile_id, prefix)
    for interface, adapter in zip(interfaces, network_adapters):
        interface["managed_network"] = adapter["libvirt_network"]["name"]
    seen_unique = {
        value.strip().lower()
        for value in physical_identifiers.values()
        if str(value).strip()
    }
    for value in (serials["system"], serials["board"], serials["chassis"]):
        seen_unique.add(value.lower())
    identity = {
        "system_uuid": generated_uuid,
        "system_serial": serials["system"],
        "board_serial": serials["board"],
        "chassis_serial": serials["chassis"],
        "bios_serial": take_unique(lambda: bios_serial(platform, serials["system"]), seen_unique),
        "cpu_serial": take_unique(lambda: tokens(HEX_DIGITS, secrets.choice((8, 12, 16))), seen_unique),
        "cpu_asset": take_unique(accessory_asset, seen_unique),
        "board_asset": take_unique(accessory_asset, seen_unique),
        "chassis_asset": take_unique(accessory_asset, seen_unique),
        "chassis_sku": platform["sku"],
        "dimm_serials": [take_unique(lambda: dimm_serial(memory_vendor), seen_unique) for _ in sizes],
        "dimm_assets": [take_unique(accessory_asset, seen_unique) for _ in sizes],
        "disk_serials": [
            take_unique(lambda item=item: storage_serial(item["identity"]), seen_unique)
            for item in normalized_storage if item["device"] != "cdrom"
        ],
        "disk_wwns": [
            take_unique(lambda item=item: storage_wwn(item["identity"]), seen_unique)
            for item in normalized_storage if item["device"] != "cdrom"
        ],
        "mac_addresses": mac_addresses,
        "network_adapters": network_adapters,
        "usb_mouse_serial": take_unique(lambda: hid_serial(hid_kit["manufacturer"]), seen_unique),
        "usb_keyboard_serial": take_unique(lambda: hid_serial(hid_kit["manufacturer"]), seen_unique),
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
    if battery.get("enabled"):
        battery["serial"] = take_unique(lambda: battery_serial(str(battery.get("manufacturer", ""))), seen_unique)
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
        "memory_slots": deepcopy(memory_slots),
        "connectors": deepcopy(smbios_facts.get("connectors") or []),
        "slots": deepcopy(smbios_facts.get("slots") or []),
        "created_at": now,
    }
    legacy = configure_guest_profile(legacy)
    smbios = build_smbios_stream(legacy, platform, identity)
    xml_sha = hashlib.sha256(xml_text.encode()).hexdigest()
    latent = latent_qemu_identities(platform, hid_kit, fw, host["vendor"], seen_unique)
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
                "slots": deepcopy(memory_slots),
                "layout": MEMORY_LAYOUT,
                "mapped_ranges": q35_ranges(total_memory),
            },
            "power": power,
            "storage_controller": {
                "sata_generation": 3,
                "ata_major_version": 8,
                "udma_mode": 6,
                "ncq": True,
                "media_policy": "sata-ssd-only",
                "source": "modern-ahci-profile",
            },
            "optical": deepcopy(optical_identity),
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
            "policy": "single-sata-ssd-v1",
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
            "smbios_entry_point": dict(ENTRY_POINT),
            "usb_hid": {
                **hid_kit,
                "mouse_serial": identity["usb_mouse_serial"],
                "keyboard_serial": identity["usb_keyboard_serial"],
                "tablet_product": latent["usb_tablet_product"],
                "tablet_serial": latent["usb_tablet_serial"],
            },
            "latent": latent,
        },
        "ovmf_policy": {
            "flash_size_bytes": FIRMWARE_SIZE,
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
            "audio": audio["xml_policy"],
        },
        "smbios_profile": legacy,
    }
    validate_record_coherence(record)
    validate_memory_record(record, smbios)
    record["artifacts"] = build_artifact_contract(record, smbios)
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
        if audio.get("xml_policy") == "onboard-hda":
            pin_desc = ", ".join(f"{pin['device']}:{pin['config']}" for pin in audio["codec_pins"])
            print(
                f"  音频:   {audio['controller_product']} {audio['bdf']} "
                f"({audio['controller_vendor_id']}:{audio['controller_device_id']}) "
                f"{audio['codec_name']} [{pin_desc}]"
            )
        else:
            print("  音频:   无板载 analog HDA（不注入芯片组声卡）")
        for item in record["devices"]["pci_identities"].get("root_ports") or []:
            print(f"  RP:     {item.get('mapping_reason')}")
        for index, adapter in enumerate(record["identity"]["network_adapters"], 1):
            services = adapter.get("lan_services") or {}
            print(
                f"  网络{index}: MAC {adapter['mac']}, "
                f"IP {adapter['ipv4']['address']}/{adapter['ipv4']['prefix_length']} "
                f"via {adapter['gateway']['ipv4_address']} "
                f"({adapter['gateway']['mac']}) "
                f"DHCP {services.get('gateway_hostname', '?')}.{services.get('dns_domain', '?')} "
                f"isolation={services.get('isolation', '?')}"
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
