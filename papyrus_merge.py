"""
Merge of quest *script state* (Papyrus section, Global Data 1001) between two saves.

Skyrim keeps each quest's progress in two places that must agree: the QUST change form
(stages/objectives/flags) and the Papyrus script variables of the quest's scripts
(counters, "has happened" flags, timers). This module copies the second part.

Design (see papyrus.py for the format):
  * Only PLAIN values are copied: int, float, bool, string. Object/array handles (refs,
    structs, arrays) are numbered per save and are never moved between saves.
  * Only quests whose donor record "won" the quest merge are touched, so the script state
    always follows the same winner as the stages (otherwise it would roll progress back).
  * Script instances are matched by (quest formID, alias id, script name). A match must have
    the same variable layout (count and per-variable type) in both saves, otherwise it is
    left alone.
  * String values are re-interned into the base save's string table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ess_parser import EssFile, RefID, remap_formid
from papyrus import (
    Papyrus,
    PapyrusError,
    ScriptInstance,
    V_BOOL,
    V_FLOAT,
    V_INT,
    V_STRING,
    parse_papyrus,
    write_papyrus,
)

PLAIN_TYPES = (V_INT, V_FLOAT, V_BOOL, V_STRING)
QUEST_LEVEL_ALIAS = 0xFFFF


@dataclass
class PapyrusMergeReport:
    instances_matched: int = 0
    instances_changed: int = 0
    variables_copied: int = 0
    skipped_shape: int = 0
    skipped_missing: int = 0
    unreadable: bool = False
    details: list[str] = field(default_factory=list)


def _blob(ess: EssFile) -> bytes | None:
    for g in ess.global_data_3:
        if g.data_type == 1001:
            return g.data
    return None


def _set_blob(ess: EssFile, data: bytes) -> None:
    for g in ess.global_data_3:
        if g.data_type == 1001:
            g.data = data
            return


def _intern(p: Papyrus, index: dict[bytes, int], s: bytes) -> int:
    i = index.get(s)
    if i is None:
        p.strings.append(s)
        i = len(p.strings) - 1
        index[s] = i
    return i


def _instance_map(ess: EssFile, p: Papyrus) -> dict[tuple[int, int, bytes], ScriptInstance]:
    out: dict[tuple[int, int, bytes], ScriptInstance] = {}
    for inst in p.instances:
        try:
            fid = ess.resolve_refid(RefID.from_bytes(*inst.ref_id))
        except IndexError:
            continue
        name = p.strings[inst.script].lower()
        out[(fid, inst.unknown, name)] = inst
    return out


def merge_quest_scripts(
    base: EssFile,
    donor: EssFile,
    plugin_remap: dict[int, int],
    winning_fids: set[int],
    verbose: bool = False,
) -> PapyrusMergeReport:
    """Copy plain script variables of donor quests listed in `winning_fids` onto `base`."""
    report = PapyrusMergeReport()
    b_blob, d_blob = _blob(base), _blob(donor)
    if b_blob is None or d_blob is None or not winning_fids:
        return report

    try:
        bp = parse_papyrus(b_blob)
        dp = parse_papyrus(d_blob)
    except PapyrusError as exc:
        report.details.append(f"Papyrus section could not be read ({exc}); script state not merged")
        report.unreadable = True
        return report
    if bp.invalid or dp.invalid:
        report.details.append("Papyrus section flagged invalid by the game; script state not merged")
        report.unreadable = True
        return report

    # Safety net: refuse to write anything that does not round-trip byte-for-byte.
    if write_papyrus(bp) != b_blob:
        report.details.append("base Papyrus section does not round-trip; script state not merged")
        return report

    b_map = _instance_map(base, bp)
    str_index = {s: i for i, s in enumerate(bp.strings)}

    for (d_fid, alias, name), d_inst in _instance_map(donor, dp).items():
        target = remap_formid(d_fid, plugin_remap)
        if target not in winning_fids:
            continue
        b_inst = b_map.get((target, alias, name))
        if b_inst is None or b_inst.data is None or d_inst.data is None:
            report.skipped_missing += 1
            continue
        bv, dv = b_inst.data.vars, d_inst.data.vars
        if len(bv) != len(dv) or any(x.type != y.type for x, y in zip(bv, dv)):
            report.skipped_shape += 1
            continue

        report.instances_matched += 1
        changed = 0
        for i, (x, y) in enumerate(zip(bv, dv)):
            if y.type not in PLAIN_TYPES:
                continue
            if y.type == V_STRING:
                new_val = _intern(bp, str_index, dp.strings[y.value])
            else:
                new_val = y.value
            if new_val != x.value:
                x.value = new_val
                changed += 1
        if changed:
            report.instances_changed += 1
            report.variables_copied += changed
            if verbose:
                report.details.append(
                    f"SCRIPT 0x{target:08X} {name.decode('latin1')}: {changed} variable(s) copied"
                )

    out = write_papyrus(bp)
    # Verify the result still parses, so a bug here can never produce a corrupt blob.
    reparsed = parse_papyrus(out)
    if write_papyrus(reparsed) != out:
        report.details.append("merged Papyrus section failed re-verification; script state NOT written")
        return PapyrusMergeReport(details=report.details)
    _set_blob(base, out)
    return report
