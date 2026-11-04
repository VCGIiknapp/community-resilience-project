# library imports
import requests
import os
import json
import pandas as pd
import arcpy
import numpy as np
from scipy.stats import gmean as _scipy_gmean


def scipygmean(values):
    """Geometric mean that returns NaN for an empty sample instead of emitting
    scipy's SmallSampleWarning. An empty rank list is expected and benign -- e.g. a
    tract unit has no VDH-district / hospital-service-area context, so its
    Ranks_District / Ranks_HSA lists are always empty. Non-empty samples are passed
    straight through to scipy.stats.gmean (which ignores NaNs via nan_policy)."""
    arr = np.asarray(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        return np.nan
    return _scipy_gmean(arr)


def _frame_weighted_rate(df, group_col, num_col='Numerator', den_col='Denominator'):
    """Population-weighted area-frame rate for a RATE indicator, broadcast to every
    member row: sum(numerator) / sum(denominator) * 100 within each group.

    This is the "Standard Mean Difference > Step 1" reference for the District and HSA
    area frames (which have no ACS geography of their own). It replaces an unweighted
    mean of town rates, so a 200-person town no longer counts the same as Burlington;
    the result equals the rate ACS would report if it tabulated the frame directly.
    Rows carrying an ACS sentinel / invalid numerator (<0) or a non-positive
    denominator are excluded from both sums. A group with no valid member (or whose
    denominators sum to 0) returns NaN."""
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
    each group, where `weight` is a per-town count (households for median income).

    A plain mean of town medians lets tiny towns pull the frame value; weighting by
    household count makes the District/HSA reference population-representative. Falls
    back to an unweighted mean when no weight column is present. Rows with an invalid
    value (<0 ACS sentinel) or a non-positive weight are excluded; an empty group
    returns NaN."""
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


# =============================================================================
# RUN CONFIGURATION
# -----------------------------------------------------------------------------
# ACS 5-year release: endpoint `year` is the END year of the 5-year window.
# 2019-2023 window  ->  endpoint year "2023".
PERIOD_END_YEAR = 2023                                   
PERIOD_START_YEAR = PERIOD_END_YEAR - 4
PERIOD_LABEL = f"{PERIOD_START_YEAR}-{PERIOD_END_YEAR}"       # e.g. "2019-2023" -> used in FILENAMES (no spaces)
DATA_YEARS = f"{PERIOD_START_YEAR} - {PERIOD_END_YEAR}"       # e.g. "2019 - 2023" -> value in the "Data Years" COLUMN (spaces, matches prior outputs)

# Project root is two levels up from this script (src/ -> project root).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# All deliverables are written here: data/output/<period>/
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data", "output", PERIOD_LABEL)
os.makedirs(OUTPUT_DIR, exist_ok=True)

def output_path(stem):
    """Build an output filepath: '<OUTPUT_DIR>/<stem><PERIOD_LABEL>.xlsx'.
    e.g. output_path('tractsIndexGeometric') -> .../2019-2023/tractsIndexGeometric2019-2023.xlsx"""
    return os.path.join(OUTPUT_DIR, f"{stem}{PERIOD_LABEL}.xlsx")

# Scratch location for intermediate/debug dumps (kept out of the deliverable dir).
SCRATCH_DIR = os.path.join(OUTPUT_DIR, "_scratch")
os.makedirs(SCRATCH_DIR, exist_ok=True)

# paths and global variables
# -----------------------------------------------------------------------------
# Internal UNC / network paths are kept OUT of version control so that server
# names and share layouts are not publicly viewable once this repo is published.
# They live in config/network_paths.json (git-ignored); config/network_paths.example.json
# documents the expected structure. Edit the real file locally, never commit it.
_networkPathsFile = os.path.join(PROJECT_ROOT, "config", "network_paths.json")
_networkPaths = {}
if os.path.exists(_networkPathsFile):
    with open(_networkPathsFile, encoding="utf-8") as _npf:
        _networkPaths = json.load(_npf)

# Census API key: prefer the repo-local config/CensusAPI.txt, fall back to the
# network path from network_paths.json so existing scheduled runs keep working.
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
    apiKey=key.read().strip()

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
# If parsing data that will then use this county function to get names, before the data gets to the
# step of grabbing county names, include a check across all records for valid county names
# ------------------------------------------------------------------------------
censusDataURL = "https://api.census.gov/data"
year = str(PERIOD_END_YEAR)   # ACS5 endpoint year = END of the 5-year window (see RUN CONFIGURATION)
dataset = "acs/acs5"
baseUrl = "/".join([censusDataURL, year, dataset])
acsDatasourceFields = 'NAME,B01003_001E'
# * indicates we are using all counties
geographyVariables = {'tract': {'boundaries': 'state:50',
                                'columns': {0: 'name', 1: 'population', 2: 'stateKey', 3: 'countyKey', 4: 'censusTract'}},
                      'county subdivision': {'boundaries': 'state:50',
                                              'columns': {0: 'name', 1: 'population', 2: 'stateKey', 3: 'countyKey', 4: 'townFIPS'}},
                      'county': {'boundaries': 'state:50',
                                 'columns': {0: 'name', 1: 'population', 2: 'stateKey', 3: 'countyKey'}},
                      'statewide': {'boundaries': '',
                                    'columns': {}}
                                 }
# HERE!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# FIGURE OUT WHY THE API TOWNS DON'T MATCH THE CROSSWALK TOWNS
# Iterate through geography types
geographyDenominatorsDataframes = dict()
# Zero/null-population units are dropped from the analysis frames below, but we
# retain them here (with GEOID + crosswalk metadata) to build the Unpopulated tables.
geographyUnpopulatedDataframes = dict()
for geography in geographyVariables:
    if geography != 'statewide':
        variables = acsDatasourceFields
        boundaries = geographyVariables[geography]['boundaries']
        parameters = {'get': variables, 'for': f'{geography}:*', 'in': boundaries, 'key': apiKey}
        request = requests.get(baseUrl, params=parameters)
        geographies = request.json()

        # Rename columns, create GEOID + countyName (+ crosswalk for towns) on the FULL frame
        geographiesDataFrame = pd.DataFrame(geographies[1:])
        columns = geographyVariables[geography]['columns']
        geographiesDataFrame = geographiesDataFrame.rename(columns=columns)
        if geography == 'tract':
            geographiesDataFrame['TractGEOID'] = (geographiesDataFrame['stateKey'] +
                                                    geographiesDataFrame['countyKey'] +
                                                    geographiesDataFrame['censusTract'])
        elif geography == 'county subdivision':
            geographiesDataFrame['TownGEOID'] = (geographiesDataFrame['stateKey'] +
                                                    geographiesDataFrame['countyKey'] +
                                                    geographiesDataFrame['townFIPS'])

            # Convert the GEOID field to ensure a join with the cross-walk table (256 filter...?)
            crosswalkFields = ["TWNNMEMC", "TWNGEOID", "HSPLSANAME", "HSPLSACODE", "VDHDSTRTNME", "VDHDSTRTCDE"]
            crosswalkDataArray = arcpy.da.FeatureClassToNumPyArray(crosswalkTable, crosswalkFields)
            crosswalkDataFrame = pd.DataFrame(crosswalkDataArray, columns = crosswalkFields)
            crosswalkDataFrame.drop_duplicates(subset='TWNGEOID', inplace=True)
            geographiesDataFrame = geographiesDataFrame.merge(crosswalkDataFrame, how='left', left_on='TownGEOID', right_on='TWNGEOID')

        elif geography == 'county':
            geographiesDataFrame['CountyGEOID'] = (geographiesDataFrame['stateKey'] +
                                                    geographiesDataFrame['countyKey'])

        geographiesDataFrame['countyName'] = geographiesDataFrame['countyKey'].map(countyNamesDictionary)

        # Split: zero/null population -> Unpopulated frame; the rest -> analysis denominators
        unpopulatedMask = (pd.to_numeric(geographiesDataFrame['population'], errors='coerce').isnull() |
                           (geographiesDataFrame['population'] == '0'))
        geographyUnpopulatedDataframes[geography] = geographiesDataFrame[unpopulatedMask].copy()
        geographiesDataFrame = geographiesDataFrame[~unpopulatedMask].copy()

        geographyDenominatorsDataframes[geography] = geographiesDataFrame

# Perform Indicator calls
# The geographies are hard-coded in this dictionary
# instead of a being pulled from the geographies dictionary.
# This is for stability, but also removes some dynamism - a trade-off
indicators = {'SupportiveIncome':{'variables': {"numerators": ["C17002_008E"],
                                                "denominators": ["C17002_001E"]},
                                  'columnsForStats': {
                                                      'tract': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'Census Tract'},
                                                      'county subdivision': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'townFIPS'},
                                                      'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                                      'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'WithoutPublicAssistance':{'variables': {"numerators": ["B19058_002E"],
                                                       "denominators": ["B19058_001E"]},
                                         'columnsForStats':{
                                                            'tract': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'Census Tract'},
                                                            'county subdivision': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'townFIPS'},
                                                            'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                                            'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'Employment':{'variables': {"numerators": ["B23001_005E", "B23001_007E", "B23001_012E", "B23001_014E", "B23001_019E", "B23001_021E", "B23001_026E", "B23001_028E", "B23001_033E", "B23001_035E", "B23001_040E", "B23001_042E", "B23001_047E", "B23001_049E", "B23001_054E", "B23001_056E", "B23001_061E", "B23001_063E", "B23001_068E", "B23001_070E", "B23001_075E", "B23001_080E", "B23001_085E", "B23001_091E", "B23001_093E", "B23001_098E", "B23001_100E", "B23001_105E", "B23001_107E", "B23001_112E", "B23001_114E", "B23001_119E", "B23001_121E", "B23001_126E", "B23001_128E", "B23001_133E", "B23001_135E", "B23001_140E", "B23001_142E", "B23001_147E", "B23001_149E", "B23001_154E", "B23001_156E", "B23001_161E", "B23001_166E", "B23001_171E"],
                                          "denominators": ["B23001_004E", "B23001_011E", "B23001_018E", "B23001_025E", "B23001_032E", "B23001_039E", "B23001_046E", "B23001_053E", "B23001_060E", "B23001_067E", "B23001_074E", "B23001_079E", "B23001_084E", "B23001_090E", "B23001_097E", "B23001_104E", "B23001_111E", "B23001_118E", "B23001_125E", "B23001_132E", "B23001_139E", "B23001_146E", "B23001_153E", "B23001_160E", "B23001_165E", "B23001_170E"]},
                            'columnsForStats':{
                                               'tract': {74: 'Census Tract'},
                                               'county subdivision': {74: 'townFIPS'},
                                               'county': {73: 'County Key'},
                                               'statewide': {72: 'State Key'}}},
              'MedianHouseholdIncome': {'variables': {"numerators": ["B19013_001E"],
                                                      "denominators": [],
                                                      # Total households (B19001_001E, the universe of
                                                      # median household income). Not used to form a rate;
                                                      # only to population-weight the District/HSA area-
                                                      # frame means in _frame_weighted_value().
                                                      "weights": ["B19001_001E"]},
                                        'columnsForStats':{
                                                           'tract': {0: 'Indicator Percentage', 3: 'Census Tract'},
                                                           'county subdivision': {0: 'Indicator Percentage', 3: 'townFIPS'},
                                                           'county': {0: 'County Indicator Mean', 2: 'County Key'},
                                                           'statewide': {0: 'Statewide Indicator Mean', 1: 'State Key'}}},
              'HighSchoolEducation':{'variables': {"numerators": ["B15001_014E", "B15001_015E", "B15001_016E", "B15001_017E", "B15001_018E", "B15001_022E", "B15001_023E", "B15001_024E", "B15001_025E", "B15001_026E", "B15001_030E", "B15001_031E", "B15001_032E", "B15001_033E", "B15001_034E", "B15001_038E", "B15001_039E", "B15001_040E", "B15001_041E", "B15001_042E", "B15001_055E", "B15001_056E", "B15001_057E", "B15001_058E", "B15001_059E", "B15001_063E", "B15001_064E", "B15001_065E", "B15001_066E", "B15001_067E", "B15001_071E", "B15001_072E", "B15001_073E", "B15001_074E", "B15001_075E", "B15001_079E", "B15001_080E", "B15001_081E", "B15001_082E", "B15001_083E"],
                                                   "denominators": ["B15001_011E", "B15001_019E", "B15001_027E", "B15001_035E", "B15001_052E", "B15001_060E", "B15001_068E", "B15001_076E"]},
                                     'columnsForStats':{
                                                        'tract': {50: 'Census Tract'},
                                                        'county subdivision': {50: 'townFIPS'},
                                                        'county': {49: 'County Key'},
                                                        'statewide': {48: 'State Key'}}},
              'HealthInsurance':{'variables': {"numerators": ["B27010_003E", "B27010_010E", "B27010_019E", "B27010_026E", "B27010_035E", "B27010_042E", "B27010_052E", "B27010_058E"],
                                               "denominators": ["B27010_001E"]},
                                 'columnsForStats':{
                                                    'tract': {0: 'Indicator Denominator', 11: 'Census Tract'},
                                                    'county subdivision': {0: 'Indicator Denominator', 11: 'townFIPS'},
                                                    'county': {0: 'Indicator Denominator', 10: 'County Key'},
                                                    'statewide': {0: 'Indicator Denominator', 9: 'State Key'}}},
              'ManageableHousingCosts':{'variables': {"numerators": ["B25095_003E", "B25095_004E", "B25095_005E", "B25095_012E", "B25095_013E", "B25095_014E", "B25095_021E", "B25095_022E", "B25095_023E", "B25095_030E", "B25095_031E", "B25095_032E", "B25095_039E", "B25095_040E", "B25095_041E", "B25095_048E", "B25095_049E", "B25095_050E", "B25095_057E", "B25095_058E", "B25095_059E", "B25095_066E", "B25095_067E", "B25095_068E", "B25070_002E", "B25070_003E", "B25070_004E", "B25070_005E", "B25070_006E"],
                                                      "denominators": ["B25095_001E", "B25070_001E"]},
                                        'columnsForStats':{
                                                           'tract': {33: 'Census Tract'},
                                                           'county subdivision': {33: 'townFIPS'},
                                                           'county': {32: 'County Key'},
                                                           'statewide': {31: 'State Key'}}},
              'BroadbandInternet':{'variables': {"numerators": ["B28005_005E", "B28005_011E", "B28005_017E"],
                                                 "denominators": ["B28005_001E"]},
                                   'columnsForStats':{
                                                      'tract': {0: 'Indicator Denominator', 6: 'Census Tract'},
                                                      'county subdivision': {0: 'Indicator Denominator', 6: 'townFIPS'},
                                                      'county': {0: 'Indicator Denominator', 5: 'County Key'},
                                                      'statewide': {0: 'Indicator Denominator', 4: 'State Key'}}},
              'OwnerOccupiedHomes':{'variables': {"numerators": ["B25003_002E"],
                                                  "denominators": ["B25003_001E"]},
                                    'columnsForStats':{
                                                       'tract': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 4: 'Census Tract'},
                                                       'county subdivision': {0: 'Indicator Denominator', 1:'Indicator Numerator', 4: 'townFIPS'},
                                                       'county': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 3: 'County Key'},
                                                       'statewide': {0: 'Indicator Denominator', 1: 'Indicator Numerator', 2: 'State Key'}}},
              'EnglishProficiency':{'variables': {"numerators": ["B16005_003E", "B16005_005E", "B16005_006E", "B16005_010E", "B16005_011E", "B16005_015E", "B16005_016E", "B16005_020E", "B16005_021E", "B16005_025E", "B16005_027E", "B16005_028E", "B16005_032E", "B16005_033E", "B16005_037E", "B16005_038E", "B16005_042E", "B16005_043E"],
                                                  "denominators": ["B16005_001E"]},
                                    'columnsForStats':{
                                                       'tract': {0: 'Indicator Denominator', 21: 'Census Tract'},
                                                       'county subdivision': {0: 'Indicator Denominator', 21: 'townFIPS'},
                                                       'county': {0: 'Indicator Denominator', 20: 'County Key'},
                                                       'statewide': {0: 'Indicator Denominator', 19: 'State Key'}}},
              'VehicleAccess':{'variables': {"numerators": ["B25044_004E", "B25044_005E", "B25044_006E", "B25044_007E", "B25044_008E", "B25044_011E", "B25044_012E", "B25044_013E", "B25044_014E", "B25044_015E"],
                                             "denominators": ["B25044_001E"]},
                               'columnsForStats':{
                                                  'tract': {0: 'Indicator Denominator', 13: 'Census Tract'},
                                                  'county subdivision': {0: 'Indicator Denominator', 13: 'townFIPS'},
                                                  'county': {0: 'Indicator Denominator', 12: 'County Key'},
                                                  'statewide': {0: 'Indicator Denominator', 11: 'State Key'}}},
              }

# Loop through each geography for each indicator,
# retrieve census api data, and store results in
# separate data frames.

# FOR NUMERATORS AND DENOMINATORS, IF len(DENOMINATOR) == 0, IT IS A COUNT, NOT A RATE
# HERE ================
# We added the Statewide dataset, and now need to address the num and denom...
geographyDataFrameGeographyFieldIndex = {'tract': 3,
                                   'county subdivision': 3,
                                   'county': 2,
                                   'statewide': 1}

geographyDataframes = dict()

for indicator in indicators:

    indicatorNumeratorVariables = indicators[indicator]['variables']['numerators']
    indicatorDenominatorVariables = indicators[indicator]['variables']['denominators']
    # Optional per-unit weight (e.g. town household count for median household income).
    indicatorWeightVariables = indicators[indicator]['variables'].get('weights', [])

    variablesStringNumerator = ','.join(indicatorNumeratorVariables)
    variablesStringDenominator = ','.join(indicatorDenominatorVariables)
    variablesStringWeight = ','.join(indicatorWeightVariables)

    geographyDataframes[indicator] = dict()

    for geography in geographyVariables:
        # tract, county subdivision, county, statewide

        if len(indicators[indicator]['columnsForStats'][geography]) == 0:
            pass
        else:

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

            indicatorResponseNumerator = requests.get(urlNumerator)
            indicatorResponseNumeratorJSON = indicatorResponseNumerator.json()
            indicatorDataFrameNumerator = pd.DataFrame.from_dict(indicatorResponseNumeratorJSON[1:])
            if len(indicatorDenominatorVariables) != 0:
                indicatorResponseDenominator = requests.get(urlDenominator)
                indicatorResponseDenominatorJSON = indicatorResponseDenominator.json()
                indicatorDataFrameDenominator = pd.DataFrame.from_dict(indicatorResponseDenominatorJSON[1:])

            # Sum all indicators for numerator then denominator minus geography fields
            geographyFieldIndex = geographyDataFrameGeographyFieldIndex[geography]
            # Create primary key for joining numerator and denominator
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

            # Optional per-unit WEIGHT (e.g. town household count for median household
            # income). Merged as a 'Weight' column and used ONLY to population-weight
            # the District/HSA area-frame means (_frame_weighted_value); it never forms
            # a rate, so median income stays on the count path.
            if len(indicatorWeightVariables) != 0:
                indicatorResponseWeightJSON = requests.get(urlWeight).json()
                indicatorDataFrameWeight = pd.DataFrame.from_dict(indicatorResponseWeightJSON[1:])
                columnsTojoin = indicatorDataFrameWeight.columns[-geographyFieldIndex:]
                indicatorDataFrameWeight['JoinKey'] = indicatorDataFrameWeight[columnsTojoin].astype(str).apply(''.join, axis=1)
                indicatorDataFrameWeight.drop(columnsTojoin, axis=1, inplace=True)
                indicatorDataFrameWeight.iloc[:, :-1] = indicatorDataFrameWeight.iloc[:, :-1].apply(pd.to_numeric, errors='coerce')
                indicatorDataFrameWeight.loc[:, 'Weight'] = indicatorDataFrameWeight.iloc[:, :-1].sum(axis=1)
                indicatorDataFrameWeight = indicatorDataFrameWeight[['JoinKey', 'Weight']]
                baseIndicatorDataFrame = baseIndicatorDataFrame.merge(indicatorDataFrameWeight, on='JoinKey', how='left')

            geographyDataframes[indicator][geography] = baseIndicatorDataFrame

# Loop through each geography for each indicator, calculate summary stats,
# and produce indicies and metadata.

# Prep crosswalk table for joining
crosswalkTableSelection = "crosswalkTableSelection"
clause = "TYPEMUNIC = 'Town' Or TYPEMUNIC = 'City'"
# Table views are session-scoped: a prior run (or a prior crash) in the same ArcGIS
# Python session leaves this view registered, so recreate it idempotently.
if arcpy.Exists(crosswalkTableSelection):
    arcpy.management.Delete(crosswalkTableSelection)
arcpy.management.MakeTableView(crosswalkTable, crosswalkTableSelection, clause)

for indicator in geographyDataframes:
    for geography in geographyDataframes[indicator]:
        if geography == 'county subdivision': # This means municipality
            currentDataFrame = geographyDataframes[indicator][geography]
            # Add groupby fields and rank fields for town-related geographies
            # Note that as of March 2024 Essex Jct is not in the Census api
            # Convert the crosswalk table to a DataFrame
            #fields = ['TOWNGEOID_256', 'HSA4_NAME', 'DISTRICT', 'CNTYNAME']
            fields = ['TWNGEOID', 'HLTHSANAME', 'VDHDSTRTNME', 'CNTYNAME']
            crosswalkListOfTuples = list()
            with arcpy.da.SearchCursor(crosswalkTableSelection, fields) as searchCursor:
                for row in searchCursor:
                    crosswalkListOfTuples.append(row)
            crosswalkDataFrame = pd.DataFrame(crosswalkListOfTuples, columns=fields)
            # Guard: the crosswalk must be one row per municipality. A duplicated
            # TWNGEOID would multiply town rows in the inner merge below, injecting
            # byte-identical rates into a county/district/HSA group and collapsing
            # its standard deviation to exactly 0 (and corrupting means/ranks).
            _dupKeys = crosswalkDataFrame['TWNGEOID'][crosswalkDataFrame['TWNGEOID'].duplicated(keep=False)].unique()
            if len(_dupKeys):
                print(f"[CRI WARNING] crosswalk has {len(_dupKeys)} duplicated TWNGEOID value(s) "
                      f"-> town rows will be multiplied in the merge. Keys: {list(_dupKeys)[:20]}")
            currentDataFrame = pd.merge(currentDataFrame, crosswalkDataFrame, left_on='JoinKey',
                                                      right_on='TWNGEOID', how='inner')
            currentDataFrame.drop('TWNGEOID', axis=1, inplace=True)
            _dupTowns = currentDataFrame['JoinKey'][currentDataFrame['JoinKey'].duplicated(keep=False)].unique()
            if len(_dupTowns):
                print(f"[CRI WARNING] {indicator}/{geography}: {len(_dupTowns)} town(s) duplicated "
                      f"after crosswalk merge -> stats will be wrong. Keys: {list(_dupTowns)[:20]}")

        else:
            currentDataFrame = geographyDataframes[indicator][geography]
            currentDataFrame['CNTYCODE'] = currentDataFrame['JoinKey'].str[2:5]
            currentDataFrame['CNTYNAME'] = currentDataFrame['CNTYCODE'].map(countyNamesDictionary)

        if 'Denominator' in currentDataFrame.columns:
            # Calculate Rates
            #currentDataFrame['PercentPerCapita'] = currentDataFrame['Numerator'].div(currentDataFrame['Denominator'].where(currentDataFrame['Denominator'] != 0)) * 100
            currentDataFrame['PercentPerCapita'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].div(currentDataFrame['Denominator'].where(currentDataFrame['Denominator'] != 0)) * 100
            # Calculate Means
            currentDataFrame['MeanPercentPerCapita'] = currentDataFrame['PercentPerCapita'].mean()
            # Calculate Percent Ranks
            currentDataFrame['PercentPerCapitaRank'] = currentDataFrame['PercentPerCapita'].rank(pct=True)*100
            # Calculate Standard Deviation of Percent Per Capita
            currentDataFrame['StdDevPercentPerCapita'] = currentDataFrame['PercentPerCapita'].std()

            if geography == 'county subdivision': # This means municipality
                # Calculate grouby percent mean, percent rank, and percent stddev values for county, VDH District, and Hospital Service Area
                currentDataFrame['PercentPerCapitaCountyRank'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].rank(pct=True)*100
                currentDataFrame['PercentPerCapitaCountySD'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].transform('std')
                currentDataFrame['PercentPerCapitaCountyMean'] = currentDataFrame.groupby('CNTYNAME')['PercentPerCapita'].transform('mean')
                currentDataFrame['PercentPerCapitaDistrictRank'] = currentDataFrame.groupby('VDHDSTRTNME')['PercentPerCapita'].rank(pct=True)*100
                currentDataFrame['PercentPerCapitaDistrictSD'] = currentDataFrame.groupby('VDHDSTRTNME')['PercentPerCapita'].transform('std')
                # Area-frame mean = population-weighted sum(num)/sum(denom), NOT an
                # unweighted mean of town rates (see _frame_weighted_rate).
                currentDataFrame['PercentPerCapitaDistrictMean'] = _frame_weighted_rate(currentDataFrame, 'VDHDSTRTNME')
                currentDataFrame['PercentPerCapitaHSARank'] = currentDataFrame.groupby('HLTHSANAME')['PercentPerCapita'].rank(pct=True)*100
                currentDataFrame['PercentPerCapitaHSASD'] = currentDataFrame.groupby('HLTHSANAME')['PercentPerCapita'].transform('std')
                currentDataFrame['PercentPerCapitaHSAMean'] = _frame_weighted_rate(currentDataFrame, 'HLTHSANAME')
            elif geography in ['tract']:
                # Calculate grouby percent mean, percent rank, and percent stddev values for county
                currentDataFrame['PercentPerCapitaCountyRank'] = currentDataFrame.groupby('CNTYCODE')['PercentPerCapita'].rank(pct=True)*100
                currentDataFrame['PercentPerCapitaCountySD'] = currentDataFrame.groupby('CNTYCODE')['PercentPerCapita'].transform('std')
                currentDataFrame['PercentPerCapitaCountyMean'] = currentDataFrame.groupby('CNTYCODE')['PercentPerCapita'].transform('mean')
            else:
                # Calculate grouby percent rank values for county code
                currentDataFrame['PercentPerCapitaCountyRank'] = currentDataFrame.groupby('CNTYCODE')['PercentPerCapita'].rank(pct=True)*100
        else:
            if geography == 'county subdivision':
                # Calculate the Mean, Std, and Rank values for those
                # Indicator/geography combinations that do not have denominators
                # Note that these will not be Percent Per Capita values.
                # They will be just the mean, standard deviation, and rank of the counts
                # Calculate count stats
                currentDataFrame['MeanIndicatorCount'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].mean()
                currentDataFrame['IndicatorRank'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].rank(pct=True)*100
                currentDataFrame['StdDevIndicator'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].std()
                currentDataFrame['IndicatorCountyRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].rank(pct=True) * 100)
                currentDataFrame['IndicatorCountySD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].transform('std'))
                currentDataFrame['IndicatorCountyMean'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYNAME')['Numerator'].transform('mean'))
                currentDataFrame['IndicatorDistrictRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('VDHDSTRTNME')['Numerator'].rank(pct=True)*100)
                currentDataFrame['IndicatorDistrictSD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('VDHDSTRTNME')['Numerator'].transform('std'))
                # Area-frame mean = household-count-weighted mean of town values (e.g.
                # median income weighted by households), NOT a plain mean of town
                # values (see _frame_weighted_value; falls back to a plain mean if no
                # 'Weight' column was fetched for this indicator).
                currentDataFrame['IndicatorDistrictMean'] = _frame_weighted_value(currentDataFrame, 'VDHDSTRTNME')
                currentDataFrame['IndicatorHSARank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('HLTHSANAME')['Numerator'].rank(pct=True)*100)
                currentDataFrame['IndicatorHSASD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('HLTHSANAME')['Numerator'].transform('std'))
                currentDataFrame['IndicatorHSAMean'] = _frame_weighted_value(currentDataFrame, 'HLTHSANAME')
            else:
                currentDataFrame['MeanIndicatorCount'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].mean()
                currentDataFrame['IndicatorRank'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].rank(pct=True)*100
                currentDataFrame['StdDevIndicator'] = currentDataFrame.loc[currentDataFrame['Numerator'] >= 0, 'Numerator'].std()
                currentDataFrame['IndicatorCountyRank'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].rank(pct=True) * 100)
                currentDataFrame['IndicatorCountySD'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].transform('std'))
                currentDataFrame['IndicatorCountyMean'] = (currentDataFrame.loc[currentDataFrame['Numerator'] >= 0].groupby('CNTYCODE')['Numerator'].transform('mean'))

        # Overwrite the dataframe stored in the dictionary with the copy that was updated above
        geographyDataframes[indicator][geography] = currentDataFrame

# -----------------------------------------------------------------------------
# Area-frame normalized indicator means  (CommunityResilienceMethodology_updated,
# "Standard Mean Difference > Step 1 - Area Frame Indicator Means"):
#   * statewide & county frames -> the indicator AGGREGATED directly from ACS at
#     that frame (the statewide / county API calls) -- a population-weighted value,
#     NOT the unweighted mean of tract/municipality rates.
#   * district & hospital frames -> a POPULATION-WEIGHTED aggregate of the member
#     municipalities (already computed above as PercentPerCapita{District,HSA}Mean /
#     Indicator{District,HSA}Mean), since ACS has no district/HSA geography. Rates use
#     sum(numerator)/sum(denominator); median household income uses a household-count-
#     weighted mean. This matches the statewide/county semantics -- a small town no
#     longer counts the same as a large one (see _frame_weighted_rate/_value).
# These area-frame means are the reference the Standard Mean Difference is measured
# against, and are also the "Indicator Mean" columns in the indicator tables and the
# "Total <frame> Mean" columns in the disaggregated-demographics tables.
# -----------------------------------------------------------------------------
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
    for geography in ['tract', 'county subdivision']:
        df = geographyDataframes[indicator].get(geography)
        if df is None:
            continue
        # county code is positions 2:5 of the GEOID (JoinKey) for both tracts and towns
        df['AreaFrameMeanStatewide'] = statewideAggregate
        df['AreaFrameMeanCounty'] = df['JoinKey'].astype(str).str[2:5].map(countyAggregateByCode)
        geographyDataframes[indicator][geography] = df

# -----------------------------------------------------------------------------
# DIAGNOSTIC: a standard deviation of exactly 0 within an area frame means every
# member unit shares one byte-identical value -- implausible in real ACS data and
# a signal that the frame was constructed wrong (e.g. a group that collapsed to a
# single town duplicated N times). Report every such group with its member values
# BEFORE the SMD loop, so a live run pinpoints the construction fault instead of
# the symptom crashing the division.
# -----------------------------------------------------------------------------
_SD_GROUP_COLS = {  # SD column -> (group-key column, value column)
    'PercentPerCapitaCountySD':    ('CNTYNAME',     'PercentPerCapita'),
    'PercentPerCapitaDistrictSD':  ('VDHDSTRTNME',  'PercentPerCapita'),
    'PercentPerCapitaHSASD':       ('HLTHSANAME',   'PercentPerCapita'),
    'IndicatorCountySD':           ('CNTYNAME',     'Numerator'),
    'IndicatorDistrictSD':         ('VDHDSTRTNME',  'Numerator'),
    'IndicatorHSASD':              ('HLTHSANAME',   'Numerator'),
}
for indicator in geographyDataframes:
    for geography in ['tract', 'county subdivision']:
        df = geographyDataframes[indicator].get(geography)
        if df is None:
            continue
        for sdCol, (keyCol, valCol) in _SD_GROUP_COLS.items():
            if sdCol not in df.columns or keyCol not in df.columns:
                continue  # e.g. tract frames have no district/HSA grouping
            sd = pd.to_numeric(df[sdCol], errors='coerce')
            zeroGroups = df.loc[sd == 0, keyCol].dropna().unique()
            for g in zeroGroups:
                members = df.loc[df[keyCol] == g, valCol]
                vals = sorted(pd.to_numeric(members, errors='coerce').dropna().round(4).unique().tolist())
                print(f"[CRI ZERO-SD] {indicator} / {geography} / {sdCol} -> group '{g}': "
                      f"{len(members)} member rows, {len(vals)} distinct value(s) {vals[:10]}")

# Calculate Standard Mean Difference
def _smd(mean_col, value_col, std_col):
    """Standard Mean Difference = (area-frame mean - unit value) / frame std * -1.

    Zero-spread frames: when the frame std is exactly 0 (every member unit shares one
    value, e.g. a saturating indicator like EnglishProficiency at 100% across a
    county), every unit equals the frame mean, so the difference is also 0. This is a
    true "no disparity" case, not missing data -> report 0.0 (project decision).

    Undefined-spread frames: when the std is NaN (a single-member frame, so spread
    can't be computed) the SMD is left blank (NaN) -- that is insufficient data, a
    different thing from zero disparity.

    A nonzero std divides normally. (A plain 0 divisor would raise ZeroDivisionError
    under numexpr; it is handled explicitly below rather than reaching the division.)"""
    std = pd.to_numeric(std_col, errors='coerce')
    result = ((mean_col - value_col) / std.replace(0, np.nan)) * -1
    # std == 0 -> genuine zero spread -> 0 disparity (NaN std stays NaN: std==0 is
    # False for NaN, so those rows keep the computed NaN).
    return result.mask(std == 0, 0.0)

for indicator in geographyDataframes:
    for geography in geographyDataframes[indicator]:
        if geography in ['tract', 'county subdivision']:
            currentDataFrame = geographyDataframes[indicator][geography]
            # Area-frame MEAN terms (statewide/county = ACS aggregate; district/HSA =
            # municipality average); STD terms are the spread of unit values in the
            # frame (methodology Step 3), left as computed above.
            if 'Denominator' in currentDataFrame.columns:
                # Statewide SMD
                currentDataFrame[f'{geography.title()}StatewideSMD'] = _smd(currentDataFrame['AreaFrameMeanStatewide'], currentDataFrame['PercentPerCapita'], currentDataFrame['StdDevPercentPerCapita'])
                # County SMD
                currentDataFrame[f'{geography.title()}CountySMD'] = _smd(currentDataFrame['AreaFrameMeanCounty'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaCountySD'])
                if geography == 'county subdivision':
                    # District SMD
                    currentDataFrame[f'{geography.title()}DistrictSMD'] = _smd(currentDataFrame['PercentPerCapitaDistrictMean'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaDistrictSD'])
                    # HSA SMD
                    currentDataFrame[f'{geography.title()}HSASMD'] = _smd(currentDataFrame['PercentPerCapitaHSAMean'], currentDataFrame['PercentPerCapita'], currentDataFrame['PercentPerCapitaHSASD'])
            else:
                # Statewide SMD
                currentDataFrame[f'{geography.title()}StatewideSMD'] = _smd(currentDataFrame['AreaFrameMeanStatewide'], currentDataFrame['Numerator'], currentDataFrame['StdDevIndicator'])
                # County SMD
                currentDataFrame[f'{geography.title()}CountySMD'] = _smd(currentDataFrame['AreaFrameMeanCounty'], currentDataFrame['Numerator'], currentDataFrame['IndicatorCountySD'])
                if geography == 'county subdivision':
                    # District SMD
                    currentDataFrame[f'{geography.title()}DistrictSMD'] = _smd(currentDataFrame['IndicatorDistrictMean'], currentDataFrame['Numerator'], currentDataFrame['IndicatorDistrictSD'])
                    # HSA SMD
                    currentDataFrame[f'{geography.title()}HSASMD'] = _smd(currentDataFrame['IndicatorHSAMean'], currentDataFrame['Numerator'], currentDataFrame['IndicatorHSASD'])
        else:
            pass
            # Statewide calcs are skipped because they are created at each geography


# Initialize a list to hold data for our new DataFrame
thematic_data_list = []

# Define themes and their associated indicators
themes = {
    'Overall': list(geographyDataframes.keys()), # 'Overall' includes all indicators
    'Socioeconomic': ['SupportiveIncome', 'WithoutPublicAssistance', 'Employment', 'MedianHouseholdIncome', 'HighSchoolEducation', 'HealthInsurance', 'ManageableHousingCosts'],
    'Housing': ['BroadbandInternet', 'OwnerOccupiedHomes', 'EnglishProficiency', 'VehicleAccess']
}

# Define the different ranking and SMD contexts we want to analyze
contexts = {
    'Statewide': {'rank': 'PercentPerCapitaRank', 'smd': 'StatewideSMD'},
    'County':    {'rank': 'PercentPerCapitaCountyRank', 'smd': 'CountySMD'},
    'District':  {'rank': 'PercentPerCapitaDistrictRank', 'smd': 'DistrictSMD'},
    'HSA':       {'rank': 'PercentPerCapitaHSARank', 'smd': 'HSASMD'}
}

# Define special handling for 'count' based indicators like MedianHouseholdIncome
count_contexts = {
    'Statewide': {'rank': 'IndicatorRank', 'smd': 'StatewideSMD'},
    'County':    {'rank': 'IndicatorCountyRank', 'smd': 'CountySMD'},
    'District':  {'rank': 'IndicatorDistrictRank', 'smd': 'DistrictSMD'},
    'HSA':       {'rank': 'IndicatorHSARank', 'smd': 'HSASMD'}
}

# --- Main Loop ---
for theme_name, indicator_list in themes.items():
    for geography in ['tract', 'county subdivision']:
        
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

        # Calculate final scores for each unit
        for geo_unit_id, data in unit_metrics.items():
            thematic_row = {
                'Theme': theme_name,
                'Geography': geography,
                'GeographyUnit': geo_unit_id,
                'CNTYNAME': data['info']['CNTYNAME'],
                'VDHDSTRTNME': data['info']['VDHDSTRTNME'],
                'HLTHSANAME': data['info']['HLTHSANAME'],
                
                # Use the new, unique function name 'scipygmean'
                'ThematicGMean_Statewide': scipygmean(data['Ranks_Statewide']),
                'ThematicGMean_County':    scipygmean(data['Ranks_County']),
                'ThematicGMean_District':  scipygmean(data['Ranks_District']),
                'ThematicGMean_HSA':       scipygmean(data['Ranks_HSA']),
            }
            thematic_data_list.append(thematic_row)

# Create the final DataFrame
thematicDataFrame = pd.DataFrame(thematic_data_list)

# Final step: Rank the thematic scores to get a final index from 0-100
for context in ['Statewide', 'County', 'District', 'HSA']:
    rank_col = f'ThematicGMean_{context}'
    index_col = f'ThematicIndex_{context}'
    if rank_col in thematicDataFrame.columns:
        # Filter out rows where the rank is NaN before applying the groupby().rank()
        # to avoid calculation errors and ensure correct percentile ranking.
        valid_ranks = thematicDataFrame[thematicDataFrame[rank_col].notna()].copy()
        
        if context == 'Statewide':
            ranked_series = valid_ranks.groupby(['Theme', 'Geography'])[rank_col].rank(pct=True) * 100
        elif context == 'County':
            ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'CNTYNAME'])[rank_col].rank(pct=True) * 100
        elif context == 'District':
            ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'VDHDSTRTNME'])[rank_col].rank(pct=True) * 100
        elif context == 'HSA':
            ranked_series = valid_ranks.groupby(['Theme', 'Geography', 'HLTHSANAME'])[rank_col].rank(pct=True) * 100
            
        # Merge the results back into the main DataFrame
        thematicDataFrame = thematicDataFrame.merge(ranked_series.rename(index_col), left_index=True, right_index=True, how='left')

# Intermediate/debug dump of the thematic index (kept out of the deliverable dir).
thematicDataFrame.to_csv(os.path.join(SCRATCH_DIR, 'thematicDataFrame.csv'), index=False)


# =============================================================================
# BUILD PUBLISHED INDEX TABLES  (tractsIndexGeometric / townsIndexGeometric)
# -----------------------------------------------------------------------------
# Path A: wire the published index deliverables to the LIVE data model
#   - thematicDataFrame            -> per-theme index scores (ThematicIndex_*)
#   - geographyDataframes          -> per-indicator percentile ranks
#   - geographyDenominatorsDataframes -> Name / Population / metadata
# This REPLACES the orphaned tractsPercentile / townsPercentile index code that
# follows below (which depends on per-indicator DataFrames that no longer exist).
#
# VALIDATION REQUIRED: this block has not been run against live ACS/arcpy here.
# Before publishing, diff its output columns/values against a prior published
# period (e.g. data/output/2018-2022/*IndexGeometric*).
# -----------------------------------------------------------------------------

# indicator key -> friendly output-column prefix used in the published schema
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

# published theme labels + stable sort order for the application
THEME_LABEL = {'Overall': 'Overall Theme', 'Socioeconomic': 'Socioeconomic Theme', 'Housing': 'Housing Theme'}
THEME_SORT = {'Overall Theme': 1, 'Socioeconomic Theme': 2, 'Housing Theme': 3}

# context -> (rate-indicator rank col, count-indicator rank col) in geographyDataframes
RANK_COLS = {
    'Statewide': ('PercentPerCapitaRank',         'IndicatorRank'),
    'County':    ('PercentPerCapitaCountyRank',   'IndicatorCountyRank'),
    'District':  ('PercentPerCapitaDistrictRank', 'IndicatorDistrictRank'),
    'HSA':       ('PercentPerCapitaHSARank',      'IndicatorHSARank'),
}
# context -> label used in the percentile COLUMN names (note HSA -> "Hospital")
PCT_CTX_LABEL = {'Statewide': 'Statewide', 'County': 'County', 'District': 'District', 'HSA': 'Hospital'}
# context -> index SCORE column name
INDEX_SCORE_LABEL = {'Statewide': 'Statewide Index Score', 'County': 'County Index Score',
                     'District': 'District Index Score', 'HSA': 'Hospital Index Score'}


def _county_with_suffix(value):
    """Prior outputs store county as 'Addison County'; county name dict yields 'Addison'."""
    if pd.isna(value):
        return value
    text = str(value).strip()
    return text if text.endswith('County') else f'{text} County'


def build_indicator_percentile_wide(geography, contexts):
    """One row per unit (JoinKey); columns '<prefix><CtxLabel>Percentile' for each indicator x context."""
    wide = None
    for indicator, prefix in INDICATOR_PREFIX.items():
        df = geographyDataframes.get(indicator, {}).get(geography)
        if df is None:
            continue
        is_count = 'IndicatorRank' in df.columns
        rename = {}
        for ctx in contexts:
            rate_col, count_col = RANK_COLS[ctx]
            src = count_col if is_count else rate_col
            if src in df.columns:
                rename[src] = f'{prefix}{PCT_CTX_LABEL[ctx]}Percentile'
        take = df[['JoinKey'] + list(rename.keys())].copy().rename(columns=rename)
        for col in rename.values():
            take[col] = pd.to_numeric(take[col], errors='coerce').round(2)
        wide = take if wide is None else wide.merge(take, on='JoinKey', how='outer')
    return wide


# geography -> build spec
INDEX_SPECS = {
    'tract': {
        'contexts': ['Statewide', 'County'],
        'stem': 'tractsIndexGeometric',
        'id_cols': ['Index Theme', 'Census Tract', 'County', 'Tract GEOID', 'Data Years', 'Tract Population'],
    },
    'county subdivision': {
        'contexts': ['Statewide', 'County', 'District', 'HSA'],
        'stem': 'townsIndexGeometric',
        'id_cols': ['Index Theme', 'Town Name', 'County', 'VDH District', 'Hospital Service Area',
                    'Town GEOID', 'Data Years', 'Town Population'],
    },
}

for geography, spec in INDEX_SPECS.items():
    contexts = spec['contexts']

    # 1) theme index scores from thematicDataFrame
    tdf = thematicDataFrame[thematicDataFrame['Geography'] == geography].copy()
    tdf['Index Theme'] = tdf['Theme'].map(THEME_LABEL)
    score_renames = {f'ThematicIndex_{ctx}': INDEX_SCORE_LABEL[ctx] for ctx in contexts
                     if f'ThematicIndex_{ctx}' in tdf.columns}
    tdf = tdf.rename(columns=score_renames)
    for col in score_renames.values():
        tdf[col] = pd.to_numeric(tdf[col], errors='coerce').round(2)

    # 2) per-indicator percentile columns
    wide = build_indicator_percentile_wide(geography, contexts)

    # 3) Name / Population / geography metadata from the denominators frame
    denom = geographyDenominatorsDataframes[geography].copy()
    if geography == 'tract':
        meta = denom[['TractGEOID', 'censusTract', 'countyName', 'population']].rename(
            columns={'TractGEOID': 'GeographyUnit', 'censusTract': 'Census Tract',
                     'countyName': 'County', 'population': 'Tract Population'})
        geoid_out = 'Tract GEOID'
    else:
        meta = denom[['TownGEOID', 'TWNNMEMC', 'countyName', 'VDHDSTRTNME', 'HSPLSANAME', 'population']].rename(
            columns={'TownGEOID': 'GeographyUnit', 'TWNNMEMC': 'Town Name', 'countyName': 'County',
                     'VDHDSTRTNME': 'VDH District', 'HSPLSANAME': 'Hospital Service Area',
                     'population': 'Town Population'})
        geoid_out = 'Town GEOID'
    meta['County'] = meta['County'].map(_county_with_suffix)

    merged = tdf.merge(meta, on='GeographyUnit', how='left')
    if wide is not None:
        merged = merged.merge(wide, left_on='GeographyUnit', right_on='JoinKey', how='left')
    merged[geoid_out] = merged['GeographyUnit']
    merged['Data Years'] = DATA_YEARS
    merged['Sort Index Theme'] = merged['Index Theme'].map(THEME_SORT).fillna(0).astype(int)

    pct_cols = [f'{prefix}{PCT_CTX_LABEL[ctx]}Percentile'
                for prefix in INDICATOR_PREFIX.values() for ctx in contexts]
    ordered = spec['id_cols'] + [INDEX_SCORE_LABEL[c] for c in contexts] + pct_cols + ['Sort Index Theme']
    out = merged.reindex(columns=ordered)
    out = out.sort_values(['Sort Index Theme', 'County', spec['id_cols'][1]]).reset_index(drop=True)
    out.to_excel(output_path(spec['stem']), index=False)
    print(f"Wrote {output_path(spec['stem'])}  ({out.shape[0]} rows, {out.shape[1]} cols)")


# =============================================================================
# BUILD PUBLISHED INDICATOR TABLES  (tractsIndicators / townsIndicators)
# -----------------------------------------------------------------------------
# Stacked long format. Each indicator is emitted twice: once under "Overall Theme"
# and once under its own theme (Socioeconomic/Housing), matching the prior schema.
# Wired to geographyDataframes (value/mean/percentile/SMD) + denominators (name/pop).
# VALIDATION REQUIRED before publishing (diff vs data/output/2018-2022/*Indicators*).

# indicator key -> published display name  (tract & town 2018-2022 use the same names)
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
INDICATOR_THEME = {k: ('Socioeconomic Theme' if k in
                       ('SupportiveIncome', 'WithoutPublicAssistance', 'Employment', 'MedianHouseholdIncome',
                        'HighSchoolEducation', 'HealthInsurance', 'ManageableHousingCosts')
                       else 'Housing Theme')
                   for k in INDICATOR_DISPLAY}

# per-context source columns in geographyDataframes: (rate-indicator col, count-indicator col)
IND_VALUE = ('PercentPerCapita', 'Numerator')
# Area-frame indicator means (methodology Step 1): statewide & county use the ACS
# aggregate for the frame; district & HSA use the municipality average.
IND_MEAN = {'Statewide': ('AreaFrameMeanStatewide',      'AreaFrameMeanStatewide'),
            'County':    ('AreaFrameMeanCounty',         'AreaFrameMeanCounty'),
            'District':  ('PercentPerCapitaDistrictMean', 'IndicatorDistrictMean'),
            'HSA':       ('PercentPerCapitaHSAMean',      'IndicatorHSAMean')}
# IND_PCTL reuses RANK_COLS defined above


def _numcol(df, name):
    """Return df[name] as numeric, or an all-NaN series if the column is absent."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors='coerce')
    return pd.Series(np.nan, index=df.index)


def build_indicator_stack(geography, contexts, geo_title):
    """Generic stacked indicator frame with _value/_mean_<ctx>/_pctl_<ctx>/_smd_<ctx>,
    Indicator, Index Theme (Overall + own), Sort Index Theme, JoinKey."""
    frames = []
    for indicator in INDICATOR_DISPLAY:
        df = geographyDataframes.get(indicator, {}).get(geography)
        if df is None:
            continue
        is_count = 'IndicatorRank' in df.columns
        base = pd.DataFrame({'JoinKey': df['JoinKey'].values})
        base['Indicator'] = INDICATOR_DISPLAY[indicator]
        base['_theme'] = INDICATOR_THEME[indicator]
        v_rate, v_count = IND_VALUE
        base['_value'] = _numcol(df, v_count if is_count else v_rate).round(2).values
        for ctx in contexts:
            m_rate, m_count = IND_MEAN[ctx]
            p_rate, p_count = RANK_COLS[ctx]
            base[f'_mean_{ctx}'] = _numcol(df, m_count if is_count else m_rate).round(2).values
            base[f'_pctl_{ctx}'] = _numcol(df, p_count if is_count else p_rate).round(2).values
            base[f'_smd_{ctx}'] = _numcol(df, f'{geo_title}{ctx}SMD').round(2).values
        frames.append(base)
    stacked = pd.concat(frames, ignore_index=True)
    overall = stacked.copy(); overall['Index Theme'] = 'Overall Theme'
    own = stacked.copy();     own['Index Theme'] = own['_theme']
    full = pd.concat([overall, own], ignore_index=True)
    full['Sort Index Theme'] = full['Index Theme'].map(THEME_SORT).fillna(0).astype(int)
    full['Data Years'] = DATA_YEARS
    return full


# ---- Tract indicator table ----
tdenom = geographyDenominatorsDataframes['tract'][['TractGEOID', 'censusTract', 'countyName', 'population']].rename(
    columns={'TractGEOID': 'JoinKey', 'censusTract': 'Census Tract', 'countyName': 'County', 'population': 'Tract Population'})
tdenom['County'] = tdenom['County'].map(_county_with_suffix)
tractsIndicators = build_indicator_stack('tract', ['Statewide', 'County'], 'Tract').merge(tdenom, on='JoinKey', how='left')
tractsIndicators = tractsIndicators.rename(columns={
    'JoinKey': 'Tract GEOID',
    '_mean_Statewide': 'Statewide Indicator Mean', '_mean_County': 'County Indicator Mean',
    '_value': 'Tract Indicator Percentage',
    '_pctl_Statewide': 'Tract Statewide Percentile', '_pctl_County': 'Tract County Percentile',
    '_smd_Statewide': 'Tract Statewide Standard Mean Difference', '_smd_County': 'Tract County Standard Mean Difference'})
tractsIndicators = tractsIndicators.reindex(columns=[
    'Indicator', 'Index Theme', 'County', 'Statewide Indicator Mean', 'County Indicator Mean', 'Census Tract',
    'Tract GEOID', 'Data Years', 'Tract Population', 'Tract Indicator Percentage', 'Tract Statewide Percentile',
    'Tract County Percentile', 'Tract Statewide Standard Mean Difference', 'Tract County Standard Mean Difference',
    'Sort Index Theme']).sort_values(['Sort Index Theme', 'Indicator', 'County']).reset_index(drop=True)
tractsIndicators.to_excel(output_path('tractsIndicators'), index=False)
print(f"Wrote {output_path('tractsIndicators')}  ({tractsIndicators.shape[0]} rows, {tractsIndicators.shape[1]} cols)")

# ---- Town indicator table ----
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
    'Indicator', 'Index Theme', 'County', 'VDH District', 'Hospital Service Area', 'Statewide Indicator Mean',
    'County Indicator Mean', 'District Indicator Mean', 'Hospital Indicator Mean', 'Town Name', 'Town GEOID',
    'Data Years', 'Town Population', 'Town Indicator Percentage', 'Town Statewide Percentile', 'Town County Percentile',
    'Town District Percentile', 'Town Hospital Percentile', 'Town Statewide Standard Mean Difference',
    'Town County Standard Mean Difference', 'Town District Standard Mean Difference',
    'Town Hospital Standard Mean Difference', 'Sort Index Theme']).sort_values(
    ['Sort Index Theme', 'Indicator', 'County', 'Town Name']).reset_index(drop=True)
townsIndicators.to_excel(output_path('townsIndicators'), index=False)
print(f"Wrote {output_path('townsIndicators')}  ({townsIndicators.shape[0]} rows, {townsIndicators.shape[1]} cols)")


# =============================================================================
# BUILD UNPOPULATED TABLES  (tractsUnpopulated / townsUnpopulated)
# -----------------------------------------------------------------------------
# Units with zero/null population, captured in the first loop before they were
# dropped from the analysis frames.
tractsUnpopulated = geographyUnpopulatedDataframes['tract'][['censusTract', 'countyName', 'TractGEOID', 'population']].copy()
tractsUnpopulated['countyName'] = tractsUnpopulated['countyName'].map(_county_with_suffix)
tractsUnpopulated['population'] = pd.to_numeric(tractsUnpopulated['population'], errors='coerce').fillna(0).astype(int)
tractsUnpopulated['Data Years'] = DATA_YEARS
tractsUnpopulated = tractsUnpopulated.rename(columns={
    'censusTract': 'Census Tract', 'countyName': 'County', 'TractGEOID': 'Tract GEOID',
    'population': 'Tract Population'}).reindex(columns=[
    'Census Tract', 'County', 'Tract GEOID', 'Data Years', 'Tract Population'])
tractsUnpopulated.to_excel(output_path('tractsUnpopulated'), index=False)
print(f"Wrote {output_path('tractsUnpopulated')}  ({tractsUnpopulated.shape[0]} rows)")

townsUnpopulated = geographyUnpopulatedDataframes['county subdivision'][
    ['TWNNMEMC', 'countyName', 'VDHDSTRTNME', 'HSPLSANAME', 'TownGEOID', 'population']].copy()
townsUnpopulated['countyName'] = townsUnpopulated['countyName'].map(_county_with_suffix)
townsUnpopulated['population'] = pd.to_numeric(townsUnpopulated['population'], errors='coerce').fillna(0).astype(int)
townsUnpopulated['Data Years'] = DATA_YEARS
townsUnpopulated = townsUnpopulated.rename(columns={
    'TWNNMEMC': 'Town Name', 'countyName': 'County', 'VDHDSTRTNME': 'VDH District',
    'HSPLSANAME': 'Hospital Service Area', 'TownGEOID': 'Town GEOID', 'population': 'Town Population'}).reindex(columns=[
    'Town Name', 'County', 'VDH District', 'Hospital Service Area', 'Town GEOID', 'Data Years', 'Town Population'])
townsUnpopulated.to_excel(output_path('townsUnpopulated'), index=False)
print(f"Wrote {output_path('townsUnpopulated')}  ({townsUnpopulated.shape[0]} rows)")


# =============================================================================
# DISAGGREGATED DEMOGRAPHICS  (config-driven rebuild)
# -----------------------------------------------------------------------------
# Race/ethnicity/age/sex disaggregation for 8 indicators. ACS variables are
# defined verbatim (and auditable) in config/demographics_variables.json. This
# REPLACES ~4,900 lines of per-group copy-paste and wires the area-frame means and
# the town crosswalk to the live model instead of the old (deleted) per-indicator
# aggregate DataFrames (incomeVTDataFrame, townsDataFrame, ...).
#
# Fixes vs. the prior section:
#   - restored documented small-cell suppression (demographic denominator < 25)
#   - fixed MHI/Hispanic town call (had a stray Employment numerator list appended)
#   - fixed the "Indigenous Hawaiian/ Pacific Islanders)" stray-paren label
#   - de-orphaned townsDataFrame and the per-indicator area-frame means
#
# VALIDATION REQUIRED (not run against live ACS here): diff the 4 outputs against
# data/output/2018-2022/*Disaggregated*. Confirm the "Total <frame> Mean" definition
# (mean of town-level total-population rates, consistent with townsIndicators) matches
# the prior methodology, and that HSPLSANAME hospital names match the index layer.
print("##################### Disaggregated Demographic Data #####################")

with open(os.path.join(PROJECT_ROOT, "config", "demographics_variables.json"), encoding="utf-8") as _demoFile:
    _demoCfg = json.load(_demoFile)
DEMO_SUPPRESS_MIN = _demoCfg["suppression_min_denominator"]
BIPOC_MEMBERS = set(_demoCfg["bipoc_member_labels"])
BIPOC_LABEL = "Black, Indigenous, Persons of Color (BIPOC)"

# baseline town crosswalk (replaces the old, never-defined `townsDataFrame`)
_townMeta = geographyDenominatorsDataframes['county subdivision']
townsDataFrame = pd.DataFrame({
    'Town GEOID':            _townMeta['TownGEOID'].astype(str),
    'County Key':            _townMeta['TownGEOID'].astype(str).str[2:5],
    'VDH District':          _townMeta['VDHDSTRTNME'],
    'VDH District Key':      _townMeta['VDHDSTRTCDE'],
    'Hospital Service Area': _townMeta['HSPLSANAME'],
    'Hospital Key':          _townMeta['HSPLSACODE'],
})
countyCodeToName = {k: v for k, v in countyNamesDictionary.items() if k != 'Statewide'}


def _total_means(model_key, kind):
    """Area-frame total-population indicator means (methodology Step 1):
    statewide & county = ACS aggregate for the frame; district & HSA = average of
    the member municipalities. Returns (statewide scalar, county Series[CNTYCODE],
    district Series[name], hsa Series[name])."""
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
    # district & HSA: POPULATION-WEIGHTED aggregates of the member municipalities
    # (rate -> sum(num)/sum(denom); median -> household-count-weighted mean), matching
    # the statewide/county semantics. Median falls back to an unweighted town mean when
    # no household-count weight is available for the model.
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

    return (statewide, county,
            _frame_mean('VDH District'),
            _frame_mean('Hospital Service Area'))


def _fetch(getvars, geo):
    predicates = {'get': ",".join(getvars), 'key': apiKey}
    if geo == 'statewide':
        predicates['for'] = 'state:50'
    elif geo == 'county':
        predicates['for'] = 'county:*'; predicates['in'] = 'state:50'
    else:  # town
        predicates['for'] = 'county subdivision:*'; predicates['in'] = 'state:50'
    resp = requests.get(baseUrl, params=predicates).json()
    return pd.DataFrame(resp[1:])


def _process_group(group, kind, geo):
    """Fetch one demographic group at one geography; return a frame with a geo key
    plus numeric 'pct' (and 'num'/'denom' for rate indicators, 'weight' = household
    count for median indicators)."""
    if kind == 'median':
        median_vars = list(group['variables'])
        # Household-count weight for each race-specific median: B19013X_001E ->
        # B19001X_001E (same race-iteration letter; B19001_001E = total households for
        # the overall median). Used to population-weight the district/HSA aggregates.
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
    geocols = list(range(n_head, total_cols))  # [state] | [state,county] | [state,county,cousub]
    if geo == 'statewide':
        df['geo'] = 'statewide'
    elif geo == 'county':
        df['County Key'] = df[geocols[1]].astype(str)
    else:
        df['Town GEOID'] = (df[geocols[0]].astype(str) + df[geocols[1]].astype(str) + df[geocols[2]].astype(str))
    return df


def _keep_rate(denom):
    return denom >= DEMO_SUPPRESS_MIN


def run_demographics():
    statewideRows, countiesRows, districtsRows, hospitalsRows = [], [], [], []
    cfg = _demoCfg['indicators']
    for name, ind in cfg.items():
        model_key = ind['model_key']; kind = ind['kind']
        ind_label = ind['indicator_label']; build_bipoc = ind['build_bipoc']
        sw_mean, cty_mean, dist_mean, hsa_mean = _total_means(model_key, kind)
        bipoc_town = None  # Town GEOID -> summed num/denom across member race groups

        for group in ind['groups']:
            label = group['label']

            # ---- statewide ----
            s = _process_group(group, kind, 'statewide')
            for _, r in s.iterrows():
                if kind == 'median':
                    if not (r['pct'] > 0):
                        continue
                elif not _keep_rate(r['denom']):
                    continue
                statewideRows.append({'Indicator': ind_label, 'Demographic Group': label,
                                      'Disaggregated Percentage': r['pct'], 'Total Statewide Mean': sw_mean})

            # ---- counties ----
            c = _process_group(group, kind, 'county')
            for _, r in c.iterrows():
                if kind == 'median':
                    if not (r['pct'] > 0):
                        continue
                elif not _keep_rate(r['denom']):
                    continue
                ckey = r['County Key']
                countiesRows.append({'Indicator': ind_label, 'Demographic Group': label,
                                     'Disaggregated Percentage': r['pct'],
                                     'Total County Mean': cty_mean.get(ckey, np.nan),
                                     'County': _county_with_suffix(countyCodeToName.get(ckey))})

            # ---- towns -> districts & hospitals ----
            # District/HSA disaggregated values are POPULATION-WEIGHTED aggregates of
            # the member towns (rate -> sum(num)/sum(denom); median -> household-count-
            # weighted mean), consistent with the BIPOC aggregate below and the
            # area-frame means, rather than an unweighted mean of town values.
            t = _process_group(group, kind, 'town')
            tm = townsDataFrame.merge(t[['Town GEOID', 'pct', 'num', 'denom', 'weight']], on='Town GEOID', how='left')
            if kind == 'median':
                tm = tm[(tm['pct'] > 0) & (tm['weight'] > 0)].copy()
                tm['_vw'] = tm['pct'] * tm['weight']
                dist = tm.groupby('VDH District', as_index=False).agg(_vw=('_vw', 'sum'), weight=('weight', 'sum'))
                hosp = tm.groupby('Hospital Service Area', as_index=False).agg(_vw=('_vw', 'sum'), weight=('weight', 'sum'))
                dist['pct'] = np.where(dist['weight'] > 0, dist['_vw'] / dist['weight'], np.nan)
                hosp['pct'] = np.where(hosp['weight'] > 0, hosp['_vw'] / hosp['weight'], np.nan)
                dist['denom'] = np.nan; hosp['denom'] = np.nan
            else:
                dist = tm.groupby('VDH District', as_index=False).agg(num=('num', 'sum'), denom=('denom', 'sum'))
                hosp = tm.groupby('Hospital Service Area', as_index=False).agg(num=('num', 'sum'), denom=('denom', 'sum'))
                dist['pct'] = np.where(dist['denom'] > 0, dist['num'] / dist['denom'] * 100, np.nan)
                hosp['pct'] = np.where(hosp['denom'] > 0, hosp['num'] / hosp['denom'] * 100, np.nan)
            for _, r in dist.iterrows():
                if kind != 'median' and not _keep_rate(r['denom']):
                    continue
                if kind == 'median' and not (r['pct'] > 0):
                    continue
                districtsRows.append({'VDH District': r['VDH District'], 'Indicator': ind_label,
                                      'Demographic Group': label, 'Disaggregated Percentage': r['pct'],
                                      'Total VDH District Mean': dist_mean.get(r['VDH District'], np.nan)})
            for _, r in hosp.iterrows():
                if kind != 'median' and not _keep_rate(r['denom']):
                    continue
                if kind == 'median' and not (r['pct'] > 0):
                    continue
                hospitalsRows.append({'Hospital Service Area': r['Hospital Service Area'], 'Indicator': ind_label,
                                      'Demographic Group': label, 'Disaggregated Percentage': r['pct'],
                                      'Total Hospital Service Area Mean': hsa_mean.get(r['Hospital Service Area'], np.nan)})

            # ---- accumulate BIPOC members (rate indicators only) ----
            if build_bipoc and kind == 'rate' and label in BIPOC_MEMBERS:
                add = t[['Town GEOID', 'num', 'denom']].copy()
                bipoc_town = add if bipoc_town is None else (
                    pd.concat([bipoc_town, add]).groupby('Town GEOID', as_index=False).sum())

        # ---- BIPOC aggregate (district + hospital only, population-weighted) ----
        if build_bipoc and bipoc_town is not None:
            bm = townsDataFrame.merge(bipoc_town, on='Town GEOID', how='left')
            for geocol, keyname, acc, meancol, lookup in [
                ('VDH District', 'VDH District', districtsRows, 'Total VDH District Mean', dist_mean),
                ('Hospital Service Area', 'Hospital Service Area', hospitalsRows, 'Total Hospital Service Area Mean', hsa_mean)]:
                g = bm.groupby(geocol, as_index=False).agg(num=('num', 'sum'), denom=('denom', 'sum'))
                g['pct'] = np.where(g['denom'] > 0, g['num'] / g['denom'] * 100, np.nan)
                for _, r in g.iterrows():
                    if not _keep_rate(r['denom']):
                        continue
                    row = {keyname: r[geocol], 'Indicator': ind_label, 'Demographic Group': BIPOC_LABEL,
                           'Disaggregated Percentage': r['pct'], meancol: lookup.get(r[geocol], np.nan)}
                    acc.append(row)

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


statewideDemographicsDataFrame, countiesDemographicsDataFrame, districtsDemographicsDataFrame, hospitalsDemographicsDataFrame = run_demographics()

statewideDemographicsDataFrame.to_excel(output_path('statewideDisaggregated'), index=False)
countiesDemographicsDataFrame.to_excel(output_path('countiesDisaggregated'), index=False)
districtsDemographicsDataFrame.to_excel(output_path('districtsDisaggregated'), index=False)
hospitalsDemographicsDataFrame.to_excel(output_path('hospitalsDisaggregated'), index=False)
print("Wrote 4 disaggregated-demographics tables to", OUTPUT_DIR)


# =============================================================================
# END OF PIPELINE
# -----------------------------------------------------------------------------
# Deliverables written to OUTPUT_DIR:
#   tractsIndexGeometric / townsIndexGeometric   (composite index)
#   tractsIndicators / townsIndicators           (per-indicator + SMD)
#   tractsUnpopulated / townsUnpopulated         (zero-population units)
#   countiesDisaggregated / statewideDisaggregated / districtsDisaggregated /
#   hospitalsDisaggregated                       (race/ethnicity disaggregation)
# Population-reference maps were removed from the product (project decision, 2026).
print("CRI pipeline complete. Outputs in:", OUTPUT_DIR)
