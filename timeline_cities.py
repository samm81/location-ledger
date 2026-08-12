#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ijson",
#     "reverse-geocode",
#     "timezonefinder",
# ]
# ///

"""Build a Nomads.com-style city timeline from Google Timeline and GPX data."""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, TextIO, cast
from zoneinfo import ZoneInfo

import ijson  # ty: ignore[unresolved-import]  # PEP 723 dependency
import reverse_geocode  # ty: ignore[unresolved-import]  # PEP 723 dependency
from timezonefinder import (  # ty: ignore[unresolved-import]  # PEP 723 dependency
    TimezoneFinder,
)

EARTH_RADIUS_KM = 6_371.0088
DEFAULT_SAMPLE_SECONDS = 60.0
LAT_LNG_PATTERN = re.compile(
    r"^\s*(?:geo:)?\s*"
    r"(?P<latitude>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*°?\s*,\s*"
    r"(?P<longitude>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*°?\s*$",
    re.IGNORECASE,
)
GPX_FILENAME_PATTERN = re.compile(r"^\d{8}\.gpx$", re.IGNORECASE)
OVERRIDES_DIRECTORY = Path("inputs") / "overrides"
PLACE_MAPPINGS_FILENAME = "place-mappings.csv"
TRIP_OVERRIDES_FILENAME = "trip-overrides.csv"
PLACE_MAPPING_FIELDS = (
    "From city",
    "From country",
    "To city",
    "To country",
)
TRIP_OVERRIDE_FIELDS = (
    "Arrival date",
    "Departure date",
    "City",
    "Country",
)
TRIP_OUTPUT_FIELDS = (
    "Arrival date",
    "Departure date",
    "Duration in days",
    "City",
    "Country",
)


@dataclass(frozen=True, slots=True)
class Position:
    """A usable position signal from the Timeline export."""

    timestamp: datetime
    latitude: float
    longitude: float
    accuracy_m: float | None
    end_timestamp: datetime | None = None
    source: str | None = None
    sample_count: int = 1


@dataclass(frozen=True, slots=True)
class CityMatch:
    """A normalized city and diagnostics for the match."""

    city: str
    state: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    population: int
    distance_km: float
    rule: str


@dataclass(slots=True)
class CityScore:
    """Accumulated evidence for a city on one local calendar day."""

    seconds: float = 0.0
    positions: int = 0
    weighted_distance_km: float = 0.0
    closest_match: CityMatch | None = None
    rules: Counter[str] = field(default_factory=Counter)
    timezones: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        match: CityMatch,
        timezone_name: str,
        seconds: float,
        *,
        sample_count: int = 1,
    ) -> None:
        """Add one position's evidence."""
        self.seconds += seconds
        self.positions += max(sample_count, 1)
        self.weighted_distance_km += match.distance_km * seconds
        self.rules[match.rule] += seconds
        self.timezones[timezone_name] += seconds
        if (
            self.closest_match is None
            or match.distance_km < self.closest_match.distance_km
        ):
            self.closest_match = match


@dataclass(frozen=True, slots=True)
class Stay:
    """A city stay with transition dates shared by adjacent stays."""

    arrival_date: date
    departure_date: date
    city: str
    country: str

    @property
    def duration_days(self) -> int:
        """Return the elapsed calendar-day distance between the endpoints."""
        return (self.departure_date - self.arrival_date).days


@dataclass(frozen=True, slots=True)
class TripOverride:
    """A manually forced or deleted stay for an inclusive date range."""

    arrival_date: date
    departure_date: date
    city: str
    country: str

    @property
    def is_delete(self) -> bool:
        """Whether this override deletes rather than replaces a stay."""
        return not self.city and not self.country


PlaceMappingTable = dict[tuple[str, str], tuple[str, str]]


@dataclass(frozen=True, slots=True)
class LoadedPositionData:
    """Primary location evidence plus raw signals available for validation."""

    positions: list[Position]
    raw_positions: list[Position]
    source: str


@dataclass(frozen=True, slots=True)
class RawSupport:
    """Raw-signal support for one normalized stay."""

    total_points: int
    candidate_points: int
    dominant_city: str = ""
    dominant_country: str = ""
    dominant_points: int = 0

    @property
    def dominant_ratio(self) -> float:
        """Return the share of raw points belonging to the dominant city."""
        if self.total_points == 0:
            return 0.0
        return self.dominant_points / self.total_points

    @property
    def has_evidence(self) -> bool:
        """Whether any usable raw points overlap the stay."""
        return self.total_points > 0

    @property
    def supports_candidate(self) -> bool:
        """Whether the dominant raw city agrees with the stay."""
        return (
            self.has_evidence
            and self.dominant_city != ""
            and self.candidate_points == self.dominant_points
        )

    @property
    def contradicts_candidate(self) -> bool:
        """Whether the dominant raw city disagrees with the stay."""
        return (
            self.has_evidence
            and self.dominant_city != ""
            and self.candidate_points < self.dominant_points
        )


RawSupportIndex = dict[date, Counter[tuple[str, str]]]


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """The raw stay and the conservative decision made for the fixed output."""

    raw_stay: Stay
    action: str
    fixed_city: str
    fixed_country: str
    confidence: str
    reason: str
    raw_support: RawSupport


@dataclass(frozen=True, slots=True)
class Settings:
    """City normalization and signal filtering settings."""

    major_population: int = 500_000
    major_radius_km: float = 75.0
    regional_population: int = 100_000
    regional_radius_km: float = 40.0
    max_accuracy_m: float = 50_000.0
    max_gap_hours: float = 3.0
    cluster_radius_m: float = 500.0
    cluster_gap_minutes: float = 60.0


class TimezoneResolver:
    """Resolve each position to its IANA timezone from its coordinates."""

    def __init__(self) -> None:
        self._finder = TimezoneFinder(in_memory=True)
        self._cache: dict[tuple[float, float], tuple[ZoneInfo, str]] = {}

    def local_date(self, position: Position) -> tuple[date, str]:
        """Return the local calendar date and timezone for a position."""
        zone, name = self.zone(position)
        return position.timestamp.astimezone(zone).date(), name

    def zone(self, position: Position) -> tuple[ZoneInfo, str]:
        """Return the timezone object and name for a position."""
        return self.zone_at(position.latitude, position.longitude)

    def zone_at(self, latitude: float, longitude: float) -> tuple[ZoneInfo, str]:
        """Return the timezone object and name for a coordinate."""
        cache_key = (round(latitude, 3), round(longitude, 3))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        name = self._finder.timezone_at(
            lat=latitude,
            lng=longitude,
        )
        if name is None:
            result = (ZoneInfo("UTC"), "UTC")
        else:
            result = (ZoneInfo(name), name)
        self._cache[cache_key] = result
        return result


class CityNormalizer:
    """Normalize coordinates to nearby major cities using offline GeoNames data."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[tuple[float, float], CityMatch] = {}

    def match(self, latitude: float, longitude: float) -> CityMatch:
        """Return a normalized city for a coordinate."""
        cache_key = (round(latitude, 3), round(longitude, 3))
        if cache_key not in self._cache:
            self._cache[cache_key] = self._match_uncached(*cache_key)
        return self._cache[cache_key]

    def _match_uncached(self, latitude: float, longitude: float) -> CityMatch:
        coordinate = (latitude, longitude)
        local = _lookup_city(coordinate, min_population=0, rule="nearest_city")

        major = _lookup_city(
            coordinate,
            min_population=self._settings.major_population,
            rule="major_city",
        )
        if (
            major.country_code == local.country_code
            and major.distance_km <= self._settings.major_radius_km
        ):
            return major

        regional = _lookup_city(
            coordinate,
            min_population=self._settings.regional_population,
            rule="regional_city",
        )
        if (
            regional.country_code == local.country_code
            and regional.distance_km <= self._settings.regional_radius_km
        ):
            return regional

        return local


def _lookup_city(
    coordinate: tuple[float, float],
    *,
    min_population: int,
    rule: str,
) -> CityMatch:
    result = cast(
        Mapping[str, Any],
        reverse_geocode.get(coordinate, min_population=min_population),
    )
    latitude = float(result["latitude"])
    longitude = float(result["longitude"])
    return CityMatch(
        city=str(result["city"]),
        state=str(result.get("state", "")),
        country=str(result["country"]),
        country_code=str(result["country_code"]),
        latitude=latitude,
        longitude=longitude,
        population=int(result.get("population", 0)),
        distance_km=haversine_km(coordinate[0], coordinate[1], latitude, longitude),
        rule=rule,
    )


def haversine_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Return the great-circle distance between two WGS84 coordinates."""
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(haversine))


def iter_json_items(path: Path, prefix: str) -> Iterator[Mapping[str, Any]]:
    """Stream mapping items from a JSON array at an ijson prefix."""
    with path.open("rb") as input_file:
        for item in ijson.items(input_file, prefix):
            if isinstance(item, Mapping):
                yield cast(Mapping[str, Any], item)


def _first_json_byte(path: Path) -> bytes:
    with path.open("rb") as input_file:
        first_byte = _first_non_whitespace_byte(input_file)
    if first_byte not in {b"{", b"["}:
        msg = "input must be a JSON object or array"
        raise ValueError(msg)
    return first_byte


def _first_non_whitespace_byte(input_file: BinaryIO) -> bytes:
    while byte := input_file.read(1):
        if not byte.isspace():
            return byte
    return b""


def _top_level_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    with path.open("rb") as input_file:
        for prefix, event, value in ijson.parse(input_file):
            if prefix == "" and event == "map_key" and isinstance(value, str):
                keys.add(value)
    return keys


def _array_kind(path: Path) -> str:
    first_item = next(iter_json_items(path, "item"), None)
    if first_item is None:
        return "unknown"
    if "position" in first_item or "wifiScan" in first_item:
        return "rawSignals"
    if {"visit", "activity", "timelinePath"} & first_item.keys():
        return "semanticSegments"
    return "unknown"


def _input_kinds(path: Path) -> list[str]:
    if _first_json_byte(path) == b"[":
        kind = _array_kind(path)
        if kind == "unknown":
            msg = "could not recognize records in the input array"
            raise ValueError(msg)
        return [kind]

    keys = _top_level_keys(path)
    kinds = [kind for kind in ("semanticSegments", "rawSignals") if kind in keys]
    if not kinds:
        msg = "input has none of: semanticSegments, rawSignals"
        raise ValueError(msg)
    return kinds


def load_position_data(
    path: Path,
    *,
    max_accuracy_m: float,
) -> LoadedPositionData:
    """Load primary evidence and any raw signals available for validation."""
    is_array = _first_json_byte(path) == b"["
    raw_positions: list[Position] = []
    semantic_positions: list[Position] = []
    for kind in _input_kinds(path):
        prefix = "item" if is_array else f"{kind}.item"
        items = iter_json_items(path, prefix)
        if kind == "semanticSegments":
            semantic_positions = extract_semantic_positions(items)
        else:
            raw_positions = extract_raw_signal_positions(
                items,
                max_accuracy_m=max_accuracy_m,
            )

    if semantic_positions:
        return LoadedPositionData(semantic_positions, raw_positions, "semanticSegments")
    if raw_positions:
        return LoadedPositionData(raw_positions, raw_positions, "rawSignals")
    return LoadedPositionData([], [], "none")


def load_positions(
    path: Path,
    *,
    max_accuracy_m: float,
) -> list[Position]:
    """Load the best available location evidence from a supported export."""
    return load_position_data(
        path,
        max_accuracy_m=max_accuracy_m,
    ).positions


def extract_raw_signal_positions(
    signals: Iterator[Mapping[str, Any]],
    *,
    max_accuracy_m: float,
) -> list[Position]:
    """Extract positions from the current phone export's rawSignals array."""
    positions: list[Position] = []
    for signal in signals:
        position = signal.get("position")
        if not isinstance(position, Mapping):
            continue
        timestamp_raw = position.get("timestamp")
        coordinate_raw = position.get("LatLng")
        if not isinstance(timestamp_raw, str) or not isinstance(coordinate_raw, str):
            continue
        coordinate = parse_lat_lng(coordinate_raw)
        if coordinate is None:
            continue
        accuracy_raw = position.get("accuracyMeters")
        accuracy_m = (
            float(accuracy_raw) if isinstance(accuracy_raw, int | float) else None
        )
        if accuracy_m is not None and accuracy_m > max_accuracy_m:
            continue
        positions.append(
            Position(
                timestamp=_parse_timestamp(timestamp_raw),
                latitude=coordinate[0],
                longitude=coordinate[1],
                accuracy_m=accuracy_m,
                source=str(position.get("source", "")) or None,
            )
        )
    positions.sort(key=lambda item: item.timestamp)
    return positions


def load_gpx_positions(directory: Path) -> list[Position]:
    """Load timestamped track points from every YYYYMMDD.gpx file."""
    if not directory.exists():
        msg = f"GPX directory does not exist: {directory}"
        raise ValueError(msg)
    if not directory.is_dir():
        msg = f"GPX path is not a directory: {directory}"
        raise ValueError(msg)

    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".gpx"
    )
    if not files:
        msg = f"GPX directory contains no .gpx files: {directory}"
        raise ValueError(msg)

    invalid_names = [
        path.name for path in files if not GPX_FILENAME_PATTERN.fullmatch(path.name)
    ]
    if invalid_names:
        msg = f"GPX filenames must use YYYYMMDD.gpx: {invalid_names[0]}"
        raise ValueError(msg)

    positions: list[Position] = []
    for path in files:
        try:
            positions.extend(_extract_gpx_file(path))
        except ET.ParseError as error:
            msg = f"invalid GPX XML in {path.name}"
            raise ValueError(msg) from error

    positions.sort(key=lambda item: item.timestamp)
    if not positions:
        msg = f"GPX directory contains no usable track points: {directory}"
        raise ValueError(msg)
    return positions


def _extract_gpx_file(path: Path) -> list[Position]:
    """Extract valid track points from one GPX XML file."""
    positions: list[Position] = []
    for _event, element in ET.iterparse(path, events=("end",)):
        if _xml_local_name(element.tag) != "trkpt":
            continue
        latitude_raw = element.get("lat")
        longitude_raw = element.get("lon")
        timestamp_raw = next(
            (child.text for child in element if _xml_local_name(child.tag) == "time"),
            None,
        )
        if (
            not isinstance(latitude_raw, str)
            or not isinstance(longitude_raw, str)
            or not isinstance(timestamp_raw, str)
        ):
            element.clear()
            continue
        try:
            latitude = float(latitude_raw)
            longitude = float(longitude_raw)
        except ValueError:
            element.clear()
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            element.clear()
            continue
        try:
            timestamp = _parse_timestamp(timestamp_raw)
        except ValueError:
            element.clear()
            continue
        positions.append(
            Position(
                timestamp=timestamp,
                latitude=latitude,
                longitude=longitude,
                accuracy_m=None,
                source=f"gpx:{path.name}",
            )
        )
        element.clear()
    return positions


def _xml_local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def merge_gpx_with_google_positions(
    google_positions: Sequence[Position],
    gpx_positions: Sequence[Position],
    *,
    max_gap_seconds: float,
    aggregate: bool = True,
    cluster_radius_m: float = 500.0,
    cluster_gap_seconds: float = 3_600.0,
) -> list[Position]:
    """Augment GPX coverage with Google positions outside it.

    Each GPX point is authoritative until the next point, capped at the
    configured interpolation gap. Google point and interval evidence is
    retained only in the uncovered gaps, so it augments rather than overrides
    the GPX stream.
    """
    if aggregate:
        authoritative_positions = aggregate_stationary_positions(
            gpx_positions,
            radius_m=cluster_radius_m,
            cluster_gap_seconds=cluster_gap_seconds,
            max_sample_gap_seconds=max_gap_seconds,
        )
        google_positions = aggregate_stationary_positions(
            google_positions,
            radius_m=cluster_radius_m,
            cluster_gap_seconds=cluster_gap_seconds,
            max_sample_gap_seconds=max_gap_seconds,
        )
    else:
        authoritative_positions = _gpx_positions_with_coverage(
            gpx_positions,
            max_gap_seconds=max_gap_seconds,
        )
    coverage = _merge_time_intervals(
        [
            (position.timestamp, position.end_timestamp)
            for position in authoritative_positions
            if position.end_timestamp is not None
        ]
    )
    augmented_positions = [
        retained
        for position in google_positions
        for retained in _retain_outside_intervals(position, coverage)
    ]
    return sorted(
        [*authoritative_positions, *augmented_positions],
        key=lambda item: item.timestamp,
    )


def aggregate_stationary_positions(
    positions: Sequence[Position],
    *,
    radius_m: float,
    cluster_gap_seconds: float,
    max_sample_gap_seconds: float,
) -> list[Position]:
    """Collapse nearby consecutive point samples into representative intervals.

    Explicit intervals are retained as barriers. Point samples are grouped only
    when each new sample is close to the cluster's first point and follows the
    previous sample within the configured time gap. This preserves movement
    boundaries while reducing the number of city and timezone lookups.
    """
    ordered = sorted(positions, key=lambda item: item.timestamp)
    aggregated: list[Position] = []
    cluster: list[Position] = []
    anchor: Position | None = None
    previous: Position | None = None

    def flush(next_timestamp: datetime | None) -> None:
        nonlocal anchor, previous
        if cluster:
            aggregated.append(
                _representative_position(
                    cluster,
                    next_timestamp=next_timestamp,
                    max_sample_gap_seconds=max_sample_gap_seconds,
                )
            )
            cluster.clear()
        anchor = None
        previous = None

    for position in ordered:
        is_interval = (
            position.end_timestamp is not None
            and position.end_timestamp > position.timestamp
        )
        if is_interval:
            flush(position.timestamp)
            aggregated.append(position)
            continue

        if not cluster:
            cluster.append(position)
            anchor = position
            previous = position
            continue

        assert anchor is not None
        assert previous is not None
        gap_seconds = (position.timestamp - previous.timestamp).total_seconds()
        distance_m = 1_000 * haversine_km(
            anchor.latitude,
            anchor.longitude,
            position.latitude,
            position.longitude,
        )
        if (
            gap_seconds < 0
            or gap_seconds > cluster_gap_seconds
            or distance_m > radius_m
        ):
            flush(position.timestamp)
            cluster.append(position)
            anchor = position
        else:
            cluster.append(position)
        previous = position

    flush(None)
    return aggregated


def _representative_position(
    cluster: Sequence[Position],
    *,
    next_timestamp: datetime | None,
    max_sample_gap_seconds: float,
) -> Position:
    """Build a weighted-mean position for one stationary cluster."""
    total_samples = sum(max(position.sample_count, 1) for position in cluster)
    latitude = (
        sum(position.latitude * max(position.sample_count, 1) for position in cluster)
        / total_samples
    )
    longitude = (
        sum(position.longitude * max(position.sample_count, 1) for position in cluster)
        / total_samples
    )
    last_timestamp = cluster[-1].timestamp
    if next_timestamp is not None:
        gap_seconds = (next_timestamp - last_timestamp).total_seconds()
    else:
        gap_seconds = 0
    if not 0 < gap_seconds <= max_sample_gap_seconds:
        gap_seconds = DEFAULT_SAMPLE_SECONDS
    source = cluster[0].source
    if any(position.source != source for position in cluster[1:]):
        source = "aggregated"
    accuracies = [
        position.accuracy_m for position in cluster if position.accuracy_m is not None
    ]
    return Position(
        timestamp=cluster[0].timestamp,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=min(accuracies) if accuracies else None,
        end_timestamp=last_timestamp + timedelta(seconds=gap_seconds),
        source=source,
        sample_count=total_samples,
    )


def _gpx_positions_with_coverage(
    positions: Sequence[Position],
    *,
    max_gap_seconds: float,
) -> list[Position]:
    """Represent each GPX observation as a bounded authoritative interval."""
    ordered = sorted(positions, key=lambda item: item.timestamp)
    covered: list[Position] = []
    for index, position in enumerate(ordered):
        sample_seconds = DEFAULT_SAMPLE_SECONDS
        if index + 1 < len(ordered):
            delta = (ordered[index + 1].timestamp - position.timestamp).total_seconds()
            if delta > 0:
                sample_seconds = min(delta, max_gap_seconds)
        covered.append(
            Position(
                timestamp=position.timestamp,
                latitude=position.latitude,
                longitude=position.longitude,
                accuracy_m=position.accuracy_m,
                end_timestamp=position.timestamp + timedelta(seconds=sample_seconds),
                source=position.source,
                sample_count=position.sample_count,
            )
        )
    return covered


def _merge_time_intervals(
    intervals: Sequence[tuple[datetime, datetime | None]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping finite time intervals."""
    ordered = sorted(
        ((start, end) for start, end in intervals if end is not None and start < end),
        key=lambda item: item[0],
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _retain_outside_intervals(
    position: Position,
    intervals: Sequence[tuple[datetime, datetime]],
) -> list[Position]:
    """Keep or split Google evidence that is not covered by GPX."""
    end = position.end_timestamp
    if end is None or end <= position.timestamp:
        if any(
            start <= position.timestamp < interval_end
            for start, interval_end in intervals
        ):
            return []
        return [position]

    retained: list[Position] = []
    cursor = position.timestamp
    for interval_start, interval_end in intervals:
        if interval_end <= cursor:
            continue
        if interval_start >= end:
            break
        if cursor < interval_start:
            retained.append(
                _copy_position_interval(position, cursor, min(interval_start, end))
            )
        cursor = max(cursor, interval_end)
        if cursor >= end:
            break
    if cursor < end:
        retained.append(_copy_position_interval(position, cursor, end))
    return retained


def _copy_position_interval(
    position: Position,
    start: datetime,
    end: datetime,
) -> Position:
    """Copy a position with a clipped interval."""
    return Position(
        timestamp=start,
        latitude=position.latitude,
        longitude=position.longitude,
        accuracy_m=position.accuracy_m,
        end_timestamp=end,
        source=position.source,
        sample_count=position.sample_count,
    )


def extract_semantic_positions(
    segments: Iterator[Mapping[str, Any]],
) -> list[Position]:
    """Extract explicit visits and path evidence from semanticSegments."""
    positions: list[Position] = []
    for segment in segments:
        start_raw = segment.get("startTime")
        end_raw = segment.get("endTime")
        start_offset = _optional_offset_minutes(
            segment.get("startTimeTimezoneUtcOffsetMinutes")
        )
        end_offset = _optional_offset_minutes(
            segment.get("endTimeTimezoneUtcOffsetMinutes")
        )
        start = _optional_timestamp(start_raw, offset_minutes=start_offset)
        end = _optional_timestamp(end_raw, offset_minutes=end_offset)

        visit_location = _nested_mapping(
            segment,
            "visit",
            "topCandidate",
            "placeLocation",
        ).get("latLng")
        if isinstance(visit_location, str) and start is not None:
            coordinate = parse_lat_lng(visit_location)
            if coordinate is not None:
                positions.append(
                    Position(
                        timestamp=start,
                        latitude=coordinate[0],
                        longitude=coordinate[1],
                        accuracy_m=None,
                        end_timestamp=end,
                    )
                )

        timeline_path = segment.get("timelinePath")
        if isinstance(timeline_path, Sequence):
            for path_point in timeline_path:
                if not isinstance(path_point, Mapping):
                    continue
                point_raw = path_point.get("point")
                point_time = _optional_timestamp(path_point.get("time"))
                if not isinstance(point_raw, str) or point_time is None:
                    continue
                coordinate = parse_lat_lng(point_raw)
                if coordinate is not None:
                    positions.append(
                        Position(
                            timestamp=point_time,
                            latitude=coordinate[0],
                            longitude=coordinate[1],
                            accuracy_m=None,
                        )
                    )

        activity = segment.get("activity")
        if isinstance(activity, Mapping) and start is not None and end is not None:
            for key, timestamp in (("start", start), ("end", end)):
                coordinate_raw = _nested_mapping(activity, key).get("latLng")
                if not isinstance(coordinate_raw, str):
                    continue
                coordinate = parse_lat_lng(coordinate_raw)
                if coordinate is not None:
                    positions.append(
                        Position(
                            timestamp=timestamp,
                            latitude=coordinate[0],
                            longitude=coordinate[1],
                            accuracy_m=None,
                        )
                    )

    positions.sort(key=lambda item: item.timestamp)
    return positions


def parse_lat_lng(value: str) -> tuple[float, float] | None:
    """Parse Google's degree-symbol or geo: latitude/longitude strings."""
    match = LAT_LNG_PATTERN.fullmatch(value)
    if match is None:
        return None
    latitude = float(match.group("latitude"))
    longitude = float(match.group("longitude"))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _nested_mapping(
    root: Mapping[str, Any],
    *keys: str,
) -> Mapping[str, Any]:
    current = root
    for key in keys:
        value = current.get(key)
        if not isinstance(value, Mapping):
            return {}
        current = cast(Mapping[str, Any], value)
    return current


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_timestamp(
    value: object,
    *,
    offset_minutes: int | None = None,
) -> datetime | None:
    if not isinstance(value, str):
        return None
    if offset_minutes is None:
        return _parse_timestamp(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    wall = parsed.replace(tzinfo=None)
    return wall.replace(tzinfo=timezone(timedelta(minutes=offset_minutes))).astimezone(
        UTC
    )


def _optional_offset_minutes(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric_value = float(value)
    if not numeric_value.is_integer() or not -24 * 60 <= numeric_value <= 24 * 60:
        return None
    return int(numeric_value)


def summarize_days(
    positions: Sequence[Position],
    *,
    normalizer: CityNormalizer,
    timezone_resolver: TimezoneResolver,
    max_gap_hours: float,
) -> dict[date, dict[tuple[str, str], CityScore]]:
    """Accumulate time-weighted city evidence by local calendar day."""
    scores: dict[date, dict[tuple[str, str], CityScore]] = defaultdict(
        lambda: defaultdict(CityScore)
    )
    max_gap_seconds = max_gap_hours * 3_600

    for index, position in enumerate(positions):
        match = normalizer.match(position.latitude, position.longitude)
        city_key = (match.city, match.country_code)
        if (
            position.end_timestamp is not None
            and position.end_timestamp > position.timestamp
        ):
            parts = _split_interval_by_local_day(position, timezone_resolver)
        else:
            seconds = _sample_seconds(positions, index, max_gap_seconds)
            local_day, timezone_name = timezone_resolver.local_date(position)
            parts = [(local_day, timezone_name, seconds)]
        for local_day, timezone_name, seconds in parts:
            scores[local_day][city_key].add(
                match,
                timezone_name,
                seconds,
                sample_count=position.sample_count,
            )

    return scores


def _split_interval_by_local_day(
    position: Position,
    timezone_resolver: TimezoneResolver,
) -> list[tuple[date, str, float]]:
    """Split an explicit visit interval at local midnight boundaries."""
    assert position.end_timestamp is not None
    zone, timezone_name = timezone_resolver.zone(position)
    cursor = position.timestamp
    parts: list[tuple[date, str, float]] = []

    while cursor < position.end_timestamp:
        local_cursor = cursor.astimezone(zone)
        next_day = local_cursor.date() + timedelta(days=1)
        next_midnight = datetime.combine(next_day, time.min, tzinfo=zone).astimezone(
            UTC
        )
        part_end = min(position.end_timestamp, next_midnight)
        seconds = (part_end - cursor).total_seconds()
        if seconds > 0:
            parts.append((local_cursor.date(), timezone_name, seconds))
        cursor = part_end

    return parts


def _sample_seconds(
    positions: Sequence[Position],
    index: int,
    max_gap_seconds: float,
) -> float:
    if index + 1 >= len(positions):
        return DEFAULT_SAMPLE_SECONDS
    delta = (
        positions[index + 1].timestamp - positions[index].timestamp
    ).total_seconds()
    if 0 < delta <= max_gap_seconds:
        return max(delta, 1.0)
    return DEFAULT_SAMPLE_SECONDS


def build_stays(
    scores: Mapping[date, Mapping[tuple[str, str], CityScore]],
) -> list[Stay]:
    """Build stays, sharing a travel date between changing cities.

    The daily score identifies the city assigned to each day, but it does not
    make a city change happen at midnight. If the incoming city has evidence on
    the previous city's final scored day, that day is the transition day for
    both stays. Otherwise the transition occurs on the incoming city's first
    scored day.
    """
    winners: list[tuple[date, CityMatch]] = []
    for local_day, cities in sorted(scores.items()):
        score = max(
            cities.values(),
            key=lambda item: (item.seconds, item.positions),
        )
        if score.closest_match is not None:
            winners.append((local_day, score.closest_match))

    if not winners:
        return []

    stays: list[Stay] = []
    arrival: date | None = None
    last_day: date | None = None
    current_match: CityMatch | None = None

    for local_day, match in winners:
        continues_stay = (
            current_match is not None
            and last_day is not None
            and local_day == last_day + timedelta(days=1)
            and match.city == current_match.city
            and match.country_code == current_match.country_code
        )
        if not continues_stay:
            arrival_date = local_day
            if (
                arrival is not None
                and last_day is not None
                and current_match is not None
            ):
                departure = last_day
                if (
                    match.city != current_match.city
                    or match.country_code != current_match.country_code
                ):
                    departure = _transition_date(
                        scores,
                        last_day=last_day,
                        incoming_day=local_day,
                        incoming_match=match,
                    )
                    arrival_date = departure
                stays.append(
                    Stay(
                        arrival_date=arrival,
                        departure_date=departure,
                        city=current_match.city,
                        country=current_match.country,
                    )
                )
            arrival = arrival_date
            current_match = match
        last_day = local_day

    assert arrival is not None
    assert last_day is not None
    assert current_match is not None
    stays.append(
        Stay(
            arrival_date=arrival,
            departure_date=last_day,
            city=current_match.city,
            country=current_match.country,
        )
    )
    return stays


def _transition_date(
    scores: Mapping[date, Mapping[tuple[str, str], CityScore]],
    *,
    last_day: date,
    incoming_day: date,
    incoming_match: CityMatch,
) -> date:
    """Find the latest incoming-city evidence before its winning day."""
    incoming_key = (incoming_match.city, incoming_match.country_code)
    candidate_days = [
        local_day
        for local_day, cities in scores.items()
        if last_day <= local_day < incoming_day and incoming_key in cities
    ]
    return max(candidate_days, default=incoming_day)


def merge_stays(stays: Sequence[Stay]) -> list[Stay]:
    """Merge adjacent stays that have the same city and country."""
    merged: list[Stay] = []
    for stay in stays:
        if (
            merged
            and merged[-1].departure_date == stay.arrival_date
            and merged[-1].city == stay.city
            and merged[-1].country == stay.country
        ):
            previous = merged[-1]
            merged[-1] = Stay(
                arrival_date=previous.arrival_date,
                departure_date=stay.departure_date,
                city=previous.city,
                country=previous.country,
            )
        else:
            merged.append(stay)
    return merged


def load_override_data(
    base_directory: Path | None = None,
) -> tuple[PlaceMappingTable, list[TripOverride]]:
    """Load optional manual overrides from the current workflow directory."""
    root = base_directory or Path.cwd()
    override_directory = root / OVERRIDES_DIRECTORY
    place_mappings_path = override_directory / PLACE_MAPPINGS_FILENAME
    trip_overrides_path = override_directory / TRIP_OVERRIDES_FILENAME
    place_mappings = (
        load_place_mappings(place_mappings_path) if place_mappings_path.exists() else {}
    )
    trip_overrides = (
        load_trip_overrides(trip_overrides_path) if trip_overrides_path.exists() else []
    )
    return place_mappings, trip_overrides


def _read_override_rows(
    path: Path,
    expected_fields: Sequence[str],
    optional_fields: set[str] | None = None,
) -> Iterator[tuple[int, dict[str, str]]]:
    """Read and validate one strict override CSV schema."""
    optional = optional_fields or set()
    with path.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames != list(expected_fields):
            actual_fields = ", ".join(reader.fieldnames or [])
            expected = ", ".join(expected_fields)
            msg = f"{path}: expected columns [{expected}], found [{actual_fields}]"
            raise ValueError(msg)
        for line_number, raw_row in enumerate(reader, start=2):
            if None in raw_row:
                msg = f"{path}: row {line_number} has extra columns"
                raise ValueError(msg)
            row: dict[str, str] = {}
            for field_name in expected_fields:
                value = raw_row.get(field_name)
                if not isinstance(value, str) or (
                    not value.strip() and field_name not in optional
                ):
                    msg = f"{path}: row {line_number} has an empty {field_name}"
                    raise ValueError(msg)
                row[field_name] = value.strip()
            yield line_number, row


def load_place_mappings(path: Path) -> PlaceMappingTable:
    """Load exact city-country mappings and resolve mapping chains."""
    mappings: PlaceMappingTable = {}
    for line_number, row in _read_override_rows(path, PLACE_MAPPING_FIELDS):
        source = (row["From city"], row["From country"])
        target = (row["To city"], row["To country"])
        if source in mappings:
            msg = f"{path}: row {line_number} duplicates mapping source {source!r}"
            raise ValueError(msg)
        if source[1] != target[1]:
            msg = f"{path}: row {line_number} changes country from {source[1]!r}"
            raise ValueError(msg)
        mappings[source] = target

    resolved: PlaceMappingTable = {}
    for source in mappings:
        current = source
        seen: set[tuple[str, str]] = set()
        while current in mappings:
            if current in seen:
                msg = f"{path}: mapping cycle includes {current!r}"
                raise ValueError(msg)
            seen.add(current)
            current = mappings[current]
        resolved[source] = current
    return resolved


def load_trip_overrides(path: Path) -> list[TripOverride]:
    """Load and validate inclusive manual trip ranges."""
    overrides: list[TripOverride] = []
    for line_number, row in _read_trip_override_rows(path):
        arrival_date = _parse_override_date(
            row["Arrival date"],
            path=path,
            line_number=line_number,
            field_name="Arrival date",
        )
        departure_date = _parse_override_date(
            row["Departure date"],
            path=path,
            line_number=line_number,
            field_name="Departure date",
        )
        if departure_date < arrival_date:
            msg = f"{path}: row {line_number} departs before it arrives"
            raise ValueError(msg)
        if bool(row["City"]) != bool(row["Country"]):
            msg = (
                f"{path}: row {line_number} must blank both City and Country to delete"
            )
            raise ValueError(msg)
        overrides.append(
            TripOverride(
                arrival_date=arrival_date,
                departure_date=departure_date,
                city=row["City"],
                country=row["Country"],
            )
        )

    ordered = sorted(
        overrides,
        key=lambda item: (item.arrival_date, item.departure_date),
    )
    latest: TripOverride | None = None
    for current in ordered:
        if latest is not None and current.arrival_date < latest.departure_date:
            msg = f"{path}: overlapping trip override ranges"
            raise ValueError(msg)
        if latest is None or current.departure_date > latest.departure_date:
            latest = current
    return ordered


def _read_trip_override_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    """Read four-column overrides and copied five-column stay rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.reader(input_file)
        try:
            header = next(reader)
        except StopIteration as error:
            msg = f"{path}: override CSV is empty"
            raise ValueError(msg) from error
        if header not in (list(TRIP_OVERRIDE_FIELDS), list(TRIP_OUTPUT_FIELDS)):
            expected = ", ".join(TRIP_OVERRIDE_FIELDS)
            msg = f"{path}: expected columns [{expected}]"
            raise ValueError(msg)

        for line_number, values in enumerate(reader, start=2):
            if not values:
                continue
            if header == list(TRIP_OVERRIDE_FIELDS) and len(values) == 5:
                if not _is_duration_value(values[2]):
                    msg = f"{path}: row {line_number} has an invalid duration column"
                    raise ValueError(msg)
                values = [values[0], values[1], values[3], values[4]]
            elif header == list(TRIP_OUTPUT_FIELDS) and len(values) == 5:
                if values[2].strip() and not _is_duration_value(values[2]):
                    msg = f"{path}: row {line_number} has an invalid duration column"
                    raise ValueError(msg)
                values = [values[0], values[1], values[3], values[4]]
            elif len(values) != len(TRIP_OVERRIDE_FIELDS):
                msg = f"{path}: row {line_number} has the wrong number of columns"
                raise ValueError(msg)

            row = {
                field_name: value.strip()
                for field_name, value in zip(TRIP_OVERRIDE_FIELDS, values)
            }
            for field_name in ("Arrival date", "Departure date"):
                if not row[field_name]:
                    msg = f"{path}: row {line_number} has an empty {field_name}"
                    raise ValueError(msg)
            if bool(row["City"]) != bool(row["Country"]):
                msg = f"{path}: row {line_number} must blank both City and Country to delete"
                raise ValueError(msg)
            yield line_number, row


def _is_duration_value(value: str) -> bool:
    try:
        int(value.strip())
    except ValueError:
        return False
    return True


def _parse_override_date(
    value: str,
    *,
    path: Path,
    line_number: int,
    field_name: str,
) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        msg = f"{path}: row {line_number} has invalid {field_name}: {value!r}"
        raise ValueError(msg) from error


def apply_place_mappings(
    stays: Sequence[Stay],
    mappings: Mapping[tuple[str, str], tuple[str, str]],
) -> tuple[list[Stay], list[AuditRecord]]:
    """Apply persistent place mappings to fixed stays and audit changes."""
    mapped_stays: list[Stay] = []
    audit: list[AuditRecord] = []
    for stay in stays:
        source = (stay.city, stay.country)
        target = mappings.get(source, source)
        mapped = Stay(
            arrival_date=stay.arrival_date,
            departure_date=stay.departure_date,
            city=target[0],
            country=target[1],
        )
        mapped_stays.append(mapped)
        if target != source:
            audit.append(
                _manual_audit_record(
                    raw_stay=stay,
                    fixed_stay=mapped,
                    action="manual mapping",
                    reason=(
                        f"persistent place mapping: {source[0]}, {source[1]} "
                        f"→ {target[0]}, {target[1]}"
                    ),
                )
            )
    return merge_stays(mapped_stays), audit


def apply_trip_overrides(
    stays: Sequence[Stay],
    overrides: Sequence[TripOverride],
    mappings: Mapping[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[list[Stay], list[AuditRecord]]:
    """Replace inclusive date ranges with manually forced city stays."""
    current_stays = list(stays)
    audit: list[AuditRecord] = []
    place_mappings = mappings or {}
    ordered_overrides = sorted(
        overrides,
        key=lambda item: (item.arrival_date, item.departure_date),
    )
    for override in ordered_overrides:
        source_target = (override.city, override.country)
        target = (
            ("", "")
            if override.is_delete
            else place_mappings.get(source_target, source_target)
        )
        forced_stay = Stay(
            arrival_date=override.arrival_date,
            departure_date=override.departure_date,
            city=target[0],
            country=target[1],
        )
        remaining: list[Stay] = []
        for stay in current_stays:
            if override.is_delete and override.arrival_date == override.departure_date:
                if (
                    stay.arrival_date == override.arrival_date
                    and stay.departure_date == override.departure_date
                ):
                    continue
                remaining.append(stay)
                continue
            outside_before = stay.departure_date < forced_stay.arrival_date
            outside_after = stay.arrival_date > forced_stay.departure_date
            if outside_before or outside_after:
                remaining.append(stay)
                continue
            if stay.arrival_date < forced_stay.arrival_date:
                remaining.append(
                    Stay(
                        arrival_date=stay.arrival_date,
                        departure_date=forced_stay.arrival_date,
                        city=stay.city,
                        country=stay.country,
                    )
                )
            if stay.departure_date > forced_stay.departure_date:
                remaining.append(
                    Stay(
                        arrival_date=forced_stay.departure_date,
                        departure_date=stay.departure_date,
                        city=stay.city,
                        country=stay.country,
                    )
                )
        if not override.is_delete:
            remaining.append(forced_stay)
        current_stays = merge_stays(
            sorted(
                remaining,
                key=lambda item: (
                    item.arrival_date,
                    item.departure_date,
                    item.city,
                    item.country,
                ),
            )
        )
        if override.is_delete:
            action = "manual delete"
            reason = (
                f"deleted the zero-day stay on {override.arrival_date.isoformat()}"
                if override.arrival_date == override.departure_date
                else (
                    f"deleted stays from {override.arrival_date.isoformat()} through "
                    f"{override.departure_date.isoformat()}"
                )
            )
        else:
            action = "manual trip override"
            target_description = f"{target[0]}, {target[1]}"
            source_description = f"{override.city}, {override.country}"
            reason = (
                f"forced {override.arrival_date.isoformat()} through "
                f"{override.departure_date.isoformat()} as {target_description}"
            )
            if source_description != target_description:
                reason += f" (canonicalized from {source_description})"
        audit.append(
            _manual_audit_record(
                raw_stay=forced_stay,
                fixed_stay=forced_stay,
                action=action,
                reason=reason,
            )
        )
    return current_stays, audit


def _manual_audit_record(
    *,
    raw_stay: Stay,
    fixed_stay: Stay,
    action: str,
    reason: str,
) -> AuditRecord:
    """Create an audit row for a manual change without raw-signal evidence."""
    return AuditRecord(
        raw_stay=raw_stay,
        action=action,
        fixed_city=fixed_stay.city,
        fixed_country=fixed_stay.country,
        confidence="manual",
        reason=reason,
        raw_support=RawSupport(total_points=0, candidate_points=0),
    )


def raw_support_for_stay(
    stay: Stay,
    raw_positions: Sequence[Position],
    *,
    normalizer: CityNormalizer,
    timezone_resolver: TimezoneResolver,
    max_accuracy_m: float = 1_000.0,
    raw_index: Mapping[date, Mapping[tuple[str, str], int]] | None = None,
) -> RawSupport:
    """Compare overlapping reliable raw points with a normalized stay."""
    if raw_index is None:
        raw_index = build_raw_support_index(
            raw_positions,
            normalizer=normalizer,
            timezone_resolver=timezone_resolver,
            max_accuracy_m=max_accuracy_m,
        )

    counts: Counter[tuple[str, str]] = Counter()
    candidate_key = (stay.city, stay.country)
    local_day = stay.arrival_date
    while local_day <= stay.departure_date:
        counts.update(raw_index.get(local_day, {}))
        local_day += timedelta(days=1)

    if not counts:
        return RawSupport(total_points=0, candidate_points=0)

    dominant_key, dominant_points = counts.most_common(1)[0]
    return RawSupport(
        total_points=sum(counts.values()),
        candidate_points=counts.get(candidate_key, 0),
        dominant_city=dominant_key[0],
        dominant_country=dominant_key[1],
        dominant_points=dominant_points,
    )


def build_raw_support_index(
    raw_positions: Sequence[Position],
    *,
    normalizer: CityNormalizer,
    timezone_resolver: TimezoneResolver,
    max_accuracy_m: float,
) -> RawSupportIndex:
    """Index reliable raw evidence once by its effective local calendar date."""
    index: defaultdict[date, Counter[tuple[str, str]]] = defaultdict(Counter)
    for position in raw_positions:
        if position.accuracy_m is not None and position.accuracy_m > max_accuracy_m:
            continue
        local_day, _ = timezone_resolver.local_date(position)
        match = normalizer.match(position.latitude, position.longitude)
        index[local_day][(match.city, match.country)] += max(
            position.sample_count,
            1,
        )
    return dict(index)


def repair_stays(
    stays: Sequence[Stay],
    *,
    max_isolated_days: int = 1,
    raw_positions: Sequence[Position] = (),
    normalizer: CityNormalizer | None = None,
    timezone_resolver: TimezoneResolver | None = None,
    raw_support_max_accuracy_m: float = 1_000.0,
    raw_support_ratio: float = 0.75,
    min_raw_support_points: int = 3,
) -> tuple[list[Stay], list[AuditRecord]]:
    """Apply only an isolated pattern contradicted by reliable raw signals.

    A short middle stay is corrected only when the surrounding stays are the same
    city, have no date gaps, the middle stay is at most ``max_isolated_days``, and
    overlapping raw signals strongly support the surrounding city. All decisions
    are recorded in the audit output.
    """
    if raw_positions and (normalizer is None or timezone_resolver is None):
        msg = "normalizer and timezone_resolver are required for raw support"
        raise ValueError(msg)

    replacements: dict[int, Stay] = {}
    audit: list[AuditRecord] = []
    raw_index: RawSupportIndex | None = None
    if normalizer is not None and timezone_resolver is not None:
        raw_index = build_raw_support_index(
            raw_positions,
            normalizer=normalizer,
            timezone_resolver=timezone_resolver,
            max_accuracy_m=raw_support_max_accuracy_m,
        )
    for index, stay in enumerate(stays):
        raw_support = RawSupport(total_points=0, candidate_points=0)
        if normalizer is not None and timezone_resolver is not None:
            raw_support = raw_support_for_stay(
                stay,
                raw_positions,
                normalizer=normalizer,
                timezone_resolver=timezone_resolver,
                max_accuracy_m=raw_support_max_accuracy_m,
                raw_index=raw_index,
            )
        replacement: Stay | None = None
        reason = "raw evidence unavailable"
        confidence = "normal"
        action = "kept"
        if raw_support.has_evidence:
            if (
                raw_support.supports_candidate
                and raw_support.dominant_ratio >= raw_support_ratio
            ):
                reason = "raw signals support the reported city"
            elif raw_support.dominant_ratio >= raw_support_ratio:
                reason = (
                    f"raw signals predominantly support {raw_support.dominant_city}"
                )
                confidence = "review"
            else:
                reason = "raw signals are mixed"
                confidence = "review"
        if 0 < index < len(stays) - 1:
            previous = stays[index - 1]
            following = stays[index + 1]
            surrounded_by_same_city = (
                previous.city == following.city
                and previous.country == following.country
                and previous.departure_date == stay.arrival_date
                and stay.departure_date == following.arrival_date
                and stay.city != previous.city
            )
            strong_contradiction = (
                raw_support.total_points >= min_raw_support_points
                and raw_support.dominant_ratio >= raw_support_ratio
                and raw_support.contradicts_candidate
                and raw_support.dominant_city == previous.city
                and raw_support.dominant_country == previous.country
            )
            if (
                surrounded_by_same_city
                and stay.duration_days <= max_isolated_days
                and strong_contradiction
            ):
                replacement = Stay(
                    arrival_date=stay.arrival_date,
                    departure_date=stay.departure_date,
                    city=previous.city,
                    country=previous.country,
                )
                replacements[index] = replacement
                action = "fixed"
                confidence = "high"
                reason = (
                    "isolated short stay contradicted by raw signals from the "
                    "surrounding city"
                )
            elif surrounded_by_same_city and stay.duration_days <= max_isolated_days:
                action = "review"
                confidence = "review"
                reason = (
                    "isolated short stay retained because raw support is absent, "
                    "mixed, or supports the reported city"
                )

        fixed_stay = replacement or stay
        audit.append(
            AuditRecord(
                raw_stay=stay,
                action=action,
                fixed_city=fixed_stay.city,
                fixed_country=fixed_stay.country,
                confidence=confidence,
                reason=reason,
                raw_support=raw_support,
            )
        )

    corrected = merge_stays(
        [replacements.get(index, stay) for index, stay in enumerate(stays)]
    )
    return corrected, audit


def write_stays_csv(stays: Sequence[Stay], output_file: TextIO) -> None:
    """Write stays using the requested five-column CSV format."""
    fieldnames = [
        "Arrival date",
        "Departure date",
        "Duration in days",
        "City",
        "Country",
    ]
    writer = csv.DictWriter(
        output_file,
        fieldnames=fieldnames,
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()

    for stay in stays:
        writer.writerow(
            {
                "Arrival date": stay.arrival_date.isoformat(),
                "Departure date": stay.departure_date.isoformat(),
                "Duration in days": stay.duration_days,
                "City": stay.city,
                "Country": stay.country,
            }
        )


def write_csv(
    scores: Mapping[date, Mapping[tuple[str, str], CityScore]],
    output_file: TextIO,
) -> None:
    """Write raw stays derived from daily scores."""
    write_stays_csv(build_stays(scores), output_file)


def write_audit_csv(records: Sequence[AuditRecord], output_file: TextIO) -> None:
    """Write the correction audit alongside the raw stays."""
    fieldnames = [
        "Arrival date",
        "Departure date",
        "Duration in days",
        "City",
        "Country",
        "Action",
        "Fixed city",
        "Fixed country",
        "Confidence",
        "Reason",
        "Raw points",
        "Raw candidate points",
        "Raw dominant city",
        "Raw dominant country",
        "Raw dominant ratio",
    ]
    writer = csv.DictWriter(
        output_file,
        fieldnames=fieldnames,
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()

    for record in records:
        stay = record.raw_stay
        writer.writerow(
            {
                "Arrival date": stay.arrival_date.isoformat(),
                "Departure date": stay.departure_date.isoformat(),
                "Duration in days": stay.duration_days,
                "City": stay.city,
                "Country": stay.country,
                "Action": record.action,
                "Fixed city": record.fixed_city,
                "Fixed country": record.fixed_country,
                "Confidence": record.confidence,
                "Reason": record.reason,
                "Raw points": record.raw_support.total_points,
                "Raw candidate points": record.raw_support.candidate_points,
                "Raw dominant city": record.raw_support.dominant_city,
                "Raw dominant country": record.raw_support.dominant_country,
                "Raw dominant ratio": f"{record.raw_support.dominant_ratio:.2f}",
            }
        )


def output_paths(
    input_path: Path | None, output_prefix: Path | None
) -> tuple[Path, Path, Path]:
    """Return raw, audit, and final-result output paths."""
    if output_prefix is not None:
        prefix = output_prefix
    elif input_path is not None:
        prefix = input_path.with_suffix("")
    else:
        prefix = Path("cities")
    if prefix.suffix.lower() == ".csv":
        prefix = prefix.with_suffix("")
    return (
        prefix.with_name(f"{prefix.name}.raw.csv"),
        prefix.with_name(f"{prefix.name}.audit.csv"),
        prefix.with_name(f"{prefix.name}.csv"),
    )


def confirm_overwrite(
    paths: Sequence[Path],
    *,
    input_file: TextIO | None = None,
    output_file: TextIO | None = None,
) -> bool:
    """Ask before replacing any existing output file."""
    existing_paths = [path for path in paths if path.exists()]
    if not existing_paths:
        return True

    input_stream = input_file or sys.stdin
    output_stream = output_file or sys.stderr
    print("The following output files already exist:", file=output_stream)
    for path in existing_paths:
        print(f"  {path}", file=output_stream)
    print("Overwrite them? [y/N] ", end="", file=output_stream, flush=True)
    answer = input_stream.readline().strip().lower()
    return answer in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Build a city timeline from Google Maps Timeline and/or GPSLogger GPX data."
        )
    )
    parser.add_argument(
        "--google-timeline",
        type=Path,
        help="Google Maps Timeline export JSON",
    )
    parser.add_argument(
        "--gpx-directory",
        "--gpx-dir",
        dest="gpx_directory",
        type=Path,
        help="directory of authoritative GPSLogger YYYYMMDD.gpx track files",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "output filename prefix (default: Google Timeline filename or cities "
            "for GPX-only runs)"
        ),
    )
    parser.add_argument("--major-population", type=int, default=500_000)
    parser.add_argument("--major-radius-km", type=float, default=75.0)
    parser.add_argument("--regional-population", type=int, default=100_000)
    parser.add_argument("--regional-radius-km", type=float, default=40.0)
    parser.add_argument(
        "--max-accuracy-m",
        type=float,
        default=50_000.0,
        help="discard less accurate position signals (default: 50000)",
    )
    parser.add_argument(
        "--max-gap-hours",
        type=float,
        default=3.0,
        help="maximum interval credited to a position (default: 3)",
    )
    parser.add_argument(
        "--cluster-radius-m",
        type=float,
        default=500.0,
        help="group consecutive samples within this radius (default: 500)",
    )
    parser.add_argument(
        "--cluster-gap-minutes",
        type=float,
        default=60.0,
        help="group consecutive samples no farther apart than this time (default: 60)",
    )
    parser.add_argument(
        "--max-isolated-days",
        type=int,
        default=1,
        help="automatically replace isolated stays up to this length (default: 1)",
    )
    return parser


def _positive(value: float, name: str) -> None:
    if value <= 0:
        msg = f"{name} must be greater than zero"
        raise ValueError(msg)


def _nonnegative(value: int, name: str) -> None:
    if value < 0:
        msg = f"{name} must not be negative"
        raise ValueError(msg)


def run(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line application."""
    args = build_parser().parse_args(arguments)
    settings = Settings(
        major_population=args.major_population,
        major_radius_km=args.major_radius_km,
        regional_population=args.regional_population,
        regional_radius_km=args.regional_radius_km,
        max_accuracy_m=args.max_accuracy_m,
        max_gap_hours=args.max_gap_hours,
        cluster_radius_m=args.cluster_radius_m,
        cluster_gap_minutes=args.cluster_gap_minutes,
    )

    try:
        if args.google_timeline is None and args.gpx_directory is None:
            msg = "provide --google-timeline and/or --gpx-directory"
            raise ValueError(msg)
        _positive(settings.major_population, "--major-population")
        _positive(settings.major_radius_km, "--major-radius-km")
        _positive(settings.regional_population, "--regional-population")
        _positive(settings.regional_radius_km, "--regional-radius-km")
        _positive(settings.max_accuracy_m, "--max-accuracy-m")
        _positive(settings.max_gap_hours, "--max-gap-hours")
        _positive(settings.cluster_radius_m, "--cluster-radius-m")
        _positive(settings.cluster_gap_minutes, "--cluster-gap-minutes")
        _nonnegative(args.max_isolated_days, "--max-isolated-days")
        timezone_resolver = TimezoneResolver()
        position_data = LoadedPositionData([], [], "none")
        if args.google_timeline is not None:
            position_data = load_position_data(
                args.google_timeline,
                max_accuracy_m=settings.max_accuracy_m,
            )
        if args.gpx_directory is not None:
            gpx_positions = load_gpx_positions(args.gpx_directory)
            position_data = LoadedPositionData(
                positions=merge_gpx_with_google_positions(
                    position_data.positions,
                    gpx_positions,
                    max_gap_seconds=settings.max_gap_hours * 3_600,
                    aggregate=True,
                    cluster_radius_m=settings.cluster_radius_m,
                    cluster_gap_seconds=settings.cluster_gap_minutes * 60,
                ),
                raw_positions=merge_gpx_with_google_positions(
                    position_data.raw_positions,
                    gpx_positions,
                    max_gap_seconds=settings.max_gap_hours * 3_600,
                    aggregate=False,
                ),
                source=f"{position_data.source}+gpx",
            )
        else:
            position_data = LoadedPositionData(
                positions=aggregate_stationary_positions(
                    position_data.positions,
                    radius_m=settings.cluster_radius_m,
                    cluster_gap_seconds=settings.cluster_gap_minutes * 60,
                    max_sample_gap_seconds=settings.max_gap_hours * 3_600,
                ),
                raw_positions=position_data.raw_positions,
                source=position_data.source,
            )
        positions = position_data.positions
        if not positions:
            msg = "no usable position signals found"
            raise ValueError(msg)

        normalizer = CityNormalizer(settings)
        scores = summarize_days(
            positions,
            normalizer=normalizer,
            timezone_resolver=timezone_resolver,
            max_gap_hours=settings.max_gap_hours,
        )
        raw_stays = build_stays(scores)
        fixed_stays, audit_records = repair_stays(
            raw_stays,
            max_isolated_days=args.max_isolated_days,
            raw_positions=position_data.raw_positions,
            normalizer=normalizer,
            timezone_resolver=timezone_resolver,
        )
        place_mappings, trip_overrides = load_override_data()
        mapped_stays, mapping_audit = apply_place_mappings(
            fixed_stays,
            place_mappings,
        )
        fixed_stays, trip_override_audit = apply_trip_overrides(
            mapped_stays,
            trip_overrides,
            place_mappings,
        )
        audit_records.extend([*mapping_audit, *trip_override_audit])
        raw_path, audit_path, final_path = output_paths(
            args.google_timeline, args.output
        )
        if not confirm_overwrite((raw_path, audit_path, final_path)):
            print(
                "aborted: existing output files were not overwritten", file=sys.stderr
            )
            return 1
        with raw_path.open("w", encoding="utf-8", newline="") as raw_file:
            write_stays_csv(raw_stays, raw_file)
        with audit_path.open("w", encoding="utf-8", newline="") as audit_file:
            write_audit_csv(audit_records, audit_file)
        with final_path.open("w", encoding="utf-8", newline="") as final_file:
            write_stays_csv(fixed_stays, final_file)
        print(f"raw: {raw_path}", file=sys.stderr)
        print(f"audit: {audit_path}", file=sys.stderr)
        print(f"final: {final_path}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
