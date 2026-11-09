"""
QR Code Detector
========================================
Module phát hiện mã QR trong ảnh sử dụng OpenCV.
Dùng trong pipeline crawl để loại bỏ bài viết chứa QR code (spam/quảng cáo).

Chiến lược phát hiện (Multi-Pass):
  1. Ảnh gốc → cv2.QRCodeDetector
  2. Ảnh grayscale → cv2.QRCodeDetector  
  3. Ảnh tăng contrast (CLAHE) → cv2.QRCodeDetector
  4. Ảnh resize (nếu quá lớn hoặc quá nhỏ) → cv2.QRCodeDetector

Mỗi pass thêm ~1-3ms trên ảnh thông thường → tổng ~5-15ms/ảnh.
"""

import cv2
import numpy as np
import os


def _detect_qr_in_image(img, detector: cv2.QRCodeDetector) -> bool:
    """
    Thử detect QR trên một ảnh numpy array.
    Returns True nếu tìm thấy QR code hợp lệ (có payload data).
    """
    if img is None or img.size == 0:
        return False
    try:
        data, bbox, _ = detector.detectAndDecode(img)
        if data and len(data.strip()) > 0:
            return True
    except Exception:
        pass
    return False


def contains_qr_code(image_path: str) -> bool:
    """
    Kiểm tra ảnh tại đường dẫn có chứa mã QR hợp lệ hay không.
    
    Sử dụng chiến lược multi-pass để tối đa hoá độ chính xác:
    - Pass 1: Ảnh gốc (nhanh nhất, bắt hầu hết QR rõ ràng)
    - Pass 2: Ảnh grayscale (loại bỏ nhiễu màu)
    - Pass 3: Ảnh tăng contrast CLAHE (bắt QR mờ/chìm trong nền)
    - Pass 4: Resize về kích thước chuẩn nếu ảnh quá lớn/nhỏ

    Args:
        image_path: Đường dẫn tuyệt đối tới file ảnh

    Returns:
        True nếu phát hiện mã QR hợp lệ, False nếu không
    """
    if not image_path or not os.path.exists(image_path):
        return False

    try:
        img = cv2.imread(image_path)
        if img is None:
            return False
    except Exception:
        return False

    detector = cv2.QRCodeDetector()

    # ====== PASS 1: Ảnh gốc ======
    if _detect_qr_in_image(img, detector):
        return True

    # ====== PASS 2: Grayscale ======
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if _detect_qr_in_image(gray, detector):
            return True
    except Exception:
        pass

    # ====== PASS 3: CLAHE (tăng contrast cục bộ) ======
    try:
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        if _detect_qr_in_image(enhanced, detector):
            return True
    except Exception:
        pass

    # ====== PASS 4: Resize nếu ảnh quá lớn hoặc quá nhỏ ======
    try:
        h, w = img.shape[:2]
        # Ảnh quá lớn (>2000px) → resize xuống để detector hoạt động tốt hơn
        if max(h, w) > 2000:
            scale = 1000.0 / max(h, w)
            resized = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if _detect_qr_in_image(resized, detector):
                return True
        # Ảnh quá nhỏ (<300px) → phóng to
        elif max(h, w) < 300:
            scale = 600.0 / max(h, w)
            resized = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            if _detect_qr_in_image(resized, detector):
                return True
    except Exception:
        pass

    return False


def scan_images_for_qr(image_paths: list) -> list:
    """
    Quét danh sách ảnh, trả về danh sách đường dẫn ảnh chứa QR code.
    
    Args:
        image_paths: List đường dẫn ảnh

    Returns:
        List[str]: Các đường dẫn ảnh phát hiện có QR code
    """
    flagged = []
    for path in image_paths:
        if contains_qr_code(path):
            flagged.append(path)
    return flagged
