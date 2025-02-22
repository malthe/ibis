# list justfile recipes
default:
    just --list

# clean untracked files
clean:
    git clean -fdx -e 'ci/ibis-testing-data'

# lock dependencies without updating existing versions
lock:
    poetry lock --no-update
    poetry export --extras all --with dev --with test --with docs --without-hashes --no-ansi > requirements.txt

# show all backends
@list-backends:
    yj -tj < pyproject.toml | \
        jq -rcM '.tool.poetry.plugins["ibis.backends"] | keys[]' | grep -v '^spark' | sort

# format code
fmt:
    black .
    ruff --fix .

# run all non-backend tests; additional arguments are forwarded to pytest
check *args:
    pytest -m core {{ args }}

# run pytest for ci; additional arguments are forwarded to pytest
ci-check *args:
    poetry run pytest --junitxml=junit.xml --cov=ibis --cov-report=xml:coverage.xml {{ args }}

# lint code
lint:
    black -q . --check
    ruff .

# run the test suite for one or more backends
test +backends:
    #!/usr/bin/env bash
    set -euo pipefail

    pytest_args=("-m" "$(sed 's/ / or /g' <<< '{{ backends }}')")

    if ! [[ "{{ backends }}" =~ impala|pyspark ]]; then
        pytest_args+=("-n" "auto" "-q" "--dist" "loadgroup")
    fi

    pytest "${pytest_args[@]}"

# run doctests
doctest *args:
    #!/usr/bin/env bash

    # TODO(cpcloud): why doesn't pytest --ignore-glob=test_*.py work?
    mapfile -t doctest_modules < <(
      find \
        ibis \
        -wholename '*.py' \
        -and -not -wholename '*test*.py' \
        -and -not -wholename '*__init__*' \
        -and -not -wholename '*gen_*.py'
    )
    pytest --doctest-modules {{ args }} "${doctest_modules[@]}"

# download testing data
download-data owner="ibis-project" repo="testing-data" rev="master":
    #!/usr/bin/env bash
    outdir="{{ justfile_directory() }}/ci/ibis-testing-data"
    rm -rf "$outdir"
    url="https://github.com/{{ owner }}/{{ repo }}"

    args=("$url")
    if [ "{{ rev }}" = "master" ]; then
        args+=("--depth" "1")
    fi
    args+=("$outdir")
    git clone "${args[@]}"

# start backends using docker compose; no arguments starts all backends
up *backends:
    docker compose up --wait {{ backends }}

# stop and remove containers -> clean up dangling volumes -> start backends
reup *backends:
    just down {{ backends }}
    docker system prune --force --volumes
    just up {{ backends }}

# stop and remove containers; clean up networks and volumes
down *backends:
    #!/usr/bin/env bash
    if [ -z "{{ backends }}" ]; then
        docker compose down --volumes --remove-orphans
    else
        docker compose rm {{ backends }} --force --stop --volumes
    fi

# run the benchmark suite
bench +args='ibis/tests/benchmarks':
    pytest --benchmark-only --benchmark-enable --benchmark-autosave {{ args }}

# run benchmarks and compare with a previous run
benchcmp *args:
    just bench {{ args }} --benchmark-compare

# check for invalid links in a locally built version of the docs
checklinks *args:
    #!/usr/bin/env bash
    mapfile -t files < <(find site -name '*.html')
    lychee --base site "${files[@]}" {{ args }}

# view the changelog for upcoming release (use --pretty to format with glow)
view-changelog flags="":
    #!/usr/bin/env bash
    npx -y -p conventional-changelog-cli \
        -- conventional-changelog --config ./.conventionalcommits.js \
        | ([ "{{ flags }}" = "--pretty" ] && glow -p - || cat -)
