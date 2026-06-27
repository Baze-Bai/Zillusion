/** TypeScript mirrors of Python Pydantic models */

export type DataSourceType = "api" | "file" | "embedded";

export type AccessLevel =
  | "open"
  | "free_reg"
  | "api_key_free"
  | "api_key_paid"
  | "oauth"
  | "paywall"
  | "unknown";

export interface SourceScores {
  relevance: number;
  authority: number;
  freshness: number;
  accessibility: number;
  license_fit: number;
  overall: number;
  relevance_rationale: string;
  authority_rationale: string;
}

export interface APISpec {
  endpoint: string;
  method: string;
  auth_type: string;
  auth_location?: string;
  signup_url?: string;
  openapi_spec_url?: string;
  has_sdk: boolean;
  example_code?: string;
  documentation_url?: string;
}

export interface FileSpec {
  download_url: string;
  file_format: string;
  file_size?: number;
  content_type?: string;
  column_headers?: string[];
  row_sample?: Record<string, unknown>[];
}

export interface DataPageNode {
  url: string;
  page_type: string;
  title: string;
  depth: number;
  fields_available: string[];
  record_count?: number;
  children: DataPageNode[];
  is_sampled: boolean;
}

export interface DataPageTree {
  root: DataPageNode;
  total_detail_pages: number;
  sampled_detail_pages: number;
  field_progression: Record<string, string[]>;
  tree_summary: string;
}

export interface EmbeddedSpec {
  extraction_method: string;
  data_shape: string;
  schema_type?: string;
  fields_present: string[];
  coverage: number;
  extraction_difficulty: string;
  extraction_code_hint?: string;
  is_list_page: boolean;
  page_tree?: DataPageTree;
}

export interface DataSource {
  id: string;
  name: string;
  provider: string;
  url: string;
  source_type: DataSourceType;
  description: string;
  domain: string;
  tags: string[];
  data_format: string[];
  access_level: AccessLevel;
  license?: string;
  scores?: SourceScores;
  usage_guide?: string;
  api_spec?: APISpec;
  file_spec?: FileSpec;
  embedded_spec?: EmbeddedSpec;
  discovery_method: string;
}

export interface StructuredRequirement {
  original_query: string;
  title?: string;
  domain?: string;
  sub_domains?: string[];
  data_type_hints?: string[];
  desired_formats?: string[];
  geographic_scope?: string[];
  temporal_range?: string | null;
  update_frequency?: string | null;
  license_constraint?: string;
  budget_constraint?: string;
  sub_questions?: string[];
  target_schema?: Record<string, unknown> | null;
}

export interface FinalReport {
  query: string;
  requirement?: StructuredRequirement;
  api_sources: DataSource[];
  file_sources: DataSource[];
  embedded_sources: DataSource[];
  all_sources_ranked: DataSource[];
  total_found: number;
  coverage_summary: string;
  api_quickstart_guide?: string;
  file_download_guide?: string;
  embedded_extraction_guide?: string;
  iterations: number;
  total_candidates_screened: number;
  processing_time_seconds: number;
  estimated_cost_usd: number;
}

// SSE Event Types
export type SSEEvent =
  | { type: "query_accepted"; data: { query_id: string; query: string } }
  | { type: "stage_start"; data: { stage: string } }
  | { type: "stage_complete"; data: { stage: string; duration_ms: number; cost_usd: number } }
  | { type: "progress"; data: { stage: string; candidates_found?: number } }
  | { type: "partial_sources"; data: Partial<DataSource>[] }
  | { type: "done"; data: { report: FinalReport; query_id: string } }
  | { type: "error"; data: { message: string } };
