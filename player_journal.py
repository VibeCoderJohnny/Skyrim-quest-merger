"""
Synchronizes ONLY the player's quest journal lists inside the PlayerCharacter ACHR
ChangeForm (FormID 0x00000014) without modifying player level, XP, skills, perks,
inventory, position, camera, or appearance.

Skyrim stores the journal UI's displayed log entries and objective states on the
PlayerCharacter ACHR (not in QUST ChangeForms):
  1. QUEST OBJECTIVES (log entries): vsval count of (RefID quest, int16 stage, uint8 log_index)
  2. QUEST TARGETS (journal objective states): vsval count of
     (RefID quest, uint32 obj_index, uint32 quest_instance, uint32 state)

Layout verified against ReSaver++ (src/core/ChangeFormData.cpp `PlayerData` & `Quests.cpp`).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ess_parser import BinaryReader, BinaryWriter, ChangeForm, EssFile, RefID, remap_formid

PLAYER_FORM_ID = 0x00000014
CHANGEFORM_TYPE_ACHR = 1


@dataclass(frozen=True)
class PlayerLogEntry:
    quest_fid: int
    stage: int
    log_idx: int


@dataclass(frozen=True)
class PlayerObjectiveEntry:
    quest_fid: int
    obj_index: int
    instance: int
    state: int


@dataclass
class PlayerJournalSlice:
    start_offset: int
    end_offset: int
    log_entries: list[PlayerLogEntry]
    obj_entries: list[PlayerObjectiveEntry]


def _find_player_achr(ess: EssFile) -> ChangeForm | None:
    for cf in ess.change_forms:
        if cf.form_type == CHANGEFORM_TYPE_ACHR and ess.resolve_refid(cf.form_id) == PLAYER_FORM_ID:
            return cf
    return None


def _verify_player_tail(d: bytes, pos: int, v: int) -> bool:
    """Verify that `pos` (right after QUEST TARGETS) parses cleanly to the exact end of PlayerData."""
    try:
        r = BinaryReader(d, pos)
        r.read_bytes(20)  # 5 x i32

        # PlayerCamera
        r.read_bytes(10)  # u8, u8, f32, f32
        r.read_bytes(4 + (10 if v > 0x14 else 0))  # FIRST PERSON

        def _third_person() -> None:
            r.read_bytes(29)  # 3*f32 + f32 + 8B + u8 + f32
            r.read_wstring_raw()

        _third_person()  # THIRD PERSON
        _third_person()
        r.read_bytes(8)  # HORSE
        _third_person()  # BLEEDOUT
        _third_person()  # ANIMATED
        if v > 0x40:
            _third_person()
            r.read_bytes(8)  # DRAGON
        r.read_u32()  # CURRENT STATE
        st_n = r.read_u32()
        if st_n > 256:
            return False
        r.read_bytes(4 * st_n)
        if v > 0x12:
            r.read_bytes(9)

        # CRIME GOLD
        for _ in range(r.read_vsval()):
            r.read_bytes(19)
        r.read_bytes(18)  # REF89C + 2*i32 + REF9F8 + i32
        # STOLEN ITEM VALUES
        for _ in range(r.read_vsval()):
            r.read_bytes(11)
        # LIST1, LIST2, LIST3
        for _ in range(r.read_vsval()):
            r.read_bytes(4)
        for _ in range(r.read_vsval()):
            r.read_bytes(3)
        for _ in range(r.read_vsval()):
            r.read_bytes(7)

        r.read_u8()  # FLAG7
        unk_i8 = r.read_u8()
        if unk_i8 != 0xFF:
            r.read_u16()
        r.read_bytes(28)  # REF630 + 3*f32 + i32 + 3*RefID

        # PlayerSkin
        r.read_bytes(6 if v < 0x1C else 9)
        if v > 4:
            r.read_u32()
        for _ in range(2):
            tn = r.read_u32()
            if tn > 2048:
                return False
            r.read_bytes(10 * tn)
        r.read_bytes(3)

        r.read_bytes(7)  # UNK728 + REF720
        for _ in range(r.read_vsval()):
            r.read_bytes(11)  # CRIMES3
        r.read_bytes(3 + (3 if v > 6 else 0))
        for _ in range(r.read_vsval()):
            r.read_bytes(4)  # UNK5B2BB0
        r.read_bytes(7)  # REFA00 + REFA08 + PERK POINTS
        for _ in range(r.read_vsval()):
            r.read_bytes(11)  # LIST4

        if v > 0x0D:
            r.read_bytes(4)
        if v > 0x11:
            r.read_bytes(3)
        if v > 0x3A:
            r.read_bytes(8)
        if v > 0x3C:
            if r.read_u8():
                r.read_bytes(15 + (6 if v > 0x3E else 0) + (3 if v > 0x3F else 0))
        if v < 0x48:
            if v > 0x43:
                r.read_u8()
        else:
            r.read_u32()
        if v > 0x45:
            r.read_u8()
        if v > 0x46:
            r.read_bytes(6)
        if v > 0x4C:
            r.read_u8()

        return r.remaining() == 0
    except (EOFError, ValueError, struct.error):
        return False


def locate_player_journal(ess: EssFile, player_cf: ChangeForm) -> PlayerJournalSlice | None:
    """
    Locate and decode the exact `[start_offset : end_offset]` slice containing
    `QUEST OBJECTIVES` + `QUEST TARGETS` in `player_cf`, verified from both
    the SKILLS anchor before it and the PlayerData tail to EOF after it.
    """
    d = player_cf.get_uncompressed_data()
    v = player_cf.version

    exp_bytes = struct.pack("<ff", ess.header.player_cur_exp, ess.header.player_lvl_up_exp)
    search_from = 0
    while True:
        idx = d.find(exp_bytes, search_from)
        if idx == -1:
            return None
        search_from = idx + 1

        try:
            r = BinaryReader(d, idx)
            r.read_f32()
            r.read_f32()
            sk_n = r.read_u32()
            if sk_n > 200:
                continue
            total = max(sk_n, 18)
            r.read_bytes(total * (16 if v > 0x49 else 12))
            r.read_bytes(16)  # UNK498 (3 floats) + UNKAD8 (1 float)
            pos_n = r.read_vsval()
            if pos_n > 1000:
                continue
            r.read_bytes(pos_n * 16)
            r.read_bytes(24)  # 8 RefIDs
            tel_n = r.read_vsval()
            if tel_n > 1000:
                continue
            for _ in range(tel_n):
                t_idx = r.read_u32()
                if t_idx < 15:
                    r.read_bytes(7)

            slice_start = r.pos
            log_n = r.read_vsval()
            if log_n > 10000:
                continue
            logs: list[PlayerLogEntry] = []
            for _ in range(log_n):
                q_fid = ess.resolve_refid(r.read_refid())
                stg = r.read_i16()
                l_idx = r.read_u8()
                logs.append(PlayerLogEntry(q_fid, stg, l_idx))

            obj_n = r.read_vsval()
            if obj_n > 20000:
                continue
            objs: list[PlayerObjectiveEntry] = []
            for _ in range(obj_n):
                q_fid = ess.resolve_refid(r.read_refid())
                o_idx = r.read_u32()
                inst = r.read_u32()
                state = r.read_u32()
                objs.append(PlayerObjectiveEntry(q_fid, o_idx, inst, state))

            slice_end = r.pos
            if not _verify_player_tail(d, slice_end, v):
                continue

            return PlayerJournalSlice(
                start_offset=slice_start,
                end_offset=slice_end,
                log_entries=logs,
                obj_entries=objs,
            )
        except (EOFError, ValueError, IndexError, struct.error):
            continue


def _state_rank(state: int) -> tuple[int, int]:
    """
    Prefer completed/failed states over merely shown, while preserving visibility (odd bit).
    States: 0=not shown, 1=shown, 2=completed, 3=completed+shown, 4=failed, 5=failed+shown.
    """
    shown = state & 1
    tier = state >> 1  # 0=active/unshown, 1=completed, 2=failed
    return (tier, shown)


def merge_player_journal(
    base: EssFile,
    donor: EssFile,
    plugin_remap: dict[int, int],
    donor_won_fids: set[int],
    strategy: str = "merge",
) -> tuple[int, int]:
    """
    Merge donor's journal log entries and objective states into base's PlayerCharacter ACHR
    for quests where donor won (or all donor quests if strategy == 'donor').

    Returns `(added_or_updated_logs, added_or_updated_objectives)`.
    """
    if strategy == "base-priority":
        return (0, 0)

    b_cf = _find_player_achr(base)
    d_cf = _find_player_achr(donor)
    if b_cf is None or d_cf is None:
        return (0, 0)

    b_slice = locate_player_journal(base, b_cf)
    d_slice = locate_player_journal(donor, d_cf)
    if b_slice is None or d_slice is None:
        return (0, 0)

    def _remap_valid(d_fid: int) -> int | None:
        if d_fid == 0:
            return None
        mod_idx = (d_fid >> 24) & 0xFF
        if mod_idx not in (0x00, 0xFF):
            key = (d_fid & 0xFFFFF000) if mod_idx == 0xFE else (d_fid & 0xFF000000)
            if key not in plugin_remap:
                return None
        return remap_formid(d_fid, plugin_remap)

    # 1. Merge QUEST OBJECTIVES (log entries). Note: Skyrim stores newest log entries first!
    # Keep base entries, and prepend any donor entries for winning quests not already present.
    existing_logs = set(b_slice.log_entries)
    new_donor_logs: list[PlayerLogEntry] = []
    for entry in d_slice.log_entries:
        tfid = _remap_valid(entry.quest_fid)
        if tfid is None:
            continue
        if strategy != "donor" and tfid not in donor_won_fids:
            continue
        remapped_entry = PlayerLogEntry(tfid, entry.stage, entry.log_idx)
        if remapped_entry not in existing_logs:
            existing_logs.add(remapped_entry)
            new_donor_logs.append(remapped_entry)

    merged_logs = new_donor_logs + b_slice.log_entries

    # 2. Merge QUEST TARGETS (journal objective states).
    # Keyed by (quest_fid, obj_index, instance).
    merged_objs: list[PlayerObjectiveEntry] = list(b_slice.obj_entries)
    obj_pos_by_key: dict[tuple[int, int, int], int] = {
        (e.quest_fid, e.obj_index, e.instance): i for i, e in enumerate(merged_objs)
    }

    obj_changes = 0
    for entry in d_slice.obj_entries:
        tfid = _remap_valid(entry.quest_fid)
        if tfid is None:
            continue
        if strategy != "donor" and tfid not in donor_won_fids:
            continue
        key = (tfid, entry.obj_index, entry.instance)
        if key in obj_pos_by_key:
            idx = obj_pos_by_key[key]
            cur = merged_objs[idx]
            if strategy == "donor":
                chosen_state = entry.state
            else:
                # Keep higher progress tier (completed/failed > active), preserve 'shown' bit if either was shown
                best_tier = max(cur.state >> 1, entry.state >> 1)
                shown_bit = (cur.state & 1) | (entry.state & 1)
                # However, if donor replaced/hid an objective on a completed run (e.g. inst 1 -> inst 2),
                # and donor won, prefer donor's state when donor is completed.
                if _state_rank(entry.state) >= _state_rank(cur.state):
                    chosen_state = entry.state
                else:
                    chosen_state = (best_tier << 1) | shown_bit
            if chosen_state != cur.state:
                merged_objs[idx] = PlayerObjectiveEntry(tfid, entry.obj_index, entry.instance, chosen_state)
                obj_changes += 1
        else:
            obj_pos_by_key[key] = len(merged_objs)
            merged_objs.append(PlayerObjectiveEntry(tfid, entry.obj_index, entry.instance, entry.state))
            obj_changes += 1

    if not new_donor_logs and obj_changes == 0:
        return (0, 0)

    # Serialize replacement slice
    w = BinaryWriter()
    w.write_vsval(len(merged_logs))
    for le in merged_logs:
        w.write_refid(base.get_or_add_formid_ref(le.quest_fid))
        w.write_i16(le.stage)
        w.write_u8(le.log_idx)

    w.write_vsval(len(merged_objs))
    for oe in merged_objs:
        w.write_refid(base.get_or_add_formid_ref(oe.quest_fid))
        w.write_u32(oe.obj_index)
        w.write_u32(oe.instance)
        w.write_u32(oe.state)

    raw_player = b_cf.get_uncompressed_data()
    updated_player = raw_player[: b_slice.start_offset] + w.getvalue() + raw_player[b_slice.end_offset :]

    # Safety check: verify tail still aligns to 0 remaining bytes
    new_end = b_slice.start_offset + len(w.getvalue())
    if not _verify_player_tail(updated_player, new_end, b_cf.version):
        raise RuntimeError("Player ACHR journal splice failed structural verification; aborting write")

    b_cf.set_uncompressed_data(updated_player)
    return (len(new_donor_logs), obj_changes)
