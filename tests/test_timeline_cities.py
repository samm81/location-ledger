"""Tests for the Google Timeline city converter."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import timeline_cities
from timeline_cities import (
    CityMatch,
    CityNormalizer,
    CityScore,
    Position,
    Settings,
    TimezoneResolver,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("39.1112961°, 117.0662388°", (39.1112961, 117.0662388)),
        ("geo:38.998776,-77.0352177", (38.998776, -77.0352177)),
        (" 47.5, 19.05 ", (47.5, 19.05)),
        ("91°, 19°", None),
        ("not a coordinate", None),
    ],
)
def test_parses_phone_export_coordinates(
    value: str,
    expected: tuple[float, float] | None,
) -> None:
    assert timeline_cities.parse_lat_lng(value) == expected


def test_loads_raw_signals_array(tmp_path: Path) -> None:
    input_path = tmp_path / "rawSignals.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "position": {
                        "LatLng": "39.1112961°, 117.0662388°",
                        "accuracyMeters": 77,
                        "timestamp": "2026-06-28T19:19:25.000+02:00",
                    }
                },
                {"wifiScan": {"deliveryTime": "2026-06-28T19:19:25.000+02:00"}},
            ]
        ),
        encoding="utf-8",
    )

    positions = timeline_cities.load_positions(input_path, max_accuracy_m=1_000)

    assert len(positions) == 1
    assert positions[0].latitude == 39.1112961
    assert positions[0].accuracy_m == 77


def test_raw_phone_timestamp_converts_to_coordinate_date(tmp_path: Path) -> None:
    input_path = tmp_path / "rawSignals.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "position": {
                        "LatLng": "39.1112961°, 117.0662388°",
                        "accuracyMeters": 77,
                        "timestamp": "2026-06-28T19:19:25.000+02:00",
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    positions = timeline_cities.load_positions(
        input_path,
        max_accuracy_m=1_000,
    )
    timezone_resolver = TimezoneResolver("auto")

    assert positions[0].timestamp == datetime(2026, 6, 28, 17, 19, 25, tzinfo=UTC)
    assert timezone_resolver.local_date(positions[0]) == (
        date(2026, 6, 29),
        "Asia/Shanghai",
    )


def test_loads_manual_overrides_from_inputs_directory(tmp_path: Path) -> None:
    override_directory = tmp_path / "inputs" / "overrides"
    override_directory.mkdir(parents=True)
    (override_directory / "place-mappings.csv").write_text(
        "\ufeffFrom city,From country,To city,To country\n"
        "Midtown,United States,Manhattan,United States\n"
        "Manhattan,United States,New York City,United States\n",
        encoding="utf-8",
    )
    (override_directory / "trip-overrides.csv").write_text(
        "Arrival date,Departure date,City,Country\n"
        "2026-04-30,2026-05-22,Tianjin,China\n",
        encoding="utf-8",
    )

    mappings, overrides = timeline_cities.load_override_data(tmp_path)

    assert mappings == {
        ("Midtown", "United States"): ("New York City", "United States"),
        ("Manhattan", "United States"): ("New York City", "United States"),
    }
    assert overrides == [
        timeline_cities.TripOverride(
            date(2026, 4, 30),
            date(2026, 5, 22),
            "Tianjin",
            "China",
        )
    ]


def test_loads_blank_city_delete_override(tmp_path: Path) -> None:
    path = tmp_path / "trip-overrides.csv"
    path.write_text(
        "Arrival date,Departure date,City,Country\n2026-05-15,2026-05-15,,\n",
        encoding="utf-8",
    )

    overrides = timeline_cities.load_trip_overrides(path)

    assert overrides[0].is_delete


def test_loads_copied_output_row_with_duration_column(tmp_path: Path) -> None:
    path = tmp_path / "trip-overrides.csv"
    path.write_text(
        "Arrival date,Departure date,City,Country\n"
        "2025-08-12,2025-09-09,28,Ubud,Indonesia\n",
        encoding="utf-8",
    )

    overrides = timeline_cities.load_trip_overrides(path)

    assert overrides == [
        timeline_cities.TripOverride(
            date(2025, 8, 12), date(2025, 9, 9), "Ubud", "Indonesia"
        )
    ]


def test_rejects_cross_country_place_mapping(tmp_path: Path) -> None:
    path = tmp_path / "place-mappings.csv"
    path.write_text(
        "From city,From country,To city,To country\n"
        "Manhattan,United States,Beijing,China\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="changes country"):
        timeline_cities.load_place_mappings(path)


def test_rejects_place_mapping_cycle(tmp_path: Path) -> None:
    path = tmp_path / "place-mappings.csv"
    path.write_text(
        "From city,From country,To city,To country\nA,H,B,H\nB,H,A,H\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cycle"):
        timeline_cities.load_place_mappings(path)


def test_rejects_overlapping_trip_override_ranges(tmp_path: Path) -> None:
    path = tmp_path / "trip-overrides.csv"
    path.write_text(
        "Arrival date,Departure date,City,Country\n"
        "2026-01-01,2026-01-10,A,H\n"
        "2026-01-09,2026-01-12,B,H\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overlapping"):
        timeline_cities.load_trip_overrides(path)


def test_shared_transition_dates_are_allowed_for_trip_overrides(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trip-overrides.csv"
    path.write_text(
        "Arrival date,Departure date,City,Country\n"
        "2026-01-01,2026-01-10,A,H\n"
        "2026-01-10,2026-01-12,B,H\n",
        encoding="utf-8",
    )

    overrides = timeline_cities.load_trip_overrides(path)

    assert len(overrides) == 2


def test_trip_override_collapses_the_tianjin_example() -> None:
    stays = [
        timeline_cities.Stay(date(2026, 4, 30), date(2026, 5, 11), "Tianjin", "China"),
        timeline_cities.Stay(date(2026, 5, 11), date(2026, 5, 15), "Nanyuki", "Kenya"),
        timeline_cities.Stay(date(2026, 5, 15), date(2026, 5, 15), "Tianjin", "China"),
        timeline_cities.Stay(date(2026, 5, 19), date(2026, 5, 22), "Tianjin", "China"),
    ]
    override = timeline_cities.TripOverride(
        date(2026, 4, 30), date(2026, 5, 22), "Tianjin", "China"
    )

    fixed, audit = timeline_cities.apply_trip_overrides(stays, [override])

    assert fixed == [
        timeline_cities.Stay(date(2026, 4, 30), date(2026, 5, 22), "Tianjin", "China")
    ]
    assert [record.action for record in audit] == ["manual trip override"]


def test_trip_override_splits_partial_stays() -> None:
    stays = [
        timeline_cities.Stay(date(2026, 1, 1), date(2026, 1, 10), "Before", "Country"),
        timeline_cities.Stay(date(2026, 1, 10), date(2026, 1, 20), "After", "Country"),
    ]
    override = timeline_cities.TripOverride(
        date(2026, 1, 5), date(2026, 1, 15), "Forced", "Country"
    )

    fixed, _audit = timeline_cities.apply_trip_overrides(stays, [override])

    assert fixed == [
        timeline_cities.Stay(date(2026, 1, 1), date(2026, 1, 5), "Before", "Country"),
        timeline_cities.Stay(date(2026, 1, 5), date(2026, 1, 15), "Forced", "Country"),
        timeline_cities.Stay(date(2026, 1, 15), date(2026, 1, 20), "After", "Country"),
    ]


def test_delete_override_removes_only_exact_zero_day_stay() -> None:
    stays = [
        timeline_cities.Stay(date(2026, 4, 30), date(2026, 5, 15), "Tianjin", "China"),
        timeline_cities.Stay(date(2026, 5, 15), date(2026, 5, 15), "Nanyuki", "Kenya"),
        timeline_cities.Stay(date(2026, 5, 15), date(2026, 5, 22), "Tianjin", "China"),
    ]
    override = timeline_cities.TripOverride(
        date(2026, 5, 15), date(2026, 5, 15), "", ""
    )

    fixed, audit = timeline_cities.apply_trip_overrides(stays, [override])

    assert fixed == [
        timeline_cities.Stay(date(2026, 4, 30), date(2026, 5, 22), "Tianjin", "China")
    ]
    assert audit[0].action == "manual delete"


def test_loads_gpx_track_points_as_utc_positions(tmp_path: Path) -> None:
    gpx_directory = tmp_path / "gpx"
    gpx_directory.mkdir()
    (gpx_directory / "20260722.gpx").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="48.2227007" lon="16.3899034">
      <ele>223.9</ele><time>2026-07-21T23:00:16.896Z</time>
    </trkpt>
    <trkpt lat="48.2226946" lon="16.3898979">
      <time>2026-07-21T23:20:18.219Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )

    positions = timeline_cities.load_gpx_positions(gpx_directory)

    assert len(positions) == 2
    assert positions[0].timestamp == datetime(
        2026, 7, 21, 23, 0, 16, 896000, tzinfo=UTC
    )
    assert positions[0].latitude == 48.2227007
    assert positions[0].longitude == 16.3899034
    assert positions[0].source == "gpx:20260722.gpx"


def test_gpx_directory_is_authoritative_and_google_augments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    input_path = tmp_path / "timeline.json"
    input_path.write_text(
        json.dumps(
            {
                "semanticSegments": [
                    {
                        "startTime": "2026-07-21T23:00:00Z",
                        "endTime": "2026-07-22T01:00:00Z",
                        "visit": {
                            "topCandidate": {
                                "placeLocation": {"latLng": "47.5°, 19.05°"}
                            }
                        },
                    },
                    {
                        "startTime": "2026-07-23T00:00:00Z",
                        "endTime": "2026-07-23T02:00:00Z",
                        "visit": {
                            "topCandidate": {
                                "placeLocation": {"latLng": "47.5°, 19.05°"}
                            }
                        },
                    },
                ],
                "rawSignals": [],
            }
        ),
        encoding="utf-8",
    )
    gpx_directory = tmp_path / "gpx"
    gpx_directory.mkdir()
    (gpx_directory / "20260722.gpx").write_text(
        """<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
          <trkpt lat="48.2227007" lon="16.3899034">
            <time>2026-07-21T23:00:00Z</time>
          </trkpt>
          <trkpt lat="48.2227007" lon="16.3899034">
            <time>2026-07-22T00:00:00Z</time>
          </trkpt>
          <trkpt lat="48.2227007" lon="16.3899034">
            <time>2026-07-22T01:00:00Z</time>
          </trkpt>
        </trkseg></trk></gpx>""",
        encoding="utf-8",
    )
    budapest = _match(
        "Budapest",
        population=1_741_041,
        distance_km=2.0,
        rule="major_city",
    )
    vienna = _match(
        "Vienna",
        population=2_000_000,
        distance_km=1.0,
        rule="major_city",
    )

    def match_by_coordinate(
        _self: CityNormalizer,
        latitude: float,
        _longitude: float,
    ) -> CityMatch:
        return vienna if latitude > 48 else budapest

    monkeypatch.setattr(CityNormalizer, "match", match_by_coordinate)
    output_prefix = tmp_path / "cities"

    result = timeline_cities.run(
        [
            str(input_path),
            "--gpx-directory",
            str(gpx_directory),
            "--output",
            str(output_prefix),
            "--timezone",
            "Europe/Vienna",
        ]
    )

    assert result == 0
    rows = list(
        csv.DictReader(
            (tmp_path / "cities.raw.csv").read_text(encoding="utf-8").splitlines()
        )
    )
    assert [row["City"] for row in rows] == ["Vienna", "Budapest"]


def test_semantic_timestamp_uses_explicit_location_offset(tmp_path: Path) -> None:
    input_path = tmp_path / "semanticSegments.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "startTime": "2013-06-06T00:02:45.000+02:00",
                    "endTime": "2013-06-06T13:03:35.000+02:00",
                    "startTimeTimezoneUtcOffsetMinutes": -240,
                    "endTimeTimezoneUtcOffsetMinutes": -240,
                    "visit": {
                        "topCandidate": {
                            "placeLocation": {"latLng": "38.9987804°, -77.0350292°"}
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    positions = timeline_cities.load_positions(
        input_path,
        max_accuracy_m=50_000,
    )
    timezone_resolver = TimezoneResolver("auto")

    assert positions[0].timestamp == datetime(2013, 6, 6, 4, 2, 45, tzinfo=UTC)
    assert positions[0].end_timestamp == datetime(2013, 6, 6, 17, 3, 35, tzinfo=UTC)
    assert timezone_resolver.local_date(positions[0]) == (
        date(2013, 6, 6),
        "America/New_York",
    )


def test_phone_export_prefers_semantic_segments(tmp_path: Path) -> None:
    input_path = tmp_path / "location-history.json"
    input_path.write_text(
        json.dumps(
            {
                "rawSignals": [
                    {
                        "position": {
                            "LatLng": "47.5°, 19.05°",
                            "timestamp": "2026-01-01T12:00:00Z",
                        }
                    }
                ],
                "semanticSegments": [
                    {
                        "startTime": "2013-06-06T00:02:45.000+02:00",
                        "endTime": "2013-06-06T13:03:35.000+02:00",
                        "visit": {
                            "topCandidate": {
                                "placeLocation": {"latLng": "38.9987804°, -77.0350292°"}
                            }
                        },
                    }
                ],
                "userLocationProfile": {},
            }
        ),
        encoding="utf-8",
    )

    data = timeline_cities.load_position_data(input_path, max_accuracy_m=50_000)

    assert data.source == "semanticSegments"
    assert len(data.positions) == 1
    assert len(data.raw_positions) == 1
    assert data.positions[0].latitude == 38.9987804
    assert data.positions[0].end_timestamp == datetime(
        2013,
        6,
        6,
        11,
        3,
        35,
        tzinfo=UTC,
    )


def test_phone_export_falls_back_to_raw_signals(tmp_path: Path) -> None:
    input_path = tmp_path / "location-history.json"
    input_path.write_text(
        json.dumps(
            {
                "semanticSegments": [],
                "rawSignals": [
                    {
                        "position": {
                            "LatLng": "47.5°, 19.05°",
                            "timestamp": "2026-01-01T12:00:00Z",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    positions = timeline_cities.load_positions(input_path, max_accuracy_m=50_000)

    assert len(positions) == 1
    assert positions[0].latitude == 47.5


def test_normalizer_prefers_nearby_major_city(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _match("Budaörs", population=29_000, distance_km=1.0, rule="nearest_city")
    major = _match(
        "Budapest", population=1_741_041, distance_km=12.0, rule="major_city"
    )

    def fake_lookup(
        coordinate: tuple[float, float],
        *,
        min_population: int,
        rule: str,
    ) -> CityMatch:
        del coordinate, rule
        return local if min_population == 0 else major

    monkeypatch.setattr(timeline_cities, "_lookup_city", fake_lookup)

    result = CityNormalizer(Settings()).match(47.46, 18.95)

    assert result.city == "Budapest"
    assert result.rule == "major_city"


def test_normalizer_does_not_round_to_a_distant_major_city(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _match("Smalltown", population=10_000, distance_km=1.0, rule="nearest_city")
    distant = _match(
        "Megacity", population=2_000_000, distance_km=100.0, rule="major_city"
    )

    def fake_lookup(
        coordinate: tuple[float, float],
        *,
        min_population: int,
        rule: str,
    ) -> CityMatch:
        del coordinate, rule
        return local if min_population == 0 else distant

    monkeypatch.setattr(timeline_cities, "_lookup_city", fake_lookup)

    result = CityNormalizer(Settings()).match(45.0, 18.0)

    assert result.city == "Smalltown"
    assert result.rule == "nearest_city"


def test_timezone_resolver_uses_coordinate_timezone() -> None:
    position = Position(
        timestamp=datetime(2026, 7, 17, 23, 30, tzinfo=UTC),
        latitude=47.5008,
        longitude=19.0531,
        accuracy_m=10.0,
    )

    local_day, timezone_name = TimezoneResolver("auto").local_date(position)

    assert local_day == date(2026, 7, 18)
    assert timezone_name == "Europe/Budapest"


def test_timezone_resolver_accepts_fixed_timezone() -> None:
    position = Position(
        timestamp=datetime(2026, 7, 17, 23, 30, tzinfo=UTC),
        latitude=40.7,
        longitude=-74.0,
        accuracy_m=10.0,
    )

    local_day, timezone_name = TimezoneResolver("Europe/Budapest").local_date(position)

    assert local_day == date(2026, 7, 18)
    assert timezone_name == "Europe/Budapest"


def test_timezone_resolver_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        TimezoneResolver("Nowhere/Imaginary")


def test_csv_chooses_city_with_most_estimated_time() -> None:
    budapest = _match(
        "Budapest",
        population=1_741_041,
        distance_km=2.0,
        rule="major_city",
    )
    vienna = _match("Vienna", population=2_000_000, distance_km=1.0, rule="major_city")
    budapest_score = CityScore()
    budapest_score.add(budapest, "Europe/Budapest", 7_200)
    vienna_score = CityScore()
    vienna_score.add(vienna, "Europe/Vienna", 3_600)
    output = io.StringIO()

    timeline_cities.write_csv(
        {
            date(2026, 7, 17): {
                ("Budapest", "HU"): budapest_score,
                ("Vienna", "AT"): vienna_score,
            }
        },
        output,
    )

    row = next(csv.DictReader(io.StringIO(output.getvalue())))
    assert row == {
        "Arrival date": "2026-07-17",
        "Departure date": "2026-07-17",
        "Duration in days": "0",
        "City": "Budapest",
        "Country": "Hungary",
    }
    assert output.getvalue().startswith(
        '"Arrival date","Departure date","Duration in days","City","Country"'
    )


def test_consecutive_city_days_are_merged_but_gaps_are_not() -> None:
    budapest = _match(
        "Budapest",
        population=1_741_041,
        distance_km=2.0,
        rule="major_city",
    )
    vienna = _match("Vienna", population=2_000_000, distance_km=1.0, rule="major_city")

    stays = timeline_cities.build_stays(
        {
            date(2026, 7, 17): {("Budapest", "HU"): _score(budapest)},
            date(2026, 7, 18): {("Budapest", "HU"): _score(budapest)},
            date(2026, 7, 19): {("Vienna", "AT"): _score(vienna)},
            date(2026, 7, 21): {("Vienna", "AT"): _score(vienna)},
        }
    )

    assert [
        (
            stay.arrival_date,
            stay.departure_date,
            stay.duration_days,
            stay.city,
        )
        for stay in stays
    ] == [
        (date(2026, 7, 17), date(2026, 7, 19), 2, "Budapest"),
        (date(2026, 7, 19), date(2026, 7, 19), 0, "Vienna"),
        (date(2026, 7, 21), date(2026, 7, 21), 0, "Vienna"),
    ]


def test_transition_day_is_shared_when_incoming_city_has_evidence() -> None:
    budapest = _match(
        "Budapest",
        population=1_741_041,
        distance_km=2.0,
        rule="major_city",
    )
    vienna = _match("Vienna", population=2_000_000, distance_km=1.0, rule="major_city")
    budapest_day = _score(budapest, seconds=7_200)
    vienna_transition_day = _score(vienna, seconds=3_600)
    budapest_transition_day = _score(budapest, seconds=7_200)
    vienna_next_day = _score(vienna, seconds=7_200)

    stays = timeline_cities.build_stays(
        {
            date(2026, 7, 17): {("Budapest", "HU"): budapest_day},
            date(2026, 7, 18): {("Budapest", "HU"): budapest_day},
            date(2026, 7, 19): {
                ("Budapest", "HU"): budapest_transition_day,
                ("Vienna", "AT"): vienna_transition_day,
            },
            date(2026, 7, 20): {("Vienna", "AT"): vienna_next_day},
        }
    )

    assert [(stay.arrival_date, stay.departure_date, stay.city) for stay in stays] == [
        (date(2026, 7, 17), date(2026, 7, 19), "Budapest"),
        (date(2026, 7, 19), date(2026, 7, 20), "Vienna"),
    ]


def test_repair_replaces_only_isolated_one_day_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_stays = [
        timeline_cities.Stay(
            date(2026, 7, 17), date(2026, 7, 18), "Budapest", "Hungary"
        ),
        timeline_cities.Stay(date(2026, 7, 18), date(2026, 7, 19), "Vienna", "Austria"),
        timeline_cities.Stay(
            date(2026, 7, 19), date(2026, 7, 20), "Budapest", "Hungary"
        ),
    ]

    normalizer = CityNormalizer(Settings())
    budapest = _match(
        "Budapest",
        population=1_741_041,
        distance_km=2.0,
        rule="major_city",
    )
    monkeypatch.setattr(normalizer, "match", lambda _lat, _lng: budapest)
    raw_positions = [
        Position(
            datetime(2026, 7, 18, hour, 0, tzinfo=UTC),
            47.5,
            19.05,
            100.0,
            source="GPS",
        )
        for hour in (8, 12, 16)
    ]
    timezone_resolver = TimezoneResolver("UTC")

    fixed_stays, audit = timeline_cities.repair_stays(
        raw_stays,
        raw_positions=raw_positions,
        normalizer=normalizer,
        timezone_resolver=timezone_resolver,
    )

    assert fixed_stays == [
        timeline_cities.Stay(
            date(2026, 7, 17), date(2026, 7, 20), "Budapest", "Hungary"
        )
    ]
    assert [record.action for record in audit] == ["kept", "fixed", "kept"]
    assert audit[1].fixed_city == "Budapest"
    assert audit[1].raw_support.total_points == 3
    assert audit[1].raw_support.dominant_ratio == 1.0

    disabled_stays, disabled_audit = timeline_cities.repair_stays(
        raw_stays,
        max_isolated_days=0,
        raw_positions=raw_positions,
        normalizer=normalizer,
        timezone_resolver=timezone_resolver,
    )
    assert disabled_stays == raw_stays
    assert all(record.action == "kept" for record in disabled_audit)


def test_run_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    input_path = tmp_path / "timeline.json"
    output_prefix = tmp_path / "cities.csv"
    raw_path = tmp_path / "cities.raw.csv"
    audit_path = tmp_path / "cities.audit.csv"
    fixed_path = tmp_path / "cities.fixed.csv"
    input_path.write_text(
        json.dumps(
            {
                "semanticSegments": [],
                "rawSignals": [
                    {
                        "position": {
                            "LatLng": "47.5°, 19.05°",
                            "timestamp": "2026-07-17T15:57:58.174Z",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = timeline_cities.run(
        [
            str(input_path),
            "--output",
            str(output_prefix),
            "--timezone",
            "Europe/Budapest",
        ]
    )

    assert result == 0
    row = next(csv.DictReader(io.StringIO(raw_path.read_text(encoding="utf-8"))))
    assert row["Arrival date"] == "2026-07-17"
    assert row["Departure date"] == "2026-07-17"
    assert row["City"] == "Budapest"
    assert audit_path.exists()
    assert fixed_path.exists()


def test_run_does_not_overwrite_without_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    input_path = tmp_path / "timeline.json"
    input_path.write_text(
        json.dumps(
            {
                "semanticSegments": [],
                "rawSignals": [
                    {
                        "position": {
                            "LatLng": "47.5°, 19.05°",
                            "timestamp": "2026-07-17T15:57:58.174Z",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    raw_path = tmp_path / "cities.raw.csv"
    raw_path.write_text("keep this file", encoding="utf-8")
    monkeypatch.setattr(timeline_cities.sys, "stdin", io.StringIO("n\n"))

    result = timeline_cities.run(
        [
            str(input_path),
            "--output",
            str(tmp_path / "cities"),
            "--timezone",
            "Europe/Budapest",
        ]
    )

    assert result == 1
    assert raw_path.read_text(encoding="utf-8") == "keep this file"


def test_confirm_overwrite_accepts_yes_and_defaults_to_no(tmp_path: Path) -> None:
    existing_path = tmp_path / "existing.csv"
    existing_path.write_text("existing", encoding="utf-8")

    no_output = io.StringIO()
    assert not timeline_cities.confirm_overwrite(
        [existing_path],
        input_file=io.StringIO("\n"),
        output_file=no_output,
    )
    assert str(existing_path) in no_output.getvalue()

    yes_output = io.StringIO()
    assert timeline_cities.confirm_overwrite(
        [existing_path],
        input_file=io.StringIO("yes\n"),
        output_file=yes_output,
    )


def test_run_auto_discovers_overrides_from_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "timeline.json"
    output_prefix = tmp_path / "cities"
    input_path.write_text(
        json.dumps(
            {
                "semanticSegments": [],
                "rawSignals": [
                    {
                        "position": {
                            "LatLng": "47.5°, 19.05°",
                            "timestamp": "2026-07-17T15:57:58.174Z",
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    override_directory = tmp_path / "inputs" / "overrides"
    override_directory.mkdir(parents=True)
    (override_directory / "place-mappings.csv").write_text(
        "From city,From country,To city,To country\n"
        "Budapest,Hungary,Budapest Metro,Hungary\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = timeline_cities.run(
        [
            str(input_path),
            "--output",
            str(output_prefix),
            "--timezone",
            "Europe/Budapest",
        ]
    )

    assert result == 0
    raw_row = next(
        csv.DictReader(
            (tmp_path / "cities.raw.csv").read_text(encoding="utf-8").splitlines()
        )
    )
    fixed_row = next(
        csv.DictReader(
            (tmp_path / "cities.fixed.csv").read_text(encoding="utf-8").splitlines()
        )
    )
    audit_rows = list(
        csv.DictReader(
            (tmp_path / "cities.audit.csv").read_text(encoding="utf-8").splitlines()
        )
    )
    assert raw_row["City"] == "Budapest"
    assert fixed_row["City"] == "Budapest Metro"
    assert audit_rows[-1]["Action"] == "manual mapping"


def test_run_reports_export_without_positions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "empty.json"
    input_path.write_text(
        '{"semanticSegments": [], "rawSignals": []}',
        encoding="utf-8",
    )

    result = timeline_cities.run([str(input_path)])

    assert result == 1
    assert "no usable position signals found" in capsys.readouterr().err


def test_rejects_timeline_edits_export(tmp_path: Path) -> None:
    input_path = tmp_path / "legacy.json"
    input_path.write_text('{"timelineEdits": []}', encoding="utf-8")

    with pytest.raises(ValueError, match="semanticSegments, rawSignals"):
        timeline_cities.load_positions(input_path, max_accuracy_m=50_000)


def test_rejects_non_object_or_array_json(tmp_path: Path) -> None:
    input_path = tmp_path / "scalar.json"
    input_path.write_text('"not an export"', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object or array"):
        timeline_cities.load_positions(input_path, max_accuracy_m=50_000)


def test_sample_weight_uses_interval_and_caps_long_gap() -> None:
    positions = [
        Position(datetime(2026, 1, 1, 10, 0, tzinfo=UTC), 0, 0, None),
        Position(datetime(2026, 1, 1, 10, 30, tzinfo=UTC), 0, 0, None),
        Position(datetime(2026, 1, 2, 10, 30, tzinfo=UTC), 0, 0, None),
    ]

    assert timeline_cities._sample_seconds(positions, 0, 10_800) == 1_800
    assert timeline_cities._sample_seconds(positions, 1, 10_800) == 60
    assert timeline_cities._sample_seconds(positions, 2, 10_800) == 60


def test_aggregates_nearby_consecutive_points_and_preserves_boundary() -> None:
    positions = [
        Position(datetime(2026, 1, 1, 10, 0, tzinfo=UTC), 47.5000, 19.0000, None),
        Position(datetime(2026, 1, 1, 10, 20, tzinfo=UTC), 47.5001, 19.0001, None),
        Position(datetime(2026, 1, 1, 10, 40, tzinfo=UTC), 47.5002, 19.0002, None),
        Position(datetime(2026, 1, 1, 10, 50, tzinfo=UTC), 47.6000, 19.1000, None),
    ]

    aggregated = timeline_cities.aggregate_stationary_positions(
        positions,
        radius_m=500,
        cluster_gap_seconds=3_600,
        max_sample_gap_seconds=10_800,
    )

    assert len(aggregated) == 2
    assert aggregated[0].sample_count == 3
    assert aggregated[0].latitude == pytest.approx(47.5001)
    assert aggregated[0].longitude == pytest.approx(19.0001)
    assert aggregated[0].timestamp == positions[0].timestamp
    assert aggregated[0].end_timestamp == positions[3].timestamp
    assert aggregated[1].sample_count == 1
    assert aggregated[1].end_timestamp == datetime(
        2026,
        1,
        1,
        10,
        51,
        tzinfo=UTC,
    )


def test_explicit_visit_is_split_across_local_midnight() -> None:
    position = Position(
        timestamp=datetime(2026, 1, 1, 22, 30, tzinfo=UTC),
        latitude=47.5,
        longitude=19.05,
        accuracy_m=None,
        end_timestamp=datetime(2026, 1, 2, 1, 30, tzinfo=UTC),
    )

    parts = timeline_cities._split_interval_by_local_day(
        position,
        TimezoneResolver("Europe/Budapest"),
    )

    assert parts == [
        (date(2026, 1, 1), "Europe/Budapest", 1_800),
        (date(2026, 1, 2), "Europe/Budapest", 9_000),
    ]


def _match(
    city: str,
    *,
    population: int,
    distance_km: float,
    rule: str,
) -> CityMatch:
    country_code = "AT" if city == "Vienna" else "HU"
    country = "Austria" if country_code == "AT" else "Hungary"
    return CityMatch(
        city=city,
        state="",
        country=country,
        country_code=country_code,
        latitude=47.5,
        longitude=19.0,
        population=population,
        distance_km=distance_km,
        rule=rule,
    )


def _score(match: CityMatch, seconds: float = 3_600) -> CityScore:
    score = CityScore()
    score.add(match, "UTC", seconds)
    return score
