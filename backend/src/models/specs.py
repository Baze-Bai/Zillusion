"""Type-specific specs for the three data source categories."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.models.page_tree import DataPageTree


class APISpec(BaseModel):
    """API data source specific information."""

    endpoint: str
    method: str = "GET"
    auth_type: str = "none"  # api_key / oauth / hmac / none
    auth_location: str | None = None  # header / query / body
    auth_param_name: str | None = None
    signup_url: str | None = None
    signup_instructions: str | None = None
    openapi_spec_url: str | None = None
    has_sdk: bool = False
    example_code: str | None = None
    documentation_url: str | None = None


class FileSpec(BaseModel):
    """Downloadable file data source specific information."""

    download_url: str
    file_format: str  # csv / json / xlsx / parquet / xml / ...
    file_size: int | None = None
    content_type: str | None = None
    encoding: str | None = None
    compression: str | None = None  # gzip / zip / none
    column_headers: list[str] | None = None
    row_sample: list[dict[str, Any]] | None = None


class EmbeddedSpec(BaseModel):
    """Web-embedded data source specific information."""

    extraction_method: str  # json_ld / microdata / html_table / llm_extract
    data_shape: str  # table / list / prose / mixed
    schema_type: str | None = None  # Schema.org type
    fields_present: list[str] = Field(default_factory=list)
    coverage: float = 0.0  # 0-1 coverage of user's target schema
    extraction_difficulty: str = "medium"  # trivial / easy / medium / hard
    extraction_code_hint: str | None = None

    # === List page specifics (when is_list_page=True) ===
    is_list_page: bool = False
    page_tree: DataPageTree | None = None
