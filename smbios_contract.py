"""SMBIOS stream construction and structural contract for the fixed guest model."""

from __future__ import annotations

import struct
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime
from typing import Iterable

from smbios_memory import SCHEMA_VERSION, memory_tables, validate_stream

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


def configure_guest_profile(profile: dict) -> dict:
    result = deepcopy(profile)
    result.update(
        schema=SCHEMA_VERSION, cache_policy="unknown-until-guest-observed",
        cache_l1_kib=None, cache_l2_kib=None, cache_l3_kib=None,
        memory_rated_speed=0, memory_min_voltage=0,
        memory_max_voltage=0, memory_configured_voltage=0,
        cpu_voltage=0, cpu_external_clock_mhz=0,
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
    if profile["cpu_voltage"] != 0 or profile["cpu_external_clock_mhz"] != 0:
        raise ValueError("Guest CPU electrical measurements are unavailable")
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
