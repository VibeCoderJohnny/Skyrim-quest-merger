"""
Parser/serializer for the Papyrus VM section (Global Data type 1001) of Skyrim SE/AE saves.

Layout follows ReSaver++ (Tonycubed2/ResaverPP, src/core/Papyrus.cpp, Apache-2.0, a port of
ReSaver by Mark Fairchild). SE: string-table indices are u32, object/array ids are u64,
thread ids are u32.

parse_papyrus() -> Papyrus, Papyrus.write() -> bytes. A faithful parse must re-serialize to
exactly the original bytes (verified by the tests / `python papyrus.py <save.ess>`).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# Variable types
V_NULL, V_REF, V_STRING, V_INT, V_FLOAT, V_BOOL, V_VARIANT, V_STRUCT = 0, 1, 2, 3, 4, 5, 6, 7
V_REF_ARRAY, V_STRING_ARRAY, V_INT_ARRAY, V_FLOAT_ARRAY, V_BOOL_ARRAY = 11, 12, 13, 14, 15
V_VARIANT_ARRAY, V_STRUCT_ARRAY = 16, 17

INVALID_HEADER = 0xFFFF

# Papyrus opcode argument counts (negative = that many fixed args + variable tail)
OPCODE_ARGS = [
    0, 3, 3, 3, 3, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 1, 2, 2, -3, -2, -3, 1, 3, 3, 3,
    2, 2, 3, 3, 4, 4, 3, 1, 3, 3, 5, 5, 3, 3, 1, 3, 1, 6,
]


class PapyrusError(Exception):
    pass


def is_ref_type(t: int) -> bool:
    return t in (V_REF, V_REF_ARRAY, V_STRUCT, V_STRUCT_ARRAY)


class R:
    """Little-endian bounds-checked reader."""

    __slots__ = ("d", "p", "n")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.d = data
        self.p = pos
        self.n = len(data)

    def need(self, k: int) -> None:
        if self.n - self.p < k:
            raise PapyrusError(f"unexpected end of data at {self.p} (needed {k})")

    def u8(self) -> int:
        self.need(1)
        v = self.d[self.p]
        self.p += 1
        return v

    def i8(self) -> int:
        v = self.u8()
        return v - 256 if v >= 128 else v

    def u16(self) -> int:
        self.need(2)
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self) -> int:
        self.need(4)
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def u64(self) -> int:
        self.need(8)
        v = struct.unpack_from("<Q", self.d, self.p)[0]
        self.p += 8
        return v

    def bytes(self, k: int) -> bytes:
        self.need(k)
        v = self.d[self.p : self.p + k]
        self.p += k
        return v

    def remaining(self) -> int:
        return self.n - self.p

    def wstring(self) -> bytes:
        k = self.u16()
        return self.bytes(k)

    def lstring(self) -> bytes:
        k = self.u32()
        return self.bytes(k)


class W:
    __slots__ = ("b",)

    def __init__(self) -> None:
        self.b = bytearray()

    def u8(self, v: int) -> None:
        self.b.append(v & 0xFF)

    def i8(self, v: int) -> None:
        self.b.append(v & 0xFF)

    def u16(self, v: int) -> None:
        self.b += struct.pack("<H", v)

    def u32(self, v: int) -> None:
        self.b += struct.pack("<I", v)

    def u64(self, v: int) -> None:
        self.b += struct.pack("<Q", v)

    def raw(self, v: bytes) -> None:
        self.b += v

    def wstring(self, s: bytes) -> None:
        self.u16(len(s))
        self.b += s

    def lstring(self, s: bytes) -> None:
        self.u32(len(s))
        self.b += s

    def getvalue(self) -> bytes:
        return bytes(self.b)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Variable:
    type: int = V_NULL
    value: int = 0          # null payload / int / bool / float bits / string index / EID
    ref_type: int = 0       # script-name string index for ref / ref-array types
    inner: Optional["Variable"] = None  # variant payload

    @staticmethod
    def read(r: R) -> "Variable":
        pos = r.p
        t = r.u8()
        if t > 17 or 8 <= t <= 10:
            raise PapyrusError(f"invalid variable type {t} at {pos}")
        v = Variable(type=t)
        if t == V_STRING:
            v.value = r.u32()
        elif t in (V_NULL, V_INT, V_FLOAT, V_BOOL):
            v.value = r.u32()
        elif t in (V_REF, V_STRUCT):
            v.ref_type = r.u32()
            v.value = r.u64()
        elif t == V_VARIANT:
            v.inner = Variable.read(r)
        else:  # arrays
            if is_ref_type(t):
                v.ref_type = r.u32()
            v.value = r.u64()
        return v

    def write(self, w: W) -> None:
        t = self.type
        w.u8(t)
        if t == V_STRING:
            w.u32(self.value)
        elif t in (V_NULL, V_INT, V_FLOAT, V_BOOL):
            w.u32(self.value)
        elif t in (V_REF, V_STRUCT):
            w.u32(self.ref_type)
            w.u64(self.value)
        elif t == V_VARIANT:
            (self.inner if self.inner is not None else Variable()).write(w)
        else:
            if is_ref_type(t):
                w.u32(self.ref_type)
            w.u64(self.value)


def read_vars(r: R, n: int) -> list[Variable]:
    return [Variable.read(r) for _ in range(n)]


def write_vars(w: W, vs: list[Variable]) -> None:
    for v in vs:
        v.write(w)


@dataclass
class MemberDesc:
    name: int
    type: int


@dataclass
class Script:
    name: int
    parent: int
    members: list[MemberDesc]


@dataclass
class InstanceData:
    flag: int = 0
    state: int = 0
    unknown1: int = 0
    unknown2: int = 0
    vars: list[Variable] = field(default_factory=list)


@dataclass
class ScriptInstance:
    id: int
    script: int
    unknown2bits: int
    unknown: int
    ref_id: bytes          # 3 raw bytes
    unknown_byte: int
    data: Optional[InstanceData] = None


@dataclass
class Reference:
    id: int
    script: int
    data: Optional[InstanceData] = None


@dataclass
class ArrayInfo:
    id: int
    type: int
    ref_type: int
    length: int
    vars: list[Variable] = field(default_factory=list)
    has_data: bool = False


@dataclass
class ActiveScript:
    id: int
    type: int
    data: Optional[bytes] = None   # raw thread data (kept verbatim; not interpreted)


# ---------------------------------------------------------------------------
# Instance data
# ---------------------------------------------------------------------------

def read_instance_data(r: R) -> InstanceData:
    d = InstanceData()
    d.flag = r.u8()
    d.state = r.u32()
    d.unknown1 = r.u32()
    d.unknown2 = r.u32() if (d.flag & 0x04) else 0
    n = r.u32()
    d.vars = read_vars(r, n)
    return d


def write_instance_data(w: W, id_: int, d: InstanceData) -> None:
    w.u64(id_)
    w.u8(d.flag)
    w.u32(d.state)
    w.u32(d.unknown1)
    if d.flag & 0x04:
        w.u32(d.unknown2)
    w.u32(len(d.vars))
    write_vars(w, d.vars)


# ---------------------------------------------------------------------------
# Thread (active script) data: only its extent is needed, so it is parsed to find
# where it ends, and kept as raw bytes. Parsing follows ReadActiveData / ReadFrame.
# ---------------------------------------------------------------------------

def _skip_param(r: R) -> None:
    t = r.u8()
    if t == 0:
        return
    if t in (1, 2, 8):
        r.u32()
    elif t in (3, 4):
        r.u32()
    elif t == 5:
        r.u8()
    else:
        raise PapyrusError(f"invalid opcode parameter type {t} at {r.p}")


def _skip_opcode(r: R) -> None:
    op = r.u8()
    if op >= 48:
        raise PapyrusError(f"invalid opcode {op} at {r.p}")
    args = OPCODE_ARGS[op]
    extra = args < 0
    fixed = 1 - args if extra else args
    last_type = None
    last_val = 0
    for _ in range(fixed):
        pos = r.p
        t = r.u8()
        val = 0
        if t == 0:
            pass
        elif t in (1, 2, 8):
            r.u32()
        elif t in (3, 4):
            val = r.u32()
        elif t == 5:
            r.u8()
        else:
            raise PapyrusError(f"invalid opcode parameter type {t} at {pos}")
        last_type, last_val = t, val
    if extra and fixed > 0:
        if last_type != 3:
            raise PapyrusError("variable-argument opcode without an integer count")
        count = last_val - (1 << 32) if last_val >= (1 << 31) else last_val
        for _ in range(count):
            _skip_param(r)


def _skip_frame(r: R) -> None:
    var_count = r.u32()
    if var_count > 50000:
        raise PapyrusError(f"invalid stack frame variable count at {r.p}")
    flag = r.u8()
    fn_type = r.u8()
    r.u32()  # script name
    r.u32()  # base name
    r.u32()  # event
    if not (flag & 1) and fn_type == 0:
        r.u32()  # status
    r.u8()   # opcode major
    r.u8()   # opcode minor
    r.u32()  # return type
    r.u32()  # doc string
    r.u32()  # user flags
    r.u8()   # fn flags
    for _ in range(r.u16()):
        r.u32(); r.u32()
    for _ in range(r.u16()):
        r.u32(); r.u32()
    for _ in range(r.u16()):
        _skip_opcode(r)
    r.u32()  # ptr
    Variable.read(r)  # owner
    read_vars(r, var_count)


def read_fragment_len(r: R, kind: int) -> None:
    if kind == 2:
        return
    type_code = r.lstring()
    if type_code == b"QuestStage":
        r.bytes(3 + 2 + 1)
    elif type_code in (b"ScenePhaseResults", b"SceneActionResults"):
        r.bytes(3 + 4)
    elif type_code == b"SceneResults":
        r.bytes(3)
    elif type_code == b"TerminalRunResults":
        r.bytes(1 + 4 + 3 + 4)
    elif type_code == b"TopicInfo":
        pass
    else:
        raise PapyrusError(f"unknown fragment task type {type_code!r} at {r.p}")


def skip_active_data(r: R) -> tuple[int, bool]:
    """Advance over one thread's data (after its u32 id). Returns (frame_count, has_unknown5)."""
    major = r.i8()
    minor = r.i8()
    if major > 0 and (minor < 1 or minor > 2):
        raise PapyrusError(f"bad active script minor version {minor} at {r.p}")
    Variable.read(r)
    flag = r.u8()
    r.u8()
    if flag & 1:
        r.u32()
    unknown3 = r.u8()
    if 1 <= unknown3 <= 3:
        read_fragment_len(r, unknown3)
        if unknown3 in (2, 3):
            Variable.read(r)
    count = r.u32()
    if count > 100000:
        raise PapyrusError(f"invalid stack frame count at {r.p}")
    first_fn_flags = 0
    for i in range(count):
        start = r.p
        _skip_frame(r)
        if i == 0:
            # re-read just fn_flags of the first frame for the "native bottom frame" test
            first_fn_flags = _frame_fn_flags(r.d, start)
    has_u5 = False
    if count:
        r.u8()
        has_u5 = True
    return count, has_u5


def _frame_fn_flags(data: bytes, start: int) -> int:
    r = R(data, start)
    r.u32()
    flag = r.u8()
    fn_type = r.u8()
    r.u32(); r.u32(); r.u32()
    if not (flag & 1) and fn_type == 0:
        r.u32()
    r.u8(); r.u8()
    r.u32(); r.u32(); r.u32()
    return r.u8()


# ---------------------------------------------------------------------------
# Function messages / suspended stacks
# ---------------------------------------------------------------------------

def _read_fm_data(r: R) -> bytes:
    start = r.p
    r.u8()
    r.u32()
    r.u32()
    Variable.read(r)
    n = r.u32()
    read_vars(r, n)
    return r.d[start : r.p]


def _read_suspended(r: R) -> list[tuple[int, int, Optional[bytes]]]:
    out = []
    for _ in range(r.u32()):
        id_ = r.u32()
        flag = r.u8()
        data = _read_fm_data(r) if flag != 0 else None
        out.append((id_, flag, data))
    return out


def _write_suspended(w: W, items) -> None:
    w.u32(len(items))
    for id_, flag, data in items:
        w.u32(id_)
        w.u8(flag)
        if data is not None:
            w.raw(data)


# ---------------------------------------------------------------------------
# Papyrus section
# ---------------------------------------------------------------------------

@dataclass
class Papyrus:
    header: int = 0
    strings: list[bytes] = field(default_factory=list)
    scripts: list[Script] = field(default_factory=list)
    instances: list[ScriptInstance] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    arrays: list[ArrayInfo] = field(default_factory=list)
    runtime: int = 0
    active: list[ActiveScript] = field(default_factory=list)
    function_messages: list[tuple] = field(default_factory=list)  # (flag, id|None, flags, data|None)
    suspended1: list = field(default_factory=list)
    suspended2: list = field(default_factory=list)
    unk1: int = 0
    unk2: Optional[int] = None
    unknown_ids: list[int] = field(default_factory=list)
    unbinds: list[tuple[int, int]] = field(default_factory=list)
    save_file_version: Optional[int] = None
    tail: bytes = b""
    invalid: bool = False

    instance_data_order: list[int] = field(default_factory=list)
    reference_data_order: list[int] = field(default_factory=list)
    array_data_order: list[int] = field(default_factory=list)
    active_data_order: list[int] = field(default_factory=list)


def parse_papyrus(data: bytes) -> Papyrus:
    r = R(data)
    p = Papyrus()
    p.header = r.u16()
    if p.header == INVALID_HEADER:
        p.invalid = True
        p.tail = r.bytes(r.remaining())
        return p

    str_count = r.u32()
    p.strings = [r.wstring() for _ in range(str_count)]

    for _ in range(r.u32()):
        name = r.u32()
        parent = r.u32()
        members = [MemberDesc(r.u32(), r.u32()) for _ in range(r.u32())]
        p.scripts.append(Script(name, parent, members))

    for _ in range(r.u32()):
        sid = r.u64()
        script = r.u32()
        u2b = r.u16()
        unk = r.u16()
        ref_id = r.bytes(3)
        ub = r.u8()
        p.instances.append(ScriptInstance(sid, script, u2b, unk, ref_id, ub))

    for _ in range(r.u32()):
        rid = r.u64()
        script = r.u32()
        p.references.append(Reference(rid, script))

    for _ in range(r.u32()):
        aid = r.u64()
        t = r.u8()
        if t > 17 or 8 <= t <= 10:
            raise PapyrusError(f"invalid array type {t} at {r.p}")
        rt = r.u32() if is_ref_type(t) else 0
        length = r.u32()
        p.arrays.append(ArrayInfo(aid, t, rt, length))

    p.runtime = r.u32()

    for _ in range(r.u32()):
        tid = r.u32()
        tt = r.u8()
        p.active.append(ActiveScript(tid, tt))

    inst_by_id = {i.id: i for i in p.instances}
    ref_by_id = {x.id: x for x in p.references}
    arr_by_id = {a.id: a for a in p.arrays}
    act_by_id = {a.id: a for a in p.active}

    for _ in range(len(p.instances)):
        at = r.p
        iid = r.u64()
        inst = inst_by_id.get(iid)
        if inst is None:
            raise PapyrusError(f"script instance data for unknown id {iid:#x} at {at}")
        inst.data = read_instance_data(r)
        p.instance_data_order.append(iid)

    for _ in range(len(p.references)):
        at = r.p
        rid = r.u64()
        ref = ref_by_id.get(rid)
        if ref is None:
            raise PapyrusError(f"reference data for unknown id {rid:#x} at {at}")
        ref.data = read_instance_data(r)
        p.reference_data_order.append(rid)

    for _ in range(len(p.arrays)):
        at = r.p
        aid = r.u64()
        arr = arr_by_id.get(aid)
        if arr is None:
            raise PapyrusError(f"array data for unknown id {aid:#x} at {at}")
        arr.vars = read_vars(r, arr.length)
        arr.has_data = True
        p.array_data_order.append(aid)

    for i in range(len(p.active)):
        at = r.p
        tid = r.u32()
        act = act_by_id.get(tid)
        if act is None:
            raise PapyrusError(f"thread data for unknown id {tid:#x} at {at} (entry {i})")
        start = r.p
        count, has_u5 = skip_active_data(r)
        # A thread whose bottom frame is a native function has no trailing byte; ReSaver detects
        # that by checking the next thread's id. Only needed if the strict parse mis-sized it.
        act.data = r.d[start : r.p]
        p.active_data_order.append(tid)

    for _ in range(r.u32()):
        flag = r.u8()
        mid = r.u32() if flag <= 2 else None
        flags = r.u8()
        d = _read_fm_data(r) if flags != 0 else None
        p.function_messages.append((flag, mid, flags, d))

    p.suspended1 = _read_suspended(r)
    p.suspended2 = _read_suspended(r)

    p.unk1 = r.u32()
    if p.unk1 != 0:
        p.unk2 = r.u32()
    p.unknown_ids = [r.u64() for _ in range(r.u32())]
    p.unbinds = [(r.u64(), r.u32()) for _ in range(r.u32())]
    p.save_file_version = r.u16()
    p.tail = r.bytes(r.remaining())
    return p


def write_papyrus(p: Papyrus) -> bytes:
    w = W()
    w.u16(p.header)
    if p.invalid:
        w.raw(p.tail)
        return w.getvalue()

    w.u32(len(p.strings))
    for s in p.strings:
        w.wstring(s)

    w.u32(len(p.scripts))
    for s in p.scripts:
        w.u32(s.name)
        w.u32(s.parent)
        w.u32(len(s.members))
        for m in s.members:
            w.u32(m.name)
            w.u32(m.type)

    w.u32(len(p.instances))
    for i in p.instances:
        w.u64(i.id)
        w.u32(i.script)
        w.u16(i.unknown2bits)
        w.u16(i.unknown)
        w.raw(i.ref_id)
        w.u8(i.unknown_byte)

    w.u32(len(p.references))
    for x in p.references:
        w.u64(x.id)
        w.u32(x.script)

    w.u32(len(p.arrays))
    for a in p.arrays:
        w.u64(a.id)
        w.u8(a.type)
        if is_ref_type(a.type):
            w.u32(a.ref_type)
        w.u32(len(a.vars) if a.has_data else a.length)

    w.u32(p.runtime)

    w.u32(len(p.active))
    for a in p.active:
        w.u32(a.id)
        w.u8(a.type)

    def in_order(order, items, write_one):
        by_id = {it.id: it for it in items}
        done = set()
        for id_ in order:
            it = by_id.get(id_)
            if it is not None and id_ not in done:
                done.add(id_)
                write_one(it)
        for it in items:
            if it.id not in done:
                write_one(it)

    in_order(p.instance_data_order, p.instances,
             lambda i: write_instance_data(w, i.id, i.data or InstanceData()))
    in_order(p.reference_data_order, p.references,
             lambda x: write_instance_data(w, x.id, x.data or InstanceData()))

    def write_array(a: ArrayInfo) -> None:
        w.u64(a.id)
        write_vars(w, a.vars)

    in_order(p.array_data_order, p.arrays, write_array)

    def write_active(a: ActiveScript) -> None:
        if a.data is None:
            raise PapyrusError("active script without data cannot be written")
        w.u32(a.id)
        w.raw(a.data)

    in_order(p.active_data_order, p.active, write_active)

    w.u32(len(p.function_messages))
    for flag, mid, flags, d in p.function_messages:
        w.u8(flag)
        if mid is not None:
            w.u32(mid)
        w.u8(flags)
        if d is not None:
            w.raw(d)

    _write_suspended(w, p.suspended1)
    _write_suspended(w, p.suspended2)

    w.u32(p.unk1)
    if p.unk2 is not None:
        w.u32(p.unk2)
    w.u32(len(p.unknown_ids))
    for i in p.unknown_ids:
        w.u64(i)
    w.u32(len(p.unbinds))
    for i, u in p.unbinds:
        w.u64(i)
        w.u32(u)

    if p.save_file_version is not None:
        w.u16(p.save_file_version)
    w.raw(p.tail)
    return w.getvalue()


if __name__ == "__main__":
    import sys
    from ess_parser import parse_ess

    for path in sys.argv[1:]:
        ess = parse_ess(path)
        blob = next(g.data for g in ess.global_data_3 if g.data_type == 1001)
        p = parse_papyrus(blob)
        out = write_papyrus(p)
        ok = out == blob
        print(
            f"{path}: strings={len(p.strings)} scripts={len(p.scripts)} instances={len(p.instances)} "
            f"refs={len(p.references)} arrays={len(p.arrays)} threads={len(p.active)} "
            f"roundtrip={'OK' if ok else 'MISMATCH'} (len {len(blob)} -> {len(out)})"
        )
        if not ok:
            for i in range(min(len(blob), len(out))):
                if blob[i] != out[i]:
                    print("  first diff at", i)
                    break
