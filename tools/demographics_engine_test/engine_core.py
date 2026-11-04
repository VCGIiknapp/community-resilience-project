"""Core demographics engine functions — IDENTICAL to what will be inserted into
the main script. Kept here first so the logic can be unit-tested offline with
stubbed API + live-model globals. Do not add script-only imports here.

The engine expects these names to exist in its namespace at runtime:
  pd, np, os, requests, apiKey, baseUrl,
  geographyDataframes, geographyDenominatorsDataframes, countyNamesDictionary,
  _county_with_suffix, DATA_YEARS, output_path,
  _demoCfg, DEMO_SUPPRESS_MIN, BIPOC_MEMBERS, BIPOC_LABEL, townsDataFrame,
  countyCodeToName
"""


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
    src = geographyDataframes[model_key]['county subdivision']
    valcol = 'Numerator' if kind == 'median' else 'PercentPerCapita'
    df = pd.DataFrame({'Town GEOID': src['JoinKey'].astype(str),
                       'val': pd.to_numeric(src[valcol], errors='coerce')})
    df = df.merge(townsDataFrame, on='Town GEOID', how='left')
    if kind == 'median':
        df = df[df['val'] > 0]
    return (statewide, county,
            df.groupby('VDH District')['val'].mean(),
            df.groupby('Hospital Service Area')['val'].mean())


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
    plus numeric 'pct' (and 'num'/'denom' for rate indicators)."""
    if kind == 'median':
        getvars = group['variables']
    else:
        getvars = group['denominators'] + group['numerators']
    n_head = len(getvars)
    df = _fetch(getvars, geo)
    total_cols = df.shape[1]
    for c in range(n_head):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if kind == 'median':
        df['pct'] = df[0]
        df['denom'] = np.nan
        df['num'] = np.nan
    else:
        nd = len(group['denominators']); nn = len(group['numerators'])
        df['denom'] = df.iloc[:, 0:nd].sum(axis=1)
        df['num'] = df.iloc[:, nd:nd + nn].sum(axis=1)
        df['pct'] = np.where(df['denom'] > 0, df['num'] / df['denom'] * 100, np.nan)
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
            t = _process_group(group, kind, 'town')
            tm = townsDataFrame.merge(t[['Town GEOID', 'pct', 'num', 'denom']], on='Town GEOID', how='left')
            if kind == 'median':
                tm = tm[tm['pct'] > 0]
                dist = tm.groupby('VDH District', as_index=False).agg(pct=('pct', 'mean'))
                hosp = tm.groupby('Hospital Service Area', as_index=False).agg(pct=('pct', 'mean'))
                dist['denom'] = np.nan; hosp['denom'] = np.nan
            else:
                dist = tm.groupby('VDH District', as_index=False).agg(pct=('pct', 'mean'), denom=('denom', 'sum'))
                hosp = tm.groupby('Hospital Service Area', as_index=False).agg(pct=('pct', 'mean'), denom=('denom', 'sum'))
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
