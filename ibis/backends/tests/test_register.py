from __future__ import annotations

import contextlib
import csv
import gzip
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest import param

from ibis.backends.conftest import TEST_TABLES

if TYPE_CHECKING:
    import pyarrow as pa

pytestmark = pytest.mark.notimpl(["druid"])


@contextlib.contextmanager
def pushd(new_dir):
    previous_dir = os.getcwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(previous_dir)


@pytest.fixture
def gzip_csv(data_directory, tmp_path):
    basename = "diamonds.csv"
    f = tmp_path.joinpath(f"{basename}.gz")
    data = data_directory.joinpath(basename).read_bytes()
    f.write_bytes(gzip.compress(data))
    return str(f.absolute())


@pytest.mark.parametrize(
    ("fname", "in_table_name", "out_table_name"),
    [
        param("diamonds.csv", None, "ibis_read_csv_", id="default"),
        param("csv://diamonds.csv", "Diamonds2", "Diamonds2", id="csv_name"),
        param(
            "file://diamonds.csv",
            "fancy_stones",
            "fancy_stones",
            id="file_name",
        ),
        param(
            "file://diamonds.csv",
            "fancy stones",
            "fancy stones",
            id="file_atypical_name",
        ),
        param(
            ["file://diamonds.csv", "diamonds.csv"],
            "fancy stones",
            "fancy stones",
            id="multi_csv",
            marks=pytest.mark.notyet(
                ["polars", "datafusion"],
                reason="doesn't accept multiple files to scan or read",
            ),
        ),
    ],
)
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_csv(con, data_directory, fname, in_table_name, out_table_name):
    with pushd(data_directory):
        table = con.register(fname, table_name=in_table_name)

    assert any(t.startswith(out_table_name) for t in con.list_tables())
    if con.name != "datafusion":
        table.count().execute()


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_csv_gz(con, data_directory, gzip_csv):
    with pushd(data_directory):
        table = con.register(gzip_csv)

    assert table.count().execute()


@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_with_dotted_name(con, data_directory, tmp_path):
    basename = "foo.bar.baz/diamonds.csv"
    f = tmp_path.joinpath(basename)
    f.parent.mkdir()
    data = data_directory.joinpath("diamonds.csv").read_bytes()
    f.write_bytes(data)
    table = con.register(str(f.absolute()))

    if con.name != "datafusion":
        table.count().execute()


def read_table(path: Path) -> Iterator[tuple[str, pa.Table]]:
    """For each csv `names` in `data_dir` return a `pyarrow.Table`."""
    pac = pytest.importorskip("pyarrow.csv")

    table_name = path.stem
    schema = TEST_TABLES[table_name]
    convert_options = pac.ConvertOptions(
        column_types={name: typ.to_pyarrow() for name, typ in schema.items()}
    )
    data_dir = path.parent
    return pac.read_csv(data_dir / f"{table_name}.csv", convert_options=convert_options)


@pytest.mark.parametrize(
    ("fname", "in_table_name", "out_table_name"),
    [
        pytest.param(
            "parquet://functional_alltypes.parquet",
            None,
            "ibis_read_parquet",
        ),
        ("functional_alltypes.parquet", "funk_all", "funk_all"),
        pytest.param("parquet://functional_alltypes.parq", "funk_all", "funk_all"),
        ("parquet://functional_alltypes", None, "ibis_read_parquet"),
    ],
)
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_parquet(
    con, tmp_path, data_directory, fname, in_table_name, out_table_name
):
    pq = pytest.importorskip("pyarrow.parquet")

    fname = Path(fname)
    table = read_table(data_directory / fname.name)

    pq.write_table(table, tmp_path / fname.name)

    with pushd(tmp_path):
        table = con.register(f"parquet://{fname.name}", table_name=in_table_name)

    assert any(t.startswith(out_table_name) for t in con.list_tables())

    if con.name != "datafusion":
        table.count().execute()


@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "datafusion",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "polars",  # polars supports parquet dirs, not lists of files
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_iterator_parquet(
    con,
    tmp_path,
    data_directory,
):
    pq = pytest.importorskip("pyarrow.parquet")

    table = read_table(data_directory / "functional_alltypes.csv")

    pq.write_table(table, tmp_path / "functional_alltypes.parquet")

    with pushd(tmp_path):
        table = con.register(
            ["parquet://functional_alltypes.parquet", "functional_alltypes.parquet"],
            table_name=None,
        )

    assert any(t.startswith("ibis_read_parquet") for t in con.list_tables())

    assert table.count().execute()


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_pandas(con):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})

    t = con.register(df)
    assert t.x.sum().execute() == 6

    t = con.register(df, "my_table")
    assert t.op().name == "my_table"
    assert t.x.sum().execute() == 6


@pytest.mark.notimpl(["datafusion", "polars"])
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_pyarrow_tables(con):
    pa = pytest.importorskip("pyarrow")
    pa_t = pa.Table.from_pydict({"x": [1, 2, 3], "y": ["a", "b", "c"]})

    t = con.register(pa_t)
    assert t.x.sum().execute() == 6


@pytest.mark.broken(
    ["polars"], reason="it's working but polars infers the int column as 32"
)
@pytest.mark.notimpl(["datafusion"])
@pytest.mark.notyet(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "impala",
        "mssql",
        "mysql",
        "pandas",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_csv_reregister_schema(con, tmp_path):
    foo = tmp_path.joinpath("foo.csv")
    with foo.open("w", newline="") as csvfile:
        csv.writer(csvfile, delimiter=",").writerows(
            [
                ["cola", "colb", "colc"],
                [0, 1, 2],
                [1, 5, 6],
                [2, 3.0, "bar"],
            ]
        )

    # For a full file scan, expect correct schema based on final row
    # We also use the same `table_name` for both tests to ensure that
    # the table is re-reflected in sqlalchemy
    foo_table = con.register(foo, table_name="same")
    result_schema = foo_table.schema()

    assert result_schema.names == ("cola", "colb", "colc")
    assert result_schema["cola"].is_integer()
    assert result_schema["colb"].is_float64()
    assert result_schema["colc"].is_string()

    # If file scan is limited to first two rows, should be all some kind of integer.
    # The specific type isn't so important, and may vary across backends/versions
    foo_table = con.register(foo, SAMPLE_SIZE=2, table_name="same")
    result_schema = foo_table.schema()
    assert result_schema.names == ("cola", "colb", "colc")
    assert result_schema["cola"].is_integer()
    assert result_schema["colb"].is_integer()
    assert result_schema["colc"].is_integer()


@pytest.mark.notimpl(
    [
        "bigquery",
        "clickhouse",
        "dask",
        "datafusion",
        "impala",
        "mysql",
        "mssql",
        "pandas",
        "polars",
        "postgres",
        "pyspark",
        "snowflake",
        "sqlite",
        "trino",
    ]
)
def test_register_garbage(con):
    sa = pytest.importorskip("sqlalchemy")
    with pytest.raises(
        sa.exc.OperationalError, match="No files found that match the pattern"
    ):
        con.read_csv("garbage_notafile")

    with pytest.raises(
        sa.exc.OperationalError, match="No files found that match the pattern"
    ):
        con.read_parquet("garbage_notafile")
