"""
Parser, serializer, RefID remapper, and merger for Skyrim QUST ChangeForms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ess_parser import (
    BinaryReader,
    BinaryWriter,
    ChangeForm,
    EssFile,
    RefID,
    remap_formid,
)

# QUST changeFlags bitmasks
CHANGE_FORM_FLAGS = 0x00000001
CHANGE_QUEST_FLAGS = 0x00000002
CHANGE_QUEST_SCRIPT_DELAY = 0x00000004
CHANGE_QUEST_ALREADY_RUN = 0x04000000
CHANGE_QUEST_INSTANCES = 0x08000000
CHANGE_QUEST_RUNDATA = 0x10000000
CHANGE_QUEST_OBJECTIVES = 0x20000000
CHANGE_QUEST_SCRIPT = 0x40000000
CHANGE_QUEST_STAGES = 0x80000000

# Common Quest Flags (uint16)
QUEST_FLAG_ENABLED = 0x0001
QUEST_FLAG_COMPLETED = 0x0002
QUEST_FLAG_ACTIVE = 0x0008


@dataclass
class QuestRunDataItem:
    unk: int
    flag: int
    refs: list[RefID]


@dataclass
class QuestRunDataItem2:
    unk: int
    ref: RefID


@dataclass
class QuestRunDataItem3Data:
    dtype: int
    ref_val: RefID | None = None
    int_val: int | None = None


@dataclass
class QuestRunDataItem3:
    unk1: int
    unk2: float
    items: list[QuestRunDataItem3Data]


@dataclass
class QuestRunData:
    unk: int
    items1: list[QuestRunDataItem]
    items2: list[QuestRunDataItem2]
    has_item3: int
    item3: QuestRunDataItem3 | None = None


@dataclass
class QuestInstanceData:
    unk: int
    entries1: list[tuple[int, RefID]]
    entries2: list[tuple[RefID, int]]
    unk16: int
    unk8: int


@dataclass
class QuestInstances:
    unk: int
    instances: list[QuestInstanceData]


@dataclass
class QustData:
    change_flags: int
    form_flags: bytes | None = None          # 6 bytes if CHANGE_FORM_FLAGS
    quest_flags: int | None = None           # uint16 if CHANGE_QUEST_FLAGS
    script_delay: float | None = None        # float32 if CHANGE_QUEST_SCRIPT_DELAY
    stages: list[tuple[int, int]] = field(default_factory=list)       # (sint16 stage, uint8 status)
    objectives: list[tuple[int, int]] = field(default_factory=list)   # (uint32 index, uint32 flags)
    run_data: QuestRunData | None = None
    instances: QuestInstances | None = None
    already_run: int | None = None           # uint8 if CHANGE_QUEST_ALREADY_RUN
    trailing_bytes: bytes = b""

    def max_stage(self) -> int:
        active_or_done = [s for s, st in self.stages if st != 0]
        inst_stages = [i.unk16 for i in self.instances.instances] if self.instances else []
        candidates = active_or_done + inst_stages
        if candidates:
            return max(candidates)
        return max((s for s, _ in self.stages), default=-1)

    def is_completed(self) -> bool:
        if self.quest_flags is not None and (self.quest_flags & QUEST_FLAG_COMPLETED):
            return True
        # Quests that finish and shut down clear ENABLED (quest_flags == 0) and record
        # stage >= 200 either in stages or in QuestInstances history.
        if self.quest_flags == 0:
            if any(s >= 200 and st != 0 for s, st in self.stages):
                return True
            if self.instances and any(i.unk16 >= 200 for i in self.instances.instances):
                return True
        return False

    def progress_score(self) -> tuple[int, int, int, int, int, int]:
        """Heuristic tuple for comparing which save has more progress on this quest."""
        completed = 1 if self.is_completed() else 0
        inst_cur = self.instances.unk if self.instances else 0
        max_stg = self.max_stage()
        num_stages = sum(1 for _, st in self.stages if st != 0)
        completed_obj = sum(1 for _, fl in self.objectives if fl & 0x02)
        num_obj = len(self.objectives)
        return (completed, inst_cur, max_stg, num_stages, completed_obj, num_obj)


# ---------------------------------------------------------------------------
# Parse & Serialize
# ---------------------------------------------------------------------------

def parse_qust(cf: ChangeForm) -> QustData:
    raw = cf.get_uncompressed_data()
    r = BinaryReader(raw)
    flags = cf.change_flags

    form_flags = r.read_bytes(6) if (flags & CHANGE_FORM_FLAGS) else None
    quest_flags = r.read_u16() if (flags & CHANGE_QUEST_FLAGS) else None
    script_delay = r.read_f32() if (flags & CHANGE_QUEST_SCRIPT_DELAY) else None

    stages: list[tuple[int, int]] = []
    if flags & CHANGE_QUEST_STAGES:
        count = r.read_vsval()
        for _ in range(count):
            stg = r.read_i16()
            status = r.read_u8()
            stages.append((stg, status))

    objectives: list[tuple[int, int]] = []
    if flags & CHANGE_QUEST_OBJECTIVES:
        count = r.read_vsval()
        for _ in range(count):
            idx = r.read_u32()
            obj_flags = r.read_u32()
            objectives.append((idx, obj_flags))

    run_data: QuestRunData | None = None
    instances: QuestInstances | None = None
    already_run: int | None = None
    trailing = b""

    try:
        if flags & CHANGE_QUEST_RUNDATA:
            run_data = _read_run_data(r)
        if flags & CHANGE_QUEST_INSTANCES:
            instances = _read_instances(r)
        if flags & CHANGE_QUEST_ALREADY_RUN:
            already_run = r.read_u8()
        if r.remaining() > 0:
            trailing = r.read_bytes(r.remaining())
    except (EOFError, ValueError):
        # Preserve any unparsed remainder safely if a modded/unusual QUST differs
        trailing = raw[r.pos :]

    return QustData(
        change_flags=flags,
        form_flags=form_flags,
        quest_flags=quest_flags,
        script_delay=script_delay,
        stages=stages,
        objectives=objectives,
        run_data=run_data,
        instances=instances,
        already_run=already_run,
        trailing_bytes=trailing,
    )


def _read_run_data(r: BinaryReader) -> QuestRunData:
    unk = r.read_u8()
    c1 = r.read_u32()
    items1: list[QuestRunDataItem] = []
    for _ in range(c1):
        i_unk = r.read_u32()
        i_flag = r.read_u8()
        ref_count = 1 if i_flag == 0 else 5
        refs = [r.read_refid() for _ in range(ref_count)]
        items1.append(QuestRunDataItem(unk=i_unk, flag=i_flag, refs=refs))

    c2 = r.read_u32()
    items2: list[QuestRunDataItem2] = []
    for _ in range(c2):
        i2_unk = r.read_u32()
        i2_ref = r.read_refid()
        items2.append(QuestRunDataItem2(unk=i2_unk, ref=i2_ref))

    has_item3 = r.read_u8()
    item3: QuestRunDataItem3 | None = None
    if has_item3 != 0:
        u1 = r.read_u32()
        u2 = r.read_f32()
        c3 = r.read_u32()
        sub_items: list[QuestRunDataItem3Data] = []
        for _ in range(c3):
            dtype = r.read_u32()
            if dtype != 3:
                sub_items.append(QuestRunDataItem3Data(dtype=dtype, ref_val=r.read_refid()))
            else:
                sub_items.append(QuestRunDataItem3Data(dtype=dtype, int_val=r.read_u32()))
        item3 = QuestRunDataItem3(unk1=u1, unk2=u2, items=sub_items)

    return QuestRunData(
        unk=unk, items1=items1, items2=items2, has_item3=has_item3, item3=item3
    )


def _read_instances(r: BinaryReader) -> QuestInstances:
    unk = r.read_u32()
    count = r.read_vsval()
    instances: list[QuestInstanceData] = []
    for _ in range(count):
        inst_unk = r.read_u32()
        c1 = r.read_vsval()
        entries1 = [(r.read_u32(), r.read_refid()) for _ in range(c1)]
        c2 = r.read_vsval()
        entries2 = [(r.read_refid(), r.read_u32()) for _ in range(c2)]
        unk16 = r.read_u16()
        unk8 = r.read_u8()
        instances.append(
            QuestInstanceData(
                unk=inst_unk,
                entries1=entries1,
                entries2=entries2,
                unk16=unk16,
                unk8=unk8,
            )
        )
    return QuestInstances(unk=unk, instances=instances)


def serialize_qust(q: QustData) -> bytes:
    w = BinaryWriter()
    flags = q.change_flags

    if flags & CHANGE_FORM_FLAGS:
        w.write_bytes(q.form_flags if q.form_flags is not None else b"\x00" * 6)
    if flags & CHANGE_QUEST_FLAGS:
        w.write_u16(q.quest_flags if q.quest_flags is not None else 0)
    if flags & CHANGE_QUEST_SCRIPT_DELAY:
        w.write_f32(q.script_delay if q.script_delay is not None else 0.0)
    if flags & CHANGE_QUEST_STAGES:
        w.write_vsval(len(q.stages))
        for stg, status in q.stages:
            w.write_i16(stg)
            w.write_u8(status)
    if flags & CHANGE_QUEST_OBJECTIVES:
        w.write_vsval(len(q.objectives))
        for idx, obj_flags in q.objectives:
            w.write_u32(idx)
            w.write_u32(obj_flags)
    if (flags & CHANGE_QUEST_RUNDATA) and q.run_data is not None:
        _write_run_data(w, q.run_data)
    if (flags & CHANGE_QUEST_INSTANCES) and q.instances is not None:
        _write_instances(w, q.instances)
    if (flags & CHANGE_QUEST_ALREADY_RUN) and q.already_run is not None:
        w.write_u8(q.already_run)
    if q.trailing_bytes:
        w.write_bytes(q.trailing_bytes)

    return w.getvalue()


def _write_run_data(w: BinaryWriter, rd: QuestRunData) -> None:
    w.write_u8(rd.unk)
    w.write_u32(len(rd.items1))
    for item in rd.items1:
        w.write_u32(item.unk)
        w.write_u8(item.flag)
        for ref in item.refs:
            w.write_refid(ref)
    w.write_u32(len(rd.items2))
    for item2 in rd.items2:
        w.write_u32(item2.unk)
        w.write_refid(item2.ref)
    w.write_u8(rd.has_item3)
    if rd.has_item3 != 0 and rd.item3 is not None:
        w.write_u32(rd.item3.unk1)
        w.write_f32(rd.item3.unk2)
        w.write_u32(len(rd.item3.items))
        for sub in rd.item3.items:
            w.write_u32(sub.dtype)
            if sub.dtype != 3:
                assert sub.ref_val is not None
                w.write_refid(sub.ref_val)
            else:
                assert sub.int_val is not None
                w.write_u32(sub.int_val)


def _write_instances(w: BinaryWriter, qi: QuestInstances) -> None:
    w.write_u32(qi.unk)
    w.write_vsval(len(qi.instances))
    for inst in qi.instances:
        w.write_u32(inst.unk)
        w.write_vsval(len(inst.entries1))
        for u, ref in inst.entries1:
            w.write_u32(u)
            w.write_refid(ref)
        w.write_vsval(len(inst.entries2))
        for ref, u in inst.entries2:
            w.write_refid(ref)
            w.write_u32(u)
        w.write_u16(inst.unk16)
        w.write_u8(inst.unk8)


# ---------------------------------------------------------------------------
# RefID Remapping & Merging
# ---------------------------------------------------------------------------

def remap_qust_refids(
    q: QustData,
    donor: EssFile,
    base: EssFile,
    plugin_remap: dict[int, int],
) -> QustData:
    """Remap all RefIDs inside `q` from `donor`'s formIDArray/load-order to `base`'s."""

    def _remap_ref(ref: RefID) -> RefID:
        if ref.is_null():
            return ref
        fid = donor.resolve_refid(ref)
        new_fid = remap_formid(fid, plugin_remap)
        return base.get_or_add_formid_ref(new_fid)

    new_run_data: QuestRunData | None = None
    if q.run_data is not None:
        rd = q.run_data
        new_items1 = [
            QuestRunDataItem(unk=it.unk, flag=it.flag, refs=[_remap_ref(r) for r in it.refs])
            for it in rd.items1
        ]
        new_items2 = [
            QuestRunDataItem2(unk=it.unk, ref=_remap_ref(it.ref))
            for it in rd.items2
        ]
        new_item3: QuestRunDataItem3 | None = None
        if rd.item3 is not None:
            new_subs = [
                QuestRunDataItem3Data(
                    dtype=s.dtype,
                    ref_val=_remap_ref(s.ref_val) if s.ref_val is not None else None,
                    int_val=s.int_val,
                )
                for s in rd.item3.items
            ]
            new_item3 = QuestRunDataItem3(unk1=rd.item3.unk1, unk2=rd.item3.unk2, items=new_subs)
        new_run_data = QuestRunData(
            unk=rd.unk,
            items1=new_items1,
            items2=new_items2,
            has_item3=rd.has_item3,
            item3=new_item3,
        )

    new_instances: QuestInstances | None = None
    if q.instances is not None:
        new_inst_list = [
            QuestInstanceData(
                unk=inst.unk,
                entries1=[(u, _remap_ref(r)) for u, r in inst.entries1],
                entries2=[(_remap_ref(r), u) for r, u in inst.entries2],
                unk16=inst.unk16,
                unk8=inst.unk8,
            )
            for inst in q.instances.instances
        ]
        new_instances = QuestInstances(unk=q.instances.unk, instances=new_inst_list)

    return QustData(
        change_flags=q.change_flags,
        form_flags=q.form_flags,
        quest_flags=q.quest_flags,
        script_delay=q.script_delay,
        stages=list(q.stages),
        objectives=list(q.objectives),
        run_data=new_run_data,
        instances=new_instances,
        already_run=q.already_run,
        trailing_bytes=q.trailing_bytes,
    )


def merge_qust(base_q: QustData, donor_q: QustData, strategy: str = "merge") -> QustData:
    """
    Merge two QustData records for the same quest FormID.
    `donor_q` is assumed to already have its RefIDs remapped to `base`.
    """
    if strategy == "donor":
        return donor_q
    if strategy == "base-priority":
        return base_q

    # Default: "merge" (pick further-progressed save; if primary finished and shut down
    # the quest (quest_flags == 0), take primary cleanly instead of re-adding active objectives/run_data)
    donor_ahead = donor_q.progress_score() > base_q.progress_score()
    primary = donor_q if donor_ahead else base_q
    secondary = base_q if donor_ahead else donor_q

    if primary.is_completed() and primary.quest_flags == 0:
        return primary

    # Merge stages: union by stage number, status = 1 if either is non-zero
    stage_map: dict[int, int] = {}
    for stg, status in secondary.stages:
        stage_map[stg] = status
    for stg, status in primary.stages:
        prev = stage_map.get(stg, 0)
        stage_map[stg] = 1 if (prev or status) else 0
    merged_stages = sorted(stage_map.items(), key=lambda kv: kv[0])

    # Merge objectives: union by objective index, bitwise OR of flags
    obj_map: dict[int, int] = {}
    for idx, fl in secondary.objectives:
        obj_map[idx] = obj_map.get(idx, 0) | fl
    for idx, fl in primary.objectives:
        obj_map[idx] = obj_map.get(idx, 0) | fl
    merged_objectives = sorted(obj_map.items(), key=lambda kv: kv[0])

    merged_flags = primary.change_flags
    if merged_stages:
        merged_flags |= CHANGE_QUEST_STAGES
    if merged_objectives:
        merged_flags |= CHANGE_QUEST_OBJECTIVES

    # Combine quest_flags: keep primary's completion/state, ensure enabled/completed bits propagate
    q_flags = primary.quest_flags
    if q_flags is not None and secondary.quest_flags is not None:
        if (secondary.quest_flags & QUEST_FLAG_COMPLETED) and not (q_flags & QUEST_FLAG_COMPLETED):
            q_flags |= QUEST_FLAG_COMPLETED

    return QustData(
        change_flags=merged_flags,
        form_flags=primary.form_flags if primary.form_flags is not None else secondary.form_flags,
        quest_flags=q_flags if q_flags is not None else secondary.quest_flags,
        script_delay=primary.script_delay if primary.script_delay is not None else secondary.script_delay,
        stages=merged_stages,
        objectives=merged_objectives,
        run_data=primary.run_data if primary.run_data is not None else secondary.run_data,
        instances=primary.instances if primary.instances is not None else secondary.instances,
        already_run=primary.already_run if primary.already_run is not None else secondary.already_run,
        trailing_bytes=primary.trailing_bytes if primary.trailing_bytes else secondary.trailing_bytes,
    )


# ---------------------------------------------------------------------------
# Global Data 107: Quest Static Data
# ---------------------------------------------------------------------------

def _read_item3(r: BinaryReader) -> QuestRunDataItem3:
    u1 = r.read_u32()
    u2 = r.read_f32()
    c3 = r.read_u32()
    sub_items: list[QuestRunDataItem3Data] = []
    for _ in range(c3):
        dtype = r.read_u32()
        if dtype != 3:
            sub_items.append(QuestRunDataItem3Data(dtype=dtype, ref_val=r.read_refid()))
        else:
            sub_items.append(QuestRunDataItem3Data(dtype=dtype, int_val=r.read_u32()))
    return QuestRunDataItem3(unk1=u1, unk2=u2, items=sub_items)


def _write_item3(w: BinaryWriter, item3: QuestRunDataItem3) -> None:
    w.write_u32(item3.unk1)
    w.write_f32(item3.unk2)
    w.write_u32(len(item3.items))
    for sub in item3.items:
        w.write_u32(sub.dtype)
        if sub.dtype != 3:
            assert sub.ref_val is not None
            w.write_refid(sub.ref_val)
        else:
            assert sub.int_val is not None
            w.write_u32(sub.int_val)


@dataclass
class QuestStaticData:
    unk0: list[QuestRunDataItem3]
    unk1: list[QuestRunDataItem3]
    refs2: list[RefID]
    refs3: list[RefID]
    refs4: list[RefID]
    unk5: list[tuple[RefID, list[tuple[int, int]]]]
    unk6: int


def parse_quest_static_data(data: bytes) -> QuestStaticData:
    r = BinaryReader(data)
    c0 = r.read_u32()
    unk0 = [_read_item3(r) for _ in range(c0)]
    c1 = r.read_u32()
    unk1 = [_read_item3(r) for _ in range(c1)]
    c2 = r.read_u32()
    refs2 = [r.read_refid() for _ in range(c2)]
    c3 = r.read_u32()
    refs3 = [r.read_refid() for _ in range(c3)]
    c4 = r.read_u32()
    refs4 = [r.read_refid() for _ in range(c4)]
    c5 = r.read_vsval()
    unk5: list[tuple[RefID, list[tuple[int, int]]]] = []
    for _ in range(c5):
        ref = r.read_refid()
        sc = r.read_vsval()
        subs = [(r.read_u32(), r.read_u32()) for _ in range(sc)]
        unk5.append((ref, subs))
    unk6 = r.read_u8() if r.remaining() > 0 else 1
    return QuestStaticData(
        unk0=unk0, unk1=unk1, refs2=refs2, refs3=refs3, refs4=refs4, unk5=unk5, unk6=unk6
    )


def serialize_quest_static_data(qsd: QuestStaticData) -> bytes:
    w = BinaryWriter()
    w.write_u32(len(qsd.unk0))
    for it in qsd.unk0:
        _write_item3(w, it)
    w.write_u32(len(qsd.unk1))
    for it in qsd.unk1:
        _write_item3(w, it)
    w.write_u32(len(qsd.refs2))
    for ref in qsd.refs2:
        w.write_refid(ref)
    w.write_u32(len(qsd.refs3))
    for ref in qsd.refs3:
        w.write_refid(ref)
    w.write_u32(len(qsd.refs4))
    for ref in qsd.refs4:
        w.write_refid(ref)
    w.write_vsval(len(qsd.unk5))
    for ref, subs in qsd.unk5:
        w.write_refid(ref)
        w.write_vsval(len(subs))
        for u0, u1 in subs:
            w.write_u32(u0)
            w.write_u32(u1)
    w.write_u8(qsd.unk6)
    return w.getvalue()


def sync_quest_static_data(
    base: EssFile,
    donor: EssFile,
    plugin_remap: dict[int, int],
    completed_fids: set[int],
    donor_won_fids: set[int],
    strategy: str = "merge",
) -> None:
    """
    Synchronize Global Data 107 (Quest Static Data) active-quest RefID list (`refs3`)
    so newly added or progressed quests from `donor` are registered and finished
    quests are unregistered.
    """
    base_gd107 = next((g for g in base.global_data_2 if g.data_type == 107), None)
    donor_gd107 = next((g for g in donor.global_data_2 if g.data_type == 107), None)
    if base_gd107 is None or donor_gd107 is None or len(base_gd107.data) < 20 or len(donor_gd107.data) < 20:
        return

    try:
        b_qsd = parse_quest_static_data(base_gd107.data)
        d_qsd = parse_quest_static_data(donor_gd107.data)
    except (EOFError, ValueError):
        return

    b_fids = [base.resolve_refid(r) for r in b_qsd.refs3]
    b_fid_set = set(b_fids)

    d_fids: list[int] = []
    d_fid_set: set[int] = set()
    for r in d_qsd.refs3:
        fid = remap_formid(donor.resolve_refid(r), plugin_remap)
        d_fids.append(fid)
        d_fid_set.add(fid)

    if strategy == "donor":
        # Keep base quests not in donor unless completed, add all donor refs3
        merged_fids = [f for f in b_fids if f not in completed_fids]
        seen = set(merged_fids)
        for f in d_fids:
            if f not in seen and f not in completed_fids:
                merged_fids.append(f)
                seen.add(f)
    else:
        # Remove quests that donor completed/stopped (when donor won), and add quests from donor refs3
        merged_fids: list[int] = []
        seen: set[int] = set()
        for f in b_fids:
            if f in donor_won_fids and (f in completed_fids or f not in d_fid_set):
                continue
            if f not in seen:
                merged_fids.append(f)
                seen.add(f)
        if strategy != "base-priority":
            for f in d_fids:
                if f not in seen and f not in completed_fids:
                    merged_fids.append(f)
                    seen.add(f)

    b_qsd.refs3 = [base.get_or_add_formid_ref(f) for f in merged_fids]
    base_gd107.data = serialize_quest_static_data(b_qsd)

