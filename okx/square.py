"""High-level OKX Square publishing pipeline.

One call orchestrates the whole flow:
    normalize coin tags -> ensure $TAGs appear in the text -> presign+PUT each
    image -> assemble the publish body -> POST ugc/publish.

Evidence behind the payload shape (from captured HAR + live feed):
  * imageList: one {url,width,height,fileSize} per image; url is the OSS key
    returned by presign (imageUrl). Multiple images => multiple presign/PUT calls.
  * contentTags.cashTagList: one {tag,coinName,position} per coin tag. Real feed
    posts use position:[1] for every tag; the server RECOMPUTES position and
    enriches each tag with instId/instType/deeplinkUrl, so [1] is a safe default.
  * The $TAG text must appear in `content` (feed posts embed tags inline). Missing
    tags are appended on a new line, matching the captured single-tag publish.
"""
import json
import uuid
from pathlib import Path

from . import config
from .client import OkxClient, image_size, _okx_json


def normalize_tags(tags):
    """'$btc' / 'BTC' / 'btc' -> 'BTC'; dedup, preserve order."""
    out = []
    for t in tags:
        n = t.strip().lstrip("$").upper()
        if n and n not in out:
            out.append(n)
    return out


def ensure_tags_in_content(content, tag_names):
    """Append any $TAG not already present in the text (inline tags are kept)."""
    missing = [n for n in tag_names if f"${n}" not in content]
    if missing:
        content = content.rstrip() + "\n" + " ".join(f"${n}" for n in missing)
    return content


def build_cash_tag_list(tag_names):
    return [{"tag": f"${n}", "coinName": n, "position": [1]} for n in tag_names]


def build_publish_body(content, image_list, tag_names, group="USDT"):
    """Pure body assembler (content is assumed already tag-augmented)."""
    return {
        "content": content,
        "imageList": image_list,
        "contentTags": {"cashTagList": build_cash_tag_list(tag_names)},
        "group": group,
        "publishId": str(uuid.uuid4()),
    }


def describe_image(path):
    """Local image metadata for dry-run previews (no network)."""
    data = Path(path).read_bytes()
    w, h = image_size(data)
    return {"url": f"<presign:{Path(path).name}>", "width": str(w),
            "height": str(h), "fileSize": str(len(data))}


def publish(content, images=(), tags=(), group="USDT", dry_run=True, client=None):
    """Run the full pipeline. Returns {dry_run, content, body, response?}.

    dry_run=True (default): build the exact payload, read image dimensions locally,
    but do NOT presign/upload/publish. Pass dry_run=False to send live.
    """
    tag_names = normalize_tags(tags)
    content = ensure_tags_in_content(content, tag_names)

    image_list = []
    if images and not dry_run and client is None:
        client = OkxClient()
    for p in images:
        image_list.append(describe_image(p) if dry_run else client.upload_image(p))

    body = build_publish_body(content, image_list, tag_names, group)
    result = {"dry_run": dry_run, "content": content, "body": body}
    if dry_run:
        return result

    client = client or OkxClient()
    body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    resp = client.signed_request("POST", config.PUBLISH_PATH, body=body_str, publish=True)
    result["response"] = _okx_json(resp)
    return result