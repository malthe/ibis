from __future__ import annotations

import contextlib
import functools
import operator
from typing import Any

import sqlalchemy as sa
from sqlalchemy.sql.functions import FunctionElement

import ibis.common.exceptions as com
import ibis.expr.analysis as an
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.types as ir
from ibis.backends.base.sql.alchemy.database import AlchemyTable


def variance_reduction(func_name):
    suffix = {'sample': 'samp', 'pop': 'pop'}

    def variance_compiler(t, op):
        arg = op.arg

        if arg.output_dtype.is_boolean():
            arg = ops.Cast(op.arg, to=dt.int32)

        func = getattr(sa.func, f'{func_name}_{suffix[op.how]}')

        if op.where is not None:
            arg = ops.Where(op.where, arg, None)

        return func(t.translate(arg))

    return variance_compiler


def fixed_arity(sa_func, arity):
    def formatter(t, op):
        arg_count = len(op.args)
        if arity != arg_count:
            raise com.IbisError(
                f'Incorrect number of args. Expected: {arity}. Current: {arg_count}'
            )

        return _varargs_call(sa_func, t, op.args)

    return formatter


def _varargs_call(sa_func, t, args):
    trans_args = []
    for raw_arg in args:
        arg = t.translate(raw_arg)
        with contextlib.suppress(AttributeError):
            arg = arg.scalar_subquery()
        trans_args.append(arg)
    return sa_func(*trans_args)


def varargs(sa_func):
    def formatter(t, op):
        return _varargs_call(sa_func, t, op.arg)

    return formatter


def get_sqla_table(ctx, table):
    if ctx.has_ref(table, parent_contexts=True):
        ctx_level = ctx
        sa_table = ctx_level.get_ref(table)
        while sa_table is None and ctx_level.parent is not ctx_level:
            ctx_level = ctx_level.parent
            sa_table = ctx_level.get_ref(table)
    elif isinstance(table, AlchemyTable):
        sa_table = table.sqla_table
    else:
        sa_table = ctx.get_compiled_expr(table)

    return sa_table


def get_col(sa_table, op: ops.TableColumn) -> sa.sql.ColumnClause:
    """Extract a column from a table."""
    cols = sa_table.exported_columns
    colname = op.name

    if (col := cols.get(colname)) is not None:
        return col

    # `cols` is a SQLAlchemy column collection that contains columns
    # with names that are secretly prefixed by table that contains them
    #
    # for example, in `t0.join(t1).select(t0.a, t1.b)` t0.a will be named `t0_a`
    # and t1.b will be named `t1_b`
    #
    # unfortunately SQLAlchemy doesn't let you select by the *un*prefixed
    # column name despite the uniqueness of `colname`
    #
    # however, in ibis we have already deduplicated column names so we can
    # refer to the name by position
    colindex = op.table.schema._name_locs[colname]
    return cols[colindex]


def _table_column(t, op):
    ctx = t.context
    table = op.table

    sa_table = get_sqla_table(ctx, table)

    out_expr = get_col(sa_table, op)
    out_expr.quote = t._always_quote_columns

    # If the column does not originate from the table set in the current SELECT
    # context, we should format as a subquery
    if t.permit_subquery and ctx.is_foreign_expr(table):
        try:
            subq = sa_table.subquery()
        except AttributeError:
            subq = sa_table
        return sa.select(subq.c[out_expr.name])

    return out_expr


def _table_array_view(t, op):
    # the table that the TableArrayView op contains (op.table) has
    # one or more input relations that we need to "pin" for sqlalchemy's
    # auto correlation functionality -- this is what `.correlate_except` does
    #
    # every relation that is NOT passed to `correlate_except` is considered an
    # outer-query table
    ctx = t.context
    table = ctx.get_compiled_expr(op.table)
    # TODO: handle the case of `op.table` being a join
    first, *_ = an.find_immediate_parent_tables(op.table, keep_input=False)
    ref = ctx.get_ref(first)
    return table.correlate_except(ref)


def _exists_subquery(t, op):
    ctx = t.context

    # TODO(kszucs): avoid converting the predicates to expressions
    # this should be done by the rewrite step before compilation
    filtered = (
        op.foreign_table.to_expr()
        .filter([pred.to_expr() for pred in op.predicates])
        .projection([ir.literal(1).name("")])
    )

    sub_ctx = ctx.subcontext()
    clause = ctx.compiler.to_sql(filtered, sub_ctx, exists=True)

    if isinstance(op, ops.NotExistsSubquery):
        clause = sa.not_(clause)

    return clause


def _cast(t, op):
    arg = op.arg
    typ = op.to
    arg_dtype = arg.output_dtype

    sa_arg = t.translate(arg)

    # specialize going from an integer type to a timestamp
    if arg_dtype.is_integer() and typ.is_timestamp():
        return t.integer_to_timestamp(sa_arg)

    if arg_dtype.is_binary() and typ.is_string():
        return sa.func.encode(sa_arg, 'escape')

    if typ.is_binary():
        #  decode yields a column of memoryview which is annoying to deal with
        # in pandas. CAST(expr AS BYTEA) is correct and returns byte strings.
        return sa.cast(sa_arg, sa.LargeBinary())

    if typ.is_json() and not t.native_json_type:
        return sa_arg

    return sa.cast(sa_arg, t.get_sqla_type(typ))


def _contains(func):
    def translate(t, op):
        left = t.translate(op.value)

        options = op.options
        if isinstance(options, tuple):
            right = [t.translate(x) for x in op.options]
        elif options.output_shape.is_columnar():
            right = t.translate(ops.TableArrayView(options.to_expr().as_table()))
            if not isinstance(right, sa.sql.Selectable):
                right = sa.select(right)
        else:
            right = t.translate(options)

        return func(left, right)

    return translate


def _alias(t, op):
    # just compile the underlying argument because the naming is handled
    # by the translator for the top level expression
    return t.translate(op.arg)


def _literal(_, op):
    dtype = op.output_dtype
    value = op.value

    if value is None:
        return sa.null()

    if dtype.is_set():
        return list(map(sa.literal, value))
    elif dtype.is_array():
        value = list(value)

    return sa.literal(value)


def _is_null(t, op):
    arg = t.translate(op.arg)
    return arg.is_(sa.null())


def _not_null(t, op):
    arg = t.translate(op.arg)
    return arg.is_not(sa.null())


def _round(t, op):
    sa_arg = t.translate(op.arg)

    f = sa.func.round

    if op.digits is not None:
        sa_digits = t.translate(op.digits)
        return f(sa_arg, sa_digits)
    else:
        return f(sa_arg)


def _floor_divide(t, op):
    left = t.translate(op.left)
    right = t.translate(op.right)
    return sa.func.floor(left / right)


def _simple_case(t, op):
    return _translate_case(t, op, value=t.translate(op.base))


def _searched_case(t, op):
    return _translate_case(t, op, value=None)


def _translate_case(t, op, *, value):
    return sa.case(
        *zip(map(t.translate, op.cases), map(t.translate, op.results)),
        value=value,
        else_=t.translate(op.default),
    )


def _negate(t, op):
    arg = t.translate(op.arg)
    return sa.not_(arg) if op.arg.output_dtype.is_boolean() else -arg


def unary(sa_func):
    return fixed_arity(sa_func, 1)


def _string_like(method_name, t, op):
    method = getattr(t.translate(op.arg), method_name)
    return method(t.translate(op.pattern), escape=op.escape)


def _startswith(t, op):
    return t.translate(op.arg).startswith(t.translate(op.start))


def _endswith(t, op):
    return t.translate(op.arg).endswith(t.translate(op.end))


_cumulative_to_reduction = {
    ops.CumulativeSum: ops.Sum,
    ops.CumulativeMin: ops.Min,
    ops.CumulativeMax: ops.Max,
    ops.CumulativeMean: ops.Mean,
    ops.CumulativeAny: ops.Any,
    ops.CumulativeAll: ops.All,
}


def _cumulative_to_window(translator, op, frame):
    klass = _cumulative_to_reduction[type(op)]
    new_op = klass(*op.args)
    new_expr = new_op.to_expr().name(op.name)
    new_frame = frame.copy(start=None, end=0)

    if type(new_op) in translator._rewrites:
        new_expr = translator._rewrites[type(new_op)](new_expr)

    # TODO(kszucs): rewrite to receive and return an ops.Node
    return an.windowize_function(new_expr, frame=new_frame)


def _translate_window_boundary(boundary):
    if boundary is None:
        return None

    if isinstance(boundary.value, ops.Literal):
        if boundary.preceding:
            return -boundary.value.value
        else:
            return boundary.value.value

    raise com.TranslationError("Window boundaries must be literal values")


def _window_function(t, window):
    if isinstance(window.func, ops.CumulativeOp):
        func = _cumulative_to_window(t, window.func, window.frame).op()
        return t.translate(func)

    reduction = t.translate(window.func)

    # Some analytic functions need to have the expression of interest in
    # the ORDER BY part of the window clause
    if isinstance(window.func, t._require_order_by) and not window.frame.order_by:
        order_by = t.translate(window.func.arg)  # .args[0])
    else:
        order_by = [t.translate(arg) for arg in window.frame.order_by]

    partition_by = [t.translate(arg) for arg in window.frame.group_by]

    if isinstance(window.frame, ops.RowsWindowFrame):
        if window.frame.max_lookback is not None:
            raise NotImplementedError(
                'Rows with max lookback is not implemented for SQLAlchemy-based '
                'backends.'
            )
        how = 'rows'
    elif isinstance(window.frame, ops.RangeWindowFrame):
        how = 'range_'
    else:
        raise NotImplementedError(type(window.frame))

    if t._forbids_frame_clause and isinstance(window.func, t._forbids_frame_clause):
        # some functions on some backends don't support frame clauses
        additional_params = {}
    else:
        start = _translate_window_boundary(window.frame.start)
        end = _translate_window_boundary(window.frame.end)
        additional_params = {how: (start, end)}

    result = reduction.over(
        partition_by=partition_by, order_by=order_by, **additional_params
    )

    if isinstance(window.func, (ops.RowNumber, ops.DenseRank, ops.MinRank, ops.NTile)):
        return result - 1
    else:
        return result


def _lag(t, op):
    if op.default is not None:
        raise NotImplementedError()

    sa_arg = t.translate(op.arg)
    sa_offset = t.translate(op.offset) if op.offset is not None else 1
    return sa.func.lag(sa_arg, sa_offset)


def _lead(t, op):
    if op.default is not None:
        raise NotImplementedError()
    sa_arg = t.translate(op.arg)
    sa_offset = t.translate(op.offset) if op.offset is not None else 1
    return sa.func.lead(sa_arg, sa_offset)


def _ntile(t, op):
    return sa.func.ntile(t.translate(op.buckets))


def _sort_key(t, op):
    func = sa.asc if op.ascending else sa.desc
    return func(t.translate(op.expr))


def _string_join(t, op):
    return sa.func.concat_ws(t.translate(op.sep), *map(t.translate, op.arg))


def reduction(sa_func):
    def compile_expr(t, expr):
        return t._reduction(sa_func, expr)

    return compile_expr


def _zero_if_null(t, op):
    sa_arg = t.translate(op.arg)
    return sa.case(
        (sa_arg.is_(None), sa.cast(0, t.get_sqla_type(op.output_dtype))),
        else_=sa_arg,
    )


def _substring(t, op):
    args = t.translate(op.arg), t.translate(op.start) + 1

    if (length := op.length) is not None:
        args += (t.translate(length),)

    return sa.func.substr(*args)


def _gen_string_find(func):
    def string_find(t, op):
        if op.start is not None:
            raise NotImplementedError("`start` not yet implemented")

        if op.end is not None:
            raise NotImplementedError("`end` not yet implemented")

        return func(t.translate(op.arg), t.translate(op.substr)) - 1

    return string_find


def _nth_value(t, op):
    return sa.func.nth_value(t.translate(op.arg), t.translate(op.nth) + 1)


def _bitwise_op(operator):
    def translate(t, op):
        left = t.translate(op.left)
        right = t.translate(op.right)
        return left.op(operator)(right)

    return translate


def _bitwise_not(t, op):
    arg = t.translate(op.arg)
    return sa.sql.elements.UnaryExpression(
        arg,
        operator=sa.sql.operators.custom_op("~"),
    )


def _count_star(t, op):
    if (where := op.where) is None:
        return sa.func.count()

    if t._has_reduction_filter_syntax:
        return sa.func.count().filter(t.translate(where))

    return sa.func.count(t.translate(ops.Where(where, 1, None)))


def _extract(fmt: str):
    def translator(t, op: ops.Node):
        return sa.cast(sa.extract(fmt, t.translate(op.arg)), sa.SMALLINT)

    return translator


class array_map(FunctionElement):
    pass


class array_filter(FunctionElement):
    pass


sqlalchemy_operation_registry: dict[Any, Any] = {
    ops.Alias: _alias,
    ops.And: fixed_arity(operator.and_, 2),
    ops.Or: fixed_arity(operator.or_, 2),
    ops.Xor: fixed_arity(lambda x, y: (x | y) & ~(x & y), 2),
    ops.Not: unary(sa.not_),
    ops.Abs: unary(sa.func.abs),
    ops.Cast: _cast,
    ops.Coalesce: varargs(sa.func.coalesce),
    ops.NullIf: fixed_arity(sa.func.nullif, 2),
    ops.Contains: _contains(lambda left, right: left.in_(right)),
    ops.NotContains: _contains(lambda left, right: left.notin_(right)),
    ops.Count: reduction(sa.func.count),
    ops.CountStar: _count_star,
    ops.Sum: reduction(sa.func.sum),
    ops.Mean: reduction(sa.func.avg),
    ops.Min: reduction(sa.func.min),
    ops.Max: reduction(sa.func.max),
    ops.Variance: variance_reduction("var"),
    ops.StandardDev: variance_reduction("stddev"),
    ops.BitAnd: reduction(sa.func.bit_and),
    ops.BitOr: reduction(sa.func.bit_or),
    ops.BitXor: reduction(sa.func.bit_xor),
    ops.CountDistinct: reduction(lambda arg: sa.func.count(arg.distinct())),
    ops.ApproxCountDistinct: reduction(lambda arg: sa.func.count(arg.distinct())),
    ops.GroupConcat: reduction(sa.func.group_concat),
    ops.Between: fixed_arity(sa.between, 3),
    ops.IsNull: _is_null,
    ops.NotNull: _not_null,
    ops.Negate: _negate,
    ops.Round: _round,
    ops.Literal: _literal,
    ops.NullLiteral: lambda *_: sa.null(),
    ops.SimpleCase: _simple_case,
    ops.SearchedCase: _searched_case,
    ops.TableColumn: _table_column,
    ops.TableArrayView: _table_array_view,
    ops.ExistsSubquery: _exists_subquery,
    ops.NotExistsSubquery: _exists_subquery,
    # miscellaneous varargs
    ops.Least: varargs(sa.func.least),
    ops.Greatest: varargs(sa.func.greatest),
    # string
    ops.Capitalize: unary(
        lambda arg: sa.func.concat(
            sa.func.upper(sa.func.substr(arg, 1, 1)), sa.func.substr(arg, 2)
        )
    ),
    ops.LPad: fixed_arity(sa.func.lpad, 3),
    ops.RPad: fixed_arity(sa.func.rpad, 3),
    ops.Strip: unary(sa.func.trim),
    ops.LStrip: unary(sa.func.ltrim),
    ops.RStrip: unary(sa.func.rtrim),
    ops.Repeat: fixed_arity(sa.func.repeat, 2),
    ops.Reverse: unary(sa.func.reverse),
    ops.StrRight: fixed_arity(sa.func.right, 2),
    ops.Lowercase: unary(sa.func.lower),
    ops.Uppercase: unary(sa.func.upper),
    ops.StringAscii: unary(sa.func.ascii),
    ops.StringFind: _gen_string_find(sa.func.strpos),
    ops.StringLength: unary(sa.func.length),
    ops.StringJoin: _string_join,
    ops.StringReplace: fixed_arity(sa.func.replace, 3),
    ops.StringSQLLike: functools.partial(_string_like, "like"),
    ops.StringSQLILike: functools.partial(_string_like, "ilike"),
    ops.StartsWith: _startswith,
    ops.EndsWith: _endswith,
    ops.StringConcat: varargs(sa.func.concat),
    ops.Substring: _substring,
    # math
    ops.Ln: unary(sa.func.ln),
    ops.Exp: unary(sa.func.exp),
    ops.Sign: unary(sa.func.sign),
    ops.Sqrt: unary(sa.func.sqrt),
    ops.Ceil: unary(sa.func.ceil),
    ops.Floor: unary(sa.func.floor),
    ops.Power: fixed_arity(sa.func.pow, 2),
    ops.FloorDivide: _floor_divide,
    ops.Acos: unary(sa.func.acos),
    ops.Asin: unary(sa.func.asin),
    ops.Atan: unary(sa.func.atan),
    ops.Atan2: fixed_arity(sa.func.atan2, 2),
    ops.Cos: unary(sa.func.cos),
    ops.Sin: unary(sa.func.sin),
    ops.Tan: unary(sa.func.tan),
    ops.Cot: unary(sa.func.cot),
    ops.Pi: fixed_arity(sa.func.pi, 0),
    ops.E: fixed_arity(lambda: sa.func.exp(1), 0),
    # other
    ops.SortKey: _sort_key,
    ops.Date: unary(lambda arg: sa.cast(arg, sa.DATE)),
    ops.DateFromYMD: fixed_arity(sa.func.date, 3),
    ops.TimeFromHMS: fixed_arity(sa.func.time, 3),
    ops.TimestampFromYMDHMS: lambda t, op: sa.func.make_timestamp(
        *map(t.translate, op.args)
    ),
    ops.Degrees: unary(sa.func.degrees),
    ops.Radians: unary(sa.func.radians),
    ops.ZeroIfNull: _zero_if_null,
    ops.RandomScalar: fixed_arity(sa.func.random, 0),
    # Binary arithmetic
    ops.Add: fixed_arity(operator.add, 2),
    ops.Subtract: fixed_arity(operator.sub, 2),
    ops.Multiply: fixed_arity(operator.mul, 2),
    # XXX `ops.Divide` is overwritten in `translator.py` with a custom
    # function `_true_divide`, but for some reason both are required
    ops.Divide: fixed_arity(operator.truediv, 2),
    ops.Modulus: fixed_arity(operator.mod, 2),
    # Comparisons
    ops.Equals: fixed_arity(operator.eq, 2),
    ops.NotEquals: fixed_arity(operator.ne, 2),
    ops.Less: fixed_arity(operator.lt, 2),
    ops.LessEqual: fixed_arity(operator.le, 2),
    ops.Greater: fixed_arity(operator.gt, 2),
    ops.GreaterEqual: fixed_arity(operator.ge, 2),
    ops.IdenticalTo: fixed_arity(
        sa.sql.expression.ColumnElement.is_not_distinct_from, 2
    ),
    ops.Where: fixed_arity(
        lambda predicate, value_if_true, value_if_false: sa.case(
            (predicate, value_if_true),
            else_=value_if_false,
        ),
        3,
    ),
    ops.BitwiseAnd: _bitwise_op("&"),
    ops.BitwiseOr: _bitwise_op("|"),
    ops.BitwiseXor: _bitwise_op("^"),
    ops.BitwiseLeftShift: _bitwise_op("<<"),
    ops.BitwiseRightShift: _bitwise_op(">>"),
    ops.BitwiseNot: _bitwise_not,
    ops.JSONGetItem: fixed_arity(lambda x, y: x.op("->")(y), 2),
    ops.ExtractYear: _extract('year'),
    ops.ExtractQuarter: _extract('quarter'),
    ops.ExtractMonth: _extract('month'),
    ops.ExtractDay: _extract('day'),
    ops.ExtractHour: _extract('hour'),
    ops.ExtractMinute: _extract('minute'),
    ops.ExtractSecond: _extract('second'),
    ops.Time: fixed_arity(lambda arg: sa.cast(arg, sa.TIME), 1),
}


sqlalchemy_window_functions_registry = {
    ops.Lag: _lag,
    ops.Lead: _lead,
    ops.NTile: _ntile,
    ops.FirstValue: unary(sa.func.first_value),
    ops.LastValue: unary(sa.func.last_value),
    ops.RowNumber: fixed_arity(sa.func.row_number, 0),
    ops.DenseRank: unary(lambda _: sa.func.dense_rank()),
    ops.MinRank: unary(lambda _: sa.func.rank()),
    ops.PercentRank: unary(lambda _: sa.func.percent_rank()),
    ops.CumeDist: unary(lambda _: sa.func.cume_dist()),
    ops.NthValue: _nth_value,
    ops.WindowFunction: _window_function,
    ops.CumulativeMax: unary(sa.func.max),
    ops.CumulativeMin: unary(sa.func.min),
    ops.CumulativeSum: unary(sa.func.sum),
    ops.CumulativeMean: unary(sa.func.avg),
    ops.CumulativeAny: unary(sa.func.bool_or),
    ops.CumulativeAll: unary(sa.func.bool_and),
}

geospatial_functions = {
    ops.GeoArea: unary(sa.func.ST_Area),
    ops.GeoAsBinary: unary(sa.func.ST_AsBinary),
    ops.GeoAsEWKB: unary(sa.func.ST_AsEWKB),
    ops.GeoAsEWKT: unary(sa.func.ST_AsEWKT),
    ops.GeoAsText: unary(sa.func.ST_AsText),
    ops.GeoAzimuth: fixed_arity(sa.func.ST_Azimuth, 2),
    ops.GeoBuffer: fixed_arity(sa.func.ST_Buffer, 2),
    ops.GeoCentroid: unary(sa.func.ST_Centroid),
    ops.GeoContains: fixed_arity(sa.func.ST_Contains, 2),
    ops.GeoContainsProperly: fixed_arity(sa.func.ST_Contains, 2),
    ops.GeoCovers: fixed_arity(sa.func.ST_Covers, 2),
    ops.GeoCoveredBy: fixed_arity(sa.func.ST_CoveredBy, 2),
    ops.GeoCrosses: fixed_arity(sa.func.ST_Crosses, 2),
    ops.GeoDFullyWithin: fixed_arity(sa.func.ST_DFullyWithin, 3),
    ops.GeoDifference: fixed_arity(sa.func.ST_Difference, 2),
    ops.GeoDisjoint: fixed_arity(sa.func.ST_Disjoint, 2),
    ops.GeoDistance: fixed_arity(sa.func.ST_Distance, 2),
    ops.GeoDWithin: fixed_arity(sa.func.ST_DWithin, 3),
    ops.GeoEndPoint: unary(sa.func.ST_EndPoint),
    ops.GeoEnvelope: unary(sa.func.ST_Envelope),
    ops.GeoEquals: fixed_arity(sa.func.ST_Equals, 2),
    ops.GeoGeometryN: fixed_arity(sa.func.ST_GeometryN, 2),
    ops.GeoGeometryType: unary(sa.func.ST_GeometryType),
    ops.GeoIntersection: fixed_arity(sa.func.ST_Intersection, 2),
    ops.GeoIntersects: fixed_arity(sa.func.ST_Intersects, 2),
    ops.GeoIsValid: unary(sa.func.ST_IsValid),
    ops.GeoLineLocatePoint: fixed_arity(sa.func.ST_LineLocatePoint, 2),
    ops.GeoLineMerge: unary(sa.func.ST_LineMerge),
    ops.GeoLineSubstring: fixed_arity(sa.func.ST_LineSubstring, 3),
    ops.GeoLength: unary(sa.func.ST_Length),
    ops.GeoNPoints: unary(sa.func.ST_NPoints),
    ops.GeoOrderingEquals: fixed_arity(sa.func.ST_OrderingEquals, 2),
    ops.GeoOverlaps: fixed_arity(sa.func.ST_Overlaps, 2),
    ops.GeoPerimeter: unary(sa.func.ST_Perimeter),
    ops.GeoSimplify: fixed_arity(sa.func.ST_Simplify, 3),
    ops.GeoSRID: unary(sa.func.ST_SRID),
    ops.GeoSetSRID: fixed_arity(sa.func.ST_SetSRID, 2),
    ops.GeoStartPoint: unary(sa.func.ST_StartPoint),
    ops.GeoTouches: fixed_arity(sa.func.ST_Touches, 2),
    ops.GeoTransform: fixed_arity(sa.func.ST_Transform, 2),
    ops.GeoUnaryUnion: unary(sa.func.ST_Union),
    ops.GeoUnion: fixed_arity(sa.func.ST_Union, 2),
    ops.GeoWithin: fixed_arity(sa.func.ST_Within, 2),
    ops.GeoX: unary(sa.func.ST_X),
    ops.GeoY: unary(sa.func.ST_Y),
    # Missing Geospatial ops:
    #   ST_AsGML
    #   ST_AsGeoJSON
    #   ST_AsKML
    #   ST_AsRaster
    #   ST_AsSVG
    #   ST_AsTWKB
    #   ST_Distance_Sphere
    #   ST_Dump
    #   ST_DumpPoints
    #   ST_GeogFromText
    #   ST_GeomFromEWKB
    #   ST_GeomFromEWKT
    #   ST_GeomFromText
}
