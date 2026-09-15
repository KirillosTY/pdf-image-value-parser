"""Define shared provenance and typed measurement tables for all chart schemas."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from schema_format import CHART_SCHEMAS

metadata = MetaData()

schema_versions = Table(
    "schema_versions",
    metadata,
    Column("schema_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("definition", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)

run_info = Table(
    "run_info",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("status", Text, nullable=False, server_default="incomplete"),
    Column(
        "started_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("completed_at", DateTime(timezone=True)),
    Column("schema_id", Text, ForeignKey("schema_versions.schema_id"), nullable=False),
    Column("config_snapshot", JSONB, nullable=False),
    Column("outcome", Text),
    Column("errors", JSONB),
    CheckConstraint("status IN ('incomplete', 'complete')", name="run_info_status"),
)

documents = Table(
    "documents",
    metadata,
    Column("manifest_key", Text, primary_key=True),
    Column("document_id", Text, index=True),
    Column("run_id", Text, ForeignKey("run_info.run_id")),
    Column("pdf_sha256", Text),
    Column("source_path", Text, nullable=False),
    Column("title", Text),
    Column("publication_year", Integer, index=True),
    Column("doi", Text, index=True),
    Column("metadata", JSONB, nullable=False),
    Column("snapshot_hash", Text, nullable=False),
    Column(
        "stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)
Index("ix_documents_run_document", documents.c.run_id, documents.c.document_id)
images = Table(
    "images",
    metadata,
    Column("image_key", Text, primary_key=True),
    Column(
        "manifest_key",
        Text,
        ForeignKey("documents.manifest_key"),
        nullable=False,
        index=True,
    ),
    Column("asset_id", Text, nullable=False),
    Column("page_number", Integer, nullable=False),
    Column("width", Integer, nullable=False),
    Column("height", Integer, nullable=False),
    Column("raw_output", Text),
    Column("failed", Boolean, nullable=False, server_default="false"),
    Column("vlm_model", Text),
    Column("context", JSONB),
)
charts = Table(
    "charts",
    metadata,
    Column("chart_id", Text, primary_key=True),
    Column(
        "image_key", Text, ForeignKey("images.image_key"), nullable=False, index=True
    ),
    Column("chart_type", Text, nullable=False, index=True),
    Column("panel_label", Text),
    Column("title", Text),
    Column("x_axis_label", Text),
    Column("y_axis_label", Text),
    Column("secondary_y_axis_label", Text),
    Column("unit", Text, index=True),
    Column("orientation", Text),
    Column("stacking", Text),
    Column("source", Text),
    Column("donut", Boolean),
)
extraction_details = Table(
    "extraction_details",
    metadata,
    Column("image_key", Text, ForeignKey("images.image_key"), primary_key=True),
    Column("formatter_model", Text),
    Column("schema", JSONB(none_as_null=True)),
    Column("model_response", Text),
    Column("formatted_data", JSONB(none_as_null=True)),
    Column("unmapped_observations", JSONB(none_as_null=True)),
)

# Names are fixed in code, never constructed from model output.
_MEASUREMENT_NAMES = {
    "BAR_CHART": "bar_values",
    "LINE_CHART": "line_values",
    "AREA_CHART": "area_values",
    "RADAR_CHART": "radar_values",
    "PIE_CHART": "pie_values",
    "FUNNEL_CHART": "funnel_values",
    "SCATTER_PLOT": "scatter_points",
    "BUBBLE_CHART": "bubble_points",
    "HISTOGRAM": "histogram_bins",
    "BOX_PLOT": "box_statistics",
    "HEATMAP": "heatmap_cells",
    "CANDLESTICK": "candlestick_values",
    "WATERFALL": "waterfall_values",
    "COMBO_CHART": "combo_values",
}
_SQL_TYPES = {"string": Text, "number": Numeric, "integer": Integer, "boolean": Boolean}


def measurement_table(name: str, item_schema: dict) -> Table:
    """Represent a schema's scalar measurement fields as actual SQL columns."""
    required = set(item_schema.get("required", []))
    table = Table(
        name,
        metadata,
        Column("chart_id", Text, ForeignKey("charts.chart_id"), primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        *(
            Column(field, _SQL_TYPES[schema["type"]], nullable=field not in required)
            for field, schema in item_schema["properties"].items()
        ),
    )
    if "category" in table.c and "series" in table.c:
        Index(f"ix_{name}_series_category", table.c.series, table.c.category)
    return table


MEASUREMENT_TABLES = {
    kind: measurement_table(name, CHART_SCHEMAS[kind]["properties"]["data"]["items"])
    for kind, name in _MEASUREMENT_NAMES.items()
}
flow_nodes = measurement_table(
    "flow_nodes", CHART_SCHEMAS["FLOW_CHART"]["properties"]["nodes"]["items"]
)
flow_edges = measurement_table(
    "flow_edges", CHART_SCHEMAS["FLOW_CHART"]["properties"]["edges"]["items"]
)
combo_series = measurement_table(
    "combo_series", CHART_SCHEMAS["COMBO_CHART"]["properties"]["series_types"]["items"]
)
unknown_observations = measurement_table(
    "unknown_observations",
    {"properties": {"observation": {"type": "string"}}, "required": ["observation"]},
)
