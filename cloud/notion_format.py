"""Render a Notion page as a Markdown reference without rewriting the source."""

from html import unescape
from mimetypes import guess_extension
from pathlib import PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from bridge import BridgeError, digest, page_id


CODE = re.compile(r"(?ms)(^[ 	]*(`{3,}|~{3,})[^\n]*\n.*?^[ 	]*\2[ \t]*(?=\n|$)|(`+)[^\n]*?\3)")
PAGE = re.compile(r'<page\b[^>]*\burl="([^"]+)"[^>]*>(.*?)</page>', re.S)
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
HOSTED = re.compile(r"(?:prod-files-secure\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com|file\.notion\.so)\Z")
MEDIA = re.compile(r'<(image|file|pdf|audio|video|bookmark|embed)\b([^>]*?)(?:\s*/>|>(.*?)</\1>)', re.S)
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_attachment(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not HOSTED.fullmatch(parsed.hostname or "") or parsed.username or parsed.port not in (None, 443):
        raise BridgeError("Unrecognised Notion attachment host")
    with build_opener(NoRedirect).open(url, timeout=12) as response:
        if int(response.headers.get("Content-Length", "0")) > MAX_ATTACHMENT_BYTES:
            raise BridgeError("Notion attachment exceeds the 20 MiB limit")
        data = response.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise BridgeError("Notion attachment exceeds the 20 MiB limit")
        ext = guess_extension(response.headers.get_content_type())
    filename = PurePosixPath(unquote(parsed.path)).name
    stem = re.sub(r"[^\w .-]", "-", PurePosixPath(filename).stem).strip(" .")[:55] or "attachment"
    ext = ext or PurePosixPath(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext or "") or ext in {".md", ".html", ".htm", ".svg"}:
        ext = ".bin"
    return f"{stem}-{digest(parsed.netloc + parsed.path)[:16]}{ext}", data


def child_pages(body):
    for fragment in CODE.split(body)[::4]:
        for match in PAGE.finditer(fragment):
            try:
                yield page_id(unescape(match[1]))
            except BridgeError:
                continue


def source_link(pid):
    return "https://www.notion.so/" + pid.replace("-", "")


def markdown_link(label, url, *, image=False):
    target_url = unescape(url)
    if urlsplit(target_url).scheme not in ("https", "http") or not urlsplit(target_url).hostname:
        return label
    escaped_label = re.sub(r"\s+", " ", label).strip().replace("[", r"\[").replace("]", r"\]")
    return ("!" if image else "") + f"[{escaped_label}](<{target_url.replace('<', '%3C').replace('>', '%3E')}>)"


def render_reference(body, pid, local_title, asset, *, incomplete=False):
    """Protect literal code and render Notion layout as a readable local copy."""
    protected = []

    def hold(text):
        marker = f"\x00NOTION{len(protected)}\x00"
        protected.append(text)
        return marker

    def restore(text):
        for index in range(len(protected) - 1, -1, -1):
            text = text.replace(f"\x00NOTION{index}\x00", protected[index])
        return text

    rendered = CODE.sub(lambda match: hold(match[0]), body)

    def inline_code(match):
        value = unescape(restore(match[1]))
        fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
        pad = " " if value.startswith("`") or value.endswith("`") else ""
        return hold(f"{fence}{pad}{value}{pad}{fence}")

    rendered = re.sub(r"<code>(.*?)</code>", inline_code, rendered, flags=re.S)

    def child(match):
        label = re.sub(r"<[^>]+>", "", unescape(match[2])).strip() or "Child page"
        url = unescape(match[1])
        target = local_title(url)
        if target and not any(char in target for char in "[]|#"):
            return f"\n\n[[{target}]]\n\n"
        return f"\n\n{hold(markdown_link(label, url))}\n\n"

    def media(kind, url, caption):
        target_url = unescape(url)
        label = re.sub(r"<[^>]+>", "", unescape(caption or "")).strip() or kind.capitalize()
        if urlsplit(target_url).scheme == "https" and HOSTED.fullmatch(urlsplit(target_url).hostname or ""):
            path = asset(target_url, kind)
            if path:
                prefix = "!" if kind == "image" else ""
                return hold(f"{prefix}[{label}]({quote(path, safe='/')})")
            return hold(markdown_link(label + " in Notion", source_link(pid)))
        return hold(markdown_link(label, target_url, image=kind == "image"))

    def database(match):
        label = re.sub(r"<[^>]+>", "", match[2]).strip() or "Database"
        return f"\n\n{hold(markdown_link(label, match[1]))}\n\n"

    def media_block(match):
        source = dict(ATTR.findall(match[2])).get("source", "")
        return f"\n\n{media(match[1], source, match[3])}\n\n"

    rendered = PAGE.sub(child, rendered)
    rendered = re.sub(r'<database\b[^>]*\burl="([^"]+)"[^>]*>(.*?)</database>',
                      database, rendered, flags=re.S)
    rendered = MEDIA.sub(media_block, rendered)
    rendered = re.sub(r"!\[([^\]\n]*)\]\(<?(https?://[^\s)>]+)>?\)",
                      lambda m: media("image", m[2], m[1]), rendered)
    for tag, delimiter in (("b", "**"), ("strong", "**"), ("i", "*"), ("em", "*"), ("s", "~~")):
        rendered = re.sub(rf"<{tag}\b[^>]*>(.*?)</{tag}>",
                          lambda m: delimiter + m[1].strip() + delimiter, rendered, flags=re.S)
    rendered = re.sub(r"<h([1-6])\b[^>]*>(.*?)</h\1>",
                      lambda m: f"\n\n{'#' * int(m[1])} {m[2].strip()}\n\n", rendered, flags=re.S)
    rendered = re.sub(r"<(?:toggle|callout)\b[^>]*>\s*([^\n<]+)",
                      lambda m: f"\n\n**{m[1].strip()}**\n\n", rendered)
    rendered = re.sub(r"<li\b[^>]*>\s*", "\n- ", rendered)
    rendered = re.sub(r"<br\s*/?>", "  \n", rendered)
    rendered = re.sub(r"<table-of-contents\b[^>]*/>", "\n\n", rendered)
    rendered = re.sub(r"</?(?:p|ul|ol|li|columns?|toggle|callout|quote|span|u)\b[^>]*>", "\n", rendered)
    rendered = re.sub(r'<(?:unknown|link-preview)\b[^>]*?\burl="([^"]+)"[^>]*/>',
                      lambda m: hold(markdown_link("Open block in Notion", m[1])), rendered)
    # Display unfamiliar markup as text. The linked source retains its semantics.
    rendered = re.sub(r"<[^>\n]+>", lambda m: m[0].replace("<", "&lt;").replace(">", "&gt;"), rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    rendered = restore(rendered)
    notice = " Some blocks could only be represented as links." if incomplete else ""
    return f"> [!info] Notion reference\n> [Open the source in Notion]({source_link(pid)}). Edit the source page.{notice}\n\n{rendered}".rstrip()
