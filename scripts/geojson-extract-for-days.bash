#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  printf 'usage: %s START_DATE END_DATE INPUT_FILE\n' "$0" >&2
  exit 2
fi

start_date=$1
end_date=$2
input_file=$3
output_file=points.geojson

date_pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
if [[ ! $start_date =~ $date_pattern || ! $end_date =~ $date_pattern ]]; then
  printf 'error: dates must use YYYY-MM-DD format\n' >&2
  exit 2
fi

if [[ $start_date > $end_date ]]; then
  printf 'error: START_DATE must not be later than END_DATE\n' >&2
  exit 2
fi

if [[ ! -f $input_file ]]; then
  printf 'error: input file not found: %s\n' "$input_file" >&2
  exit 1
fi

jq --arg start_date "$start_date" --arg end_date "$end_date" '
{
  type: "FeatureCollection",
  features: [
    .semanticSegments[]
    | select((.startTime[0:10] >= $start_date)
        and (.startTime[0:10] <= $end_date))
    | .timelinePath[]?
    | . as $item
    | ($item.point
        | capture("(?<lat>-?[0-9.]+)°,\\s*(?<lon>-?[0-9.]+)°")
      ) as $p
    | {
        type: "Feature",
        properties: {
          time: $item.time
        },
        geometry: {
          type: "Point",
          coordinates: [
            ($p.lon | tonumber),
            ($p.lat | tonumber)
          ]
        }
      }
  ]
}
' "$input_file" > "$output_file"

printf '%s\n' "$output_file"
