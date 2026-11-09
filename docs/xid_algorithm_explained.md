# Thuật toán Ký X-Id và X-Client-Signature của OKX

Dựa trên việc Hook sâu vào lớp bảo mật của cấu trúc mạng (O.ffD interceptor và java.security.MessageDigest/Signature), dưới đây là thuật toán chính xác 100% được OKX sử dụng để tạo `X-Id` và `X-Client-Signature`.

## 1. Headers Tổng Quan
- **`X-Request-Timestamp`**: Thời gian hiện tại tính bằng milliseconds (Unix Epoch).
- **`X-Id`**: Chuỗi MD5 hash đóng vai trò định danh mỗi Request (chống lại Replay Attack và Duplicate).
- **`X-Client-Signature`**: Chữ ký điện tử ECDSA (Chỉ gắn trên POST/PUT API có payload body).
- **`X-Client-Public-Key`**: Khóa công khai của cặp khóa ECDSA.

---

## 2. Giải mã `X-Id` (MD5)
Toàn bộ `x-id` được tạo ra bằng cách Hash MD5 một chuỗi format rất nguyên thủy và rõ ràng.
**Công thức đầu vào (Raw String):**
```text
[DevId]_[PathWithoutQuery]_[RandomUUID]
```
- `[DevId]`: Lấy từ header `devId` của máy (vd: `5da582bd-cede-3d11-b09a-2ea938549b97`)
- `[PathWithoutQuery]`: Đường dẫn của API, KHÔNG bao gồm parameters. (Vd: `/priapi/v5/content/ugc/publish`)
- `[RandomUUID]`: Một mã UUID bất kỳ dùng làm Nonce cho request hiện tại.

**Code Python giả lập:**
```python
import hashlib
import uuid

dev_id = "5da582bd-cede-3d11-b09a-2ea938549b97"
path = "/priapi/v5/content/ugc/publish"
req_uuid = str(uuid.uuid4())

raw_xid_input = f"{dev_id}_{path}_{req_uuid}"
x_id = hashlib.md5(raw_xid_input.encode('utf-8')).hexdigest()
```

---

## 3. Giải mã `X-Client-Signature` (ECDSA)
Chữ ký này chỉ được tính toán cho các request có chứa Body (chẳng hạn như `POST`, phương thức `GET` thông thường của API OKX User không có header signature này).
Nó sử dụng thuật toán ký `SHA256withECDSA` (Curve SECP256R1 / NIST P-256).

**Công thức đầu vào (Raw String):**
```text
[PathWithoutQuery][RequestBody][Timestamp]
```
*(Ghi chú: Nối trực tiếp liền nhau không có bất kỳ dấu phân cách nào).*

- `[PathWithoutQuery]`: Đường dẫn API (Vd: `/priapi/v5/content/ugc/publish`).
- `[RequestBody]`: Đoạn JSON raw dạng thô chuẩn bị gửi đi.
- `[Timestamp]`: Chuỗi thời gian mili-giây (Vd: `1775559606601`).

**Code Python giả lập:**
```python
import ecdsa
import hashlib
import base64

# Dữ liệu
path = "/priapi/v5/content/ugc/publish"
body = '{"publishId":"1775559606587","content":"hh","channelId":"1001","group":"USDT"}'
timestamp = "1775559606601"

# Chuỗi cần ký
raw_sig_input = f"{path}{body}{timestamp}"

# Ký ECDSA (Giả sử bạn đã có private_key)
# private_key = ecdsa.SigningKey.from_pem(...)
signature = private_key.sign(
    raw_sig_input.encode('utf-8'),
    hashfunc=hashlib.sha256,
    sigencode=ecdsa.util.sigencode_der
)

x_client_signature = base64.b64encode(signature).decode('utf-8')
```

> **Ghi chú quan trọng về Private/Public Key:** 
> Header `X-Client-Public-Key` được cấp phát từ cặp khóa P-256 ECDSA. Đối với Pure Python, nếu server chấp nhận public key động, bạn có thể tự Generate 1 cặp PEM Pair, chuyển public sang base64 nhét vào Header, và dùng Private tự ký cho toàn bộ Request mà không cần Frida/Emulator Keystore!
