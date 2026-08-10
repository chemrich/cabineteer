"""Tests for the visualize module.

The generate_viewer_html / _build_html path works without CadQuery installed
(only needs file I/O + base64).  Tests that require CadQuery geometry are
skipped when the library is not present.
"""

from __future__ import annotations

import base64
import json
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cabineteer.visualize import _build_html, generate_viewer_html


# ── Helpers ───────────────────────────────────────────────────────────────────

def _minimal_glb() -> bytes:
    """Construct a minimal valid GLB so tests do not need real CadQuery output."""
    # JSON chunk
    json_data = json.dumps({
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
    }).encode("utf-8")
    pad = (4 - len(json_data) % 4) % 4
    json_data += b" " * pad

    json_chunk = struct.pack("<II", len(json_data), 0x4E4F534A) + json_data  # type JSON
    total = 12 + len(json_chunk)
    header = struct.pack("<III", 0x46546C67, 2, total)  # magic, version, length
    return header + json_chunk


# ── _build_html ───────────────────────────────────────────────────────────────

class TestBuildHtml:
    def test_returns_doctype(self):
        html = _build_html("My Cabinet", "ZmFrZQ==", {})
        assert html.strip().startswith("<!DOCTYPE html>")

    def test_contains_title(self):
        html = _build_html("Kitchen Base", "ZmFrZQ==", {})
        assert "Kitchen Base" in html

    def test_embeds_b64_data(self):
        b64 = "SGVsbG9Xb3JsZA=="
        html = _build_html("Test", b64, {})
        assert b64 in html

    def test_width_height_depth_in_panel(self):
        html = _build_html("Test", "ZmFrZQ==", {"width": 600, "height": 720, "depth": 550})
        assert "600" in html
        assert "720" in html
        assert "550" in html

    def test_unknown_info_keys_rendered(self):
        html = _build_html("Test", "ZmFrZQ==", {"openings": 3})
        assert "3" in html

    def test_closes_html_tag(self):
        html = _build_html("Test", "ZmFrZQ==", {})
        assert "</html>" in html

    def test_importmap_present(self):
        html = _build_html("Test", "ZmFrZQ==", {})
        assert "importmap" in html
        assert "three" in html

    def test_gltfloader_present(self):
        html = _build_html("Test", "ZmFrZQ==", {})
        assert "GLTFLoader" in html

    def test_orbit_controls_present(self):
        html = _build_html("Test", "ZmFrZQ==", {})
        assert "OrbitControls" in html


# ── generate_viewer_html ──────────────────────────────────────────────────────

class TestGenerateViewerHtml:
    def test_creates_html_file(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        result = generate_viewer_html(glb, html, title="Test Cabinet")

        assert result == html
        assert html.exists()

    def test_returns_resolved_path(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        result = generate_viewer_html(glb, html)

        assert result.is_absolute()

    def test_embeds_glb_as_base64(self, tmp_path):
        glb_data = _minimal_glb()
        glb = tmp_path / "model.glb"
        glb.write_bytes(glb_data)
        html = tmp_path / "viewer.html"

        generate_viewer_html(glb, html)

        expected_b64 = base64.b64encode(glb_data).decode("ascii")
        content = html.read_text(encoding="utf-8")
        assert expected_b64 in content

    def test_title_appears_in_html(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        generate_viewer_html(glb, html, title="My Fancy Cabinet")

        assert "My Fancy Cabinet" in html.read_text(encoding="utf-8")

    def test_cabinet_info_dimensions_appear(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        generate_viewer_html(
            glb, html,
            cabinet_info={"width": 900, "height": 800, "depth": 600},
        )

        content = html.read_text(encoding="utf-8")
        assert "900" in content
        assert "800" in content
        assert "600" in content

    def test_creates_parent_directories(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        nested = tmp_path / "a" / "b" / "c" / "viewer.html"

        generate_viewer_html(glb, nested)

        assert nested.exists()

    def test_no_info_dict_produces_valid_html(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        generate_viewer_html(glb, html)  # no cabinet_info

        content = html.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content

    def test_string_paths_accepted(self, tmp_path):
        """Path-like strings should work as well as Path objects."""
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html_str = str(tmp_path / "viewer.html")

        result = generate_viewer_html(str(glb), html_str)

        assert Path(result).exists()

    def test_utf8_encoding(self, tmp_path):
        glb = tmp_path / "model.glb"
        glb.write_bytes(_minimal_glb())
        html = tmp_path / "viewer.html"

        generate_viewer_html(glb, html, title="Küche")

        # Must be readable as UTF-8 without exception
        content = html.read_text(encoding="utf-8")
        assert "Küche" in content


# ── export_glb (requires CadQuery) ───────────────────────────────────────────

class TestExportGlb:
    def test_raises_without_cadquery(self, tmp_path):
        """ImportError when cadquery is not installed."""
        import cabineteer.visualize as viz

        original = viz.cq
        viz.cq = None
        try:
            with pytest.raises(ImportError, match="cadquery is required"):
                viz.export_glb(MagicMock(), tmp_path / "out.glb")
        finally:
            viz.cq = original

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("cadquery") is None,
        reason="cadquery not installed",
    )
    def test_export_produces_glb(self, tmp_path):
        """Integration: build a real cabinet and export to GLB."""
        import cadquery as cq
        from cabineteer.cabinet import CabinetConfig, build_cabinet
        from cabineteer.visualize import export_glb

        cfg = CabinetConfig(width=400, height=500, depth=350)
        assy, _ = build_cabinet(cfg)
        glb = tmp_path / "test.glb"
        result = export_glb(assy, glb)

        assert result == glb
        assert glb.exists()
        assert glb.stat().st_size > 0
        # GLB magic number: 0x46546C67 ("glTF")
        magic = glb.read_bytes()[:4]
        assert magic == b"glTF"


# ── build_and_visualize (requires CadQuery) ───────────────────────────────────

class TestBuildAndVisualize:
    def test_raises_without_cadquery(self, tmp_path):
        import cabineteer.visualize as viz

        original = viz.cq
        viz.cq = None
        try:
            from cabineteer.cabinet import CabinetConfig
            with pytest.raises(ImportError, match="cadquery is required"):
                viz.build_and_visualize(CabinetConfig(), output_dir=tmp_path)
        finally:
            viz.cq = original

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("cadquery") is None,
        reason="cadquery not installed",
    )
    def test_returns_correct_keys(self, tmp_path):
        from cabineteer.cabinet import CabinetConfig
        from cabineteer.visualize import build_and_visualize

        cfg = CabinetConfig(width=400, height=500, depth=350)
        result = build_and_visualize(cfg, output_dir=tmp_path, open_browser=False)

        assert "glb"  in result
        assert "html" in result
        assert "parts" in result
        assert "glb_size_kb" in result

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("cadquery") is None,
        reason="cadquery not installed",
    )
    def test_output_files_exist(self, tmp_path):
        from cabineteer.cabinet import CabinetConfig
        from cabineteer.visualize import build_and_visualize

        cfg = CabinetConfig(width=400, height=500, depth=350)
        result = build_and_visualize(
            cfg, output_dir=tmp_path, name="test_cab", open_browser=False
        )

        assert Path(result["glb"]).exists()
        assert Path(result["html"]).exists()

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("cadquery") is None,
        reason="cadquery not installed",
    )
    def test_html_is_self_contained(self, tmp_path):
        """The HTML must not reference the GLB by file path — only via base64."""
        from cabineteer.cabinet import CabinetConfig
        from cabineteer.visualize import build_and_visualize

        cfg = CabinetConfig(width=400, height=500, depth=350)
        result = build_and_visualize(
            cfg, output_dir=tmp_path, name="check_cab", open_browser=False
        )

        html_content = Path(result["html"]).read_text(encoding="utf-8")
        glb_filename  = Path(result["glb"]).name
        # The viewer should NOT load the GLB by filename — it's embedded
        assert f'"{glb_filename}"' not in html_content
        assert f"'{glb_filename}'" not in html_content
        # Instead it must contain base64 data
        assert "GLB_B64" in html_content


# ── visualize_project ────────────────────────────────────────────────────────


class TestVisualizeProject:
    def test_project_composed_at_run_offsets(self, tmp_path, monkeypatch):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_project

        payload = {
            "name": "viz_test",
            "cabinets": [
                {"name": "left",  "config": {"width": 600, "height": 720, "depth": 500,
                                             "drawer_config": [[150, "drawer"], [534, "drawer"]]}},
                {"name": "right", "config": {"width": 800, "height": 720, "depth": 500,
                                             "openings": [[684, "door"]]}},
            ],
        }
        out = asyncio.get_event_loop().run_until_complete(
            _tool_visualize_project({
                "project": payload,
                "gap_mm": 3,
                "open_browser": False,
                "output_dir": str(tmp_path),
            }))
        data = json.loads(out[0].text)

        assert data["cabinet_count"] == 2
        # left at 0, right at 600 + 3 mm gap
        offsets = {c["name"]: c["x_offset_mm"] for c in data["per_cabinet"]}
        assert offsets == {"left": 0.0, "right": 603.0}
        assert data["total_run_width_mm"] == 600 + 3 + 800
        assert Path(data["glb"]).exists()
        assert Path(data["html"]).exists()

    def test_missing_project_args_raises(self):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_project
        with pytest.raises(ValueError, match="project_name"):
            asyncio.get_event_loop().run_until_complete(_tool_visualize_project({}))

    def test_worktop_rendered_with_legs(self, tmp_path, monkeypatch):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_project

        payload = {
            "name": "viz_worktop",
            "cabinets": [
                {"name": "left",  "config": {"width": 381, "height": 1168, "depth": 457,
                                             "drawer_config": [[300, "drawer"], [832, "drawer"]]}},
                {"name": "right", "config": {"width": 381, "height": 1168, "depth": 457,
                                             "drawer_config": [[300, "drawer"], [832, "drawer"]]}},
            ],
            "worktop": {
                "width_mm": 1219.2, "depth_mm": 457.2, "thickness_mm": 19,
                "surface_height_mm": 660.4, "x_offset_mm": 381,
                "y_offset_mm": -18, "leg_count": 4,
            },
        }
        out = asyncio.get_event_loop().run_until_complete(
            _tool_visualize_project({
                "project": payload,
                "gap_mm": 1219.2,
                "furniture_top": True,
                "open_browser": False,
                "output_dir": str(tmp_path),
            }))
        data = json.loads(out[0].text)

        assert data["worktop"]["surface_height_mm"] == pytest.approx(660.4)
        # GLB node names live in the JSON chunk — slab and all four legs present.
        glb = Path(data["glb"]).read_bytes()
        assert b'"worktop"' in glb
        for i in range(4):
            assert f'"worktop_leg{i}"'.encode() in glb
        # furniture_top adds the top front cap strip per cabinet.
        assert glb.count(b'top_front_cap') >= 2


# ── Manga scale reference ────────────────────────────────────────────────────


class TestMangaScaleReference:
    _tower = {"width": 381, "height": 1168, "depth": 457,
              "drawer_config": [[300, "drawer"], [832, "drawer"]]}

    def test_manga_stack_in_glb(self, tmp_path):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_cabinet

        out = asyncio.get_event_loop().run_until_complete(
            _tool_visualize_cabinet({
                "name": "manga_test", **self._tower, "manga": True,
                "open_browser": False, "output_dir": str(tmp_path),
            }))
        data = json.loads(out[0].text)
        glb = Path(data["glb"]).read_bytes()
        for k in range(5):
            assert f'"manga{k}"'.encode() in glb
        html = Path(data["html"]).read_text(encoding="utf-8")
        assert "MANGA_NODE_RE" in html
        assert "cycleManga" in html
        assert "help-manga" in html

    def test_manga_off_by_default(self, tmp_path):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_cabinet

        out = asyncio.get_event_loop().run_until_complete(
            _tool_visualize_cabinet({
                "name": "manga_off", **self._tower,
                "open_browser": False, "output_dir": str(tmp_path),
            }))
        data = json.loads(out[0].text)
        glb = Path(data["glb"]).read_bytes()
        assert b'"manga0"' not in glb

    def test_short_drawer_raises_named_error(self, tmp_path):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_cabinet

        # A 104 mm opening snaps to a 76 mm box; interior ~58 mm < 75 mm stack.
        with pytest.raises(ValueError, match=r"bay0_drawer0.*manga"):
            asyncio.get_event_loop().run_until_complete(
                _tool_visualize_cabinet({
                    "name": "manga_short",
                    "width": 600, "height": 300, "depth": 457,
                    "drawer_config": [[104, "drawer"], [160, "drawer"]],
                    "manga": True,
                    "open_browser": False, "output_dir": str(tmp_path),
                }))

    def test_project_path_names_cabinet_in_error(self, tmp_path, monkeypatch):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_project

        payload = {
            "name": "viz_manga_err",
            "cabinets": [
                {"name": "shallow", "config": {"width": 600, "height": 300, "depth": 457,
                                               "drawer_config": [[104, "drawer"], [160, "drawer"]]}},
            ],
        }
        with pytest.raises(ValueError, match=r"cabinet 'shallow'.*bay0_drawer0"):
            asyncio.get_event_loop().run_until_complete(
                _tool_visualize_project({
                    "project": payload, "manga": True,
                    "open_browser": False, "output_dir": str(tmp_path),
                }))


# ── Shared junction feet ─────────────────────────────────────────────────────


class TestSharedJunctionFeet:
    def test_shared_junction_feet(self, tmp_path):
        pytest.importorskip("cadquery")
        import asyncio
        from cabineteer.server import _tool_visualize_project

        payload = {
            "name": "viz_shared_feet",
            "cabinets": [
                {"name": "a", "config": {"width": 400, "height": 500, "depth": 330,
                                         "fixed_shelf_positions": [240]}},
                {"name": "b", "config": {"width": 500, "height": 900, "depth": 330,
                                         "fixed_shelf_positions": [240]}},
            ],
        }

        def render(shared):
            out = asyncio.get_event_loop().run_until_complete(
                _tool_visualize_project({
                    "project": payload, "shared_junction_feet": shared,
                    "open_browser": False, "output_dir": str(tmp_path),
                }))
            return Path(json.loads(out[0].text)["glb"]).read_bytes()

        # Each foot contributes 3 name occurrences in the GLB JSON
        # (node + mesh + object entries).
        # Default: each cabinet carries 4 feet -> 8 total.
        assert render(False).count(b'"foot_') == 8 * 3
        # Shared: 2 ends + 1 junction = 3 stations x 2 = 6 feet.
        assert render(True).count(b'"foot_') == 6 * 3
