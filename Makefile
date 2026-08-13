# https://tech.davis-hansson.com/p/make/
SHELL := bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules

APP_NAME := timeline-cities
BUILD_DIRECTORY ?= _build
BUNDLE_DIRECTORY := $(BUILD_DIRECTORY)/$(APP_NAME)
UV_VERSION ?= 0.11.15
UV_TARGET ?= x86_64-unknown-linux-gnu
UV_RELEASE_BASE_URL ?= https://github.com/astral-sh/uv/releases/download
UV_ARCHIVE_NAME := uv-$(UV_TARGET).tar.gz
UV_DOWNLOAD_DIRECTORY := $(BUILD_DIRECTORY)/uv-$(UV_VERSION)-$(UV_TARGET)
UV_ARCHIVE_PATH := $(UV_DOWNLOAD_DIRECTORY)/$(UV_ARCHIVE_NAME)
UV_CHECKSUM_PATH := $(UV_ARCHIVE_PATH).sha256
UV_BINARY_PATH := $(UV_DOWNLOAD_DIRECTORY)/uv-$(UV_TARGET)/uv
DEPLOY_PATH ?=

BUNDLE_FILES :=
BUNDLE_FILES += timeline_cities.py
BUNDLE_FILES += timeline_cities.py.lock
BUNDLE_FILES += README.md
BUNDLE_OUTPUTS := $(BUNDLE_FILES:%=$(BUNDLE_DIRECTORY)/%)
BUNDLE_OUTPUTS += $(BUNDLE_DIRECTORY)/bin/uv

## public

.PHONY: build build/prod
build build/prod: $(BUNDLE_OUTPUTS)

.PHONY: lock
lock: timeline_cities.py.lock

timeline_cities.py.lock: timeline_cities.py
	uv lock --script timeline_cities.py

.PHONY: deploy/prod
deploy/prod: build/prod
	deploy_path="$(DEPLOY_PATH)"
	if [[ -z "$$deploy_path" ]]; then
		echo 'error: set DEPLOY_PATH to the target application directory' >&2
		exit 1
	fi

	rsync \
		--archive \
		--delete \
		--human-readable \
		--verbose \
		"$(BUNDLE_DIRECTORY)/" \
		"$${deploy_path%/}/"

.PHONY: test
test:
	uv run \
		--with ijson \
		--with reverse-geocode \
		--with timezonefinder \
		--with pytest \
		--with pytest-cov \
		pytest --cov=timeline_cities --cov-fail-under=80

.PHONY: lint
lint:
	uv run --with ruff ruff check timeline_cities.py tests
	uv run --with ruff ruff format --check timeline_cities.py tests
	uv run --with ty ty check timeline_cities.py

.PHONY: format
format:
	uv run --with ruff ruff format timeline_cities.py tests

.PHONY: check
check: test lint

.PHONY: clean
clean:
	rm -rf -- "$(BUILD_DIRECTORY)"

## private

$(BUNDLE_DIRECTORY)/bin/uv: $(UV_BINARY_PATH)
	@mkdir -p -- "$(@D)"
	install -m 0755 "$<" "$@"

$(BUNDLE_DIRECTORY)/%: %
	@mkdir -p -- "$(@D)"
	install -m 0644 "$<" "$@"

$(UV_ARCHIVE_PATH):
	command -v curl >/dev/null 2>&1
	mkdir -p -- "$(dir $@)"
	curl --fail --location --retry 3 --silent --show-error \
		--output "$@" \
		"$(UV_RELEASE_BASE_URL)/$(UV_VERSION)/$(UV_ARCHIVE_NAME)"

$(UV_CHECKSUM_PATH):
	command -v curl >/dev/null 2>&1
	mkdir -p -- "$(dir $@)"
	curl --fail --location --retry 3 --silent --show-error \
		--output "$@" \
		"$(UV_RELEASE_BASE_URL)/$(UV_VERSION)/$(UV_ARCHIVE_NAME).sha256"

$(UV_BINARY_PATH): $(UV_ARCHIVE_PATH) $(UV_CHECKSUM_PATH)
	command -v tar >/dev/null 2>&1
	if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
		echo 'error: sha256sum or shasum is required' >&2
		exit 1
	fi

	uv_download_directory="$(UV_DOWNLOAD_DIRECTORY)"
	uv_archive_path="$(UV_ARCHIVE_PATH)"
	uv_checksum_path="$(UV_CHECKSUM_PATH)"
	if command -v sha256sum >/dev/null 2>&1; then
		(
			cd "$$uv_download_directory"
			sha256sum --check "$${uv_checksum_path##*/}"
		)
	else
		expected_checksum="$$(awk '{print $$1}' "$$uv_checksum_path")"
		actual_checksum="$$(shasum --algorithm 256 "$$uv_archive_path" | awk '{print $$1}')"
		if [[ "$$expected_checksum" != "$$actual_checksum" ]]; then
			echo 'error: uv archive checksum mismatch' >&2
			exit 1
		fi
	fi

	tar --extract --gzip --file "$$uv_archive_path" --directory "$$uv_download_directory"
	test -x "$@"
	touch "$@"
