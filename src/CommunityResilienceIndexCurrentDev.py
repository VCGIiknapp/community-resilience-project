# library imports
import requests
import os
import json
import pandas as pd
import arcpy
import numpy as np
from scipy.stats import gmean as _scipy_gmean


class AcsUnavailable(Exception):
    """Raised when the Census API has no data for a requested variable/year
    (e.g. broadband B28005/B28009 before it existed). Callers catch this to drop
    the affected indicator from that 5-year period instead of crashing the run."""
    pass


def acs_get(url):
    """GET a Census API tabular endpoint and return the parsed JSON list of rows.
    Raises AcsUnavailable on any non-200 status or non-tabular body (the API answers
    an unknown variable with an HTTP 400 + plain-text error, not a JSON array)."""
    r = requests.get(url)
    if r.status_code != 200:
        raise AcsUnavailable(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        data = r.json()
    except ValueError:
        raise AcsUnavailable(f"non-JSON response: {r.text[:160]}")
    if not isinstance(data, list):
        raise AcsUnavailable(f"non-tabular response: {str(data)[:160]}")
    return data


def scipygmean(values):
    """Geometric mean that returns NaN for an empty sample instead of emitting
    scipy's SmallSampleWarning. An empty rank list is expected and benign. Non-empty
    samples are passed straight through to scipy.stats.gmean."""
    arr = np.asarray(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return np.nan
    return _scipy_gmean(arr)


def _frame_weighted_rate(df, group_col, num_col='Numerator', den_col='Denominator'):
    """Population-weighted area-frame rate for a RATE indicator, broadcast to every
    member row: sum(numerator) / sum(denominator) * 100 within each group. This is the
    District / HSA area-frame reference (frames that have no ACS geography of their own).
    Rows with an invalid numerator (<0) or non-positive denominator are excluded."""
    num = pd.to_numeric(df[num_col], errors='coerce')
    den = pd.to_numeric(df[den_col], errors='coerce')
    valid = (num >= 0) & (den > 0)
    num = num.where(valid)
    den = den.where(valid)
    g = df[group_col]
    sum_num = num.groupby(g).transform('sum', min_count=1)
    sum_den = den.groupby(g).transform('sum', min_count=1)
    return (sum_num / sum_den.replace(0, np.nan)) * 100


def _frame_weighted_value(df, group_col, value_col='Numerator', weight_col='Weight'):
    """Count-weighted area-frame mean for a VALUE indicator (e.g. median household
    income), broadcast to every member row: sum(value * weight) / sum(weight) within
    each group. Falls back to an unweighted mean when no weight column is present."""
    val = pd.to_numeric(df[value_col], errors='coerce')
    g = df[group_col]
    if weight_col not in df.columns:
        return val.where(val >= 0).groupby(g).transform('mean')
    w = pd.to_numeric(df[weight_col], errors='coerce')
    valid = (val >= 0) & (w > 0)
    val = val.where(valid)
    w = w.where(valid)
    sum_vw = (val * w).groupby(g).transform('sum', min_count=1)
    sum_w = w.groupby(g).transform('sum', min_count=1)
    return sum_vw / sum_w.replace(0, np.nan)


def _smd(mean_col, value_col, std_col):
    """Standard Mean Difference = (area-frame mean - unit value) / frame std * -1.
    std == 0 (true zero spread) -> 0 disparity; std NaN (single-member frame) -> NaN."""
    std = pd.to_numeric(std_col, errors='coerce')
    result = ((mean_col - value_col) / std.replace(0, np.nan)) * -1
    return result.mask(std == 0, 0.0)


def _numcol(df, name):
    """Return df[name] as numeric, or an all-NaN series if the column is absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors='coerce')
    return pd.Series(np.nan, index=df.index)


def _county_with_suffix(value):
    """Prior outputs store county as 'Addison County'; the name dict yields 'Addison'."""
    if pd.isna(value):
        return value
    text = str(value).strip()
    return text if text.endswith('County') else f'{text} County'


# =============================================================================
# RUN CONFIGURATION
# -----------------------------------------------------------------------------
# The pipeline runs once per 5-year ACS window and APPENDS every window into a
# single combined workbook per output type (one file per deliverable, all vintages
# stacked, tagged by the "ACS 5-Year" column). Each value below is the END year of a
# 5-year window: 2014 -> 2010-2014, 2019 -> 2015-2019, 2024 -> 2020-2024.
#
# NOTE: broadband (ACS table B28005 / B28009) does not exist in the earliest window;
# the script detects the missing data at runtime, drops Broadband Internet for that
# window, and builds that year's composite index from the remaining 10 indicators.
YEARS = [2014, 2019, 2024]            # edit this list to add/remove 5-year windows

# Project root is two levels up from this script (src/ -> project root).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# All combined, multi-year deliverables are written here: data/output/<stem>.xlsx
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# Scratch location for per-period intermediate/debug dumps.
SCRATCH_DIR = os.path.join(OUTPUT_DIR, "_scratch")
os.makedirs(SCRATCH_DIR, exist_ok=True)

# -----------------------------------------------------------------------------
# Internal UNC / network paths are kept OUT of version control (config/network_paths.json,
# git-ignored). config/network_paths.example.json documents the expected structure.
_networkPathsFile = os.path.join(PROJECT_ROOT, "config", "network_paths.json")
_networkPaths = {}
if os.path.exists(_networkPathsFile):
    with open(_networkPathsFile, encoding="utf-8") as _npf:
        _networkPaths = json.load(_npf)

# Census API key: prefer repo-local config/CensusAPI.txt, else the network path.
_localKeyFile = os.path.join(PROJECT_ROOT, "config", "CensusAPI.txt")
_networkKeyFile = _networkPaths.get("censusKeyFile")
if os.path.exists(_localKeyFile):
    censusAPIKeyFile = _localKeyFile
elif _networkKeyFile:
    censusAPIKeyFile = _networkKeyFile
else:
    raise FileNotFoundError(
        "No Census API key found. Add config/CensusAPI.txt, or set 'censusKeyFile' in "
        "config/network_paths.json (see config/network_paths.example.json)."
    )

# Municipality crosswalk SDE feature class -- network path, kept private.
crosswalkTable = _networkPaths.get("crosswalkTable")
if not crosswalkTable:
    raise KeyError(
        "'crosswalkTable' is not set in config/network_paths.json "
        "(see config/network_paths.example.json)."
    )

with open(censusAPIKeyFile) as key:
    apiKey = key.read().strip()

# defining state variable to Vermont
state = '50'

pd.options.display.max_columns = None
pd.options.display.max_rows = 20

countyNamesDictionary = {'001': 'Addison', '003': 'Bennington', '005': 'Caledonia',
                         '007': 'Chittenden', '009': 'Essex', '011': 'Franklin',
                         '013': 'Grand Isle', '015': 'Lamoille', '017': 'Orange',
                         '019': 'Orleans', '021': 'Rutland', '023': 'Washington',
                         '025': 'Windham', '027': 'Windsor', 'Statewide': 'Statewide',
                         }
countyCodeToName = {k: v for k, v in countyNamesDictionary.items() if k != 'Statewide'}

censusDataURL = "https://api.census.gov/data"
dataset = "acs/acs5"
acsDatasourceFields = 'NAME,B01003_001E'

# Per-period globals set by run_period(); module-level helpers read these.
year = None
baseUrl = None
PERIOD_LABEL = None
DATA_YEARS = None
geographyDataframes = {}
geographyDenominatorsDataframes = {}
geographyUnpopulatedDataframes = {}
townsDataFrame = None

geographyVariables = {'county subdivision': {'boundaries': 'state:50',
                                             'columns': {0: 'name', 1: 'population', 2: 'stateKey', 3: 'countyKey', 4: 'townFIPS'}},
                      'county': {'boundaries': 'state:50',
                                 'columns': {0: 'name', 1: 'population', 2: 'stateKey', 3: 'countyKey'}},
                      'statewide': {'boundaries': '',
                                    'columns': {}}
                      }

# Perform Indicator calls (geographies hard-coded for stability).
indicators = {'SupportiveIncome': {'variables': {"numerators": ["C17002_008E"],
                                                 "denominators": ["C17002_001E"]},
                                   'columnsForStats': {
                                       'county subdivision': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'townFIPS'},
                                       'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                       'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'WithoutPublicAssistance': {'variables': {"numerators": ["B19058_002E"],
                                                        "denominators": ["B19058_001E"]},
                                          'columnsForStats': {
                                              'county subdivision': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'townFIPS'},
                                              'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                              'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'Employment': {'variables': {"numerators": ["B23001_005E", "B23001_007E", "B23001_012E", "B23001_014E", "B23001_019E", "B23001_021E", "B23001_026E", "B23001_028E", "B23001_033E", "B23001_035E", "B23001_040E", "B23001_042E", "B23001_047E", "B23001_049E", "B23001_054E", "B23001_056E", "B23001_061E", "B23001_063E", "B23001_068E", "B23001_070E", "B23001_075E", "B23001_080E", "B23001_085E", "B23001_091E", "B23001_093E", "B23001_098E", "B23001_100E", "B23001_105E", "B23001_107E", "B23001_112E", "B23001_114E", "B23001_119E", "B23001_121E", "B23001_126E", "B23001_128E", "B23001_133E", "B23001_135E", "B23001_140E", "B23001_142E", "B23001_147E", "B23001_149E", "B23001_154E", "B23001_156E", "B23001_161E", "B23001_166E", "B23001_171E"],
                                           "denominators": ["B23001_004E", "B23001_011E", "B23001_018E", "B23001_025E", "B23001_032E", "B23001_039E", "B23001_046E", "B23001_053E", "B23001_060E", "B23001_067E", "B23001_074E", "B23001_079E", "B23001_084E", "B23001_090E", "B23001_097E", "B23001_104E", "B23001_111E", "B23001_118E", "B23001_125E", "B23001_132E", "B23001_139E", "B23001_146E", "B23001_153E", "B23001_160E", "B23001_165E", "B23001_170E"]},
                             'columnsForStats': {
                                 'county subdivision': {74: 'townFIPS'},
                                 'county': {73: 'County Key'},
                                 'statewide': {72: 'State Key'}}},
              'MedianHouseholdIncome': {'variables': {"numerators": ["B19013_001E"],
                                                      "denominators": [],
                                                      "weights": ["B19001_001E"]},
                                        'columnsForStats': {
                                            'county subdivision': {0: 'Indicator Percentage', 3: 'townFIPS'},
                                            'county': {0: 'County Indicator Mean', 2: 'County Key'},
                                            'statewide': {0: 'Statewide Indicator Mean', 1: 'State Key'}}},
              'HighSchoolEducation': {'variables': {"numerators": ["B15001_014E", "B15001_015E", "B15001_016E", "B15001_017E", "B15001_018E", "B15001_022E", "B15001_023E", "B15001_024E", "B15001_025E", "B15001_026E", "B15001_030E", "B15001_031E", "B15001_032E", "B15001_033E", "B15001_034E", "B15001_038E", "B15001_039E", "B15001_040E", "B15001_041E", "B15001_042E", "B15001_055E", "B15001_056E", "B15001_057E", "B15001_058E", "B15001_059E", "B15001_063E", "B15001_064E", "B15001_065E", "B15001_066E", "B15001_067E", "B15001_071E", "B15001_072E", "B15001_073E", "B15001_074E", "B15001_075E", "B15001_079E", "B15001_080E", "B15001_081E", "B15001_082E", "B15001_083E"],
                                                    "denominators": ["B15001_011E", "B15001_019E", "B15001_027E", "B15001_035E", "B15001_052E", "B15001_060E", "B15001_068E", "B15001_076E"]},
                                      'columnsForStats': {
                                          'county subdivision': {50: 'townFIPS'},
                                          'county': {49: 'County Key'},
                                          'statewide': {48: 'State Key'}}},
              'HealthInsurance': {'variables': {"numerators": ["B27010_003E", "B27010_010E", "B27010_019E", "B27010_026E", "B27010_035E", "B27010_042E", "B27010_052E", "B27010_058E"],
                                                "denominators": ["B27010_001E"]},
                                  'columnsForStats': {
                                      'county subdivision': {0: 'Indicator Denominator', 11: 'townFIPS'},
                                      'county': {0: 'Indicator Denominator', 10: 'County Key'},
                                      'statewide': {0: 'Indicator Denominator', 9: 'State Key'}}},
              'ManageableHousingCosts': {'variables': {"numerators": ["B25095_003E", "B25095_004E", "B25095_005E", "B25095_012E", "B25095_013E", "B25095_014E", "B25095_021E", "B25095_022E", "B25095_023E", "B25095_030E", "B25095_031E", "B25095_032E", "B25095_039E", "B25095_040E", "B25095_041E", "B25095_048E", "B25095_049E", "B25095_050E", "B25095_057E", "B25095_058E", "B25095_059E", "B25095_066E", "B25095_067E", "B25095_068E", "B25070_002E", "B25070_003E", "B25070_004E", "B25070_005E", "B25070_006E"],
                                                       "denominators": ["B25095_001E", "B25070_001E"]},
                                         'columnsForStats': {
                                             'county subdivision': {33: 'townFIPS'},
                                             'county': {32: 'County Key'},
                                             'statewide': {31: 'State Key'}}},
              'BroadbandInternet': {'variables': {"numerators": ["B28005_005E", "B28005_011E", "B28005_017E"],
                                                  "denominators": ["B28005_001E"]},
                                    'columnsForStats': {
                                        'county subdivision': {0: 'Indicator Denominator', 6: 'townFIPS'},
                                        'county': {0: 'Indicator Denominator', 5: 'County Key'},
                                        'statewide': {0: 'Indicator Denominator', 4: 'State Key'}}},
              'OwnerOccupiedHomes': {'variables': {"numerators": ["B25003_002E"],
                                                   "denominators": ["B25003_001E"]},
                                     'columnsForStats': {
                                         'county subdivision': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'townFIPS'},
                                         'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                         'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'EnglishProficiency': {'variables': {"numerators": ["B16005_003E", "B16005_005E", "B16005_006E", "B16005_010E", "B16005_011E", "B16005_015E", "B16005_016E", "B16005_020E", "B16005_021E", "B16005_025E", "B16005_027E", "B16005_028E", "B16005_032E", "B16005_033E", "B16005_037E", "B16005_038E", "B16005_042E", "B16005_043E"],
                                                   "denominators": ["B16005_001E"]},
                                     'columnsForStats': {
                                         'county subdivision': {0: 'Indicator Denominator', 21: 'townFIPS'},
                                         'county': {0: 'Indicator Denominator', 20: 'County Key'},
                                         'statewide': {0: 'Indicator Denominator', 19: 'State Key'}}},
              'VehicleAccess': {'variables': {"numerators": ["B25044_004E", "B25044_005E", "B25044_006E", "B25044_007E", "B25044_008E", "B25044_011E", "B25044_012E", "B25044_013E", "B25044_014E", "B25044_015E"],
                                              "denominators": ["B25044_001E"]},
                                'columnsForStats': {
                                    'county subdivision': {0: 'Indicator Denominator', 13: 'townFIPS'},
                                    'county': {0: 'Indicator Denominator', 12: 'County Key'},
                                    'statewide': {0: 'Indicator Denominator', 11: 'State Key'}}},
              }

geographyDataFrameGeographyFieldIndex = {'county subdivision': 3,
                                         'county': 2,
                                         'statewide': 1}

# Ranking / SMD contexts (HSA retained -- project decision 2026-11-04).
contexts = {
    'Statewide': {'rank': 'PercentPerCapitaRank', 'smd': 'StatewideSMD'},
    'County':    {'rank': 'PercentPerCapitaCountyRank', 'smd': 'CountySMD'},
    'District':  {'rank': 'PercentPerCapitaDistrictRank', 'smd': 'DistrictSMD'},
    'HSA':       {'rank': 'PercentPerCapitaHSARank', 'smd': 'HSASMD'}
}
count_contexts = {
    'Statewide': {'rank': 'IndicatorRank', 'smd': 'StatewideSMD'},
    'County':    {'rank': 'IndicatorCountyRank', 'smd': 'CountySMD'},
    'District':  {'rank': 'IndicatorDistrictRank', 'smd': 'DistrictSMD'},
    'HSA':       {'rank': 'IndicatorHSARank', 'smd': 'HSASMD'}
}

# indicator key -> friendly output-column prefix used in the published index schema
INDICATOR_PREFIX = {
    'SupportiveIncome':        'income',
    'WithoutPublicAssistance': 'noPublicAssistance',
    'Employment':              'employment',
    'MedianHouseholdIncome':   'medianIncome',
    'HighSchoolEducation':     'highSchoolEducation',
    'HealthInsurance':         'healthInsurance',
    'ManageableHousingCosts':  'manageableHousingCosts',
    'BroadbandInternet':       'broadbandInternet',
    'OwnerOccupiedHomes':      'ownerOccupiedHousing',
    'EnglishProficiency':      'englishProficiency',
    'VehicleAccess':           'vehicleAccess',
}
# context -> (rate-indicator rank col, count-indicator rank col) in geographyDataframes
RANK_COLS = {
    'Statewide': ('PercentPerCapitaRank',         'IndicatorRank'),
    'County':    ('PercentPerCapitaCountyRank',   'IndicatorCountyRank'),
    'District':  ('PercentPerCapitaDistrictRank', 'IndicatorDistrictRank'),
    'HSA':       ('PercentPerCapitaHSARank',      'IndicatorHSARank'),
}
PCT_CTX_LABEL = {'Statewide': 'Statewide', 'County': 'County', 'District': 'District', 'HSA': 'Hospital'}
INDEX_SCORE_LABEL = {'Statewide': 'Statewide Index Score', 'County': 'County Index Score',
                     'District': 'District Index Score', 'HSA': 'Hospital Index Score'}

INDEX_SPECS = {
    'county subdivision': {
        'contexts': ['Statewide', 'County', 'District', 'HSA'],
        'stem': 'townsIndexGeometric',
        'id_cols': ['Town Name', 'County', 'VDH District', 'Hospital Service Area',
                    'Town GEOID', 'Data Years', 'Town Population'],
    },
}

# indicator key -> published display name
INDICATOR_DISPLAY = {
    'SupportiveIncome':        'Supportive Income',
    'WithoutPublicAssistance': 'No Public Assistance',
    'Employment':              'Employment',
    'MedianHouseholdIncome':   'Median Household Income',
    'HighSchoolEducation':     'High School Education',
    'HealthInsurance':         'Health Insurance',
    'ManageableHousingCosts':  'Manageable Housing Costs',
    'BroadbandInternet':       'Broadband Internet Subscription',
    'OwnerOccupiedHomes':      'Owner-Occupied Homes',
    'EnglishProficiency':      'English Proficiency',
    'VehicleAccess':           'Vehicle Access',
}
IND_VALUE = ('PercentPerCapita', 'Numerator')
IND_MEAN = {'Statewide': ('AreaFrameMeanStatewide',      'AreaFrameMeanStatewide'),
            'County':    ('AreaFrameMeanCounty',         'AreaFrameMeanCounty'),
            'District':  ('PercentPerCapitaDistrictMean', 'IndicatorDistrictMean'),
            'HSA':       ('PercentPerCapitaHSAMean',      'IndicatorHSAMean')}

# Disaggregation config (ACS variables per indicator/group) is auditable JSON.
with open(os.path.join(PROJECT_ROOT, "config", "demographics_variables.json"), encoding="utf-8") as _demoFile:
    _demoCfg = json.load(_demoFile)
DEMO_SUPPRESS_MIN = _demoCfg["suppression_min_denominator"]
BIPOC_MEMBERS = set(_demoCfg["bipoc_member_labels"])
BIPOC_LABEL = "Black, Indigenous, Persons of Color (BIPOC)"
# =============================================================================
# PUBLISHED-TABLE HELPERS (read the per-period globals set by run_period)
# =============================================================================
def build_indicator_percentile_wide(geography, ctx_list):
    """One row per unit (JoinKey); columns '<prefix><CtxLabel>Percentile' for each
    indicator x context. Indicators absent this period (e.g. broadband in 2010-2014)
    are simply skipped -> their columns come through as NaN after reindex."""
    wide = None
    for indicator, prefix in INDICATOR_PREFIX.items():
        df = geographyDataframes.get(indicator, {}).get(geography)
        if df is None:
            continue
        is_count = 'IndicatorRank' in df.columns
        rename = {}
        for ctx in ctx_list:
            rate_col, count_col = RANK_COLS[ctx]
            src = count_col if is_count else rate_col
            if src in df.columns:
                rename[src] = f'{prefix}{PCT_CTX_LABEL[ctx]}Percentile'
        take = df[['JoinKey'] + list(rename.keys())].copy().rename(columns=rename)
        for col in rename.values():
            take[col] = pd.to_numeric(take[col], errors='coerce').round(2)
        wide = take if wide is None else wide.merge(take, on='JoinKey', how='outer')
    return wide


def build_indicator_stack(geography, ctx_list, geo_title):
    """Stacked indicator frame with _value/_mean_<ctx>/_pctl_<ctx>/_smd_<ctx>,
    Indicator, JoinKey (one row per indicator x unit). Absent indicators are skipped."""
    frames = []
    for indicator in INDICATOR_DISPLAY:
        df = geographyDataframes.get(indicator, {}).get(geography)
        if df is None:
            continue
        is_count = 'IndicatorRank' in df.columns
        base = pd.DataFrame({'JoinKey': df['JoinKey'].values})
        base['Indicator'] = INDICATOR_DISPLAY[indicator]
        v_rate, v_count = IND_VALUE
        base['_value'] = _numcol(df, v_count if is_count else v_rate).round(2).values
        for ctx in ctx_list:
            m_rate, m_count = IND_MEAN[ctx]
            p_rate, p_count = RANK_COLS[ctx]
            base[f'_mean_{ctx}'] = _numcol(df, m_count if is_count else m_rate).round(2).values
            base[f'_pctl_{ctx}'] = _numcol(df, p_count if is_count else p_rate).round(2).values
            base[f'_smd_{ctx}'] = _numcol(df, f'{geo_title}{ctx}SMD').round(2).values
        frames.append(base)
    full = pd.concat(frames, ignore_index=True)
    full['Data Years'] = DATA_YEARS
    return full


# =============================================================================
# DISAGGREGATION HELPERS
# -----------------------------------------------------------------------------
# Race category now resolves to exactly TWO groups at every area frame (statewide,
# county, VDH district, HSA): a composite BIPOC (sum/weighted-mean of all non-white
# race members) and White. Age/sex groups are emitted unchanged. Suppression is
# applied ONLY at the aggregated area-frame level (never to individual towns):
# towns under 25 are still pooled into their frame; a frame-level group whose count
# (summed denominator, or household weight for median income) is < 25 is suppressed.
# =============================================================================
def _total_means(model_key, kind):
    """Area-frame total-population indicator means: statewide & county = ACS aggregate
    for the frame; district & HSA = population-weighted aggregate of member towns."""
    sw = geographyDataframes[model_key].get('statewide')
    co = geographyDataframes[model_key].get('county')
    swCol = 'PercentPerCapita' if (sw is not None and 'PercentPerCapita' in sw.columns) else 'Numerator'
    statewide = (pd.to_numeric(sw[swCol], errors='coerce').iloc[0]
                 if sw is not None and len(sw) else np.nan)
    if co is not None and len(co):
        coCol = 'PercentPerCapita' if 'PercentPerCapita' in co.columns else 'Numerator'
        county = pd.to_numeric(co.set_index('CNTYCODE')[coCol], errors='coerce')
    else:
        county = pd.Series(dtype=float)
    src = geographyDataframes[model_key]['county subdivision']
    tmp = pd.DataFrame({'Town GEOID': src['JoinKey'].astype(str)})
    if kind == 'median':
        tmp['val'] = pd.to_numeric(src['Numerator'], errors='coerce')
        tmp['w'] = pd.to_numeric(src['Weight'], errors='coerce') if 'Weight' in src.columns else 1.0
    else:
        tmp['num'] = pd.to_numeric(src['Numerator'], errors='coerce')
        tmp['den'] = pd.to_numeric(src['Denominator'], errors='coerce')
    tmp = tmp.merge(townsDataFrame, on='Town GEOID', how='left')

    def _frame_mean(gcol):
        if kind == 'median':
            d = tmp[(tmp['val'] > 0) & (tmp['w'] > 0)]
            if d.empty:
                return pd.Series(dtype=float)
            vw = (d['val'] * d['w']).groupby(d[gcol]).sum()
            ww = d['w'].groupby(d[gcol]).sum()
            return vw / ww.replace(0, np.nan)
        d = tmp[(tmp['num'] >= 0) & (tmp['den'] > 0)]
        n = d['num'].groupby(d[gcol]).sum()
        de = d['den'].groupby(d[gcol]).sum()
        return (n / de.replace(0, np.nan)) * 100

    return (statewide, county, _frame_mean('VDH District'), _frame_mean('Hospital Service Area'))


def _fetch(getvars, geo):
    predicates = {'get': ",".join(getvars), 'key': apiKey}
    if geo == 'statewide':
        predicates['for'] = 'state:50'
    elif geo == 'county':
        predicates['for'] = 'county:*'; predicates['in'] = 'state:50'
    else:  # town
        predicates['for'] = 'county subdivision:*'; predicates['in'] = 'state:50'
    r = requests.get(baseUrl, params=predicates)
    if r.status_code != 200:
        raise AcsUnavailable(f"HTTP {r.status_code} for {getvars[:2]}: {r.text[:120]}")
    try:
        data = r.json()
    except ValueError:
        raise AcsUnavailable(f"non-JSON for {getvars[:2]}: {r.text[:120]}")
    if not isinstance(data, list):
        raise AcsUnavailable(f"non-tabular for {getvars[:2]}: {str(data)[:120]}")
    return pd.DataFrame(data[1:])


def _process_group(group, kind, geo):
    """Fetch one demographic group at one geography; return a frame with a geo key
    plus numeric 'pct' (and 'num'/'denom' for rate indicators, 'weight' = household
    count for median indicators)."""
    if kind == 'median':
        median_vars = list(group['variables'])
        weight_vars = list(group.get('weights') or
                           [v.replace('B19013', 'B19001') for v in median_vars])
        getvars = median_vars + weight_vars
    else:
        getvars = group['denominators'] + group['numerators']
    n_head = len(getvars)
    df = _fetch(getvars, geo)
    total_cols = df.shape[1]
    for c in range(n_head):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if kind == 'median':
        nmed = len(median_vars)
        df['pct'] = df.iloc[:, 0:nmed].sum(axis=1)
        df['weight'] = df.iloc[:, nmed:nmed + len(weight_vars)].sum(axis=1)
        df['denom'] = np.nan
        df['num'] = np.nan
    else:
        nd = len(group['denominators']); nn = len(group['numerators'])
        df['denom'] = df.iloc[:, 0:nd].sum(axis=1)
        df['num'] = df.iloc[:, nd:nd + nn].sum(axis=1)
        df['pct'] = np.where(df['denom'] > 0, df['num'] / df['denom'] * 100, np.nan)
        df['weight'] = np.nan
    geocols = list(range(n_head, total_cols))
    if geo == 'statewide':
        df['geo'] = 'statewide'
    elif geo == 'county':
        df['County Key'] = df[geocols[1]].astype(str)
    else:
        df['Town GEOID'] = (df[geocols[0]].astype(str) + df[geocols[1]].astype(str) + df[geocols[2]].astype(str))
    return df


def _agg_town_to_frame(t, kind, gcol):
    """Aggregate town-level rows to a frame key (VDH District / Hospital Service Area),
    returning per-frame 'pct' and 'count' (summed denominator, or household weight for
    median). Towns under 25 are NOT dropped here -- they are pooled; suppression on
    'count' is applied by the caller at the frame level."""
    tm = townsDataFrame.merge(t[['Town GEOID', 'pct', 'num', 'denom', 'weight']], on='Town GEOID', how='left')
    if kind == 'median':
        tm = tm[(tm['pct'] > 0) & (tm['weight'] > 0)].copy()
        tm['_vw'] = tm['pct'] * tm['weight']
        g = tm.groupby(gcol, as_index=False).agg(_vw=('_vw', 'sum'), weight=('weight', 'sum'))
        g['pct'] = np.where(g['weight'] > 0, g['_vw'] / g['weight'], np.nan)
        g['count'] = g['weight']
    else:
        g = tm.groupby(gcol, as_index=False).agg(num=('num', 'sum'), denom=('denom', 'sum'))
        g['pct'] = np.where(g['denom'] > 0, g['num'] / g['denom'] * 100, np.nan)
        g['count'] = g['denom']
    return g


def _emit_group(ind_label, label, s, c, t, kind, sw_mean, cty_mean, dist_mean, hsa_mean,
                statewideRows, countiesRows, districtsRows, hospitalsRows):
    """Emit one non-BIPOC group (White or age/sex) at all four frames, suppressing any
    frame whose count (denominator, or household weight for median) is < 25."""
    # statewide
    for _, r in s.iterrows():
        if kind == 'median':
            if not (r['pct'] > 0 and r['weight'] >= DEMO_SUPPRESS_MIN):
                continue
        elif not (r['denom'] >= DEMO_SUPPRESS_MIN):
            continue
        statewideRows.append({'Indicator': ind_label, 'Demographic Group': label,
                              'Disaggregated Percentage': r['pct'], 'Total Statewide Mean': sw_mean})
    # counties
    for _, r in c.iterrows():
        if kind == 'median':
            if not (r['pct'] > 0 and r['weight'] >= DEMO_SUPPRESS_MIN):
                continue
        elif not (r['denom'] >= DEMO_SUPPRESS_MIN):
            continue
        ckey = r['County Key']
        countiesRows.append({'Indicator': ind_label, 'Demographic Group': label,
                             'Disaggregated Percentage': r['pct'],
                             'Total County Mean': cty_mean.get(ckey, np.nan),
                             'County': _county_with_suffix(countyCodeToName.get(ckey))})
    # districts & hospitals (aggregate towns, suppress at frame level)
    for gcol, keyname, acc, meancol, lookup in [
            ('VDH District', 'VDH District', districtsRows, 'Total VDH District Mean', dist_mean),
            ('Hospital Service Area', 'Hospital Service Area', hospitalsRows, 'Total Hospital Service Area Mean', hsa_mean)]:
        g = _agg_town_to_frame(t, kind, gcol)
        for _, r in g.iterrows():
            if pd.isna(r['pct']) or r['count'] < DEMO_SUPPRESS_MIN:
                continue
            acc.append({keyname: r[gcol], 'Indicator': ind_label, 'Demographic Group': label,
                        'Disaggregated Percentage': r['pct'], meancol: lookup.get(r[gcol], np.nan)})


def _emit_bipoc(ind_label, kind, bip_sw, bip_cty, bip_town, sw_mean, cty_mean, dist_mean, hsa_mean,
                statewideRows, countiesRows, districtsRows, hospitalsRows):
    """Emit the composite BIPOC group at all four frames. Rate: pooled sum(num)/sum(den).
    Median: household-weighted mean of member-race medians. Frame-level suppression on
    the summed denominator (rate) or household weight (median)."""
    # statewide
    if kind == 'median':
        if bip_sw['w'] >= DEMO_SUPPRESS_MIN and bip_sw['w'] > 0:
            statewideRows.append({'Indicator': ind_label, 'Demographic Group': BIPOC_LABEL,
                                  'Disaggregated Percentage': bip_sw['vw'] / bip_sw['w'],
                                  'Total Statewide Mean': sw_mean})
    else:
        if bip_sw['den'] >= DEMO_SUPPRESS_MIN and bip_sw['den'] > 0:
            statewideRows.append({'Indicator': ind_label, 'Demographic Group': BIPOC_LABEL,
                                  'Disaggregated Percentage': bip_sw['num'] / bip_sw['den'] * 100,
                                  'Total Statewide Mean': sw_mean})
    # counties
    for ck, d in bip_cty.items():
        if kind == 'median':
            if d['w'] < DEMO_SUPPRESS_MIN or d['w'] <= 0:
                continue
            pct = d['vw'] / d['w']
        else:
            if d['den'] < DEMO_SUPPRESS_MIN or d['den'] <= 0:
                continue
            pct = d['num'] / d['den'] * 100
        countiesRows.append({'Indicator': ind_label, 'Demographic Group': BIPOC_LABEL,
                             'Disaggregated Percentage': pct,
                             'Total County Mean': cty_mean.get(ck, np.nan),
                             'County': _county_with_suffix(countyCodeToName.get(ck))})
    # districts & hospitals (aggregate accumulated member-town rows)
    if bip_town is not None:
        bm = townsDataFrame.merge(bip_town, on='Town GEOID', how='left')
        for gcol, keyname, acc, meancol, lookup in [
                ('VDH District', 'VDH District', districtsRows, 'Total VDH District Mean', dist_mean),
                ('Hospital Service Area', 'Hospital Service Area', hospitalsRows, 'Total Hospital Service Area Mean', hsa_mean)]:
            if kind == 'median':
                d = bm[(bm['pct'] > 0) & (bm['weight'] > 0)].copy()
                d['_vw'] = d['pct'] * d['weight']
                gg = d.groupby(gcol, as_index=False).agg(_vw=('_vw', 'sum'), weight=('weight', 'sum'))
                gg['pct'] = np.where(gg['weight'] > 0, gg['_vw'] / gg['weight'], np.nan)
                gg['count'] = gg['weight']
            else:
                gg = bm.groupby(gcol, as_index=False).agg(num=('num', 'sum'), denom=('denom', 'sum'))
                gg['pct'] = np.where(gg['denom'] > 0, gg['num'] / gg['denom'] * 100, np.nan)
                gg['count'] = gg['denom']
            for _, r in gg.iterrows():
                if pd.isna(r['pct']) or r['count'] < DEMO_SUPPRESS_MIN:
                    continue
                acc.append({keyname: r[gcol], 'Indicator': ind_label, 'Demographic Group': BIPOC_LABEL,
                            'Disaggregated Percentage': r['pct'], meancol: lookup.get(r[gcol], np.nan)})


def run_demographics():
    """Build the four area-frame disaggregation tables for the current period. Race is
    collapsed to BIPOC + White at every frame; age/sex groups pass through. Indicators
    whose base model is unavailable this period (broadband in 2010-2014) are skipped."""
    global townsDataFrame
    _townMeta = geographyDenominatorsDataframes['county subdivision']
    townsDataFrame = pd.DataFrame({
        'Town GEOID':            _townMeta['TownGEOID'].astype(str),
        'County Key':            _townMeta['TownGEOID'].astype(str).str[2:5],
        'VDH District':          _townMeta['VDHDSTRTNME'],
        'VDH District Key':      _townMeta['VDHDSTRTCDE'],
        'Hospital Service Area': _townMeta['HSPLSANAME'],
        'Hospital Key':          _townMeta['HSPLSACODE'],
    })
    statewideRows, countiesRows, districtsRows, hospitalsRows = [], [], [], []
    cfg = _demoCfg['indicators']
    for name, ind in cfg.items():
        model_key = ind['model_key']; kind = ind['kind']
        ind_label = ind['indicator_label']
        if model_key not in geographyDataframes:
            print(f"[CRI] {PERIOD_LABEL}: base indicator '{model_key}' unavailable -> skipping its disaggregation.")
            continue
        sw_mean, cty_mean, dist_mean, hsa_mean = _total_means(model_key, kind)

        # BIPOC accumulators across non-white race members
        bip_sw = {'num': 0.0, 'den': 0.0, 'vw': 0.0, 'w': 0.0, 'present': False}
        bip_cty = {}        # County Key -> dict
        bip_town = None     # concatenated member-town rows

        for group in ind['groups']:
            label = group['label']
            try:
                s = _process_group(group, kind, 'statewide')
                c = _process_group(group, kind, 'county')
                t = _process_group(group, kind, 'town')
            except AcsUnavailable as e:
                print(f"[CRI] {PERIOD_LABEL}: {ind_label}/{label} unavailable ({e}) -> group skipped.")
                continue

            if label in BIPOC_MEMBERS:
                bip_sw['present'] = True
                for _, r in s.iterrows():
                    if kind == 'median':
                        if r['pct'] > 0 and r['weight'] > 0:
                            bip_sw['vw'] += r['pct'] * r['weight']; bip_sw['w'] += r['weight']
                    elif r['denom'] >= 0:
                        bip_sw['num'] += (r['num'] if r['num'] >= 0 else 0); bip_sw['den'] += r['denom']
                for _, r in c.iterrows():
                    d = bip_cty.setdefault(r['County Key'], {'num': 0.0, 'den': 0.0, 'vw': 0.0, 'w': 0.0})
                    if kind == 'median':
                        if r['pct'] > 0 and r['weight'] > 0:
                            d['vw'] += r['pct'] * r['weight']; d['w'] += r['weight']
                    elif r['denom'] >= 0:
                        d['num'] += (r['num'] if r['num'] >= 0 else 0); d['den'] += r['denom']
                add = t[['Town GEOID', 'num', 'denom', 'weight', 'pct']].copy()
                bip_town = add if bip_town is None else pd.concat([bip_town, add], ignore_index=True)
            else:
                _emit_group(ind_label, label, s, c, t, kind, sw_mean, cty_mean, dist_mean, hsa_mean,
                            statewideRows, countiesRows, districtsRows, hospitalsRows)

        if bip_sw['present']:
            _emit_bipoc(ind_label, kind, bip_sw, bip_cty, bip_town, sw_mean, cty_mean, dist_mean, hsa_mean,
                        statewideRows, countiesRows, districtsRows, hospitalsRows)

    statewide = pd.DataFrame(statewideRows)
    counties = pd.DataFrame(countiesRows)
    districts = pd.DataFrame(districtsRows)
    hospitals = pd.DataFrame(hospitalsRows)
    for d in (statewide, counties, districts, hospitals):
        if 'Disaggregated Percentage' in d:
            d['Disaggregated Percentage'] = d['Disaggregated Percentage'].round(2)
    for d, mc in [(statewide, 'Total Statewide Mean'), (counties, 'Total County Mean'),
                  (districts, 'Total VDH District Mean'), (hospitals, 'Total Hospital Service Area Mean')]:
        if mc in d:
            d[mc] = d[mc].round(2)
        d['Data Years'] = DATA_YEARS
    statewide = statewide.reindex(columns=['Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total Statewide Mean', 'Data Years'])
    counties = counties.reindex(columns=['Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total County Mean', 'County', 'Data Years'])
    districts = districts.reindex(columns=['VDH District', 'Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total VDH District Mean', 'Data Years'])
    hospitals = hospitals.reindex(columns=['Hospital Service Area', 'Indicator', 'Demographic Group', 'Disaggregated Percentage', 'Total Hospital Service Area Mean', 'Data Years'])
    return statewide, counties, districts, hospitals
# =============================================================================
# PER-PERIOD PIPELINE
# =============================================================================
def run_period(period_end_year):
    """Run the full ETL for one 5-year ACS window (END year) and RETURN a dict of
    output DataFrames (not written here). The driver loop appends across periods."""
    global year, baseUrl, PERIOD_LABEL, DATA_YEARS
    global geographyDataframes, geographyDenominatorsDataframes, geographyUnpopulatedDataframes

    period_start_year = period_end_year - 4
    PERIOD_LABEL = f"{period_start_year}-{period_end_year}"       # e.g. "2020-2024" (filenames/vintage)
    DATA_YEARS = f"{period_start_year} - {period_end_year}"       # e.g. "2020 - 2024" (Data Years column)
    year = str(period_end_year)
    baseUrl = "/".join([censusDataURL, year, dataset])
    print(f"\n#################### ACS 5-Year {PERIOD_LABEL} ####################")

    # ---- geography denominators + unpopulated split ----
    geographyDenominatorsDataframes = dict()
    geographyUnpopulatedDataframes = dict()
    for geography in geographyVariables:
        if geography != 'statewide':
            variables = acsDatasourceFields
            boundaries = geographyVariables[geography]['boundaries']
            parameters = {'get': variables, 'for': f'{geography}:*', 'in': boundaries, 'key': apiKey}
            geographies = requests.get(baseUrl, params=parameters).json()

            geographiesDataFrame = pd.DataFrame(geographies[1:])
            columns = geographyVariables[geography]['columns']
            geographiesDataFrame = geographiesDataFrame.rename(columns=columns)
            if geography == 'county subdivision':
                geographiesDataFrame['TownGEOID'] = (geographiesDataFrame['stateKey'] +
                                                     geographiesDataFrame['countyKey'] +
                                                     geographiesDataFrame['townFIPS'])
                crosswalkFields = ["TWNNMEMC", "TWNGEOID", "HSPLSANAME", "HSPLSACODE", "VDHDSTRTNME", "VDHDSTRTCDE"]
                crosswalkDataArray = arcpy.da.FeatureClassToNumPyArray(crosswalkTable, crosswalkFields)
                crosswalkDataFrame = pd.DataFrame(crosswalkDataArray, columns=crosswalkFields)
                crosswalkDataFrame.drop_duplicates(subset='TWNGEOID', inplace=True)
                geographiesDataFrame = geographiesDataFrame.merge(crosswalkDataFrame, how='left', left_on='TownGEOID', right_on='TWNGEOID')
            elif geography == 'county':
                geographiesDataFrame['CountyGEOID'] = (geographiesDataFrame['stateKey'] +
                                                       geographiesDataFrame['countyKey'])

            geographiesDataFrame['countyName'] = geographiesDataFrame['countyKey'].map(countyNamesDictionary)

            unpopulatedMask = (pd.to_numeric(geographiesDataFrame['population'], errors='coerce').isnull() |
                               (geographiesDataFrame['population'] == '0'))
            geographyUnpopulatedDataframes[geography] = geographiesDataFrame[unpopulatedMask].copy()
            geographiesDataFrame = geographiesDataFrame[~unpopulatedMask].copy()
            geographyDenominatorsDataframes[geography] = geographiesDataFrame

    # ---- per-indicator ACS acquisition (drop an indicator whose data is absent) ----
    geographyDataframes = dict()
    for indicator in indicators:
        indicatorNumeratorVariables = indicators[indicator]['variables']['numerators']
        indicatorDenominatorVariables = indicators[indicator]['variables']['denominators']
        indicatorWeightVariables = indicators[indicator]['variables'].get('weights', [])
        variablesStringNumerator = ','.join(indicatorNumeratorVariables)
        variablesStringDenominator = ','.join(indicatorDenominatorVariables)
        variablesStringWeight = ','.join(indicatorWeightVariables)
        perGeo = dict()
        try:
            for geography in geographyVariables:
                if len(indicators[indicator]['columnsForStats'][geography]) == 0:
                    continue
                if geography == 'county':
                    urlNumerator = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringNumerator}&for={geography}:*&in=state:{state}&key={apiKey}'
                    urlDenominator = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringDenominator}&for={geography}:*&in=state:{state}&key={apiKey}'
                    urlWeight = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringWeight}&for={geography}:*&in=state:{state}&key={apiKey}'
                elif geography == 'statewide':
                    urlNumerator = f'https://api.census.gov/data/{year}/acs/acs5?for=state:{state}&get={variablesStringNumerator}&key={apiKey}'
                    urlDenominator = f'https://api.census.gov/data/{year}/acs/acs5?for=state:{state}&get={variablesStringDenominator}&key={apiKey}'
                    urlWeight = f'https://api.census.gov/data/{year}/acs/acs5?for=state:{state}&get={variablesStringWeight}&key={apiKey}'
                else:
                    urlNumerator = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringNumerator}&for={geography}:*&in=state:{state}+county:*&key={apiKey}'
                    urlDenominator = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringDenominator}&for={geography}:*&in=state:{state}+county:*&key={apiKey}'
                    urlWeight = f'https://api.census.gov/data/{year}/acs/acs5?get={variablesStringWeight}&for={geography}:*&in=state:{state}+county:*&key={apiKey}'

                indicatorResponseNumeratorJSON = acs_get(urlNumerator)
                indicatorDataFrameNumerator = pd.DataFrame.from_dict(indicatorResponseNumeratorJSON[1:])
                if len(indicatorDenominatorVariables) != 0:
                    indicatorResponseDenominatorJSON = acs_get(urlDenominator)
                    indicatorDataFrameDenominator = pd.DataFrame.from_dict(indicatorResponseDenominatorJSON[1:])

                geographyFieldIndex = geographyDataFrameGeographyFieldIndex[geography]
                columnsTojoin = indicatorDataFrameNumerator.columns[-geographyFieldIndex:]
                indicatorDataFrameNumerator['JoinKey'] = indicatorDataFrameNumerator[columnsTojoin].astype(str).apply(''.join, axis=1)
                indicatorDataFrameNumerator.drop(columnsTojoin, axis=1, inplace=True)
                indicatorDataFrameNumerator.iloc[:, :-1] = indicatorDataFrameNumerator.iloc[:, :-1].apply(pd.to_numeric, errors='coerce')
                indicatorDataFrameNumerator.loc[:, 'Numerator'] = indicatorDataFrameNumerator.iloc[:, :-1].sum(axis=1)
                indicatorDataFrameNumerator.drop(indicatorDataFrameNumerator.columns[:-2], axis=1, inplace=True)

                if len(indicatorDenominatorVariables) != 0:
                    columnsTojoin = indicatorDataFrameDenominator.columns[-geographyFieldIndex:]
                    indicatorDataFrameDenominator['JoinKey'] = indicatorDataFrameDenominator[columnsTojoin].astype(str).apply(''.join, axis=1)
                    indicatorDataFrameDenominator.drop(columnsTojoin, axis=1, inplace=True)
                    indicatorDataFrameDenominator.iloc[:, :-1] = indicatorDataFrameDenominator.iloc[:, :-1].apply(pd.to_numeric, errors='coerce')
                    indicatorDataFrameDenominator.loc[:, 'Denominator'] = indicatorDataFrameDenominator.iloc[:, :-1].sum(axis=1)
                    indicatorDataFrameDenominator.drop(indicatorDataFrameDenominator.columns[:-2], axis=1, inplace=True)
                    baseIndicatorDataFrame = indicatorDataFrameNumerator.merge(indicatorDataFrameDenominator, on='JoinKey')
                else:
                    baseIndicatorDataFrame = indicatorDataFrameNumerator

                if len(indicatorWeightVariables) != 0:
                    indicatorResponseWeightJSON = acs_get(urlWeight)
                    indicatorDataFrameWeight = pd.DataFrame.from_dict(indicatorResponseWeightJSON[1:])
                    columnsTojoin = indicatorDataFrameWeight.columns[-geographyFieldIndex:]
                    indicatorDataFrameWeight['JoinKey'] = indicatorDataFrameWeight[columnsTojoin].astype(str).apply(''.join, axis=1)
                    indicatorDataFrameWeight.drop(columnsTojoin, axis=1, inplace=True)
                    indicatorDataFrameWeight.iloc[:, :-1] = indicatorDataFrameWeight.iloc[:, :-1].apply(pd.to_numeric, errors='coerce')
                    indicatorDataFrameWeight.loc[:, 'Weight'] = indicatorDataFrameWeight.iloc[:, :-1].sum(axis=1)
                    indicatorDataFrameWeight = indicatorDataFrameWeight[['JoinKey', 'Weight']]
                    baseIndicatorDataFrame = baseIndicatorDataFrame.merge(indicatorDataFrameWeight, on='JoinKey', how='left')

                perGeo[geography] = baseIndicatorDataFrame
        except AcsUnavailable as e:
            print(f"[CRI] {PERIOD_LABEL}: indicator '{indicator}' unavailable -> dropped from this period's index. ({e})")
            continue
        geographyDataframes[indicator] = perGeo

    # ---- prep crosswalk table view for joining ----
    crosswalkTableSelection = "crosswalkTableSelection"
    clause = "TYPEMUNIC = 'Town' Or TYPEMUNIC = 'City'"
    if arcpy.Exists(crosswalkTableSelection):
        arcpy.management.Delete(crosswalkTableSelection)
    arcpy.management.MakeTableView(crosswalkTable, crosswalkTableSelection, clause)

    # ---- per-indicator summary stats (rate & count paths) ----
    for indicator in geographyDataframes:
        for geography in geographyDataframes[indicator]:
            if geography == 'county subdivision':
                currentDataFrame = geographyDataframes[indicator][geography]
                fields = ['TWNGEOID', 'HLTHSANAME', 'VDHDSTRTNME', 'CNTYNAME']
                crosswalkListOfTuples = list()
                with arcpy.da.SearchCursor(crosswalkTableSelection, fields) as searchCursor:
                    for row in searchCursor:
                        crosswalkListOfTuples.append(row)
                crosswalkDataFrame = pd.DataFrame(crosswalkListOfTuples, columns=fields)
                _dupKeys = crosswalkDataFrame['TWNGEOID'][crosswalkDataFrame['TWNGEOID'].duplicated(keep=False)].unique()
                if len(_dupKeys):
                    print(f"[CRI WARNING] crosswalk has {len(_dupKeys)} duplicated TWNGEOID value(s). Keys: {list(_dupKeys)[:20]}")
                currentDataFrame = pd.merge(currentDataFrame, crosswalkDataFrame, left_on='JoinKey',
                                            right_on='TWNGEOID', how='inner')
                currentDataFrame.drop('TWNGEOID', axis=1, inplace=True)
                _dupTowns = currentDataFrame['JoinKey'][currentDataFrame['JoinKey'].duplicated(keep=False)].unique()
                if len(_dupTowns):
                    print(f"[CRI WARNING] {indicator}/{geography}: {len(_dupTowns)} town(s) duplicated after merge. Keys: {list(_dupTowns)[:20]}")
            else:
                currentDataFrame = geographyDataframes[indicator][geography]
                currentDataFrame['CNTYCODE'] = currentDataFrame['JoinKey'].str[2:5]
                currentDataFrame['CNTYNAME'] = currentDataFrame['CNTYCODE'].map(countyNamesDictionary)

            if 'Denominator' in currentDataFrame.columns:
                currentDataFrame['PercentPerCapita'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].div(currentDataFrame['Denominator'].where(currentDataFrame['Denominator'] != 0)) * 100
                currentDataFrame['MeanPercentPerCapita'] = currentDataFrame['PercentPerCapita'].mean()
                currentDataFrame['PercentPerCapitaRank'] = currentDataFrame['PercentPerCapita'].rank(pct=True) * 100
                currentDataFrame['StdDevPercentPerCapita'] = currentDataFrame['PercentPerCapita'].std()

                if geography == 'county subdivision':
                    currentDataFrame['PercentPerCapitaCountyRank'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].rank(pct=True) * 100
                    currentDataFrame['PercentPerCapitaCountySD'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].transform('std')
                    currentDataFrame['PercentPerCapitaCountyMean'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].transform('mean')
                    currentDataFrame['PercentPerCapitaDistrictRank'] = currentDataFrame.groupby('VDHDSTRTNME')['PercentPerCapita'].rank(pct=True) * 100
                    currentDataFrame['PercentPerCapitaDistrictSD'] = currentDataFrame.groupby('VDHDSTRTNME')['PercentPerCapita'].transform('std')
                    currentDataFrame['PercentPerCapitaDistrictMean'] = _frame_weighted_rate(currentDataFrame, 'VDHDSTRTNME')
                    currentDataFrame['PercentPerCapitaHSARank'] = currentDataFrame.groupby('HLTHSANAME')['PercentPerCapita'].rank(pct=True) * 100
                    currentDataFrame['PercentPerCapitaHSASD'] = currentDataFrame.groupby('HLTHSANAME')['PercentPerCapita'].transform('std')
                    currentDataFrame['PercentPerCapitaHSAMean'] = _frame_weighted_rate(currentDataFrame, 'HLTHSANAME')
                else:
                    currentDataFrame['PercentPerCapitaCountyRank'] = currentDataFrame.groupby('CNTYCODE')['PercentPerCapita'].rank(pct=True) * 100
            else:
                if geography == 'county subdivision':
                    currentDataFrame['MeanIndicatorCount'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].mean()
                    currentDataFrame['IndicatorRank'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].rank(pct=True) * 100
                    currentDataFrame['StdDevIndicator'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].std()
                    currentDataFrame['IndicatorCountyRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].rank(pct=True) * 100)
                    currentDataFrame['IndicatorCountySD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].transform('std'))
                    currentDataFrame['IndicatorCountyMean'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].transform('mean'))
                    currentDataFrame['IndicatorDistrictRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('VDHDSTRTNME')['Numerator'].rank(pct=True) * 100)
                    currentDataFrame['IndicatorDistrictSD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('VDHDSTRTNME')['Numerator'].transform('std'))
                    currentDataFrame['IndicatorDistrictMean'] = _frame_weighted_value(currentDataFrame, 'VDHDSTRTNME')
                    currentDataFrame['IndicatorHSARank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('HLTHSANAME')['Numerator'].rank(pct=True) * 100)
                    currentDataFrame['IndicatorHSASD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('HLTHSANAME')['Numerator'].transform('std'))
                    currentDataFrame['IndicatorHSAMean'] = _frame_weighted_value(currentDataFrame, 'HLTHSANAME')
                else:
                    currentDataFrame['MeanIndicatorCount'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].mean()
                    currentDataFrame['IndicatorRank'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].rank(pct=True) * 100
                    currentDataFrame['StdDevIndicator'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].std()
                    currentDataFrame['IndicatorCountyRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].rank(pct=True) * 100)
                    currentDataFrame['IndicatorCountySD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].transform('std'))
                    currentDataFrame['IndicatorCountyMean'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].transform('mean'))

            geographyDataframes[indicator][geography] = currentDataFrame
    # ---- area-frame normalized indicator means (SMD Step 1 reference) ----
    for indicator in geographyDataframes:
        swFrame = geographyDataframes[indicator].get('statewide')
        coFrame = geographyDataframes[indicator].get('county')
        swValCol = 'PercentPerCapita' if (swFrame is not None and 'PercentPerCapita' in swFrame.columns) else 'Numerator'
        statewideAggregate = (pd.to_numeric(swFrame[swValCol], errors='coerce').iloc[0]
                              if swFrame is not None and len(swFrame) else np.nan)
        if coFrame is not None and len(coFrame):
            coValCol = 'PercentPerCapita' if 'PercentPerCapita' in coFrame.columns else 'Numerator'
            countyAggregateByCode = pd.to_numeric(coFrame.set_index('CNTYCODE')[coValCol], errors='coerce')
        else:
            countyAggregateByCode = pd.Series(dtype=float)
        for geography in ['county subdivision']:
            df = geographyDataframes[indicator].get(geography)
            if df is None:
                continue
            df['AreaFrameMeanStatewide'] = statewideAggregate
            df['AreaFrameMeanCounty'] = df['JoinKey'].astype(str).str[2:5].map(countyAggregateByCode)
            geographyDataframes[indicator][geography] = df

    # ---- zero-SD diagnostic (flags a frame collapsed to one byte-identical value) ----
    _SD_GROUP_COLS = {
        'PercentPerCapitaCountySD':    ('CNTYNAME',     'PercentPerCapita'),
        'PercentPerCapitaDistrictSD':  ('VDHDSTRTNME',  'PercentPerCapita'),
        'PercentPerCapitaHSASD':       ('HLTHSANAME',   'PercentPerCapita'),
        'IndicatorCountySD':           ('CNTYNAME',     'Numerator'),
        'IndicatorDistrictSD':         ('VDHDSTRTNME',  'Numerator'),
        'IndicatorHSASD':              ('HLTHSANAME',   'Numerator'),
    }
    for indicator in geographyDataframes:
        for geography in ['county subdivision']:
            df = geographyDataframes[indicator].get(geography)
            if df is None:
                continue
            for sdCol, (keyCol, valCol) in _SD_GROUP_COLS.items():
                if sdCol not in df.columns or keyCol not in df.columns:
                    continue
                sd = pd.to_numeric(df[sdCol], errors='coerce')
                zeroGroups = df.loc[sd == 0, keyCol].dropna().unique()
                for g in zeroGroups:
                    members = df.loc[df[keyCol] == g, valCol]
                    vals = sorted(pd.to_numeric(members, errors='coerce').dropna().round(4).unique().tolist())
                    print(f"[CRI ZERO-SD] {indicator} / {geography} / {sdCol} -> group '{g}': "
                          f"{len(members)} member rows, {len(vals)} distinct value(s) {vals[:10]}")

    # ---- Standard Mean Difference ----
    for indicator in geographyDataframes:
        for geography in geographyDataframes[indicator]:
            if geography == 'county subdivision':
                currentDataFrame = geographyDataframes[indicator][geography]
                if 'Denominator' in currentDataFrame.columns:
                    currentDataFrame[f'{geography.title()}StatewideSMD'] = _smd(currentDataFrame['AreaFrameMeanStatewide'], currentDataFrame['PercentPerCapita'], currentDataFrame['StdDevPercentPerCapita'])
                    currentDataFrame[f'{geography.title()}CountySMD'] = _smd(currentDataFrame['AreaFrameMeanCounty'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaCountySD'])
                    currentDataFrame[f'{geography.title()}DistrictSMD'] = _smd(currentDataFrame['PercentPerCapitaDistrictMean'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaDistrictSD'])
                    currentDataFrame[f'{geography.title()}HSASMD'] = _smd(currentDataFrame['PercentPerCapitaHSAMean'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaHSASD'])
                else:
                    currentDataFrame[f'{geography.title()}StatewideSMD'] = _smd(currentDataFrame['AreaFrameMeanStatewide'], currentDataFrame['Numerator'], currentDataFrame['StdDevIndicator'])
                    currentDataFrame[f'{geography.title()}CountySMD'] = _smd(currentDataFrame['AreaFrameMeanCounty'], currentDataFrame['Numerator'], currentDataFrame['IndicatorCountySD'])
                    currentDataFrame[f'{geography.title()}DistrictSMD'] = _smd(currentDataFrame['IndicatorDistrictMean'], currentDataFrame['Numerator'], currentDataFrame['IndicatorDistrictSD'])
                    currentDataFrame[f'{geography.title()}HSASMD'] = _smd(currentDataFrame['IndicatorHSAMean'], currentDataFrame['Numerator'], currentDataFrame['IndicatorHSASD'])
            else:
                pass

    # ---- single composite index: one "Overall" theme over ALL present indicators ----
    thematic_data_list = []
    themes = {'Overall': list(geographyDataframes.keys())}

    for theme_name, indicator_list in themes.items():
        for geography in ['county subdivision']:
            unit_metrics = {}
            for indicator in indicator_list:
                if geography not in geographyDataframes.get(indicator, {}):
                    continue
                current_df = geographyDataframes[indicator][geography]
                is_count_indicator = 'IndicatorRank' in current_df.columns
                active_contexts = count_contexts if is_count_indicator else contexts
                for index, row in current_df.iterrows():
                    geo_unit_id = row['JoinKey']
                    if geo_unit_id not in unit_metrics:
                        unit_metrics[geo_unit_id] = {
                            'Ranks_Statewide': [], 'SMDs_Statewide': [],
                            'Ranks_County': [], 'SMDs_County': [],
                            'Ranks_District': [], 'SMDs_District': [],
                            'Ranks_HSA': [], 'SMDs_HSA': []
                        }
                        unit_metrics[geo_unit_id]['info'] = {
                            'Geography': geography,
                            'CNTYNAME': row.get('CNTYNAME', np.nan),
                            'VDHDSTRTNME': row.get('VDHDSTRTNME', np.nan),
                            'HLTHSANAME': row.get('HLTHSANAME', np.nan)
                        }
                    for context_name, cols in active_contexts.items():
                        rank_col = cols['rank']
                        smd_col = f"{geography.title()}{cols['smd']}"
                        if rank_col in row and not pd.isna(row[rank_col]):
                            unit_metrics[geo_unit_id][f'Ranks_{context_name}'].append(row[rank_col])
                        if smd_col in row and not pd.isna(row[smd_col]):
                            unit_metrics[geo_unit_id][f'SMDs_{context_name}'].append(row[smd_col])

            for geo_unit_id, data in unit_metrics.items():
                thematic_data_list.append({
                    'Theme': theme_name,
                    'Geography': geography,
                    'GeographyUnit': geo_unit_id,
                    'CNTYNAME': data['info']['CNTYNAME'],
                    'VDHDSTRTNME': data['info']['VDHDSTRTNME'],
                    'HLTHSANAME': data['info']['HLTHSANAME'],
                    'ThematicGMean_Statewide': scipygmean(data['Ranks_Statewide']),
                    'ThematicGMean_County':    scipygmean(data['Ranks_County']),
                    'ThematicGMean_District':  scipygmean(data['Ranks_District']),
                    'ThematicGMean_HSA':       scipygmean(data['Ranks_HSA']),
                })

    thematicDataFrame = pd.DataFrame(thematic_data_list)
    for context in ['Statewide', 'County', 'District', 'HSA']:
        rank_col = f'ThematicGMean_{context}'
        index_col = f'ThematicIndex_{context}'
        if rank_col in thematicDataFrame.columns:
            valid_ranks = thematicDataFrame[thematicDataFrame[rank_col].notna()].copy()
            if context == 'Statewide':
                ranked_series = valid_ranks.groupby(['Theme', 'Geography'])[rank_col].rank(pct=True) * 100
            elif context == 'County':
                ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'CNTYNAME'])[rank_col].rank(pct=True) * 100
            elif context == 'District':
                ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'VDHDSTRTNME'])[rank_col].rank(pct=True) * 100
            elif context == 'HSA':
                ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'HLTHSANAME'])[rank_col].rank(pct=True) * 100
            thematicDataFrame = thematicDataFrame.merge(ranked_series.rename(index_col), left_index=True, right_index=True, how='left')

    thematicDataFrame.to_csv(os.path.join(SCRATCH_DIR, f'thematicDataFrame_{PERIOD_LABEL}.csv'), index=False)
    outputs = {}

    # ---- published index table (townsIndexGeometric) ----
    for geography, spec in INDEX_SPECS.items():
        ctx_list = spec['contexts']
        tdf = thematicDataFrame[thematicDataFrame['Geography'] == geography].copy()
        score_renames = {f'ThematicIndex_{ctx}': INDEX_SCORE_LABEL[ctx] for ctx in ctx_list
                         if f'ThematicIndex_{ctx}' in tdf.columns}
        tdf = tdf.rename(columns=score_renames)
        for col in score_renames.values():
            tdf[col] = pd.to_numeric(tdf[col], errors='coerce').round(2)

        wide = build_indicator_percentile_wide(geography, ctx_list)
        denom = geographyDenominatorsDataframes[geography].copy()
        meta = denom[['TownGEOID', 'TWNNMEMC', 'countyName', 'VDHDSTRTNME', 'HSPLSANAME', 'population']].rename(
            columns={'TownGEOID': 'GeographyUnit', 'TWNNMEMC': 'Town Name', 'countyName': 'County',
                     'VDHDSTRTNME': 'VDH District', 'HSPLSANAME': 'Hospital Service Area',
                     'population': 'Town Population'})
        meta['County'] = meta['County'].map(_county_with_suffix)

        merged = tdf.merge(meta, on='GeographyUnit', how='left')
        if wide is not None:
            merged = merged.merge(wide, left_on='GeographyUnit', right_on='JoinKey', how='left')
        merged['Town GEOID'] = merged['GeographyUnit']
        merged['Data Years'] = DATA_YEARS

        pct_cols = [f'{prefix}{PCT_CTX_LABEL[ctx]}Percentile'
                    for prefix in INDICATOR_PREFIX.values() for ctx in ctx_list]
        ordered = spec['id_cols'] + [INDEX_SCORE_LABEL[c] for c in ctx_list] + pct_cols
        out = merged.reindex(columns=ordered)
        out = out.sort_values(['County', 'Town Name']).reset_index(drop=True)
        outputs[spec['stem']] = out

    # ---- published indicator table (townsIndicators, stacked long) ----
    wdenom = geographyDenominatorsDataframes['county subdivision'][
        ['TownGEOID', 'TWNNMEMC', 'countyName', 'VDHDSTRTNME', 'HSPLSANAME', 'population']].rename(
        columns={'TownGEOID': 'JoinKey', 'TWNNMEMC': 'Town Name', 'countyName': 'County',
                 'VDHDSTRTNME': 'VDH District', 'HSPLSANAME': 'Hospital Service Area', 'population': 'Town Population'})
    wdenom['County'] = wdenom['County'].map(_county_with_suffix)
    townsIndicators = build_indicator_stack('county subdivision', ['Statewide', 'County', 'District', 'HSA'],
                                            'County Subdivision').merge(wdenom, on='JoinKey', how='left')
    townsIndicators = townsIndicators.rename(columns={
        'JoinKey': 'Town GEOID',
        '_mean_Statewide': 'Statewide Indicator Mean', '_mean_County': 'County Indicator Mean',
        '_mean_District': 'District Indicator Mean', '_mean_HSA': 'Hospital Indicator Mean',
        '_value': 'Town Indicator Percentage',
        '_pctl_Statewide': 'Town Statewide Percentile', '_pctl_County': 'Town County Percentile',
        '_pctl_District': 'Town District Percentile', '_pctl_HSA': 'Town Hospital Percentile',
        '_smd_Statewide': 'Town Statewide Standard Mean Difference', '_smd_County': 'Town County Standard Mean Difference',
        '_smd_District': 'Town District Standard Mean Difference', '_smd_HSA': 'Town Hospital Standard Mean Difference'})
    townsIndicators = townsIndicators.reindex(columns=[
        'Indicator', 'County', 'VDH District', 'Hospital Service Area', 'Statewide Indicator Mean',
        'County Indicator Mean', 'District Indicator Mean', 'Hospital Indicator Mean', 'Town Name', 'Town GEOID',
        'Data Years', 'Town Population', 'Town Indicator Percentage', 'Town Statewide Percentile', 'Town County Percentile',
        'Town District Percentile', 'Town Hospital Percentile', 'Town Statewide Standard Mean Difference',
        'Town County Standard Mean Difference', 'Town District Standard Mean Difference',
        'Town Hospital Standard Mean Difference']).sort_values(
        ['Indicator', 'County', 'Town Name']).reset_index(drop=True)
    outputs['townsIndicators'] = townsIndicators

    # ---- unpopulated towns ----
    townsUnpopulated = geographyUnpopulatedDataframes['county subdivision'][
        ['TWNNMEMC', 'countyName', 'VDHDSTRTNME', 'HSPLSANAME', 'TownGEOID', 'population']].copy()
    townsUnpopulated['countyName'] = townsUnpopulated['countyName'].map(_county_with_suffix)
    townsUnpopulated['population'] = pd.to_numeric(townsUnpopulated['population'], errors='coerce').fillna(0).astype(int)
    townsUnpopulated['Data Years'] = DATA_YEARS
    townsUnpopulated = townsUnpopulated.rename(columns={
        'TWNNMEMC': 'Town Name', 'countyName': 'County', 'VDHDSTRTNME': 'VDH District',
        'HSPLSANAME': 'Hospital Service Area', 'TownGEOID': 'Town GEOID', 'population': 'Town Population'}).reindex(columns=[
        'Town Name', 'County', 'VDH District', 'Hospital Service Area', 'Town GEOID', 'Data Years', 'Town Population'])
    outputs['townsUnpopulated'] = townsUnpopulated

    # ---- disaggregated demographics (BIPOC + White race groups) ----
    print("##################### Disaggregated Demographic Data #####################")
    sw, co, di, ho = run_demographics()
    outputs['statewideDisaggregated'] = sw
    outputs['countiesDisaggregated'] = co
    outputs['districtsDisaggregated'] = di
    outputs['hospitalsDisaggregated'] = ho

    # ---- stamp vintage on every output for the appended multi-year workbook ----
    for stem, df in outputs.items():
        if 'ACS 5-Year' not in df.columns:
            df.insert(0, 'ACS 5-Year', PERIOD_LABEL)
    return outputs


# =============================================================================
# DRIVER: run every 5-year window, append each output type into one workbook.
# =============================================================================
all_outputs = {}
for _end_year in YEARS:
    period_outputs = run_period(_end_year)
    for stem, df in period_outputs.items():
        all_outputs.setdefault(stem, []).append(df)

for stem, frames in all_outputs.items():
    combined = pd.concat(frames, ignore_index=True)
    path = os.path.join(OUTPUT_DIR, f"{stem}.xlsx")
    combined.to_excel(path, index=False)
    print(f"Wrote {path}  ({combined.shape[0]} rows, {combined.shape[1]} cols)")

print("CRI pipeline complete. Combined multi-year outputs in:", OUTPUT_DIR)
