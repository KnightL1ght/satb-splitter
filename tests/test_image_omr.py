"""Tests for sheet music image & camera photo OMR preprocessing, CLI, worker, and GUI."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import fitz
import numpy as np
import pytest
from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from satb_splitter.gui.main_window import MainWindow
from satb_splitter.gui.workers import PipelineWorker
from satb_splitter.omr import (
    IMAGE_EXTS,
    images_to_pdf,
    is_image_path,
    preprocess_image_for_omr,
)
from satb_splitter.omr.audiveris import AudiverisRun
from satb_splitter.omr.image_prep import estimate_skew_angle, is_camera_photo
from satb_splitter.omr.preprocess import prepare_pages_for_omr


def _make_staves_image(
    width: int = 1800,
    height: int = 2400,
    *,
    num_systems: int = 4,
    bg_val: int = 255,
    ink_val: int = 15,
) -> Image.Image:
    """Render crisp 5-line choral staves with vertical barlines & noteheads."""
    img = Image.new("L", (width, height), color=bg_val)
    draw = ImageDraw.Draw(img)
    left, right = int(width * 0.10), int(width * 0.90)
    for sys_i in range(num_systems):
        y_top = int(height * (0.14 + sys_i * 0.21))
        for staff_offset in (0, 120):
            base = y_top + staff_offset
            for line_i in range(5):
                y = base + line_i * 14
                draw.line([(left, y), (right, y)], fill=ink_val, width=3)
        # barlines + noteheads
        for bx in range(left, right + 1, (right - left) // 4):
            draw.line([(bx, y_top), (bx, y_top + 120 + 56)], fill=ink_val, width=3)
        for nx in range(left + 40, right - 40, 90):
            draw.ellipse([nx - 7, y_top + 9, nx + 7, y_top + 21], fill=ink_val)
            draw.line([(nx + 7, y_top + 15), (nx + 7, y_top - 22)], fill=ink_val, width=2)
    return img


def test_image_exts_and_is_image_path():
    assert IMAGE_EXTS == frozenset(
        {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
    )
    for ext in (".png", ".JPG", ".jpeg", ".bmp", ".tif", ".TIFF", ".webp"):
        assert is_image_path(f"sheet{ext}")
    assert not is_image_path("score.pdf")
    assert not is_image_path("score.musicxml")
    assert not is_image_path("score.mxl")


def test_exif_orientation_transpose():
    # Create landscape 2200x1700 image with Orientation=6 (rotate 270 CW / 90 CCW -> 1700x2200)
    import io

    img = _make_staves_image(width=2200, height=1700)
    exif = img.getexif()
    exif[274] = 6  # Orientation = 6

    bio = io.BytesIO()
    img.save(bio, format="JPEG", exif=exif.tobytes())
    bio.seek(0)
    with Image.open(bio) as loaded:
        out, notes = preprocess_image_for_omr(loaded, photo_enhance="off", deskew=False)
    assert out.size == (1700, 2200)
    assert any("EXIF" in n for n in notes)


def test_is_camera_photo_detects_shadow_dim_warm_vs_clean_scan():
    clean = _make_staves_image(1200, 1600, bg_val=255, ink_val=10).convert("RGB")
    is_p, _ = is_camera_photo(clean)
    assert is_p is False

    # Uneven shadow gradient (>= 18 spread across 4x4 tiles)
    arr = np.asarray(clean, dtype=np.float32)
    grad = np.linspace(155.0, 250.0, arr.shape[1], dtype=np.float32)[None, :, None]
    shaded = Image.fromarray(np.clip(arr * (grad / 255.0), 0, 255).astype(np.uint8), "RGB")
    is_p, reason = is_camera_photo(shaded)
    assert is_p is True
    assert "uneven illumination" in reason

    # Dim gray background (< 225 median)
    dim = _make_staves_image(1200, 1600, bg_val=200, ink_val=20)
    is_p, reason = is_camera_photo(dim)
    assert is_p is True
    assert "dim paper" in reason

    # Warm colored paper cast (max_ch - min_ch >= 12)
    warm_arr = np.zeros((800, 600, 3), dtype=np.uint8)
    warm_arr[:, :] = (245, 238, 210)  # spread = 35 >= 12
    warm_arr[200:204, 50:550] = (20, 20, 20)
    warm = Image.fromarray(warm_arr, "RGB")
    is_p, reason = is_camera_photo(warm)
    assert is_p is True
    assert "cast" in reason


@pytest.mark.parametrize("tilt_deg", [3.5, -4.0])
def test_estimate_skew_and_deskew_both_signs(tilt_deg: float):
    upright = _make_staves_image(1800, 2400)
    tilted = upright.rotate(
        tilt_deg, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
    )

    est_angle, gain = estimate_skew_angle(np.asarray(tilted, dtype=np.uint8))
    assert gain > 1.08
    assert abs(est_angle + tilt_deg) < 0.5  # PIL rotate(-tilt_deg) restores upright

    out, notes = preprocess_image_for_omr(tilted, photo_enhance="off", deskew=True)
    assert any("Deskewed" in n for n in notes)
    residual_angle, _ = estimate_skew_angle(np.asarray(out, dtype=np.uint8))
    assert abs(residual_angle) < 0.5


def test_photo_enhance_flattens_shadows_and_preserves_antialiasing():
    # Create clean anti-aliased score with intermediate gray edges
    base = _make_staves_image(1800, 2400, bg_val=255, ink_val=15)
    # Tilt slightly first so there are genuine anti-aliased gray edge pixels
    base = base.rotate(0.4, resample=Image.Resampling.BICUBIC, fillcolor=255)
    arr = np.asarray(base, dtype=np.float32)

    # Apply strong corner-to-corner warm illumination gradient (120 -> 245)
    yy = np.linspace(0.50, 0.98, arr.shape[0], dtype=np.float32)[:, None]
    xx = np.linspace(0.55, 1.00, arr.shape[1], dtype=np.float32)[None, :]
    illum = np.clip(yy * xx * 255.0, 90.0, 250.0)
    shaded = np.clip((arr / 255.0) * illum, 0.0, 255.0).astype(np.uint8)
    rgb_shaded = np.stack(
        [
            shaded,
            np.clip(shaded * 0.96, 0, 255).astype(np.uint8),
            np.clip(shaded * 0.88, 0, 255).astype(np.uint8),
        ],
        axis=2,
    )
    photo = Image.fromarray(rgb_shaded, "RGB")

    out, notes = preprocess_image_for_omr(photo, photo_enhance="auto", deskew=True)
    assert out.mode == "L"
    assert any("Camera photo detected" in n for n in notes)

    out_arr = np.asarray(out, dtype=np.uint8)
    # Shadows flattened: top-left vs bottom-right paper brightness should now both be ~255
    assert np.percentile(out_arr[:300, :300], 85) >= 245
    assert np.percentile(out_arr[-300:, -300:], 85) >= 245
    # Anti-aliased grayscale, NOT 1-bit binarized
    midtones = np.sum((out_arr > 25) & (out_arr < 235))
    assert midtones > 500


def test_resolution_normalization_upscale_and_downscale():
    small = _make_staves_image(800, 1100)
    up, up_notes = preprocess_image_for_omr(small, photo_enhance="off", deskew=False)
    assert min(up.size) == 2200
    assert max(up.size) <= 3500
    assert any("Upscaled" in n for n in up_notes)

    huge = _make_staves_image(4500, 6000)
    down, down_notes = preprocess_image_for_omr(huge, photo_enhance="off", deskew=False)
    assert max(down.size) == 3300
    assert any("Downscaled" in n for n in down_notes)


def test_images_to_pdf_multi_image_and_multiframe_tiff(tmp_path: Path):
    p1 = tmp_path / "sheet1.png"
    p2 = tmp_path / "sheet2.webp"
    _make_staves_image(1700, 2200).save(p1)
    _make_staves_image(1700, 2200).save(p2)

    out_pdf = tmp_path / "multi.pdf"
    notes: list[str] = []
    images_to_pdf([p1, p2], out_pdf, photo_enhance="off", deskew=False, notes=notes)
    assert out_pdf.is_file()
    doc = fitz.open(str(out_pdf))
    try:
        assert doc.page_count == 2
    finally:
        doc.close()

    # Multi-frame TIFF (3 pages)
    tiff_path = tmp_path / "booklet.tiff"
    frames = [_make_staves_image(1700, 2200) for _ in range(3)]
    frames[0].save(tiff_path, save_all=True, append_images=frames[1:])
    tiff_pdf = tmp_path / "from_tiff.pdf"
    images_to_pdf([tiff_path], tiff_pdf, photo_enhance="off", deskew=False)
    doc2 = fitz.open(str(tiff_pdf))
    try:
        assert doc2.page_count == 3
    finally:
        doc2.close()


def test_prepare_pages_and_run_per_page_omr_with_images(monkeypatch, tmp_path: Path):
    img1 = tmp_path / "p1.jpg"
    img2 = tmp_path / "p2.png"
    _make_staves_image(1700, 2200).save(img1)
    _make_staves_image(1700, 2200).save(img2)

    pages = prepare_pages_for_omr(
        [img1, img2],
        tmp_path / "prep_work",
        whiteout_fraction=0.12,
        whiteout_mode="all",
        photo_enhance="auto",
        deskew=True,
    )
    assert len(pages) == 2
    assert all(p.is_file() for p in pages)
    assert (tmp_path / "prep_work" / "images_input.pdf").is_file()

    # run_per_page_omr with sequence of images
    import satb_splitter.omr.pipeline as pl

    monkeypatch.setattr(pl, "find_audiveris", lambda a=None: Path("Audiveris.exe"))

    def fake_run(src, out, audiveris=None, timeout_s=600):
        src = Path(src)
        mxl = Path(out) / f"{src.stem}.mxl"
        mxl.write_bytes(b"fake-mxl")
        return AudiverisRun(input_path=src, success=True, mxl_paths=[mxl])

    monkeypatch.setattr(pl, "run_audiveris_export", fake_run)
    logs: list[str] = []
    res = pl.run_per_page_omr(
        [img1, img2],
        tmp_path / "omr_work",
        progress=logs.append,
        photo_enhance="on",
        deskew=True,
    )
    assert res.pdf_path == img1
    assert res.success_count == 2
    assert len(res.mxl_paths) == 2
    assert any("Photo enhance forced" in ln for ln in logs)


def test_omr_cli_images_and_reject_mixed_pdf_image(monkeypatch, tmp_path: Path, capsys):
    import satb_splitter.omr_cli as omr_cli

    pdf = tmp_path / "score.pdf"
    img1 = tmp_path / "p1.jpg"
    img2 = tmp_path / "p2.jpg"
    pdf.write_bytes(b"%PDF-1.4\n")
    _make_staves_image(1700, 2200).save(img1)
    _make_staves_image(1700, 2200).save(img2)

    # Reject mixing PDF + image
    rc_mix = omr_cli.main([str(pdf), str(img1)])
    assert rc_mix == 1
    assert "cannot mix PDF and image" in capsys.readouterr().err

    # Multi-image invocation passes list, photo_enhance, deskew, and names split output after inputs[0].stem
    seen: dict = {}
    mxl1 = tmp_path / "p1.mxl"
    mxl1.write_bytes(b"mxl")

    def fake_omr(source, work, **kwargs):
        seen["source"] = source
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            audiveris_path=Path("Audiveris.exe"),
            fail_count=0,
            success_count=1,
            review_count=0,
            page_inputs=[tmp_path / "page_01.pdf", tmp_path / "page_02.pdf"],
            page_runs=[AudiverisRun(input_path=tmp_path / "page_01.pdf", success=True, mxl_paths=[mxl1])],
            mxl_paths=[mxl1],
            whiteout_notes=[],
        )

    split_seen: dict = {}

    def fake_split_merge(mxls, split_dir, stem, **kwargs):
        split_seen["stem"] = stem
        return {"SATB": split_dir / f"{stem}_SATB.musicxml"}, ["Merged 1 page"], None

    monkeypatch.setattr(omr_cli, "run_per_page_omr", fake_omr)
    import satb_splitter.export.merge as merge_mod

    monkeypatch.setattr(merge_mod, "split_and_merge_mxl_files", fake_split_merge)

    rc = omr_cli.main(
        [
            str(img1),
            str(img2),
            "-o",
            str(tmp_path / "work"),
            "--photo-enhance",
            "on",
            "--no-deskew",
            "--split",
        ]
    )
    assert rc == 0
    assert seen["source"] == [img1, img2]
    assert seen["kwargs"]["photo_enhance"] == "on"
    assert seen["kwargs"]["deskew"] is False
    assert split_seen["stem"] == "p1"


def test_pipeline_worker_image_branch(monkeypatch, tmp_path: Path):
    import satb_splitter.omr.pipeline as pl
    import satb_splitter.export.merge as merge_mod

    img1 = tmp_path / "sheet_a.jpg"
    img2 = tmp_path / "sheet_b.jpg"
    img1.write_bytes(b"jpg")
    img2.write_bytes(b"jpg")
    mxl = tmp_path / "page_01.mxl"
    mxl.write_bytes(b"mxl")

    captured: dict = {}

    def fake_omr(source, work_dir, **kwargs):
        captured["source"] = source
        captured["photo_enhance"] = kwargs.get("photo_enhance")
        captured["deskew"] = kwargs.get("deskew")
        return SimpleNamespace(
            page_runs=[AudiverisRun(input_path=tmp_path / "page_01.pdf", success=True, mxl_paths=[mxl])],
            mxl_paths=[mxl],
        )

    monkeypatch.setattr(pl, "run_per_page_omr", fake_omr)
    satb_out = tmp_path / "work" / "voices" / "sheet_a_SATB.musicxml"

    def fake_split_merge(mxl_paths, voices_dir, basename, **kwargs):
        voices_dir.mkdir(parents=True, exist_ok=True)
        satb_out.write_text("<score-partwise/>", encoding="utf-8")
        return {"SATB": satb_out, "Soprano": satb_out}, ["ok"], SimpleNamespace(flags=[], divisi_choices=[])

    monkeypatch.setattr(merge_mod, "split_and_merge_mxl_files", fake_split_merge)

    worker = PipelineWorker(
        img1,
        tmp_path / "work",
        is_pdf=False,
        is_image=True,
        image_paths=[img1, img2],
        photo_enhance="on",
        deskew=False,
        whiteout=0.12,
        whiteout_mode="all",
        template="auto",
        unison="duplicate_both",
        audiveris=None,
    )
    res = worker._run()
    assert captured["source"] == [img1, img2]
    assert captured["photo_enhance"] == "on"
    assert captured["deskew"] is False
    assert res["satb"] == str(satb_out)


def test_main_window_image_queue_reorder_settings_and_merge(monkeypatch, tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    settings_file = tmp_path / "test_settings.ini"

    win = MainWindow()
    win.settings = QSettings(str(settings_file), QSettings.Format.IniFormat)
    win._load_settings()

    img1 = tmp_path / "01_page1.jpg"
    img2 = tmp_path / "02_page2.png"
    mxl = tmp_path / "03_score.musicxml"
    _make_staves_image(600, 800).save(img1)
    _make_staves_image(600, 800).save(img2)
    mxl.write_text("<score-partwise/>", encoding="utf-8")

    win.add_paths([img1, img2, mxl])
    assert win.file_list.count() == 3
    assert win.file_list.itemWidget(win.file_list.item(0)) is not None

    # Move row 1 (img2) up to row 0 using btn_move_up
    win.file_list.setCurrentRow(1)
    win.file_list.item(1).setSelected(True)
    win.btn_move_up.click()
    assert Path(win.file_list.item(0).data(Qt.ItemDataRole.UserRole)).name == "02_page2.png"
    assert Path(win.file_list.item(1).data(Qt.ItemDataRole.UserRole)).name == "01_page1.jpg"
    # Item widget remains intact after move
    assert win.file_list.itemWidget(win.file_list.item(0)) is not None
    assert win.file_list.itemWidget(win.file_list.item(1)) is not None

    # Move back down using btn_move_down
    win.btn_move_down.click()
    assert Path(win.file_list.item(0).data(Qt.ItemDataRole.UserRole)).name == "01_page1.jpg"
    assert Path(win.file_list.item(1).data(Qt.ItemDataRole.UserRole)).name == "02_page2.png"
    assert win.file_list.itemWidget(win.file_list.item(1)) is not None

    # Verify settings persistence for photo_enhance_combo & chk_merge_images
    on_idx = win.photo_enhance_combo.findData("on")
    assert on_idx >= 0
    win.photo_enhance_combo.setCurrentIndex(on_idx)
    win.chk_merge_images.setChecked(True)
    win._save_settings()
    win.settings.sync()

    win2 = MainWindow()
    win2.settings = QSettings(str(settings_file), QSettings.Format.IniFormat)
    win2._load_settings()
    assert win2.photo_enhance_combo.currentData() == "on"
    assert win2.chk_merge_images.isChecked() is True
    win2.close()

    # Verify multi-page grouping when chk_merge_images is checked vs unchecked
    started_jobs: list[dict] = []

    def fake_start_worker(path, work, *, is_pdf, divisi_overrides, mxl_paths=None, is_image=False, image_paths=None):
        started_jobs.append(
            {
                "path": path,
                "work": work,
                "is_pdf": is_pdf,
                "is_image": is_image,
                "image_paths": image_paths,
            }
        )

    monkeypatch.setattr(win, "_start_worker", fake_start_worker)
    win.out_edit.setText(str(tmp_path / "out"))

    # Checked: img1 + img2 grouped into 1 multi-page job, mxl remains separate
    win.file_list.clearSelection()
    win.chk_merge_images.setChecked(True)
    win.start_pipeline()
    assert len(started_jobs) == 1
    assert started_jobs[0]["is_image"] is True
    assert [p.name for p in started_jobs[0]["image_paths"]] == ["01_page1.jpg", "02_page2.png"]
    assert started_jobs[0]["work"] == (tmp_path / "out" / "01_page1")
    assert len(win._queue) == 1 and Path(win._queue[0]).name == "03_score.musicxml"

    # Unchecked: each image runs independently
    started_jobs.clear()
    win._thread = None
    win.chk_merge_images.setChecked(False)
    win.start_pipeline()
    assert len(started_jobs) == 1
    assert started_jobs[0]["is_image"] is True
    assert [p.name for p in started_jobs[0]["image_paths"]] == ["01_page1.jpg"]
    assert len(win._queue) == 2

    win.close()
    app.processEvents()
