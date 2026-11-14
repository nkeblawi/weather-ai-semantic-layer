-- Seed for analytics.station_aliases: extra names lookup_station matches for a
-- station (airport names, codes, nicknames), on top of its name, city, ICAO and IATA.
-- This file is the source of truth for dev and prod. Edit it, then load dev first:
--   scripts/deploy_sql.sh --env dev station_aliases
--   scripts/deploy_sql.sh --env prod station_aliases
-- INSERT OVERWRITE replaces every row; the table, its owner and its grants stay.
-- Matching ignores case and punctuation, so 'St Johns' also matches "St. John's".
INSERT OVERWRITE weather.analytics.station_aliases (ghcn_id, alias)
SELECT ghcn_id, explode(aliases) FROM VALUES
  -- Edmonton (EDMONTON INTL A)
  ('CAN03012216', array('Edmonton', 'YEG', 'CYEG', 'Edmonton Airport', 'Edmonton International', 'Edmonton International Airport', 'Nisku', 'Leduc')),
  -- Toronto (TORONTO INTL A)
  ('CAN06158731', array('Toronto', 'YYZ', 'CYYZ', 'Pearson', 'Toronto Pearson', 'Toronto Airport', 'Toronto Pearson International Airport', 'Lester B Pearson International Airport', 'Toronto International Airport')),
  -- Montreal (MONTREAL INTL A)
  ('CAN07025251', array('Montréal', 'YUL', 'CYUL', 'Trudeau', 'Montreal Trudeau', 'Montreal Airport', 'Montreal Trudeau International Airport', 'Pierre Elliott Trudeau International Airport', 'Dorval', 'Montreal Dorval', 'Montreal International Airport')),
  -- Halifax (HALIFAX STANFIELD INT'L A)
  ('CAN08202251', array('Halifax', 'YHZ', 'CYHZ', 'Stanfield', 'Halifax Stanfield', 'Halifax Airport', 'Halifax Stanfield International Airport', 'Halifax Nova Scotia', 'Halifax Stanfield Intl Airport')),
  -- St. John's (ST. JOHN'S INTL A)
  ('CAN08403505', array('St Johns', 'Saint Johns', 'St Johns Newfoundland', 'YYT', 'CYYT', 'St Johns Airport', 'St Johns International Airport', 'Newfoundland')),
  -- Denver (DENVER INTL AP)
  ('USW00003017', array('Denver', 'DIA', 'Denver Airport', 'Denver International Airport', 'Denver International', 'Mile High City', 'DEN', 'KDEN')),
  -- Dallas (DAL-FTW WSCMO AP)
  ('USW00003927', array('Dallas', 'Fort Worth', 'Dallas Fort Worth', 'DFW Airport', 'Dallas Fort Worth International Airport', 'Dallas Airport', 'DFW Metroplex', 'DFW', 'KDFW', 'Dal-Ftw Wscmo Airport', 'Dallas/Fort Worth International Airport', 'DFW International')),
  -- Kansas City (KANSAS CITY INTL AP)
  ('USW00003947', array('Kansas City', 'KC', 'KCI', 'Kansas City Airport', 'Kansas City International Airport', 'Kansas City International', 'MCI', 'KMCI')),
  -- Orlando (ORLANDO INTL AP)
  ('USW00012815', array('Orlando', 'Orlando Airport', 'Orlando International Airport', 'Orlando International', 'McCoy', 'MCO', 'KMCO')),
  -- Miami (MIAMI INTL AP)
  ('USW00012839', array('MIA', 'Miami', 'Miami International Airport', 'Miami Airport', 'KMIA')),
  -- Tampa (TAMPA INTL AP)
  ('USW00012842', array('Tampa', 'Tampa Airport', 'Tampa International Airport', 'Tampa International', 'Tampa Bay', 'TPA', 'KTPA')),
  -- New Orleans (NEW ORLEANS AP)
  ('USW00012916', array('New Orleans', 'NOLA', 'New Orleans Airport', 'Louis Armstrong Airport', 'Louis Armstrong New Orleans International Airport', 'Armstrong International', 'Moisant Field', 'MSY', 'KMSY', 'Louis Armstrong')),
  -- San Antonio (SAN ANTONIO INTL AP)
  ('USW00012921', array('San Antonio', 'San Antonio Airport', 'San Antonio International Airport', 'San Antonio International', 'Alamo City', 'SAT', 'KSAT')),
  -- Houston (HOUSTON INTERCONTINENTAL AP)
  ('USW00012960', array('Houston', 'Houston Intercontinental', 'Bush Intercontinental', 'George Bush Intercontinental Airport', 'Houston Bush', 'Houston Airport', 'IAH', 'KIAH', 'Houston Intercontinental Airport')),
  -- Raleigh (RALEIGH AP)
  ('USW00013722', array('Raleigh', 'Durham', 'Raleigh Durham', 'Raleigh Durham International Airport', 'RDU Airport', 'Research Triangle', 'RDU', 'KRDU', 'Raleigh Airport', 'Raleigh-Durham')),
  -- Philadelphia (PHILA INTL AP)
  ('USW00013739', array('Philadelphia', 'Philly', 'Philadelphia Airport', 'Philadelphia International Airport', 'Philly Airport', 'PHL', 'KPHL', 'Phila International Airport', 'Philadelphia International')),
  -- Washington, DC (WASHINGTON REAGAN NATL AP)
  ('USW00013743', array('National Airport', 'Washington Reagan National Airport', 'Reagan National Airport', 'Reagan National', 'Washington National Airport', 'Washington National', 'DCA', 'KDCA', 'Reagan')),
  -- Atlanta (ATLANTA HARTSFIELD-JACKSON INT)
  ('USW00013874', array('ATL', 'Atlanta', 'Atlanta International Airport', 'Hartsfield-Jackson', 'Hartsfield-Jackson Atlanta International Airport', 'Atlanta Airport', 'KATL', 'Atlanta Hartsfield-Jackson International Airport', 'Hartsfield')),
  -- Charlotte (CHARLOTTE DOUGLAS AP)
  ('USW00013881', array('Charlotte', 'Charlotte Douglas', 'Charlotte Douglas International Airport', 'Charlotte Airport', 'Douglas Airport', 'CLT', 'KCLT', 'Charlotte Douglas Airport')),
  -- Nashville (NASHVILLE INTL AP)
  ('USW00013897', array('Nashville', 'Nashville Airport', 'Nashville International Airport', 'Nashville International', 'Music City', 'BNA', 'KBNA')),
  -- St. Louis (ST LOUIS LAMBERT INTL AP)
  ('USW00013994', array('St Louis', 'Saint Louis', 'Lambert', 'Lambert Field', 'St Louis Lambert', 'St Louis Lambert International Airport', 'St Louis Airport', 'STL', 'KSTL')),
  -- New York (LAGUARDIA AP)
  ('USW00014732', array('LaGuardia Airport', 'LaGuardia', 'New York LaGuardia Airport', 'NY LaGuardia', 'New York LaGuardia', 'Queens Airport', 'LGA', 'KLGA', 'NYC', 'La Guardia')),
  -- Boston (BOSTON)
  ('USW00014739', array('Boston', 'Logan', 'Logan Airport', 'Boston Logan', 'Logan International Airport', 'Boston Logan International Airport', 'BOS', 'KBOS')),
  -- Cleveland (CLEVELAND)
  ('USW00014820', array('Cleveland', 'Cleveland Hopkins', 'Cleveland Hopkins International Airport', 'Hopkins Airport', 'Cleveland Airport', 'CLE', 'KCLE')),
  -- Columbus (JOHN GLENN INTL AP)
  ('USW00014821', array('Columbus Ohio', 'John Glenn', 'John Glenn Columbus International Airport', 'Port Columbus', 'Columbus Airport', 'CMH', 'KCMH', 'Columbus', 'John Glenn International Airport')),
  -- Minneapolis (MINNEAPOLIS-ST PAUL INTL AP)
  ('USW00014922', array('Minneapolis St Paul International Airport', 'Minneapolis Airport', 'MSP', 'KMSP', 'Minneapolis', 'St Paul', 'Saint Paul', 'Twin Cities', 'Minneapolis St Paul', 'Minneapolis-St Paul International Airport', 'Minneapolis-St. Paul', 'MSP Airport')),
  -- Honolulu (HONOLULU INTL AP)
  ('USW00022521', array('Honolulu', 'Oahu', 'Honolulu Airport', 'Honolulu International Airport', 'Daniel K Inouye International Airport', 'Inouye Airport', 'HNL', 'PHNL', 'Daniel K. Inouye International Airport', 'Daniel K. Inouye')),
  -- Las Vegas (MCCARRAN INTL AP)
  ('USW00023169', array('Las Vegas', 'Vegas', 'Harry Reid Airport', 'Harry Reid International Airport', 'McCarran', 'McCarran Airport', 'Las Vegas Airport', 'LAS', 'KLAS', 'McCarran International Airport', 'Harry Reid')),
  -- Los Angeles (LOS ANGELES INTL AP)
  ('USW00023174', array('LAX', 'Los Angeles', 'Los Angeles International Airport', 'LA International Airport', 'KLAX', 'Los Angeles Airport', 'LA Airport')),
  -- Phoenix (PHOENIX AP)
  ('USW00023183', array('Phoenix', 'Sky Harbor', 'Phoenix Sky Harbor', 'Phoenix Sky Harbor International Airport', 'Phoenix Airport', 'PHX', 'KPHX')),
  -- San Diego (SAN DIEGO INTL AP)
  ('USW00023188', array('San Diego', 'Lindbergh Field', 'San Diego Airport', 'San Diego International Airport', 'San Diego International', 'SD Airport', 'SAN', 'KSAN')),
  -- San Francisco (SAN FRANCISCO INTL AP)
  ('USW00023234', array('SFO', 'San Francisco', 'San Francisco International Airport', 'SF Airport', 'KSFO')),
  -- Salt Lake City (SALT LAKE CITY INTL AP)
  ('USW00024127', array('Salt Lake City', 'Salt Lake', 'SLC Airport', 'Salt Lake City Airport', 'Salt Lake City International Airport', 'Salt Lake City International', 'SLC', 'KSLC', 'Salt Lake airport')),
  -- Portland (PORTLAND INTL AP)
  ('USW00024229', array('Portland', 'Portland Oregon', 'Portland Airport', 'Portland International Airport', 'Portland International', 'PDX Airport', 'PDX', 'KPDX')),
  -- Seattle (SEATTLE TACOMA AP)
  ('USW00024233', array('Seattle Tacoma', 'Seattle Tacoma International Airport', 'Seattle Airport', 'SEA', 'KSEA', 'Seattle', 'SeaTac', 'Sea Tac', 'SeaTac Airport', 'Seattle Tacoma Airport', 'Seattle-Tacoma')),
  -- Anchorage (ANCHORAGE TED STEVENS INTL AP)
  ('USW00026451', array('Anchorage', 'Ted Stevens', 'Ted Stevens Anchorage International Airport', 'Anchorage Airport', 'Anchorage International', 'ANC', 'PANC', 'Anchorage Ted Stevens International Airport')),
  -- Baltimore (BALTIMORE-WASHINGTON INTL AP)
  ('USW00093721', array('Baltimore', 'BWI Airport', 'Baltimore Airport', 'Baltimore Washington', 'Baltimore Washington International Airport', 'BWI Marshall', 'Thurgood Marshall Airport', 'BWI', 'KBWI', 'Baltimore-Washington International Airport')),
  -- Sterling (WASHINGTON DULLES INTL AP)
  ('USW00093738', array('Dulles Airport', 'Dulles International Airport', 'Washington Dulles International Airport', 'Washington Dulles Intl Airport', 'Dulles Intl AP', 'Dulles Intl', 'Dulles')),
  -- Cincinnati, OH (CINCINNATI/NORTHERN KENTUCKY I)
  ('USW00093814', array('Cincinnati', 'Northern Kentucky', 'Cincinnati Airport', 'Cincinnati Northern Kentucky', 'Cincinnati Northern Kentucky International Airport', 'Greater Cincinnati Airport', 'CVG', 'KCVG', 'Cincinnati/Northern Kentucky International Airport')),
  -- Indianapolis (INDIANAPOLIS)
  ('USW00093819', array('Indianapolis', 'Indy', 'Indianapolis Airport', 'Indianapolis International Airport', 'Indianapolis International', 'Indy Airport', 'IND', 'KIND')),
  -- New York JFK (JFK INTL AP)
  ('USW00094789', array('John F Kennedy International', 'JFK International', 'Kennedy Airport', 'John F Kennedy', 'New York JFK', 'JFK Airport', 'Idlewild', 'NY JFK', 'JFK International Airport', 'New York JFK, NY', 'Kennedy')),
  -- Pittsburgh (PITTSBURGH ASOS)
  ('USW00094823', array('PIT', 'KPIT', 'Pittsburgh', 'Pittsburgh Airport', 'Pittsburgh International Airport', 'Pittsburgh International', 'Greater Pittsburgh Airport')),
  -- Chicago (CHICAGO OHARE INTL AP)
  ('USW00094846', array('OHare', 'Chicago OHare', 'Chicago OHare International Airport', 'OHare International Airport', 'Chicago ORD', 'ORD', 'KORD', 'ohare', 'OHare Airport')),
  -- Detroit (DETROIT METRO AP)
  ('USW00094847', array('Detroit', 'Detroit Metro', 'Detroit Metro Airport', 'Detroit Metropolitan Airport', 'Detroit Metropolitan Wayne County Airport', 'Detroit Airport', 'DTW', 'KDTW'))
AS t(ghcn_id, aliases);
