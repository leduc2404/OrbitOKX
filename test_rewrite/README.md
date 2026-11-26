# OKXSquare — Prompt Tuning Lab (`test_rewrite`)

Giao diện web Flask gọn nhẹ, chuyên sâu để **Crawl → Chạy thử 20 Prompt (Group 1 + Group 2) → Phát hiện điểm yếu & trùng lặp → Sửa trực tiếp từng file `.txt`**. Tách biệt hoàn toàn khỏi database production (`data/okx_square.db`).

## Chạy nhanh

```powershell
python test_rewrite/app.py
# Tùy chọn port và chế độ lưu crawl ban đầu (ram | disk):
python test_rewrite/app.py --port 5055 --storage ram
```

Truy cập: `http://127.0.0.1:5055`

## 3 Chế độ Tinh chỉnh Prompt

1. **Mode 1 · 1 Bài × 1 Prompt (`Tập trung & Sửa trực tiếp`)**
   - 2 ô so sánh **Bài gốc (A)** ↔ **Bài đã xử lý (B)** kèm **Trình sửa Prompt `.txt`** ngay bên dưới.
   - **Test nháp** không ghi đè file; khi ưng ý nhấn **Lưu file `.txt`** (tự động tạo backup gốc trong `test_rewrite/cache/prompt_backups/` để khôi phục 1 click).
   - Tab **Diff Trước ↔ Sau** so sánh trực tiếp output trước và sau khi bạn sửa prompt để thấy rõ tác dụng từng câu chữ chỉnh sửa.
2. **Mode 2 · 1 Bài × 20 Prompt (`Ma trận G1 + G2`)**
   - Chạy cùng 1 bài viết qua toàn bộ 20 prompt (`g1_01..g1_10`, `g2_01..g2_10` hoặc tập tick chọn) song song đa luồng (có nút **Dừng**).
   - **Radar Độ đa dạng & Kiểm định tự động**: phát hiện cặp prompt viết na ná nhau (`trùng X%`), câu mở đầu bị lặp, và tổng hợp lỗi tuân thủ (lệch số từ, dính văn mẫu AI, emoji...).
   - Mỗi card kết quả có nút **✎ Sửa Prompt** (mở Drawer trượt bên phải) và **↻ Chạy lại** ngay tại chỗ không cần chạy lại cả 20 prompt.
3. **Mode 3 · Xoay đủ 20 Prompt (`1 Bài — 1 Prompt`)**
   - Gán xoay vòng 1-1: `Bài 1 → g1_01`, `Bài 2 → g1_02`, …, `Bài 20 → g2_10`.
   - Hiển thị **Bản đồ Coverage 20 prompt** theo màu điểm số, danh sách 20 cặp **Bài gốc ↔ Bài xử lý** song song, và hỗ trợ sửa prompt trực tiếp từ bất kỳ cặp nào.
