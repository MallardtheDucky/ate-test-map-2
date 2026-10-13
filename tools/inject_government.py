
import argparse
import csv
import json
import re
import sys
from pathlib import Path

import inject_history as ih
import inject_nations as inn

TIERS = {"b": 0, "c": 1, "d": 2, "k": 3, "e": 4}
TIER_NAMES = {"baron": 0, "count": 1, "duke": 2, "king": 3, "emperor": 4}
HOLDING_FOR = {"castle": "CASTLE", "city": "CITY", "temple": "TEMPLE", "tribal": "TRIBAL"}
FALLBACK = {
    "CASTLE": "feudal_government",
    "CITY": "republic_government",
    "TEMPLE": "theocracy_government",
    "TRIBAL": "tribal_government",
}
TITLE_KEY = re.compile(r"^[bcdke]_")

def pairs_of(path):
    pairs, _ = ih.parse_block(ih.tokenize(ih.read_text(path)), 0)
    return pairs

def load_title_tree(folder):

    tree = {}

    def walk(pairs, parent):
        for key, value in pairs:
            if key and TITLE_KEY.match(key) and isinstance(value, list):
                capital = None
                head_of = None
                for k, v in value:
                    if k == "capital" and isinstance(v, str) and v.isdigit():
                        capital = int(v)
                    elif k == "controls_religion" and isinstance(v, str):
                        head_of = v
                tree[key] = {"capital": capital, "children": [], "head_of": head_of}
                if parent is not None:
                    tree[parent]["children"].append(key)
                walk(value, key)

    for path in sorted(folder.glob("*.txt")):
        walk(pairs_of(path), None)
    return tree

def first_capital(tree, title, seen=None):
    seen = seen or set()
    if title in seen or title not in tree:
        return None
    seen.add(title)
    if tree[title]["capital"]:
        return tree[title]["capital"]
    for child in tree[title]["children"]:
        found = first_capital(tree, child, seen)
        if found:
            return found
    return None

def load_province_holdings(folder):

    out = {}
    for path in sorted(folder.glob("*.txt")):
        match = re.match(r"\s*(\d+)", path.name)
        if not match:
            continue
        county = None
        baronies = []
        for key, value in pairs_of(path):
            if key == "title" and isinstance(value, str):
                county = value
            elif key and key.startswith("b_") and isinstance(value, str) and value in HOLDING_FOR:
                baronies.append((key, value))
        out[int(match.group(1))] = (county, baronies)
    return out

def capital_holding(holdings, province_of_county, titles, county, holder):
    province = province_of_county.get(county)
    if province is None:
        return None
    _, baronies = holdings[province]
    for barony, holding in baronies:
        entry = titles.get(barony)
        if entry is not None and inn.is_held(titles, barony) and entry["holder"] != holder:
            continue
        return holding
    return None


def resolve_capital(own_titles, primary_title, ruler, tree, holdings, province_of_county, title_order):
    held = sorted((t for t in own_titles if t[0] == "c" and t in province_of_county),
                  key=lambda t: title_order.get(t, 10 ** 9))
    explicit = ruler.get("capital") if ruler else None
    if explicit and explicit in province_of_county:
        return explicit, "explicit", held
    ordered = sorted(own_titles, key=lambda t: (-TIERS.get(t[0], -1), t != primary_title, title_order.get(t, 10 ** 9)))
    for title in ordered:
        if title[0] == "c":
            county = title if title in province_of_county else None
        else:
            capital = first_capital(tree, title)
            county = holdings[capital][0] if capital in holdings else None
        if county and county in held:
            return county, "title", held
    if held:
        return held[0], "fallback", held
    return None, "none", held


def load_religion_groups(folder):
    groups = {}
    for path in sorted(folder.glob("*.txt")):
        for group, block in pairs_of(path):
            if not group or not isinstance(block, list):
                continue
            for name, inner in block:
                if name and isinstance(inner, list) and any(k == "color" for k, _ in inner):
                    groups[name] = group
    return groups

def load_temple_rules(folder):

    rules = {}
    for path in sorted(folder.glob("*.txt")):
        for group, block in pairs_of(path):
            if not group or not isinstance(block, list):
                continue
            for name, inner in block:
                if name and isinstance(inner, list) and any(k == "color" for k, _ in inner):
                    male, female = get(inner, "male_temple_holders"), get(inner, "female_temple_holders")
                    rules[name] = (male != "no", female == "yes")
    return rules

def load_culture_groups(folder):

    groups = {}
    for path in sorted(folder.glob("*.txt")):
        for group, block in pairs_of(path):
            if not group or not isinstance(block, list):
                continue
            for name, inner in block:
                if name and isinstance(inner, list) and any(k in ("male_names", "color") for k, _ in inner):
                    groups[name] = group
    return groups

def load_regions(path, tree, province_of_county):

    regions = {}
    for name, block in pairs_of(path):
        if not name or not isinstance(block, list):
            continue
        provinces = set()
        for key, value in block:
            items = [v for k, v in value if k is None and isinstance(v, str)] if isinstance(value, list) else []
            if key == "provinces":
                provinces.update(int(v) for v in items if v.isdigit())
            elif key == "counties":
                provinces.update(province_of_county[v] for v in items if v in province_of_county)
            elif key == "duchies":
                for duchy in items:
                    for county in tree.get(duchy, {}).get("children", []):
                        if county in province_of_county:
                            provinces.add(province_of_county[county])
            elif key == "regions":
                for sub in items:
                    provinces |= regions.get(sub, set())
        regions[name] = provinces
    return regions

def load_rulers(folder, holders, cutoff):

    rulers = {}
    for path in sorted(folder.glob("*.txt")):
        for key, block in pairs_of(path):
            if key in holders and isinstance(block, list):
                rulers[key] = {
                    "religion": inn.latest(block, "religion", cutoff),
                    "culture": inn.latest(block, "culture", cutoff),
                    "capital": inn.latest(block, "capital", cutoff),
                    "name": next((v for k, v in block if k == "name" and isinstance(v, str)), None),
                    "dynasty": next((v for k, v in block if k == "dynasty" and isinstance(v, str)), None),
                    "female": any(k == "female" and v == "yes" for k, v in block),
                }
    return rulers

def load_dynasties(folder):
    names = {}
    for path in sorted(folder.glob("*.txt")):
        for key, block in pairs_of(path):
            if key and isinstance(block, list):
                name = next((v for k, v in block if k == "name" and isinstance(v, str)), None)
                if name:
                    names[key] = name
    return names


def full_name(ruler, dynasties):
    if not ruler or not ruler.get("name"):
        return None
    dynasty = dynasties.get(ruler.get("dynasty")) if ruler.get("dynasty") else None
    return f"{ruler['name']} {dynasty}" if dynasty else ruler["name"]


def load_governments(folder):

    out = []
    for path in sorted(folder.glob("*.txt")):
        for _, block in pairs_of(path):
            if isinstance(block, list):
                for name, definition in block:
                    if name and isinstance(definition, list):
                        out.append((name, definition))
    return out

def get(block, key):
    for k, v in block:
        if k == key:
            return v
    return None

class Evaluator:

    def __init__(self, governments, groups, ctx, unknown=None):
        self.defs = dict(governments)
        self.groups = groups
        self.ctx = ctx
        self.active = set()
        self.unknown = unknown if unknown is not None else set()

    def potential(self, name):
        definition = self.defs.get(name)
        if definition is None:
            return None
        block = get(definition, "potential")
        if not isinstance(block, list):
            return True
        if name in self.active:
            return None
        self.active.add(name)
        try:
            return self.block(block, "AND")
        finally:
            self.active.discard(name)

    @staticmethod
    def land(values):
        if any(v is False for v in values):
            return False
        return None if any(v is None for v in values) else True

    @staticmethod
    def lor(values):
        if any(v is True for v in values):
            return True
        return None if any(v is None for v in values) else False

    @staticmethod
    def lnot(value):
        return None if value is None else not value

    def block(self, pairs, mode):
        values = [self.trigger(k, v) for k, v in pairs if k]
        if mode == "AND":
            return self.land(values)
        if mode == "OR":
            return self.lor(values)
        if mode == "NOR":
            return self.lnot(self.lor(values))
        return self.lnot(self.land(values))

    @staticmethod
    def boolean(known, value):

        if known is None:
            return None
        return known == (value == "yes")

    def trigger(self, key, value):
        ctx = self.ctx
        if key in ("AND", "OR", "NOR", "NAND") and isinstance(value, list):
            return self.block(value, key)
        if key == "NOT" and isinstance(value, list):
            return self.block(value, "NOR")
        if key == "religion":
            return None if ctx["religion"] is None else ctx["religion"] == value
        if key == "religion_group":
            group = self.groups.get(ctx["religion"])
            return None if group is None else group == value
        if key == "culture":
            return None if ctx["culture"] is None else ctx["culture"] == value
        if key == "culture_group":
            group = ctx["culture_groups"].get(ctx["culture"])
            return None if group is None else group == value
        if key == "region":
            return None if value not in ctx["regions"] or ctx["capital_province"] is None else ctx["capital_province"] in ctx["regions"][value]
        if key in ("has_religion_feature", "has_selected_religion_feature"):
            return False
        if key == "has_religion_features":
            return self.boolean(True, value)
        if key == "character":
            return False
        if key == "is_merchant_republic":
            return self.boolean(ctx["liege_merchant"], value)
        if key == "liege_before_war":
            return self.block(value, "AND") if ctx["vassal"] and isinstance(value, list) else False
        if key in ("religion_allows_female_temple_holders", "religion_allows_male_temple_holders"):
            rule = ctx["temple_rules"].get(ctx["religion"])
            if rule is None:
                return None
            return self.boolean(rule[1] if "female" in key else rule[0], value)
        if key == "has_title_flag":
            return False
        if key == "is_theocracy":
            return self.boolean(ctx["theocracy"], value)
        if key == "is_government_potential":
            return self.potential(value)
        if key == "tier":
            return ctx["tier"] == TIER_NAMES.get(str(value).lower())
        if key in ("higher_tier_than", "higher_real_tier_than"):
            return ctx["tier"] > TIER_NAMES.get(str(value).lower(), 99)
        if key == "has_landed_title":
            return value in ctx["titles"]
        if key == "primary_title" and isinstance(value, list):
            return self.block(value, "AND")
        if key == "top_liege" and isinstance(value, list):
            outer = self.ctx
            self.ctx = dict(outer, top=outer["nation_top"], vassal=False)
            try:
                return self.block(value, "AND")
            finally:
                self.ctx = outer
        if key == "any_liege":
            return ctx["vassal"] and self.block(value, "AND") if isinstance(value, list) else ctx["vassal"]
        if key == "title":
            return value == ctx["top"]
        if key == "capital_scope" and isinstance(value, list):
            return self.block(value, "AND")
        if key == "port":
            return self.boolean(ctx["port"], value)
        if key == "controls_religion":
            return self.boolean(ctx["controls"], value)
        if key == "is_female":
            return self.boolean(ctx["female"], value)
        if key == "always":
            return value == "yes"
        if key == "any_demesne_province":
            return True
        if key == "has_game_started":
            return value == "no"
        if key == "is_save_game":
            return value == "no"
        if key in ("is_patrician", "holy_order", "mercenary", "is_offmap_tag"):
            return value == "no" if key != "is_offmap_tag" else False
        self.unknown.add(key)
        return None

def hex_color(block):
    numbers = [v for k, v in (block or []) if k is None and isinstance(v, str)]
    try:
        values = [float(n) for n in numbers[:3]]
    except ValueError:
        return None
    if len(values) < 3:
        return None
    if any("." in n for n in numbers[:3]):
        values = [v * 255 for v in values]
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in values)

def load_government_names(folder):
    names = {}
    for path in sorted(folder.glob("*.csv")):
        with open(path, encoding="cp1252", errors="replace", newline="") as handle:
            for row in csv.reader(handle, delimiter=";"):
                if len(row) >= 2 and row[0].strip().endswith("government") and row[0].strip() not in names and row[1].strip():
                    names[row[0].strip()] = row[1].strip()
                elif len(row) >= 2 and row[0].strip() == "military_governorate" and row[1].strip():
                    names.setdefault("military_governorate", row[1].strip())
    return names

def clean_government_key(key):
    words = re.sub(r"_government$", "", key).replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words) or key

def sea_vertices(features):
    seen = set()
    for feature in features:
        if feature["properties"].get("kind") != "sea":
            continue
        for ring in rings(feature["geometry"]):
            seen.update((round(x, 2), round(y, 2)) for x, y in ring)
    return seen

def rings(geometry):
    if geometry["type"] == "Polygon":
        return geometry["coordinates"]
    if geometry["type"] == "MultiPolygon":
        return [ring for polygon in geometry["coordinates"] for ring in polygon]
    return []

def is_port(feature, sea):
    return any((round(x, 2), round(y, 2)) in sea for ring in rings(feature["geometry"]) for x, y in ring)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--geojson", default="provinces_nations.geojson", help="output of inject_nations.py")
    parser.add_argument("--common", default="common")
    parser.add_argument("--history", default="history")
    parser.add_argument("--localisation", default="localisation")
    parser.add_argument("--regions", default=None, help="path to map/geographical_region.txt (needed for region triggers such as Open Range Feudalism)")
    parser.add_argument("--out", default="provinces_gov.geojson")
    args = parser.parse_args()

    common, history = Path(args.common), Path(args.history)
    for folder in (common / "governments", common / "landed_titles", common / "religions", history / "titles", history / "provinces", history / "characters"):
        if not folder.is_dir():
            sys.exit(f"{folder} is not a directory")

    with open(args.geojson, encoding="utf-8") as handle:
        collection = json.load(handle)
    date = collection.get("nation_date")
    cutoff = ih.parse_date(date) if date else None
    if cutoff is None:
        sys.exit("the geojson has no nation_date; run inject_nations.py first")

    tree = load_title_tree(common / "landed_titles")
    holdings = load_province_holdings(history / "provinces")
    province_of_county = {county: pid for pid, (county, _) in holdings.items() if county}
    groups = load_religion_groups(common / "religions")
    temple_rules = load_temple_rules(common / "religions")
    culture_groups = load_culture_groups(common / "cultures") if (common / "cultures").is_dir() else {}
    region_path = Path(args.regions) if args.regions else history.parent / "map" / "geographical_region.txt"
    regions = load_regions(region_path, tree, province_of_county) if region_path.is_file() else {}
    governments = load_governments(common / "governments")
    names = load_government_names(Path(args.localisation))
    titles = inn.load_titles(history / "titles", cutoff)
    held_by = {}
    for key, entry in titles.items():
        if inn.is_held(titles, key):
            held_by.setdefault(entry["holder"], set()).add(key)

    nomad_titles = set()
    for path in sorted((history / "titles").glob("*.txt")):
        if inn.latest(pairs_of(path), "historical_nomad", cutoff) == "yes":
            nomad_titles.add(path.stem)

    feature_list = collection["features"]
    counties_of = {}
    for feature in feature_list:
        top, county = feature["properties"].get("nation_title"), feature["properties"].get("title")
        if top and county:
            counties_of.setdefault(top, []).append(county)

    realms = {}
    for feature in feature_list:
        properties = feature["properties"]
        top = properties.get("nation_title")
        if not top:
            continue
        realms.setdefault(top, top)
        vassal = properties.get("vassal_title")
        if vassal and vassal in titles:
            realms.setdefault(vassal, top)
        elif vassal:
            realms.setdefault(vassal, top)

    every_holder = {entry["holder"] for entry in titles.values() if entry["holder"] not in (None, "0")}
    rulers = load_rulers(history / "characters", every_holder, cutoff)
    dynasties = load_dynasties(common / "dynasties")
    sea = sea_vertices(feature_list)
    feature_of = {f["properties"]["id"]: f for f in feature_list}
    colors = {name: hex_color(get(definition, "color")) for name, definition in governments}
    county_names = inn.load_names(Path(args.localisation))
    primary = inn.primary_titles(titles)
    title_order = {title: index for index, title in enumerate(tree)}
    unknown_keys = set()
    stats = {"capital basis": {}, "no ruler data": [], "no capital holding": [], "uncertain": [], "ambiguous capital": []}

    def evaluate(holder, ruler, own_titles, primary_title, county, nation_top, vassal, liege_merchant):
        holding = capital_holding(holdings, province_of_county, titles, county, holder)
        if holding is None:
            return None
        religion = ruler["religion"] if ruler else None
        capital_province = province_of_county.get(county)
        port = is_port(feature_of[capital_province], sea) if capital_province in feature_of else None
        controls = None if religion is None else any(tree.get(t, {}).get("head_of") == religion for t in own_titles)
        tier = max((TIERS[t[0]] for t in own_titles if t[0] in TIERS), default=TIERS[primary_title[0]])
        ctx = {
            "controls": controls,
            "religion": religion,
            "culture": ruler["culture"] if ruler else None,
            "female": ruler["female"] if ruler else None,
            "tier": tier,
            "titles": own_titles,
            "top": primary_title,
            "nation_top": nation_top,
            "vassal": vassal,
            "port": port,
            "capital_province": capital_province,
            "culture_groups": culture_groups,
            "regions": regions,
            "theocracy": holding == "TEMPLE",
            "temple_rules": temple_rules,
            "liege_merchant": liege_merchant,
        }
        evaluator = Evaluator(governments, groups, ctx, unknown_keys)
        wanted = HOLDING_FOR[holding]
        order = [name for name, definition in governments if wanted in [v for k, v in (get(definition, "preferred_holdings") or [])]]
        verdicts = [(name, evaluator.potential(name)) for name in order]
        first = next(((name, ok) for name, ok in verdicts if ok is not False), None)
        if first is None:
            return (FALLBACK[wanted], False, [name for name, ok in verdicts])
        return (first[0], first[1] is True, [name for name, ok in verdicts if ok is None])

    decided = {}

    def decide(holder, nation_top, vassal, liege_merchant):
        key = (holder, nation_top, vassal, liege_merchant)
        if key in decided:
            return decided[key]
        decided[key] = None
        ruler = rulers.get(holder)
        religion = ruler["religion"] if ruler else None
        culture = ruler["culture"] if ruler else None
        own_titles = held_by.get(holder, set())
        primary_title = primary.get(holder)
        if ruler is None:
            stats["no ruler data"].append(holder)
        if not own_titles or primary_title is None:
            return None
        if nomad_titles & own_titles:
            decided[key] = {"government": "nomadic_government", "certain": True, "religion": religion,
                            "culture": culture, "capital": None, "basis": "nomad title", "person": full_name(ruler, dynasties)}
            return decided[key]
        county, basis, held_counties = resolve_capital(own_titles, primary_title, ruler, tree, holdings, province_of_county, title_order)
        if county is None:
            stats["no capital holding"].append((holder, primary_title))
            decided[key] = {"government": None, "certain": False, "religion": religion, "culture": culture,
                            "capital": None, "basis": basis, "person": full_name(ruler, dynasties)}
            return decided[key]
        outcome = evaluate(holder, ruler, own_titles, primary_title, county, nation_top, vassal, liege_merchant)
        if outcome is None:
            stats["no capital holding"].append((holder, county))
            decided[key] = {"government": None, "certain": False, "religion": religion, "culture": culture,
                            "capital": county, "basis": basis, "person": full_name(ruler, dynasties)}
            return decided[key]
        pick, certain, unresolved = outcome
        if basis == "fallback" and len(held_counties) > 1:
            alternatives = {}
            for other in held_counties:
                other_outcome = evaluate(holder, ruler, own_titles, primary_title, other, nation_top, vassal, liege_merchant)
                alternatives[other] = other_outcome[0] if other_outcome else None
            if len(set(alternatives.values())) > 1:
                certain = False
                stats["ambiguous capital"].append((holder, primary_title, alternatives))
        if not certain:
            stats["uncertain"].append((holder, primary_title, pick, unresolved))
        stats["capital basis"][basis] = stats["capital basis"].get(basis, 0) + 1
        decided[key] = {"government": pick, "certain": certain, "religion": religion, "culture": culture,
                        "capital": county, "basis": basis, "person": full_name(ruler, dynasties)}
        return decided[key]

    realms = {}
    for feature in feature_list:
        properties = feature["properties"]
        top = properties.get("nation_title")
        if not top:
            continue
        realms.setdefault(top, top)
        vassal_title = properties.get("vassal_title")
        if vassal_title:
            realms.setdefault(vassal_title, top)

    result = {}
    for realm, top in sorted(realms.items(), key=lambda i: (i[0] != i[1], i[0])):
        entry = titles.get(realm)
        if entry is None:
            continue
        merchant = None if realm == top else bool(result.get(top) and result[top]["government"] == "merchant_republic_government")
        found = decide(entry["holder"], top, realm != top, merchant)
        if found:
            result[realm] = found

    def gov_of_holder(holder, nation_top, trail=()):
        realm = primary.get(holder)
        if realm is None or holder in trail:
            return None
        liege_title = titles[realm]["liege"]
        vassal, merchant = False, None
        if liege_title and inn.is_held(titles, liege_title) and titles[liege_title]["holder"] != holder:
            vassal = True
            above = gov_of_holder(titles[liege_title]["holder"], nation_top, trail + (holder,))
            merchant = bool(above and above["government"] == "merchant_republic_government")
        return decide(holder, nation_top, vassal, merchant)

    def capital_label(info):
        county = info.get("capital") if info else None
        if not county:
            return None
        name, _ = inn.display_name(county, county_names)
        return name

    def fill(properties, prefix, found):
        pick = found["government"] if found else None
        properties[prefix + "government"] = pick
        properties[prefix + "government_name"] = (names.get(pick) or clean_government_key(pick)) if pick else None
        properties[prefix + "government_color"] = colors.get(pick) if pick else None
        properties[prefix + "government_certain"] = found["certain"] if found else None

    reset_keys = ("government", "government_name", "government_color", "government_certain",
                  "holder_government", "holder_government_name", "holder_government_color", "holder_government_certain",
                  "ruler_religion", "ruler_culture", "ruler_capital", "ruler_capital_basis",
                  "vassal_religion", "vassal_culture",
                  "county_government", "county_government_name", "county_government_color",
                  "county_government_certain", "county_holder_religion", "county_holder_culture",
                  "county_holder_capital", "county_holder_capital_basis",
                  "ruler_name", "vassal_ruler_name", "county_holder_name")
    counted = {}
    missing = []
    for feature in feature_list:
        properties = feature["properties"]
        for key in reset_keys:
            properties[key] = None
        top = properties.get("nation_title")
        if top not in result or result[top]["government"] is None:
            if top:
                missing.append((properties.get("id"), properties.get("title"), top))
            continue
        ruler_info = result[top]
        fill(properties, "", ruler_info)
        properties["ruler_religion"], properties["ruler_culture"] = ruler_info["religion"], ruler_info["culture"]
        properties["ruler_capital"], properties["ruler_capital_basis"] = capital_label(ruler_info), ruler_info["basis"]
        properties["ruler_name"] = ruler_info["person"]
        vassal_title = properties.get("vassal_title") or top
        vassal_info = result.get(vassal_title) or ruler_info
        fill(properties, "holder_", vassal_info)
        properties["vassal_religion"], properties["vassal_culture"] = vassal_info["religion"], vassal_info["culture"]
        properties["vassal_ruler_name"] = vassal_info["person"]
        county_holder = titles.get(properties.get("title") or "", {}).get("holder")
        own = gov_of_holder(county_holder, top) if county_holder else None
        if not own or not own["government"]:
            missing.append((properties.get("id"), properties.get("title"), "county holder"))
            continue
        fill(properties, "county_", own)
        properties["county_holder_religion"], properties["county_holder_culture"] = own["religion"], own["culture"]
        properties["county_holder_capital"], properties["county_holder_capital_basis"] = capital_label(own), own["basis"]
        properties["county_holder_name"] = own["person"]
        counted[own["government"]] = counted.get(own["government"], 0) + 1

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(collection, handle, separators=(",", ":"))

    by_nation = {}
    for top in {f["properties"].get("nation_title") for f in feature_list}:
        if top in result and result[top]["government"]:
            by_nation[result[top]["government"]] = by_nation.get(result[top]["government"], 0) + 1
    print(f"governments for {len(result)} realms (nations and direct vassals) on {date}")
    print("nations: " + ", ".join(f"{n} {g}" for g, n in sorted(by_nation.items(), key=lambda i: -i[1])))
    print("county holders: " + ", ".join(f"{n} {g}" for g, n in sorted(counted.items(), key=lambda i: -i[1])))
    print("capital resolved by: " + ", ".join(f"{k} {v}" for k, v in sorted(stats["capital basis"].items())))
    print(f"rulers not found in history/characters: {len(stats['no ruler data'])}")
    print(f"realms or holders with no capital holding: {len(stats['no capital holding'])}")
    print(f"triggers the script could not evaluate: {sorted(unknown_keys) or 'none'}")
    print(f"decisions that are not certain: {len(stats['uncertain'])}")
    for holder, title, pick, unresolved in stats["uncertain"][:40]:
        print(f"  {holder} {title} -> {pick}; unresolved: {unresolved}")
    print(f"holders whose capital had to be guessed between counties that give different governments: {len(stats['ambiguous capital'])}")
    for holder, title, alternatives in stats["ambiguous capital"][:60]:
        print(f"  {holder} {title}: " + ", ".join(f"{c}={g}" for c, g in alternatives.items()))
    print(f"provinces left without a government: {len(missing)}")
    for item in missing[:20]:
        print(f"  {item}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
