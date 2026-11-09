# Phân tích các API Mới Của OKX App (Bắt qua Frida)

Sau khi dùng Frida để hook vào `java.security.Signature` (ECDSA) và `java.security.MessageDigest` (MD5 X-Id), tôi đã bắt trọn vẹn đường dẫn (Path) và cấu trúc nội dung (Body) của các request mã hóa được thực hiện từ máy ảo AVD.

Dưới đây là chi tiết để bạn có thể thêm vào `OKXApi` / `OKXPublisher` khi nâng cấp tool:

## 1. API: Thả Tim (Like / Unlike)
*   **Method:** `POST`
*   **Path:** `/priapi/v5/content/follower/like`
*   **Body (JSON):**
    ```json
    {
      "contentId": "73402238725184",
      "isLiked": true
    }
    ```
    *Ghi chú: Thay `73402238725184` bằng ID của bài viết. Dùng `true` để Like hoặc `false` để Unlike.*

## 2. API: Comment
*   **Method:** `POST`
*   **Path:** `/priapi/v5/content/user/comment`
*   **Body (JSON):**
    ```json
    {
      "hashTagListStr": "[]",
      "contentId": "73402238725184",
      "comment": "hay"
    }
    ```
    *Ghi chú: Truyền text bình luận vào trường `comment`. Trường `hashTagListStr` có thể truyền là một chuỗi mảng rỗng `"[]"` như trên app.*

## 3. API: Theo dõi (Follow)
*   **Method:** `POST`
*   **Path:** `/priapi/v5/content/user/follow`
*   **Body (JSON):**
    ```json
    {
      "status": "1",
      "authorId": "788088993798946829"
    }
    ```
    *Ghi chú: `status: "1"` là Follow, `"0"` có thể là Unfollow.*

## 4. Các API Đọc Dữ Liệu (GET)
Các API này không sinh ra `Signature(body)` (do body rỗng), nhưng Frida MD5 đã tiết lộ rõ các URL lấy dữ liệu:
*   **Load thông tin tác giả cá nhân:** `/priapi/v5/content/public/author/detail`
*   **Load Feed màn hình chính (Dành cho bạn):** 
    `/priapi/v5/rubik/public/content/feed/1`
*   **Load chi tiết 1 bài viết:**
    `/priapi/v5/rubik/public/content/detail/1`
*   **Load danh sách comment của bài viết:** 
    `/priapi/v5/content/public/comments`

**Tích hợp vào Codebase:**
Việc tích hợp vào module `core/okx_api.py` của bạn rất dễ dàng vì các request POST này đều theo đúng format của cơ chế sinh mã hiện tại: `Signature = ECDSA(path + body + timestamp)`. Bạn chỉ cần truyền chính xác `path` và JSON chuỗi payload như trên vào code ký là OKX sẽ cho qua!
