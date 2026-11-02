"""Backend tests for the room analyzer and FastAPI extract endpoint.

Run from the project root:

    .\\.venv\\Scripts\\python.exe -m unittest backend.test_backend -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from analyzer import analyze_rooms, classify_label, normalize_label
from extractor import build_json_payload

SAMPLE_PATH = Path(PROJECT_ROOT) / "samples" / "sample_floorplan.pdf"


def load_sample() -> bytes:
    return SAMPLE_PATH.read_bytes()


class TestLabelClassification(unittest.TestCase):
    """Unit tests for the room label classifier."""

    def test_normalize(self):
        self.assertEqual(normalize_label("  living   room "), "LIVING ROOM")

    def test_explicit_labels(self):
        cases = {
            "BEDROOM": "bedroom",
            "MASTER BEDROOM": "bedroom",
            "BEDROOM 1": "bedroom",
            "KITCHEN": "kitchen",
            "KITCHENETTE": "kitchen",
            "LIVING ROOM": "living_room",
            "LOUNGE": "living_room",
            "BATHROOM": "bathroom",
            "TOILET": "bathroom",
            "WC": "bathroom",
            "DINING ROOM": "dining_room",
            "STUDY": "study",
            "STORE": "store",
            "CORRIDOR": "corridor",
        }
        for text, expected in cases.items():
            result = classify_label(text)
            self.assertIsNotNone(result, f"{text} should classify")
            self.assertEqual(result["room_type"], expected, text)
            self.assertEqual(result["detection"], "explicit", text)

    def test_inferred_abbreviations(self):
        cases = {"BED RM": "bedroom", "KIT": "kitchen", "LIV RM": "living_room"}
        for text, expected in cases.items():
            result = classify_label(text)
            self.assertIsNotNone(result, text)
            self.assertEqual(result["room_type"], expected, text)
            self.assertEqual(result["detection"], "inferred", text)

    def test_non_labels_rejected(self):
        for text in ("ROOM SCHEDULE", "LEGEND", "SCALE 1:100", "DOOR SCHEDULE",
                     "NORTH ARROW", "TITLE BLOCK"):
            self.assertIsNone(classify_label(text), text)

    def test_schedule_rows_rejected(self):
        for text in (
            "1 Living Room 4.0 x 3.5 14.0",
            "5 Bedroom 2 3.0 x 3.0 9.0",
            "7 Toilet 1 2.0 x 1.5 3.0",
            "Total Indoor Area 70.8",
        ):
            self.assertIsNone(classify_label(text), text)


class TestSampleFloorPlan(unittest.TestCase):
    """Integration tests against samples/sample_floorplan.pdf."""

    @classmethod
    def setUpClass(cls):
        cls.result = analyze_rooms(load_sample())

    def test_status_ok_and_reliable(self):
        self.assertEqual(self.result["status"], "ok")
        self.assertTrue(self.result["classification_reliable"])

    def test_four_rooms_detected(self):
        self.assertEqual(self.result["summary"]["total_rooms"], 4)

    def test_room_types(self):
        labels = {room["label"]: room["room_type"] for room in self.result["rooms"]}
        self.assertEqual(labels.get("LIVING ROOM"), "living_room")
        self.assertEqual(labels.get("KITCHEN"), "kitchen")
        self.assertEqual(labels.get("BEDROOM 1"), "bedroom")
        self.assertEqual(labels.get("BEDROOM 2"), "bedroom")

    def test_summary_counts(self):
        summary = self.result["summary"]
        self.assertEqual(summary["bedrooms"], 2)
        self.assertEqual(summary["kitchens"], 1)
        self.assertEqual(summary["living_rooms"], 1)
        self.assertEqual(summary["bathrooms"], 0)
        self.assertEqual(summary["other_rooms"], 0)

    def test_all_explicit(self):
        for room in self.result["rooms"]:
            self.assertEqual(room["detection"], "explicit", room["label"])
        self.assertEqual(self.result["summary"]["inferred_labels"], 0)

    def test_page_numbers_preserved(self):
        for room in self.result["rooms"]:
            self.assertEqual(room["page"], 1)

    def test_no_scale_no_dimensions(self):
        # The sample has an OVERALL dimension note but no SCALE statement,
        # so per-room dimensions/areas must stay uncalculated.
        self.assertFalse(self.result["scale"]["found"])
        for room in self.result["rooms"]:
            self.assertIsNone(room["width_mm"], room["label"])
            self.assertIsNone(room["height_mm"], room["label"])
            self.assertIsNone(room["area_m2"], room["label"])
            self.assertEqual(room["dimension_status"], "not_calculated_no_scale")
        self.assertFalse(self.result["summary"]["dimensions_available"])

    def test_outer_dimensions_note_present(self):
        joined = " ".join(self.result["notes"])
        self.assertIn("Outer/overall dimensions", joined)
        self.assertIn("do NOT establish", joined)

    def test_vector_regions_associated(self):
        # Labels sit inside drawn wall rectangles; association should work.
        for room in self.result["rooms"]:
            self.assertIsNotNone(room["region"], room["label"])
            self.assertEqual(room["region"]["source"], "vector_rect")

    def test_doors_windows_not_invented(self):
        self.assertIsNone(self.result["summary"]["doors"]["count"])
        self.assertIsNone(self.result["summary"]["windows"]["count"])
        self.assertEqual(self.result["summary"]["doors"]["status"], "not_detected")
        self.assertEqual(self.result["summary"]["windows"]["status"], "not_detected")

    def test_json_payload_includes_analysis(self):
        payload = build_json_payload(
            {"document": {}, "pages": []}, "sample.pdf", 100,
            analysis=self.result,
        )
        import json
        parsed = json.loads(payload)
        self.assertIn("analysis", parsed)
        self.assertEqual(parsed["analysis"]["summary"]["total_rooms"], 4)


class TestNoTextAndNoLabels(unittest.TestCase):
    """Honesty rules for PDFs without usable labels."""

    def test_no_text_pdf(self):
        import pymupdf

        doc = pymupdf.open()
        doc.new_page(width=400, height=400)
        data = doc.tobytes()
        doc.close()
        result = analyze_rooms(data)
        self.assertEqual(result["status"], "no_text")
        self.assertFalse(result["classification_reliable"])
        self.assertEqual(result["summary"]["total_rooms"], 0)
        self.assertIn("OCR is currently unavailable", result["message"])
        self.assertIn("text-based CAD PDF", result["message"])

    def test_project_sources_have_no_ocr_imports(self):
        """OCR was removed; project code must not reference OCR libraries."""
        forbidden = ("rapidocr", "onnxruntime", "import cv2", "opencv",
                     "pytesseract", "tesseract")
        root = Path(PROJECT_ROOT)
        project_files = [
            root / "extractor.py",
            root / "analyzer.py",
            root / "app.py",
            root / "backend" / "main.py",
            root / "backend" / "test_backend.py",
        ]
        for path in project_files:
            source = path.read_text(encoding="utf-8")
            for term in forbidden:
                # Word-boundary style check on import statements only.
                for line in source.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("#", '"""', "'''")):
                        continue
                    if stripped.startswith(("import ", "from ")):
                        self.assertNotIn(
                            term, stripped,
                            f"{path.name} imports OCR module: {stripped}",
                        )

    def test_ocr_modules_not_installed(self):
        """ONNX Runtime / RapidOCR must not be present in the environment."""
        import importlib.util
        for module in ("onnxruntime", "rapidocr_onnxruntime", "cv2"):
            self.assertIsNone(
                importlib.util.find_spec(module),
                f"{module} should have been uninstalled",
            )

    def test_text_without_room_labels(self):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text((50, 50), "GENERAL ARRANGEMENT DRAWING")
        page.insert_text((50, 80), "NORTH ARROW")
        data = doc.tobytes()
        doc.close()
        result = analyze_rooms(data)
        self.assertEqual(result["status"], "no_labels")
        self.assertFalse(result["classification_reliable"])
        self.assertEqual(result["summary"]["total_rooms"], 0)
        self.assertIn("could not be reliably determined", result["message"])


if __name__ == "__main__":
    unittest.main()
