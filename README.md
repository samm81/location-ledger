# timeline cities

**build a [nomads.com-style timeline][1] from android location data**

use one local script to turn google maps timeline and [gpslogger][2] tracks into city stays.

## purpose

this is a personal-use script for creating a [nomads.com-style timeline][1] from my own travel data. it accepts an android google maps timeline export, [gpslogger][2] gpx data, or both.

at least one source is required.

when both sources cover the same period, [gpslogger][2] data wins. google maps timeline data only augments gaps outside the gpx coverage.

the workflow is tailored to this data and this output format. it is not a general-purpose location-history application.

## ai assistance

completely vibecoded. verify generated timelines before relying on them.

## requirements

- python 3.11 or newer.
- [uv][3].
- an android google maps timeline export, [gpslogger][2] gpx data, or both.

the script header declares its python dependencies in a format that [uv][3] can auto-install when you run the script with `uv run`.

## quickstart

1. export google maps timeline data on android if you want to use it. open **settings > location > location services > timeline > export timeline data**, then choose where to save the file. see [google's android export instructions][4].

2. configure [gpslogger][2] to save gpx tracks if you want to use them. by default, it should save files with `YYYYMMDD.gpx` file names, such as `20260722.gpx`.

3. run the converter from the repository directory with either source or both:

```console
# google maps timeline only
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --output cities

# gpx only
uv run timeline_cities.py --gpx-directory ./gpx --output cities

# both sources: gpx wins where it covers the same time
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --gpx-directory ./gpx \
  --output cities

cat cities.csv
```

the command writes `cities.raw.csv`, `cities.audit.csv`, and the final `cities.csv`.

## what this script does

- **combines sources:** accepts google maps timeline data, [gpslogger][2] gpx data, or both, with gpx taking priority over overlapping google records.
- **normalizes places:** reverse-geocodes locally and rounds districts and suburbs toward larger sensible city names.
- **assigns local dates:** converts utc timestamps to the timezone at each coordinate before grouping points into city stays.
- **corrects from raw evidence:** lets reliable raw signals override a conflicting google maps inferred stay, regardless of its duration. it does not use legacy `timelineEdits` data.

no location data is sent to a reverse-geocoding service.

## outputs

for `--output cities`, the script creates:

- `cities.raw.csv`: normalized stays before corrections and manual overrides.
- `cities.audit.csv`: raw stays, automatic corrections, and manual changes.
- `cities.csv`: the final corrected result.

arrival and departure dates describe the stay's endpoints. if a move occurs on a shared transition date, that date appears in both adjacent rows intentionally, so the travel day is counted twice in the timeline. `Duration in days` remains the difference between the arrival and departure dates.

example:

```csv
"Arrival date","Departure date","Duration in days","City","Country"
"2026-07-15","2026-07-20","5","Budapest","Hungary"
"2026-07-20","2026-07-25","5","Vienna","Austria"
```

## manual overrides

the repository includes header-only templates in `examples/overrides/`:

- `place-mappings.example.csv` maps one exact city and country to another place in the same country.
- `trip-overrides.example.csv` forces an inclusive date range to one city and country.

copy each example to `place-mappings.csv` or `trip-overrides.csv` in a personal directory, then pass that directory with `--overrides-dir`. for example:

```console
mkdir -p inputs/overrides
cp examples/overrides/place-mappings.example.csv inputs/overrides/place-mappings.csv
cp examples/overrides/trip-overrides.example.csv inputs/overrides/trip-overrides.csv
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --overrides-dir inputs/overrides \
  --output cities
```

the script does not load overrides unless `--overrides-dir` is provided. the personal files in `inputs/overrides/` are ignored by git. to delete one zero-day stay without deleting neighboring stays, leave `City` and `Country` blank for that exact date.

## options

| option | default | description |
| --- | --- | --- |
| `--google-timeline` | not set | read an optional google maps timeline export json. |
| `--gpx-directory` | not set | read [gpslogger][2] `YYYYMMDD.gpx` tracks and augment them with google data outside their coverage. |
| `--overrides-dir` | not set | read optional `place-mappings.csv` and `trip-overrides.csv` files from this directory. |
| `--output` | google timeline filename or `cities` for gpx-only runs | set the output filename prefix. |
| `--overwrite` | not set | overwrite existing output files without prompting. |
| `--major-population`, `--major-radius-km` | `500000`, `75` | prefer a major city within the configured radius. |
| `--regional-population`, `--regional-radius-km` | `100000`, `40` | use a smaller populated place when the major-city rule does not fit. |
| `--cluster-radius-m`, `--cluster-gap-minutes` | `500`, `60` | group nearby stationary samples to reduce repeated lookups. |

run `uv run timeline_cities.py --help` for signal-quality and time-gap options.

[1]: https://nomads.com/@athousandcups
[2]: https://github.com/mendhak/gpslogger/
[3]: https://docs.astral.sh/uv/
[4]: https://support.google.com/maps/answer/6258979?co=GENIE.Platform%3DAndroid&hl=en
[5]: https://github.com/richardpenman/reverse_geocode/
[6]: https://www.geonames.org/
[7]: https://creativecommons.org/licenses/by/4.0/
