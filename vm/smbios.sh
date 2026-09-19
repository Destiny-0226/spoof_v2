#!/bin/bash
set -u

if ! command -v python3 >/dev/null 2>&1; then
    echo "错误: python3 未安装"
    echo "Ubuntu/Debian: sudo apt install python3"
    echo "Arch/Manjaro:  sudo pacman -S python"
    exit 1
fi

# ==================== Python 部分：生成 /usr/local/bin/smbios.bin ====================
PYTHON_RUNNER=(python3)
if [[ ${EUID:-$(id -u)} -ne 0 && ! -w /usr/local/bin ]]; then
    PYTHON_RUNNER=(sudo python3)
fi

"${PYTHON_RUNNER[@]}" <<'PY'
from pathlib import Path
import random
import re
import string
import sys
import uuid

OUT_PATH = Path('/usr/local/bin/smbios.bin')

PLACEHOLDERS = {
    '', 'NOT SPECIFIED', 'NONE', 'DEFAULT STRING', 'TO BE FILLED BY O.E.M.',
    'TO BE FILLED BY OEM', '00000000', 'EMPTY', 'UNKNOWN', 'N/A', 'NA',
    'SYSTEM PRODUCT NAME', 'SYSTEM SERIAL NUMBER', 'SYSTEM MANUFACTURER',
    'OEM', 'NO DIMM', 'SYSTEM RAM', 'SERIALNUMBER', 'SERIAL NUMBER',
}

BRANDS = {
    'asus': {
        'aliases': ('ASUS', 'ASUSTEK'),
        'prefixes': ('ROG ', 'TUF ', 'PRIME ', 'PROART ', 'EXPERT ', 'BATTLE-AX '),
        'fallback': 'ASUS',
        'serial_style': 'asus',
    },
    'gigabyte': {
        'aliases': ('GIGABYTE', 'AORUS'),
        'prefixes': ('AORUS ', 'GIGABYTE ', 'UD ', 'DS3H', 'GAMING ', 'AERO '),
        'fallback': 'GIGABYTE',
        'serial_style': 'alnum12',
    },
    'msi': {
        'aliases': ('MICRO-STAR', 'MICRO STAR', 'MSI'),
        'prefixes': ('MS-', 'MPG ', 'MAG ', 'MEG ', 'PRO '),
        'fallback': 'MSI',
        'serial_style': 'alnum10',
    },
    'asrock': {
        'aliases': ('ASROCK',),
        'prefixes': ('ASROCK ', 'STEEL LEGEND ', 'TAICHI ', 'PHANTOM GAMING ', 'PRO RS ', 'PG '),
        'fallback': 'ASRock',
        'serial_style': 'alnum12',
    },
    'colorful': {'aliases': ('COLORFUL', 'IGAME'), 'prefixes': ('CVN ', 'IGAME ', 'BATTLE-AX '), 'fallback': 'Colorful', 'serial_style': 'alnum12'},
    'maxsun': {'aliases': ('MAXSUN',), 'prefixes': ('TERMINATOR ', 'ICRAFT ', 'CHALLENGER '), 'fallback': 'MAXSUN', 'serial_style': 'alnum12'},
    'supermicro': {'aliases': ('SUPERMICRO', 'SUPER MICRO'), 'prefixes': ('X11', 'X12', 'X13', 'H11', 'H12', 'MBD-'), 'fallback': 'Supermicro', 'serial_style': 'alnum14'},
    'intel': {'aliases': ('INTEL',), 'prefixes': ('INTEL ', 'NUC', 'DH', 'DQ', 'DB', 'DP'), 'fallback': 'Intel', 'serial_style': 'alnum12'},
    'huanan': {'aliases': ('HUANAN', 'HUANANZHI', '华南'), 'prefixes': ('X99 ', 'X79 ', 'HUANAN '), 'fallback': 'HUANANZHI', 'serial_style': 'alnum10'},
}

GENERIC_BOARD_PATTERNS = (
    r'^[A-Z]?[ZXBHWA][0-9]{2,4}[A-Z0-9 -]*(WIFI|WI-FI|GAMING|PLUS|PRO|ELITE|MASTER|HERO|ACE|CARBON|EDGE|MORTAR|TOMAHAWK|AORUS|TAICHI|STEEL LEGEND|DASH)?$',
    r'^(ROG|TUF|PRIME|PROART|AORUS|MPG|MAG|MEG|PRO|CVN|IGAME|RACING|N[57])\b',
)

ADATA_DDR5_PARTS = (
    'AX5U5600C3616G-DTLABK',
    'AX5U6000C3016G-DTLABK',
    'AX5U6000C3016G-DTLABRWH',
    'AX5U6400C3216G-DTLABK',
    'AX5U5600C3616G-DTLABK-DP',
    'AX5U6000C3016G-DTLABK-DP',
    'AX5U6400C3216G-DTLABK-DP',
)

DDR5_PARTS = ADATA_DDR5_PARTS + (
    'KF556C40BB-16', 'KF560C36BBE-16', 'CMK32GX5M2B6000C36',
    'F5-6000J3038F16GX2-TZ5N', 'CT16G56C46U5',
)

DDR4_BY_PREFIX = (
    (('CM4', 'CMK'), ('CM4X8GD3200C16K2E', 'CM4X8GD3000C16K4D', 'CMK16GX4M2B3200C16', 'CMK16GX4M2E3200C16')),
    (('M378', 'M471'), ('M378A1K43CB2-CRC', 'M378A2K43CB1-CTD', 'M378A1K43DB2-CTD', 'M471A1K43DB1-CTD')),
    (('HMA', 'HMC'), ('HMA81GU6CJR8N-VK', 'HMA82GU6DJR8N-VK', 'HMA81GU6AFR8N-UH', 'HMA82GU6CJR8N-XN')),
    (('CT',), ('CT8G4DFS824A', 'CT16G4DFRA32A', 'CT8G4DFS832A', 'CT16G4DFD832A')),
    (('KF4', 'KHX'), ('KF432C16BB/8', 'KHX3200C16D4/8GX', 'KF426C16BB/8', 'KF432C16BB/16')),
    (('F4-',), ('F4-3200C16-8GVKB', 'F4-3600C18-8GVK', 'F4-3200C16-16GVK')),
)

KINGSTON_OEM_DDR4 = ('9905622-058.A00G', '9905702-017.A00G', '9905713-030.A00G', '9965745-005.A00G')
DDR4_PARTS = tuple(part for _, parts in DDR4_BY_PREFIX for part in parts) + KINGSTON_OEM_DDR4
ALL_RAM_PARTS = DDR5_PARTS + DDR4_PARTS


def rand_alnum(length):
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(max(length, 0)))


def rand_hex(length):
    return ''.join(random.choice('0123456789ABCDEF') for _ in range(max(length, 0)))


def is_placeholder(value):
    return value.strip().upper() in PLACEHOLDERS


def dmi(field):
    path = Path('/sys/class/dmi/id') / field
    try:
        return path.read_text().strip() if path.exists() else ''
    except (OSError, PermissionError):
        return ''


def to_smbios_uuid(value):
    raw = value.replace('-', '')
    return ''.join(raw[i:i + 2] for i in (6, 4, 2, 0, 10, 8, 14, 12)) + raw[16:]


def detect_brand():
    text = ' '.join(dmi(field) for field in ('board_vendor', 'sys_vendor', 'board_name', 'product_name')).upper()
    for brand, profile in BRANDS.items():
        if any(alias in text for alias in profile['aliases']):
            return brand
    for brand, profile in BRANDS.items():
        if any(text.startswith(prefix) or f' {prefix}' in text for prefix in profile['prefixes']):
            return brand
    return 'unknown'


def brand_profile():
    return BRANDS.get(detect_brand(), {})


def platform_text():
    values = [dmi(field) for field in ('board_name', 'product_name', 'board_vendor', 'sys_vendor')]
    try:
        values.append(Path('/proc/cpuinfo').read_text(errors='ignore'))
    except (OSError, PermissionError):
        pass
    return ' '.join(values).upper()


def memory_generation():
    text = platform_text()
    if re.search(r'\b(B650|B650E|B840|B850|X670|X670E|X870|X870E|A620)\b', text):
        return 'DDR5'
    if 'RYZEN' in text and re.search(r'\b(7[0-9]{3}X3D|8[0-9]{3}G|9[0-9]{3}X3D|9[0-9]{3}X|9[0-9]{3})\b', text):
        return 'DDR5'
    if 'DDR5' in text:
        return 'DDR5'
    if 'DDR4' in text:
        return 'DDR4'
    return 'UNKNOWN'


def looks_like_board(value):
    value = value.strip().upper()
    if is_placeholder(value):
        return False
    for profile in BRANDS.values():
        if any(alias in value for alias in profile['aliases']) or any(value.startswith(prefix) for prefix in profile['prefixes']):
            return True
    return any(re.match(pattern, value) for pattern in GENERIC_BOARD_PATTERNS)


def fit(value, original):
    target = len(original.encode('latin-1', errors='ignore'))
    raw = value.encode('latin-1', errors='ignore')
    if len(raw) < target:
        value += rand_alnum(target - len(raw))
        raw = value.encode('latin-1', errors='ignore')
    return raw[:target].decode('latin-1', errors='ignore')


def prefix_len(length, max_keep=6):
    if length >= 15:
        return min(max_keep, 6)
    if length >= 10:
        return min(max_keep, 4)
    if length >= 8:
        return min(max_keep, 2)
    return 0


def similar_serial(original, fallback_len=10, keep=None):
    original = original.strip().upper()
    if not original or is_placeholder(original):
        return rand_alnum(fallback_len)

    if original.isdigit():
        length = len(original)
        keep = prefix_len(length) if keep is None else min(keep, length)
        return original[:keep] + ''.join(random.choice(string.digits) for _ in range(length - keep))

    if re.match(r'^[A-Z0-9]+$', original):
        length = len(original)
        keep = prefix_len(length, 4) if keep is None else min(keep, length)
        return original[:keep] + rand_alnum(length - keep)

    keep = prefix_len(len(original), 4)
    out = []
    for i, ch in enumerate(original):
        if i < keep or not ch.isalnum():
            out.append(ch)
        elif ch.isdigit():
            out.append(random.choice(string.digits))
        else:
            out.append(random.choice(string.ascii_uppercase))
    return ''.join(out)


def board_serial(original):
    style = brand_profile().get('serial_style', 'preserve')
    fallback = {'asus': 15, 'alnum14': 14, 'alnum12': 12, 'alnum10': 10}.get(style, 10)
    if is_placeholder(original):
        # 无原串可参照时按品牌习惯造：ASUS 15 位纯数字，其余品牌大写字母+数字。
        if style == 'asus':
            return ''.join(random.choice(string.digits) for _ in range(fallback))
        return rand_alnum(fallback)
    # keep=0：整串随机，不保留原厂批次前缀。similar_serial 会沿用原串的
    # 长度和字符类型（纯数字保持纯数字），所以格式仍然像真实序列号。
    return similar_serial(original, fallback, keep=0)


def smart_string(original):
    if re.match(r'^[0-9A-Fa-f]+$', original):
        return rand_hex(len(original))
    return rand_alnum(len(original))


def memory_serial(original):
    length = min(max(len(original), 8), 16)
    return rand_hex(length) if all(ch in '0123456789ABCDEFabcdef' for ch in original) else rand_alnum(length)


def pick_same_length(templates, original):
    same = [item for item in templates if len(item) == len(original)]
    different = [item for item in same if item != original]
    if different:
        return random.choice(different)
    if same:
        return random.choice(same)
    return similar_serial(original, len(original))


def adata_target_speed(original_speed):
    candidates = [speed for speed in (5600, 6000, 6400) if speed > original_speed]
    return candidates[0] if candidates else original_speed


def adata_part(original, target_speed=None):
    match = re.match(r'^(AX5U)(\d{4})(C\d{2})(\d{2}G)(-.+)$', original.strip().upper())
    if not match:
        return random.choice(ADATA_DDR5_PARTS)

    prefix, old_speed, old_timing, capacity, suffix = match.groups()
    if suffix not in ('-DTLABK', '-DTLABRWH', '-DTLABK-DP'):
        suffix = '-DTLABRWH' if suffix.startswith('-DTLABRWH') else '-DTLABK'

    speed = str(target_speed) if target_speed and str(target_speed) in ('5600', '6000', '6400') else random.choice([s for s in ('5600', '6000', '6400') if s != old_speed] or [old_speed])
    timing_by_speed = {'5600': ('C36', 'C40'), '6000': ('C30', 'C36'), '6400': ('C32', 'C40')}
    timing_choices = [timing for timing in timing_by_speed.get(speed, (old_timing,)) if timing != old_timing]
    timing = random.choice(timing_choices or timing_by_speed.get(speed, (old_timing,)))
    return f'{prefix}{speed}{timing}{capacity}{suffix}'


def ddr4_part(original):
    original = original.strip().upper()
    if re.match(r'^99[0-9]{5}-[0-9]{3}\.A00G$', original):
        return pick_same_length(KINGSTON_OEM_DDR4, original)
    for prefixes, templates in DDR4_BY_PREFIX:
        if original.startswith(prefixes):
            return pick_same_length(templates, original)
    return pick_same_length(DDR4_PARTS, original)


def ram_part(original):
    original = original.strip().upper()
    generation = memory_generation()
    if generation == 'DDR5':
        return adata_part(original) if original.startswith('AX5U') else random.choice(DDR5_PARTS)
    if generation == 'DDR4':
        return ddr4_part(original)
    if original.startswith('AX5U'):
        return adata_part(original)
    if original.startswith(('KF5', 'F5-')):
        return random.choice(DDR5_PARTS)
    if original.startswith(('CM4', 'CMK', 'KF4', 'KHX', 'F4-', 'M378', 'M471', 'HMA', 'HMC')) or re.match(r'^99[0-9]{5}-[0-9]{3}\.A00G$', original):
        return ddr4_part(original)
    if original.startswith('CT'):
        return pick_same_length(DDR5_PARTS + DDR4_PARTS, original)
    return pick_same_length(ALL_RAM_PARTS, original)


def choose_speed(original_speed, generation):
    speeds = {
        'DDR5': (4800, 5200, 5600, 6000, 6200, 6400, 6600, 6800, 7200, 7600, 8000),
        'DDR4': (2133, 2400, 2666, 2933, 3000, 3200, 3466, 3600, 4000),
    }.get(generation, (2133, 2400, 2666, 2933, 3000, 3200, 3466, 3600, 4000, 4800, 5200, 5600, 6000, 6200, 6400, 6600, 6800, 7200, 7600, 8000))
    candidates = [speed for speed in speeds if speed > original_speed]
    return random.choice(candidates[:3]) if candidates else original_speed


def unique(generator, original, used):
    for _ in range(30):
        value = generator(original)
        if value != original and value not in used:
            used.add(value)
            return value
    # 兜底：空串会被当成字符串区终止符，导致该结构后续字符串全部丢失，
    # 所以 original 为空时必须给一个非空长度。
    value = fit(similar_serial(original, len(original) or 8), original) or rand_alnum(8)
    used.add(value)
    return value


def smbios_parts(raw, header_len):
    return raw[header_len:].split(b'\x00') if len(raw) > header_len else []


def string_at(parts, index, allow_placeholder=False):
    if index <= 0 or index > len(parts):
        return None
    value = parts[index - 1].decode('latin-1', errors='ignore').strip()
    if not value or (not allow_placeholder and is_placeholder(value)):
        return None
    return value


def set_string(parts, index, value):
    if index <= 0 or index > len(parts):
        return False
    parts[index - 1] = value.encode('latin-1', errors='ignore')
    return True


def word(raw, offset):
    return int.from_bytes(raw[offset:offset + 2], 'little') if len(raw) >= offset + 2 else None


def set_word(raw, offset, value):
    raw[offset:offset + 2] = int(value).to_bytes(2, 'little')


def replace_string_bytes(data, old_value, new_value, once=False):
    old_b = old_value.encode('latin-1', errors='ignore')
    new_b = new_value.encode('latin-1', errors='ignore')
    if old_b not in data:
        return data, False
    return data.replace(old_b, new_b, 1 if once else -1), True


def entry_point_len(data):
    # smbios.bin = entry point + DMI 结构表。_SM3_ 为 24 字节，_SM_ 为 31 字节，
    # 实际长度取 EP 自带的 length 字段，取不到时回退到规范默认值。
    if data[:5] == b'_SM3_':
        return data[6] if len(data) > 6 and 24 <= data[6] <= 32 else 24
    if data[:4] == b'_SM_':
        return data[5] if len(data) > 5 and 31 <= data[5] <= 40 else 31
    return 0


def split_table(table):
    # 逐个结构切分：header(4) + formatted area 到 raw[1]，之后是以 \0 分隔、
    # 以 \0\0 结尾的字符串区。返回每个结构的完整字节块。
    blocks = []
    offset = 0
    while offset + 4 <= len(table):
        length = table[offset + 1]
        if length < 4:
            break
        cursor = offset + length
        while cursor + 1 < len(table) and not (table[cursor] == 0 and table[cursor + 1] == 0):
            cursor += 1
        cursor += 2
        if cursor > len(table):
            break
        blocks.append(bytes(table[offset:cursor]))
        if table[offset] == 127:  # Type 127 = End-of-Table
            break
        offset = cursor
    return blocks


def max_structure_size(table):
    return max((len(block) for block in split_table(table)), default=0)


def split_blocks(data):
    # 直接从 blob 解析结构表，不依赖 /sys/firmware/dmi/entries（需 dmi-sysfs 模块）。
    # 返回 (entry point, 结构块列表, 未能解析的尾部)，join_blocks 可无损还原。
    ep_len = entry_point_len(data)
    if not ep_len or len(data) <= ep_len:
        return data, [], b''
    blocks = split_table(data[ep_len:])
    consumed = sum(len(block) for block in blocks)
    return data[:ep_len], blocks, data[ep_len + consumed:]


def join_blocks(ep, blocks, tail):
    return ep + b''.join(blocks) + tail


def checksum(buf, start, length, slot):
    buf[slot] = 0
    buf[slot] = (-sum(buf[start:start + length])) & 0xFF


def fix_entry_point(data):
    # 序列号长度变化后，EP 里的 table length / max structure size / 校验和会失效。
    # dmidecode --from-dump 依赖这些字段；QEMU 的 -smbios file= 会自己重建 EP，不受影响。
    ep_len = entry_point_len(data)
    if not ep_len or len(data) <= ep_len:
        return data, None

    blob = bytearray(data)
    table_len = len(blob) - ep_len

    if blob[:5] == b'_SM3_':
        old_len = int.from_bytes(blob[0x0C:0x10], 'little')
        blob[0x0C:0x10] = table_len.to_bytes(4, 'little')
        checksum(blob, 0, ep_len, 0x05)
    else:
        old_len = word(blob, 0x16)
        set_word(blob, 0x16, table_len)
        set_word(blob, 0x08, max_structure_size(blob[ep_len:]))
        checksum(blob, 0x10, 15, 0x15)   # Intermediate checksum: _DMI_ 起 15 字节
        checksum(blob, 0, ep_len, 0x04)

    return bytes(blob), (old_len, table_len)


def modify_uuid(data):
    current = dmi('product_uuid')
    if not current:
        return data, None
    old_b = bytes.fromhex(to_smbios_uuid(current))
    new_uuid = str(uuid.uuid4())
    index = data.find(old_b)
    if index == -1:
        return data, None
    return data[:index] + bytes.fromhex(to_smbios_uuid(new_uuid)) + data[index + 16:], (current, new_uuid)


SERIAL_FIELDS = ('product_serial', 'board_serial', 'chassis_serial')


def modify_sysfs_field(data, field, label):
    value = dmi(field)
    if not value:
        return data, None

    is_serial = field in SERIAL_FIELDS

    # 占位符（如 System Serial Number / SerialNumber / To be filled by O.E.M.）
    # 同样需要替换成真实随机序列号，否则会原样留在 smbios.bin 里。
    if is_placeholder(value) and not is_serial:
        return data, None

    if is_serial:
        # 不再补齐到原长度：占位符（如 20 字符的 System Serial Number）会生成
        # 同样 20 位的随机串，本身就是可疑特征。长度按品牌 profile 走，
        # 由 fix_entry_point() 在写文件前重算 entry point，避免表长度失效。
        new_value = board_serial(value)
    else:
        new_value = smart_string(value)

    # 逐个替换：product_serial 与 chassis_serial 常常是同一个占位符字符串，
    # once=True 才能让它们各自拿到不同的序列号。
    data, changed = replace_string_bytes(data, value, new_value, once=is_serial)
    return (data, (value, new_value)) if changed else (data, None)


def modify_board_asset_tag(data):
    value = dmi('board_asset_tag')
    if not value or is_placeholder(value):
        return data, None
    new_value = fit(smart_string(value), value)
    data, changed = replace_string_bytes(data, value, new_value)
    return (data, (value, new_value)) if changed else (data, None)


def modify_product_name(data):
    value = dmi('product_name')
    fallback = brand_profile().get('fallback')
    if not value or is_placeholder(value) or not fallback or looks_like_board(value):
        return data, None
    data, changed = replace_string_bytes(data, value, fit(fallback, value))
    return (data, (value, fallback)) if changed else (data, None)


def modify_cpu(data):
    ep, blocks, tail = split_blocks(data)
    if not blocks:
        return data, []

    changes = []
    absent = []
    used = set()
    fields = (('CPU Serial', 0x20, 16), ('CPU Asset Tag', 0x21, 12), ('CPU Part Number', 0x22, 12))

    for i, raw in enumerate(blocks):
        if raw[0] != 4:
            continue
        header_len = raw[1]
        parts = smbios_parts(raw, header_len)
        entry_changes = []
        for label, offset, fallback_len in fields:
            if header_len <= offset:
                continue
            index = raw[offset]
            if index == 0:
                # string index = 0 表示主机 SMBIOS 里这个字段根本没有字符串条目，
                # 没有内容可随机化。注入新字符串反而会让虚拟机比真机更特殊。
                absent.append(label)
                continue
            value = string_at(parts, index, allow_placeholder=True)
            if value is None:
                continue
            if is_placeholder(value):
                new_value = rand_alnum(fallback_len)
                used.add(new_value)
            else:
                new_value = unique(lambda original: similar_serial(original, fallback_len), value, used)
            if set_string(parts, index, new_value):
                entry_changes.append(f'{label}: {value or "(空)"} -> {new_value}')
        if entry_changes:
            blocks[i] = raw[:header_len] + b'\x00'.join(parts)
            changes.extend(entry_changes)

    if absent:
        changes.append(f'CPU 提示: {", ".join(sorted(set(absent)))} 主机本身无此字符串条目，未注入')
    return join_blocks(ep, blocks, tail), changes


def modify_memory(data):
    ep, blocks, tail = split_blocks(data)
    indexes = [i for i, raw in enumerate(blocks) if raw[0] == 17 and raw[1] >= 27]
    if not indexes:
        return data, []

    changes = []
    used_serials = set()
    generation = memory_generation()
    base_speeds = []
    for i in indexes:
        raw = blocks[i]
        values = [v for v in (word(raw, 0x15), word(raw, 0x20) if raw[1] >= 0x22 else None) if v and v not in (0, 0xFFFF)]
        if values:
            base_speeds.append(max(values))

    base_speed = max(base_speeds) if base_speeds else 0
    target_speed = adata_target_speed(base_speed) if generation == 'DDR5' else choose_speed(base_speed, generation) if base_speed else None
    part_map = {}

    for i in indexes:
        raw = blocks[i]
        header_len = raw[1]
        parts = smbios_parts(raw, header_len)
        entry_changes = []

        # allow_placeholder=True：占位符（如空串、Unknown）同样要随机化。
        serial = string_at(parts, raw[0x18], allow_placeholder=True)
        if serial is not None and len(serial) <= 30:
            new_serial = unique(memory_serial, serial, used_serials)
            if set_string(parts, raw[0x18], new_serial):
                entry_changes.append(f'Memory Serial: {serial or "(空)"} -> {new_serial}')

        part = string_at(parts, raw[0x1A], allow_placeholder=True)
        if part is not None and len(part) <= 32:
            key = part.strip().upper()
            if key not in part_map:
                part_map[key] = adata_part(part, target_speed) if key.startswith('AX5U') and target_speed else ram_part(part)
            if set_string(parts, raw[0x1A], part_map[key]):
                entry_changes.append(f'Memory PartNumber: {part or "(空)"} -> {part_map[key]}')

        new_raw = bytearray(raw[:header_len] + b'\x00'.join(parts)) if entry_changes else bytearray(raw)

        if generation == 'DDR5' and header_len > 0x12 and raw[0x12] != 0x22:
            new_raw[0x12] = 0x22
            entry_changes.insert(0, f'Memory Type: {raw[0x12]} -> DDR5')

        speed = word(raw, 0x15)
        configured = word(raw, 0x20) if header_len >= 0x22 else None
        if target_speed:
            if speed and speed not in (0, 0xFFFF):
                set_word(new_raw, 0x15, target_speed)
                if speed != target_speed:
                    entry_changes.append(f'Memory Speed: {speed} MT/s -> {target_speed} MT/s')
            if configured and configured not in (0, 0xFFFF):
                set_word(new_raw, 0x20, target_speed)
                if configured != target_speed:
                    entry_changes.append(f'Memory Configured Speed: {configured} MT/s -> {target_speed} MT/s')

        if entry_changes:
            blocks[i] = bytes(new_raw)
            changes.extend(entry_changes)

    return join_blocks(ep, blocks, tail), changes


def read_smbios_tables():
    with OUT_PATH.open('wb') as out:
        for path in ('/sys/firmware/dmi/tables/smbios_entry_point', '/sys/firmware/dmi/tables/DMI'):
            out.write(Path(path).read_bytes())
    return OUT_PATH.read_bytes()


def main():
    print('=' * 70)
    print('SMBIOS Tool- 生成随机硬件信息')
    print('=' * 70)
    print('特性: 标准UUID生成 / 主板品牌Profile / CPU Type4 / 内存 Type17')
    print('=' * 70)
    print(f'检测到主板品牌Profile: {detect_brand()}')
    print('提示: 本工具处理 SMBIOS/DMI；硬盘物理序列号需在虚拟磁盘/控制器配置中单独处理。')

    try:
        data = read_smbios_tables()
    except PermissionError:
        print('错误: 权限不足 (需 sudo)')
        sys.exit(1)
    except Exception as exc:
        print(f'错误: 无法读取 DMI 表: {exc}')
        sys.exit(1)

    print('\n[1] 读取SMBIOS数据成功')
    change_log = []

    steps = (
        ('\n[2] 修改 UUID...', lambda blob: modify_uuid(blob), lambda c: f'UUID           : {c[0]} -> {c[1]}'),
        ('\n[3] 修改系统序列号...', None, None),
        ('\n[4] 修改主板资产标签...', lambda blob: modify_board_asset_tag(blob), lambda c: f'Asset Tag      : {c[0]} -> {c[1]}'),
        ('\n[5] 修改产品名称...', lambda blob: modify_product_name(blob), lambda c: f'Product Name   : {c[0]} -> {c[1]}'),
        ('\n[6] 修改CPU信息...', lambda blob: modify_cpu(blob), None),
        ('\n[7] 修改内存信息...', lambda blob: modify_memory(blob), None),
    )

    for title, func, formatter in steps:
        print(title)
        if func is None:
            for field, label in (('product_serial', 'Product Serial'), ('board_serial', 'Board Serial'), ('chassis_serial', 'Chassis Serial')):
                data, change = modify_sysfs_field(data, field, label)
                if change:
                    change_log.append(f'{label}: {change[0]} -> {change[1]}')
            continue
        data, result = func(data)
        if isinstance(result, list):
            prefix = 'Memory Detail  : ' if '内存' in title else ''
            change_log.extend(f'{prefix}{item}' for item in result)
        elif result:
            change_log.append(formatter(result))

    if not change_log:
        print('\n⚠️  未检测到可修改的有效特征，或数据未发生变化。')
        return

    print('\n' + '-' * 70)
    print('修改详情:')
    for item in change_log:
        print(f'  [+] {item}')

    data, ep_change = fix_entry_point(data)
    if ep_change and ep_change[0] != ep_change[1]:
        print(f'  [*] Entry Point   : table length {ep_change[0]} -> {ep_change[1]} 字节，校验和已重算')

    OUT_PATH.write_bytes(data)
    print('\n' + '-' * 70)
    print(f'✅ 修改完成，文件已保存至: {OUT_PATH}')
    print(f'📊 总共修改了 {len(change_log)} 个字段')


if __name__ == '__main__':
    main()
PY

# ==================== Shell 部分：只更新虚拟机 XML 的 -smbios 参数 ====================
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

if run_virsh list --all --name | grep -qx "win11"; then
    VM_NAME="win11"
elif run_virsh list --all --name | grep -qx "win10"; then
    VM_NAME="win10"
else
    print_message "未找到 win10 或 win11 虚拟机，请检查名称。" "$RED"
    exit 1
fi

NEW_SMBIOS_PATH="/usr/local/bin/smbios.bin"
if [ ! -f "$NEW_SMBIOS_PATH" ]; then
    print_message "❌ smbios.bin文件不存在，请先运行菜单3或菜单4生成" "$RED"
    exit 1
fi
if ! command -v virt-xml >/dev/null 2>&1; then
    print_message "❌ virt-xml 未安装" "$RED"
    echo "Ubuntu/Debian: sudo apt install virtinst"
    echo "Arch/Manjaro:  sudo pacman -S virt-install"
    exit 1
fi
print_message "✅ 已找到smbios.bin文件" "$GREEN"

TMP_XML="/tmp/${VM_NAME}_backup.xml"
run_virsh dumpxml "$VM_NAME" > "$TMP_XML"

echo -e "\n${BLUE}=== 开始更新虚拟机 SMBIOS 配置 ===${NC}"

if grep -E '<qemu:commandline[[:space:]]*>' "$TMP_XML" >/dev/null 2>&1; then
    echo "✅ 找到qemu:commandline节点..."
    if grep "value=['\"]-smbios['\"]" "$TMP_XML" >/dev/null 2>&1; then
        echo "  找到现有的-smbios配置，正在更新路径..."
        awk -v new_path="$NEW_SMBIOS_PATH" '
        BEGIN { in_smbios = 0 }
        /value=["\047]-smbios["\047]/ { in_smbios = 1; print; next }
        in_smbios && /value=["\047]file=/ {
            if (match($0, /value=(["'"'"'"])/)) {
                quote = substr($0, RSTART+6, 1)
                sub(/value=['"'"'"]file=[^'"'"'"]*['"'"'"]/, "value=" quote "file=" new_path quote)
            }
            print; in_smbios = 0; next
        }
        { print }
        ' "$TMP_XML" > "${TMP_XML}.new" && mv "${TMP_XML}.new" "$TMP_XML"
    else
        echo "  未找到现有的-smbios配置，正在添加新配置..."
        sed -i '/<qemu:commandline[[:space:]]*>/a\
        <qemu:arg value="-smbios"/>\
        <qemu:arg value="'"file=$NEW_SMBIOS_PATH"'"/>' "$TMP_XML"
    fi

    if run_virsh define "$TMP_XML" > /dev/null 2>&1; then
        print_message "✅ SMBIOS配置已成功更新" "$GREEN"
    else
        print_message "❌ SMBIOS配置更新失败" "$RED"
    fi
else
    echo "⚠️  未找到qemu:commandline节点，正在创建并添加配置..."
    if sudo virt-xml "$VM_NAME" --edit --qemu-commandline="
    -smbios
    file=$NEW_SMBIOS_PATH
    "; then
        print_message "✅ SMBIOS配置已成功添加" "$GREEN"
    else
        print_message "❌ SMBIOS配置添加失败" "$RED"
    fi
fi

echo -e "\n${BLUE}=== 修改总结 ===${NC}"
echo -e "${GREEN}虚拟机: $VM_NAME${NC}"
echo -e "${GREEN}SMBIOS文件: $NEW_SMBIOS_PATH${NC}"
rm -f "$TMP_XML" "${TMP_XML}.new" 2>/dev/null

echo -e "\n${GREEN}✅ SMBIOS 文件生成完成！${NC}"
echo -e "${YELLOW}提示: 请重启虚拟机使 SMBIOS 更改生效${NC}"
read -p "按回车键返回主菜单..."
