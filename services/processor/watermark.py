"""Watermark removal (best-effort) cho OKXSquare.

Ba chế độ cấu hình được (image_processing.watermark):
  * off     — không xử lý.
  * crop    — cắt dải trên/dưới theo tỉ lệ (watermark thường nằm ở biên).
  * inpaint — nội suy xoá vùng watermark:
      - template: nếu có ảnh mẫu logo, dò đa tỉ lệ (matchTemplate) rồi inpaint.
      - heuristic: tách nét sáng mảnh (top-hat) trong ROI góc/biên — watermarks
        dạng chữ/logo sáng. Đây là heuristic, KHÔNG đảm bảo mọi watermark;
        chỉnh ROI/radius hoặc dùng template để đạt kết quả tốt hơn.

Output ghi đè file (atomic), giữ nguyên đuôi .jpg. Trả {path, changed, method}.
"""
import os
from typing import Dict, List, Optional

import cv2
import numpy as np

from infra.logging_config import get_logger

logger = get_logger(__name__)

_DEFAULT = {
    "enabled": True,
    "mode": "inpaint",            # off | crop | inpaint
    "crop_top_ratio": 0.0,
    "crop_bottom_ratio": 0.0,
    "inpaint_roi": "corners",     # corners | edges | full
    "inpaint_radius": 3,
    "template": "",               # đường dẫn ảnh mẫu watermark (tuỳ chọn)
    "template_threshold": 0.80,
    "corner_width_ratio": 0.24,
    "corner_height_ratio": 0.16,
}


def _roi_boxes(h: int, w: int, cfg: dict) -> List[tuple]:
    roi = cfg.get("inpaint_roi", "corners")
    if roi == "full":
        return [(0, 0, w, h)]
    if roi == "edges":
        bh = max(8, int(h * cfg["corner_height_ratio"]))
        return [(0, 0, w, bh), (0, h - bh, w, h)]
    # corners (mặc định)
    bw = max(8, int(w * cfg["corner_width_ratio"]))
    bh = max(8, int(h * cfg["corner_height_ratio"]))
    return [
        (0, 0, bw, bh), (w - bw, 0, w, bh),
        (0, h - bh, bw, h), (w - bw, h - bh, w, h),
    ]


def _crop(img: np.ndarray, cfg: dict) -> np.ndarray:
    h, w = img.shape[:2]
    top = int(h * float(cfg.get("crop_top_ratio", 0.0)))
    bottom = int(h * float(cfg.get("crop_bottom_ratio", 0.0)))
    top = min(top, h - 8)
    bottom = min(bottom, h - top - 8)
    if top <= 0 and bottom <= 0:
        return img
    return img[top:h - bottom if bottom else h, :, :]


def _template_mask(img: np.ndarray, cfg: dict) -> Optional[np.ndarray]:
    tpl_path = cfg.get("template") or ""
    if not tpl_path or not os.path.exists(tpl_path):
        return None
    tpl = cv2.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    th = float(cfg.get("template_threshold", 0.80))
    gh, gw = gray.shape[:2]
    for scale in (1.0, 0.75, 0.5, 1.25, 1.5):
        tw, tw_h = int(tpl.shape[1] * scale), int(tpl.shape[0] * scale)
        if tw < 8 or tw_h < 8 or tw > gw or tw_h > gh:
            continue
        resized = cv2.resize(tpl, (tw, tw_h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= th)
        for y, x in zip(ys, xs):
            mask[y:y + tw_h, x:x + tw] = 255
    return mask if mask.any() else None


def _heuristic_mask(img: np.ndarray, cfg: dict) -> np.ndarray:
    """Tách nét sáng mảnh (top-hat) trong ROI — ứng viên watermark chữ/logo sáng."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = np.zeros((h, w), dtype=np.uint8)
    k = max(9, int(min(h, w) * 0.02) | 1)  # kernel lẻ
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    for (x0, y0, x1, y1) in _roi_boxes(h, w, cfg):
        roi = gray[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        tophat = cv2.morphologyEx(roi, cv2.MORPH_TOPHAT, kernel)
        _, bright = cv2.threshold(tophat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Chỉ giữ nét mảnh (logo/chữ), bỏ mảng đặc lớn
        bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
        mask[y0:y1, x0:x1] = cv2.bitwise_or(mask[y0:y1, x0:x1], bright)
    return mask


def _inpaint(img: np.ndarray, cfg: dict):
    mask = _template_mask(img, cfg)
    method = "inpaint:template"
    if mask is None:
        mask = _heuristic_mask(img, cfg)
        method = "inpaint:heuristic"
    if not mask.any():
        return img, "inpaint:none"
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
    radius = int(cfg.get("inpaint_radius", 3))
    out = cv2.inpaint(img, mask, radius, cv2.INPAINT_TELEA)
    return out, method


def remove_watermark(filepath: str, config: Optional[dict] = None) -> Dict[str, object]:
    cfg = {**_DEFAULT, **(config or {})}
    result = {"path": filepath, "changed": False, "method": "off"}
    if not cfg.get("enabled", True) or cfg.get("mode", "off") == "off":
        return result
    if not filepath or not os.path.exists(filepath):
        return result
    img = cv2.imread(filepath)
    if img is None:
        return result

    mode = cfg.get("mode", "inpaint")
    if mode == "crop":
        out = _crop(img, cfg)
        method = "crop"
    elif mode == "inpaint":
        out, method = _inpaint(img, cfg)
    else:
        return result

    if out.shape == img.shape and np.array_equal(out, img):
        result["method"] = method
        return result

    stem, ext = os.path.splitext(filepath)
    ext_lower = ext.lower()
    if ext_lower not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
        ext_lower = ".jpg"
    tmp = f"{stem}.wm_tmp{ext}"
    try:
        if ext_lower in (".jpg", ".jpeg"):
            params = [int(cv2.IMWRITE_JPEG_QUALITY), 92]
        elif ext_lower == ".webp":
            params = [int(cv2.IMWRITE_WEBP_QUALITY), 92]
        else:
            params = []
        ok = cv2.imwrite(tmp, out, params) if params else cv2.imwrite(tmp, out)
        if not ok or not os.path.exists(tmp):
            raise RuntimeError(f"cv2.imwrite returned False for {tmp}")
        os.replace(tmp, filepath)
        result["changed"] = True
        result["method"] = method
    except Exception as e:
        logger.warning("[WATERMARK] failed to write %s: %s", filepath, e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return result


def process_images(image_paths: List[str], config: Optional[dict] = None) -> List[str]:
    """Áp dụng remove_watermark cho từng ảnh; trả về danh sách đường dẫn (không đổi)."""
    for p in image_paths:
        remove_watermark(p, config)
    return image_paths