"""
Parser and serializer for Skyrim (.ess) save files (LE, SE, and AE).

Preserves all non-quest data (player level, stats, inventory/items, NPCs,
cells, world state, Papyrus VM, screenshot) byte-for-byte, while allowing
inspection and modification of QUST ChangeForms and RefID/FormID tables.
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field

from lz4_block import compress_block, decompress_block

MAGIC = b"TESV_SAVEGAME"
CHANGEFORM_TYPE_QUST = 8


# ---------------------------------------------------------------------------
# Low-level binary helpers
# ---------------------------------------------------------------------------

class BinaryReader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos
        self.length = len(data)

    def remaining(self) -> int:
        return self.length - self.pos

    def read_bytes(self, n: int) -> bytes:
        if self.pos + n > self.length:
            raise EOFError(
                f"Unexpected EOF at {self.pos}: wanted {n} bytes, {self.remaining()} left"
            )
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def read_u8(self) -> int:
        if self.pos + 1 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 1 byte")
        val = self.data[self.pos]
        self.pos += 1
        return val

    def read_u16(self) -> int:
        if self.pos + 2 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 2 bytes")
        val = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return val

    def read_i16(self) -> int:
        if self.pos + 2 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 2 bytes")
        val = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return val

    def read_u32(self) -> int:
        if self.pos + 4 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 4 bytes")
        val = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_u64(self) -> int:
        if self.pos + 8 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 8 bytes")
        val = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_f32(self) -> float:
        if self.pos + 4 > self.length:
            raise EOFError(f"Unexpected EOF at {self.pos}: wanted 4 bytes")
        val = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_wstring(self) -> str:
        length = self.read_u16()
        raw = self.read_bytes(length)
        return raw.decode("windows-1252", errors="replace")

    def read_wstring_raw(self) -> bytes:
        length = self.read_u16()
        return self.read_bytes(length)

    def read_vsval(self) -> int:
        b0 = self.read_u8()
        kind = b0 & 0x03
        if kind == 0:
            return b0 >> 2
        elif kind == 1:
            b1 = self.read_u8()
            return (b0 | (b1 << 8)) >> 2
        elif kind == 2:
            b1 = self.read_u8()
            b2 = self.read_u8()
            return (b0 | (b1 << 8) | (b2 << 16)) >> 2
        else:
            raise ValueError(f"Invalid vsval type bits (3) at offset {self.pos - 1}")

    def read_refid(self) -> RefID:
        b0, b1, b2 = self.read_bytes(3)
        return RefID.from_bytes(b0, b1, b2)


class BinaryWriter:
    def __init__(self) -> None:
        self.buf = io.BytesIO()

    def tell(self) -> int:
        return self.buf.tell()

    def write_bytes(self, data: bytes) -> None:
        self.buf.write(data)

    def write_u8(self, val: int) -> None:
        self.buf.write(struct.pack("<B", val))

    def write_u16(self, val: int) -> None:
        self.buf.write(struct.pack("<H", val))

    def write_i16(self, val: int) -> None:
        self.buf.write(struct.pack("<h", val))

    def write_u32(self, val: int) -> None:
        self.buf.write(struct.pack("<I", val))

    def write_u64(self, val: int) -> None:
        self.buf.write(struct.pack("<Q", val))

    def write_f32(self, val: float) -> None:
        self.buf.write(struct.pack("<f", val))

    def write_wstring(self, text: str) -> None:
        raw = text.encode("windows-1252", errors="replace")
        self.write_u16(len(raw))
        self.write_bytes(raw)

    def write_wstring_raw(self, raw: bytes) -> None:
        self.write_u16(len(raw))
        self.write_bytes(raw)

    def write_vsval(self, val: int) -> None:
        if val < 0:
            raise ValueError(f"vsval cannot be negative: {val}")
        if val < 0x40:
            self.write_u8((val << 2) | 0)
        elif val < 0x4000:
            encoded = (val << 2) | 1
            self.write_u8(encoded & 0xFF)
            self.write_u8((encoded >> 8) & 0xFF)
        elif val < 0x400000:
            encoded = (val << 2) | 2
            self.write_u8(encoded & 0xFF)
            self.write_u8((encoded >> 8) & 0xFF)
            self.write_u8((encoded >> 16) & 0xFF)
        else:
            raise ValueError(f"vsval too large: {val}")

    def write_refid(self, refid: RefID) -> None:
        self.write_bytes(refid.to_bytes())

    def getvalue(self) -> bytes:
        return self.buf.getvalue()


# ---------------------------------------------------------------------------
# RefID & FormID structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RefID:
    """
    3-byte RefID in Skyrim .ess files.
    Upper 2 bits of byte0:
      0 = index into formIDArray (0 -> 0x00000000, else formIDArray[value - 1])
      1 = default (Skyrim.esm, 0x00xxxxxx)
      2 = created in-game (0xFFxxxxxx)
      3 = unknown
    """

    kind: int
    value: int

    @classmethod
    def from_bytes(cls, b0: int, b1: int, b2: int) -> RefID:
        kind = (b0 >> 6) & 0x03
        value = ((b0 & 0x3F) << 16) | (b1 << 8) | b2
        return cls(kind=kind, value=value)

    def to_bytes(self) -> bytes:
        b0 = ((self.kind & 0x03) << 6) | ((self.value >> 16) & 0x3F)
        b1 = (self.value >> 8) & 0xFF
        b2 = self.value & 0xFF
        return bytes((b0, b1, b2))

    def is_null(self) -> bool:
        return self.kind == 0 and self.value == 0


# ---------------------------------------------------------------------------
# Save file data structures
# ---------------------------------------------------------------------------

@dataclass
class EssHeader:
    version: int
    save_number: int
    player_name: str
    player_level: int
    player_location: str
    game_date: str
    player_race: str
    player_sex: int
    player_cur_exp: float
    player_lvl_up_exp: float
    filetime: int
    shot_width: int
    shot_height: int
    compression_type: int = 0  # SE+ only (0=None, 1=zLib, 2=LZ4)
    raw_bytes: bytes = b""     # Exact header bytes for lossless roundtrip


@dataclass
class GlobalDataEntry:
    data_type: int
    data: bytes


@dataclass
class ChangeForm:
    form_id: RefID
    change_flags: int
    type_byte: int
    version: int
    data: bytes          # Raw payload as stored (possibly zlib-compressed if length2 != 0)
    is_compressed: bool  # True if length2 != 0 on disk

    @property
    def form_type(self) -> int:
        return self.type_byte & 0x3F

    def get_uncompressed_data(self) -> bytes:
        if self.is_compressed:
            return zlib.decompress(self.data)
        return self.data

    def set_uncompressed_data(self, raw: bytes, compress: bool | None = None) -> None:
        if compress is None:
            compress = self.is_compressed
        if compress:
            self.data = zlib.compress(raw)
            self.is_compressed = True
            self._uncompressed_len = len(raw)
        else:
            self.data = raw
            self.is_compressed = False
            self._uncompressed_len = 0

    # Track original uncompressed length when read from disk
    _uncompressed_len: int = 0


@dataclass
class EssFile:
    header: EssHeader
    screenshot_data: bytes
    form_version: int
    plugins: list[str]
    light_plugins: list[str]
    has_light_plugins: bool
    plugin_info_includes_light: bool
    global_data_1: list[GlobalDataEntry]
    global_data_2: list[GlobalDataEntry]
    change_forms: list[ChangeForm]
    global_data_3: list[GlobalDataEntry]
    global_data_3_count_field: int
    form_id_array: list[int]
    visited_worldspaces: list[int]
    unknown3_table_raw: bytes
    unused_location_fields: bytes
    offset_base: int = 0  # Difference between body-relative pos and stored FileLocationTable offsets

    def resolve_refid(self, refid: RefID) -> int:
        """Resolve a 3-byte RefID to a 4-byte FormID."""
        if refid.kind == 0:
            if refid.value == 0:
                return 0
            idx = refid.value - 1
            if idx >= len(self.form_id_array):
                raise IndexError(
                    f"RefID index {idx} out of range for formIDArray (len {len(self.form_id_array)})"
                )
            return self.form_id_array[idx]
        elif refid.kind == 1:
            return refid.value & 0x003FFFFF
        elif refid.kind == 2:
            return 0xFF000000 | (refid.value & 0x003FFFFF)
        else:
            return refid.value

    def get_or_add_formid_ref(self, form_id: int) -> RefID:
        """
        Convert a 4-byte FormID into a 3-byte RefID for this save,
        appending to `form_id_array` if needed.
        """
        if form_id == 0:
            return RefID(kind=0, value=0)
        mod_idx = (form_id >> 24) & 0xFF
        local_id = form_id & 0x00FFFFFF
        if mod_idx == 0 and local_id <= 0x3FFFFF:
            return RefID(kind=1, value=local_id)
        if mod_idx == 0xFF and local_id <= 0x3FFFFF:
            return RefID(kind=2, value=local_id)

        try:
            idx = self.form_id_array.index(form_id)
        except ValueError:
            self.form_id_array.append(form_id)
            idx = len(self.form_id_array) - 1
        return RefID(kind=0, value=idx + 1)


# ---------------------------------------------------------------------------
# Plugin / FormID remapping helper
# ---------------------------------------------------------------------------

def build_plugin_remap(donor: EssFile, base: EssFile) -> dict[int, int]:
    """
    Build a mapping from donor's 1-byte plugin index (0x00..0xFE) to base's
    plugin index, plus ESL (0xFE) sub-index remapping if applicable.

    Returns a dict mapping donor's full mod prefix:
      - For normal plugins: key = (0x00..0xFD) << 24, val = base_idx << 24
      - For ESL plugins (0xFE): key = (0xFE << 24) | (esl_idx << 12), val = (0xFE << 24) | (base_esl_idx << 12)
    """
    base_plugins_lower = {name.lower(): i for i, name in enumerate(base.plugins)}
    base_light_lower = {name.lower(): i for i, name in enumerate(base.light_plugins)}

    remap: dict[int, int] = {}
    for i, name in enumerate(donor.plugins):
        b_idx = base_plugins_lower.get(name.lower())
        if b_idx is not None:
            remap[i << 24] = b_idx << 24

    for i, name in enumerate(donor.light_plugins):
        b_idx = base_light_lower.get(name.lower())
        if b_idx is not None:
            remap[(0xFE << 24) | (i << 12)] = (0xFE << 24) | (b_idx << 12)

    return remap


def remap_formid(form_id: int, plugin_remap: dict[int, int]) -> int:
    """Remap a 4-byte FormID from donor plugin indices to base plugin indices."""
    if form_id == 0:
        return 0
    mod_idx = (form_id >> 24) & 0xFF
    if mod_idx == 0xFF or mod_idx == 0x00:
        return form_id
    if mod_idx == 0xFE:
        key = form_id & 0xFFFFF000
        if key in plugin_remap:
            return plugin_remap[key] | (form_id & 0x00000FFF)
        return form_id
    key = form_id & 0xFF000000
    if key in plugin_remap:
        return plugin_remap[key] | (form_id & 0x00FFFFFF)
    return form_id


# ---------------------------------------------------------------------------
# Parsing & Serialization
# ---------------------------------------------------------------------------

def parse_ess(path: str) -> EssFile:
    with open(path, "rb") as f:
        raw = f.read()
    return parse_ess_bytes(raw)


def parse_ess_bytes(raw: bytes) -> EssFile:
    r = BinaryReader(raw)
    magic = r.read_bytes(len(MAGIC))
    if magic != MAGIC:
        raise ValueError(f"Invalid .ess magic: {magic!r}")

    header_size = r.read_u32()
    header_bytes = r.read_bytes(header_size)
    hr = BinaryReader(header_bytes)

    version = hr.read_u32()
    save_number = hr.read_u32()
    player_name = hr.read_wstring()
    player_level = hr.read_u32()
    player_location = hr.read_wstring()
    game_date = hr.read_wstring()
    player_race = hr.read_wstring()
    player_sex = hr.read_u16()
    player_cur_exp = hr.read_f32()
    player_lvl_up_exp = hr.read_f32()
    filetime = hr.read_u64()
    shot_width = hr.read_u32()
    shot_height = hr.read_u32()
    compression_type = hr.read_u16() if version >= 12 and hr.remaining() >= 2 else 0

    header = EssHeader(
        version=version,
        save_number=save_number,
        player_name=player_name,
        player_level=player_level,
        player_location=player_location,
        game_date=game_date,
        player_race=player_race,
        player_sex=player_sex,
        player_cur_exp=player_cur_exp,
        player_lvl_up_exp=player_lvl_up_exp,
        filetime=filetime,
        shot_width=shot_width,
        shot_height=shot_height,
        compression_type=compression_type,
        raw_bytes=header_bytes,
    )

    bpp = 4 if version >= 12 else 3
    screenshot_data = r.read_bytes(bpp * shot_width * shot_height)

    # Body (may be compressed in SE/AE)
    body_file_offset = r.pos
    if version >= 12 and compression_type != 0:
        uncompressed_len = r.read_u32()
        compressed_len = r.read_u32()
        compressed_body = r.read_bytes(compressed_len)
        if compression_type == 2:
            body_bytes = decompress_block(compressed_body, uncompressed_len)
        elif compression_type == 1:
            body_bytes = zlib.decompress(compressed_body)
        else:
            raise ValueError(f"Unsupported compression type: {compression_type}")
        br = BinaryReader(body_bytes)
    else:
        body_bytes = raw[r.pos :]
        br = BinaryReader(body_bytes)

    form_version = br.read_u8()
    plugin_info_size = br.read_u32()
    plugin_block_start = br.pos

    plugin_count = br.read_u8()
    plugins = [br.read_wstring() for _ in range(plugin_count)]

    light_plugins: list[str] = []
    has_light_plugins = False
    plugin_info_includes_light = False

    consumed_in_plugin_block = br.pos - plugin_block_start
    if consumed_in_plugin_block < plugin_info_size:
        # Light plugins are inside pluginInfoSize
        light_count = br.read_u16()
        light_plugins = [br.read_wstring() for _ in range(light_count)]
        has_light_plugins = True
        plugin_info_includes_light = True
        # Ensure we align with plugin_info_size
        br.pos = plugin_block_start + plugin_info_size
    elif version >= 12 and form_version >= 78:
        # Light plugins immediately follow pluginInfoSize
        light_count = br.read_u16()
        light_plugins = [br.read_wstring() for _ in range(light_count)]
        has_light_plugins = True
        plugin_info_includes_light = False

    # File Location Table (100 bytes)
    form_id_array_count_offset = br.read_u32()
    unknown_table3_offset = br.read_u32()
    gd1_offset = br.read_u32()
    gd2_offset = br.read_u32()
    change_forms_offset = br.read_u32()
    gd3_offset = br.read_u32()
    gd1_count = br.read_u32()
    gd2_count = br.read_u32()
    gd3_count_field = br.read_u32()
    change_form_count = br.read_u32()
    unused_location_fields = br.read_bytes(15 * 4)

    # Calibrate offset_base (for LE, stored offsets are absolute from file start;
    # for SE compressed, stored offsets are relative to start of uncompressed body).
    offset_base = gd1_offset - br.pos

    def _read_global_data_entry() -> GlobalDataEntry:
        dtype = br.read_u32()
        dlen = br.read_u32()
        dbytes = br.read_bytes(dlen)
        return GlobalDataEntry(data_type=dtype, data=dbytes)

    br.pos = gd1_offset - offset_base
    global_data_1 = [_read_global_data_entry() for _ in range(gd1_count)]

    br.pos = gd2_offset - offset_base
    global_data_2 = [_read_global_data_entry() for _ in range(gd2_count)]

    br.pos = change_forms_offset - offset_base
    change_forms: list[ChangeForm] = []
    for _ in range(change_form_count):
        cf_form_id = br.read_refid()
        cf_flags = br.read_u32()
        cf_type_byte = br.read_u8()
        cf_version = br.read_u8()

        len_kind = (cf_type_byte >> 6) & 0x03
        if len_kind == 0:
            len1 = br.read_u8()
            len2 = br.read_u8()
        elif len_kind == 1:
            len1 = br.read_u16()
            len2 = br.read_u16()
        elif len_kind == 2:
            len1 = br.read_u32()
            len2 = br.read_u32()
        else:
            raise ValueError(f"Invalid ChangeForm length kind {len_kind}")

        cf_data = br.read_bytes(len1)
        change_forms.append(
            ChangeForm(
                form_id=cf_form_id,
                change_flags=cf_flags,
                type_byte=cf_type_byte,
                version=cf_version,
                data=cf_data,
                is_compressed=(len2 != 0),
                _uncompressed_len=len2,
            )
        )

    br.pos = gd3_offset - offset_base
    form_id_array_target = form_id_array_count_offset - offset_base
    global_data_3: list[GlobalDataEntry] = []
    # Read all GlobalData3 entries up to formIDArrayCountOffset (handles Bethesda's +1 Papyrus bug)
    while br.pos < form_id_array_target:
        global_data_3.append(_read_global_data_entry())

    br.pos = form_id_array_target
    form_id_count = br.read_u32()
    form_id_array = [br.read_u32() for _ in range(form_id_count)]

    visited_count = br.read_u32()
    visited_worldspaces = [br.read_u32() for _ in range(visited_count)]

    unknown3_size = br.read_u32()
    unknown3_table_raw = br.read_bytes(unknown3_size)

    # Note: for uncompressed saves, offset_base equals body_file_offset
    _ = body_file_offset

    return EssFile(
        header=header,
        screenshot_data=screenshot_data,
        form_version=form_version,
        plugins=plugins,
        light_plugins=light_plugins,
        has_light_plugins=has_light_plugins,
        plugin_info_includes_light=plugin_info_includes_light,
        global_data_1=global_data_1,
        global_data_2=global_data_2,
        change_forms=change_forms,
        global_data_3=global_data_3,
        global_data_3_count_field=gd3_count_field,
        form_id_array=form_id_array,
        visited_worldspaces=visited_worldspaces,
        unknown3_table_raw=unknown3_table_raw,
        unused_location_fields=unused_location_fields,
        offset_base=offset_base,
    )


def write_ess(ess: EssFile, path: str) -> None:
    raw = serialize_ess(ess)
    with open(path, "wb") as f:
        f.write(raw)


def serialize_ess(ess: EssFile) -> bytes:
    # 1. Serialize body first so we can compute exact FileLocationTable offsets
    bw = BinaryWriter()
    bw.write_u8(ess.form_version)

    # Plugin info
    pw = BinaryWriter()
    pw.write_u8(len(ess.plugins))
    for p in ess.plugins:
        pw.write_wstring(p)
    if ess.has_light_plugins and ess.plugin_info_includes_light:
        pw.write_u16(len(ess.light_plugins))
        for lp in ess.light_plugins:
            pw.write_wstring(lp)
    plugin_bytes = pw.getvalue()

    bw.write_u32(len(plugin_bytes))
    bw.write_bytes(plugin_bytes)

    if ess.has_light_plugins and not ess.plugin_info_includes_light:
        bw.write_u16(len(ess.light_plugins))
        for lp in ess.light_plugins:
            bw.write_wstring(lp)

    # Reserve 100 bytes for FileLocationTable
    flt_pos = bw.tell()
    bw.write_bytes(b"\x00" * 100)

    gd1_pos = bw.tell()
    for entry in ess.global_data_1:
        bw.write_u32(entry.data_type)
        bw.write_u32(len(entry.data))
        bw.write_bytes(entry.data)

    gd2_pos = bw.tell()
    for entry in ess.global_data_2:
        bw.write_u32(entry.data_type)
        bw.write_u32(len(entry.data))
        bw.write_bytes(entry.data)

    cf_pos = bw.tell()
    for cf in ess.change_forms:
        len1 = len(cf.data)
        len2 = cf._uncompressed_len if cf.is_compressed else 0
        max_len = max(len1, len2)
        if max_len <= 0xFF:
            len_kind = 0
        elif max_len <= 0xFFFF:
            len_kind = 1
        else:
            len_kind = 2
        type_byte = (len_kind << 6) | (cf.type_byte & 0x3F)

        bw.write_refid(cf.form_id)
        bw.write_u32(cf.change_flags)
        bw.write_u8(type_byte)
        bw.write_u8(cf.version)
        if len_kind == 0:
            bw.write_u8(len1)
            bw.write_u8(len2)
        elif len_kind == 1:
            bw.write_u16(len1)
            bw.write_u16(len2)
        else:
            bw.write_u32(len1)
            bw.write_u32(len2)
        bw.write_bytes(cf.data)

    gd3_pos = bw.tell()
    for entry in ess.global_data_3:
        bw.write_u32(entry.data_type)
        bw.write_u32(len(entry.data))
        bw.write_bytes(entry.data)

    form_id_array_pos = bw.tell()
    bw.write_u32(len(ess.form_id_array))
    for fid in ess.form_id_array:
        bw.write_u32(fid)

    bw.write_u32(len(ess.visited_worldspaces))
    for ws in ess.visited_worldspaces:
        bw.write_u32(ws)

    unknown3_pos = bw.tell()
    bw.write_u32(len(ess.unknown3_table_raw))
    bw.write_bytes(ess.unknown3_table_raw)

    body_bytes = bytearray(bw.getvalue())

    # Compute offset_base for FileLocationTable
    # If uncompressed, offset_base is the start of the body in the final file.
    # If compressed, offset_base is 0 (or whatever ess.offset_base was).
    out = BinaryWriter()
    out.write_bytes(MAGIC)
    out.write_u32(len(ess.header.raw_bytes))
    out.write_bytes(ess.header.raw_bytes)
    out.write_bytes(ess.screenshot_data)

    if ess.header.version >= 12 and ess.header.compression_type != 0:
        base_off = ess.offset_base
    else:
        base_off = out.tell()

    flt_bytes = struct.pack(
        "<10I",
        form_id_array_pos + base_off,
        unknown3_pos + base_off,
        gd1_pos + base_off,
        gd2_pos + base_off,
        cf_pos + base_off,
        gd3_pos + base_off,
        len(ess.global_data_1),
        len(ess.global_data_2),
        ess.global_data_3_count_field,
        len(ess.change_forms),
    ) + ess.unused_location_fields

    body_bytes[flt_pos : flt_pos + 100] = flt_bytes
    final_body = bytes(body_bytes)

    if ess.header.version >= 12 and ess.header.compression_type != 0:
        if ess.header.compression_type == 2:
            compressed = compress_block(final_body)
        elif ess.header.compression_type == 1:
            compressed = zlib.compress(final_body)
        else:
            raise ValueError(f"Unsupported compression type: {ess.header.compression_type}")
        out.write_u32(len(final_body))
        out.write_u32(len(compressed))
        out.write_bytes(compressed)
    else:
        out.write_bytes(final_body)

    return out.getvalue()
