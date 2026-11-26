"""Instrumented multi-provider rewrite engine + prompt file editor + compliance & diversity analyzer.

Reuses OKXSquare's exact safety patterns, 10 prompt variants (variants/ split 5+5 in UI),
output cleaner, cashtag extractor, and SDK protocols (OpenAI-compatible proxy & Google GenAI SDK).
"""
from __future__ import annotations

import os
import random
import re
import time
import traceback
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from google import genai as new_genai
from google.genai import types as new_genai_types
from openai import OpenAI

from services.processor.content_tags import (
    REJECTION_KEYWORDS as _REJECTION_KEYWORDS,
    extract_coin_tags,
)
from services.processor.gemini_processor import _SENSITIVE_PATTERNS
from test_rewrite.config_loader import StudioConfigLoader, mask_secret
from test_rewrite.log_buffer import LogBuffer


_BANNED_AI_CLICHES = [
    "nhìn chung",
    "tóm lại",
    "bức tranh toàn cảnh",
    "đi sâu vào",
    "đáng chú ý",
    "hứa hẹn",
    "chắc chắn lời",
    "không rủi ro",
    "siêu sóng",
    "to the moon",
    "cá con đang fomo",
    "đám đông sai lầm",
    "retail đang đu đỉnh",
]

_FORBIDDEN_EXCHANGES_AND_PLATFORMS = [
    r"\bbinance\b",
    r"\bbybit\b",
    r"\bokx\b",
    r"\bkucoin\b",
    r"\bbitget\b",
    r"\btelegram\b",
    r"\bdiscord\b",
    r"\bfacebook\b",
    r"https?://\S+",
    r"\bt\.me/\S+",
]

_ADVICE_WORDS = [
    r"\bmua\b",
    r"\bbán\b",
    r"\blong\b",
    r"\bshort\b",
    r"\ball-?in\b",
]

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000231A-\U000023FF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_SPELLED_VN_NUM_RE = re.compile(
    r"\b("
    r"(?:hai|ba|bốn|tư|năm|sáu|bảy|tám|chín)\s+(?:mươi|trăm|nghìn|ngàn|triệu|tỷ|tỉ)"
    r"|một\s+(?:trăm|nghìn|ngàn|phẩy|triệu|tỷ|tỉ)"
    r"|mười\s+(?:một|hai|ba|bốn|lăm|sáu|bảy|tám|chín)\s*(?:triệu|tỷ|tỉ|nghìn|ngàn|lần|phần trăm)"
    r"|(?:trăm|nghìn|ngàn)\s+(?:linh|lẻ)\s+(?:một|hai|ba|bốn|năm|sáu|bảy|tám|chín)"
    r")\b",
    flags=re.IGNORECASE | re.UNICODE,
)

_ANGLE_RE = re.compile(
    r"(?:\[\s*)?GÓC NHÌN(?:\s+BẮT BUỘC(?:\s+CỦA BÀI NÀY)?)?\s*:\s*([^\]\r\n]+)",
    re.IGNORECASE,
)


def find_sensitive_matches(text: str) -> List[str]:
    """Return all matched sensitive keywords/phrases from OKXSquare's pre-screen rules."""
    if not text:
        return []
    low = text.lower()
    hits: List[str] = []
    seen = set()
    for pat in _SENSITIVE_PATTERNS:
        for m in re.finditer(pat, low):
            val = m.group(0).strip()
            if val and val not in seen:
                seen.add(val)
                hits.append(val)
    return hits


def clean_output_text(text: str) -> Tuple[str, bool, Optional[str]]:
    """Mirror GeminiProcessor._clean_output while reporting rejection reason if triggered."""
    if not text:
        return "", False, None

    low = text.lower()
    for kw in _REJECTION_KEYWORDS:
        if kw in low:
            return "[REJECT]", True, f"Khớp từ khóa từ chối từ model: '{kw}'"

    cleaned = text.replace("*", "").replace("`", "")
    unwanted_prefixes = [
        "okay, here's",
        "here is the",
        "here's the",
        "sure!",
        "sure,",
        "i'll",
        "based on",
        "this is",
        "let me",
        "alright,",
        "dưới đây là",
        "sau đây là",
    ]
    lines = cleaned.strip().split("\n")
    while lines:
        first_lower = lines[0].strip().lower()
        if any(first_lower.startswith(p) for p in unwanted_prefixes):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        else:
            break

    final_lines = []
    for line in lines:
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"(?:^|\s)#[A-Za-z0-9_]{2,30}\b", "", line)
        if line.strip():
            line = line.rstrip()
        final_lines.append(line)

    return "\n".join(final_lines).strip(), False, None


def audit_rewrite(
    original_text: str,
    rewritten_text: str,
    raw_output: str,
    group: int = 2,
) -> Dict[str, Any]:
    """Audit rewritten output against OKXSquare editorial & compliance rules."""
    if not rewritten_text or rewritten_text == "[REJECT]":
        return {
            "score": 0,
            "word_count": 0,
            "char_count": 0,
            "orig_word_count": len((original_text or "").split()),
            "tickers": [],
            "emoji_count": 0,
            "checks": [],
        }

    words = rewritten_text.split()
    wc = len(words)
    orig_wc = len((original_text or "").split())
    min_w, max_w = (55, 125) if group == 1 else (70, 145)

    checks: List[dict] = []

    # 1. Word count range
    if min_w <= wc <= max_w:
        checks.append({
            "id": "word_count",
            "label": f"{wc} từ",
            "status": "pass",
            "detail": f"Đạt khung Group {group} ({min_w}–{max_w} từ)",
        })
    elif (min_w - 20) <= wc <= (max_w + 35):
        checks.append({
            "id": "word_count",
            "label": f"{wc} từ",
            "status": "warn",
            "detail": f"Hơi lệch khung Group {group} ({min_w}–{max_w} từ)",
        })
    else:
        checks.append({
            "id": "word_count",
            "label": f"{wc} từ",
            "status": "fail",
            "detail": f"Ngoài khung chuẩn Group {group} ({min_w}–{max_w} từ)",
        })

    # 2. Markdown stars
    star_in_clean = "*" in rewritten_text
    star_in_raw = "*" in (raw_output or "")
    if star_in_clean:
        checks.append({
            "id": "no_markdown",
            "label": "Còn dấu *",
            "status": "fail",
            "detail": "Vẫn còn ký tự '*' trong output",
        })
    elif star_in_raw:
        checks.append({
            "id": "no_markdown",
            "label": "Đã lọc *",
            "status": "warn",
            "detail": "Model sinh '*' nhưng _clean_output đã tự gỡ bỏ",
        })
    else:
        checks.append({
            "id": "no_markdown",
            "label": "Sạch *",
            "status": "pass",
            "detail": "Không có markdown * hay **",
        })

    # 2b. Backtick leakage (e.g. `$BTC`)
    bt_in_clean = "`" in rewritten_text
    bt_in_raw = "`" in (raw_output or "")
    if bt_in_clean:
        checks.append({
            "id": "no_backticks",
            "label": "Còn dấu `",
            "status": "fail",
            "detail": "Vẫn còn ký tự backtick (`) trong output (VD: `$BTC`)",
        })
    elif bt_in_raw:
        checks.append({
            "id": "no_backticks",
            "label": "Đã lọc `",
            "status": "warn",
            "detail": "Model sinh ký tự backtick (`) nhưng cleaner đã tự gỡ bỏ",
        })
    else:
        checks.append({
            "id": "no_backticks",
            "label": "Sạch `",
            "status": "pass",
            "detail": "Không rò rỉ ký tự backtick quanh $TICKER",
        })

    # 2c. Spelled-out Vietnamese numbers (e.g. "ba trăm ba mươi", "hai mươi lần")
    spelled_hits = list(dict.fromkeys(m.group(0).strip() for m in _SPELLED_VN_NUM_RE.finditer(rewritten_text)))
    if spelled_hits:
        checks.append({
            "id": "no_spelled_numbers",
            "label": f"Số viết chữ ({len(spelled_hits)})",
            "status": "warn",
            "detail": f"Nên viết bằng chữ số chuẩn thay vì đánh vần: {', '.join(spelled_hits[:4])}",
        })
    else:
        checks.append({
            "id": "no_spelled_numbers",
            "label": "Số chuẩn",
            "status": "pass",
            "detail": "Không đánh vần số liệu lớn thành chữ",
        })

    # 2d. Basic number grounding check (flags large ungrounded numbers like fabricated prices)
    def _extract_nums(txt: str) -> List[float]:
        # Remove $TICKER tokens so $SUI2 or $0G doesn't pollute
        stripped = re.sub(r"\$[A-Za-z][A-Za-z0-9]*", " ", txt or "")
        # Remove 0x hex addresses
        stripped = re.sub(r"0x[0-9a-fA-F]+", " ", stripped)
        vals: List[float] = []
        for raw_n in re.findall(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)*", stripped):
            s = raw_n
            if "." in s and "," in s:
                if s.rfind(",") > s.rfind("."):
                    s = s.replace(".", "").replace(",", ".")
                else:
                    s = s.replace(",", "")
            elif "." in s:
                parts = s.split(".")
                if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3):
                    s = "".join(parts)
            elif "," in s:
                parts = s.split(",")
                if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3):
                    s = "".join(parts)
                else:
                    s = s.replace(",", ".")
            try:
                vals.append(float(s))
            except ValueError:
                pass
        return vals

    in_nums = _extract_nums(original_text)
    out_nums = _extract_nums(rewritten_text)
    if in_nums and out_nums:
        allowed = set(in_nums)
        # Allow basic verifiable derivations from input numbers: sums, diffs, ratios, 100/x, a/b, a*b, percentages
        for a in in_nums:
            allowed.add(round(a))
            if a > 0:
                allowed.add(100.0 / a)
                allowed.add(a * 100.0)
                allowed.add(a / 100.0)
                allowed.add(a * 1000.0)
                allowed.add(a / 1000.0)
                allowed.add(a * 1e6)
                allowed.add(a / 1e6)
        for i in range(len(in_nums)):
            for j in range(len(in_nums)):
                if i == j:
                    continue
                a, b = in_nums[i], in_nums[j]
                allowed.add(a + b)
                allowed.add((a + b) * 1e6)
                allowed.add(abs(a - b))
                if b > 0:
                    allowed.add(a / b)
                    allowed.add((a / b) * 100.0)
                    allowed.add((a / b) * 1e6)
                    allowed.add((a * 1e6) / b)
                if a + b > 0:
                    allowed.add((a / (a + b)) * 100.0)
                    for k in range(len(in_nums)):
                        c = in_nums[k]
                        if c > 0:
                            allowed.add((a + b) / c)
                            allowed.add((c / (a + b)) * 100.0)
                            allowed.add((c / ((a + b) / c)) * 100.0 if ((a + b) / c) > 0 else 0.0)
                            if (a + b) > 0 and c > 0:
                                for m_idx in range(len(in_nums)):
                                    lev = in_nums[m_idx]
                                    if 1 < lev <= 200:
                                        margin = (a + b) / lev
                                        if margin > 0:
                                            allowed.add(margin)
                                            allowed.add((c / margin) * 100.0)

        suspicious_nums: List[str] = []
        for v in out_nums:
            # Ignore small structural integers (1..25, 50, 100)
            if v <= 25 or v in (50.0, 100.0):
                continue
            matched = any(
                (abs(v - ref) <= max(1.5, abs(ref) * 0.06))
                for ref in allowed
                if ref > 0
            )
            if not matched:
                fmt = f"{int(v)}" if v.is_integer() else f"{v:.2f}"
                if fmt not in suspicious_nums:
                    suspicious_nums.append(fmt)

        if suspicious_nums:
            checks.append({
                "id": "number_grounding",
                "label": f"Số lạ ({len(suspicious_nums)})",
                "status": "warn",
                "detail": f"Kiểm tra khả năng bịa số/giá ngoài bài gốc: {', '.join(suspicious_nums[:5])}",
            })
        else:
            checks.append({
                "id": "number_grounding",
                "label": "Số khớp gốc",
                "status": "pass",
                "detail": "Các con số lớn đều khớp bài gốc hoặc phép tính hợp lệ",
            })

    # 3. No hashtags
    hashtags = re.findall(r"(?<!\w)#[A-Za-z0-9_]+", rewritten_text)
    if hashtags:
        checks.append({
            "id": "no_hashtags",
            "label": f"{len(hashtags)} hashtag",
            "status": "fail",
            "detail": f"Vi phạm luật không hashtag: {', '.join(hashtags[:5])}",
        })
    else:
        checks.append({
            "id": "no_hashtags",
            "label": "Không #",
            "status": "pass",
            "detail": "Tuân thủ quy tắc không dùng hashtag",
        })

    # 4. AI clichés
    low = rewritten_text.lower()
    cliche_hits = [c for c in _BANNED_AI_CLICHES if c in low]
    if cliche_hits:
        checks.append({
            "id": "anti_cliche",
            "label": f"Văn mẫu ({len(cliche_hits)})",
            "status": "fail",
            "detail": f"Phát hiện cụm cấm: {', '.join(cliche_hits)}",
        })
    else:
        checks.append({
            "id": "anti_cliche",
            "label": "Không văn mẫu",
            "status": "pass",
            "detail": "Không chứa các cụm sáo rỗng bị cấm",
        })

    # 5. Forbidden exchanges / platforms / URLs
    forbidden_hits: List[str] = []
    for pat in _FORBIDDEN_EXCHANGES_AND_PLATFORMS:
        for m in re.finditer(pat, low):
            if m.group(0) not in forbidden_hits:
                forbidden_hits.append(m.group(0))
    if forbidden_hits:
        checks.append({
            "id": "no_links_exchanges",
            "label": f"Sàn/Link ({len(forbidden_hits)})",
            "status": "fail",
            "detail": f"Cấm xuất hiện: {', '.join(forbidden_hits[:6])}",
        })
    else:
        checks.append({
            "id": "no_links_exchanges",
            "label": "Sạch sàn/link",
            "status": "pass",
            "detail": "Không chứa URL hay tên sàn/mạng xã hội",
        })

    # 6. Financial advice words
    advice_hits: List[str] = []
    for pat in _ADVICE_WORDS:
        for m in re.finditer(pat, low):
            w = m.group(0)
            if w not in advice_hits:
                advice_hits.append(w)
    if advice_hits:
        checks.append({
            "id": "advice_words",
            "label": f"Từ lệnh ({len(advice_hits)})",
            "status": "warn",
            "detail": f"Kiểm tra ngữ cảnh: {', '.join(advice_hits)}",
        })
    else:
        checks.append({
            "id": "advice_words",
            "label": "Không hô lệnh",
            "status": "pass",
            "detail": "Không chứa Mua/Bán/Long/Short/All-in",
        })

    # 7. Tickers, Paragraph Structure, Banned Opener & Emoji
    tickers = [f"${t}" for t in extract_coin_tags(rewritten_text, max_tags=5)]
    orig_mentions_coin = bool(
        re.search(
            r"(\$[A-Z0-9]{2,10}|\b(?:BTC|ETH|SOL|BNB|XRP|ADA|DOGE|AVAX|LINK|SUI|TON|ARB|OP|HBAR|AAVE|PEPE|Bitcoin|Ethereum|Solana)\b)",
            original_text or "",
            flags=re.IGNORECASE,
        )
    )
    if orig_mentions_coin and not tickers:
        checks.append({
            "id": "ticker_format",
            "label": "Thiếu $TICKER",
            "status": "warn",
            "detail": "Bài gốc có nhắc coin nhưng bài rewrite chưa gắn tiền tố $ (VD: $BTC, $ETH)",
        })
    elif tickers:
        checks.append({
            "id": "ticker_format",
            "label": f"{len(tickers)} $Ticker",
            "status": "pass",
            "detail": f"Đã gắn chuẩn: {', '.join(tickers[:6])}",
        })

    if re.search(r"^\s*(\$[A-Z0-9]+\s+hiện đang|biểu đồ\s+\$?[A-Z0-9]+\s+cho thấy)", rewritten_text, flags=re.IGNORECASE):
        checks.append({
            "id": "banned_opener",
            "label": "Mở bài văn mẫu",
            "status": "fail",
            "detail": "Vi phạm luật cấm mở đầu bằng '$TICKER hiện đang...' hoặc 'Biểu đồ $TICKER cho thấy...'",
        })

    non_empty_lines = [ln for ln in rewritten_text.splitlines() if ln.strip()]
    if wc >= 65 and len(non_empty_lines) <= 1:
        checks.append({
            "id": "paragraphs",
            "label": "Chưa tách đoạn",
            "status": "warn",
            "detail": "Bài dài trên 65 từ nhưng chỉ có 1 đoạn liền — nên xuống dòng tách ý",
        })

    has_zwj_emoji = "\u200d" in rewritten_text
    emojis = _EMOJI_RE.findall(rewritten_text)
    emoji_count = len(emojis)
    if has_zwj_emoji:
        checks.append({
            "id": "emoji",
            "label": "Emoji ghép ZWJ",
            "status": "warn",
            "detail": f"Chứa emoji ghép ZWJ (đếm thành {emoji_count} cụm) — nên dùng 0-1 emoji đơn",
        })
    else:
        checks.append({
            "id": "emoji",
            "label": f"{emoji_count} emoji",
            "status": "pass" if emoji_count <= 1 else "warn",
            "detail": "Sạch emoji / tối đa 1 emoji tinh tế" if emoji_count <= 1 else f"Có {emoji_count} cụm emoji (nên <= 1)",
        })

    score = 100
    for c in checks:
        if c["status"] == "fail":
            score -= 18
        elif c["status"] == "warn":
            score -= 6
    score = max(0, min(100, score))

    return {
        "score": score,
        "word_count": wc,
        "char_count": len(rewritten_text),
        "orig_word_count": orig_wc,
        "orig_char_count": len(original_text or ""),
        "tickers": tickers,
        "emoji_count": emoji_count,
        "checks": checks,
    }


def _tokenize_norm(text: str) -> List[str]:
    clean = re.sub(r"[^\w$]+", " ", (text or "").lower(), flags=re.UNICODE)
    return [w for w in clean.split() if len(w) >= 2]


def _bigrams(tokens: List[str]) -> set:
    if len(tokens) < 2:
        return set(tokens)
    return {f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)}


def analyze_matrix_results(items: List[dict]) -> Dict[str, Any]:
    """Compute cross-prompt diversity, overlap pairs, repeated openings/disclaimers & top violations."""
    ok_items = [x for x in items if x.get("status") == "ok" and (x.get("output_text") or "").strip()]
    err_items = [x for x in items if x.get("status") == "error"]
    rej_items = [x for x in items if x.get("status") == "rejected"]

    scores = [int((x.get("audit") or {}).get("score", 0)) for x in ok_items]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    g1_words = [int((x.get("audit") or {}).get("word_count", 0)) for x in ok_items if x.get("group") == 1]
    g2_words = [int((x.get("audit") or {}).get("word_count", 0)) for x in ok_items if x.get("group") == 2]

    # Pairwise bigram Jaccard similarity across prompts
    bg_sets = [(x.get("variant_id", "?"), _bigrams(_tokenize_norm(x.get("output_text", "")))) for x in ok_items]
    sim_pairs: List[dict] = []
    all_sims: List[float] = []
    per_variant_max_sim: Dict[str, float] = {}

    for i in range(len(bg_sets)):
        vid_a, sa = bg_sets[i]
        for j in range(i + 1, len(bg_sets)):
            vid_b, sb = bg_sets[j]
            if not sa or not sb:
                continue
            inter = len(sa & sb)
            union = len(sa | sb)
            sim = inter / union if union else 0.0
            all_sims.append(sim)
            if sim > per_variant_max_sim.get(vid_a, 0.0):
                per_variant_max_sim[vid_a] = sim
            if sim > per_variant_max_sim.get(vid_b, 0.0):
                per_variant_max_sim[vid_b] = sim
            if sim >= 0.24:
                sim_pairs.append({
                    "a": vid_a,
                    "b": vid_b,
                    "similarity": round(sim * 100, 1),
                })

    sim_pairs.sort(key=lambda p: p["similarity"], reverse=True)
    avg_sim = (sum(all_sims) / len(all_sims)) if all_sims else 0.0
    # Diversity score: 100% when avg bigram overlap = 0, drops smoothly as overlap grows
    diversity_score = max(0, min(100, round((1.0 - min(1.0, avg_sim * 2.2)) * 100)))

    # Repeated openings (first 4 normalized words) & repeated disclaimers (last line)
    opening_map: Dict[str, List[str]] = {}
    closing_map: Dict[str, List[str]] = {}
    for x in ok_items:
        vid = x.get("variant_id", "?")
        toks = _tokenize_norm(x.get("output_text", ""))
        if len(toks) >= 4:
            head = " ".join(toks[:4])
            opening_map.setdefault(head, []).append(vid)
        lines = [ln.strip() for ln in (x.get("output_text") or "").splitlines() if ln.strip()]
        if lines:
            tail_toks = _tokenize_norm(lines[-1])
            if len(tail_toks) >= 4:
                tail = " ".join(tail_toks[:6])
                closing_map.setdefault(tail, []).append(vid)

    repeated_openings = [
        {"phrase": k, "variants": v} for k, v in opening_map.items() if len(v) > 1
    ]
    repeated_closings = [
        {"phrase": k, "variants": v} for k, v in closing_map.items() if len(v) > 1
    ]

    # Aggregate compliance failures & warnings across all prompts
    issue_variants: Dict[str, List[str]] = {}
    issue_labels = {
        "word_count": "Lệch khung số từ",
        "no_markdown": "Sinh ký tự *",
        "no_backticks": "Rò rỉ backtick (`)",
        "no_spelled_numbers": "Đánh vần số thành chữ",
        "number_grounding": "Nghi vấn số lạ ngoài bài gốc",
        "no_hashtags": "Còn #hashtag",
        "anti_cliche": "Dính văn mẫu AI",
        "banned_opener": "Mở bài văn mẫu",
        "no_links_exchanges": "Còn tên sàn / URL",
        "advice_words": "Chứa từ hô lệnh",
        "ticker_format": "Thiếu $TICKER",
        "paragraphs": "Chưa tách đoạn",
        "emoji": "Số lượng/loại emoji chưa chuẩn",
    }
    for x in ok_items:
        vid = x.get("variant_id", "?")
        for chk in (x.get("audit") or {}).get("checks") or []:
            if chk.get("status") in ("fail", "warn"):
                cid = chk.get("id", "other")
                issue_variants.setdefault(cid, []).append(vid)

    top_issues = [
        {
            "id": cid,
            "label": issue_labels.get(cid, cid),
            "count": len(vids),
            "variants": vids,
        }
        for cid, vids in sorted(issue_variants.items(), key=lambda kv: len(kv[1]), reverse=True)
    ]

    weak_variants = sorted(
        [
            {"variant_id": x.get("variant_id"), "score": int((x.get("audit") or {}).get("score", 0))}
            for x in ok_items
            if int((x.get("audit") or {}).get("score", 0)) < 82
        ],
        key=lambda z: z["score"],
    )

    return {
        "total": len(items),
        "ok_count": len(ok_items),
        "rejected_count": len(rej_items),
        "error_count": len(err_items),
        "avg_score": avg_score,
        "diversity_score": diversity_score,
        "avg_words_g1": round(sum(g1_words) / len(g1_words), 1) if g1_words else None,
        "avg_words_g2": round(sum(g2_words) / len(g2_words), 1) if g2_words else None,
        "overlap_pairs": sim_pairs[:12],
        "per_variant_max_sim": {k: round(v * 100, 1) for k, v in per_variant_max_sim.items()},
        "repeated_openings": repeated_openings[:6],
        "repeated_closings": repeated_closings[:6],
        "top_issues": top_issues[:7],
        "weak_variants": weak_variants[:10],
    }


class RewriteEngine:
    """Multi-provider LLM rewrite engine with live prompt file editing, backups, and diagnostics."""

    def __init__(
        self,
        project_root: Path,
        cache_dir: Path,
        config_loader: StudioConfigLoader,
        logs: LogBuffer,
    ):
        self.project_root = Path(project_root).resolve()
        self.prompts_dir = self.project_root / "config" / "prompts"
        self.backup_dir = Path(cache_dir).resolve() / "prompt_backups"
        self.config_loader = config_loader
        self.logs = logs
        self._lock = Lock()
        self._recent_variants = {1: deque(maxlen=4), 2: deque(maxlen=4)}
        self._rr_key_idx: Dict[str, int] = {"gemma": 0, "gemini_direct": 0}
        self._prompts_cache: Dict[str, Any] = {}
        self.reload_prompts()

    @staticmethod
    def _extract_angle(content: str, fallback: str) -> str:
        m = _ANGLE_RE.search(content or "")
        if m:
            return m.group(1).strip().rstrip(".")
        return fallback

    def reload_prompts(self) -> Dict[str, Any]:
        """Load system prompt + 10 variants (từ config/prompts/variants, chia 5+5 cho UI; fallback group1/group2)."""
        def _read_dir(subdir: Path, group_num: int) -> List[dict]:
            items: List[dict] = []
            if not subdir.is_dir():
                return items
            for name in sorted(os.listdir(subdir)):
                if not name.lower().endswith(".txt"):
                    continue
                p = subdir / name
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception as e:
                    self.logs.warning("CONFIG", f"Không đọc được prompt {p.name}: {e}")
                    continue
                vid = os.path.splitext(name)[0]
                short_code = vid.split("_")[0].upper() if "_" in vid else vid.upper()
                pretty_name = re.sub(r"^(?:g[12]|p\d+)_", "", vid).replace("_", " ").title()
                angle = self._extract_angle(content, pretty_name)
                bak_path = self.backup_dir / f"{vid}.orig.txt"
                items.append({
                    "id": vid,
                    "code": short_code,
                    "group": group_num,
                    "label": f"{short_code} · {angle}",
                    "angle": angle,
                    "filename": name,
                    "file_path": str(p),
                    "has_placeholder": "{text}" in content,
                    "has_backup": bak_path.exists(),
                    "char_count": len(content),
                    "word_count": len(content.split()),
                    "est_tokens": max(1, len(content) // 4),
                    "content": content,
                })
            return items

        sys_path = self.prompts_dir / "gemini_system_prompt.txt"
        system_prompt = sys_path.read_text(encoding="utf-8").strip() if sys_path.exists() else ""

        variants = _read_dir(self.prompts_dir / "variants", 1)
        if variants:
            mid = max(1, (len(variants) + 1) // 2)
            g1 = variants[:mid]
            g2 = variants[mid:]
            for v in g1:
                v["group"] = 1
            for v in g2:
                v["group"] = 2
            all_variants = variants
        else:
            g1 = _read_dir(self.prompts_dir / "group1", 1)
            g2 = _read_dir(self.prompts_dir / "group2", 2)
            all_variants = [*g1, *g2]

        by_id = {v["id"]: v for v in all_variants}

        with self._lock:
            self._prompts_cache = {
                "prompts_dir": str(self.prompts_dir),
                "system_prompt": system_prompt,
                "group1": g1,
                "group2": g2,
                "all": all_variants,
                "by_id": by_id,
            }
        return self.get_prompts_catalog()

    def get_prompts_catalog(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "prompts_dir": self._prompts_cache.get("prompts_dir", ""),
                "system_prompt": self._prompts_cache.get("system_prompt", ""),
                "group1": list(self._prompts_cache.get("group1") or []),
                "group2": list(self._prompts_cache.get("group2") or []),
                "total": len(self._prompts_cache.get("all") or []),
            }

    def resolve_variant_list(self, scope_or_ids: Any) -> List[str]:
        """Resolve 'all' | 'group1' | 'group2' | list[str] to ordered list of valid variant IDs."""
        with self._lock:
            g1 = [v["id"] for v in (self._prompts_cache.get("group1") or [])]
            g2 = [v["id"] for v in (self._prompts_cache.get("group2") or [])]
            by_id = self._prompts_cache.get("by_id") or {}

        if isinstance(scope_or_ids, list):
            valid = [vid for vid in scope_or_ids if vid in by_id]
            return valid or [*g1, *g2]

        s = str(scope_or_ids or "all").lower().strip()
        if s in ("group1", "g1", "1"):
            return g1
        if s in ("group2", "g2", "2"):
            return g2
        return [*g1, *g2]

    def save_prompt_file(self, variant_id: str, new_content: str) -> dict:
        """Save edited prompt directly to its .txt file in config/prompts/, creating a 1-time original backup."""
        with self._lock:
            item = (self._prompts_cache.get("by_id") or {}).get(variant_id)
        if not item:
            raise KeyError(f"Không tìm thấy prompt '{variant_id}'")

        target = Path(item["file_path"]).resolve()
        # Strict path safety: must remain inside config/prompts
        if self.prompts_dir.resolve() not in target.parents:
            raise PermissionError("Đường dẫn prompt nằm ngoài thư mục config/prompts!")

        if "{text}" not in new_content:
            raise ValueError("Prompt bắt buộc phải có placeholder {text} để chèn nội dung bài viết!")

        # Create one-time exact byte-for-byte backup before first edit
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        bak_path = self.backup_dir / f"{variant_id}.orig.txt"
        if not bak_path.exists() and target.exists():
            bak_path.write_bytes(target.read_bytes())

        tmp_path = target.with_suffix(".txt.tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content.replace("\r\n", "\n"))
        os.replace(tmp_path, target)

        self.logs.success("CONFIG", f"Đã lưu prompt [{variant_id}] vào {target.name} (backup sẵn có: {bak_path.name})")
        self.reload_prompts()
        with self._lock:
            return dict(self._prompts_cache["by_id"][variant_id])

    def restore_prompt_backup(self, variant_id: str) -> dict:
        """Restore prompt file byte-for-byte from its pre-edit original backup."""
        with self._lock:
            item = (self._prompts_cache.get("by_id") or {}).get(variant_id)
        if not item:
            raise KeyError(f"Không tìm thấy prompt '{variant_id}'")

        bak_path = self.backup_dir / f"{variant_id}.orig.txt"
        if not bak_path.exists():
            raise FileNotFoundError(f"Chưa có bản backup gốc cho '{variant_id}'")

        target = Path(item["file_path"]).resolve()
        if self.prompts_dir.resolve() not in target.parents:
            raise PermissionError("Đường dẫn không hợp lệ!")

        tmp_path = target.with_suffix(".txt.tmp")
        tmp_path.write_bytes(bak_path.read_bytes())
        os.replace(tmp_path, target)

        self.logs.info("CONFIG", f"Đã khôi phục prompt [{variant_id}] về bản gốc trước khi sửa")
        self.reload_prompts()
        with self._lock:
            return dict(self._prompts_cache["by_id"][variant_id])

    def _resolve_prompt(
        self,
        text: str,
        source: str = "",
        group_pref: Any = "auto",
        variant_id: str = "auto",
        custom_prompt_template: Optional[str] = None,
        custom_system_prompt: Optional[str] = None,
    ) -> Tuple[str, str, str, int, str, str]:
        """Returns (rendered_user_prompt, system_prompt, used_variant_id, used_group, angle, template)."""
        with self._lock:
            sys_prompt = (
                custom_system_prompt
                if custom_system_prompt is not None
                else self._prompts_cache.get("system_prompt", "")
            )
            by_id = self._prompts_cache.get("by_id") or {}
            g1_pool = self._prompts_cache.get("group1") or []
            g2_pool = self._prompts_cache.get("group2") or []

        if str(group_pref) in ("1", "2"):
            group = int(group_pref)
        else:
            group = 1 if (source or "").lower() == "binance" else 2

        angle = ""
        if variant_id and variant_id != "auto" and variant_id in by_id:
            v = by_id[variant_id]
            template = custom_prompt_template if custom_prompt_template else v["content"]
            used_vid = v["id"]
            group = v["group"]
            angle = self._extract_angle(template, v["angle"])
        elif custom_prompt_template:
            template = custom_prompt_template
            used_vid = variant_id if variant_id and variant_id != "auto" else "custom"
            angle = self._extract_angle(template, "Custom Prompt")
        else:
            pool = g1_pool if group == 1 else g2_pool
            if not pool:
                pool = g2_pool or g1_pool
            if not pool:
                template = "Viết lại nội dung crypto sau bằng tiếng Việt:\n\n{text}"
                used_vid = "fallback_default"
                angle = "Default"
            else:
                with self._lock:
                    recent = self._recent_variants[group]
                    candidates = [v for v in pool if v["id"] not in set(recent)] or pool
                    chosen = random.choice(candidates)
                    recent.append(chosen["id"])
                    template = chosen["content"]
                    used_vid = chosen["id"]
                    angle = chosen["angle"]

        if "{text}" in template:
            rendered = template.replace("{text}", text)
        else:
            rendered = f"{template}\n\nINPUT TEXT:\n{text}"

        return rendered, (sys_prompt or "").strip(), used_vid, group, angle, template

    def _next_rr_key_index(self, provider: str, total_keys: int) -> int:
        with self._lock:
            idx = self._rr_key_idx.get(provider, 0) % total_keys
            self._rr_key_idx[provider] = (idx + 1) % total_keys
            return idx

    # ------------------------------------------------------------------
    # Provider Ping / Diagnostics
    # ------------------------------------------------------------------
    def ping_provider(
        self,
        provider_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rt = self.config_loader.resolve_provider_runtime(provider_name, overrides)
        provider = rt["provider"]
        t0 = time.time()

        if not rt["api_keys"]:
            err = f"Provider '{provider}' không có API key nào!"
            self.logs.error("CONFIG", err)
            return {"ok": False, "provider": provider, "error": err, "latency_ms": 0}

        try:
            if provider == "gemini_proxy":
                if not rt["endpoint"]:
                    raise ValueError("gemini_proxy thiếu endpoint!")
                client = OpenAI(
                    base_url=rt["endpoint"],
                    api_key=rt["api_keys"][0],
                    timeout=min(10, rt["request_timeout"]),
                    max_retries=0,
                )
                models_resp = client.models.list()
                models = sorted([m.id for m in (models_resp.data or []) if getattr(m, "id", None)])
                latency_ms = int((time.time() - t0) * 1000)
                self.logs.success(
                    "CONFIG",
                    f"Ping [{provider}] OK ({latency_ms}ms) — {rt['endpoint']} ({len(models)} models)",
                )
                return {
                    "ok": True,
                    "provider": provider,
                    "endpoint": rt["endpoint"],
                    "latency_ms": latency_ms,
                    "models": models[:50],
                    "selected_model": rt["model"],
                }
            else:
                idx = rt["selected_key_index"] if rt["selected_key_index"] is not None else 0
                api_key = rt["api_keys"][idx]
                client = new_genai.Client(
                    api_key=api_key,
                    http_options={"timeout": min(15, rt["request_timeout"]) * 1000},
                )
                models = []
                for m in client.models.list():
                    name = getattr(m, "name", "") or ""
                    if name.startswith("models/"):
                        name = name[len("models/"):]
                    if name and ("gemma" in name.lower() or "gemini" in name.lower()):
                        models.append(name)
                    if len(models) >= 40:
                        break
                latency_ms = int((time.time() - t0) * 1000)
                label = rt["key_labels"].get(api_key, f"Key #{idx + 1}")
                self.logs.success(
                    "CONFIG",
                    f"Ping [{provider}] OK ({latency_ms}ms) với {label} ({mask_secret(api_key)})",
                )
                return {
                    "ok": True,
                    "provider": provider,
                    "endpoint": "https://generativelanguage.googleapis.com",
                    "key_used": f"#{idx + 1} {label} ({mask_secret(api_key)})",
                    "latency_ms": latency_ms,
                    "models": sorted(models),
                    "selected_model": rt["model"],
                }
        except Exception as e:
            latency_ms = int((time.time() - t0) * 1000)
            tb = traceback.format_exc()
            self.logs.error(
                "CONFIG",
                f"Ping [{provider}] THẤT BẠI ({latency_ms}ms): {e}",
                details=tb,
            )
            return {
                "ok": False,
                "provider": provider,
                "endpoint": rt["endpoint"],
                "latency_ms": latency_ms,
                "error": str(e),
                "traceback": tb,
            }

    # ------------------------------------------------------------------
    # Rewrite Execution
    # ------------------------------------------------------------------
    def rewrite(
        self,
        text: str,
        *,
        post_id: str = "",
        source: str = "",
        provider: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        group: Any = "auto",
        variant_id: str = "auto",
        custom_prompt_template: Optional[str] = None,
        custom_system_prompt: Optional[str] = None,
        enable_prescreen: bool = True,
        enable_clean_output: bool = True,
        is_draft: bool = False,
    ) -> Dict[str, Any]:
        started = time.time()
        rt = self.config_loader.resolve_provider_runtime(provider, overrides)
        prov = rt["provider"]
        model_name = rt["model"]

        base_meta = {
            "timestamp": started,
            "post_id": post_id,
            "provider": prov,
            "endpoint": rt["endpoint"] or "https://generativelanguage.googleapis.com",
            "model": model_name,
            "is_draft": bool(is_draft),
            "params": {
                "temperature": rt["temperature"],
                "top_p": rt["top_p"],
                "top_k": rt["top_k"],
                "max_output_tokens": rt["max_output_tokens"],
                "timeout": rt["request_timeout"],
            },
        }

        if not (text or "").strip():
            err = "Nội dung bài viết gốc đang rỗng!"
            self.logs.error("REWRITE", err)
            return {**base_meta, "status": "error", "error": err, "latency_ms": 0}

        # Resolve prompt first so even rejected items keep variant_id, angle & exact template
        rendered_prompt, system_prompt, used_vid, used_group, angle, prompt_template = self._resolve_prompt(
            text=text,
            source=source,
            group_pref=group,
            variant_id=variant_id,
            custom_prompt_template=custom_prompt_template,
            custom_system_prompt=custom_system_prompt,
        )

        # 1. Pre-screen sensitive keywords
        prescreen_matches = find_sensitive_matches(text)
        if enable_prescreen and prescreen_matches:
            reason = f"Pre-screen chặn sớm do khớp từ khóa nhạy cảm: {', '.join(prescreen_matches)}"
            self.logs.warning("REWRITE", f"[{used_vid}] REJECTED — {reason}")
            return {
                **base_meta,
                "status": "rejected",
                "output_text": "[REJECT]",
                "raw_output": "",
                "cleaned_output": "[REJECT]",
                "reject_reason": reason,
                "prescreen_matches": prescreen_matches,
                "variant_id": used_vid,
                "group": used_group,
                "angle": angle,
                "prompt_template": prompt_template,
                "rendered_prompt": rendered_prompt,
                "system_prompt": system_prompt,
                "latency_ms": int((time.time() - started) * 1000),
                "usage": None,
                "finish_reason": "safety_prescreen",
                "audit": audit_rewrite(text, "[REJECT]", "", group=used_group),
                "error": "",
            }

        if not rt["api_keys"]:
            err = f"Provider '{prov}' không có API key hợp lệ trong gemini_config.yaml."
            self.logs.error("REWRITE", f"[{used_vid}] {err}")
            return {
                **base_meta,
                "status": "error",
                "variant_id": used_vid,
                "group": used_group,
                "angle": angle,
                "prompt_template": prompt_template,
                "rendered_prompt": rendered_prompt,
                "system_prompt": system_prompt,
                "prescreen_matches": prescreen_matches,
                "error": err,
                "latency_ms": int((time.time() - started) * 1000),
            }

        if not model_name:
            err = f"Provider '{prov}' thiếu tên model!"
            self.logs.error("REWRITE", err)
            return {
                **base_meta,
                "status": "error",
                "variant_id": used_vid,
                "group": used_group,
                "angle": angle,
                "prompt_template": prompt_template,
                "rendered_prompt": rendered_prompt,
                "system_prompt": system_prompt,
                "prescreen_matches": prescreen_matches,
                "error": err,
                "latency_ms": int((time.time() - started) * 1000),
            }

        self.logs.info(
            "REWRITE",
            f"[{used_vid}{'*nháp' if is_draft else ''}] {prov}/{model_name} | T={rt['temperature']} P={rt['top_p']}",
        )

        try:
            if prov == "gemini_proxy":
                raw_text, finish_reason, usage, key_used_info = self._call_proxy(
                    rt=rt,
                    rendered_prompt=rendered_prompt,
                    system_prompt=system_prompt,
                )
            else:
                raw_text, finish_reason, usage, key_used_info = self._call_google_genai(
                    rt=rt,
                    rendered_prompt=rendered_prompt,
                    system_prompt=system_prompt,
                )
        except Exception as e:
            latency_ms = int((time.time() - started) * 1000)
            tb = traceback.format_exc()
            self.logs.error(
                "REWRITE",
                f"[{used_vid}] Lỗi {prov}/{model_name} ({latency_ms}ms): {e}",
                details=tb,
            )
            return {
                **base_meta,
                "status": "error",
                "variant_id": used_vid,
                "group": used_group,
                "angle": angle,
                "prompt_template": prompt_template,
                "rendered_prompt": rendered_prompt,
                "system_prompt": system_prompt,
                "prescreen_matches": prescreen_matches,
                "output_text": "",
                "raw_output": "",
                "cleaned_output": "",
                "error": str(e),
                "traceback": tb,
                "latency_ms": latency_ms,
                "usage": None,
                "finish_reason": "error",
            }

        latency_ms = int((time.time() - started) * 1000)
        cleaned_text, was_rejected, reject_reason = clean_output_text(raw_text)
        final_text = cleaned_text if enable_clean_output else raw_text

        status = "rejected" if (was_rejected and enable_clean_output) else "ok"
        audit = audit_rewrite(text, final_text, raw_text, group=used_group)

        if status == "rejected":
            self.logs.warning("REWRITE", f"[{used_vid}] Model từ chối ({latency_ms}ms): {reject_reason}")
        else:
            self.logs.success(
                "REWRITE",
                f"[{used_vid}] Xong ({latency_ms}ms) · {audit['word_count']} từ · {audit['score']}/100đ",
            )

        return {
            **base_meta,
            "status": status,
            "output_text": final_text,
            "raw_output": raw_text,
            "cleaned_output": cleaned_text,
            "reject_reason": reject_reason or "",
            "prescreen_matches": prescreen_matches,
            "variant_id": used_vid,
            "group": used_group,
            "angle": angle,
            "prompt_template": prompt_template,
            "rendered_prompt": rendered_prompt,
            "system_prompt": system_prompt,
            "key_used": key_used_info,
            "latency_ms": latency_ms,
            "usage": usage,
            "finish_reason": finish_reason,
            "audit": audit,
            "error": "",
            "traceback": "",
        }

    def _call_proxy(
        self,
        rt: Dict[str, Any],
        rendered_prompt: str,
        system_prompt: str,
    ) -> Tuple[str, str, Optional[dict], str]:
        if not rt["endpoint"]:
            raise ValueError("gemini_proxy thiếu endpoint trong cấu hình!")

        api_key = rt["api_keys"][0]
        client = OpenAI(
            base_url=rt["endpoint"],
            api_key=api_key,
            timeout=rt["request_timeout"],
            max_retries=0,
        )
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": rendered_prompt})

        max_attempts = rt["retry_attempts"]
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = client.chat.completions.create(
                    model=rt["model"],
                    messages=messages,
                    temperature=rt["temperature"],
                    top_p=rt["top_p"],
                    max_tokens=rt["max_output_tokens"],
                )
                choice = resp.choices[0] if resp.choices else None
                content = (choice.message.content if choice and choice.message else "") or ""
                finish_reason = (choice.finish_reason if choice else "") or "stop"
                usage = None
                if getattr(resp, "usage", None):
                    usage = {
                        "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                        "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                        "total_tokens": getattr(resp.usage, "total_tokens", None),
                    }
                if not content.strip():
                    raise RuntimeError(f"Proxy trả về nội dung rỗng (finish_reason={finish_reason})")
                return content, finish_reason, usage, f"proxy ({mask_secret(api_key)})"
            except Exception as e:
                last_exc = e
                if attempt < max_attempts:
                    delay = rt["retry_delay"]
                    self.logs.warning(
                        "REWRITE",
                        f"[gemini_proxy] Lần {attempt}/{max_attempts} lỗi: {e} — retry sau {delay}s",
                    )
                    time.sleep(delay)
                else:
                    raise last_exc
        raise RuntimeError("Proxy call failed")

    def _call_google_genai(
        self,
        rt: Dict[str, Any],
        rendered_prompt: str,
        system_prompt: str,
    ) -> Tuple[str, str, Optional[dict], str]:
        provider = rt["provider"]
        api_keys = rt["api_keys"]
        labels = rt["key_labels"]

        if rt["selected_key_index"] is not None:
            candidate_indices = [rt["selected_key_index"]]
        else:
            start = self._next_rr_key_index(provider, len(api_keys))
            max_keys_try = min(len(api_keys), 8)
            candidate_indices = [(start + i) % len(api_keys) for i in range(max_keys_try)]

        effective_prompt = rendered_prompt
        use_sys_instruction = None
        if system_prompt:
            if provider == "gemma":
                effective_prompt = f"[SYSTEM INSTRUCTION]\n{system_prompt}\n\n{rendered_prompt}"
            else:
                use_sys_instruction = system_prompt

        last_exc: Optional[Exception] = None
        for pos, key_idx in enumerate(candidate_indices):
            api_key = api_keys[key_idx]
            label = labels.get(api_key, f"Key #{key_idx + 1}")
            key_display = f"#{key_idx + 1} {label} ({mask_secret(api_key)})"

            try:
                client = new_genai.Client(
                    api_key=api_key,
                    http_options={"timeout": int(rt["request_timeout"] * 1000)},
                )
                cfg_kwargs: Dict[str, Any] = {
                    "temperature": rt["temperature"],
                    "top_p": rt["top_p"],
                    "top_k": rt["top_k"],
                    "max_output_tokens": rt["max_output_tokens"],
                    "response_mime_type": "text/plain",
                }
                if use_sys_instruction:
                    cfg_kwargs["system_instruction"] = use_sys_instruction

                resp = client.models.generate_content(
                    model=rt["model"],
                    contents=effective_prompt,
                    config=new_genai_types.GenerateContentConfig(**cfg_kwargs),
                )
                content = getattr(resp, "text", None) or ""
                finish_reason = "stop"
                if getattr(resp, "candidates", None):
                    fr = getattr(resp.candidates[0], "finish_reason", None)
                    if fr is not None:
                        finish_reason = str(fr)
                usage = None
                um = getattr(resp, "usage_metadata", None)
                if um:
                    usage = {
                        "prompt_tokens": getattr(um, "prompt_token_count", None),
                        "completion_tokens": getattr(um, "candidates_token_count", None),
                        "total_tokens": getattr(um, "total_token_count", None),
                    }
                if not content.strip():
                    raise RuntimeError(f"Model trả về nội dung rỗng (finish_reason={finish_reason})")
                return content, finish_reason, usage, key_display
            except Exception as e:
                last_exc = e
                msg_str = str(e)
                msg_low = msg_str.lower()
                is_retryable_key_err = (
                    "429" in msg_str
                    or "403" in msg_str
                    or "quota" in msg_low
                    or "resource_exhausted" in msg_low
                    or "rate" in msg_low
                    or "permission_denied" in msg_low
                    or "leaked" in msg_low
                    or "api_key_invalid" in msg_low
                    or "api key not valid" in msg_low
                )
                if is_retryable_key_err and pos + 1 < len(candidate_indices):
                    self.logs.warning(
                        "REWRITE",
                        f"[{provider}] {key_display} lỗi key/quota ({msg_str[:85]}), chuyển key kế tiếp...",
                    )
                    continue
                raise last_exc
        raise RuntimeError(f"Tất cả key thử cho {provider} đều thất bại: {last_exc}")
