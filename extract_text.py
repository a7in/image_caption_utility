"""Extract embedded text (prompt/caption metadata) from image files.

Public API:
    extract_caption(path) -> str | None

Two storage conventions are recognised:
  * Automatic1111 / SD style — plain text in EXIF ``UserComment``
    (JPEG; UTF-16 with a "UNICODE" prefix). Returned verbatim.
  * ComfyUI — a workflow/prompt graph stored as JSON in EXIF
    (``ImageDescription`` / ``Make`` for WEBP) or in PNG text chunks
    (``prompt`` / ``workflow``). Only the active text nodes are extracted.
"""

import json

from PIL import Image
from PIL.ExifTags import TAGS, IFD


# Input keys that almost always carry actual prompt text.
_CONTENT_KEYS = {
    "text", "prompt", "value", "string", "positive",
    "text_g", "text_l", "wildcard_text", "populated_text",
}

# Minimum length of a "meaningful" string (filters out tokens like key='mu').
_MIN_TEXT_LEN = 20

# Utility node classes: their title may contain "text", but the content is
# service data (json configs, replacement templates), not a prompt.
_UTILITY_CLASSES = {
    "jsonextractstring", "jsonextract", "stringreplace", "previewany",
    "previewtext", "showtext", "string", "stringconcatenate", "note",
    "markdownnote", "comfymathexpression", "customcombo",
}

# Node titles/names to skip entirely: system and negative prompts.
_SKIP_NAME_HINTS = ("system", "negative")


def _decode_user_comment(raw) -> str:
    """Decode an EXIF UserComment value (8-byte encoding prefix + body)."""
    if isinstance(raw, str):
        return raw
    if raw[:8] == b"UNICODE\x00":
        body = raw[8:]
        # Automatic1111 writes UTF-16 big-endian; fall back to other variants.
        for enc in ("utf-16-be", "utf-16", "utf-16-le"):
            try:
                return body.decode(enc).rstrip("\x00")
            except UnicodeDecodeError:
                continue
    if raw[:8] == b"ASCII\x00\x00\x00":
        return raw[8:].decode("ascii", "replace").rstrip("\x00")
    return raw.decode("utf-8", "replace").rstrip("\x00")


def _collect_raw_fields(path: str) -> dict[str, str]:
    """Gather every text-bearing field from an image into {name: text}."""
    fields: dict[str, str] = {}
    img = Image.open(path)

    # PNG text chunks and similar entries land in img.info.
    for key, val in img.info.items():
        if isinstance(val, str) and val.strip():
            fields[f"info:{key}"] = val

    # Top-level EXIF (ImageDescription, Make, ...).
    exif = img.getexif()
    for tag, val in exif.items():
        name = TAGS.get(tag, str(tag))
        if isinstance(val, (str, bytes)) and val:
            fields[f"exif:{name}"] = (
                val.decode("utf-8", "replace") if isinstance(val, bytes) else val
            )

    # Exif sub-IFD holds the UserComment.
    try:
        sub = exif.get_ifd(IFD.Exif)
    except Exception:
        sub = {}
    for tag, val in sub.items():
        name = TAGS.get(tag, str(tag))
        if name == "UserComment" and val:
            fields["exif:UserComment"] = _decode_user_comment(val)
        elif isinstance(val, str) and val.strip():
            fields[f"exif:{name}"] = val

    return fields


def _load_comfy_prompt(raw: str):
    """Parse the ComfyUI API prompt format from a raw field value.

    The value may carry a "Prompt: " / "Workflow: " prefix, which is stripped.
    Returns the node dict (node_id -> {class_type, inputs, _meta}) or None.
    """
    text = raw.strip()
    for prefix in ("Prompt:", "Workflow:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    # API format: every value is a node carrying 'class_type' and 'inputs'.
    if isinstance(data, dict) and any(
        isinstance(v, dict) and "class_type" in v and "inputs" in v
        for v in data.values()
    ):
        return data
    return None


def _extract_comfy_texts(prompt: dict) -> list[str]:
    """Return prompt text from the active text nodes of a ComfyUI prompt.

    The API prompt format already contains only active, executable nodes
    (notes and muted/bypassed nodes are absent). Of those, keep nodes whose
    class/title mentions "text" or "prompt", drop utility and system/negative
    nodes, and take the meaningful string content of their inputs.
    """
    texts: list[str] = []
    seen: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        cls = node.get("class_type", "")
        if cls.lower() in _UTILITY_CLASSES:
            continue
        title = (node.get("_meta") or {}).get("title", "")
        name = f"{cls} {title}".lower()
        if "text" not in name and "prompt" not in name:
            continue
        if any(hint in name for hint in _SKIP_NAME_HINTS):
            continue  # system / negative prompts are not wanted
        for key, val in (node.get("inputs") or {}).items():
            if not isinstance(val, str):
                continue  # a link to another node (list) or a number
            stripped = val.strip()
            if not stripped or stripped in seen:
                continue
            is_content = (
                key.lower() in _CONTENT_KEYS
                or "\n" in stripped
                or len(stripped) >= _MIN_TEXT_LEN
            )
            if is_content:
                seen.add(stripped)
                texts.append(stripped)
    return texts


def extract_caption(path: str) -> str | None:
    """Extract embedded prompt/caption text from an image.

    Returns the text, or None if the image carries no recognised text.
      * EXIF UserComment is returned verbatim.
      * A ComfyUI workflow yields the joined text of its active text nodes.
    """
    try:
        fields = _collect_raw_fields(path)
    except Exception:
        return None

    # 1) UserComment is taken as-is.
    user_comment = fields.get("exif:UserComment", "").strip()
    if user_comment:
        return user_comment

    # 2) Otherwise look for a ComfyUI prompt in any field.
    texts: list[str] = []
    seen: set[str] = set()
    for value in fields.values():
        prompt = _load_comfy_prompt(value)
        if not prompt:
            continue
        for text in _extract_comfy_texts(prompt):
            if text not in seen:
                seen.add(text)
                texts.append(text)

    return "\n\n".join(texts) if texts else None
