# Timeline Cities

**Build a [Nomads.com-style timeline][1] from Android location data**

Use one local script to turn Google Maps Timeline and [GPSLogger][2] tracks into city stays.

## Purpose

This is a personal-use script for creating a [Nomads.com-style timeline][1] from my own travel data. It accepts an Android Google Maps Timeline export, [GPSLogger][2] GPX data, or both.

At least one source is required.

When both sources cover the same period, [GPSLogger][2] data wins. Google Maps Timeline data only augments gaps outside the GPX coverage.

The workflow is tailored to this data and this output format. It is not a general-purpose location-history application.

## ai assistance

completely vibecoded. verify generated timelines before relying on them.

## Requirements

- Python 3.11 or newer.
- [uv][3].
- An Android Google Maps Timeline export, [GPSLogger][2] GPX data, or both.

The script header declares its Python dependencies in a format that [uv][3] can auto-install when you run the script with `uv run`.

## Quickstart

1. Export Google Maps Timeline data on Android if you want to use it. Open **Settings > Location > Location services > Timeline > Export Timeline data**, then choose where to save the file. See [Google's Android export instructions][4].

2. Configure [GPSLogger][2] to save GPX tracks if you want to use them. By default, it should save files with `YYYYMMDD.gpx` file names, such as `20260722.gpx`.

3. Run the converter from the repository directory with either source or both:

```console
# Google Maps Timeline only
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --output cities

# GPX only
uv run timeline_cities.py --gpx-directory ./gpx --output cities

# Both sources: GPX wins where it covers the same time
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --gpx-directory ./gpx \
  --output cities

cat cities.csv
```

The command writes `cities.raw.csv`, `cities.audit.csv`, and the final `cities.csv`.

## What this script does

- **Combines sources:** accepts Google Maps Timeline data, [GPSLogger][2] GPX data, or both, with GPX taking priority over overlapping Google records.
- **Normalizes places:** reverse-geocodes locally and rounds districts and suburbs toward larger sensible city names.
- **Assigns local dates:** converts UTC timestamps to the timezone at each coordinate before grouping points into city stays.
- **Corrects from raw evidence:** lets reliable raw signals override a conflicting Google Maps inferred stay, regardless of its duration. It does not use legacy `timelineEdits` data.

No location data is sent to a reverse-geocoding service.

## Outputs

For `--output cities`, the script creates:

- `cities.raw.csv`: normalized stays before corrections and manual overrides.
- `cities.audit.csv`: raw stays, automatic corrections, and manual changes.
- `cities.csv`: the final corrected result.

Arrival and departure dates describe the stay's endpoints. If a move occurs on a shared transition date, that date appears in both adjacent rows intentionally, so the travel day is counted twice in the timeline. `Duration in days` remains the difference between the arrival and departure dates.

Example:

```csv
"Arrival date","Departure date","Duration in days","City","Country"
"2026-07-15","2026-07-20","5","Budapest","Hungary"
"2026-07-20","2026-07-25","5","Vienna","Austria"
```

## Manual overrides

The repository includes header-only templates in `examples/overrides/`:

- `place-mappings.example.csv` maps one exact city and country to another place in the same country.
- `trip-overrides.example.csv` forces an inclusive date range to one city and country.

Copy each example to `place-mappings.csv` or `trip-overrides.csv` in a personal directory, then pass that directory with `--overrides-dir`. For example:

```console
mkdir -p inputs/overrides
cp examples/overrides/place-mappings.example.csv inputs/overrides/place-mappings.csv
cp examples/overrides/trip-overrides.example.csv inputs/overrides/trip-overrides.csv
uv run timeline_cities.py \
  --google-timeline location-history.json \
  --overrides-dir inputs/overrides \
  --output cities
```

The script does not load overrides unless `--overrides-dir` is provided. The personal files in `inputs/overrides/` are ignored by Git. To delete one zero-day stay without deleting neighboring stays, leave `City` and `Country` blank for that exact date.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--google-timeline` | not set | Read an optional Google Maps Timeline export JSON. |
| `--gpx-directory` | not set | Read [GPSLogger][2] `YYYYMMDD.gpx` tracks and augment them with Google data outside their coverage. |
| `--overrides-dir` | not set | Read optional `place-mappings.csv` and `trip-overrides.csv` files from this directory. |
| `--output` | Google Timeline filename or `cities` for GPX-only runs | Set the output filename prefix. |
| `--overwrite` | not set | Overwrite existing output files without prompting. |
| `--major-population`, `--major-radius-km` | `500000`, `75` | Prefer a major city within the configured radius. |
| `--regional-population`, `--regional-radius-km` | `100000`, `40` | Use a smaller populated place when the major-city rule does not fit. |
| `--cluster-radius-m`, `--cluster-gap-minutes` | `500`, `60` | Group nearby stationary samples to reduce repeated lookups. |

Run `uv run timeline_cities.py --help` for signal-quality and time-gap options.

[1]: https://nomads.com/@athousandcups
[2]: https://github.com/mendhak/gpslogger/
[3]: https://docs.astral.sh/uv/
[4]: https://support.google.com/maps/answer/6258979?co=GENIE.Platform%3DAndroid&hl=en
[5]: https://github.com/richardpenman/reverse_geocode/
[6]: https://www.geonames.org/
[7]: https://creativecommons.org/licenses/by/4.0/
