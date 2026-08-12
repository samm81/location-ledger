# Contributing

Run these checks from the repository root before submitting a change:

```console
uv run --with ijson --with reverse-geocode --with timezonefinder \
  --with pytest --with pytest-cov \
  pytest --cov=timeline_cities --cov-fail-under=80
uv run --with ruff ruff check timeline_cities.py tests
uv run --with ruff ruff format --check timeline_cities.py tests
uv run --with ty ty check timeline_cities.py
```
