"""Sheet music image & smartphone camera photo preprocessor for OMR."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageOps

IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
)

_VALID_PHOTO_MODES = frozenset({"auto", "on", "off"})


def is_image_path(path: Path | str) -> bool:
    """Return True when ``path`` has a supported sheet music image extension."""
    return Path(path).suffix.lower() in IMAGE_EXTS


def _box_blur_2d(arr: np.ndarray, radius: int) -> np.ndarray:
    """Fast separable 2-pass box filter using edge-padded integral sums."""
    a = np.asarray(arr, dtype=np.float32)
    if radius <= 0 or a.size == 0:
        return a.copy()

    k = 2 * radius + 1
    h, w = a.shape

    # Pass 1: horizontal
    p_h = np.pad(a, ((0, 0), (radius, radius)), mode="edge")
    cs_h = np.zeros((h, w + 2 * radius + 1), dtype=np.float64)
    np.cumsum(p_h, axis=1, out=cs_h[:, 1:])
    horiz = (cs_h[:, k:] - cs_h[:, :w]) / float(k)

    # Pass 2: vertical
    p_v = np.pad(horiz, ((radius, radius), (0, 0)), mode="edge")
    cs_v = np.zeros((h + 2 * radius + 1, w), dtype=np.float64)
    np.cumsum(p_v, axis=0, out=cs_v[1:, :])
    out = (cs_v[k:, :] - cs_v[:h, :]) / float(k)
    return out.astype(np.float32)


def _flatten_alpha_to_white(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Composite any alpha/transparency onto solid white.

    Returns ``(rgb_or_gray_for_check, gray_L)``.
    """
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")

    if img.mode in ("RGBA", "LA") or "A" in img.getbands():
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas, canvas.convert("L")

    if img.mode in ("L", "1"):
        gray = img.convert("L")
        return gray, gray

    rgb = img.convert("RGB")
    return rgb, rgb.convert("L")


def is_camera_photo(rgb_or_gray: Image.Image) -> tuple[bool, str]:
    """Heuristically classify whether an image looks like a camera photo vs clean scan.

    Computes 4x4 tile 85th-percentile background brightness on grayscale and
    flags uneven illumination, dim/yellowed paper, or RGB color casts.
    """
    rgb_or_l, gray_img = _flatten_alpha_to_white(rgb_or_gray)
    gray = np.asarray(gray_img, dtype=np.float32)
    h, w = gray.shape
    if h < 4 or w < 4:
        return False, "clean scan"

    # Subsample huge images to keep percentile estimation sub-10ms
    step_y = max(1, h // 1000)
    step_x = max(1, w // 1000)
    g_sub = gray[::step_y, ::step_x]
    sh, sw = g_sub.shape

    tile_vals: list[float] = []
    for r in range(4):
        r0 = (r * sh) // 4
        r1 = ((r + 1) * sh) // 4
        if r1 <= r0:
            continue
        for c in range(4):
            c0 = (c * sw) // 4
            c1 = ((c + 1) * sw) // 4
            if c1 <= c0:
                continue
            tile_vals.append(float(np.percentile(g_sub[r0:r1, c0:c1], 85)))

    if not tile_vals:
        return False, "clean scan"

    tile_bg = np.asarray(tile_vals, dtype=np.float32)
    spread = float(tile_bg.max() - tile_bg.min())
    med_bg = float(np.median(tile_bg))

    reasons: list[str] = []
    if spread >= 18.0:
        reasons.append(f"uneven illumination (4x4 spread={spread:.1f})")
    if med_bg < 225.0:
        reasons.append(f"dim paper background (median={med_bg:.1f})")

    if rgb_or_l.mode == "RGB":
        rgb_arr = np.asarray(rgb_or_l, dtype=np.float32)[::step_y, ::step_x]
        lum = rgb_arr.mean(axis=2)
        bright_mask = lum >= max(100.0, float(np.percentile(lum, 50)))
        bright_px = rgb_arr[bright_mask] if np.any(bright_mask) else rgb_arr.reshape(-1, 3)
        chroma = bright_px.max(axis=1) - bright_px.min(axis=1)
        med_chroma = float(np.median(chroma))
        if med_chroma >= 12.0:
            reasons.append(f"warm/colored cast (channel spread={med_chroma:.1f})")

    if reasons:
        return True, "; ".join(reasons)
    return False, "clean scan"


def estimate_skew_angle(gray: np.ndarray, *, max_angle: float = 8.0) -> tuple[float, float]:
    """Estimate PIL counter-clockwise rotation angle (degrees) that makes staves horizontal.

    Returns ``(best_angle_deg, variance_gain)`` where ``best_angle_deg`` can be
    passed directly to ``PIL.Image.Image.rotate(best_angle_deg, ...)``.
    """
    arr = np.asarray(gray, dtype=np.uint8)
    h, w = arr.shape
    if h < 16 or w < 16:
        return 0.0, 1.0

    # Downsample to width ~600 px for fast projection sweeps
    target_w = min(600, w)
    if w != target_w:
        scale = target_w / float(w)
        target_h = max(16, int(round(h * scale)))
        small_img = Image.fromarray(arr, mode="L").resize(
            (target_w, target_h), resample=Image.Resampling.BILINEAR
        )
        small = np.asarray(small_img, dtype=np.float32)
    else:
        small = arr.astype(np.float32)

    ink_p1 = float(np.percentile(small, 1))
    paper_p85 = float(np.percentile(small, 85))
    if paper_p85 - ink_p1 >= 20.0:
        threshold = ink_p1 + 0.55 * (paper_p85 - ink_p1)
    else:
        threshold = 180.0

    mask = np.where(small < threshold, 255, 0).astype(np.uint8)
    if not np.any(mask):
        return 0.0, 1.0

    mask_img = Image.fromarray(mask, mode="L")

    def _row_var(ang: float) -> float:
        if abs(ang) < 1e-6:
            rot = mask_img
        else:
            rot = mask_img.rotate(
                ang, resample=Image.Resampling.BILINEAR, fillcolor=0
            )
        m = np.asarray(rot, dtype=np.float32)
        row_means = m.mean(axis=1)
        return float(np.var(row_means))

    zero_var = _row_var(0.0)
    if zero_var <= 1e-9:
        return 0.0, 1.0

    # Coarse sweep: -max_angle .. +max_angle step 0.5 deg
    n_half = int(round(max_angle / 0.5))
    coarse_angles = [round((k - n_half) * 0.5, 4) for k in range(2 * n_half + 1)]
    best_coarse = 0.0
    best_coarse_var = zero_var
    for ang in coarse_angles:
        v = zero_var if abs(ang) < 1e-6 else _row_var(ang)
        if v > best_coarse_var:
            best_coarse_var = v
            best_coarse = ang

    # Fine sweep: best_coarse - 0.6 .. best_coarse + 0.6 step 0.1 deg
    best_angle = best_coarse
    best_var = best_coarse_var
    for step in range(-6, 7):
        ang = round(best_coarse + step * 0.1, 2)
        if abs(ang) > max_angle + 1e-6:
            continue
        v = zero_var if abs(ang) < 1e-6 else _row_var(ang)
        if v > best_var:
            best_var = v
            best_angle = ang

    return float(best_angle), float(best_var / max(zero_var, 1e-9))


def preprocess_image_for_omr(
    img: Image.Image,
    *,
    photo_enhance: str = "auto",
    deskew: bool = True,
) -> tuple[Image.Image, list[str]]:
    """Preprocess a sheet music image or camera photo into OMR-ready grayscale ``'L'``."""
    mode = str(photo_enhance).lower().strip()
    if mode not in _VALID_PHOTO_MODES:
        raise ValueError(
            f"Invalid photo_enhance={photo_enhance!r}; expected 'auto', 'on', or 'off'."
        )

    notes: list[str] = []

    # Step A: respect smartphone EXIF orientation
    orig_size = img.size
    exif_ori = None
    try:
        exif_ori = img.getexif().get(274)
    except Exception:  # noqa: BLE001
        exif_ori = None
    img = ImageOps.exif_transpose(img)
    if exif_ori not in (None, 1):
        notes.append(f"Applied EXIF orientation ({exif_ori}; {orig_size[0]}x{orig_size[1]} -> {img.size[0]}x{img.size[1]})")

    # Step B: composite alpha onto white and convert to grayscale
    rgb_or_l, gray = _flatten_alpha_to_white(img)

    # Step C: uneven shadow flattening + smooth anti-aliased contrast stretch
    do_enhance = False
    if mode == "on":
        do_enhance = True
        notes.append("Photo enhance forced: shadow flatten + anti-aliased contrast stretch")
    elif mode == "auto":
        is_photo, reason = is_camera_photo(rgb_or_l)
        if is_photo:
            do_enhance = True
            notes.append(f"Camera photo detected ({reason}): shadow flatten + contrast stretch")

    if do_enhance:
        gray_f = np.asarray(gray, dtype=np.float32)
        w, h = gray.size
        radius = max(25, min(w, h) // 28)
        bg = _box_blur_2d(gray_f, radius=radius)
        norm = np.clip((gray_f / np.maximum(bg, 1.0)) * 248.0, 0.0, 255.0)
        p2 = float(np.percentile(norm, 2))
        low = min(120.0, p2)
        high = 232.0
        if low >= high - 10.0:
            low = 120.0
        stretched = 15.0 + (norm - low) * ((255.0 - 15.0) / (high - low))
        stretched = np.clip(stretched, 0.0, 255.0)
        gray = Image.fromarray(np.rint(stretched).astype(np.uint8), mode="L")

    # Step D: deskew + trim outer pure-white margins keeping ~24 px border
    if deskew:
        angle, gain = estimate_skew_angle(np.asarray(gray, dtype=np.uint8))
        if 0.25 <= abs(angle) <= 8.0 and gain >= 1.08:
            rotated = gray.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=255,
            )
            arr = np.asarray(rotated, dtype=np.uint8)
            nonwhite = arr < 250
            if np.any(nonwhite):
                ys, xs = np.where(nonwhite)
                y0 = max(0, int(ys.min()) - 24)
                y1 = min(arr.shape[0], int(ys.max()) + 1 + 24)
                x0 = max(0, int(xs.min()) - 24)
                x1 = min(arr.shape[1], int(xs.max()) + 1 + 24)
                gray = rotated.crop((x0, y0, x1, y1))
            else:
                gray = rotated
            notes.append(f"Deskewed {angle:+.2f}° (gain {gain:.2f}x)")

    # Step E: resolution normalization for Audiveris ~300 DPI
    w, h = gray.size
    min_dim = min(w, h)
    max_dim = max(w, h)
    if min_dim < 1650:
        scale = 2200.0 / float(min_dim)
        if max_dim * scale > 3500.0:
            scale = 3500.0 / float(max_dim)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        gray = gray.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
        notes.append(f"Upscaled {w}x{h} -> {new_w}x{new_h} for OMR")
    elif max_dim > 3600:
        scale = 3300.0 / float(max_dim)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        gray = gray.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
        notes.append(f"Downscaled {w}x{h} -> {new_w}x{new_h} for OMR")

    if gray.mode != "L":
        gray = gray.convert("L")
    return gray, notes


def images_to_pdf(
    image_paths: Sequence[Path | str],
    out_pdf: Path | str,
    *,
    photo_enhance: str = "auto",
    deskew: bool = True,
    dpi: int = 300,
    notes: list[str] | None = None,
) -> Path:
    """Preprocess one or more sheet music images (including multi-frame TIFFs) into a PDF."""
    import fitz  # PyMuPDF

    if not image_paths:
        raise ValueError("images_to_pdf requires at least one image path.")

    dst = Path(out_pdf)
    dst.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    try:
        for raw_path in image_paths:
            p = Path(raw_path)
            with Image.open(p) as im:
                n_frames = int(getattr(im, "n_frames", 1) or 1)
                for frame_idx in range(n_frames):
                    if n_frames > 1:
                        im.seek(frame_idx)
                    frame = im.copy()
                    processed, img_notes = preprocess_image_for_omr(
                        frame,
                        photo_enhance=photo_enhance,
                        deskew=deskew,
                    )
                    if notes is not None:
                        tag = f"{p.name}#frame{frame_idx + 1}" if n_frames > 1 else p.name
                        for ln in img_notes:
                            notes.append(f"{tag}: {ln}")

                    buf = io.BytesIO()
                    processed.save(buf, format="PNG")
                    pw = processed.width * 72.0 / float(dpi)
                    ph = processed.height * 72.0 / float(dpi)
                    page = doc.new_page(width=pw, height=ph)
                    page.insert_image(page.rect, stream=buf.getvalue())

        doc.save(str(dst))
    finally:
        doc.close()

    return dst
