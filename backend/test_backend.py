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

from analyzer import (
    _parse_printed_dimension,
    _split_ocr_room_line,
    analyze_rooms,
    classify_label,
    normalize_label,
)
from extractor import (
    _format_ocr_text,
    _parse_tesseract_tsv,
    build_json_payload,
    read_page_text,
)

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
            "BALCONY": "balcony",
            "TERRACE": "balcony",
        }
        for text, expected in cases.items():
            result = classify_label(text)
            self.assertIsNotNone(result, f"{text} should classify")
            self.assertEqual(result["room_type"], expected, text)
            self.assertEqual(result["detection"], "explicit", text)

    def test_inferred_abbreviations(self):
        cases = {
            "BED RM": "bedroom",
            "B.R. #2": "bedroom",
            "KIT": "kitchen",
            "LIV RM": "living_room",
        }
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

    def test_selectable_text_source_is_preserved(self):
        from extractor import extract_pdf

        info, previews = extract_pdf(load_sample())
        self.assertEqual(info["pages"][0]["text_source"], "selectable")
        self.assertIn(0, previews)

    def test_backend_reuses_extracted_text_for_analysis(self):
        from backend.service import build_result

        result = build_result(load_sample(), "sample_floorplan.pdf")
        self.assertEqual(result["summary"]["pages_with_selectable_text"], 1)
        self.assertEqual(result["summary"]["pages_with_ocr_text"], 0)
        self.assertEqual(result["analysis"]["summary"]["total_rooms"], 4)

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


class TestOpeningCounting(unittest.TestCase):
    """Door/window counts from schedules, plan tags and text labels."""

    def test_schedule_of_openings_counts_types(self):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=600, height=400)
        page.insert_text((380, 50), "SCHEDULE OF OPENINGS")
        page.insert_text((380, 70), "D1  1000 x 2100  Main Door")
        page.insert_text((380, 85), "D2  900 x 2100  Internal Door")
        page.insert_text((380, 100), "D3  800 x 2100  Store Door")
        page.insert_text((380, 115), "W1  1500 x 1200  Sliding Window")
        page.insert_text((380, 130), "W2  1200 x 1200  Sliding Window")
        data = doc.tobytes()
        doc.close()

        result = analyze_rooms(data, use_image_regions=False)
        self.assertEqual(result["summary"]["doors"]["count"], 3)
        self.assertEqual(result["summary"]["doors"]["status"], "detected_from_schedule")
        self.assertEqual(result["summary"]["windows"]["count"], 2)
        self.assertEqual(
            result["summary"]["windows"]["status"], "detected_from_schedule"
        )

    def test_plan_opening_tags_count_occurrences(self):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=600, height=400)
        page.insert_text((50, 50), "BEDROOM")
        page.insert_text((100, 100), "D1")
        page.insert_text((100, 150), "D2")
        page.insert_text((200, 150), "W1")
        page.insert_text((200, 200), "W2")
        data = doc.tobytes()
        doc.close()

        result = analyze_rooms(data, use_image_regions=False)
        self.assertEqual(result["summary"]["doors"]["count"], 2)
        self.assertEqual(result["summary"]["doors"]["status"], "detected_from_plan_tags")
        self.assertEqual(result["summary"]["windows"]["count"], 2)
        self.assertEqual(
            result["summary"]["windows"]["status"], "detected_from_plan_tags"
        )

    def test_ocr_confused_opening_id_is_corrected(self):
        from analyzer import _canonical_opening_tag

        self.assertEqual(_canonical_opening_tag("W", "i"), "W1")
        self.assertEqual(_canonical_opening_tag("D", "l"), "D1")
        self.assertEqual(_canonical_opening_tag("W", "O"), "W0")


class TestNoTextAndNoLabels(unittest.TestCase):
    """Honesty rules for PDFs without usable labels."""

    def test_ocr_display_text_is_readable_without_spatial_padding(self):
        text = _format_ocr_text(
            [
                {"text": "LEFT LABEL", "bbox": [0, 0, 50, 10]},
                {"text": "RIGHT LABEL", "bbox": [300, 20, 350, 30]},
            ]
        )
        lines = text.splitlines()
        self.assertEqual(lines[0], "LEFT LABEL")
        self.assertEqual(lines[1], "RIGHT LABEL")

    def test_printed_room_dimensions_are_read_from_drawing_text(self):
        import pymupdf

        doc = pymupdf.open()
        doc.new_page(width=400, height=400)
        data = doc.tobytes()
        doc.close()

        result = analyze_rooms(
            data,
            use_image_regions=False,
            extracted_pages=[
                {
                    "text": "BEDROOM 1\n4.50 X 2.10 m",
                    "text_source": "ocr",
                    "lines": [
                        {"text": "BEDROOM 1", "bbox": [100, 100, 180, 115]},
                        {"text": "4.50 X 2.10 m", "bbox": [102, 120, 185, 135]},
                    ],
                }
            ],
        )

        self.assertEqual(result["summary"]["total_rooms"], 1)
        room = result["rooms"][0]
        self.assertEqual(room["printed_dimensions"], "4.50 × 2.10 m")
        self.assertEqual((room["width_mm"], room["height_mm"]), (4500.0, 2100.0))
        self.assertEqual(room["area_m2"], 9.45)
        self.assertEqual(room["dimension_status"], "read_from_drawing")

    def test_feet_and_inches_dimensions_are_read_from_drawing_text(self):
        dimension = _parse_printed_dimension('11\'6" X 10\'6"')
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["text"], '11\'6" × 10\'6"')
        self.assertEqual(dimension["width_mm"], 3505.2)
        self.assertEqual(dimension["height_mm"], 3200.4)
        self.assertEqual(dimension["area_m2"], 11.22)

    def test_hyphenated_feet_and_inches_dimensions_are_read(self):
        # AutoCAD-style labels put a hyphen between feet and inches:
        # 10'-7" x 11'-2"
        cases = {
            '10\'-7" X 11\'-2"': (3225.8, 3403.6, 10.98),
            '13\'-11" X 13\'-2"': (4241.8, 4013.2, 17.02),
            '5\'-0" X 7\'-3"': (1524.0, 2209.8, 3.37),
        }
        for text, (width_mm, height_mm, area_m2) in cases.items():
            dimension = _parse_printed_dimension(text)
            self.assertIsNotNone(dimension, text)
            self.assertEqual(dimension["width_mm"], width_mm, text)
            self.assertEqual(dimension["height_mm"], height_mm, text)
            self.assertEqual(dimension["area_m2"], area_m2, text)

    def test_hyphenated_feet_and_inches_without_marks_are_read(self):
        # Low-resolution scans often lose the prime marks.
        dimension = _parse_printed_dimension('8-7"x7-3"')
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["text"], '8\'7" × 7\'3"')
        self.assertEqual(dimension["width_mm"], 2616.2)
        self.assertEqual(dimension["height_mm"], 2209.8)
        # Implausible inch values are rejected.
        self.assertIsNone(_parse_printed_dimension("8-15 x 7-3"))

    def test_split_ocr_words_reconstruct_feet_and_inches_dimensions(self):
        dimension = _parse_printed_dimension('11\'6" X 10\'6"')
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["text"], '11\'6" × 10\'6"')

    def test_low_confidence_room_words_are_kept_but_noise_is_dropped(self):
        # Tesseract can report a zero confidence for a correctly read room
        # name (e.g. "Kitchen") in sparse-text mode; keep it, but still
        # discard unrelated low-confidence noise.
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
            "top\twidth\theight\tconf\ttext\n"
        )
        tsv = (
            header
            + "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t0.0\tKitchen\n"
            + "5\t1\t1\t1\t2\t1\t10\t40\t80\t20\t0.0\twxyz\n"
        )
        lines = _parse_tesseract_tsv(tsv, 100, 100, 100, 100)
        texts = [line["text"] for line in lines]
        self.assertIn("Kitchen", texts)
        self.assertNotIn("wxyz", texts)

    def test_ocr_k_x_ambiguity_is_accepted_in_dimension(self):
        dimension = _parse_printed_dimension("3.70 K 2.10 m")
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["width_mm"], 3700.0)
        self.assertEqual(dimension["height_mm"], 2100.0)

    def test_unitless_room_dimensions_are_read_as_metres(self):
        dimension = _parse_printed_dimension("3.50 x 3.00")
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["width_mm"], 3500.0)
        self.assertEqual(dimension["height_mm"], 3000.0)
        self.assertEqual(dimension["area_m2"], 10.5)

    def test_large_unitless_room_dimensions_are_read_as_millimetres(self):
        dimension = _parse_printed_dimension("3000 x 3000")
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension["width_mm"], 3000.0)
        self.assertEqual(dimension["height_mm"], 3000.0)

    def test_same_line_room_label_and_unitless_dimension(self):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=600, height=400)
        page.insert_text((50, 50), "BED ROOM 3.50 x 3.00", fontsize=10)
        page.insert_text((50, 80), "SCALE : 1 : 100", fontsize=10)
        data = doc.tobytes()
        doc.close()

        result = analyze_rooms(data)
        self.assertEqual(result["summary"]["total_rooms"], 1)
        room = result["rooms"][0]
        self.assertEqual(room["printed_dimensions"], "3.50 × 3.00 m")
        self.assertEqual((room["width_mm"], room["height_mm"]), (3500.0, 3000.0))
        self.assertEqual(room["dimension_status"], "read_from_drawing")

    def test_combined_label_with_unitless_dimension_keeps_label(self):
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=600, height=400)
        page.insert_text((50, 50), "LIVING / DINING 6.00 x 3.00", fontsize=10)
        data = doc.tobytes()
        doc.close()

        result = analyze_rooms(data)
        self.assertEqual(result["summary"]["total_rooms"], 1)
        room = result["rooms"][0]
        self.assertEqual(room["room_type"], "living_room")
        self.assertEqual(room["width_mm"], 6000.0)
        self.assertEqual(room["height_mm"], 3000.0)

    def test_adjacent_ocr_room_labels_are_separated(self):
        segments = _split_ocr_room_line(
            {
                "text": "Bath room Bath room",
                "bbox": [10, 10, 150, 30],
                "words": [
                    {"text": "Bath", "bbox": [10, 10, 35, 30]},
                    {"text": "room", "bbox": [40, 10, 65, 30]},
                    {"text": "Bath", "bbox": [100, 10, 125, 30]},
                    {"text": "room", "bbox": [130, 10, 150, 30]},
                ],
            }
        )
        self.assertEqual([segment["text"] for segment in segments],
                         ["Bath room", "Bath room"])
        mixed = _split_ocr_room_line(
            {
                "text": "Bath room Bed roont",
                "bbox": [10, 10, 150, 30],
                "words": [
                    {"text": "Bath", "bbox": [10, 10, 35, 30]},
                    {"text": "room", "bbox": [40, 10, 65, 30]},
                    {"text": "Bed", "bbox": [90, 10, 115, 30]},
                    {"text": "roont", "bbox": [120, 10, 150, 30]},
                ],
            }
        )
        self.assertEqual(
            [classify_label(segment["text"])["room_type"] for segment in mixed],
            ["bathroom", "bedroom"],
        )

    def test_no_text_pdf(self):
        import pymupdf
        from unittest.mock import patch

        doc = pymupdf.open()
        doc.new_page(width=400, height=400)
        data = doc.tobytes()
        doc.close()
        with patch("analyzer.is_ocr_available", return_value=False), patch(
            "extractor.is_ocr_available", return_value=False
        ):
            result = analyze_rooms(data)
        self.assertEqual(result["status"], "no_text")
        self.assertFalse(result["classification_reliable"])
        self.assertEqual(result["summary"]["total_rooms"], 0)
        self.assertIn("Install Tesseract OCR", result["message"])

    def test_ocr_fallback_reads_text_when_tesseract_is_available(self):
        from io import BytesIO
        from types import SimpleNamespace
        from unittest.mock import patch

        from PIL import Image

        png = BytesIO()
        Image.new("RGB", (300, 400), "white").save(png, format="PNG")

        class FakePage:
            rect = SimpleNamespace(x0=0, y0=0, width=100, height=200)

            def get_text(self, kind):
                if kind != "text":
                    raise AssertionError(f"Unexpected extraction kind: {kind}")
                return ""

            def get_pixmap(self, matrix, alpha):
                if alpha is not False or matrix.a != 200 / 72:
                    raise AssertionError("Unexpected OCR render settings.")

                class FakePixmap:
                    width = 300
                    height = 400

                    def tobytes(self, image_format):
                        if image_format != "png":
                            raise AssertionError("Expected PNG output.")
                        return png.getvalue()

                return FakePixmap()

        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
            "top\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t30\t30\t50\t10\t90.0\tLIVING\n"
            "5\t1\t1\t1\t1\t2\t90\t30\t45\t10\t80.0\tROOM\n"
            "5\t1\t1\t1\t2\t1\t20\t80\t30\t10\t5.0\tnoise\n"
        )
        fake_process = SimpleNamespace(stdout=tsv.encode("utf-8"))
        with patch("extractor.shutil.which", return_value="tesseract.exe"), patch(
            "extractor.subprocess.run", return_value=fake_process
        ) as run:
            text, source, ocr_lines = read_page_text(FakePage())

        self.assertIn("LIVING ROOM", text)
        self.assertEqual(source, "ocr")
        living_room = next(line for line in ocr_lines if line["text"] == "LIVING ROOM")
        self.assertEqual(living_room["bbox"], [2.5, 3.75, 11.25, 5.0])
        self.assertIn("--psm", run.call_args.args[0])
        self.assertIn("tsv", run.call_args.args[0])

    def test_selectable_text_does_not_suppress_ocr_for_embedded_plan_images(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from PIL import Image

        class FakePage:
            rect = SimpleNamespace(x0=0, y0=0, width=100, height=200)

            def get_text(self, kind):
                if kind == "text":
                    return "DRAWING TITLE"
                if kind == "dict":
                    return {
                        "blocks": [
                            {
                                "type": 0,
                                "lines": [
                                    {
                                        "spans": [{"text": "DRAWING TITLE"}],
                                        "bbox": [10, 10, 80, 20],
                                    }
                                ],
                            }
                        ]
                    }
                raise AssertionError(f"Unexpected extraction kind: {kind}")

            def get_images(self, full):
                return [(1,)]

        rows = [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
            "top\twidth\theight\tconf\ttext"
        ]
        for index, word in enumerate(
            ("BEDROOM", "KITCHEN", "BATHROOM", "LIVING", "DINING", "STUDY", "STORE", "BALCONY"),
            start=1,
        ):
            rows.append(
                f"5\t1\t1\t1\t{index}\t1\t10\t{index * 15}\t30\t10\t90\t{word}"
            )
        fake_process = SimpleNamespace(stdout=("\n".join(rows) + "\n").encode())
        with patch("extractor.is_ocr_available", return_value=True), patch(
            "extractor._embedded_page_image",
            return_value=(
                Image.new("L", (300, 400), "white"),
                FakePage.rect,
            ),
        ), patch("extractor.subprocess.run", return_value=fake_process):
            text, source, lines = read_page_text(FakePage())

        self.assertEqual(source, "mixed")
        self.assertIn("DRAWING TITLE", text)
        self.assertIn("KITCHEN", text)
        self.assertIn("selectable", {line.get("text_source") for line in lines})
        self.assertIn("ocr", {line.get("text_source") for line in lines})

    def test_analyzer_uses_ocr_text_and_reduces_confidence(self):
        import pymupdf

        doc = pymupdf.open()
        doc.new_page(width=400, height=400)
        data = doc.tobytes()
        doc.close()
        result = analyze_rooms(
            data,
            extracted_pages=[
                {
                    "text": "BEDROOM",
                    "text_source": "ocr",
                    "lines": [{"text": "BEDROOM", "bbox": [100, 100, 160, 115]}],
                }
            ],
        )

        self.assertEqual(result["summary"]["total_rooms"], 1)
        self.assertEqual(result["rooms"][0]["label"], "BEDROOM")
        self.assertEqual(result["rooms"][0]["confidence"], "medium")
        self.assertEqual(result["diagnostics"]["pages_with_ocr_text"], 1)
        self.assertTrue(any("Verify OCR-read labels" in note for note in result["notes"]))

    def test_project_sources_have_no_ocr_imports(self):
        """Tesseract integration must not add another Python OCR dependency."""
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
        """Optional OCR Python runtimes are not needed for the Tesseract path."""
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
