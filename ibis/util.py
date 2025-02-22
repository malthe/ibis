"""Ibis utility functions."""
from __future__ import annotations

import abc
import collections
import functools
import importlib.metadata
import itertools
import logging
import operator
import os
import sys
import textwrap
import types
import uuid
import warnings
from numbers import Real
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
)
from uuid import uuid4

import numpy as np
import toolz

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd
    import pyarrow as pa

    import ibis.expr.operations as ops
    import ibis.expr.schema as sch

    Graph = Mapping[ops.Node, Sequence[ops.Node]]

T = TypeVar("T", covariant=True)
U = TypeVar("U", covariant=True)
K = TypeVar("K")
V = TypeVar("V")


# https://www.compart.com/en/unicode/U+22EE
VERTICAL_ELLIPSIS = "\u22EE"
# https://www.compart.com/en/unicode/U+2026
HORIZONTAL_ELLIPSIS = "\u2026"


def guid() -> str:
    """Return a uuid4 hexadecimal value."""
    return uuid4().hex


def indent(text: str, spaces: int) -> str:
    """Apply an indentation using the given spaces into the given text.

    Parameters
    ----------
    text
        Text to indent
    spaces
        Number of leading spaces per line

    Returns
    -------
    str
        Indented text
    """
    prefix = " " * spaces
    return textwrap.indent(text, prefix=prefix)


def is_one_of(values: Sequence[T], t: type[U]) -> Iterator[bool]:
    """Check if the type of each value is the same of the given type.

    Parameters
    ----------
    values
        Input values
    t
        Type to check against

    Returns
    -------
    tuple
    """
    return (isinstance(x, t) for x in values)


any_of = toolz.compose(any, is_one_of)
all_of = toolz.compose(all, is_one_of)


def promote_list(val: V | Sequence[V]) -> list[V]:
    """Ensure that the value is a list.

    Parameters
    ----------
    val
        Value to promote

    Returns
    -------
    list
    """
    if isinstance(val, list):
        return val
    elif is_iterable(val):
        return list(val)
    elif val is None:
        return []
    else:
        return [val]


def promote_tuple(val: V | Sequence[V]) -> tuple[V]:
    """Ensure that the value is a tuple.

    Parameters
    ----------
    val
        Value to promote

    Returns
    -------
    tuple
    """
    if isinstance(val, tuple):
        return val
    elif is_iterable(val):
        return tuple(val)
    elif val is None:
        return ()
    else:
        return (val,)


def is_function(v: Any) -> bool:
    """Check if the given object is a function.

    Returns
    -------
    bool
        Whether `v` is a function
    """
    return isinstance(v, (types.FunctionType, types.LambdaType))


def log(msg: str) -> None:
    """Log `msg` using ``options.verbose_log`` if set, otherwise ``print``."""
    from ibis.config import options

    if options.verbose:
        (options.verbose_log or print)(msg)


def approx_equal(a: Real, b: Real, eps: Real):
    """Return whether the difference between `a` and `b` is less than `eps`.

    Raises
    ------
    AssertionError
    """
    assert abs(a - b) < eps


def safe_index(elements: Sequence[int], value: int) -> int:
    """Find the location of `value` in `elements`.

    Return -1 if `value` is not found instead of raising ``ValueError``.

    Parameters
    ----------
    elements
        Elements to index into
    value : int
        Index of the given sequence/elements

    Returns
    -------
    int

    Examples
    --------
    >>> sequence = [1, 2, 3]
    >>> safe_index(sequence, 2)
    1
    >>> safe_index(sequence, 4)
    -1
    """
    try:
        return elements.index(value)
    except ValueError:
        return -1


def is_iterable(o: Any) -> bool:
    """Return whether `o` is iterable and not a :class:`str` or :class:`bytes`.

    Parameters
    ----------
    o : object
        Any python object

    Returns
    -------
    bool

    Examples
    --------
    >>> is_iterable('1')
    False
    >>> is_iterable(b'1')
    False
    >>> is_iterable(iter('1'))
    True
    >>> is_iterable(i for i in range(1))
    True
    >>> is_iterable(1)
    False
    >>> is_iterable([])
    True
    """
    return not isinstance(o, (str, bytes)) and isinstance(o, collections.abc.Iterable)


def convert_unit(value, unit, to, floor: bool = True):
    """Convert a value between different units.

    Convert `value`, is assumed to be in units of `unit`, to units of `to`.
    If `floor` is true, then use floor division on `value` if necessary.

    Parameters
    ----------
    value
        Number or numeric ibis expression
    unit
        Unit of `value`
    to
        Unit to convert to
    floor
        Whether or not to use floor division on `value` if necessary.

    Returns
    -------
    Union[numbers.Integral, ibis.expr.types.NumericValue]
        Integer converted unit

    Examples
    --------
    >>> one_second = 1000
    >>> x = convert_unit(one_second, 'ms', 's')
    >>> x
    1
    >>> one_second = 1
    >>> x = convert_unit(one_second, 's', 'ms')
    >>> x
    1000
    >>> x = convert_unit(one_second, 's', 's')
    >>> x
    1
    >>> x = convert_unit(one_second, 's', 'M')
    Traceback (most recent call last):
        ...
    ValueError: Cannot convert to or from variable length interval
    """
    # Don't do anything if from and to units are equivalent
    if unit == to:
        return value

    units = ('W', 'D', 'h', 'm', 's', 'ms', 'us', 'ns')
    factors = (7, 24, 60, 60, 1000, 1000, 1000)

    monthly_units = ('Y', 'Q', 'M')
    monthly_factors = (4, 3)

    try:
        i, j = units.index(unit), units.index(to)
    except ValueError:
        try:
            i, j = monthly_units.index(unit), monthly_units.index(to)
            factors = monthly_factors
        except ValueError:
            raise ValueError('Cannot convert to or from variable length interval')

    factor = functools.reduce(operator.mul, factors[min(i, j) : max(i, j)], 1)
    assert factor > 1

    if i < j:
        op = operator.mul
    else:
        assert i > j
        op = operator.floordiv if floor else operator.truediv
    try:
        return op(value.to_expr(), factor).op()
    except AttributeError:
        return op(value, factor)


def get_logger(
    name: str,
    level: str | None = None,
    format: str | None = None,
    propagate: bool = False,
) -> logging.Logger:
    """Get a logger.

    Parameters
    ----------
    name
        Logger name
    level
        Logging level
    format
        Format string
    propagate
        Propagate the logger

    Returns
    -------
    logging.Logger
    """
    logging.basicConfig()
    handler = logging.StreamHandler()

    if format is None:
        format = (
            '%(relativeCreated)6d '
            '%(name)-20s '
            '%(levelname)-8s '
            '%(threadName)-25s '
            '%(message)s'
        )
    handler.setFormatter(logging.Formatter(fmt=format))
    logger = logging.getLogger(name)
    logger.propagate = propagate
    logger.setLevel(
        level or getattr(logging, os.environ.get('LOGLEVEL', 'WARNING').upper())
    )
    logger.addHandler(handler)
    return logger


# taken from the itertools documentation
def consume(iterator: Iterator[T], n: int | None = None) -> None:
    """Advance `iterator` n-steps ahead. If `n` is `None`, consume entirely."""
    # Use functions that consume iterators at C speed.
    if n is None:
        # feed the entire iterator into a zero-length deque
        collections.deque(iterator, maxlen=0)
    else:
        # advance to the empty slice starting at position n
        next(itertools.islice(iterator, n, n), None)


# TODO(kszucs): make it a more robust to better align with graph._flatten_collections()
def recursive_get(obj, mapping):
    if isinstance(obj, tuple):
        return tuple(recursive_get(o, mapping) for o in obj)
    elif isinstance(obj, dict):
        return {k: recursive_get(v, mapping) for k, v in obj.items()}
    else:
        return mapping.get(obj, obj)


def flatten_iterable(iterable):
    """Recursively flatten the iterable `iterable`."""
    if not is_iterable(iterable):
        raise TypeError("flatten is only defined for non-str iterables")

    for item in iterable:
        if is_iterable(item):
            yield from flatten_iterable(item)
        else:
            yield item


def deprecated_msg(name, *, instead, as_of="", removed_in=""):
    msg = f"`{name}` is deprecated"

    msgs = []

    if as_of:
        msgs.append(f"as of v{as_of}")

    if removed_in:
        msgs.append(f"removed in v{removed_in}")

    if msgs:
        msg += f" {', '.join(msgs)}"
    msg += f'; {instead}'
    return msg


def warn_deprecated(name, *, instead, as_of="", removed_in="", stacklevel=1):
    """Warn about deprecated usage.

    The message includes a stacktrace and what to do instead.
    """

    msg = deprecated_msg(name, instead=instead, as_of=as_of, removed_in=removed_in)
    warnings.warn(msg, FutureWarning, stacklevel=stacklevel + 1)


def append_admonition(
    func: Callable, *, msg: str, body: str = "", kind: str = "warning"
) -> str:
    """Append a `kind` admonition with `msg` to `func`'s docstring."""
    if docstr := func.__doc__:
        preamble, *rest = docstr.split("\n\n", maxsplit=1)

        # count leading spaces and add them to the deprecation warning so the
        # docstring parses correctly
        leading_spaces = " " * sum(
            1 for _ in itertools.takewhile(str.isspace, rest[0] if rest else [])
        )

        admonition_doc = f'{leading_spaces}!!! {kind} "{msg}"'

        if body:
            rest = [indent(body, spaces=len(leading_spaces) + 4), *rest]

        docstr = "\n\n".join([preamble, admonition_doc, *rest])
    else:
        admonition_doc = f'!!! {kind} "{msg}"'
        if body:
            admonition_doc += f"\n\n{indent(body, spaces=4)}"
        docstr = admonition_doc
    return docstr


def deprecated(*, instead: str, as_of: str = "", removed_in: str = ""):
    """Decorate to warn of deprecated usage and what to do instead."""

    def decorator(func):
        msg = deprecated_msg(
            func.__qualname__, instead=instead, as_of=as_of, removed_in=removed_in
        )

        func.__doc__ = append_admonition(func, msg=f"DEPRECATED: {msg}")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            warn_deprecated(
                func.__qualname__,
                instead=instead,
                as_of=as_of,
                removed_in=removed_in,
                stacklevel=2,
            )
            return func(*args, **kwargs)

        return wrapper

    return decorator


def backend_sensitive(
    *,
    msg: str = "This operation differs between backends.",
    why: str = "",
):
    """Indicate that an API may be sensitive to a backend."""

    def wrapper(func):
        func.__doc__ = append_admonition(func, msg=msg, body=why, kind="info")
        return func

    return wrapper


def experimental(func):
    """Decorate a callable to add warning about API instability in docstring."""

    func.__doc__ = append_admonition(
        func, msg="This API is experimental and subject to change."
    )
    return func


class ToFrame(abc.ABC):
    """Interface for in-memory objects that can be converted to an in-memory structure.

    Supports pandas DataFrames and PyArrow Tables.
    """

    __slots__ = ()

    @abc.abstractmethod
    def to_frame(self) -> pd.DataFrame:  # pragma: no cover
        """Convert this input to a pandas DataFrame."""

    @abc.abstractmethod
    def to_pyarrow(self, schema: sch.Schema) -> pa.Table:  # pragma: no cover
        """Convert this input to a PyArrow Table."""


def backend_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Get the list of installed `ibis.backend` entrypoints."""

    if sys.version_info < (3, 10):
        eps = importlib.metadata.entry_points()["ibis.backends"]
    else:
        eps = importlib.metadata.entry_points(group="ibis.backends")
    return sorted(eps)


def import_object(qualname: str) -> Any:
    """Attempt to import an object given its full qualname.

    Examples
    --------
    >>> ex = import_object("ibis.examples")

    Is the same as

    >>> from ibis import examples as ex
    """
    mod_name, name = qualname.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    try:
        return getattr(mod, name)
    except AttributeError:
        raise ImportError(f"cannot import name {name!r} from {mod_name!r}") from None


def normalize_filename(source: str | Path) -> str:
    def _removeprefix(text, prefix):
        # TODO: remove when we drop Python 3.8
        try:
            return text.removeprefix(prefix)
        except AttributeError:
            return text[text.startswith(prefix) and len(prefix) :]

    source = str(source)
    for prefix in (
        "parquet",
        "csv",
        "csv.gz",
        "txt",
        "txt.gz",
        "tsv",
        "tsv.gz",
        "file",
    ):
        source = _removeprefix(source, f"{prefix}://")

    def _absolufy_paths(name):
        if not name.startswith(("http", "s3")):
            return os.path.abspath(name)
        return name

    source = _absolufy_paths(source)
    return source


def generate_unique_table_name(namespace: str) -> str:
    """Creates case-insensitive uuid4 unique table name."""
    return f"_ibis_{namespace}_{np.base_repr(uuid.uuid4().int, 36)}".lower()
