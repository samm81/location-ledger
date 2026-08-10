# Google Timeline cities

`timeline_cities.py` converts a Google Timeline JSON export into consecutive,
normalized city stays. All location processing and reverse geocoding happens
locally.

## Run

Install [`uv`](https://docs.astral.sh/uv/) and run:

```console
./timeline_cities.py location-history.json -o cities
```

The script's PEP 723 metadata lets `uv` create an isolated environment and install
the required packages automatically. The first run downloads the GeoNames-derived
city index and timezone boundary data. Later runs use the local cache and require no
geocoding service.

The current phone export format is accepted:

- Phone exports containing `semanticSegments`, `rawSignals`, and
  `userLocationProfile`.
- Direct sample arrays such as `semanticSegments.json` and `rawSignals.json`.

When semantic segments are available, the script prefers them over raw signals.
Visits provide explicit start/end times and inferred place coordinates. Timeline
paths and activity endpoints provide supplementary evidence. Raw GPS positions are
used only when semantic location evidence is unavailable.

## City normalization

For every usable position, the script:

1. Chooses the nearest city with at least 500,000 residents when it is no more than
   75 km away and in the same country.
2. Otherwise chooses the nearest city with at least 100,000 residents when it is no
   more than 40 km away and in the same country.
3. Otherwise keeps the nearest known populated place.

This rounds nearby districts and suburbs toward major city names without assigning a
remote major city to rural travel. The thresholds are configurable:

```console
./timeline_cities.py location-history.json \
  --major-population 500000 \
  --major-radius-km 75 \
  --regional-population 100000 \
  --regional-radius-km 40
```

Timestamps are normalized to UTC, then converted to the timezone at each data point
before assigning a calendar date. Semantic visits and activities use their explicit
`*TimezoneUtcOffsetMinutes` fields when present; otherwise the timestamp's own ISO
offset is used. Use a fixed timezone when you want every record assigned to one date
boundary:

```console
./timeline_cities.py location-history.json --timezone Europe/Budapest
```

The city with the most estimated time wins each internal daily assignment. Semantic
visit durations are split across local midnight boundaries. For raw positions and
path points, time is estimated from the next timestamp; gaps longer than three hours
receive a neutral one-minute weight instead of being treated as continuous presence.
When a new city has evidence on the previous city's final scored day, that day is
used as the shared travel/transition date for both stays.

## Output

```csv
"Arrival date","Departure date","Duration in days","City","Country"
"2023-07-17","2023-08-03","17","Warsaw","Poland"
"2023-08-04","2023-08-11","7","Prague","Czechia"
```

The departure date is the final or transition day. A same-day move therefore uses
the same date as the previous stay's departure and the next stay's arrival.
`Duration in days` remains `departure date - arrival date`, so the shared transition
day is represented by both stays without being counted twice.

## Three output files

Each run writes three files using the output prefix. For the command above:

- `cities.raw.csv` contains the unmodified normalized stays.
- `cities.audit.csv` records every raw stay and any automatic correction decision.
- `cities.fixed.csv` contains the corrected stays.

The first correction pass only fixes a one-day-or-less stay sandwiched between two
matching stays in the same city when reliable overlapping raw signals contradict
the middle stay. For example, a semantic `Beijing → Paris → Beijing` sequence is
fixed only if the raw signals during the Paris stay remain in Beijing. A genuine
`Tianjin → Beijing → Tianjin` trip remains unchanged when raw signals support
Beijing. If raw signals are unavailable or mixed, the stay is retained and marked
for review. Use `--max-isolated-days 0` to disable automatic correction.

## Development checks

```console
uv run --with ijson --with reverse-geocode --with timezonefinder \
  --with pytest --with pytest-cov \
  pytest --cov=timeline_cities --cov-fail-under=80
uv run --with ruff ruff check timeline_cities.py tests
uv run --with ruff ruff format --check timeline_cities.py tests
uv run --with ty ty check timeline_cities.py
```

City data comes from [GeoNames](https://www.geonames.org/) and is licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
