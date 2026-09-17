"""Shared fixed-memory contract for the four-stage spoof workflow."""

from __future__ import annotations

import struct
from typing import Any
from xml.etree import ElementTree as ET

MIB = 1024**2
GIB = 1024**3
SCHEMA_VERSION = 25
MEMORY_LAYOUT = "q35-fixed-sequential-v1"


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
        if any(value != 0 for value in voltages) or rated != 0:
            raise ValueError("Unobserved virtual DIMM electrical/rated values must remain unknown")
        body = struct.pack(
            "<5H5B2H5BI4H", 0x1000, 0xFFFE, 64 if installed else 0xFFFF,
            64 if installed else 0xFFFF, size_field, slot["form_factor"], 0, 1, 2,
            int(platform["memory_type_code"]) if installed else 0x02, 0x80 if installed else 0,
            rated, 3 if installed else 0, 4 if installed else 0, 5 if installed else 0,
            6 if installed else 0, 0, size if size_field == 0x7FFF else 0, min(speed, 0xFFFF), *voltages,
        )
        body += struct.pack(
            "<BHB4H4Q2I", 3 if installed else 2, 8 if installed else 4,
            0, 0, 0, 0, 0, 0, size * MIB, 0, 0, 0,
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


def validate_record(record: dict, data: bytes | None = None) -> None:
    try:
        if record["meta"]["schema_version"] != SCHEMA_VERSION:
            raise ValueError("身份文件需要 schema 25，请重新运行 01 并重建 02/03")
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
        from smbios_contract import ENTRY_POINT, FIRMWARE_SIZE, build_smbios_stream, validate_profile

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
