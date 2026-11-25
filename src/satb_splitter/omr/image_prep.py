"""Sheet music image & smartphone camera photo preprocessor for OMR."""

from __future__ import annotations

from collections import deque
import io
import math
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


def _solve_perspective_coeffs(
    dst_pts: Sequence[tuple[float, float]],
    src_pts: Sequence[tuple[float, float]],
) -> tuple[float, ...]:
    """Solve 8-parameter PIL ``PERSPECTIVE`` coefficients mapping ``dst -> src``."""
    if len(dst_pts) != 4 or len(src_pts) != 4:
        raise ValueError("Expected 4 destination and 4 source points.")
    rows: list[list[float]] = []
    rhs: list[float] = []
    for (x, y), (u, v) in zip(dst_pts, src_pts):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        rhs.append(u)
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        rhs.append(v)
    a_mat = np.asarray(rows, dtype=np.float64)
    b_vec = np.asarray(rhs, dtype=np.float64)
    coeffs = np.linalg.solve(a_mat, b_vec)
    return tuple(float(c) for c in coeffs)


def _find_thick_border_mask(
    gray_arr: np.ndarray,
    *,
    dark_thresh: int = 232,
) -> tuple[np.ndarray, int]:
    """Return ``(edge_connected_dark_mask, cell)`` via block-max pooling + 4-edge BFS."""
    arr = np.asarray(gray_arr, dtype=np.uint8)
    h, w = arr.shape
    cell = max(4, min(h, w) // 360)
    grid_h, grid_w = h // cell, w // cell
    if grid_h < 4 or grid_w < 4:
        return np.zeros((max(grid_h, 1), max(grid_w, 1)), dtype=bool), cell

    block_max = (
        arr[: grid_h * cell, : grid_w * cell]
        .reshape(grid_h, cell, grid_w, cell)
        .max(axis=(1, 3))
    )
    dark = block_max < dark_thresh
    mask = np.zeros((grid_h, grid_w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()

    for r in range(grid_h):
        for c in (0, grid_w - 1):
            if dark[r, c] and not mask[r, c]:
                mask[r, c] = True
                queue.append((r, c))
    for c in range(grid_w):
        for r in (0, grid_h - 1):
            if dark[r, c] and not mask[r, c]:
                mask[r, c] = True
                queue.append((r, c))

    while queue:
        r, c = queue.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < grid_h and 0 <= nc < grid_w and dark[nr, nc] and not mask[nr, nc]:
                mask[nr, nc] = True
                queue.append((nr, nc))

    return mask, cell


def _fit_trimmed_line(
    pts: Sequence[tuple[float, float]],
    *,
    span_total: int,
    other_dim: int,
    min_cov: float,
    min_span: float,
    min_inliers: float,
) -> tuple[float, float] | None:
    """Two-pass trimmed OLS fit ``dep = a * indep + b`` on grid coordinates."""
    if len(pts) < max(6, int(math.ceil(min_cov * span_total))):
        return None
    indep = np.asarray([p[0] for p in pts], dtype=np.float64)
    dep = np.asarray([p[1] for p in pts], dtype=np.float64)
    if float(indep.max() - indep.min()) / float(max(span_total, 1)) < min_span:
        return None

    slope, intercept = np.polyfit(indep, dep, 1)
    tol = max(2.0, 0.01 * float(other_dim))
    inliers = np.abs(dep - (slope * indep + intercept)) <= tol
    if int(inliers.sum()) < max(6, int(math.ceil(min_inliers * span_total))):
        return None

    indep_in = indep[inliers]
    dep_in = dep[inliers]
    slope, intercept = np.polyfit(indep_in, dep_in, 1)
    mad = float(np.median(np.abs(dep_in - (slope * indep_in + intercept))))
    if mad >= 0.012 * float(other_dim):
        return None
    return float(slope), float(intercept)


def _intersect_hv_lines(
    a_lr: float,
    b_lr: float,
    a_tb: float,
    b_tb: float,
) -> tuple[float, float]:
    """Intersect ``x = a_lr * y + b_lr`` and ``y = a_tb * x + b_tb``."""
    denom = 1.0 - a_lr * a_tb
    if abs(denom) < 1e-6:
        y = b_tb
        return a_lr * y + b_lr, y
    x = (a_lr * b_tb + b_lr) / denom
    y = a_tb * x + b_tb
    return x, y


def dewarp_paper_perspective(gray: Image.Image) -> tuple[Image.Image, str | None]:
    """Rectify trapezoidal camera perspective when dark outer margins bound the sheet."""
    arr = np.asarray(gray, dtype=np.uint8)
    h, w = arr.shape
    if h < 64 or w < 64:
        return gray, None

    mask, cell = _find_thick_border_mask(arr)
    if not np.any(mask):
        return gray, None

    grid_h, grid_w = mask.shape
    left_lim = int(0.22 * grid_w)
    right_lim = int(0.78 * grid_w)
    row_mask_frac = mask.mean(axis=1)

    l_pts: list[tuple[float, float]] = []
    r_pts: list[tuple[float, float]] = []
    for r in range(grid_h):
        if row_mask_frac[r] > 0.40:
            continue
        lm = np.where(mask[r, :left_lim])[0]
        if len(lm):
            x_l = int(lm.max()) + 1
            if 0 < x_l < left_lim and not mask[r, x_l]:
                l_pts.append((float(r), float(x_l)))
        rm = np.where(mask[r, right_lim:])[0]
        if len(rm):
            first_dark = int(rm.min()) + right_lim
            if right_lim < first_dark < grid_w and not mask[r, first_dark - 1]:
                r_pts.append((float(r), float(first_dark)))

    fit_l = _fit_trimmed_line(
        l_pts,
        span_total=grid_h,
        other_dim=grid_w,
        min_cov=0.25,
        min_span=0.40,
        min_inliers=0.20,
    )
    fit_r = _fit_trimmed_line(
        r_pts,
        span_total=grid_h,
        other_dim=grid_w,
        min_cov=0.25,
        min_span=0.40,
        min_inliers=0.20,
    )
    if fit_l is None or fit_r is None:
        return gray, None

    a_l, b_l = fit_l
    a_r, b_r = fit_r

    # Optional top/bottom boundary fits when clearly bordered
    top_lim = int(0.15 * grid_h)
    bot_lim = int(0.85 * grid_h)
    col_mask_frac = mask.mean(axis=0)
    t_pts: list[tuple[float, float]] = []
    b_pts: list[tuple[float, float]] = []
    for c in range(grid_w):
        if col_mask_frac[c] > 0.40:
            continue
        tm = np.where(mask[:top_lim, c])[0]
        if len(tm):
            y_t = int(tm.max()) + 1
            if 0 < y_t < top_lim and not mask[y_t, c]:
                t_pts.append((float(c), float(y_t)))
        bm_c = np.where(mask[bot_lim:, c])[0]
        if len(bm_c):
            first_dark_y = int(bm_c.min()) + bot_lim
            if bot_lim < first_dark_y < grid_h and not mask[first_dark_y - 1, c]:
                b_pts.append((float(c), float(first_dark_y)))

    fit_t = _fit_trimmed_line(
        t_pts,
        span_total=grid_w,
        other_dim=grid_h,
        min_cov=0.30,
        min_span=0.45,
        min_inliers=0.25,
    )
    fit_b = _fit_trimmed_line(
        b_pts,
        span_total=grid_w,
        other_dim=grid_h,
        min_cov=0.30,
        min_span=0.45,
        min_inliers=0.25,
    )

    a_t, b_t_full = (fit_t[0], fit_t[1] * cell) if fit_t is not None else (0.0, 0.0)
    a_b, b_b_full = (fit_b[0], fit_b[1] * cell) if fit_b is not None else (0.0, float(h - 1))

    b_l_full = b_l * cell
    b_r_full = b_r * cell
    tl_x, tl_y = _intersect_hv_lines(a_l, b_l_full, a_t, b_t_full)
    tr_x, tr_y = _intersect_hv_lines(a_r, b_r_full, a_t, b_t_full)
    br_x, br_y = _intersect_hv_lines(a_r, b_r_full, a_b, b_b_full)
    bl_x, bl_y = _intersect_hv_lines(a_l, b_l_full, a_b, b_b_full)

    # Inset slightly (~3 px horizontally) and clamp to image bounds
    inset = 3.0
    def _clamp_pt(px: float, py: float) -> tuple[float, float]:
        return (
            float(min(max(px, 0.0), float(w - 1))),
            float(min(max(py, 0.0), float(h - 1))),
        )

    tl = _clamp_pt(tl_x + inset, tl_y)
    tr = _clamp_pt(tr_x - inset, tr_y)
    br = _clamp_pt(br_x - inset, br_y)
    bl = _clamp_pt(bl_x + inset, bl_y)

    w_top = tr[0] - tl[0]
    w_bot = br[0] - bl[0]
    if w_top <= 16.0 or w_bot <= 16.0:
        return gray, None

    taper = abs(w_bot - w_top) / max(w_top, w_bot, 1.0)
    opposing_slopes = a_l * a_r < 0.0 and abs(a_r - a_l) >= 0.025
    if taper < 0.025 and not opposing_slopes:
        return gray, None

    if min(w_top, w_bot) < 0.55 * float(w):
        return gray, None

    quad = (tl, tr, br, bl)
    xs_q = [p[0] for p in quad]
    ys_q = [p[1] for p in quad]
    area = 0.5 * abs(
        sum(xs_q[i] * ys_q[(i + 1) % 4] - xs_q[(i + 1) % 4] * ys_q[i] for i in range(4))
    )
    if area < 0.55 * float(w * h):
        return gray, None

    quad_height = max(
        math.hypot(bl[0] - tl[0], bl[1] - tl[1]),
        math.hypot(br[0] - tr[0], br[1] - tr[1]),
    )
    out_w = max(16, int(round(max(w_top, w_bot))))
    out_h = max(16, int(round(quad_height)))

    dst_pts = [
        (0.0, 0.0),
        (float(out_w - 1), 0.0),
        (float(out_w - 1), float(out_h - 1)),
        (0.0, float(out_h - 1)),
    ]
    try:
        coeffs = _solve_perspective_coeffs(dst_pts, quad)
    except np.linalg.LinAlgError:
        return gray, None

    warped = gray.transform(
        (out_w, out_h),
        Image.Transform.PERSPECTIVE,
        coeffs,
        resample=Image.Resampling.BICUBIC,
        fillcolor=255,
    )
    padded = ImageOps.expand(warped, border=24, fill=255)
    return padded, f"Perspective-rectified paper quad (taper {taper * 100:.1f}%)"


def whiten_dark_borders(gray: Image.Image) -> tuple[Image.Image, str | None]:
    """Whiten solid dark table/shadow margins connected to the image perimeter."""
    arr = np.asarray(gray, dtype=np.uint8)
    h, w = arr.shape
    if h < 16 or w < 16:
        return gray, None

    mask, cell = _find_thick_border_mask(arr)
    area_frac = float(mask.mean())
    if area_frac < 0.002 or area_frac > 0.25:
        return gray, None

    # Dilate by 2 cells using 5x5 slice shifts (zero external dependencies)
    grid_h, grid_w = mask.shape
    padded_m = np.pad(mask, 2, mode="constant", constant_values=False)
    dilated = np.zeros_like(mask, dtype=bool)
    for dr in range(5):
        for dc in range(5):
            dilated |= padded_m[dr : dr + grid_h, dc : dc + grid_w]

    up = np.repeat(np.repeat(dilated, cell, axis=0), cell, axis=1)
    pad_h = max(0, h - up.shape[0])
    pad_w = max(0, w - up.shape[1])
    if pad_h > 0 or pad_w > 0:
        up = np.pad(up, ((0, pad_h), (0, pad_w)), mode="edge")
    up = up[:h, :w]

    my = max(1, int(round(0.14 * h)))
    mx = max(1, int(round(0.14 * w)))
    in_margin = np.zeros((h, w), dtype=bool)
    in_margin[:my, :] = True
    in_margin[-my:, :] = True
    in_margin[:, :mx] = True
    in_margin[:, -mx:] = True

    full_mask = up & in_margin
    if not np.any(full_mask & (arr < 255)):
        return gray, None

    cleaned = arr.copy()
    cleaned[full_mask] = 255
    return (
        Image.fromarray(cleaned, mode="L"),
        f"Whitened dark outer borders ({area_frac * 100:.1f}% of area)",
    )


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

    # Step B2: perspective paper rectification & safe outer dark border removal
    border_cleaned = False
    if deskew:
        gray, persp_note = dewarp_paper_perspective(gray)
        if persp_note:
            notes.append(persp_note)
            border_cleaned = True
    if mode != "off" or deskew:
        gray, border_note = whiten_dark_borders(gray)
        if border_note:
            notes.append(border_note)
            border_cleaned = True

    # Step C: uneven shadow flattening + smooth anti-aliased contrast stretch
    do_enhance = False
    if mode == "on":
        do_enhance = True
        notes.append("Photo enhance forced: shadow flatten + anti-aliased contrast stretch")
    elif mode == "auto":
        is_photo, reason = is_camera_photo(gray if border_cleaned else rgb_or_l)
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
