The ETL script CommunityResilienceIndexCurrentDev uses the Census API to acquire ACS 5-year estimate data from multiple datasets in order to construct indicators. The script then performs extractions and transformations to the data to create the final output necessary to drive a dashboard.

The final output must consist of the following components:
    - 11 ACS indicators (10 percentage, 1 dollar value) at the town level
    - a geometric mean composite index equally weighted of all input indicators. Each town will have 3 geographic levels of calculation: 1 calculation ranking all towns statewide, 1 calculation ranking towns within each VDH health district, 1 calculation ranking towns within each county
    - A standard mean difference for each indicator
        - each town has 3 SMD values: 1 with a full state comparison, 1 just against the other towns of its health district, and 1 just against the other towns of its county
        - SMD requires knowing the mean for the overall area frame (ie. state, specific VDH district, specific county) and the standard deviation of that area frame. State and county-level indicator values are pulled directly from Census API. Helath district values need to be constructed from the input town-level values - add all numerator indicator values and denominator population counts of input towns to sum a district-wide numerator and denominator to determine the mean. For median household income, there is no denominator so instead the mean should be household weighted. 
    - disaggregations of indicator data is provided at the area frame level (ie. whole state, individual counties, individual health districts). Each indicator is disaggregated according to what data is available. Not all indicators will receive the same disaggregation. However, sum the numeraotr and denominator values of all non-white ACS categories to create a composite BIPOC disaggregation. Do this at all state, district, and country frames. 
        - see "G:\EnvHealth\Projects\Community Resilience Project\data\reference\VDH_CRI_DisaggreatedVariableAppendix.xlsx" for a comprehensive breakdown of which ACS variables are used in each indicator's available disaggregations
    - preserve an unpopulated town output
    - ACS 5-year ranges will include 2021-2025, 2016-2020, 2011-2015
        - note that indicator broadband B28005 will not be available 2011-2015
        - see "G:\EnvHealth\Projects\Community Resilience Project\data\reference\VDH_CRI_IndexVariableAppendix.xlsx" for a comprehensive breakdown of ACS data variables used in constructing indicators
