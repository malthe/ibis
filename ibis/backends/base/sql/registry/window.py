from __future__ import annotations

import ibis.common.exceptions as com
import ibis.expr.analysis as an
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops

_map_interval_to_microseconds = {
    'W': 604800000000,
    'D': 86400000000,
    'h': 3600000000,
    'm': 60000000,
    's': 1000000,
    'ms': 1000,
    'us': 1,
}


_cumulative_to_reduction = {
    ops.CumulativeSum: ops.Sum,
    ops.CumulativeMin: ops.Min,
    ops.CumulativeMax: ops.Max,
    ops.CumulativeMean: ops.Mean,
    ops.CumulativeAny: ops.Any,
    ops.CumulativeAll: ops.All,
}


def cumulative_to_window(translator, func, frame):
    klass = _cumulative_to_reduction[type(func)]
    func = klass(*func.args)

    try:
        rule = translator._rewrites[type(func)]
    except KeyError:
        pass
    else:
        func = rule(func)

    frame = frame.copy(start=None, end=0)
    expr = an.windowize_function(func.to_expr(), frame)
    return expr.op()


def interval_boundary_to_integer(boundary):
    if boundary is None:
        return None
    elif boundary.output_dtype.is_numeric():
        return boundary

    value = boundary.value
    try:
        multiplier = _map_interval_to_microseconds[value.output_dtype.unit]
    except KeyError:
        raise com.IbisInputError(
            f"Unsupported interval unit: {value.output_dtype.unit}"
        )

    if isinstance(value, ops.Literal):
        value = ops.Literal(value.value * multiplier, dt.int64)
    else:
        left = ops.Cast(value, to=dt.int64)
        value = ops.Multiply(left, multiplier)

    return boundary.copy(value=value)


def time_range_to_range_window(frame):
    # Check that ORDER BY column is a single time column:
    if len(frame.order_by) > 1:
        raise com.IbisInputError(
            f"Expected 1 order-by variable, got {len(frame.order_by)}"
        )

    order_by = frame.order_by[0]
    order_by = order_by.copy(expr=ops.Cast(order_by.expr, dt.int64))
    start = interval_boundary_to_integer(frame.start)
    end = interval_boundary_to_integer(frame.end)

    return frame.copy(order_by=(order_by,), start=start, end=end)


def format_window_boundary(translator, boundary):
    if isinstance(boundary.value, ops.Literal) and boundary.value.value == 0:
        return "CURRENT ROW"

    value = translator.translate(boundary.value)
    direction = "PRECEDING" if boundary.preceding else "FOLLOWING"

    return f'{value} {direction}'


def format_window_frame(translator, func, frame):
    components = []

    if frame.group_by:
        partition_args = ', '.join(map(translator.translate, frame.group_by))
        components.append(f'PARTITION BY {partition_args}')

    if frame.order_by:
        order_args = ', '.join(map(translator.translate, frame.order_by))
        components.append(f'ORDER BY {order_args}')

    if frame.start is None and frame.end is None:
        # no-op, default is full sample
        pass
    elif not isinstance(func, translator._forbids_frame_clause):
        if frame.start is None:
            start = 'UNBOUNDED PRECEDING'
        else:
            start = format_window_boundary(translator, frame.start)

        if frame.end is None:
            end = 'UNBOUNDED FOLLOWING'
        else:
            end = format_window_boundary(translator, frame.end)

        frame = f'{frame.how.upper()} BETWEEN {start} AND {end}'
        components.append(frame)

    return 'OVER ({})'.format(' '.join(components))


def window(translator, op):
    _unsupported_reductions = (
        ops.ApproxMedian,
        ops.GroupConcat,
        ops.ApproxCountDistinct,
    )

    if isinstance(op.func, _unsupported_reductions):
        raise com.UnsupportedOperationError(
            f'{type(op.func)} is not supported in window functions'
        )

    if isinstance(op.func, ops.CumulativeOp):
        arg = cumulative_to_window(translator, op.func, op.frame)
        return translator.translate(arg)

    # Some analytic functions need to have the expression of interest in
    # the ORDER BY part of the window clause
    frame = op.frame
    if isinstance(op.func, translator._require_order_by) and not frame.order_by:
        frame = frame.copy(order_by=(op.func.arg,))

    # Time ranges need to be converted to microseconds.
    if isinstance(frame, ops.RangeWindowFrame):
        if any(c.output_dtype.is_temporal() for c in frame.order_by):
            frame = time_range_to_range_window(frame)
    elif isinstance(frame, ops.RowsWindowFrame):
        if frame.max_lookback is not None:
            raise NotImplementedError(
                'Rows with max lookback is not implemented for SQL-based backends.'
            )

    window_formatted = format_window_frame(translator, op.func, frame)

    arg_formatted = translator.translate(op.func)
    result = f'{arg_formatted} {window_formatted}'

    if isinstance(op.func, ops.RankBase):
        return f'({result} - 1)'
    else:
        return result


def shift_like(name):
    def formatter(translator, op):
        arg, offset, default = op.args

        arg_formatted = translator.translate(arg)

        if default is not None:
            if offset is None:
                offset_formatted = '1'
            else:
                offset_formatted = translator.translate(offset)

            default_formatted = translator.translate(default)

            return '{}({}, {}, {})'.format(
                name, arg_formatted, offset_formatted, default_formatted
            )
        elif offset is not None:
            offset_formatted = translator.translate(offset)
            return f'{name}({arg_formatted}, {offset_formatted})'
        else:
            return f'{name}({arg_formatted})'

    return formatter


def ntile(translator, op):
    return f'ntile({translator.translate(op.buckets)})'
