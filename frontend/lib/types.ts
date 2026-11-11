export interface PageInfo {
  page_number: number;
  width_points: number;
  height_points: number;
  width_mm: number;
  height_mm: number;
  rotation_degrees: number;
  text_characters: number;
  text_source: "selectable" | "ocr" | "mixed" | "ocr_empty" | "none";
  vector_paths: number | null;
  vector_segments: number | null;
  text: string;
}

export interface DocumentInfo {
  page_count: number;
  pdf_format: string;
  title: string;
  author: string;
  producer: string;
  creation_date: string;
}

export interface PreviewImage {
  page_number: number;
  png_base64: string;
}

export interface ExtractionSummary {
  page_count: number;
  pages_with_selectable_text: number;
  pages_without_selectable_text: number;
  pages_with_ocr_text: number;
  total_text_characters: number;
  total_vector_paths: number | null;
  total_vector_segments: number | null;
}

export type RoomType =
  | "bedroom"
  | "kitchen"
  | "living_room"
  | "bathroom"
  | "dining_room"
  | "study"
  | "store"
  | "corridor"
  | "balcony"
  | "other";

export interface RoomRegion {
  source: string;
  bbox: [number, number, number, number];
}

export interface Room {
  id: string;
  label: string;
  merged_labels: string[];
  room_type: RoomType;
  type_label: string;
  page: number;
  detection: "explicit" | "inferred";
  confidence: "high" | "medium" | "low";
  bbox: [number, number, number, number];
  region: RoomRegion | null;
  width_mm: number | null;
  height_mm: number | null;
  area_m2: number | null;
  printed_dimensions: string | null;
  dimension_status: string;
  combined_label: boolean;
  also_matches: RoomType[];
}

export interface OpeningSummary {
  count: number | null;
  status: string;
}

export interface AnalysisSummary {
  total_rooms: number;
  bedrooms: number;
  kitchens: number;
  living_rooms: number;
  bathrooms: number;
  other_rooms: number;
  explicit_labels: number;
  inferred_labels: number;
  dimensions_available: boolean;
  doors: OpeningSummary;
  windows: OpeningSummary;
}

export interface ScaleInfo {
  found: boolean;
  ratio: number | null;
  text: string | null;
}

export interface AnalysisDiagnostics {
  pages_analyzed: number;
  pages_with_room_labels: number;
  pages_with_embedded_images: number;
  pages_with_ocr_text: number;
  pages_ocr_attempted: number;
  ocr_available: boolean;
  text_lines_scanned: number;
  room_label_candidates: number;
  duplicate_labels_merged: number;
  opening_duplicates_merged: number;
  vector_regions_matched: number;
  image_regions_matched: number;
  image_regions_detected: number;
  image_pages_segmented: number;
  image_region_detection: string;
  region_method: string;
  analysis_ms: number;
}

export interface RoomAnalysis {
  status: "ok" | "no_labels" | "no_text";
  classification_reliable: boolean;
  message: string;
  summary: AnalysisSummary;
  rooms: Room[];
  notes: string[];
  scale: ScaleInfo;
  diagnostics: AnalysisDiagnostics;
}

export interface ExtractResponse {
  filename: string;
  file_size_bytes: number;
  preview_dpi: number;
  count_vectors: boolean;
  document: DocumentInfo;
  pages: PageInfo[];
  previews: PreviewImage[];
  analysis: RoomAnalysis;
  summary: ExtractionSummary;
  json_payload: string;
}

export interface ApiError {
  error: string;
  code?: string | null;
  status: number;
}
