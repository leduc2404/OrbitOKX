# Những Điểm Tinh Tuý Từ Hệ Thống Binance Square

Sau khi phân tích kỹ hai thư mục `squareAPI` và `Utils` (của hệ thống Binance gốc), đây là những kiến trúc và kỹ thuật lập trình tinh tuý nhất có thể áp dụng để nâng cấp hệ thống `OKXOrbis`:

> [!TIP]
> Các kỹ thuật này không chỉ giúp ứng dụng chạy nhanh hơn mà còn tăng khả năng chịu lỗi (fault tolerance), tối ưu hoá chi phí API, và tránh các rủi ro bị ban IP/Tài khoản.

## 1. Quản Lý Cơ Sở Dữ Liệu Chuyên Sâu (Database Manager)
Hệ thống Binance sở hữu một `DatabaseManager` cực kỳ mạnh mẽ, khắc phục được điểm yếu cố hữu của SQLite khi chạy đa luồng:
- **WAL Mode (Write-Ahead Logging)**: Thay vì khoá (lock) toàn bộ database mỗi khi ghi dữ liệu, WAL mode cho phép nhiều luồng (threads) đọc và một luồng ghi đồng thời mà không bị "Database is locked".
- **Connection Pooling & Auto Retry**: Kết hợp cơ chế `Exponential Backoff` khi thực thi câu lệnh SQL. Nếu bị lock do quá tải, nó sẽ tự lùi lại một chút rồi thử lại thay vì crash ngay lập tức `(execute_with_retry)`.
- **Atomic Operations (Dùng `BEGIN IMMEDIATE`)**: Khi có nhiều Worker (luồng) cùng tranh nhau vào lấy bài đăng (Claim Post), hệ thống dùng cơ chế Transaction chặt chẽ kết hợp khoá ngay lập tức từ lúc SELECT, đảm bảo tuyệt đối không có 2 luồng cùng lấy nhầm 1 bài viết.
- **Garbage Collection (Cleanup & Vacuum)**: Khả năng tự động xoá ảnh cũ, prune database và chạy lệnh `PRAGMA incremental_vacuum` để giải phóng dung lượng rác mà không gây gián đoạn hệ thống.

## 2. Sliding Window Rate Limiter & Load Balancer (Bộ Định Tuyến API Key)
Đây là "trái tim" để vượt qua giới hạn của Google Gemini/Gemma API:
- **Anti-Burst**: API thường cấm số request quá nhanh trong 1 giây (VD: gọi 10 request cùng lúc dù limit cho 1 phút là 10). Thuật toán ép khoảng nghỉ `min_interval` tối thiểu giữa những lần gọi.
- **Fail-Fast & Auto Penalty**: Nếu một API Key báo hết hạn mức (Quota) hoặc quá tải (429 Rate Limit), hệ thống không chờ đợi vô ích (Sleep 60s như code thông thường) mà lập tức giáng "Penalty" lên key đó và **chuyển ngay sang Key khác** đang rảnh (Load Balancer).
- **Phân phối tải đều (Fair Distribution)**: Thuật toán luôn chọn Key đang rảnh nhất thay vì round-robin truyền thống, tối ưu hiệu suất đến mức tối đa.

## 3. Kiến Trúc AI Đa Nguồn (Multimodal Gemini Processor)
- Modun xử lý nội dung được thiết kế theo `Strategy Pattern` cho phép hoạt động với 3 chế độ độc lập: 
  1. Trực tiếp Google GenAI (bằng thư viện mới) với 1 mảng các dòng key tự luân phiên.
  2. Qua một Local Proxy giả lập OpenAI API `gemini_proxy` (tiết kiệm chi phí, dễ cắm endpoint tuỳ chỉnh).
  3. Cơ chế song song hoặc tuần tự tự động tuỳ biến.
- Tự động Cache đối tượng client thay vì khởi tạo lại gây tràn RAM, giới hạn Cache size với `_cache_lock`.

## 4. Cơ Chế Xử Lý Lỗi Và Retry Triệt Để (Post-level Retry)
- Khi gọi Gemini thất bại, hệ thống lưu lại rành mạch: `retry_count`, `last_error`, `failed_at`.
- Có một Background Job để nhặt các bài bị lỗi, chờ qua thời gian `cooldown_minutes` (5-10 phút) để thử đi thử lại cho đến khi báo thành công (hoặc đến giới hạn cắt hẵn).

## 👉 Ứng Dụng Thiết Lập Cho Dàn OKXOrbis Mới Của Bạn
Dựa theo hệ thống này, tôi sẽ triển khai lại module `utils` cho bạn gồm:
1. Tạo module `gemini_processor.py` (3 provider architecture).
2. Kết nối nó vào 1 `GeminiProcessorPool` (xử lý đa luồng an toàn).
3. Sử dụng `rate_limiter.py` tuyệt vời từ Binance.
4. Xây dựng lại `database_manager.py` cho OKX (**Vì OKX không chia group ra 2 nhóm như Binance, ta sẽ bỏ qua logic chia bảng g1/g2, chỉ cập nhật trực tiếp `gemini_output`** - giúp database nhẹ và nhánh code sạch gọn).
