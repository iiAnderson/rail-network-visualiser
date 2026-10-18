-- Station reference: TIPLOC -> CRS code, name and OSGB grid reference (metres).
SELECT tiploccode AS TiplocCode, crscode AS CrsCode, stationname AS StationName, easting, northing
FROM tiplocs
WHERE crscode IS NOT NULL AND crscode <> ''
