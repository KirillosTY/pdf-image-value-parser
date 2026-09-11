"""Define JSON Schemas that force valid VLM structured output per chart family.

Each entry is a plain JSON Schema ``dict`` so it drops straight into Ollama's
``format`` field when the VLM extracts a chart. The organising idea is that a
*chart type* is not a schema: what fixes the shape of the output is the *data
family* the chart belongs to, and many chart types share one family (a grouped
bar and a stacked bar are both ``category``/``series``/``value``). So the data
item schemas below are defined once per family and reused, while a shared
``_METADATA_PROPS`` block (title, axis labels, unit, orientation, stacking,
source) spreads into every schema. Only the data array shape changes.

The exported :data:`CHART_SCHEMAS` maps each chart type to its schema, and
:func:`schema_for` resolves a Docling classification label to the right schema,
falling back to ``UNKNOWN`` for anything Docling routes to ``other``.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Shared metadata
# --------------------------------------------------------------------------- #
# Optional descriptors common to every chart. They are never ``required`` so the
# VLM may omit any it cannot read from the figure. ``orientation`` and
# ``stacking`` are only meaningful for some families but stay uniform here so a
# single block spreads into all schemas.
_METADATA_PROPS: dict[str, dict] = {
    "title": {"type": "string"},
    "x_axis_label": {"type": "string"},
    "y_axis_label": {"type": "string"},
    "unit": {"type": "string"},
    "orientation": {"type": "string", "enum": ["vertical", "horizontal"]},
    "stacking": {"type": "string", "enum": ["none", "stacked", "percent"]},
    "source": {"type": "string"},
}


def _array(items: dict) -> dict:
    """Wrap an item schema in an array schema."""
    return {"type": "array", "items": items}


def _obj(properties: dict[str, dict], required: list[str]) -> dict:
    """Build an object schema from its properties and required keys."""
    return {"type": "object", "properties": properties, "required": required}


def _schema(chart_type: str, data_props: dict[str, dict], required: list[str]) -> dict:
    """Compose one chart schema: a ``chart_type`` const, shared metadata, data.

    ``data_props`` carries the family-specific payload (usually a single
    ``data`` array, but ``flow_chart`` uses ``nodes``/``edges`` and ``unknown``
    uses ``observations``). ``required`` names the payload keys that must be
    present; ``chart_type`` is always required and prepended here.
    """
    return {
        "type": "object",
        "properties": {
            "chart_type": {"type": "string", "const": chart_type},
            **_METADATA_PROPS,
            **data_props,
        },
        "required": ["chart_type", *required],
    }


# --------------------------------------------------------------------------- #
# Data-family item schemas (each defined once, reused across chart types)
# --------------------------------------------------------------------------- #

# Family: tidy category / series / value. Covers bar, line, area, radar, combo.
# A single series collapses to one distinct ``series`` value.
_CATEGORY_SERIES_VALUE = _obj(
    {
        "category": {"type": "string"},
        "series": {"type": "string"},
        "value": {"type": "number"},
    },
    ["category", "series", "value"],
)

# Family: part-of-whole / ordered stages. Covers pie and funnel.
_LABEL_VALUE = _obj(
    {"label": {"type": "string"}, "value": {"type": "number"}},
    ["label", "value"],
)

# Family: xy points (scatter). ``series`` is optional for grouped point clouds.
_XY_POINT = _obj(
    {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "series": {"type": "string"},
    },
    ["x", "y"],
)

# Family: xy points + size (bubble).
_XY_SIZE_POINT = _obj(
    {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "size": {"type": "number"},
        "series": {"type": "string"},
    },
    ["x", "y", "size"],
)

# Family: histogram bins (half-open range + count).
_BIN = _obj(
    {
        "bin_start": {"type": "number"},
        "bin_end": {"type": "number"},
        "count": {"type": "number"},
    },
    ["bin_start", "bin_end", "count"],
)

# Family: five-number summary per group (box plot).
_BOX = _obj(
    {
        "group": {"type": "string"},
        "minimum": {"type": "number"},
        "q1": {"type": "number"},
        "median": {"type": "number"},
        "q3": {"type": "number"},
        "maximum": {"type": "number"},
    },
    ["group", "minimum", "q1", "median", "q3", "maximum"],
)

# Family: matrix cell (heatmap). Axes are categorical; ``value`` is the colour.
_MATRIX_CELL = _obj(
    {
        "x": {"type": "string"},
        "y": {"type": "string"},
        "value": {"type": "number"},
    },
    ["x", "y", "value"],
)

# Family: financial OHLC per period (candlestick).
_OHLC = _obj(
    {
        "date": {"type": "string"},
        "open": {"type": "number"},
        "high": {"type": "number"},
        "low": {"type": "number"},
        "close": {"type": "number"},
    },
    ["date", "open", "high", "low", "close"],
)

# Family: ordered deltas with running total flag (waterfall).
_WATERFALL_STEP = _obj(
    {
        "label": {"type": "string"},
        "value": {"type": "number"},
        "is_total": {"type": "boolean"},
    },
    ["label", "value", "is_total"],
)

# Family: graph nodes and edges (flow chart / diagram, not a data chart).
_FLOW_NODE = _obj(
    {"id": {"type": "string"}, "label": {"type": "string"}},
    ["id", "label"],
)
_FLOW_EDGE = _obj(
    {
        "source": {"type": "string"},
        "target": {"type": "string"},
        "label": {"type": "string"},
    },
    ["source", "target"],
)


# --------------------------------------------------------------------------- #
# The 16 chart schemas
# --------------------------------------------------------------------------- #

# 1. Vertical / horizontal / grouped / stacked / percent all reduce to tidy
#    category-series-value; orientation and stacking are read from metadata.
BAR_CHART = _schema("BAR_CHART", {"data": _array(_CATEGORY_SERIES_VALUE)}, ["data"])

# 2.
LINE_CHART = _schema("LINE_CHART", {"data": _array(_CATEGORY_SERIES_VALUE)}, ["data"])

# 3. Stacking flag lives in metadata.
AREA_CHART = _schema("AREA_CHART", {"data": _array(_CATEGORY_SERIES_VALUE)}, ["data"])

# 4. Axes are the categories; each series is one ring.
RADAR_CHART = _schema("RADAR_CHART", {"data": _array(_CATEGORY_SERIES_VALUE)}, ["data"])

# 5. Part-of-whole; ``donut`` distinguishes a doughnut from a full pie.
PIE_CHART = _schema(
    "PIE_CHART",
    {"data": _array(_LABEL_VALUE), "donut": {"type": "boolean"}},
    ["data"],
)

# 6. Ordered stages, largest to smallest.
FUNNEL_CHART = _schema("FUNNEL_CHART", {"data": _array(_LABEL_VALUE)}, ["data"])

# 7.
SCATTER_PLOT = _schema("SCATTER_PLOT", {"data": _array(_XY_POINT)}, ["data"])

# 8.
BUBBLE_CHART = _schema("BUBBLE_CHART", {"data": _array(_XY_SIZE_POINT)}, ["data"])

# 9.
HISTOGRAM = _schema("HISTOGRAM", {"data": _array(_BIN)}, ["data"])

# 10.
BOX_PLOT = _schema("BOX_PLOT", {"data": _array(_BOX)}, ["data"])

# 11.
HEATMAP = _schema("HEATMAP", {"data": _array(_MATRIX_CELL)}, ["data"])

# 12.
CANDLESTICK = _schema("CANDLESTICK", {"data": _array(_OHLC)}, ["data"])

# 13.
WATERFALL = _schema("WATERFALL", {"data": _array(_WATERFALL_STEP)}, ["data"])

# 14. A diagram: nodes plus directed edges rather than a data array.
FLOW_CHART = _schema(
    "FLOW_CHART",
    {"nodes": _array(_FLOW_NODE), "edges": _array(_FLOW_EDGE)},
    ["nodes", "edges"],
)

# 15. Dual-axis: tidy category-series-value plus a per-series render type and an
#     optional secondary axis label.
COMBO_CHART = _schema(
    "COMBO_CHART",
    {
        "data": _array(_CATEGORY_SERIES_VALUE),
        "series_types": _array(
            _obj(
                {
                    "series": {"type": "string"},
                    "render_type": {"type": "string", "enum": ["bar", "line", "area"]},
                    "axis": {"type": "string", "enum": ["primary", "secondary"]},
                },
                ["series", "render_type"],
            )
        ),
        "secondary_y_axis_label": {"type": "string"},
    },
    ["data", "series_types"],
)

# 16. Fallback for anything Docling dumps in ``other``: keep the metadata and
#     collect free-form observations instead of structured data.
UNKNOWN = _schema(
    "UNKNOWN",
    {"observations": _array({"type": "string"})},
    ["observations"],
)


# --------------------------------------------------------------------------- #
# Registry and routing
# --------------------------------------------------------------------------- #

#: Every schema keyed by its ``chart_type`` const.
CHART_SCHEMAS: dict[str, dict] = {
    "BAR_CHART": BAR_CHART,
    "LINE_CHART": LINE_CHART,
    "AREA_CHART": AREA_CHART,
    "RADAR_CHART": RADAR_CHART,
    "PIE_CHART": PIE_CHART,
    "FUNNEL_CHART": FUNNEL_CHART,
    "SCATTER_PLOT": SCATTER_PLOT,
    "BUBBLE_CHART": BUBBLE_CHART,
    "HISTOGRAM": HISTOGRAM,
    "BOX_PLOT": BOX_PLOT,
    "HEATMAP": HEATMAP,
    "CANDLESTICK": CANDLESTICK,
    "WATERFALL": WATERFALL,
    "FLOW_CHART": FLOW_CHART,
    "COMBO_CHART": COMBO_CHART,
    "UNKNOWN": UNKNOWN,
}

#: Map Docling's snake_case classification labels onto chart types. Docling only
#: routes bar / line / pie / flow with confidence; everything else lands in the
#: ``UNKNOWN`` fallback via :func:`schema_for`.
_DOCLING_ROUTING: dict[str, str] = {
    "bar_chart": "BAR_CHART",
    "line_chart": "LINE_CHART",
    "pie_chart": "PIE_CHART",
    "flow_chart": "FLOW_CHART",
    "scatter_plot": "SCATTER_PLOT",
    "box_plot": "BOX_PLOT",
}


def schema_for(class_name: str | None) -> dict:
    """Return the schema for a Docling classification label, or ``UNKNOWN``.

    Matching is case-insensitive; an unrecognised or missing label resolves to
    the ``UNKNOWN`` fallback so the VLM always receives a valid ``format``.
    """
    if not class_name:
        return UNKNOWN
    chart_type = _DOCLING_ROUTING.get(class_name.strip().lower())
    return CHART_SCHEMAS.get(chart_type, UNKNOWN) if chart_type else UNKNOWN
