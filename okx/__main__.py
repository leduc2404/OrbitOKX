"""Single CLI entry for the OKX Square pipeline: python -m okx <command>.

Commands:
    post          build/publish a post (dry-run by default; --send to publish live)
    presign       live diagnostic: request one OSS upload slot
    selftest      offline verification of all reversed crypto against the capture/HAR
    sectoken      offline x-sec-token generate + self-decrypt demo
    extract-keys  derive keys.json from okx-capture.json (when the session rotates)
"""
import argparse
import json
import sys
from pathlib import Path

from . import config, selftest
from .client import OkxClient
from .sectoken import generate_sec_token, decrypt_sec_token
from .square import publish


def _print_result(result):
    body = result["body"]
    mode = "DRY-RUN" if result["dry_run"] else "LIVE"
    print(f"=== OKX Square post ({mode}) ===")
    print("content:", repr(result["content"]))
    tags = [t["tag"] for t in body["contentTags"]["cashTagList"]]
    print("tags:", " ".join(tags) if tags else "(none)")
    imgs = body["imageList"]
    print(f"images: {len(imgs)}")
    for i, im in enumerate(imgs, 1):
        print(f"  [{i}] {im['width']}x{im['height']} {im['fileSize']}B -> {im['url']}")
    print("group:", body["group"], "publishId:", body["publishId"])
    print("body:", json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    if result["dry_run"]:
        print("\n[DRY-RUN] not sent. Add --send to publish live.")
    else:
        data = result.get("response", {})
        print("\n=== RESPONSE ===")
        print("contentId:", data.get("contentId"))
        print("data:", json.dumps(data, ensure_ascii=False)[:800])


def cmd_post(args):
    content = args.text
    if args.text_file:
        content = Path(args.text_file).read_text(encoding="utf-8")
    if not content:
        print("error: provide --text or --text-file", file=sys.stderr)
        return 2
    if args.send and args.image:
        print(f"Uploading {len(args.image)} image(s)...", file=sys.stderr)
    result = publish(content, images=args.image, tags=args.tag,
                     group=args.group, dry_run=not args.send)
    _print_result(result)
    return 0


def cmd_presign(args):
    client = OkxClient()
    slot = client.presign()
    print("PRESIGN ok")
    print("imageUrl:", slot["imageUrl"])
    print("presignUrl:", slot["presignUrl"][:90] + "...")
    return 0


def cmd_selftest(args):
    selftest.run()
    return 0


def cmd_extract_keys(args):
    if not config.CAPTURE_FILE.exists():
        print(f"error: missing {config.CAPTURE_FILE.name} (run the capture userscript first)", file=sys.stderr)
        return 2
    config.extract_keys()
    print(f"extracted keys -> {config.KEYS_FILE.name} (ecdsa_pkcs8_b64, hmac_raw_b64)")
    return 0


def cmd_sectoken(args):
    cfg = config.Config()
    tok = generate_sec_token("POST", config.PUBLISH_PATH, cfg.devid)
    print("segment lengths:", [len(s) for s in tok.split(".")])
    meta, fp, rh = decrypt_sec_token(tok)
    print("self-decrypt OK, did match:", meta["did"] == cfg.devid)
    print("request-hash:", rh)
    print("token:", tok[:60] + "...")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="okx", description="OKX Square publishing pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    post = sub.add_parser("post", help="build/publish a post (dry-run by default)")
    post.add_argument("--text", help="post content")
    post.add_argument("--text-file", help="read content from a UTF-8 file")
    post.add_argument("--image", action="append", default=[], metavar="PATH",
                      help="image file (repeatable for multiple images)")
    post.add_argument("--tag", action="append", default=[], metavar="COIN",
                      help="coin tag, e.g. BTC (repeatable for multiple tags)")
    post.add_argument("--group", default="USDT", help="publish group (default USDT)")
    post.add_argument("--send", action="store_true", help="publish live (otherwise dry-run)")
    post.set_defaults(func=cmd_post)

    pre = sub.add_parser("presign", help="live diagnostic: request one OSS upload slot")
    pre.set_defaults(func=cmd_presign)

    st = sub.add_parser("selftest", help="offline verification against capture/HAR")
    st.set_defaults(func=cmd_selftest)

    ek = sub.add_parser("extract-keys", help="derive keys.json from okx-capture.json")
    ek.set_defaults(func=cmd_extract_keys)

    sec = sub.add_parser("sectoken", help="offline x-sec-token demo")
    sec.set_defaults(func=cmd_sectoken)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())