#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'usage: %s INPUT.csv\n' "$0" >&2
    exit 2
fi

input=$1
if [[ ! -f $input ]]; then
    printf 'error: input file not found: %s\n' "$input" >&2
    exit 1
fi

case $input in
    *.csv) output=${input%.csv}.truncated-nomad-list.csv ;;
    *)
        printf 'error: input file must end in .csv: %s\n' "$input" >&2
        exit 1
        ;;
esac

python3 - "$input" "$output" <<'PY'
import csv
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
cutoff = date(2023, 7, 17)

with input_path.open("r", encoding="utf-8-sig", newline="") as input_file:
    reader = csv.reader(input_file)
    try:
        header = next(reader)
    except StopIteration:
        raise SystemExit("error: input CSV is empty")

    try:
        arrival_index = header.index("Arrival date")
    except ValueError as error:
        raise SystemExit("error: input CSV has no 'Arrival date' column") from error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        try:
            writer = csv.writer(temporary_file, quoting=csv.QUOTE_ALL)
            writer.writerow(header)
            for line_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                if arrival_index >= len(row):
                    raise SystemExit(
                        f"error: row {line_number} has no Arrival date value"
                    )
                try:
                    arrival_date = date.fromisoformat(row[arrival_index])
                except ValueError as error:
                    raise SystemExit(
                        f"error: invalid Arrival date on row {line_number}: "
                        f"{row[arrival_index]!r}"
                    ) from error
                if arrival_date >= cutoff:
                    writer.writerow(row)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

os.replace(temporary_path, output_path)
print(output_path)
PY
