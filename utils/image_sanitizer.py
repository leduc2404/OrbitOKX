"""
Image Sanitizer & Perceptual Hash Evasion
=========================================
Module làm sạch ảnh trước khi lưu/đăng lên OKX Orbit:
  1. Xoá 100% metadata (EXIF, XMP, IPTC, GPS, ICC Profile, JFIF comment, PNG tEXt/iTXt/eXIf).
  2. Biến đổi chống trùng khớp hình ảnh cơ bản (MD5/SHA256 + aHash/dHash/pHash/color-histogram)
     bằng tổ hợp:
       - Asymmetric micro-crop (1.4%–2.8%) + dịch tâm lưới 8x8/32x32
       - Xoay vi mô dưới ngưỡng mắt thường (±0.35°–0.65°) kèm crop biên sạch
       - Điều biến ánh sáng tần số thấp 2D (DCT low-frequency illumination ramp ±4.5%)
         tác động trực tiếp vào miền tần số thấp của aHash/dHash/pHash nhưng giữ nguyên
         độ sắc nét của chữ/nến/chart (miền tần số cao không bị làm mờ)
       - Tinh chỉnh cân bằng màu vi mô theo kênh RGB + dither nhẹ (σ ≈ 0.8)
       - Mã hoá lại thành baseline JPEG sạch (quality 89–93)
  3. Idempotent: lưu chỉ mục `.sanitized.json` ngoài file ảnh để không biến đổi lặp lại
     một ảnh đã làm sạch và vẫn giữ byte stream JPEG hoàn toàn chuẩn.
"""

import hashlib
import json
import math
import os
import random
import threading
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

_INDEX_LOCK = threading.Lock()
_INDEX_FILENAME = ".sanitized.json"
_MAX_DIMENSION = 1920
_MIN_VALID_BYTES = 128


def _index_path(image_dir: str) -> str:
    return os.path.join(image_dir, _INDEX_FILENAME)


def _load_index(image_dir: str) -> Dict[str, dict]:
    path = _index_path(image_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(image_dir: str, index: Dict[str, dict]) -> None:
    path = _index_path(image_dir)
    tmp_path = path + ".tmp"
    try:
        os.makedirs(image_dir, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _to_clean_rgb(img: Image.Image) -> Image.Image:
    """Chuẩn hoá ảnh về RGB thuần pixel, xử lý xoay EXIF trước khi bỏ metadata."""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba).convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _build_targeted_lowfreq_field(
    img: Image.Image,
    rng: random.Random,
    target_bits: int = 10,
    max_delta: float = 11.5,
) -> np.ndarray:
    """
    Tạo trường điều biến độ sáng tần số thấp mượt (bicubic từ lưới 9x8) nhắm vào
    các cặp ô kề nhau có độ chênh thấp nhất (dHash) và các mode DCT gần trung vị (pHash).
    Vì chỉ chứa tần số không gian rất thấp và biên độ giới hạn ±max_delta (~±4.5%),
    mắt người hoàn toàn không nhận ra (hiệu ứng bất biến độ sáng võng mạc),
    nhưng làm đổi mạnh dHash / aHash / pHash.
    """
    w, h = img.size
    gray9x8 = np.asarray(
        img.convert("L").resize((9, 8), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    diffs = gray9x8[:, 1:] - gray9x8[:, :-1]  # (8, 8)
    abs_diffs = np.abs(diffs)

    # Chọn các vị trí có |diff| nhỏ nhất (dễ đảo dấu nhất mà không cần biên độ lớn)
    flat_order = np.argsort(abs_diffs.flatten())
    grid_delta = np.zeros((8, 9), dtype=np.float32)

    flipped = 0
    for idx in flat_order:
        if flipped >= target_bits:
            break
        r, c = divmod(int(idx), 8)
        cur_diff = float(diffs[r, c])
        # Cần đẩy theo hướng ngược lại của cur_diff
        needed = (abs(cur_diff) * 0.65) + rng.uniform(3.5, 6.0)
        step = min(max_delta, needed)
        if cur_diff >= 0:
            # Đẩy ô phải xuống, ô trái lên
            grid_delta[r, c] += step
            grid_delta[r, c + 1] -= step
        else:
            grid_delta[r, c] -= step
            grid_delta[r, c + 1] += step
        flipped += 1

    # Cộng thêm 3 sóng DCT tần số thấp ngẫu nhiên để dịch chuyển pHash & aHash
    gx = np.linspace(0.0, 1.0, 9, dtype=np.float32)[None, :]
    gy = np.linspace(0.0, 1.0, 8, dtype=np.float32)[:, None]
    modes = [(1, 0), (0, 1), (1, 1), (2, 1), (1, 2), (2, 2), (3, 1), (1, 3), (3, 2)]
    for kx, ky in rng.sample(modes, 3):
        phase = rng.uniform(0.0, math.pi)
        sign = rng.choice([-1.0, 1.0])
        amp = rng.uniform(3.5, 5.5) * sign
        grid_delta += amp * np.cos(math.pi * (kx * gx + ky * gy) + phase)

    grid_delta = np.clip(grid_delta, -max_delta, max_delta)

    # Nội suy mượt lên kích thước thật bằng BICUBIC (chỉ giữ tần số cực thấp)
    field_img = Image.fromarray(grid_delta, mode="F").resize(
        (w, h), resample=Image.Resampling.BICUBIC
    )
    return np.asarray(field_img, dtype=np.float32)


def _apply_perceptual_evasion(img: Image.Image, seed_key: str) -> Image.Image:
    """
    Biến đổi nhẹ không ảnh hưởng thị giác nhưng làm đổi MD5/SHA và dịch chuyển
    các bit của aHash / dHash / pHash.
    """
    rng = random.Random(f"orbit-img-v1|{seed_key}")
    w, h = img.size

    if w < 32 or h < 32:
        arr = np.asarray(img, dtype=np.uint8).copy()
        arr[0, 0, 0] = (int(arr[0, 0, 0]) ^ 1) & 0xFF
        return Image.fromarray(arr, mode="RGB")

    # 1. Xoay vi mô (±0.40° .. ±0.70°) và crop phần biên tam giác để không lộ viền
    angle_deg = rng.choice([-1.0, 1.0]) * rng.uniform(0.40, 0.70)
    rotated = img.rotate(angle_deg, resample=Image.Resampling.BICUBIC, expand=False)
    rad = math.radians(abs(angle_deg))
    pad_x = int(math.ceil(h * math.sin(rad) * 0.55)) + 1
    pad_y = int(math.ceil(w * math.sin(rad) * 0.55)) + 1

    # 2. Asymmetric micro-crop: một phía crop 2.6%–3.8%, phía đối diện 0.5%–1.0%
    #    để dịch hẳn tâm lưới 8x8 / 32x32 nhưng tổng diện tích mất < 4%.
    major_x = int(round(w * rng.uniform(0.026, 0.038)))
    minor_x = int(round(w * rng.uniform(0.005, 0.010)))
    major_y = int(round(h * rng.uniform(0.026, 0.038)))
    minor_y = int(round(h * rng.uniform(0.005, 0.010)))

    if rng.random() < 0.5:
        crop_left, crop_right = max(pad_x, major_x), max(pad_x, minor_x)
    else:
        crop_left, crop_right = max(pad_x, minor_x), max(pad_x, major_x)

    if rng.random() < 0.5:
        crop_top, crop_bottom = max(pad_y, major_y), max(pad_y, minor_y)
    else:
        crop_top, crop_bottom = max(pad_y, minor_y), max(pad_y, major_y)

    if crop_left + crop_right < w - 24 and crop_top + crop_bottom < h - 24:
        cropped = rotated.crop((crop_left, crop_top, w - crop_right, h - crop_bottom))
    else:
        cropped = img

    # 3. Chuẩn hoá kích thước + biến thiên tỉ lệ vi mô (±1.2%)
    cw, ch = cropped.size
    scale_x = rng.uniform(0.986, 1.014)
    scale_y = rng.uniform(0.986, 1.014)
    target_w = max(32, int(round(w * scale_x)))
    target_h = max(32, int(round(h * scale_y)))

    max_side = max(target_w, target_h)
    if max_side > _MAX_DIMENSION:
        scale_down = _MAX_DIMENSION / float(max_side)
        target_w = max(32, int(round(target_w * scale_down)))
        target_h = max(32, int(round(target_h * scale_down)))

    if (cw, ch) != (target_w, target_h):
        resized = cropped.resize((target_w, target_h), resample=Image.Resampling.LANCZOS)
    else:
        resized = cropped

    # 4. Trường điều biến ánh sáng tần số thấp nhắm đích + cân bằng màu vi mô
    arr = np.asarray(resized, dtype=np.float32)
    luma_field = _build_targeted_lowfreq_field(resized, rng, target_bits=12, max_delta=12.0)

    channel_gains = np.array([
        rng.uniform(0.985, 1.015),
        rng.uniform(0.988, 1.012),
        rng.uniform(0.985, 1.015),
    ], dtype=np.float32)[None, None, :]
    channel_bias = np.array([
        rng.uniform(-1.5, 1.5),
        rng.uniform(-1.2, 1.2),
        rng.uniform(-1.5, 1.5),
    ], dtype=np.float32)[None, None, :]

    np_rng = np.random.default_rng(rng.randrange(1, 2**31 - 1))
    dither = np_rng.normal(0.0, 0.75, size=arr.shape).astype(np.float32)

    out = arr * channel_gains + luma_field[:, :, None] + channel_bias + dither
    out_u8 = np.clip(np.rint(out), 0, 255).astype(np.uint8)

    return Image.fromarray(out_u8, mode="RGB")


def sanitize_image(
    filepath: str,
    force: bool = False,
    seed: Optional[str] = None,
    normalize_to_jpg: bool = True,
) -> Optional[Dict[str, object]]:
    """
    Xoá sạch metadata và áp dụng bước chống trùng khớp ảnh mức cơ bản.

    Args:
        filepath: Đường dẫn file ảnh trên đĩa.
        force: True nếu muốn bắt buộc xử lý lại kể cả khi đã có trong index.
        seed: Seed tuỳ chọn (mặc định băm từ tên file gốc).
        normalize_to_jpg: Chuẩn hoá đầu ra về đuôi .jpg (khớp presign key .jpg của OKX OSS).

    Returns:
        dict {'path': str, 'width': int, 'height': int, 'file_size': int}
        hoặc None nếu file rỗng/hỏng (file hỏng sẽ được xoá).
    """
    if not filepath or not os.path.exists(filepath):
        return None

    try:
        raw_size = os.path.getsize(filepath)
    except OSError:
        return None

    if raw_size < _MIN_VALID_BYTES:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return None

    image_dir = os.path.dirname(filepath) or "."
    base_name = os.path.basename(filepath)
    stem, ext = os.path.splitext(base_name)
    out_ext = ".jpg" if normalize_to_jpg else (ext.lower() if ext.lower() in (".jpg", ".jpeg", ".png") else ".jpg")
    out_name = f"{stem}{out_ext}"
    out_path = os.path.join(image_dir, out_name)

    # Kiểm tra idempotency qua index
    if not force and os.path.exists(out_path):
        with _INDEX_LOCK:
            index = _load_index(image_dir)
            entry = index.get(out_name)
        if entry:
            try:
                st = os.stat(out_path)
                if entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
                    return {
                        "path": out_path,
                        "width": int(entry["width"]),
                        "height": int(entry["height"]),
                        "file_size": int(st.st_size),
                    }
            except Exception:
                pass

    # Đọc và decode ảnh gốc
    try:
        with Image.open(filepath) as src:
            src.load()
            clean_rgb = _to_clean_rgb(src)
    except Exception:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return None

    seed_key = seed or stem
    transformed = _apply_perceptual_evasion(clean_rgb, seed_key=seed_key)
    width, height = transformed.size

    rng = random.Random(f"orbit-jpeg-q|{seed_key}")
    quality = rng.randint(89, 93)
    subsampling = rng.choice([0, 1, 2])

    tmp_path = f"{out_path}.sanitize_tmp"
    try:
        if out_ext in (".jpg", ".jpeg"):
            # Lưu JPEG thuần không EXIF, không ICC, không comment
            transformed.save(
                tmp_path,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=False,
                subsampling=subsampling,
            )
        else:
            transformed.save(tmp_path, format="PNG", optimize=True)

        os.replace(tmp_path, out_path)

        # Nếu đổi đuôi (vd .png -> .jpg), xoá file gốc cũ
        if os.path.normcase(os.path.abspath(filepath)) != os.path.normcase(os.path.abspath(out_path)):
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except OSError:
                pass

        st = os.stat(out_path)
        with _INDEX_LOCK:
            index = _load_index(image_dir)
            index[out_name] = {
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "width": width,
                "height": height,
            }
            if base_name != out_name and base_name in index:
                index.pop(base_name, None)
            _save_index(image_dir, index)

        return {
            "path": out_path,
            "width": width,
            "height": height,
            "file_size": st.st_size,
        }
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None


def ensure_sanitized_image(filepath: str) -> Optional[Dict[str, object]]:
    """
    Đảm bảo file ảnh đã được xoá metadata + biến đổi chống trùng trước khi upload,
    và trả về kích thước thật {'path', 'width', 'height', 'file_size'}.
    """
    if not filepath:
        return None
    if not os.path.exists(filepath):
        # Thử kiểm tra phiên bản .jpg nếu DB còn lưu đuôi .png cũ
        stem, _ = os.path.splitext(filepath)
        alt_jpg = f"{stem}.jpg"
        if os.path.exists(alt_jpg):
            filepath = alt_jpg
        else:
            return None
    return sanitize_image(filepath, force=False, normalize_to_jpg=True)


def cleanup_image_files(image_paths_str: Optional[str]) -> int:
    """Xoá các file ảnh vật lý khi bài viết đã đăng xong hoặc bị loại bỏ."""
    if not image_paths_str:
        return 0
    removed = 0
    for raw_path in str(image_paths_str).split("|"):
        path = raw_path.strip()
        if not path:
            continue
        candidates = [path]
        stem, _ = os.path.splitext(path)
        if f"{stem}.jpg" not in candidates:
            candidates.append(f"{stem}.jpg")
        for candidate in candidates:
            if os.path.exists(candidate):
                try:
                    os.remove(candidate)
                    removed += 1
                except OSError:
                    pass
    return removed


def compute_dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Tính 64-bit dHash để kiểm thử độ lệch perceptual hash."""
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float32)
    diff = pixels[:, 1:] > pixels[:, :-1]
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(bool(b))
    return bits


def compute_ahash(img: Image.Image, hash_size: int = 8) -> int:
    """Tính 64-bit aHash để kiểm thử độ lệch perceptual hash."""
    gray = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float32)
    avg = float(pixels.mean())
    diff = pixels >= avg
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(bool(b))
    return bits


def compute_phash(img: Image.Image, hash_size: int = 8) -> int:
    """Tính 64-bit DCT pHash chuẩn (32x32 DCT -> 8x8 low-freq AC vs median)."""
    size = hash_size * 4
    gray = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    # 1D DCT-II Orthonormal matrix
    n = size
    dct_mat = np.zeros((n, n), dtype=np.float64)
    for k in range(n):
        alpha = math.sqrt(1.0 / n) if k == 0 else math.sqrt(2.0 / n)
        for i in range(n):
            dct_mat[k, i] = alpha * math.cos(math.pi * (2 * i + 1) * k / (2.0 * n))
    dct_2d = dct_mat @ pixels @ dct_mat.T
    low = dct_2d[:hash_size, :hash_size].flatten()
    ac = low[1:]  # bỏ thành phần DC
    med = float(np.median(ac))
    bits = 0
    for val in low:
        bits = (bits << 1) | int(val > med)
    return bits


def hamming_distance(h1: int, h2: int) -> int:
    return (h1 ^ h2).bit_count()
