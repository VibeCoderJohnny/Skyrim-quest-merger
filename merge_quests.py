#!/usr/bin/env python3
"""
Skyrim Quest Progress Merger (.ess)

Merges ONLY quest progress (QUST ChangeForms: stages, objectives, quest flags,
and quest run/instance data) from a donor .ess save into a base .ess save.

Does NOT modify character levels, skills, perks, items/inventory, stats,
player location, NPCs, or world containers.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from ess_parser import (
    CHANGEFORM_TYPE_QUST,
    ChangeForm,
    EssFile,
    build_plugin_remap,
    parse_ess,
    remap_formid,
    write_ess,
)
from papyrus_merge import merge_quest_scripts
from player_journal import merge_player_journal
from qust import (
    merge_qust,
    parse_qust,
    remap_qust_refids,
    serialize_qust,
    sync_quest_static_data,
)


@dataclass
class MergeReport:
    base_player: str
    base_level: int
    base_version: int
    donor_player: str
    donor_level: int
    donor_version: int
    base_quest_count: int = 0
    donor_quest_count: int = 0
    merged_count: int = 0
    added_count: int = 0
    unchanged_count: int = 0
    skipped_missing_plugin: int = 0
    script_instances_changed: int = 0
    script_variables_copied: int = 0
    journal_logs_added: int = 0
    journal_objectives_updated: int = 0
    warnings: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _formid_plugin_exists_in_base(
    donor_fid: int, donor: EssFile, plugin_remap: dict[int, int]
) -> bool:
    if donor_fid == 0:
        return True
    mod_idx = (donor_fid >> 24) & 0xFF
    if mod_idx in (0x00, 0xFF):
        return True
    if mod_idx == 0xFE:
        return (donor_fid & 0xFFFFF000) in plugin_remap
    return (donor_fid & 0xFF000000) in plugin_remap


def merge_ess_quests(
    base: EssFile,
    donor: EssFile,
    strategy: str = "merge",
    verbose: bool = False,
    merge_scripts: bool = True,
) -> MergeReport:
    """
    Merge quest progress from `donor` into `base` in-place.
    Only QUST ChangeForms (and formIDArray entries they reference) are modified.
    """
    report = MergeReport(
        base_player=base.header.player_name,
        base_level=base.header.player_level,
        base_version=base.header.version,
        donor_player=donor.header.player_name,
        donor_level=donor.header.player_level,
        donor_version=donor.header.version,
    )

    if (base.header.version >= 12) != (donor.header.version >= 12):
        report.warnings.append(
            f"Save version mismatch: base is v{base.header.version} "
            f"({'SE/AE' if base.header.version >= 12 else 'LE'}), "
            f"donor is v{donor.header.version} "
            f"({'SE/AE' if donor.header.version >= 12 else 'LE'})."
        )

    plugin_remap = build_plugin_remap(donor, base)

    # Index base QUST ChangeForms by resolved 4-byte FormID
    base_qust_by_fid: dict[int, ChangeForm] = {}
    for cf in base.change_forms:
        if cf.form_type == CHANGEFORM_TYPE_QUST:
            fid = base.resolve_refid(cf.form_id)
            base_qust_by_fid[fid] = cf

    report.base_quest_count = len(base_qust_by_fid)

    donor_qusts = [cf for cf in donor.change_forms if cf.form_type == CHANGEFORM_TYPE_QUST]
    report.donor_quest_count = len(donor_qusts)

    completed_fids: set[int] = set()
    donor_won_fids: set[int] = set()

    for fid, b_cf in base_qust_by_fid.items():
        if parse_qust(b_cf).is_completed():
            completed_fids.add(fid)

    for d_cf in donor_qusts:
        d_fid = donor.resolve_refid(d_cf.form_id)
        if not _formid_plugin_exists_in_base(d_fid, donor, plugin_remap):
            report.skipped_missing_plugin += 1
            if verbose:
                report.details.append(
                    f"SKIP 0x{d_fid:08X}: plugin not present in base save"
                )
            continue

        target_fid = remap_formid(d_fid, plugin_remap)
        d_qust = parse_qust(d_cf)
        d_qust_remapped = remap_qust_refids(d_qust, donor, base, plugin_remap)

        if target_fid in base_qust_by_fid:
            if strategy == "base-priority":
                report.unchanged_count += 1
                continue

            b_cf = base_qust_by_fid[target_fid]
            b_qust = parse_qust(b_cf)
            if strategy == "donor" or d_qust_remapped.progress_score() > b_qust.progress_score():
                donor_won_fids.add(target_fid)
            merged_qust = merge_qust(b_qust, d_qust_remapped, strategy=strategy)
            if merged_qust.is_completed():
                completed_fids.add(target_fid)
            else:
                completed_fids.discard(target_fid)

            new_raw = serialize_qust(merged_qust)
            old_raw = b_cf.get_uncompressed_data()
            if new_raw != old_raw or merged_qust.change_flags != b_cf.change_flags:
                b_cf.change_flags = merged_qust.change_flags
                b_cf.set_uncompressed_data(new_raw)
                report.merged_count += 1
                if verbose:
                    report.details.append(
                        f"MERGE 0x{target_fid:08X}: "
                        f"stages {len(b_qust.stages)}+{len(d_qust.stages)}->{len(merged_qust.stages)}, "
                        f"objectives {len(b_qust.objectives)}+{len(d_qust.objectives)}->{len(merged_qust.objectives)}"
                    )
            else:
                report.unchanged_count += 1
        else:
            new_refid = base.get_or_add_formid_ref(target_fid)
            new_raw = serialize_qust(d_qust_remapped)
            new_cf = ChangeForm(
                form_id=new_refid,
                change_flags=d_qust_remapped.change_flags,
                type_byte=(d_cf.type_byte & 0x3F),
                version=d_cf.version,
                data=b"",
                is_compressed=d_cf.is_compressed,
            )
            new_cf.set_uncompressed_data(new_raw, compress=d_cf.is_compressed)
            base.change_forms.append(new_cf)
            base_qust_by_fid[target_fid] = new_cf
            donor_won_fids.add(target_fid)
            if d_qust_remapped.is_completed():
                completed_fids.add(target_fid)
            report.added_count += 1
            if verbose:
                report.details.append(
                    f"ADD   0x{target_fid:08X}: "
                    f"stages={len(d_qust_remapped.stages)}, "
                    f"objectives={len(d_qust_remapped.objectives)}"
                )

    sync_quest_static_data(
        base=base,
        donor=donor,
        plugin_remap=plugin_remap,
        completed_fids=completed_fids,
        donor_won_fids=donor_won_fids,
        strategy=strategy,
    )

    j_logs, j_objs = merge_player_journal(
        base=base,
        donor=donor,
        plugin_remap=plugin_remap,
        donor_won_fids=donor_won_fids,
        strategy=strategy,
    )
    report.journal_logs_added = j_logs
    report.journal_objectives_updated = j_objs

    if merge_scripts and strategy != "base-priority":
        pm = merge_quest_scripts(
            base, donor, plugin_remap, winning_fids=donor_won_fids, verbose=verbose
        )
        report.script_instances_changed = pm.instances_changed
        report.script_variables_copied = pm.variables_copied
        report.details.extend(pm.details)
        if pm.unreadable:
            report.warnings.append(
                "The Papyrus (script) section of one of the saves could not be read, so quest script "
                "variables were NOT merged; only the quest records were. " + "; ".join(pm.details)
            )
        if pm.skipped_shape:
            report.warnings.append(
                f"{pm.skipped_shape} quest script(s) had a different layout in the two saves and were left alone."
            )

    if report.skipped_missing_plugin > 0:
        report.warnings.append(
            f"Skipped {report.skipped_missing_plugin} quest(s) from plugins not loaded in the base save."
        )

    return report


def merge_save_files(
    base_path: str,
    donor_path: str,
    output_path: str | None = None,
    strategy: str = "merge",
    dry_run: bool = False,
    verbose: bool = False,
    merge_scripts: bool = True,
) -> MergeReport:
    base = parse_ess(base_path)
    donor = parse_ess(donor_path)
    report = merge_ess_quests(
        base, donor, strategy=strategy, verbose=verbose, merge_scripts=merge_scripts
    )
    if not dry_run:
        if not output_path:
            raise ValueError("output_path is required when dry_run is False")
        write_ess(base, output_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge quest progress (QUST ChangeForms) from a donor Skyrim .ess save "
            "into a base .ess save without touching items, levels, or character stats."
        )
    )
    parser.add_argument("base", help="Base .ess save (keeps character, levels, items, world)")
    parser.add_argument("donor", help="Donor .ess save (source of quest progress to merge)")
    parser.add_argument(
        "-o", "--output",
        help="Output .ess file path (required unless --dry-run is set)",
    )
    parser.add_argument(
        "--strategy",
        choices=("merge", "donor", "base-priority"),
        default="merge",
        help=(
            "merge (default): union stages/objectives & keep furthest progress per quest; "
            "donor: overwrite base quests with donor; "
            "base-priority: only add quests missing from base."
        ),
    )
    parser.add_argument(
        "--no-scripts",
        action="store_true",
        help="Do not merge quest script variables (Papyrus); merge quest records only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print merge summary without writing an output file.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-quest merge details.",
    )

    args = parser.parse_args(argv)
    if not args.dry_run and not args.output:
        parser.error("the following arguments are required: -o/--output (unless --dry-run is used)")

    report = merge_save_files(
        base_path=args.base,
        donor_path=args.donor,
        output_path=args.output,
        strategy=args.strategy,
        dry_run=args.dry_run,
        verbose=args.verbose,
        merge_scripts=not args.no_scripts,
    )

    print(f"Base save : {report.base_player} (Level {report.base_level}, v{report.base_version})")
    print(f"Donor save: {report.donor_player} (Level {report.donor_level}, v{report.donor_version})")
    print(f"Strategy  : {args.strategy}")
    print(f"Quests in base : {report.base_quest_count}")
    print(f"Quests in donor: {report.donor_quest_count}")
    print(f"  Merged (updated): {report.merged_count}")
    print(f"  Added (new)     : {report.added_count}")
    print(f"  Unchanged       : {report.unchanged_count}")
    print(f"  Quest script variables copied: {report.script_variables_copied} "
          f"(in {report.script_instances_changed} script(s))")
    print(f"  Player journal updated      : +{report.journal_logs_added} log entry(s), "
          f"{report.journal_objectives_updated} objective(s) synced")
    if report.skipped_missing_plugin:
        print(f"  Skipped (missing plugin): {report.skipped_missing_plugin}")

    for w in report.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if args.verbose and report.details:
        print("\nDetails:")
        for line in report.details:
            print(f"  {line}")

    if args.dry_run:
        print("\nDry run complete — no file written.")
    else:
        print()
        print(f"Saved merged .ess to: {args.output}")
        print("Note: Character level, items, inventory, skills, and stats were kept from the base save.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
