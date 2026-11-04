"""Parse the demographics section of the (pre-rebuild) CRI script and emit a
verbatim, auditable config: config/demographics_variables.json.

Captures, per indicator:
  - new-model indicator key (to source area-frame means from geographyDataframes)
  - kind: 'rate' (denominator+numerator) or 'median' (single value, no denominator)
  - build_bipoc: whether a district/hospital BIPOC aggregate is produced
  - groups: list of {label, denominators, numerators}  (rate)
                      {label, variables}                 (median)

The ACS variable lists are copied verbatim from the source (never retyped).

Run once against a checkout that still contains the old demographics section:
    python -I tools/extract_demographics_config.py <path-to-old-script>
"""
import re, ast, json, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "src/CommunityResilienceIndexCurrentDev.py"
lines = open(SRC, encoding="utf-8").read().split("\n")

# indicator display name -> new-model key used in geographyDataframes
MODEL_KEY = {
    "Income": "SupportiveIncome",
    "Employment": "Employment",
    "Median Household Income": "MedianHouseholdIncome",
    "High School Education": "HighSchoolEducation",
    "Health Insurance": "HealthInsurance",
    "Broadband Internet Subscription": "BroadbandInternet",
    "Owner Occupancy Rate": "OwnerOccupiedHomes",
    "Vehicle Access": "VehicleAccess",
}
BIPOC_INDICATORS = {"Employment", "HighSchoolEducation", "HealthInsurance",
                    "BroadbandInternet", "OwnerOccupiedHousing"}  # internal bipoc* names
# the 7 race groups summed into BIPOC (all race groups except White/European American)
BIPOC_MEMBER_LABELS = [
    "Asian/ Asian American", "Black/ African American", "Hispanic/ Latin(a/o/x)",
    "Indigenous American/ Alaskan", "Indigenous Hawaiian/ Pacific Islanders",
    "Additional Race Group", "Multiracial",
]

def clean_label(lbl):
    if lbl is None:
        return None
    return lbl.replace("Indigenous Hawaiian/ Pacific Islanders)",
                       "Indigenous Hawaiian/ Pacific Islanders")

headers = [(i, l) for i, l in enumerate(lines) if "Demographics #######" in l]
bounds = []
for k, (i, l) in enumerate(headers):
    name = re.search(r'####### (.+?) Demographics', l).group(1)
    end = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
    bounds.append((name, i, end))

denomnum_rx = re.compile(r'^(\w+?)(Denominator|Numerator)Variables\s*=\s*(\[[^\]]*\])')
single_rx = re.compile(r'^(\w+?)Variables\s*=\s*(\[[^\]]*\])')
# Read only the STATEWIDE predicate per group: it is the authoritative variable
# set, and is unaffected by a town-level copy-paste bug in the MHI/hispanic block
# (line ~2051 appends a stray Employment numerator list).
getjoin_rate_rx = re.compile(r'^(\w+?)StatewidePredicates\["get"\]\s*=\s*",".join\((\w+)\s*\+\s*(\w+)\)')
getjoin_single_rx = re.compile(r'^(\w+?)StatewidePredicates\["get"\]\s*=\s*",".join\((\w+)\)\s*$')
label_rx = re.compile(r"^(\w+?)DataFrame\['Demographic Group'\]\s*=\s*'([^']+)'")

config = {}
for name, start, end in bounds:
    block = lines[start:end]
    blocktext = "\n".join(block)
    lists = {}      # varname -> python list
    joins = {}      # group_prefix -> tuple(varnames)   (1 for median, 2 for rate)
    labels = {}     # dataframe_prefix -> label
    is_median = False
    # capture every "<name>Variables = [ ... ]" (possibly multi-line) literal
    for m in re.finditer(r'(\w+Variables)\s*=\s*(\[.*?\])', blocktext, re.DOTALL):
        try:
            lists[m.group(1)] = ast.literal_eval(m.group(2))
        except Exception:
            pass
    for raw in block:
        l = raw.strip()
        m = getjoin_rate_rx.match(l)
        if m:
            joins[m.group(1)] = (m.group(2), m.group(3)); continue
        m = getjoin_single_rx.match(l)
        if m:
            joins[m.group(1)] = (m.group(2),); is_median = True; continue
        m = label_rx.match(l)
        if m:
            labels.setdefault(m.group(1), m.group(2))

    model_key = MODEL_KEY[name]
    is_median = (model_key == "MedianHouseholdIncome")  # the only median-valued indicator
    groups = []
    for gpfx, varnames in joins.items():
        # label: dataframe prefix that starts with the group prefix (longest match wins)
        lbl = None
        for dfpfx in sorted(labels, key=len, reverse=True):
            if dfpfx.startswith(gpfx):
                lbl = labels[dfpfx]; break
        lbl = clean_label(lbl)
        if len(varnames) == 1:
            groups.append({"label": lbl, "variables": lists[varnames[0]]})
        else:
            groups.append({"label": lbl,
                           "denominators": lists[varnames[0]],
                           "numerators": lists[varnames[1]]})

    config[name] = {
        "indicator_label": None,
        "model_key": model_key,
        "kind": "median" if is_median else "rate",
        "build_bipoc": model_key in ("Employment", "HighSchoolEducation", "HealthInsurance",
                                     "BroadbandInternet", "OwnerOccupiedHomes"),
        "groups": groups,
    }

# fill indicator_label from the Indicator assignment
for name, start, end in bounds:
    for raw in lines[start:end]:
        mm = re.search(r"\['Indicator'\]\s*=\s*'([^']+)'", raw)
        if mm:
            config[name]["indicator_label"] = mm.group(1); break

meta = {
    "_comment": "Auto-extracted ACS variables for CRI race/ethnicity/age/sex disaggregation. "
                "Lists are verbatim from the prior script. kind=rate uses denominators+numerators; "
                "kind=median uses a single median-dollar variable per group.",
    "bipoc_member_labels": BIPOC_MEMBER_LABELS,
    "suppression_min_denominator": 25,
    "indicators": config,
}
out = "config/demographics_variables.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=1)

# report
print("indicator | key | kind | bipoc | n_groups | labels_ok | lists_ok")
for name, cfg in config.items():
    gok = all(g["label"] for g in cfg["groups"])
    lok = all(("variables" in g) or ("denominators" in g and "numerators" in g) for g in cfg["groups"])
    print(f"{name:<32} {cfg['model_key']:<22} {cfg['kind']:<6} {str(cfg['build_bipoc']):<5} "
          f"{len(cfg['groups']):<2} {gok!s:<5} {lok}")
print("\nWrote", out)
