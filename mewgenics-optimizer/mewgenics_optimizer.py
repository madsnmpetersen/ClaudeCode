#!/usr/bin/env python3
"""
Mewgenics Breeding Optimizer
Reads a Mewgenics .sav file and outputs the optimal room arrangement.

Usage:
  python3 mewgenics_optimizer.py --save <file.sav>
  python3 mewgenics_optimizer.py --save <file.sav> --calibrate Dzuba 7 3 5 9 6 8 4
  python3 mewgenics_optimizer.py --inspect <file.sav>
"""

import argparse, sqlite3, struct, sys, json, os, re, urllib.request, shutil
from dataclasses import dataclass, field
from typing import Optional
from tabulate import tabulate
import lz4.block

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = os.path.expanduser("~/Desktop/mewgenics_config.json")
DEFAULT_CONFIG = {
    "save_url":          "",
    "save_local":        "~/Desktop/steamcampaign01.sav",
    "furniture_db":      "~/Desktop/furniture_effects.json",
    "gpak_url":          "",
    "kittens":           [],
    "discard_threshold": 20,
}

def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path) as f:
                loaded = json.load(f)
            cfg.update({k: v for k, v in loaded.items() if k != "_comment"})
        except Exception as e:
            print(f"Warning: could not read config {path}: {e}")
    return cfg

def save_config(cfg: dict, path: str = DEFAULT_CONFIG_PATH):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

def run_setup(config_path: str = DEFAULT_CONFIG_PATH):
    """Interactive first-time configuration wizard."""
    cfg = load_config(config_path)
    print("\n─── Mewgenics Optimizer Setup ───\n")
    print("Press Enter to keep the current value shown in [brackets].\n")

    def ask(prompt, current):
        val = input(f"  {prompt} [{current}]: ").strip()
        return val if val else current

    print("── Save file ──")
    print("  The save file lives on your Windows PC.")
    print("  Option A: point a Python HTTP server at the save folder and give the URL.")
    print("  Option B: provide a direct local path (e.g. if mounted via SMB).\n")
    cfg["save_url"]   = ask("HTTP URL to save file (leave blank for local-only)",
                             cfg.get("save_url", ""))
    cfg["save_local"] = ask("Local path to cache the save file",
                             cfg.get("save_local", DEFAULT_CONFIG["save_local"]))

    print("\n── Furniture database ──")
    print("  furniture_effects.json is extracted from resources.gpak.")
    print("  If you already have it, provide its path.")
    print("  If not, provide the HTTP URL to resources.gpak and run --extract-furniture.\n")
    cfg["furniture_db"] = ask("Path to furniture_effects.json",
                               cfg.get("furniture_db", DEFAULT_CONFIG["furniture_db"]))
    cfg["gpak_url"]     = ask("HTTP URL to resources.gpak (for --extract-furniture)",
                               cfg.get("gpak_url", ""))

    print("\n── Playthrough settings ──")
    kittens_str = ", ".join(cfg.get("kittens", []))
    new_kittens = ask("Comma-separated kitten names (cats too young to breed)", kittens_str)
    cfg["kittens"] = [k.strip() for k in new_kittens.split(",") if k.strip()]

    save_config(cfg, config_path)
    print(f"\n  Config saved to {config_path}\n")
    return cfg

def refresh_save(cfg: dict) -> str:
    """Download the latest save file from the configured URL."""
    url  = cfg.get("save_url", "")
    dest = os.path.expanduser(cfg.get("save_local", DEFAULT_CONFIG["save_local"]))
    if not url:
        print("No save_url configured. Use --setup to set one, or pass --save directly.")
        sys.exit(1)
    print(f"Downloading save from {url} …", end="", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
        print(f" done ({os.path.getsize(dest):,} bytes)")
    except Exception as e:
        print(f" FAILED: {e}")
        sys.exit(1)
    return dest


def refresh(cfg: dict, config_path: str, force_furniture: bool = False):
    """Refresh save file and check for furniture database updates."""
    save_path = refresh_save(cfg)
    refresh_furniture_db(cfg, config_path, force=force_furniture)
    return save_path

def _gpak_remote_size(url: str) -> Optional[int]:
    """Return the Content-Length of the GPAK via HEAD request, or None on failure."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=5) as resp:
            cl = resp.headers.get('Content-Length')
            return int(cl) if cl else None
    except Exception:
        return None


def refresh_furniture_db(cfg: dict, config_path: str, force: bool = False):
    """Re-extract furniture_effects.json from resources.gpak if the GPAK has changed.

    Uses the file size from a HEAD request as a cheap change-detection heuristic.
    Downloads the full ~4.9 GB GPAK only when the size differs from the last known value.
    Skips silently if no gpak_url is configured.
    """
    url  = cfg.get("gpak_url", "")
    dest = os.path.expanduser(cfg.get("furniture_db", DEFAULT_CONFIG["furniture_db"]))

    if not url:
        return  # no GPAK URL configured, skip silently

    needs_extract = force or not os.path.exists(dest)

    if not needs_extract:
        remote_size = _gpak_remote_size(url)
        last_size   = cfg.get("_gpak_last_size")
        if remote_size is not None and remote_size != last_size:
            print(f"  Game update detected (GPAK size changed). Updating furniture database…")
            needs_extract = True
        elif remote_size is None:
            print(f"  Could not reach GPAK server — skipping furniture update.")

    if not needs_extract:
        return

    print(f"  Downloading resources.gpak from {url}")
    print(f"  (Large file ~4.9 GB — this takes a few minutes on a local network)")
    gpak_local = "/tmp/mewgenics_resources.gpak"
    try:
        with urllib.request.urlopen(url, timeout=600) as resp, open(gpak_local, "wb") as out:
            downloaded = shutil.copyfileobj(resp, out)
    except Exception as e:
        print(f"  Download failed: {e}")
        return

    print("  Extracting furniture_effects.gon from GPAK…")
    try:
        data = open(gpak_local, "rb").read()
        entries, o = [], 4
        while o < len(data) - 6:
            nlen = struct.unpack_from('<H', data, o)[0]
            if nlen == 0 or nlen > 200: break
            name_b = data[o+2:o+2+nlen]
            if not all(0x20 <= b < 0x7f or b == 0x2f for b in name_b): break
            entries.append((name_b.decode('ascii'), struct.unpack_from('<I', data, o+2+nlen)[0]))
            o += 2 + nlen + 4
        file_off = o
        for name, size in entries:
            if name == 'data/furniture_effects.gon':
                gon_text = data[file_off:file_off+size].decode('utf-8', errors='replace')
                _parse_and_save_furniture_db(gon_text, dest)
                # Remember the GPAK size so we don't re-download unnecessarily
                cfg["_gpak_last_size"] = len(data)
                save_config(cfg, config_path)
                print(f"  Furniture database updated ({dest})")
                return
            file_off += size
        print("  data/furniture_effects.gon not found in GPAK.")
    except Exception as e:
        print(f"  Extraction failed: {e}")
    finally:
        if os.path.exists(gpak_local):
            os.remove(gpak_local)

def _parse_and_save_furniture_db(gon_text: str, dest: str):
    pattern = re.compile(r'^([a-zA-Z0-9_]+)\s*\{([^{}]*)\}', re.MULTILINE)
    STAT_KEYS = ['Comfort','Stimulation','Health','Appeal','Mutation','Evolution',
                 'InheritAbilityChance','InheritSecondAbilityChance','InheritPassiveChance',
                 'KittenChanceOfExtraStr','KittenChanceOfExtraDex','KittenChanceOfExtraCon',
                 'KittenChanceOfExtraInt','KittenChanceOfExtraCha','KittenChanceOfExtraSpd',
                 'KittenChanceOfExtraLck','IncreaseFertility','IncreaseRoomBreedChance']
    db = {}
    for m in pattern.finditer(gon_text):
        item_id, body = m.group(1), m.group(2)
        if 'removed true' in body: continue
        stats = {}
        for key in STAT_KEYS:
            match = re.search(rf'\b{key}\s+(-?\d+(?:\.\d+)?)', body)
            if match: stats[key] = float(match.group(1))
        if stats: db[item_id] = stats
    with open(os.path.expanduser(dest), 'w') as f:
        json.dump(db, f, indent=2)

# ---------------------------------------------------------------------------
# Furniture effects database
# ---------------------------------------------------------------------------
FURN_DB: dict = {}

def _load_furniture_db(path: str):
    global FURN_DB
    p = os.path.expanduser(path)
    if os.path.exists(p):
        with open(p) as f:
            FURN_DB = json.load(f)
    else:
        print(f"Warning: furniture database not found at {p}")
        print("  Run with --extract-furniture to generate it from resources.gpak.")

# ---------------------------------------------------------------------------
# Stat configuration
# ---------------------------------------------------------------------------
STATS = ["str", "dex", "con", "int", "spd", "cha", "lck"]
STAT_LABELS = {"str":"STR","dex":"DEX","con":"CON","int":"INT",
               "spd":"SPD","cha":"CHA","lck":"LCK"}

# Stat → ability keywords for synergy scoring
STAT_SYNERGY = {
    "str": ["melee","slash","strike","smash","bash","cleave","fury","warrior"],
    "dex": ["ranged","shoot","arrow","gun","snipe","throw","volley"],
    "int": ["mana","spell","magic","arcane","mystic","cast","hex","curse"],
    "cha": ["charm","aura","summon","rally","inspire","command"],
    "spd": ["dash","rush","leap","dodge","evade","sprint","swift"],
}
ANTI_INT_KEYWORDS = ["berserker","rage","zoomzerk","merciless"]

STIMULATION_THRESHOLDS = [(196,"2nd active guaranteed"),(95,"Passive guaranteed"),
                           (32,"1st active guaranteed"),(0,"No guarantee")]

# Calibration: maps float position index (0-6) → stat name
# Updated by --calibrate or auto-detection. Default order until calibrated.
STAT_FLOAT_ORDER = ["str","dex","con","int","spd","cha","lck"]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Ability:
    name: str
    ability_type: str  # "active" | "passive"
    scales_with: Optional[str] = None  # stat name

@dataclass
class Cat:
    cat_id: int
    name: str
    stats: dict = field(default_factory=dict)  # genetic stats: STR DEX CON INT SPD CHA LCK in [1,7]
    abilities: list = field(default_factory=list)
    inbreeding_pct: float = 0.0
    parent_ids: list = field(default_factory=list)
    room_name: Optional[str] = None
    is_kitten: bool = False
    has_adventured: bool = False

    def stat_total(self):
        return sum(self.stats.values()) if self.stats else 0

    def synergy_score(self):
        score = 0.0
        for ab in self.abilities:
            abl = ab.name.lower()
            for stat, kws in STAT_SYNERGY.items():
                if any(kw in abl for kw in kws):
                    score += self.stats.get(stat, 3) * 0.5
            if any(kw in abl for kw in ANTI_INT_KEYWORDS):
                score -= self.stats.get("int", 3) * 0.8
        return max(score, 0.0)

@dataclass
class FurnitureItem:
    item_id: int
    name: str
    room_name: str
    x: int = 0
    y: int = 0

@dataclass
class Room:
    name: str
    stats: dict = field(default_factory=dict)  # comfort, stimulation, health, mutation, appeal
    cat_names: list = field(default_factory=list)
    furniture: list = field(default_factory=list)

# ---------------------------------------------------------------------------
# Save file parser
# ---------------------------------------------------------------------------
def extract_cat_name(blob: bytes) -> str:
    n = struct.unpack_from('<H', blob, 17)[0]
    mystery = blob[21]
    # cats with mystery_byte=0xf2 have an extra byte at position 22; name starts at 23
    start = 23 if mystery == 0xf2 else 22
    raw = blob[start:start + 2*n - 1]
    return bytes(raw[i] for i in range(0, len(raw), 2)).decode('ascii', errors='replace').strip()

def decompress_cat_blob(blob: bytes) -> bytes:
    """Cat blobs: first 4 bytes = uncompressed size, rest = LZ4 block compressed."""
    size = struct.unpack_from('<I', blob, 0)[0]
    return lz4.block.decompress(blob[4:], uncompressed_size=size)

def extract_genetic_stats(blob: bytes) -> list:
    """Read the 7 genetic/base stats from a decompressed cat blob.
    Stats are 7 consecutive u32 little-endian values in range [1,7].
    Located at roughly byte 0x1C0-0x1E0 in the decompressed data.
    Order: STR, DEX, CON, INT, SPD, CHA, LCK.
    These are the GENETIC values (what offspring inherit), NOT the
    displayed stats (which include adventure bonuses and injury penalties).
    """
    try:
        dec = decompress_cat_blob(blob)
    except Exception:
        return []
    for off in range(0x80, min(len(dec) - 28, 0x300)):
        vals = [struct.unpack_from('<I', dec, off + i*4)[0] for i in range(7)]
        if all(1 <= v <= 7 for v in vals):
            return list(vals)
    return []

def extract_cat_abilities(blob: bytes) -> list:
    """Extract ability names from the cat blob (ASCII strings in ability section)."""
    # Abilities appear after the stat section as ASCII strings following known patterns
    abilities = []
    # Look for ability markers: sequences of printable ASCII chars (length 3-30)
    none_pos = blob.find(b'None', 30)
    if none_pos < 0:
        return abilities
    # Scan for ASCII string chunks after position none_pos+80
    i = none_pos + 80
    while i < len(blob) - 4:
        # Try to read a length-prefixed ASCII string
        slen = struct.unpack_from('<I', blob, i)[0] if i + 4 <= len(blob) else 0
        if 3 <= slen <= 30 and i + 4 + slen <= len(blob):
            candidate = blob[i+4:i+4+slen]
            if all(32 <= b < 127 for b in candidate):
                name = candidate.decode('ascii')
                if name not in ('None', 'male', 'female') and not name.startswith('Floor') and not name.startswith('Attic'):
                    abilities.append(Ability(name=name, ability_type="unknown"))
                i += 4 + slen
                continue
        i += 1
    return abilities[:6]

def parse_furniture_blob(blob: bytes) -> Optional[FurnitureItem]:
    try:
        o = 4  # skip version int32
        nlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
        name = blob[o:o+nlen].decode('ascii'); o += nlen
        o += 8  # padding
        rlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
        room = blob[o:o+rlen].decode('ascii'); o += rlen
        x = struct.unpack_from('<i', blob, o)[0]; o += 4
        y = struct.unpack_from('<i', blob, o)[0]
        return FurnitureItem(item_id=0, name=name, room_name=room, x=x, y=y)
    except Exception:
        return None

def compute_room_stats_from_furniture(conn) -> dict[str, dict]:
    """Compute room stats by summing furniture effects for each room.
    Applies cat-count Comfort penalty (-1 per cat above 4).
    Returns {room_name: {stat_name: value}}.
    """
    room_stats = {}  # room_name → {stat: total}
    room_furn = {}   # room_name → list of furniture names (for display)

    for row in conn.execute("SELECT data FROM furniture"):
        try:
            blob = row[0]
            o = 4
            nlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
            name = blob[o:o+nlen].decode('ascii'); o += nlen
            o += 8
            rlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
            room = blob[o:o+rlen].decode('ascii')
        except Exception:
            continue

        effects = FURN_DB.get(name, {})
        room_stats.setdefault(room, {})
        room_furn.setdefault(room, []).append(name)
        for stat, val in effects.items():
            room_stats[room][stat] = room_stats[room].get(stat, 0.0) + val

    # Normalise key names to lowercase
    result = {}
    for room, stats in room_stats.items():
        result[room] = {k.lower(): v for k, v in stats.items()}
        result[room]['_furniture'] = room_furn.get(room, [])
    return result

def parse_cat_room(blob: bytes) -> Optional[str]:
    """Extract which room a cat is in from blob data."""
    for room_name in [b'Floor1_Large', b'Attic', b'Floor1_Small']:
        pos = blob.find(room_name)
        if pos >= 0:
            return room_name.decode('ascii')
    return None

def parse_active_cat_ids(conn) -> set:
    """Read house_state to find which cat IDs are currently in the house."""
    try:
        blob = conn.execute("SELECT data FROM files WHERE key='house_state'").fetchone()[0]
    except Exception:
        return set()
    active = set()
    o = 8
    while o + 8 <= len(blob):
        rlen = struct.unpack_from('<Q', blob, o)[0]
        if rlen > 30:
            o += 1
            continue
        room_end = o + 8 + rlen
        data_end  = room_end + 32
        if data_end > len(blob):
            break
        if rlen and not all(32 <= b < 127 for b in blob[o+8:room_end]):
            o += 1
            continue
        eid = struct.unpack_from('<I', blob, room_end + 24)[0]
        if eid > 0:
            active.add(eid)
        o = data_end
    return active


def parse_save(save_path: str):
    try:
        conn = sqlite3.connect(save_path)
    except Exception as e:
        sys.exit(f"Cannot open save file: {e}")

    cats = []
    rooms_dict = {}

    active_ids = parse_active_cat_ids(conn)

    # Parse cats
    for row in conn.execute("SELECT key, data FROM cats ORDER BY key"):
        blob = row[1]
        try:
            name = extract_cat_name(blob)
            if not name or len(name) < 1:
                name = f"Cat_{row[0]}"
            abilities = extract_cat_abilities(blob)
            room_name = parse_cat_room(blob)
            # Skip cats not found in house_state (retired/released)
            if active_ids and row[0] not in active_ids:
                continue

            genetic = extract_genetic_stats(blob)
            stats_dict = dict(zip(STATS, genetic)) if genetic else {}

            cat = Cat(
                cat_id=row[0],
                name=name,
                stats=stats_dict,
                abilities=abilities,
                room_name=room_name,
            )
            cats.append(cat)
        except Exception as e:
            pass  # skip malformed cat entries

    # Discover all unlocked rooms from house_unlocks, canonicalize names
    known_rooms = {'Floor1_Large', 'Attic'}
    try:
        hu = conn.execute("SELECT data FROM files WHERE key='house_unlocks'").fetchone()[0]
        import re as _re
        for s in _re.findall(rb'[\x20-\x7e]{4,}', hu):
            name = canonicalize_room(s.decode('ascii'))
            if name.startswith('Floor') or name == 'Attic':
                known_rooms.add(name)
    except Exception:
        pass

    # Load all furniture items with their effects
    all_furniture = []  # list of (FurnitureItem, effects_dict)
    try:
        o = 4
        def _pfurn(blob):
            o = 4
            nlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
            name = blob[o:o+nlen].decode('ascii'); o += nlen
            o += 8
            rlen = struct.unpack_from('<Q', blob, o)[0]; o += 8
            room = blob[o:o+rlen].decode('ascii')
            return name, room
        for row in conn.execute("SELECT key, data FROM furniture"):
            name, room = _pfurn(row[1])
            fx = FURN_DB.get(name, {})
            all_furniture.append((FurnitureItem(item_id=row[0], name=name, room_name=room), fx))
    except Exception:
        pass

    # Compute room stats from furniture (used as starting point; optimizer will reassign)
    room_data = compute_room_stats_from_furniture(conn)
    for room_name in known_rooms:
        if room_name not in room_data:
            room_data[room_name] = {}
    for room_name, stats in room_data.items():
        stats.pop('_furniture', None)
        rooms_dict[room_name] = Room(name=room_name, stats=stats)

    rooms = list(rooms_dict.values())
    conn.close()
    return cats, rooms, all_furniture

# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def calibrate(cats: list, cat_name: str, stat_values: list[int]):
    """Use known in-game stat values (STR DEX CON INT SPD CHA LCK) to map float positions."""
    global STAT_FLOAT_ORDER
    target = next((c for c in cats if c.name.lower() == cat_name.lower()), None)
    if not target:
        print(f"Cat '{cat_name}' not found. Available: {[c.name for c in cats]}")
        return

    floats = target.stat_floats
    float_vals = [float_to_stat(f) for f in floats]
    print(f"\nCalibrating with {target.name}")
    print(f"  Extracted floats: {[round(f,4) for f in floats]}")
    print(f"  Float→stat estimates: {float_vals}")
    print(f"  Known stats (STR DEX CON INT SPD CHA LCK): {stat_values}")

    # Value-based matching: for each extracted float value, find which stat name matches
    # Works best when stat values are unique; duplicate values (e.g. DEX=CHA=5) are noted
    stat_map = {}  # float_value → stat_name (for unique values only)
    value_to_stats = {}
    for i, (stat, val) in enumerate(zip(STATS, stat_values)):
        value_to_stats.setdefault(val, []).append(stat)
    unique_values = {v: stats[0] for v, stats in value_to_stats.items() if len(stats) == 1}

    new_order = ['?'] * max(7, len(floats))
    matched = set()
    for float_idx, fval in enumerate(float_vals):
        if fval in unique_values and unique_values[fval] not in matched:
            new_order[float_idx] = unique_values[fval]
            matched.add(unique_values[fval])

    STAT_FLOAT_ORDER = new_order
    print(f"  Calibrated float order: {STAT_FLOAT_ORDER}")
    unmatched = [s for s in STATS if s not in matched]
    if unmatched:
        print(f"  Could not assign (duplicate values or missing floats): {unmatched}")

    # Recompute stats for all cats
    for cat in cats:
        cat.stats = {}
        for i, stat in enumerate(STAT_FLOAT_ORDER[:len(cat.stat_floats)]):
            if stat != '?':
                cat.stats[stat] = float_to_stat(cat.stat_floats[i])

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def p_inherit_higher(stimulation: float) -> float:
    stim = max(0, int(stimulation))
    return (100 + stim) / (200 + stim)

def score_pair(cat_a: Cat, cat_b: Cat, stimulation: float, all_cats: list) -> float:
    p = p_inherit_higher(stimulation)
    # Stat score: expected offspring genetic value per stat
    stat_score = 0.0
    if cat_a.stats and cat_b.stats:
        for stat in STATS:
            a, b = cat_a.stats.get(stat, 1), cat_b.stats.get(stat, 1)
            hi, lo = max(a, b), min(a, b)
            stat_score += hi * p + lo * (1 - p)
        stat_score /= (len(STATS) * 7)  # normalise to [0,1] (max genetic stat = 7)

    # Inbreeding penalty
    shared = set(cat_a.parent_ids) & set(cat_b.parent_ids)
    inbreed_frac = (cat_a.inbreeding_pct + cat_b.inbreeding_pct) / 200.0
    if shared:
        inbreed_frac = 1.0
    inbreed_bonus = max(0.0, 1.0 - inbreed_frac * 2)

    # Synergy
    synergy = (cat_a.synergy_score() + cat_b.synergy_score()) / 2
    synergy_norm = min(synergy / 30.0, 1.0)

    # Abilities
    ab_score = min((len(cat_a.abilities) + len(cat_b.abilities)) / 6.0, 1.0)

    return stat_score * 0.55 + inbreed_bonus * 0.25 + synergy_norm * 0.15 + ab_score * 0.05

def score_cat(cat: Cat) -> float:
    return cat.stat_total() / 10.0 - cat.inbreeding_pct / 100.0 * 3 + cat.synergy_score() / 20.0

# ---------------------------------------------------------------------------
# Room layout optimizer
# ---------------------------------------------------------------------------
def furniture_breeding_score(effects: dict) -> float:
    """Score a furniture item by its value for a breeding room."""
    return (effects.get('Stimulation', 0) * 3
            + effects.get('InheritAbilityChance', 0) * 2
            + effects.get('Comfort', 0) * 1
            + effects.get('Appeal', 0) * 0.3)


def assign_furniture(all_furniture: list, rooms: list) -> dict:
    """Distribute furniture across rooms optimally.
    Stimulation/InheritAbility items always go to the primary breeding room.
    Comfort items are distributed so every room reaches Comfort ≥ 3.
    Returns {room_name: [FurnitureItem, ...]}
    """
    if not rooms:
        return {}

    primary = next((r for r in rooms if 'Large' in r.name), rooms[0])
    others  = [r for r in rooms if r is not primary]

    assigned: dict[str, list] = {r.name: [] for r in rooms}

    # Sort by breeding priority: Stimulation/InheritAbility first, then Comfort, then rest
    sorted_furn = sorted(all_furniture, key=lambda x: furniture_breeding_score(x[1]), reverse=True)

    # Pass 1: all high-priority items (Stimulation, InheritAbility) → primary
    remaining = []
    for item, fx in sorted_furn:
        if fx.get('Stimulation', 0) > 0 or fx.get('InheritAbilityChance', 0) > 0:
            assigned[primary.name].append(item)
        else:
            remaining.append((item, fx))

    # Pass 2: distribute Comfort items so secondary rooms reach Comfort ≥ 3
    COMFORT_TARGET = 3
    comfort_items   = [(item, fx) for item, fx in remaining if fx.get('Comfort', 0) > 0]
    non_comfort     = [(item, fx) for item, fx in remaining if fx.get('Comfort', 0) <= 0]

    room_comfort = {r.name: 0.0 for r in rooms}
    for item, fx in comfort_items:
        # Find the room most in need (furthest below target), prefer secondary rooms
        need = [(r, COMFORT_TARGET - room_comfort[r.name]) for r in others
                if room_comfort[r.name] < COMFORT_TARGET]
        if need:
            neediest = max(need, key=lambda x: x[1])[0]
            assigned[neediest.name].append(item)
            room_comfort[neediest.name] += fx.get('Comfort', 0)
        else:
            assigned[primary.name].append(item)

    # Pass 3: everything else → primary
    for item, fx in non_comfort:
        assigned[primary.name].append(item)

    return assigned


def room_breeding_score(group: list, comfort_furniture: float, stim: float) -> float:
    """Expected breeding output for a group of cats in one room.
    = effective_Comfort × Σ(pair_score for every combination in the group)
    """
    n = len(group)
    if n < 2:
        return 0.0
    comfort_penalty = max(0, n - 4)
    effective_comfort = max(0.0, comfort_furniture - comfort_penalty)
    if effective_comfort == 0:
        return 0.0
    pair_sum = sum(
        score_pair(group[i], group[j], stim, group)
        for i in range(n) for j in range(i + 1, n)
    )
    return effective_comfort * pair_sum


def effective_comfort(comfort_from_furniture: float, n_cats: int) -> float:
    return max(0.0, comfort_from_furniture - max(0, n_cats - 4))


def breeding_potential(comfort: float, n_cats: int) -> float:
    """Comfort × number of possible pairs — proxy for kittens-per-night."""
    pairs = n_cats * (n_cats - 1) / 2
    return comfort * pairs


def greedy_cat_assignment(breeders: list, kittens: list, rooms: list,
                          all_furniture: list, stim: float) -> tuple[dict, dict, list]:
    """
    Assign every breeder cat to a room to maximise total breeding potential
    (sum over rooms of: effective_Comfort × Σpair_scores).

    Works for any number of rooms. Returns:
      groups      — {room_name: [Cat, ...]}
      furn_alloc  — {room_name: [FurnitureItem, ...]}
      groups_list — list of groups in room order
    """
    n_rooms = len(rooms)
    if n_rooms == 0:
        return {}, {}, []

    total_comfort = sum(FURN_DB.get(f.name, {}).get('Comfort', 0) for f, _ in all_furniture)
    total_stim    = sum(FURN_DB.get(f.name, {}).get('Stimulation', 0) for f, _ in all_furniture)

    # Kittens always go to the last room
    groups: dict[str, list] = {r.name: [] for r in rooms}
    for k in kittens:
        groups[rooms[-1].name].append(k)

    # Precompute all pair scores
    pair_score_cache: dict[tuple, float] = {}
    for i, a in enumerate(breeders):
        for j, b in enumerate(breeders):
            if i < j:
                s = score_pair(a, b, stim, breeders)
                pair_score_cache[(a.cat_id, b.cat_id)] = s
                pair_score_cache[(b.cat_id, a.cat_id)] = s

    def group_pair_sum(group):
        total = 0.0
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                total += pair_score_cache.get(
                    (group[i].cat_id, group[j].cat_id), 0.0)
        return total

    def room_score(room_name, extra_cat, comfort_per_room):
        """Marginal score gain from adding extra_cat to this room."""
        current = groups[room_name]
        n_new = len(current) + 1
        c_new = effective_comfort(comfort_per_room[room_name], n_new)
        c_old = effective_comfort(comfort_per_room[room_name], len(current))
        # New pairs introduced by adding this cat
        new_pair_sum = sum(
            pair_score_cache.get((extra_cat.cat_id, x.cat_id), 0.0)
            for x in current if not x.is_kitten
        )
        old_pair_sum = group_pair_sum([x for x in current if not x.is_kitten])
        return c_new * (old_pair_sum + new_pair_sum) - c_old * old_pair_sum

    # Estimate Comfort per room: distribute proportionally to room index
    # (will be refined after cat assignment)
    comfort_per_room = {r.name: total_comfort / n_rooms for r in rooms}

    # Greedy: assign each cat to the room where it adds the most breeding potential
    for cat in sorted(breeders, key=lambda c: -score_cat(c)):
        gains = {r.name: room_score(r.name, cat, comfort_per_room) for r in rooms}
        best_room = max(gains, key=gains.get)
        groups[best_room].append(cat)

    # Optimise furniture split given final cat counts
    n_cats_per_room = [len(groups[r.name]) for r in rooms]
    comfort_targets = _optimal_comfort_split(total_comfort, n_cats_per_room)
    comfort_per_room = {rooms[i].name: comfort_targets[i] for i in range(n_rooms)}

    furn_alloc = _distribute_furniture(all_furniture, rooms, comfort_targets, total_stim)
    return groups, furn_alloc, [groups[r.name] for r in rooms]


def _optimal_comfort_split(total_comfort: float, n_cats_per_room: list[int]) -> list[float]:
    """Find the Comfort-furniture allocation across N rooms that maximises
    sum of (effective_Comfort × pairs) for each room."""
    n = len(n_cats_per_room)
    if n == 0:
        return []
    if n == 1:
        return [total_comfort]

    # Enumerate integer splits (fast for small total_comfort)
    best, best_alloc = -1.0, [total_comfort / n] * n

    def _search(room_idx, remaining, current):
        nonlocal best, best_alloc
        if room_idx == n - 1:
            current.append(remaining)
            score = sum(
                effective_comfort(current[i], n_cats_per_room[i])
                * n_cats_per_room[i] * (n_cats_per_room[i] - 1) / 2
                for i in range(n)
            )
            if score > best:
                best = score
                best_alloc = list(current)
            current.pop()
            return
        for c in range(0, int(remaining) + 1):
            current.append(float(c))
            _search(room_idx + 1, remaining - c, current)
            current.pop()

    if total_comfort <= 15:  # exact for small budgets
        _search(0, total_comfort, [])
    else:
        # Proportional split heuristic for large budgets
        total_cats = sum(n_cats_per_room)
        best_alloc = [total_comfort * n_i / max(total_cats, 1)
                      for n_i in n_cats_per_room]

    return best_alloc


def _distribute_furniture(all_furniture, rooms, comfort_targets, total_stim):
    """Assign individual furniture items to rooms to match computed Comfort targets.
    Stimulation items always go to the first room (primary breeding room).
    """
    assigned: dict[str, list] = {r.name: [] for r in rooms}

    stim_items    = [(f, fx) for f, fx in all_furniture if fx.get('Stimulation', 0) > 0]
    comfort_items = sorted(
        [(f, fx) for f, fx in all_furniture if fx.get('Stimulation', 0) == 0],
        key=lambda x: -x[1].get('Comfort', 0)
    )

    # Stimulation → room 0 (primary)
    for f, _ in stim_items:
        assigned[rooms[0].name].append(f)

    # Comfort items → fill toward targets
    room_comfort = [0.0] * len(rooms)
    for f, fx in comfort_items:
        c = fx.get('Comfort', 0)
        needs = [max(0.0, comfort_targets[i] - room_comfort[i]) for i in range(len(rooms))]
        dest  = needs.index(max(needs))
        assigned[rooms[dest].name].append(f)
        room_comfort[dest] += c

    return assigned


def optimize(cats: list, rooms: list, all_furniture: list, discard_pct: int = 20) -> dict:
    """
    Optimise for maximum kitten production across all available rooms.
    Every cat is assigned to a room or released — no holding.
    Works for any number of rooms.
    """
    adults  = [c for c in cats if not c.is_kitten]
    kittens = [c for c in cats if c.is_kitten]

    scores = {c.cat_id: score_cat(c) for c in adults}
    mn, mx = (min(scores.values()), max(scores.values())) if scores else (0, 1)
    rng    = max(mx - mn, 0.001)
    percentiles = {cid: int((s - mn) / rng * 100) for cid, s in scores.items()}

    discard = [c for c in adults
               if percentiles.get(c.cat_id, 100) < discard_pct or c.inbreeding_pct > 50]
    discard_ids = {c.cat_id for c in discard}
    breeders    = [c for c in adults if c.cat_id not in discard_ids]

    stim = max((r.stats.get("stimulation", 0) for r in rooms), default=0)

    if not rooms:
        return {"cat_room": {}, "pairs_per_room": {}, "discard": discard,
                "kittens": kittens, "percentiles": percentiles,
                "furn_assignment": {}, "rooms": rooms}

    groups, furn_assignment, groups_list = greedy_cat_assignment(
        breeders, kittens, rooms, all_furniture, stim)

    cat_room: dict[int, str] = {}
    for rname, group in groups.items():
        for c in group:
            cat_room[c.cat_id] = rname

    # Top pairs per room for display
    pairs_per_room: dict[str, list] = {}
    for room in rooms:
        group = [c for c in groups[room.name] if not c.is_kitten]
        room_pairs = sorted(
            [(score_pair(group[i], group[j], stim, group), group[i], group[j])
             for i in range(len(group)) for j in range(i + 1, len(group))],
            key=lambda x: x[0], reverse=True
        )
        pairs_per_room[room.name] = room_pairs[:3]

    return {
        "cat_room":        cat_room,
        "groups":          groups,
        "pairs_per_room":  pairs_per_room,
        "discard":         discard,
        "kittens":         kittens,
        "percentiles":     percentiles,
        "furn_assignment": furn_assignment,
        "rooms":           rooms,
    }

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def stim_label(stim: float) -> str:
    for threshold, label in STIMULATION_THRESHOLDS:
        if stim >= threshold:
            sym = "✓" if stim >= 32 else "✗"
            return f"{stim:.1f} {sym} ({label})"
    return str(stim)

# Canonical room display names
ROOM_DISPLAY = {
    'Floor1_Large': 'Main Room (Floor1_Large)',
    'SmallHouse_Attic': 'Attic',
    'Attic': 'Attic',
}

def room_display_name(name: str) -> str:
    return ROOM_DISPLAY.get(name, name)

# Treat SmallHouse_Attic and Attic as the same room
ROOM_CANONICAL = {'SmallHouse_Attic': 'Attic'}

def canonicalize_room(name: str) -> str:
    return ROOM_CANONICAL.get(name, name)


def compute_stats_with_furniture(furniture: list, n_cats: int) -> dict:
    """Compute room stats purely from assigned furniture + cat-count Comfort penalty."""
    stats: dict = {}
    for item in furniture:
        for k, v in FURN_DB.get(item.name, {}).items():
            stats[k.lower()] = stats.get(k.lower(), 0) + v
    comfort_penalty = max(0, n_cats - 4)
    if comfort_penalty:
        stats['comfort'] = stats.get('comfort', 0) - comfort_penalty
    return stats


def print_results(layout: dict, cats: list, rooms: list, all_furniture: list):  # noqa: C901
    cat_by_id      = {c.cat_id: c for c in cats}
    furn_by_room   = layout["furn_assignment"]
    cat_room       = layout["cat_room"]
    pairs_per_room = layout["pairs_per_room"]
    groups         = layout["groups"]

    W = 65
    print()
    print("═" * W)
    print("  MEWGENICS OPTIMIZER")
    print("═" * W)

    # ── ROOM SUMMARY ─────────────────────────────────────────────────────────
    for room in rooms:
        rname = room.name
        assigned_furn = furn_by_room.get(rname, [])
        room_group    = [c for c in groups.get(rname, []) if not c.is_kitten]
        room_kittens  = [c for c in groups.get(rname, []) if c.is_kitten]
        n_cats        = len(groups.get(rname, []))

        eff     = compute_stats_with_furniture(assigned_furn, n_cats)
        comfort = eff.get('comfort', 0)
        stim    = eff.get('stimulation', 0)
        health  = eff.get('health', 0)
        appeal  = eff.get('appeal', 0)

        top_pairs = pairs_per_room.get(rname, [])

        print(f"\n  Room: {room_display_name(rname)}  [{n_cats} cats]")
        print(f"  Stats:  Comfort {comfort:.0f}  Stimulation {stim:.0f}  "
              f"Health {health:.0f}  Appeal {appeal:.0f}")

        if comfort <= 0 and room_group:
            print(f"  No breeding (Comfort=0) — cats here are on standby.")
        elif top_pairs:
            print(f"  Top pairings possible here:")
            for score, a, b in top_pairs:
                ga = sum(a.stats.values()) if a.stats else 0
                gb = sum(b.stats.values()) if b.stats else 0
                print(f"    {a.name} × {b.name}  "
                      f"(score {score:.2f}  |  genetics {ga} × {gb})")

        if assigned_furn:
            print(f"  Furniture ({len(assigned_furn)}):")
            for f in assigned_furn:
                fx = FURN_DB.get(f.name, {})
                bonuses = "  ".join(
                    f"{k}{'+' if v > 0 else ''}{v:.0f}" for k, v in fx.items()
                ) or "no effect"
                print(f"    {f.name:<40} {bonuses}")
        else:
            print(f"  Furniture: (none)")

    # ── CAT ASSIGNMENT LIST ───────────────────────────────────────────────────
    print("\n" + "─" * W)
    print("  CAT ASSIGNMENTS")
    print("─" * W)

    rows = []
    # Cats in rooms
    for room in rooms:
        for c in groups.get(room.name, []):
            ga   = sum(c.stats.values()) if c.stats else "?"
            tag  = " [kitten]" if c.is_kitten else ""
            rows.append([c.name + tag, f"→ {room_display_name(room.name)}", ga])
    # Cats to release
    for c in layout["discard"]:
        ga = sum(c.stats.values()) if c.stats else "?"
        rows.append([c.name, "Release / retire", ga])

    print(tabulate(rows, headers=["Cat", "Assignment", "Genetics"], tablefmt="simple"))

    # ── FURNITURE ASSIGNMENT LIST ─────────────────────────────────────────────
    print("\n" + "─" * W)
    print("  FURNITURE ASSIGNMENTS")
    print("─" * W)

    furn_rows = []
    for room in rooms:
        for f in furn_by_room.get(room.name, []):
            fx   = FURN_DB.get(f.name, {})
            stat = "  ".join(
                f"{k}{'+' if v>0 else ''}{v:.0f}" for k, v in fx.items()
            ) or "—"
            furn_rows.append([f.name, f"→ {room_display_name(room.name)}", stat])
    print(tabulate(furn_rows,
                   headers=["Furniture", "Assignment", "Stat effects"],
                   tablefmt="simple"))

    # ── GENETIC STAT TABLE ────────────────────────────────────────────────────
    print("\n" + "─" * W)
    print("  GENETIC STATS  (what offspring inherit, scale 1–7 per stat)")
    print("─" * W)
    stat_rows = []
    for cat in sorted(cats, key=lambda c: (not c.is_kitten, score_cat(c)), reverse=True):
        tag = " [kitten]" if cat.is_kitten else ""
        row = [cat.name + tag] + [cat.stats.get(s, "?") for s in STATS]
        row.append(sum(cat.stats.values()) if cat.stats else "?")
        stat_rows.append(row)
    print(tabulate(stat_rows,
                   headers=["Cat"] + [STAT_LABELS[s] for s in STATS] + ["Total"],
                   tablefmt="simple"))
    print()

# ---------------------------------------------------------------------------
# Inspect mode
# ---------------------------------------------------------------------------
def inspect_save(save_path: str):
    conn = sqlite3.connect(save_path)
    print("=== TABLES ===")
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        print(f"  {row[0]}")
    print("\n=== PROPERTIES (sample) ===")
    for row in conn.execute("SELECT key, data FROM properties LIMIT 20"):
        print(f"  {row[0]}: {row[1]}")
    print("\n=== CAT COUNT ===")
    count = conn.execute("SELECT COUNT(*) FROM cats").fetchone()[0]
    print(f"  {count} cats")
    conn.close()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Mewgenics Breeding Optimizer",
        epilog="Run with --setup on first use to configure save & game file locations.")
    parser.add_argument("--save",     help="Path to .sav file (overrides config)")
    parser.add_argument("--config",   default=DEFAULT_CONFIG_PATH,
                        help="Path to config file")
    parser.add_argument("--setup",           action="store_true",
                        help="Run interactive setup wizard")
    parser.add_argument("--refresh",         action="store_true",
                        help="Re-download save + check for furniture DB updates, then analyse")
    parser.add_argument("--force-furniture", action="store_true",
                        help="Force re-download of resources.gpak and rebuild furniture DB")
    parser.add_argument("--inspect",  metavar="SAVE",
                        help="Inspect raw save file schema and exit")
    parser.add_argument("--discard-threshold", type=int, default=None,
                        help="Release cats in bottom N%% by genetics (default: 20)")
    parser.add_argument("--kittens",  nargs="+", metavar="NAME",
                        help="Cat names to treat as kittens (overrides config)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.setup:
        cfg = run_setup(args.config)
        if not args.refresh and not args.save:
            return  # setup only, no analysis

    _load_furniture_db(cfg.get("furniture_db", DEFAULT_CONFIG["furniture_db"]))

    if args.inspect:
        inspect_save(args.inspect)
        return

    # Resolve save file
    if args.refresh or args.force_furniture:
        save_path = refresh(cfg, args.config,
                            force_furniture=getattr(args, 'force_furniture', False))
    elif args.save:
        save_path = args.save
    else:
        save_path = os.path.expanduser(cfg.get("save_local", ""))
        if not save_path or not os.path.exists(save_path):
            print("No save file found. Options:")
            print("  --refresh          download from configured URL")
            print("  --save PATH        specify path directly")
            print("  --setup            configure paths")
            sys.exit(1)

    discard_pct = (args.discard_threshold
                   if args.discard_threshold is not None
                   else cfg.get("discard_threshold", 20))
    kittens = args.kittens if args.kittens else cfg.get("kittens", [])

    cats, rooms, all_furniture = parse_save(save_path)

    if not cats:
        print("No cats found — check save file path.")
        return

    kitten_set = {n.lower() for n in kittens}
    for cat in cats:
        if cat.name.lower() in kitten_set:
            cat.is_kitten = True

    layout = optimize(cats, rooms, all_furniture, discard_pct)
    print_results(layout, cats, rooms, all_furniture)

if __name__ == "__main__":
    main()
