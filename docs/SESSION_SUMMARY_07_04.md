# 📝 TỔNG KẾT PHIÊN LÀM VIỆC NGÀY 07/04/2026

Hôm nay chúng ta đã tập trung nâng cấp hệ thống **Crawl (Lấy bài)** và cải tiến module **Publish (Đăng bài)** để giải quyết các vấn đề cốt lõi về hiệu năng, tốc độ, cũng như hoàn thiện định dạng dữ liệu cho nền tảng OKX.

Dưới đây là chi tiết các hạng mục đã hoàn thành:

---

## 1. 🕷️ Xây dựng OKX Spider v2 (Đa luồng & Tự động)
Hệ thống cào tin trước đây chỉ chạy đơn lẻ, nay đã được nâng cấp thành một "Spider" chạy ngầm liên tục, tự động hoá cao:
*   **Chạy song song (Multithreading):** Viết script `run_spider.py` sử dụng `ThreadPoolExecutor`, cho phép cào cùng lúc nhiều tác giả. Sức chứa được cấu hình qua `max_workers` trong file `settings.yaml`. 
*   **Loop tự động (Vòng lặp thời gian):** Tự động nghỉ 20 phút và quét lại. Hỗ trợ tắt an toàn (Graceful Shutdown) khi ấn `Ctrl+C`.
*   **Quản lý trạng thái thông minh:** Sinh ra file `data/spider_state.json` để lưu lại thời điểm cuối cùng (timestamp) bot đọc bài → Lần chạy sau **chỉ tải bài mới**, tránh việc cào đi cào lại bài cũ. Mặc định lúc chạy lần đầu sẽ tải lùi về 2 tiếng.
*   **Database Upsert:** Gắn kết trơn tru với `DatabaseManager` để chỉ chèn (hoặc cập nhật) những bài thực sự mới vào file SQLite (`data/okx_square.db`). 

## 2. 🧠 Lọc & Tiền xử lý dữ liệu (Cải tiến core/crawler.py)
*   **Bỏ qua bài ghim (`isPinned = 1`):** Cực kỳ quan trọng vì tác giả trên OKX thường ghim các bài cũ trên đỉnh timeline. Việc lọc mục này loại bỏ hoàn toàn tình trạng crawl dư thừa.
*   **Bộ lọc text rác:** Tự động loại bỏ các bài viết quá ngắn (dưới 10 ký tự).
*   **Tải Ảnh (Download):** Các ảnh trong bài đăng giờ đây được API nạp, tự động tải xuống trực tiếp và lưu vào thư mục `data/images/` với ID chỉ mục rõ ràng.
*   **Xử lý Rate Limit (Bị sút kết nối):** Bổ sung chiến lược **Exponential Backoff** cho `curl_cffi` (Retry sau 2s, 4s, 8s...). Điều này đã giúp chống đỡ trực tiếp trước các lỗi rớt mạng hoặc bị tường lửa Cloudflare chặn đột ngột (`curl error 7: Failed to connect to port 443`).

## 3. 🚀 Sửa lỗi không có "Thẻ Xanh" (Tags) khi đăng bài (core/publisher.py)
*   **Phân tích API OKX:** Phát hiện ra OKX Square không tự bắt link từ file text thuần, mà bắt buộc phải khai báo bộ thẻ con `contentTags` (gồm `cashTag` cho Coin và `hashTag` cho Hashtag).
*   **Code Parser Tự Động:** Đã viết thêm bộ trích xuất dữ liệu bằng Regex (`_parse_content_tags`):
    *   Tự động chộp các cú pháp Coin dạng `$BTC`, `$SOL`...
    *   Tự động chộp Hashtag dạng `#Crypto`, `#OKX`...
    *   **Fix lỗi cực khó về Vị trí:** Chỉnh sửa khái niệm vị trí `position` trong JSON (Từ Index đoạn văn → Index tần suất/đếm số lần xuất hiện ở nội dung). Đây là điểm giúp OKX đọc và bôi xanh chính xác tag nằm ở đâu.
*   **Kết quả:** Bài post qua bot giờ đây đẹp chuẩn như app thật với các tags xanh dương - bấm vào là chuyển hướng (Clickable Links).

---

## 📌 Các File Đã Xử Lý Hôm Nay
1.  `run_spider.py` (Script chạy tổng tạo luồng vô hạn định kỳ)
2.  `core/crawler.py` (Bộ não chọc API lấy timeline và filter ghim)
3.  `core/publisher.py` (Sửa payload POST nội dung có kèm `contentTags`)
4.  `config/settings.yaml` (Thêm khối spider config - target authors, delay, khoảng lùi thời gian...)
5.  *(Test files sinh qua và đã được xoá đi: `debug_keys.txt`, `test.py`, `test_tags.py`)*

## 🎯 Việc cần theo dõi & làm tiếp cho ngày mai
1.  Theo dõi logs của `run_spider.py` trong quá trình ngâm trên máy xem lỗi *curl error 7* có xảy ra quá thường xuyên hay không. Nếu có bị lặp quá nhiều thì cần giảm số `max_workers` trong config xuống.
2.  Phần Spider đã xong nhiệm vụ **kéo dữ liệu thô (crawled)** về cất kho. Giai đoạn tiếp theo ta sẽ đi ráp vào phần nhặt rổ `crawled` ra để chui qua AI (Gemini) xử lý viết lại bài theo style của mình.
3.  Áp dụng Image_Processor (Pillow) để tự động crop ảnh / xóa watermark (nếu lấy bài mượn từ đối thủ) giống y hệt chiến lược đã thảo luận.
