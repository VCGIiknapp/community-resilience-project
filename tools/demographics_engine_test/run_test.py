import os, sys, json, types
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# ---- stub globals the engine expects ----
apiKey = "TESTKEY"
baseUrl = "https://example/data/2023/acs/acs5"
DATA_YEARS = "2019 - 2023"
countyNamesDictionary = {'001': 'Addison', 'Statewide': 'Statewide'}
countyCodeToName = {k: v for k, v in countyNamesDictionary.items() if k != 'Statewide'}

def _county_with_suffix(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return v
    t = str(v).strip()
    return t if t.endswith('County') else t + ' County'

def output_path(stem):
    return os.path.join(HERE, stem + "TEST.xlsx")

# two towns in one county/district/hospital
TOWN_IDS = ['5000100550', '5000100551']
townsDataFrame = pd.DataFrame({
    'Town GEOID': TOWN_IDS,
    'County Key': ['001', '001'],
    'VDH District': ['Burlington District', 'Burlington District'],
    'VDH District Key': ['1', '1'],
    'Hospital Service Area': ['Barre Hospital', 'Barre Hospital'],
    'Hospital Key': ['3', '3'],
})

# live-model town totals for every indicator key (rate -> PercentPerCapita, median -> Numerator)
geographyDataframes = {}
for mk in ['SupportiveIncome', 'Employment', 'MedianHouseholdIncome', 'HighSchoolEducation',
           'HealthInsurance', 'BroadbandInternet', 'OwnerOccupiedHomes', 'VehicleAccess']:
    is_med = mk == 'MedianHouseholdIncome'
    town = pd.DataFrame({'JoinKey': TOWN_IDS, 'PercentPerCapita': [60.0, 70.0], 'Numerator': [50000.0, 60000.0]})
    if is_med:  # median indicator: statewide/county frames carry the aggregate in 'Numerator'
        statewide = pd.DataFrame({'JoinKey': ['50'], 'CNTYCODE': [''], 'Numerator': [58000.0]})
        county = pd.DataFrame({'JoinKey': ['50001'], 'CNTYCODE': ['001'], 'Numerator': [57000.0]})
    else:       # rate indicator: aggregate in 'PercentPerCapita'
        statewide = pd.DataFrame({'JoinKey': ['50'], 'CNTYCODE': [''], 'PercentPerCapita': [63.0]})
        county = pd.DataFrame({'JoinKey': ['50001'], 'CNTYCODE': ['001'], 'PercentPerCapita': [62.0]})
    geographyDataframes[mk] = {'county subdivision': town, 'statewide': statewide, 'county': county}
geographyDenominatorsDataframes = {'county subdivision': None}

# ---- stub requests.get: return [header, *rows] with K value cols + geo cols ----
class _Resp:
    def __init__(self, data): self._data = data
    def json(self): return self._data

def _requests_get(url, params=None):
    getvars = params['get'].split(',')
    K = len(getvars)
    forclause = params.get('for', '')
    header = list(getvars)
    VAL = '30'          # each variable -> 30 (rate denom per var); medians handled below
    is_median = getvars[0].startswith('B19013') and getvars[0] != 'B19013_001E'
    vrow = (['50000'] if is_median else [VAL] * K)
    if forclause.startswith('state'):
        header += ['state']; rows = [vrow + ['50']]
    elif forclause.startswith('county:'):
        header += ['state', 'county']; rows = [vrow + ['50', '001']]
    else:
        header += ['state', 'county', 'county subdivision']
        rows = [vrow + ['50', '001', '00550'], vrow + ['50', '001', '00551']]
    return _Resp([header] + rows)

requests = types.SimpleNamespace(get=_requests_get)

# config
_demoCfg = json.load(open(os.path.join(ROOT, 'config', 'demographics_variables.json'), encoding='utf-8'))
DEMO_SUPPRESS_MIN = _demoCfg['suppression_min_denominator']
BIPOC_MEMBERS = set(_demoCfg['bipoc_member_labels'])
BIPOC_LABEL = "Black, Indigenous, Persons of Color (BIPOC)"

# ---- load engine_core into this namespace and run ----
engine_src = open(os.path.join(HERE, 'engine_core.py'), encoding='utf-8').read()
ns = dict(globals())
exec(engine_src, ns)
statewide, counties, districts, hospitals = ns['run_demographics']()

def check(df, name, expected_cols):
    ok = list(df.columns) == expected_cols
    print(f"{name}: rows={len(df)} cols_match={ok}")
    if not ok:
        print("   GOT:", list(df.columns))
    return ok

print("=== OUTPUT SCHEMAS ===")
check(statewide, 'statewide', ['Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total Statewide Mean', 'Data Years'])
check(counties, 'counties', ['Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total County Mean', 'County', 'Data Years'])
check(districts, 'districts', ['VDH District', 'Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total VDH District Mean', 'Data Years'])
check(hospitals, 'hospitals', ['Hospital Service Area', 'Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total Hospital Service Area Mean', 'Data Years'])

print("\n=== SANITY ===")
print("statewide indicators:", sorted(statewide['Indicator'].unique()))
print("BIPOC present only in districts/hospitals:",
      'Black, Indigenous, Persons of Color (BIPOC)' in set(districts['Demographic Group']),
      'Black, Indigenous, Persons of Color (BIPOC)' in set(hospitals['Demographic Group']),
      'BIPOC not in statewide:', 'Black, Indigenous, Persons of Color (BIPOC)' not in set(statewide['Demographic Group']))
# MHI median: statewide mean now = ACS aggregate (58000), not mean of town medians
mhi = statewide[statewide['Indicator'] == 'Median Household Income']
print("MHI statewide mean (expect aggregate 58000.0):", mhi['Total Statewide Mean'].iloc[0])
mhi_c = counties[counties['Indicator'] == 'Median Household Income']
print("MHI county mean (expect aggregate 57000.0):", mhi_c['Total County Mean'].iloc[0])
# rate: statewide mean now = ACS aggregate (63.0), county = aggregate (62.0)
print("Supportive Income statewide mean (expect aggregate 63.0):",
      statewide[statewide['Indicator'] == 'Supportive Income']['Total Statewide Mean'].iloc[0])
print("Supportive Income county mean (expect aggregate 62.0):",
      counties[counties['Indicator'] == 'Supportive Income']['Total County Mean'].iloc[0])
print("District mean (expect municipality avg 65.0 for rate):",
      districts[districts['Indicator'] == 'Supportive Income']['Total VDH District Mean'].iloc[0])
print("County name suffix:", counties['County'].iloc[0])
print("\nTotal rows:", dict(statewide=len(statewide), counties=len(counties), districts=len(districts), hospitals=len(hospitals)))
