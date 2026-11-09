# AUTOMATION BOT CHO OKX - KIẾN TRÚC & QUY TRÌNH (V2.0)

Phiên bản này đã đạt mức siêu tự động hoá. Quá trình Onboarding tài khoản giờ đây được rút ngắn xuống chỉ còn **1 thao tác Copy-Paste** dọn sẵn cho việc Scale lên hàng trăm thiết bị cùng sự tích hợp cấu hình Proxy.

---

## 🏗️ CẤU TRÚC VẬN HÀNH DYNAMIC

```text
OrbitOKX/
├── cookies/                    # Thả mảng JSON Cookie trích xuất từ trình duyệt vào đây
├── config/
│   └── settings.yaml           # Quản lý hàng loạt tài khoản nào chạy, độ trễ và Proxy.
├── data/
│   ├── keys/                   # Các tệp khóa bảo mật chuẩn ECDSA sinh tự động
│   └── default_image.png       
├── core/
│   ├── crypto_signer.py        # Logic Thuật toán Mã hoá (Bypass MD5/ECDSA)
│   ├── okx_api.py              # Xử lý Kết nối mạng, đính kèm Proxy.
│   └── publisher.py            # Logic Nghiệp vụ. Đăng bài lên Okx Square.
└── main.py                     # Cục điều phối (Đọc YAML -> Quét Cookies -> Gửi Bài).
```

### 🧠 Luồng xử lý "Just-In-Time" (Thời Gian Thực)
Thay vì dùng Tool phụ để import Account vào File Database phiền phức:
1. `main.py` đọc tệp cấu hình YAML xem bạn muốn chạy tài khoản nào (VD: `acc_01_thanhcong`).
2. Script sẽ tự mò vào folder `cookies/` để đọc file `acc_01_thanhcong.json`.
3. Tự động bóc tách mã xác thực (Token JWT), mã thiết bị ảo (DevId) và gắn nó vào máy quét. Xong xuôi thì xóa nó khỏi bộ nhớ ROM.

---

## 🧗 QUY TRÌNH BA BƯỚC ĐỂ MULTI-ACCOUNT

### BƯỚC 1: XUẤT COOKIE TỪ TRÌNH DUYỆT
1. Mở Cốc Cốc / Chrome / Edge trên máy tính truy cập trang chủ `okx.com` và đăng nhập.
2. Cài cắm tiện ích **EditThisCookie** hoặc **Cookie-Editor**.
3. Nhấn vô nút tiện ích -> Bấm nút ngã rẽ có hình Mũi tên (Export format JSON). Sẽ có một đám mã code được đưa vào bộ nhớ tạm (Clipboard).
4. Vào thư mục `cookies/` sinh một tệp mới tên là `ten_tai_khoan_cua_ban.json`. Dán mã code lấy được vào rồi lưu lại.

### BƯỚC 2: CẬP NHẬT YAML CONFIG
Mở tệp `config/settings.yaml` lên:
```yaml
# Danh sách tài khoản sẽ chạy tuần tự
active_accounts:
  - ten_tai_khoan_cua_ban
  - acc_02_tester

# Chỉnh sửa Random Timeout giữa 2 bình luận ngẫu nhiên
delay_between_posts_seconds:
  min: 30
  max: 120

# Gắn Proxy cố định (Nếu rỗng sẽ xài Mạng Wi-Fi nhà bạn)
proxies:
  ten_tai_khoan_cua_ban: "http://user:pass@127.0.0.1:8080"
  acc_02_tester: ""
```

### BƯỚC 3: ĐỐT MÁY (RUN BOT)
Gõ lệnh `python main.py` ở terminal. Mọi ma thuật còn lại bao gồm định danh khóa ẩn, bypass MD5, đổi mạng IP sang Proxy tuỳ biến, đăng ảnh sẽ được tự động hoàn tất. Hệ thống cũng có khả năng ngưng nghỉ Random giữa các Bot để tránh cờ đỏ của OKX.
