# Skyrim Quest Progress Merger (`.ess`)

A zero-dependency Python CLI tool that merges **quest progress and journal state** from one Skyrim `.ess` save file into another — **without** touching character level, skills, perks, inventory/items, appearance, player location, or world containers.

Supports **Skyrim Special Edition (SE)**, **Anniversary Edition (AE)** (including LZ4-compressed saves and `.esl` light plugins), and **Legendary Edition (LE)**.

---

## Why a Simple `QUST` Copy Isn't Enough

Skyrim splits quest progress across **four** places inside an `.ess` save file. Updating only `QUST` records leaves the in-game journal unchanged or out of sync. This tool synchronizes all four:

1. **`QUST` ChangeForms** (`qust.py`) — Quest flags, completed/active stages, objectives, `QuestRunData`, and `QuestInstances` history (including quests that clear `ENABLED` and store completion stage `>= 200` in instance history when shutting down).
2. **Player Journal Lists in `PlayerCharacter` `ACHR 0x00000014`** (`player_journal.py`) — Splices *only* the `QUEST OBJECTIVES` (stage log text entries) and `QUEST TARGETS` (displayed journal objective states) slice inside the player's `ACHR` record, verified from the `SKILLS` anchor to the end of `PlayerData` so every other byte of the player (items, level, XP, skills, perks, appearance) stays byte-for-byte untouched.
3. **`Global Data 107` (`Quest Static Data`)** (`qust.py`) — Registers newly active donor quests and removes finished/stopped quests from Skyrim's active-quest `RefID` list.
4. **`Global Data 1001` (`Papyrus` VM)** (`papyrus.py`, `papyrus_merge.py`) — Parses the Papyrus VM snapshot byte-for-byte and copies plain quest-script variables (`Int`, `Float`, `Bool`, `String`) for quests where the donor save is further ahead, without moving save-local object/array handles.

It also automatically resolves 3-byte `RefID`s against each save's `formIDArray` and remaps plugin/ESL load-order indices between the donor and base saves.

---

## Requirements

- **Python 3.10+** (uses only the Python standard library — zero required `pip` packages).
- *(Optional)* Install `lz4` (`pip install lz4`) for faster C-accelerated LZ4 compression/decompression on large SE/AE saves; otherwise the built-in pure-Python LZ4 block codec (`lz4_block.py`) is used automatically.

---

## Usage

```bash
python merge_quests.py <base.ess> <donor.ess> -o <merged.ess>
```

- **`<base.ess>`**: The save you want to **keep** (character, level, skills, perks, items/inventory, world state, and any quests where `base.ess` is already further ahead).
- **`<donor.ess>`**: The save supplying quest progress to merge into `<base.ess>`.
- **`-o <merged.ess>`**: Output `.ess` save path to copy into `Documents/My Games/Skyrim Special Edition/Saves/`.

### Options

| Flag | Description |
| --- | --- |
| `--strategy merge` *(default)* | Unions quest progress: keeps whichever save is further ahead per quest, merges stages/objectives, updates the player journal, and copies winning quest script variables. |
| `--strategy donor` | Overwrites matching quests in `base.ess` with `donor.ess` and adds quests only in `donor.ess`. |
| `--strategy base-priority` | Leaves all existing quests in `base.ess` untouched and only adds quests missing from `base.ess`. |
| `--no-scripts` | Skips Papyrus (`Global Data 1001`) script variable merging and updates quest records + journal only. |
| `--dry-run` | Prints a summary of quests that would be merged/added without writing an output file. |
| `-v`, `--verbose` | Prints per-quest and per-script merge details. |

### Examples

Preview changes first without writing a file:

```bash
python merge_quests.py Save1_Base.ess Save2_Donor.ess --dry-run -v
```

Write a merged save:

```bash
python merge_quests.py Save1_Base.ess Save2_Donor.ess -o MergedSave.ess -v
```

---

## Project Structure

- `merge_quests.py` — CLI entry point and high-level merge orchestration.
- `ess_parser.py` — `.ess` binary reader/writer (header, screenshot, LZ4/zlib body compression, plugin & ESL tables, `FileLocationTable` offset calculation, `GlobalData`, `ChangeForm`s, and `RefID`/`FormID` remapping).
- `qust.py` — `QUST` ChangeForm and `Global Data 107` (`Quest Static Data`) parser, serializer, `RefID` remapper, and progress comparator/merger.
- `player_journal.py` — Surgical locator and merger for the `PlayerCharacter` (`ACHR 0x00000014`) quest log and journal objective lists.
- `papyrus.py` — Byte-exact parser and serializer for Skyrim SE/AE `Global Data 1001` (Papyrus VM state).
- `papyrus_merge.py` — Safe plain-variable merger (`Int`, `Float`, `Bool`, `String`) for winning quest scripts.
- `lz4_block.py` — Pure-Python LZ4 block compressor/decompressor with optional `lz4.block` acceleration.
- `tests/` — Unit and end-to-end tests (`python -m unittest discover -s tests -v`).

---

## Caveats & Safety Tips

- **Always back up your original `.ess` (and `.skse` co-save) files** before loading a merged save.
- **Object/Alias Handles**: Papyrus object and alias handles are numbered uniquely per save and are intentionally not transplanted across saves. Quests that rely on newly spawned world references or mid-scene NPC alias fills from the donor save may still need an in-game `setstage` nudge if an NPC wasn't placed in the base save's world.
- **Plugins**: Quests belonging to `.esp`/`.esm`/`.esl` plugins that are not loaded in `base.ess` are safely skipped with a warning.

---

## Acknowledgments

Save and ChangeForm/Papyrus format specifications draw on the [UESP Skyrim Save File Format Wiki](https://en.uesp.net/wiki/Skyrim_Mod:Save_File_Format) and format research from FallrimTools (ReSaver by Mark Fairchild) / [ReSaver++](https://github.com/Tonycubed2/ResaverPP) (Apache-2.0).
