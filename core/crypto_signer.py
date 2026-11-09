import hashlib
import uuid
import base64
import os
import ecdsa

class CryptoSigner:
    """
    Xử lý mọi nghiệp vụ ký và mã hóa độc lập (Pure Cryptography).
    Bao gồm: Ký chuẩn ECDSA P-256 (cho X-Client-Signature) và MD5 (cho X-Id).
    """
    def __init__(self, account_id, dev_id):
        self.account_id = account_id
        self.dev_id = dev_id
        
        # Load hoặc tạo mới ECDSA key theo Account ID
        self._sk = self._load_or_create_keypair()
        self._vk = self._sk.get_verifying_key()
        self.pub_key_b64 = base64.b64encode(self._vk.to_der()).decode("utf-8")
        
    def _load_or_create_keypair(self):
        """Mỗi account có 1 file key .pem riêng lưu trong data/keys/"""
        key_dir = os.path.join("data", "keys")
        os.makedirs(key_dir, exist_ok=True)
        key_file = os.path.join(key_dir, f"{self.account_id}_ecdsa.pem")
        
        if os.path.exists(key_file):
            with open(key_file, "rb") as f:
                return ecdsa.SigningKey.from_pem(f.read())
        
        # Tạo mới nếu chưa có
        sk = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
        with open(key_file, "wb") as f:
            f.write(sk.to_pem())
        return sk

    def generate_xid(self, path):
        """Tạo X-Id: MD5 của DevId_Path_UUID"""
        req_uuid = str(uuid.uuid4())
        raw = f"{self.dev_id}_{path}_{req_uuid}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def generate_signature(self, path, body, timestamp):
        """Tạo chuỗi ký chuẩn OKX (POST/PUT): ECDSA P-256(SHA-256) -> DER -> Base64"""
        raw = f"{path}{body}{timestamp}"
        sig = self._sk.sign(
            raw.encode("utf-8"),
            hashfunc=hashlib.sha256,
            sigencode=ecdsa.util.sigencode_der
        )
        return base64.b64encode(sig).decode("utf-8")
