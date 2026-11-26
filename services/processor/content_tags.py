"""Content tagging, prompt-angle routing, similarity fingerprinting, and output validation.

Shared utility module used by GeminiProcessor, GeminiProcessorPool,
MultiAccountPoster, and test_rewrite to enforce clean OKX $CASHTAG rules,
prevent refusal/placeholder leakage, and compute near-duplicate fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple, Union

REJECTION_KEYWORDS: Tuple[str, ...] = (
    "[reject]",
    "tôi từ chối",
    "từ chối thực hiện",
    "từ chối xử lý",
    "từ chối do",
    "không thể thực hiện yêu cầu",
    "không thể đáp ứng yêu cầu",
    "không thể xử lý yêu cầu",
    "yêu cầu bị từ chối",
    "yêu cầu này bị từ chối",
    "vi phạm chính sách",
    "vi phạm điều khoản",
    "vi phạm quy tắc",
    "bộ lọc nội dung",
    "bộ lọc thông tin",
    "huy động vốn trái phép",
    "huy động vốn và phát hành",
    "nội dung nhạy cảm",
    "chứa thông tin liên quan đến chính trị",
    "chính trị, chính phủ",
    "địa chính trị",
    "xung đột quân sự",
    "i cannot fulfill",
    "i can't fulfill",
    "cannot fulfill this request",
    "violates the safety",
    "safety guidelines",
    "against my guidelines",
    "as an ai",
    "cannot generate content",
)

_PLACEHOLDER_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"không có thông số khối lượng cụ thể", re.IGNORECASE),
    re.compile(r"quy mô ghi nhận\s*:\s*không", re.IGNORECASE),
    re.compile(r"bài viết gốc không (?:đề cập|cung cấp|nêu)", re.IGNORECASE),
    re.compile(r"dữ liệu (?:gốc )?không (?:đề cập|cung cấp|nêu rõ)", re.IGNORECASE),
    re.compile(r"không có số liệu (?:cụ thể )?trong bài gốc", re.IGNORECASE),
)

_SPELLED_NUMBER_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\b(?:hai|ba|bốn|năm|sáu|bảy|tám|chín)\s+mươi\s+(?:mốt|hai|ba|tư|bốn|lăm|sáu|bảy|tám|chín)?\s*(?:giờ|h|ngày|tuần|tháng|năm|phần trăm|triệu|tỷ|nghìn)\b", re.IGNORECASE),
    re.compile(r"\bmười\s+(?:một|hai|ba|bốn|lăm|sáu|bảy|tám|chín)\s+(?:giờ|phần trăm|triệu|tỷ)\b", re.IGNORECASE),
)

_EXCLUDED_TAGS: frozenset[str] = frozenset({
    # Fiat & stablecoins
    "USD", "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDE", "PYUSD",
    "USDD", "FRAX", "LUSD", "EUR", "EURC", "VND", "JPY", "GBP", "CNY", "RMB",
    "KRW", "AUD", "CAD", "CHF", "HKD", "SGD", "TRY", "RUB", "INR", "BRL",
    # Exchanges & social platforms
    "OKX", "OKEX", "BINANCE", "BYBIT", "KUCOIN", "BITGET", "COINBASE", "KRAKEN",
    "MEXC", "GATE", "HTX", "HUOBI", "UPBIT", "BITHUMB", "SQUARE", "BINANCESQUARE",
    "OKXSQUARE", "TELEGRAM", "DISCORD", "TWITTER", "YOUTUBE", "REDDIT",
    # Macro / TradFi / Generic acronyms
    "SEC", "CFTC", "FED", "FOMC", "CPI", "PPI", "GDP", "PCE", "NFP", "ETF",
    "ETP", "IPO", "ATH", "ATL", "ROI", "ROE", "APY", "APR", "TVL", "FDV",
    "MCAP", "PNL", "DCA", "FOMO", "FUD", "DYOR", "NFA", "KYC", "AML", "OTC",
    "DEX", "CEX", "DEFI", "NFT", "DAO", "WEB3", "AI", "RWA", "MEME", "GAMEFI",
    "DEPIN", "L1", "L2", "VIP", "API", "SDK", "USA", "US", "UK", "EU", "VN",
    "CN", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "SPX",
    "NDX", "DXY", "VIX", "XAU", "XAG", "WTI", "GOLD", "SILVER", "SP500",
    "NASDAQ", "NYSE", "MSTR", "COIN", "HOOD", "CRCL", "STOP", "LONG", "SHORT",
    "LONGSHORT", "TP", "SL", "RR", "OI", "MA", "EMA", "RSI", "MACD", "BB",
    "VP", "H1", "H4", "D1", "W1", "M15", "M5", "CRYPTO", "ALTCOIN", "ALTCOINS",
    "BITCOIN", "ETHEREUM", "TRADING", "NEWS", "MARKET", "REJECT", "MM", "KOL",
    "IDO", "IEO", "ICO", "VC", "TV", "PR", "CEO", "CTO",
})

_COIN_NAME_ALIASES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bbitcoin\b", re.IGNORECASE), "BTC"),
    (re.compile(r"\bethereum\b", re.IGNORECASE), "ETH"),
    (re.compile(r"\bsolana\b", re.IGNORECASE), "SOL"),
    (re.compile(r"\bripple\b", re.IGNORECASE), "XRP"),
    (re.compile(r"\bcardano\b", re.IGNORECASE), "ADA"),
    (re.compile(r"\bdogecoin\b", re.IGNORECASE), "DOGE"),
    (re.compile(r"\bavalanche\b", re.IGNORECASE), "AVAX"),
    (re.compile(r"\bchainlink\b", re.IGNORECASE), "LINK"),
    (re.compile(r"\bpolkadot\b", re.IGNORECASE), "DOT"),
    (re.compile(r"\btoncoin\b", re.IGNORECASE), "TON"),
    (re.compile(r"\btron\b", re.IGNORECASE), "TRX"),
    (re.compile(r"\blitecoin\b", re.IGNORECASE), "LTC"),
    (re.compile(r"\barbitrum\b", re.IGNORECASE), "ARB"),
    (re.compile(r"\boptimism\b", re.IGNORECASE), "OP"),
    (re.compile(r"\baptos\b", re.IGNORECASE), "APT"),
    (re.compile(r"\bcelestia\b", re.IGNORECASE), "TIA"),
    (re.compile(r"\buniswap\b", re.IGNORECASE), "UNI"),
    (re.compile(r"\baave\b", re.IGNORECASE), "AAVE"),
    (re.compile(r"\bbittensor\b", re.IGNORECASE), "TAO"),
    (re.compile(r"\bhyperliquid\b", re.IGNORECASE), "HYPE"),
    (re.compile(r"\bjupiter\b", re.IGNORECASE), "JUP"),
    (re.compile(r"\bethena\b", re.IGNORECASE), "ENA"),
    (re.compile(r"\bstarknet\b", re.IGNORECASE), "STRK"),
    (re.compile(r"\beigenlayer\b", re.IGNORECASE), "EIGEN"),
    (re.compile(r"\bberachain\b", re.IGNORECASE), "BERA"),
    (re.compile(r"\bworldcoin\b", re.IGNORECASE), "WLD"),
    (re.compile(r"\bshiba\s*inu\b", re.IGNORECASE), "SHIB"),
)

_KNOWN_PLAIN_TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK", "DOT",
    "TON", "TRX", "LTC", "ARB", "OP", "APT", "SUI", "TIA", "PEPE", "SHIB",
    "UNI", "AAVE", "MKR", "NEAR", "ATOM", "RENDER", "RNDR", "TAO", "HYPE",
    "JUP", "ENA", "ONDO", "WLD", "OKB", "STRK", "EIGEN", "BERA", "SEI",
    "INJ", "FTM", "WIF", "BONK", "FLOKI", "BCH", "ETC", "FIL", "ICP", "POL",
    "MATIC", "STX", "IMX", "KAS", "PYTH", "JTO", "FET", "ARKM", "PENDLE",
    "LDO", "CRV", "SUSHI", "COMP", "SNX", "GMX", "DYDX", "ENS", "BLUR",
    "ORDI", "SATS", "PI", "VIRTUAL", "TRB", "RUNE", "GRT", "ALGO", "VET",
    "HBAR", "XLM", "EOS", "XTZ", "THETA", "AXS", "SAND", "MANA", "GALA",
})

_ANGLE_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("p06_du_lieu_onchain_dong_lenh", (
        "on-chain", "onchain", "cá voi", "whale", "ví lớn", "nạp sàn", "rút sàn",
        "inflow", "outflow", "unlock", "mở khóa", "burn", "đốt", "etf", "quỹ",
        "treasury", "reserve", "exchange balance", "tích lũy dài hạn",
    )),
    ("p07_phai_sinh_funding_oi", (
        "funding", "open interest", "oi ", " oi", "thanh lý", "liquidation",
        "squeeze", "đòn bẩy", "leverage", "futures", "perpetual", "perp",
        "long/short", "tỷ lệ long", "bán khống", "margin",
    )),
    ("p02_cau_truc_vung_gia", (
        "hỗ trợ", "kháng cự", "support", "resistance", "breakout", "breakdown",
        "retest", "phá vỡ", "quét thanh khoản", "stop-loss", "sl ", "cung cầu",
        "trendline", "vùng giá", "đỉnh", "đáy", "pullback",
    )),
    ("p03_nen_bien_dong_nhip", (
        "nén", "sideway", "đi ngang", "tích lũy", "biến động", "volatility",
        "bollinger", "cạn cung", "chuyển pha", "nhịp", "khung 4h", "khung ngày",
        "chờ bung", "co hẹp",
    )),
    ("p04_luanchuyen_dong_von", (
        "dominance", "btc.d", "altseason", "altcoin", "luân chuyển", "xoay vòng",
        "dòng vốn", "sector", "narrative", "layer-1", "layer 2", "memecoin",
        "vốn hóa", "mid-cap", "dẫn dắt", "lan tỏa",
    )),
    ("p05_quan_tri_rui_ro", (
        "rủi ro", "kỷ luật", "cắt lỗ", "stop loss", "r:r", "fomo", "fud",
        "quản trị vốn", "vị thế", "giữ vốn", "bảo toàn", "đừng vội", "bẫy",
    )),
    ("p08_fact_check_be_noi", (
        "fed", "fomc", "cpi", "ppi", "lãi suất", "vĩ mô", "macro", "sec",
        "báo cáo", "report", "tin đồn", "rumor", "thông báo", "niêm yết",
        "chính thức", "đối chiếu", "dữ liệu kinh tế",
    )),
    ("p09_kich_ban_hai_chieu", (
        "kịch bản", "nếu ", "ngược lại", "mục tiêu", "target", "xác nhận",
        "vô hiệu", "scenario", "chiến lược", "điều kiện",
    )),
    ("p10_tam_ly_nhip_song", (
        "tâm lý", "đám đông", "fear", "greed", "tham lam", "sợ hãi",
        "hưng phấn", "hoảng loạn", "sentiment", "nhỏ lẻ", "retail",
    )),
    ("p01_dong_tien_thuc_chien", (
        "dòng tiền", "sổ lệnh", "order flow", "thanh khoản", "volume",
        "khối lượng", "gom hàng", "xả hàng", "lực mua", "lực bán", "tạo lập",
    )),
)

_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Za-z][A-Za-z0-9]{1,9})(?![A-Za-z0-9_])")
_TOKEN_RE = re.compile(r"[0-9a-zA-Z\u00C0-\u1EF9]+")

_VIET_STOPWORDS: frozenset[str] = frozenset({
    "và", "là", "của", "cho", "trong", "khi", "với", "từ", "đến", "vào", "ra",
    "này", "đó", "những", "các", "một", "hai", "đang", "đã", "sẽ", "vẫn", "cũng",
    "nhưng", "hoặc", "nếu", "thì", "mà", "về", "trên", "dưới", "theo", "tại",
    "bị", "được", "làm", "có", "không", "chưa", "rất", "hơn", "nhất", "cần",
    "nên", "có thể", "the", "and", "for", "with", "that", "this", "from", "are",
})


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def extract_coin_tags(
    text: str,
    fallback_tags: Optional[List[str]] = None,
    max_tags: int = 3,
) -> List[str]:
    """Extract up to ``max_tags`` valid uppercase crypto tickers for OKX ``cashTagList``.

    Priority:
    1. Explicit ``$TICKER`` cashtags appearing in ``text`` (excluding fiat, stablecoins,
       exchanges, TradFi stocks, and generic trading acronyms).
    2. Known coin names (e.g. Bitcoin -> BTC) or standalone uppercase tickers in ``text``.
    3. Clean ticker-like entries from ``fallback_tags`` if no ticker was detected in text.
    """
    if max_tags <= 0:
        return []

    text = text or ""
    seen: List[str] = []

    def _add(raw_ticker: str) -> None:
        ticker = str(raw_ticker or "").strip().lstrip("$#").upper()
        if not ticker or len(ticker) < 2 or len(ticker) > 10:
            return
        if not re.match(r"^[A-Z][A-Z0-9]{1,9}$", ticker):
            return
        if ticker in _EXCLUDED_TAGS or ticker in seen:
            return
        seen.append(ticker)

    # 1. Explicit $TICKERs present in content (only uppercase/$btc forms)
    for m in _CASHTAG_RE.finditer(text):
        _add(m.group(1))
        if len(seen) >= max_tags:
            return seen[:max_tags]

    # 2. If none found via $, detect coin names & known standalone tickers in content
    if not seen:
        for pattern, ticker in _COIN_NAME_ALIASES:
            if pattern.search(text):
                _add(ticker)
                if len(seen) >= max_tags:
                    return seen[:max_tags]

        for m in re.finditer(r"\b([A-Z]{2,8})\b", text):
            cand = m.group(1)
            if cand in _KNOWN_PLAIN_TICKERS:
                _add(cand)
                if len(seen) >= max_tags:
                    return seen[:max_tags]

    # 3. Last resort: clean ticker-like tags from raw crawled hashtags
    if not seen and fallback_tags:
        for raw in fallback_tags:
            cand = str(raw or "").strip().lstrip("$#").upper()
            if re.match(r"^[A-Z][A-Z0-9]{1,6}$", cand) and cand not in _EXCLUDED_TAGS:
                if cand in _KNOWN_PLAIN_TICKERS or len(cand) <= 5:
                    _add(cand)
                    if len(seen) >= max_tags:
                        break

    return seen[:max_tags]


def classify_content_angles(text: str) -> List[str]:
    """Return ordered variant IDs (``p01_...``..``p10_...``) best matching ``text``."""
    low = (text or "").lower()
    scored: List[Tuple[int, int, str]] = []
    for idx, (variant_id, keywords) in enumerate(_ANGLE_RULES):
        hits = sum(1 for kw in keywords if kw in low)
        if hits > 0:
            scored.append((hits, -idx, variant_id))

    if not scored:
        return [vid for vid, _ in _ANGLE_RULES]

    scored.sort(reverse=True)
    matched = [vid for _, _, vid in scored]
    matched_set = set(matched)
    for vid, _ in _ANGLE_RULES:
        if vid not in matched_set:
            matched.append(vid)
    return matched


def build_text_fingerprint(
    text: str,
    coin_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a compact JSON-serializable near-duplicate fingerprint for a post."""
    raw = text or ""
    tags = list(coin_tags) if coin_tags is not None else extract_coin_tags(raw)
    primary_coin = tags[0] if tags else ""

    norm = _strip_accents(raw.lower())
    norm = re.sub(r"https?://\S+", " ", norm)
    tokens = [
        tok for tok in _TOKEN_RE.findall(norm)
        if tok and tok not in _VIET_STOPWORDS and (len(tok) > 1 or tok.isdigit())
    ]

    shingle_set = set()
    if len(tokens) >= 3:
        for i in range(len(tokens) - 2):
            gram = f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]}".encode("utf-8")
            h = int.from_bytes(hashlib.blake2b(gram, digest_size=4).digest(), "big")
            shingle_set.add(h)
    elif tokens:
        for tok in tokens:
            h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=4).digest(), "big")
            shingle_set.add(h)

    keywords = []
    seen_kw = set()
    for tok in tokens:
        if len(tok) >= 3 and not tok.isdigit() and tok not in seen_kw:
            seen_kw.add(tok)
            keywords.append(tok)
            if len(keywords) >= 24:
                break

    numbers = sorted(set(re.findall(r"\$?\d+(?:[.,]\d+)?%?", raw)))[:12]

    return {
        "primary_coin": primary_coin,
        "coins": tags[:3],
        "shingles": sorted(shingle_set)[:128],
        "keywords": keywords,
        "numbers": numbers,
        "word_count": len(raw.split()),
    }


def _coerce_fingerprint(fp: Union[str, Dict[str, Any], None]) -> Optional[Dict[str, Any]]:
    if fp is None:
        return None
    if isinstance(fp, dict):
        return fp
    if isinstance(fp, str):
        s = fp.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def compute_similarity(
    fp1: Union[str, Dict[str, Any], None],
    fp2: Union[str, Dict[str, Any], None],
) -> float:
    """Compute Jaccard similarity in ``[0.0, 1.0]`` between two fingerprints."""
    d1 = _coerce_fingerprint(fp1)
    d2 = _coerce_fingerprint(fp2)
    if not d1 or not d2:
        return 0.0

    s1 = set(d1.get("shingles") or [])
    s2 = set(d2.get("shingles") or [])
    k1 = set(d1.get("keywords") or [])
    k2 = set(d2.get("keywords") or [])

    shingle_sim = 0.0
    if s1 and s2:
        union_len = len(s1 | s2)
        if union_len > 0:
            shingle_sim = len(s1 & s2) / union_len

    kw_sim = 0.0
    if k1 and k2:
        kw_union = len(k1 | k2)
        if kw_union > 0:
            kw_sim = len(k1 & k2) / kw_union

    if min(len(s1), len(s2)) >= 5:
        return round(shingle_sim, 4)
    return round(max(shingle_sim, 0.85 * kw_sim), 4)


def validate_rewritten_output(
    text: str,
    min_words: int = 40,
    max_words: int = 220,
) -> Tuple[bool, str]:
    """Validate rewritten OKX Square text before storing as ``processed``.

    Catches empty/reject markers, leaked model refusal messages, placeholder
    "missing data" statements, spelled-out large numbers, markdown leftovers,
    and extreme word-count outliers.
    """
    if not text or not isinstance(text, str):
        return False, "empty_output"

    stripped = text.strip()
    if not stripped or stripped == "[REJECT]":
        return False, "rejected"

    low = stripped.lower()
    if any(kw in low for kw in REJECTION_KEYWORDS):
        return False, "refusal_leakage"

    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(stripped):
            return False, "placeholder_phrase"

    for pat in _SPELLED_NUMBER_PATTERNS:
        if pat.search(stripped):
            return False, "spelled_out_number"

    if "**" in stripped or "```" in stripped or re.search(r"https?://|t\.me/", low):
        return False, "unclean_formatting"

    word_count = len(stripped.split())
    if word_count < min_words:
        return False, f"too_short:{word_count}"
    if word_count > max_words:
        return False, f"too_long:{word_count}"

    return True, "ok"
