#!/usr/bin/env python3
"""
Foxbravo PDF Edit - a small, free desktop PDF editor.

Features
  * Edit existing text (click a line and type) and find & replace across the document
  * Highlight, freehand drawing, text boxes, sticky notes
  * Signatures (draw or load an image, optionally remembered)
  * Fill in PDF forms
  * Merge / insert other PDFs, extract pages, split into single pages
  * Rotate, move and delete pages
  * Undo / redo

Built with Tkinter (GUI) and PyMuPDF (PDF engine).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageDraw

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

APP_NAME = "Foxbravo PDF Edit"
APP_VERSION = "1.0.0"

# Text extraction flags: expand ligatures (fi, fl...) into normal letters so they can be edited.
TEXT_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES


def app_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "FoxbravoPDFEdit")
    os.makedirs(path, exist_ok=True)
    return path


class EditError(Exception):
    """Raised when a text edit cannot be done safely."""


# =============================================================================
# Text runs: pieces of text on one line that share the same style
# =============================================================================

@dataclass
class Char:
    c: str
    rect: pymupdf.Rect
    x: float          # x of the glyph origin (start of the letter on the baseline)
    real: bool = True  # False for spaces we add between pieces that had a visual gap


@dataclass
class TextRun:
    chars: list
    origin: pymupdf.Point  # baseline start
    size: float
    font: str
    flags: int
    color: int

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)

    @property
    def key(self):
        return (self.font, round(self.size, 2), self.color, self.flags)

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.chars[0].rect)
        for ch in self.chars[1:]:
            r |= ch.rect
        return r


def get_text_runs(page: pymupdf.Page) -> list:
    """Return horizontal text runs on the page, in unrotated page coordinates."""
    runs = []
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    for block in raw["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block["lines"]:
            dx, dy = line["dir"]
            if abs(dx - 1) > 0.01 or abs(dy) > 0.01:
                continue  # only horizontal, left-to-right text is editable
            cur = None
            for span in line["spans"]:
                chars = [Char(ch["c"], pymupdf.Rect(ch["bbox"]), ch["origin"][0]) for ch in span["chars"]]
                if not chars:
                    continue
                size = span["size"]
                key = (span["font"], round(size, 2), span["color"], span["flags"])
                oy = span["origin"][1]
                if cur is not None and cur.key == key and abs(cur.origin.y - oy) < 0.5:
                    gap = chars[0].rect.x0 - cur.chars[-1].rect.x1
                    if -0.5 * size <= gap <= 0.8 * size:
                        if gap > 0.15 * size and not cur.chars[-1].c.isspace() and not chars[0].c.isspace():
                            prev = cur.chars[-1].rect
                            cur.chars.append(Char(" ", pymupdf.Rect(prev.x1, prev.y0, chars[0].rect.x0, prev.y1),
                                                  prev.x1, real=False))
                        cur.chars.extend(chars)
                        continue
                cur = TextRun(chars, pymupdf.Point(span["origin"]), size, span["font"], span["flags"], span["color"])
                runs.append(cur)
    return [r for r in runs if r.text.strip()]


def run_at(runs: list, point: pymupdf.Point):
    """The text run under a point (closest to the line's middle when lines overlap)."""
    best, best_d = None, None
    for run in runs:
        r = run.bbox
        if pymupdf.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1).contains(point):
            d = abs(point.y - (r.y0 + r.y1) / 2)
            if best is None or d < best_d:
                best, best_d = run, d
    return best


def count_visible_chars(page: pymupdf.Page) -> int:
    n = 0
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                n += sum(1 for ch in span["chars"] if not ch["c"].isspace())
    return n


# =============================================================================
# Fonts
# =============================================================================

def normalize_font_name(name: str) -> str:
    n = name.split("+")[-1]
    n = re.sub(r"(PS)?MT$", "", n)
    n = re.sub(r"PS(?=[-,])", "", n)
    n = re.sub(r"[^a-z0-9]", "", n.lower())
    n = re.sub(r"(regular|book|normal)$", "", n)
    return n


def classify_font(name: str, flags: int):
    """-> (family, bold, italic) where family is 'sans', 'serif' or 'mono'."""
    n = name.split("+")[-1].lower()
    bold = bool(flags & 16) or any(k in n for k in ("bold", "black", "heavy", "semibold", "demi", "cmbx"))
    italic = bool(flags & 2) or any(k in n for k in ("italic", "oblique", "cmti"))
    if flags & 8 or any(k in n for k in ("mono", "courier", "consol", "code", "cmtt")):
        family = "mono"
    elif "sans" not in n and (flags & 4 or any(k in n for k in (
            "times", "serif", "georgia", "garamond", "cambria", "minion", "palatino", "book", "cmr", "cmbx", "cmti"))):
        family = "serif"
    else:
        family = "sans"
    return family, bold, italic


BASE14 = {"sans": ("helv", "hebo", "heit", "hebi"),
          "serif": ("tiro", "tibo", "tiit", "tibi"),
          "mono": ("cour", "cobo", "coit", "cobi")}
BASE14_LABEL = {"sans": "Helvetica", "serif": "Times", "mono": "Courier"}
GENERIC_NAMES = {"sans": ("arial", "liberationsans", "dejavusans", "helvetica"),
                 "serif": ("timesnewroman", "liberationserif", "dejavuserif", "times"),
                 "mono": ("couriernew", "liberationmono", "dejavusansmono", "courier")}
STYLE_SUFFIX = ("", "bold", "italic", "bolditalic")


def style_index(bold: bool, italic: bool) -> int:
    return (1 if bold else 0) + (2 if italic else 0)


def font_dirs() -> list:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        return [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", home), "Microsoft", "Windows", "Fonts")]
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.join(home, ".local", "share", "fonts"), os.path.join(home, ".fonts")]


class SystemFontIndex:
    """Maps normalized font names to installed font files. Built once, cached on disk."""

    def __init__(self):
        self._index = None

    def _build(self):
        cache_path = os.path.join(app_data_dir(), "font-index.json")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
        new_cache, index = {}, {}
        for d in font_dirs():
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if not fn.lower().endswith((".ttf", ".otf")):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    entry = cache.get(path)
                    if entry and entry[0] == mtime:
                        norm = entry[1]
                    else:
                        try:
                            norm = normalize_font_name(pymupdf.Font(fontfile=path).name)
                        except Exception:
                            norm = ""
                    new_cache[path] = [mtime, norm]
                    if norm and norm not in index:
                        index[norm] = path
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(new_cache, f)
        except Exception:
            pass
        self._index = index

    def find(self, normalized_name: str):
        if self._index is None:
            self._build()
        return self._index.get(normalized_name)


FONT_INDEX = SystemFontIndex()


class FontChoice:
    kind = ""
    label = ""

    def covers(self, text: str) -> bool:
        raise NotImplementedError

    def width(self, text: str, size: float) -> float:
        raise NotImplementedError

    def write(self, page, point, text, size, color, hscale=100.0):
        raise NotImplementedError


class FileFont(FontChoice):
    """A font file installed on this computer, embedded into the PDF."""

    def __init__(self, path: str, kind: str, label: str):
        self.path, self.kind, self.label = path, kind, label
        self.font = pymupdf.Font(fontfile=path)
        self.resname = "FBS" + hashlib.md5(path.encode("utf-8")).hexdigest()[:8]

    def covers(self, text):
        return all(ch.isspace() or self.font.has_glyph(ord(ch)) for ch in text)

    def width(self, text, size):
        return self.font.text_length(text, fontsize=size)

    def write(self, page, point, text, size, color, hscale=100.0):
        page.insert_font(fontname=self.resname, fontfile=self.path)
        morph = (point, pymupdf.Matrix(hscale / 100.0, 1)) if hscale < 99.9 else None
        page.insert_text(point, text, fontsize=size, fontname=self.resname, color=color, morph=morph)


class Base14Font(FontChoice):
    """One of the standard PDF fonts (Helvetica, Times, Courier). No embedding needed."""

    def __init__(self, code: str, label: str, kind="standard"):
        self.code, self.label, self.kind = code, label, kind

    def covers(self, text):
        try:
            text.encode("cp1252")
            return True
        except UnicodeEncodeError:
            return False

    def width(self, text, size):
        return pymupdf.get_text_length(text, fontname=self.code, fontsize=size)

    def write(self, page, point, text, size, color, hscale=100.0):
        morph = (point, pymupdf.Matrix(hscale / 100.0, 1)) if hscale < 99.9 else None
        page.insert_text(point, text, fontsize=size, fontname=self.code, color=color, morph=morph)


def _resolve(doc, value: str) -> str:
    """Return the PDF source of an object value, following one indirect reference."""
    m = re.fullmatch(r"\s*(\d+)\s+0\s+R\s*", value or "")
    if m:
        return doc.xref_object(int(m.group(1)), compressed=True)
    return value or ""


def _get(doc, xref, key):
    t, v = doc.xref_get_key(xref, key)
    if t == "null":
        return None
    if t == "xref":
        return _resolve(doc, v)
    return v


def _numbers_and_brackets(s: str) -> list:
    return re.findall(r"\[|\]|-?\d+(?:\.\d+)?|-?\.\d+", s or "")


def _hex_to_str(h: str) -> str:
    if len(h) % 4:
        h = h.rjust((len(h) + 3) // 4 * 4, "0")
    try:
        return bytes.fromhex(h).decode("utf-16-be")
    except Exception:
        return ""


def parse_tounicode(data: bytes) -> dict:
    text = data.decode("latin-1")
    out = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = _hex_to_str(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            lo, hi, dst = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
            if hi < lo or hi - lo > 65535:
                continue
            if dst.startswith("["):
                for i, h in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    if lo + i <= hi:
                        out[lo + i] = _hex_to_str(h)
            else:
                h = dst[1:-1]
                if not h:
                    continue
                base = int(h, 16)
                for i in range(hi - lo + 1):
                    out[lo + i] = _hex_to_str(format(base + i, "0%dx" % len(h)))
    return out


class OriginalFont(FontChoice):
    """Reuse the font that is already embedded in the PDF, writing raw character codes.

    Only letters that already appear somewhere in the document with this font are used,
    because embedded fonts usually contain just those letters.
    """
    kind = "original"

    def __init__(self, doc, font_xref, two_byte, uni2code, widths, default_width, observed, basefont):
        self.doc, self.xref, self.two_byte = doc, font_xref, two_byte
        self.uni2code, self.widths, self.default_width = uni2code, widths, default_width
        self.observed = observed
        self.label = "original font (%s)" % basefont.split("+")[-1]

    def covers(self, text):
        for ch in text:
            if ch == " ":
                continue  # spaces can always be emulated with positioning
            if ch not in self.uni2code or ch not in self.observed:
                return False
        return True

    def _w(self, ch):
        if ch in self.uni2code:
            return self.widths.get(self.uni2code[ch], self.default_width)
        return 250  # emulated space

    def width(self, text, size):
        return sum(self._w(ch) for ch in text) * size / 1000.0

    def _resource_name(self, page):
        doc = self.doc
        name = "FBO%d" % self.xref
        t, v = doc.xref_get_key(page.xref, "Resources")
        if t == "xref":
            holder, prefix = int(v.split()[0]), ""
        elif t == "dict":
            holder, prefix = page.xref, "Resources/"
        else:
            raise EditError("page resources are inherited")
        ft, fv = doc.xref_get_key(holder, prefix + "Font")
        if ft == "xref":
            doc.xref_set_key(int(fv.split()[0]), name, "%d 0 R" % self.xref)
        elif ft in ("dict", "null"):
            doc.xref_set_key(holder, prefix + "Font/" + name, "%d 0 R" % self.xref)
        else:
            raise EditError("unexpected font resources")
        return name

    def write(self, page, point, text, size, color, hscale=100.0):
        name = self._resource_name(page)
        fmt = "%04X" if self.two_byte else "%02X"
        parts, cur = [], ""
        for ch in text:
            if ch in self.uni2code:
                cur += fmt % self.uni2code[ch]
            else:  # a space the font does not have: move right by 0.25 em
                if cur:
                    parts.append("<%s>" % cur)
                    cur = ""
                parts.append("-250")
        if cur:
            parts.append("<%s>" % cur)
        p = pymupdf.Point(point) * ~page.transformation_matrix
        r, g, b = color

        def f(v):
            return ("%.4f" % v).rstrip("0").rstrip(".") or "0"

        stream = ("q BT /%s %s Tf %s %s %s rg 0 Tc 0 Tw %s Tz 0 Tr 0 Ts 1 0 0 1 %s %s Tm [%s] TJ ET Q\n"
                  % (name, f(size), f(r), f(g), f(b), f(hscale), f(p.x), f(p.y), " ".join(parts)))
        append_content(page, stream.encode("latin-1"))


def append_content(page, data: bytes):
    doc = page.parent
    page.wrap_contents()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    t, v = doc.xref_get_key(page.xref, "Contents")
    if t == "array":
        items = v.strip()[1:-1]
    elif t == "xref":
        items = v
    else:
        items = ""
    doc.xref_set_key(page.xref, "Contents", "[%s %d 0 R]" % (items, xref))


class FontResolver:
    """Picks the best font for writing new text in the style of an existing text run."""

    def __init__(self, doc):
        self.doc = doc
        self._orig = {}
        self._observed = {}

    def reset(self, doc):
        self.doc = doc
        self._orig.clear()
        self._observed.clear()

    def _font_xref(self, page, fontname):
        for x in page.get_fonts(full=True):
            if x[3].split("+")[-1] == fontname:
                return x[0], x[3]
        return None, None

    def _observed_chars(self, xref, fontname):
        key = (xref, fontname)
        if key not in self._observed:
            chars = set()
            for pno in range(self.doc.page_count):
                if not any(f[0] == xref for f in self.doc.get_page_fonts(pno, full=True)):
                    continue
                d = self.doc[pno].get_text("dict", flags=TEXT_FLAGS)
                for b in d["blocks"]:
                    for line in b.get("lines", []):
                        for span in line["spans"]:
                            if span["font"] == fontname:
                                chars.update(span["text"])
            self._observed[key] = chars
        return self._observed[key]

    def _load_original(self, page, fontname):
        xref, basefont = self._font_xref(page, fontname)
        if not xref:
            return None
        if xref in self._orig:
            return self._orig[xref]
        result = None
        try:
            result = self._build_original(xref, basefont, fontname)
        except Exception:
            result = None
        self._orig[xref] = result
        return result

    def _build_original(self, xref, basefont, fontname):
        doc = self.doc
        subtype = _get(doc, xref, "Subtype") or ""
        tu = doc.xref_get_key(xref, "ToUnicode")
        code2uni = {}
        if tu[0] == "xref":
            code2uni = parse_tounicode(doc.xref_stream(int(tu[1].split()[0])) or b"")
        two_byte = subtype == "/Type0"
        widths, default_width = {}, 1000
        if two_byte:
            if (_get(doc, xref, "Encoding") or "") != "/Identity-H" or not code2uni:
                return None
            desc = _numbers_and_brackets(_get(doc, xref, "DescendantFonts"))
            m = re.search(r"(\d+)\s+0\s+R", _get(doc, xref, "DescendantFonts") or "")
            if not m:
                return None
            dxref = int(m.group(1))
            dw = _get(doc, dxref, "DW")
            default_width = float(dw) if dw else 1000.0
            toks = _numbers_and_brackets(_get(doc, dxref, "W"))
            i = 0
            while i < len(toks):
                if toks[i] in "[]":
                    i += 1
                    continue
                first = int(float(toks[i]))
                if i + 1 < len(toks) and toks[i + 1] == "[":
                    j = i + 2
                    code = first
                    while j < len(toks) and toks[j] != "]":
                        widths[code] = float(toks[j])
                        code += 1
                        j += 1
                    i = j + 1
                elif i + 2 < len(toks):
                    last, w = int(float(toks[i + 1])), float(toks[i + 2])
                    for code in range(first, min(last, first + 65535) + 1):
                        widths[code] = w
                    i += 3
                else:
                    break
        else:
            if subtype == "/Type3":
                return None
            fc = _get(doc, xref, "FirstChar")
            ws = [t for t in _numbers_and_brackets(_get(doc, xref, "Widths")) if t not in "[]"]
            if fc is not None:
                for k, w in enumerate(ws):
                    widths[int(float(fc)) + k] = float(w)
            if not code2uni:
                code2uni = self._simple_encoding(xref)
            if not code2uni:
                return None
        uni2code = {}
        for code, s in sorted(code2uni.items()):
            if len(s) == 1 and s not in uni2code:
                uni2code[s] = code
        observed = self._observed_chars(xref, fontname)
        return OriginalFont(doc, xref, two_byte, uni2code, widths, default_width, observed, basefont)

    def _simple_encoding(self, xref):
        doc = self.doc
        enc = _get(doc, xref, "Encoding")
        if not enc:
            return {}
        table = {}
        if enc == "/WinAnsiEncoding" or "/WinAnsiEncoding" in enc or "/BaseEncoding" not in enc:
            for code in range(32, 256):
                try:
                    table[code] = bytes([code]).decode("cp1252")
                except UnicodeDecodeError:
                    pass
        if enc.startswith("/") and enc != "/WinAnsiEncoding":
            return {}
        m = re.search(r"/Differences\s*\[(.*?)\]", enc, re.S)
        if m:
            code = None
            for tok in re.findall(r"/[^\s/\[\]]+|\d+", m.group(1)):
                if tok.isdigit():
                    code = int(tok)
                elif code is not None:
                    u = pymupdf.glyph_name_to_unicode(tok[1:])
                    if u and u != 0xFFFD:
                        table[code] = chr(u)
                    code += 1
        return table

    def choose(self, page, run: TextRun, text: str) -> FontChoice:
        orig = self._load_original(page, run.font)
        if orig is not None and orig.covers(text):
            return orig
        family, bold, italic = classify_font(run.font, run.flags)
        path = FONT_INDEX.find(normalize_font_name(run.font))
        if path:
            try:
                ff = FileFont(path, "system", "installed font (%s)" % os.path.basename(path))
                if ff.covers(text):
                    return ff
            except Exception:
                pass
        si = style_index(bold, italic)
        b14 = Base14Font(BASE14[family][si], "standard font (%s)" % BASE14_LABEL[family])
        if b14.covers(text):
            return b14
        for base in GENERIC_NAMES[family]:
            path = FONT_INDEX.find(base + STYLE_SUFFIX[si]) or FONT_INDEX.find(base)
            if path:
                try:
                    ff = FileFont(path, "fallback", "fallback font (%s)" % os.path.basename(path))
                    if ff.covers(text):
                        return ff
                except Exception:
                    pass
        return Base14Font(BASE14[family][si], "standard font (%s) - some letters may be missing"
                          % BASE14_LABEL[family], kind="lossy")


# =============================================================================
# Text editing
# =============================================================================

def _erase_rect(chars) -> pymupdf.Rect | None:
    ink = [ch for ch in chars if ch.real and not ch.c.isspace()]
    if not ink:
        return None
    r = pymupdf.Rect(ink[0].rect)
    for ch in ink[1:]:
        r |= ch.rect
    h = r.height
    r.y0 += 0.25 * h
    r.y1 -= 0.25 * h
    r.x0 += min(0.3, ink[0].rect.width * 0.25)
    r.x1 -= min(0.3, ink[-1].rect.width * 0.25)
    return r


def _redact(page, rect):
    page.add_redact_annot(rect, fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


class LayoutShift(Exception):
    """Erasing would make other letters move."""


def _char_list(page) -> list:
    out = []
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for ch in span["chars"]:
                    if not ch["c"].isspace():
                        out.append((ch["c"], ch["bbox"][0], ch["bbox"][1]))
    return out


def _take(pool: dict, c, x, y, tol=0.35) -> bool:
    for item in pool.get(c, ()):
        if not item[2] and abs(item[0] - x) < tol and abs(item[1] - y) < tol:
            item[2] = True
            return True
    return False


def _pool(chars) -> dict:
    pool = {}
    for c, x, y in chars:
        pool.setdefault(c, []).append([x, y, False])
    return pool


def erase_chars(page, chars):
    """Remove exactly these letters from the page, leaving every other letter where it was.
    Raises EditError or LayoutShift without changing the page if that isn't possible."""
    rect = _erase_rect(chars)
    if rect is None:
        return
    erased = [(ch.c, ch.rect.x0, ch.rect.y0) for ch in chars if ch.real and not ch.c.isspace()]
    doc = page.parent
    # Try on a throw-away copy of the page first, so a bad erase never touches the real document.
    tmp = pymupdf.open()
    tmp.insert_pdf(doc, from_page=page.number, to_page=page.number, annots=False, widgets=False)
    tpage = tmp[0]
    before = _char_list(tpage)
    pool = _pool(before)
    if not all(_take(pool, c, x, y) for c, x, y in erased):
        raise EditError("This text could not be located exactly.")
    expected = [(c, item[0], item[1]) for c, items in pool.items() for item in items if not item[2]]
    _redact(tpage, rect)
    after = _char_list(tpage)
    tmp.close()
    if len(after) != len(expected):
        raise EditError("This text overlaps other text, so it can't be changed without damaging it.")
    pool = _pool(after)
    if not all(_take(pool, c, x, y) for c, x, y in expected):
        raise LayoutShift()
    n = len(_char_list(page))
    _redact(page, rect)
    if len(_char_list(page)) != n - len(erased):
        raise EditError("The text could not be erased cleanly.")


def _erase_or_fail(page, chars):
    try:
        erase_chars(page, chars)
    except LayoutShift:
        raise EditError("Changing this text would move other text on the page, so it was left as is.")


def edit_run(doc, page, run: TextRun, new_text: str, resolver: FontResolver, squeeze=False):
    """Replace the text of a run. Only the changed part is rewritten when possible.
    Returns a short description of the font used, or None if nothing changed."""
    old = run.text
    new_text = new_text.replace("\r", "").replace("\n", " ")
    if new_text == old:
        return None
    n_old, n_new = len(old), len(new_text)
    p = 0
    while p < min(n_old, n_new) and old[p] == new_text[p]:
        p += 1
    s = 0
    while s < min(n_old, n_new) - p and old[n_old - 1 - s] == new_text[n_new - 1 - s]:
        s += 1

    chars = run.chars
    run_end = run.bbox.x1
    start_x = chars[p].x if p < n_old else chars[-1].rect.x1
    font = resolver.choose(page, run, new_text[p:])
    size = run.size
    old_mid_end = chars[n_old - s].x if s > 0 else run_end
    new_mid = new_text[p:n_new - s]
    to_end = (chars[p:], new_text[p:])
    if s > 0 and abs(font.width(new_mid, size) - (old_mid_end - start_x)) <= max(0.6, 0.05 * size):
        seg_chars, write_text = chars[p:n_old - s], new_mid
        try:
            erase_chars(page, seg_chars)
        except LayoutShift:
            # The letters after the change would move: rewrite the rest of the line instead.
            seg_chars, write_text = to_end
            _erase_or_fail(page, seg_chars)
    else:
        seg_chars, write_text = to_end
        _erase_or_fail(page, seg_chars)

    hscale = 100.0
    if squeeze and write_text:
        w = font.width(write_text, size)
        avail = run_end - start_x
        if w > avail > 0:
            hscale = max(50.0, avail / w * 100.0)

    if write_text.strip():
        color = pymupdf.sRGB_to_pdf(run.color)
        font.write(page, pymupdf.Point(start_x, run.origin.y), write_text, size, color, hscale)
    return font.label


def replace_all(doc, find: str, repl: str, match_case: bool, resolver: FontResolver):
    """Find & replace across all pages. Returns (count, set of font labels, list of failed page numbers)."""
    pat = re.compile(re.escape(find), 0 if match_case else re.IGNORECASE)
    count, labels, failed = 0, set(), []
    for page in doc:
        for run in get_text_runs(page):
            hits = len(pat.findall(run.text))
            if not hits:
                continue
            new_text = pat.sub(lambda _m: repl, run.text)
            try:
                label = edit_run(doc, page, run, new_text, resolver)
                count += hits
                if label:
                    labels.add(label)
            except EditError:
                failed.append(page.number + 1)
    return count, labels, sorted(set(failed))


def count_matches(doc, find: str, match_case: bool) -> int:
    pat = re.compile(re.escape(find), 0 if match_case else re.IGNORECASE)
    return sum(len(pat.findall(run.text)) for page in doc for run in get_text_runs(page))


# =============================================================================
# Other document helpers
# =============================================================================

def parse_page_ranges(text: str, page_count: int) -> list:
    """'1-3, 5, 8-' -> [0, 1, 2, 4, 7, ...] (0-based). Raises ValueError on bad input."""
    pages = []
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else page_count
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError("'%s' is not a page number or range" % part)
        if a < 1 or b > page_count or a > b:
            raise ValueError("'%s' is outside 1-%d" % (part, page_count))
        pages.extend(range(a - 1, b))
    if not pages:
        raise ValueError("no pages given")
    return pages


def text_box_rect(text: str, fontsize: float, at: pymupdf.Point) -> pymupdf.Rect:
    lines = text.split("\n") or [""]
    w = max(pymupdf.get_text_length(line, fontname="helv", fontsize=fontsize) for line in lines)
    h = len(lines) * fontsize * 1.25
    return pymupdf.Rect(at.x, at.y, at.x + w + fontsize * 0.8 + 4, at.y + h + fontsize * 0.4 + 4)


def signature_png_from_strokes(strokes, width, height, color=(15, 25, 80), pen=2.6, scale=4) -> bytes | None:
    pts = [p for s in strokes for p in s]
    if not pts:
        return None
    img = Image.new("RGBA", (width * scale, height * scale), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    lw = max(1, int(pen * scale))
    for s in strokes:
        sp = [(x * scale, y * scale) for x, y in s]
        if len(sp) == 1:
            x, y = sp[0]
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color + (255,))
        else:
            d.line(sp, fill=color + (255,), width=lw, joint="curve")
            for x, y in (sp[0], sp[-1]):
                d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color + (255,))
    bbox = img.getbbox()
    if not bbox:
        return None
    m = lw * 2
    img = img.crop((max(0, bbox[0] - m), max(0, bbox[1] - m),
                    min(img.width, bbox[2] + m), min(img.height, bbox[3] + m)))
    img = img.resize((max(1, img.width // 2), max(1, img.height // 2)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def signature_png_from_image(path: str) -> bytes:
    img = Image.open(path).convert("RGBA")
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if r > 230 and g > 230 and b > 230:
                px[x, y] = (r, g, b, 0)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    if img.width > 1600:
        img = img.resize((1600, int(img.height * 1600 / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def save_document(doc, path: str, password: str | None = None):
    """Save safely: write a temporary file next to the target, then swap it in."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=folder)
    os.close(fd)
    try:
        kwargs = dict(garbage=3, deflate=True)
        if password:
            kwargs.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
        doc.save(tmp, **kwargs)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# =============================================================================
# GUI
# =============================================================================

TOOLS = [
    ("select", "Select", "Click an annotation to select it, drag to move it, Delete removes it. "
                         "Double-click a note or text box to change its text."),
    ("edittext", "Edit text", "Hover over text and click it to change it.  Ctrl+H = find & replace."),
    ("highlight", "Highlight", "Drag over text to highlight it."),
    ("draw", "Draw", "Drag to draw freehand."),
    ("textbox", "Text box", "Click where the new text should go."),
    ("note", "Note", "Click to place a sticky note."),
    ("sign", "Sign", "Drag a box (or just click) where the signature should go."),
    ("forms", "Fill forms", "Click a form field to fill it in. Fields are outlined in blue."),
]
DEFAULT_COLORS = {"highlight": (1.0, 0.92, 0.2), "draw": (0.1, 0.2, 0.75), "textbox": (0.0, 0.0, 0.0),
                  "note": (1.0, 0.85, 0.2)}
PAD = 12
PAN_HINT = "View mode: drag to move the page, scroll to change pages. Click Edit (Ctrl+E) to make changes."
LOCKED_MENU_ITEMS = [
    ("file", "Merge: add PDFs at the end..."), ("file", "Merge: insert PDF after this page..."),
    ("edit", "Undo"), ("edit", "Redo"), ("edit", "Find & replace text..."),
    ("edit", "Delete selected annotation"), ("edit", "Change signature..."),
    ("pages", "Rotate left"), ("pages", "Rotate right"), ("pages", "Move page up"),
    ("pages", "Move page down"), ("pages", "Delete page"),
]


def rgb_to_hex(c):
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c)


class Dialog(tk.Toplevel):
    """Small modal dialog base class."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def buttons(self, ok_text="OK", row=99):
        f = ttk.Frame(self.body)
        f.grid(row=row, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(f, text=ok_text, command=self.ok, default="active").pack(side="right", padx=(6, 0))
        ttk.Button(f, text="Cancel", command=self.cancel).pack(side="right")

    def show(self, focus=None):
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + max(0, (p.winfo_width() - self.winfo_reqwidth()) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry("+%d+%d" % (x, y))
        self.deiconify()
        self.grab_set()
        if focus is not None:
            focus.focus_set()
        self.wait_window(self)
        return self.result

    def ok(self):
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class TextDialog(Dialog):
    def __init__(self, parent, title, prompt, initial="", multiline=False, fontsize=None, original=None,
                 squeeze=False):
        super().__init__(parent, title)
        row = 0
        if original is not None:
            ttk.Label(self.body, text="Current text:").grid(row=row, column=0, sticky="w")
            row += 1
            ttk.Label(self.body, text=original, foreground="#555", wraplength=520).grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(0, 8))
            row += 1
        ttk.Label(self.body, text=prompt).grid(row=row, column=0, sticky="w")
        row += 1
        self.multiline = multiline
        if multiline:
            self.entry = tk.Text(self.body, width=52, height=6, wrap="word", font=("Segoe UI", 10))
            self.entry.insert("1.0", initial)
        else:
            self.var = tk.StringVar(value=initial)
            self.entry = ttk.Entry(self.body, textvariable=self.var, width=60)
            self.entry.select_range(0, "end")
            self.bind("<Return>", lambda e: self.ok())
        self.entry.grid(row=row, column=0, columnspan=4, sticky="we")
        row += 1
        self.size_var = None
        if fontsize is not None:
            f = ttk.Frame(self.body)
            f.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))
            ttk.Label(f, text="Font size:").pack(side="left")
            self.size_var = tk.StringVar(value=str(fontsize))
            ttk.Spinbox(f, from_=4, to=96, textvariable=self.size_var, width=5).pack(side="left", padx=6)
            row += 1
        self.squeeze_var = None
        if squeeze:
            self.squeeze_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(self.body, text="Squeeze longer text to fit the original width",
                            variable=self.squeeze_var).grid(row=row, column=0, columnspan=4, sticky="w",
                                                            pady=(8, 0))
            row += 1
        self.buttons()

    def ok(self):
        text = self.entry.get("1.0", "end-1c") if self.multiline else self.var.get()
        size = None
        if self.size_var is not None:
            try:
                size = max(4.0, min(96.0, float(self.size_var.get())))
            except ValueError:
                size = 11.0
        self.result = (text, size, bool(self.squeeze_var.get()) if self.squeeze_var else False)
        self.destroy()


class ChoiceDialog(Dialog):
    def __init__(self, parent, title, prompt, values, initial, editable):
        super().__init__(parent, title)
        ttk.Label(self.body, text=prompt).grid(row=0, column=0, sticky="w")
        self.var = tk.StringVar(value=initial)
        self.combo = ttk.Combobox(self.body, textvariable=self.var, values=values, width=40,
                                  state="normal" if editable else "readonly")
        self.combo.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self.bind("<Return>", lambda e: self.ok())
        self.buttons()

    def ok(self):
        self.result = self.var.get()
        self.destroy()


class ReplaceDialog(Dialog):
    def __init__(self, parent, app):
        super().__init__(parent, "Find & replace text")
        self.app = app
        ttk.Label(self.body, text="Find:").grid(row=0, column=0, sticky="w")
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(self.body, textvariable=self.find_var, width=44)
        self.find_entry.grid(row=0, column=1, sticky="we", pady=2)
        ttk.Label(self.body, text="Replace with:").grid(row=1, column=0, sticky="w")
        self.repl_var = tk.StringVar()
        ttk.Entry(self.body, textvariable=self.repl_var, width=44).grid(row=1, column=1, sticky="we", pady=2)
        self.case_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body, text="Match case", variable=self.case_var).grid(row=2, column=1, sticky="w")
        self.info = ttk.Label(self.body, text="Works best when the new text is about as long as the old text.",
                              foreground="#555")
        self.info.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        f = ttk.Frame(self.body)
        f.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(f, text="Replace all", command=self.ok, default="active").pack(side="right", padx=(6, 0))
        ttk.Button(f, text="Count", command=self.count).pack(side="right", padx=(6, 0))
        ttk.Button(f, text="Close", command=self.cancel).pack(side="right")

    def count(self):
        find = self.find_var.get()
        if find:
            n = count_matches(self.app.doc, find, self.case_var.get())
            self.info.config(text="Found %d match(es)." % n)

    def ok(self):
        if self.find_var.get():
            self.result = (self.find_var.get(), self.repl_var.get(), self.case_var.get())
            self.destroy()


class SignatureDialog(Dialog):
    W, H = 480, 170

    def __init__(self, parent, saved_path):
        super().__init__(parent, "Your signature")
        self.saved_path = saved_path
        self.strokes, self.image_png = [], None
        ttk.Label(self.body, text="Draw your signature with the mouse, or load an image of it.").grid(
            row=0, column=0, columnspan=4, sticky="w")
        self.canvas = tk.Canvas(self.body, width=self.W, height=self.H, bg="white", cursor="pencil",
                                highlightthickness=1, highlightbackground="#999")
        self.canvas.grid(row=1, column=0, columnspan=4, pady=6)
        self.canvas.create_line(20, self.H - 35, self.W - 20, self.H - 35, fill="#ddd", dash=(4, 3))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        f = ttk.Frame(self.body)
        f.grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Button(f, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(f, text="Load image...", command=self.load).pack(side="left", padx=6)
        if os.path.exists(saved_path):
            ttk.Button(f, text="Use saved signature", command=self.use_saved).pack(side="left")
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body, text="Remember this signature on this computer",
                        variable=self.remember).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.buttons("Use signature")
        self._preview = None

    def _press(self, e):
        if self.image_png:
            self.clear()
        self.strokes.append([(e.x, e.y)])

    def _drag(self, e):
        if not self.strokes:
            return
        x0, y0 = self.strokes[-1][-1]
        self.strokes[-1].append((e.x, e.y))
        self.canvas.create_line(x0, y0, e.x, e.y, width=2.6, fill="#0f1950", capstyle="round",
                                smooth=True, tags="ink")

    def clear(self):
        self.strokes, self.image_png = [], None
        self.canvas.delete("ink")

    def _show_png(self, png):
        self.clear()
        self.image_png = png
        img = Image.open(io.BytesIO(png))
        img.thumbnail((self.W - 20, self.H - 20))
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        buf = io.BytesIO()
        bg.save(buf, "PNG")
        self._preview = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        self.canvas.create_image(self.W // 2, self.H // 2, image=self._preview, tags="ink")

    def load(self):
        path = filedialog.askopenfilename(parent=self, title="Signature image",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All", "*.*")])
        if path:
            try:
                self._show_png(signature_png_from_image(path))
            except Exception as ex:
                messagebox.showerror(APP_NAME, "Could not read that image:\n%s" % ex, parent=self)

    def use_saved(self):
        with open(self.saved_path, "rb") as f:
            self._show_png(f.read())

    def ok(self):
        png = self.image_png or signature_png_from_strokes(self.strokes, self.W, self.H)
        if not png:
            messagebox.showinfo(APP_NAME, "Draw a signature or load an image first.", parent=self)
            return
        if self.remember.get():
            try:
                with open(self.saved_path, "wb") as f:
                    f.write(png)
            except OSError:
                pass
        self.result = png
        self.destroy()


class App:
    MAX_HISTORY = 30

    def __init__(self, root: tk.Tk, path: str | None = None):
        self.root = root
        self.doc = None
        self.path = None
        self.password = None
        self.pno = 0
        self.zoom = 1.0
        self.fit_mode = "width"
        self.dirty = False
        self.undo_stack, self.redo_stack = [], []
        self.resolver = FontResolver(None)
        self.runs_cache = {}
        self.tool = tk.StringVar(value="pan")
        self.mode = "view"
        self.colors = dict(DEFAULT_COLORS)
        self.pen_width = tk.IntVar(value=2)
        self.signature_png = None
        self.signature_path = os.path.join(app_data_dir(), "signature.png")
        self.selected_xref = None
        self.drag = None
        self._img = None
        self._pg = self._pg_key = None

        root.title(APP_NAME)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d" % (min(1150, sw - 60), min(820, sh - 110)))
        if sys.platform == "win32":
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        root.minsize(700, 450)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._bind_keys()
        self.tool.trace_add("write", lambda *a: self._tool_changed())
        self.set_mode("view")
        self._update_title()
        if path:
            root.after(50, lambda: self.open_file(path))

    # ---------------------------------------------------------------- layout
    def _build_menu(self):
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=False)
        fm.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_dialog)
        fm.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        fm.add_command(label="Save as...", accelerator="Ctrl+Shift+S", command=self.save_as)
        fm.add_separator()
        fm.add_command(label="Merge: add PDFs at the end...", command=lambda: self.insert_pdfs(at_end=True))
        fm.add_command(label="Merge: insert PDF after this page...", command=lambda: self.insert_pdfs(at_end=False))
        fm.add_command(label="Split: extract pages to a new PDF...", command=self.extract_pages)
        fm.add_command(label="Split: one PDF per page...", command=self.split_pages)
        fm.add_separator()
        fm.add_command(label="Close document", command=self.close_doc)
        fm.add_command(label="Exit", command=self.quit)
        m.add_cascade(label="File", menu=fm)

        em = tk.Menu(m, tearoff=False)
        em.add_command(label="Switch to edit mode", accelerator="Ctrl+E", command=self.toggle_mode)
        em.add_separator()
        em.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        em.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        em.add_separator()
        em.add_command(label="Find & replace text...", accelerator="Ctrl+H", command=self.find_replace)
        em.add_command(label="Delete selected annotation", accelerator="Del", command=self.delete_selected)
        em.add_command(label="Change signature...", command=self.choose_signature)
        m.add_cascade(label="Edit", menu=em)

        pm = tk.Menu(m, tearoff=False)
        pm.add_command(label="Previous page", accelerator="PgUp", command=lambda: self.go(self.pno - 1))
        pm.add_command(label="Next page", accelerator="PgDn", command=lambda: self.go(self.pno + 1))
        pm.add_separator()
        pm.add_command(label="Rotate left", command=lambda: self.rotate(-90))
        pm.add_command(label="Rotate right", command=lambda: self.rotate(90))
        pm.add_command(label="Move page up", command=lambda: self.move_page(-1))
        pm.add_command(label="Move page down", command=lambda: self.move_page(1))
        pm.add_command(label="Delete page", command=self.delete_page)
        m.add_cascade(label="Pages", menu=pm)

        vm = tk.Menu(m, tearoff=False)
        vm.add_command(label="Zoom in", accelerator="Ctrl++", command=lambda: self.set_zoom(self.zoom * 1.25))
        vm.add_command(label="Zoom out", accelerator="Ctrl+-", command=lambda: self.set_zoom(self.zoom / 1.25))
        vm.add_command(label="Fit width", command=lambda: self.fit("width"))
        vm.add_command(label="Fit page", command=lambda: self.fit("page"))
        vm.add_command(label="Actual size", command=lambda: self.set_zoom(1.0))
        m.add_cascade(label="View", menu=vm)

        hm = tk.Menu(m, tearoff=False)
        hm.add_command(label="Quick guide", command=self.show_help)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            APP_NAME, "%s %s\nA small PDF editor built with Tkinter and PyMuPDF." % (APP_NAME, APP_VERSION)))
        m.add_cascade(label="Help", menu=hm)
        self.root.config(menu=m)
        self.menus = {"file": fm, "edit": em, "pages": pm}

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(6, 4))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Open", width=6, command=self.open_dialog).pack(side="left")
        ttk.Button(bar, text="Save", width=6, command=self.save).pack(side="left", padx=(2, 8))
        ttk.Style(self.root).configure("Mode.TButton", font=("Segoe UI", 9, "bold"))
        self.mode_btn = ttk.Button(bar, text="Edit", width=8, style="Mode.TButton", command=self.toggle_mode)
        self.mode_btn.pack(side="left", padx=(0, 8))
        self.edit_bar = ebar = ttk.Frame(bar)
        ttk.Button(ebar, text="Undo", width=6, command=self.undo).pack(side="left")
        ttk.Button(ebar, text="Redo", width=6, command=self.redo).pack(side="left", padx=(2, 8))
        ttk.Separator(ebar, orient="vertical").pack(side="left", fill="y", padx=4)
        for key, label, _hint in TOOLS:
            ttk.Radiobutton(ebar, text=label, value=key, variable=self.tool, style="Toolbutton").pack(
                side="left", padx=1)
        ttk.Separator(ebar, orient="vertical").pack(side="left", fill="y", padx=6)
        self.color_btn = tk.Button(ebar, text="  ", width=2, relief="groove", command=self.pick_color)
        self.color_btn.pack(side="left")
        ttk.Label(ebar, text=" Pen").pack(side="left")
        ttk.Spinbox(ebar, from_=1, to=12, textvariable=self.pen_width, width=3).pack(side="left", padx=(2, 0))


    def _build_body(self):
        bottom = ttk.Frame(self.root, relief="sunken", padding=(0, 2))
        bottom.pack(side="bottom", fill="x")
        nav = ttk.Frame(bottom)
        nav.pack(side="right", padx=6)
        ttk.Button(nav, text="-", width=2, command=lambda: self.set_zoom(self.zoom / 1.25)).pack(side="left")
        self.zoom_label = ttk.Label(nav, text="100%", width=5, anchor="center")
        self.zoom_label.pack(side="left")
        ttk.Button(nav, text="+", width=2, command=lambda: self.set_zoom(self.zoom * 1.25)).pack(
            side="left", padx=(0, 10))
        ttk.Button(nav, text="<", width=2, command=lambda: self.go(self.pno - 1)).pack(side="left")
        self.page_var = tk.StringVar(value="0")
        e = ttk.Entry(nav, textvariable=self.page_var, width=4, justify="center")
        e.pack(side="left", padx=2)
        e.bind("<Return>", lambda ev: self._page_entry())
        self.page_total = ttk.Label(nav, text="/ 0")
        self.page_total.pack(side="left")
        ttk.Button(nav, text=">", width=2, command=lambda: self.go(self.pno + 1)).pack(side="left", padx=(2, 0))
        self.status = ttk.Label(bottom, text="", anchor="w", padding=(8, 3))
        self.status.pack(side="left", fill="x", expand=True)
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        ttk.Label(left, text="Pages", padding=(6, 4)).pack(anchor="w")
        self.page_list = tk.Listbox(left, width=12, activestyle="none", exportselection=False)
        self.page_list.pack(fill="both", expand=True, padx=(4, 0), pady=(0, 4))
        self.page_list.bind("<<ListboxSelect>>", self._list_select)
        body.add(left, weight=0)
        right = ttk.Frame(body)
        self.canvas = tk.Canvas(right, bg="#6b6f75", highlightthickness=0)
        vs = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        hs = ttk.Scrollbar(right, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.canvas.pack(fill="both", expand=True)
        body.add(right, weight=1)
        c = self.canvas
        c.bind("<Motion>", self._motion)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", lambda e: self._scroll(-1, e))
        c.bind("<Button-5>", lambda e: self._scroll(1, e))
        c.bind("<Configure>", lambda e: self._resized())
        self._show_empty()

    def _bind_keys(self):
        r = self.root
        r.bind("<Control-o>", lambda e: self.open_dialog())
        r.bind("<Control-s>", lambda e: self.save())
        r.bind("<Control-S>", lambda e: self.save_as())
        r.bind("<Control-z>", lambda e: self.undo())
        r.bind("<Control-y>", lambda e: self.redo())
        r.bind("<Control-h>", lambda e: self.find_replace())
        r.bind("<Control-e>", lambda e: self.toggle_mode())
        r.bind("<Control-plus>", lambda e: self.set_zoom(self.zoom * 1.25))
        r.bind("<Control-equal>", lambda e: self.set_zoom(self.zoom * 1.25))
        r.bind("<Control-minus>", lambda e: self.set_zoom(self.zoom / 1.25))
        r.bind("<Prior>", lambda e: self.go(self.pno - 1))
        r.bind("<Next>", lambda e: self.go(self.pno + 1))
        r.bind("<Delete>", lambda e: self.delete_selected())
        r.bind("<Escape>", lambda e: self._cancel_drag())

    # ---------------------------------------------------------------- helpers
    def page(self):
        # Keep one Page object alive: annotations and form fields crash if their page is garbage-collected.
        key = (id(self.doc), self.pno)
        if self._pg is None or self._pg_key != key:
            self._pg, self._pg_key = self.doc[self.pno], key
        return self._pg

    def _forget_page(self):
        self._pg = self._pg_key = None

    def to_page(self, cx, cy) -> pymupdf.Point:
        """Canvas coordinates -> unrotated page coordinates."""
        return pymupdf.Point((cx - PAD) / self.zoom, (cy - PAD) / self.zoom) * self.page().derotation_matrix

    def to_canvas_rect(self, rect) -> tuple:
        r = pymupdf.Rect(rect) * self.page().rotation_matrix * pymupdf.Matrix(self.zoom, self.zoom)
        r.normalize()
        return r.x0 + PAD, r.y0 + PAD, r.x1 + PAD, r.y1 + PAD

    def canvas_xy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def set_status(self, text):
        self.status.config(text=text)

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else "No document"
        self.root.title("%s%s - %s" % ("*" if self.dirty else "", name, APP_NAME))

    def runs(self):
        if self.pno not in self.runs_cache:
            self.runs_cache[self.pno] = get_text_runs(self.page())
        return self.runs_cache[self.pno]

    def busy(self, on=True):
        self.root.config(cursor="watch" if on else "")
        self.root.update_idletasks()

    # ---------------------------------------------------------------- view / edit mode
    def set_mode(self, mode):
        """'view' (read-only, the default) or 'edit' (editing tools shown)."""
        self.mode = mode
        editing = mode == "edit"
        if editing:
            self.edit_bar.pack(side="left")
            self.mode_btn.config(text="Done")
        else:
            self.edit_bar.pack_forget()
            self.mode_btn.config(text="Edit")
            self._cancel_drag()
            self.selected_xref = None
        state = "normal" if editing else "disabled"
        for key, label in LOCKED_MENU_ITEMS:
            menu = self.menus[key]
            for i in range((menu.index("end") or 0) + 1):
                if menu.type(i) == "command" and menu.entrycget(i, "label") == label:
                    menu.entryconfig(i, state=state)
        self.menus["edit"].entryconfig(0, label="Back to view mode" if editing else "Switch to edit mode")
        self.tool.set("edittext" if editing else "pan")

    def toggle_mode(self):
        self.set_mode("view" if self.mode == "edit" else "edit")

    def _require_edit(self) -> bool:
        if self.mode == "edit":
            return True
        self.set_status("You're in view mode. Click Edit (Ctrl+E) to make changes.")
        return False

    # ---------------------------------------------------------------- document lifecycle
    def open_dialog(self):
        if not self.confirm_discard():
            return
        path = filedialog.askopenfilename(title="Open PDF", filetypes=[("PDF files", "*.pdf"), ("All", "*.*")])
        if path:
            self.open_file(path)

    def _ask_password(self, name):
        d = TextDialog(self.root, "Password", "'%s' is password-protected. Password:" % name)
        d.entry.config(show="*")
        r = d.show(d.entry)
        return r[0] if r else None

    def _load_pdf(self, path):
        with open(path, "rb") as f:
            data = f.read()
        doc = pymupdf.open(stream=data, filetype="pdf")
        password = None
        if doc.needs_pass:
            while True:
                password = self._ask_password(os.path.basename(path))
                if password is None:
                    return None, None
                if doc.authenticate(password):
                    break
                messagebox.showerror(APP_NAME, "Wrong password.")
        return doc, password

    def open_file(self, path):
        try:
            doc, password = self._load_pdf(path)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Could not open the file:\n%s" % ex)
            return
        if doc is None:
            return
        if doc.page_count == 0:
            messagebox.showerror(APP_NAME, "This PDF has no pages.")
            return
        self._forget_page()
        self.doc, self.path, self.password = doc, os.path.abspath(path), password
        self.pno, self.dirty = 0, False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.resolver.reset(doc)
        self.runs_cache.clear()
        self.selected_xref = None
        self.fit_mode = "width"
        self.set_mode("view")
        self._refresh_page_list()
        self._update_title()
        self.root.after(30, lambda: self.fit("width"))
        if not any(page.get_text().strip() for page in doc.pages(0, min(doc.page_count, 5))):
            self.set_status("This PDF looks scanned (pictures of text). Text editing needs real text.")

    def close_doc(self):
        if not self.confirm_discard():
            return
        self._forget_page()
        self.doc = None
        self.path = None
        self.dirty = False
        self.page_list.delete(0, "end")
        self._show_empty()
        self._update_title()

    def confirm_discard(self) -> bool:
        if self.doc is None or not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_NAME, "Save changes to '%s'?" % os.path.basename(self.path or ""))
        if ans is None:
            return False
        if ans:
            return self.save()
        return True

    def quit(self):
        if self.confirm_discard():
            self.root.destroy()

    def save(self) -> bool:
        if self.doc is None:
            return False
        if not self.path:
            return self.save_as()
        return self._save_to(self.path)

    def save_as(self) -> bool:
        if self.doc is None:
            return False
        base = os.path.splitext(os.path.basename(self.path or "document.pdf"))[0]
        path = filedialog.asksaveasfilename(title="Save PDF as", defaultextension=".pdf",
                                            initialdir=os.path.dirname(self.path or ""),
                                            initialfile=base + "-edited.pdf", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return False
        if self._save_to(path):
            self.path = os.path.abspath(path)
            self._update_title()
            return True
        return False

    def _save_to(self, path) -> bool:
        self.busy()
        try:
            save_document(self.doc, path, self.password)
        except PermissionError:
            messagebox.showerror(APP_NAME, "Could not save: the file is open in another program or is "
                                           "read-only.\nClose it there, or use Save as.")
            return False
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Could not save:\n%s" % ex)
            return False
        finally:
            self.busy(False)
        self.dirty = False
        self._update_title()
        self.set_status("Saved %s" % path)
        return True

    # ---------------------------------------------------------------- undo / modify
    def modify(self, description, fn):
        """Run fn() as one undoable change. Any error restores the document."""
        if self.doc is None or not self._require_edit():
            return None
        snapshot = self.doc.tobytes()
        try:
            self.busy()
            result = fn()
            self._forget_page()
        except Exception as ex:
            self._forget_page()
            self._restore(snapshot)
            msg = str(ex) if isinstance(ex, EditError) else "%s failed:\n%s" % (description, ex)
            messagebox.showerror(APP_NAME, msg)
            self.render()
            return None
        finally:
            self.busy(False)
        self.undo_stack.append((snapshot, self.pno, description))
        del self.undo_stack[:-self.MAX_HISTORY]
        self.redo_stack.clear()
        self.dirty = True
        self.runs_cache.clear()
        self._update_title()
        self.render()
        return result

    def _restore(self, data):
        self._forget_page()
        self.doc = pymupdf.open(stream=data, filetype="pdf")
        self.resolver.reset(self.doc)
        self.runs_cache.clear()
        self.selected_xref = None
        self.pno = min(self.pno, self.doc.page_count - 1)

    def undo(self):
        if not self.undo_stack or not self._require_edit():
            return
        data, pno, desc = self.undo_stack.pop()
        self.redo_stack.append((self.doc.tobytes(), self.pno, desc))
        self._restore(data)
        self.pno = min(pno, self.doc.page_count - 1)
        self.dirty = True
        self._refresh_page_list()
        self._update_title()
        self.set_status("Undid: %s" % desc)

    def redo(self):
        if not self.redo_stack or not self._require_edit():
            return
        data, pno, desc = self.redo_stack.pop()
        self.undo_stack.append((self.doc.tobytes(), self.pno, desc))
        self._restore(data)
        self.pno = min(pno, self.doc.page_count - 1)
        self.dirty = True
        self._refresh_page_list()
        self._update_title()
        self.set_status("Redid: %s" % desc)

    # ---------------------------------------------------------------- rendering
    def _show_empty(self):
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 1, 1))
        self.canvas.create_text(30, 30, anchor="nw", fill="white", font=("Segoe UI", 13),
                                text="Open a PDF to start (File > Open, or Ctrl+O).")

    def _refresh_page_list(self):
        self.page_list.delete(0, "end")
        for i in range(self.doc.page_count):
            self.page_list.insert("end", "  Page %d" % (i + 1))
        self.render()

    def _list_select(self, _e):
        sel = self.page_list.curselection()
        if sel and sel[0] != self.pno:
            self.go(sel[0])

    def _page_entry(self):
        try:
            self.go(int(self.page_var.get()) - 1)
        except ValueError:
            pass

    def go(self, pno):
        if self.doc is None:
            return
        pno = max(0, min(self.doc.page_count - 1, pno))
        if pno != self.pno:
            self.pno = pno
            self.selected_xref = None
            self.render()
            self.canvas.yview_moveto(0)

    def _resized(self):
        if self.doc is not None and self.fit_mode:
            self.fit(self.fit_mode)

    def fit(self, mode):
        if self.doc is None:
            return
        self.fit_mode = mode
        r = self.page().rect
        cw = max(100, self.canvas.winfo_width() - 2 * PAD - 4)
        ch = max(100, self.canvas.winfo_height() - 2 * PAD - 4)
        z = cw / r.width if mode == "width" else min(cw / r.width, ch / r.height)
        self._apply_zoom(z)

    def set_zoom(self, z):
        self.fit_mode = None
        self._apply_zoom(z)

    def _apply_zoom(self, z):
        if self.doc is None:
            return
        z = max(0.2, min(5.0, z))
        if abs(z - self.zoom) > 1e-3 or self._img is None:
            self.zoom = z
            self.render()

    def render(self):
        if self.doc is None:
            return
        self.pno = max(0, min(self.doc.page_count - 1, self.pno))
        page = self.page()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), annots=True)
        try:
            self._img = tk.PhotoImage(data=pix.tobytes("ppm"))
        except tk.TclError:
            self._img = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")))
        c = self.canvas
        c.delete("all")
        c.create_rectangle(PAD + 3, PAD + 3, PAD + pix.width + 3, PAD + pix.height + 3, fill="#3c3f44", width=0)
        c.create_image(PAD, PAD, anchor="nw", image=self._img, tags="page")
        c.configure(scrollregion=(0, 0, pix.width + 2 * PAD, pix.height + 2 * PAD))
        self.zoom_label.config(text="%d%%" % round(self.zoom * 100))
        self.page_var.set(str(self.pno + 1))
        self.page_total.config(text="/ %d" % self.doc.page_count)
        self.page_list.selection_clear(0, "end")
        self.page_list.selection_set(self.pno)
        self.page_list.see(self.pno)
        self._draw_overlays()

    def _draw_overlays(self):
        c = self.canvas
        c.delete("overlay")
        page = self.page()
        if self.tool.get() == "forms":
            for w in page.widgets():
                c.create_rectangle(*self.to_canvas_rect(w.rect), outline="#1e6fd9", dash=(3, 2), tags="overlay")
        if self.selected_xref:
            a = self._annot_by_xref(self.selected_xref)
            if a is not None:
                c.create_rectangle(*self.to_canvas_rect(a.rect), outline="#e0245e", width=2, dash=(4, 2),
                                   tags="overlay")

    # ---------------------------------------------------------------- tools
    def _tool_changed(self):
        tool = self.tool.get()
        hint = dict((k, h) for k, _l, h in TOOLS).get(tool, PAN_HINT)
        self.set_status(hint)
        cursors = {"pan": "fleur", "select": "arrow", "edittext": "xterm", "highlight": "xterm", "draw": "pencil",
                   "textbox": "crosshair", "note": "crosshair", "sign": "crosshair", "forms": "hand2"}
        self.canvas.config(cursor=cursors.get(tool, "arrow"))
        color = self.colors.get(tool)
        self.color_btn.config(bg=rgb_to_hex(color) if color else "SystemButtonFace"
                              if sys.platform == "win32" else "#d9d9d9",
                              state="normal" if color else "disabled")
        if tool != "select":
            self.selected_xref = None
        self.canvas.delete("hover")
        if self.doc is not None:
            self._draw_overlays()
        if tool == "sign" and self.doc is not None and self.signature_png is None:
            self.root.after(10, self.choose_signature)

    def pick_color(self):
        tool = self.tool.get()
        if tool not in self.colors:
            return
        res = colorchooser.askcolor(color=rgb_to_hex(self.colors[tool]), title="Color")
        if res and res[0]:
            self.colors[tool] = tuple(v / 255.0 for v in res[0])
            self.color_btn.config(bg=rgb_to_hex(self.colors[tool]))

    def choose_signature(self):
        png = SignatureDialog(self.root, self.signature_path).show()
        if png:
            self.signature_png = png
            self.set_status("Signature ready. Drag a box (or click) on the page to place it.")
        return png

    # ---------------------------------------------------------------- mouse
    def _motion(self, e):
        if self.doc is None or self.drag:
            return
        tool = self.tool.get()
        c = self.canvas
        c.delete("hover")
        x, y = self.canvas_xy(e)
        pt = self.to_page(x, y)
        if tool == "edittext":
            run = run_at(self.runs(), pt)
            if run is not None:
                c.create_rectangle(*self.to_canvas_rect(run.bbox), outline="#1e6fd9", width=1.5, tags="hover")
        elif tool == "select":
            a = self._annot_at(pt)
            if a is not None and a.xref != self.selected_xref:
                c.create_rectangle(*self.to_canvas_rect(a.rect), outline="#888", dash=(2, 2), tags="hover")
        elif tool == "forms":
            w = self._widget_at(pt)
            if w is not None:
                c.create_rectangle(*self.to_canvas_rect(w.rect), outline="#1e6fd9", width=2, tags="hover")

    def _press(self, e):
        if self.doc is None:
            return
        self.canvas.focus_set()
        x, y = self.canvas_xy(e)
        tool = self.tool.get()
        self.drag = {"start": (x, y), "last": (x, y), "points": [(x, y)], "tool": tool}
        if tool == "pan":
            self.canvas.scan_mark(int(e.x), int(e.y))
        if tool == "select":
            a = self._annot_at(self.to_page(x, y))
            self.selected_xref = a.xref if a is not None else None
            self.drag["annot"] = self.selected_xref
            self._draw_overlays()

    def _drag(self, e):
        d = self.drag
        if not d:
            return
        x, y = self.canvas_xy(e)
        c = self.canvas
        tool = d["tool"]
        if tool == "pan":
            c.scan_dragto(int(e.x), int(e.y), gain=1)
        elif tool == "draw":
            lx, ly = d["last"]
            w = max(1, self.pen_width.get() * self.zoom)
            c.create_line(lx, ly, x, y, fill=rgb_to_hex(self.colors["draw"]), width=w, capstyle="round",
                          tags="temp")
            d["points"].append((x, y))
        elif tool in ("highlight", "sign"):
            c.delete("temp")
            sx, sy = d["start"]
            c.create_rectangle(sx, sy, x, y, outline="#1e6fd9", dash=(3, 2), tags="temp")
        elif tool == "select" and d.get("annot"):
            c.delete("temp")
            a = self._annot_by_xref(d["annot"])
            if a is not None:
                x0, y0, x1, y1 = self.to_canvas_rect(a.rect)
                dx, dy = x - d["start"][0], y - d["start"][1]
                c.create_rectangle(x0 + dx, y0 + dy, x1 + dx, y1 + dy, outline="#e0245e", dash=(4, 2), tags="temp")
        d["last"] = (x, y)

    def _cancel_drag(self):
        self.drag = None
        self.canvas.delete("temp")

    def _release(self, e):
        d, self.drag = self.drag, None
        self.canvas.delete("temp")
        if not d or self.doc is None:
            return
        x, y = self.canvas_xy(e)
        sx, sy = d["start"]
        moved = abs(x - sx) > 4 or abs(y - sy) > 4
        tool = d["tool"]
        if tool == "edittext" and not moved:
            self.edit_text_at(self.to_page(x, y))
        elif tool == "highlight":
            self.add_highlight(self.to_page(sx, sy), self.to_page(x, y), moved)
        elif tool == "draw" and len(d["points"]) > 1:
            self.add_ink([self.to_page(px, py) for px, py in d["points"]])
        elif tool == "textbox" and not moved:
            self.add_textbox(x, y)
        elif tool == "note" and not moved:
            self.add_note(self.to_page(x, y))
        elif tool == "sign":
            self.place_signature(sx, sy, x, y, moved)
        elif tool == "forms" and not moved:
            self.fill_field(self.to_page(x, y))
        elif tool == "select" and moved and d.get("annot"):
            self.move_annot(d["annot"], self.to_page(sx, sy), self.to_page(x, y))

    def _double(self, e):
        if self.doc is None or self.tool.get() != "select":
            return
        x, y = self.canvas_xy(e)
        a = self._annot_at(self.to_page(x, y))
        if a is not None:
            self.edit_annot_text(a.xref)

    def _wheel(self, e):
        if e.state & 0x0004:  # Ctrl held: zoom
            self.set_zoom(self.zoom * (1.1 if e.delta > 0 else 1 / 1.1))
        else:
            self._scroll(-1 if e.delta > 0 else 1, e)

    def _scroll(self, direction, e):
        c = self.canvas
        top, bottom = c.yview()
        if direction > 0 and bottom >= 0.999 and self.doc is not None and self.pno < self.doc.page_count - 1:
            self.go(self.pno + 1)
            c.yview_moveto(0)
        elif direction < 0 and top <= 0.001 and self.doc is not None and self.pno > 0:
            self.go(self.pno - 1)
            c.yview_moveto(1)
        else:
            c.yview_scroll(direction * 3, "units")

    # ---------------------------------------------------------------- annotations
    def _annot_at(self, pt):
        found = None
        for a in self.page().annots():
            if a.rect.contains(pt):
                found = a
        return found

    def _annot_by_xref(self, xref):
        for a in self.page().annots():
            if a.xref == xref:
                return a
        return None

    def _widget_at(self, pt):
        for w in self.page().widgets():
            if w.rect.contains(pt):
                return w
        return None

    def add_highlight(self, p1, p2, moved):
        color = self.colors["highlight"]

        def do():
            page = self.page()
            area = pymupdf.Rect(p1, p2).normalize()
            if not moved:
                area = pymupdf.Rect(p1.x - 1, p1.y - 1, p1.x + 1, p1.y + 1)
            lines = {}
            for w in page.get_text("words"):
                wr = pymupdf.Rect(w[:4])
                inter = wr & area
                if not inter.is_empty and (inter.width >= wr.width * 0.4 or not moved):
                    key = (w[5], w[6])
                    lines[key] = lines[key] | wr if key in lines else wr
            if lines:
                quads = [r.quad for _k, r in sorted(lines.items())]
                annot = page.add_highlight_annot(quads)
            elif moved:
                annot = page.add_highlight_annot(area)
            else:
                return False
            annot.set_colors(stroke=color)
            annot.update()
            return True

        if not self.modify("Highlight", do):
            self.set_status("No text there to highlight. Drag across words.")

    def add_ink(self, points):
        color, width = self.colors["draw"], max(1, self.pen_width.get())

        def do():
            annot = self.page().add_ink_annot([[(p.x, p.y) for p in points]])
            annot.set_border(width=width)
            annot.set_colors(stroke=color)
            annot.update()

        self.modify("Draw", do)

    def add_textbox(self, cx, cy):
        r = TextDialog(self.root, "Add text", "Text:", multiline=True, fontsize=11).show()
        if not r or not r[0].strip():
            return
        text, size, _ = r
        color = self.colors["textbox"]

        def do():
            page = self.page()
            # Build the box upright in screen space, then map it back to page space.
            box = text_box_rect(text, size, pymupdf.Point(cx, cy))
            w, h = box.width * self.zoom, box.height * self.zoom
            p1, p2 = self.to_page(cx, cy), self.to_page(cx + w, cy + h)
            rect = pymupdf.Rect(p1, p2).normalize()
            annot = page.add_freetext_annot(rect, text, fontsize=size, fontname="helv", text_color=color,
                                            rotate=page.rotation)
            annot.update()

        self.modify("Add text", do)

    def add_note(self, pt):
        r = TextDialog(self.root, "Sticky note", "Note:", multiline=True).show()
        if not r or not r[0].strip():
            return
        color = self.colors["note"]

        def do():
            annot = self.page().add_text_annot(pt, r[0], icon="Note")
            annot.set_colors(stroke=color)
            annot.update()

        self.modify("Add note", do)

    def edit_annot_text(self, xref):
        a = self._annot_by_xref(xref)
        if a is None or a.type[0] not in (pymupdf.PDF_ANNOT_TEXT, pymupdf.PDF_ANNOT_FREE_TEXT):
            return
        is_free = a.type[0] == pymupdf.PDF_ANNOT_FREE_TEXT
        r = TextDialog(self.root, "Edit text", "Text:", initial=a.info.get("content", ""), multiline=True).show()
        if not r:
            return

        def do():
            annot = self._annot_by_xref(xref)
            annot.set_info(content=r[0])
            if is_free:
                m = re.search(r"([\d.]+)\s+Tf", self.doc.xref_get_key(annot.xref, "DA")[1] or "")
                size = float(m.group(1)) if m else 11
                old = annot.rect
                box = text_box_rect(r[0], size, pymupdf.Point(0, 0))
                if self.page().rotation in (90, 270):
                    box = pymupdf.Rect(0, 0, box.height, box.width)
                annot.set_rect(pymupdf.Rect(old.x0, old.y0, old.x0 + box.width, old.y0 + box.height))
            annot.update()

        self.modify("Edit annotation", do)

    def move_annot(self, xref, p1, p2):
        def do():
            annot = self._annot_by_xref(xref)
            if annot.type[0] in (pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_POLYGON,
                                 pymupdf.PDF_ANNOT_POLY_LINE, pymupdf.PDF_ANNOT_LINE):
                raise EditError("Drawings and highlights can't be moved. Delete it (Del) and make it again.")
            annot.set_rect(annot.rect + (p2.x - p1.x, p2.y - p1.y, p2.x - p1.x, p2.y - p1.y))
            annot.update()

        self.modify("Move", do)

    def delete_selected(self):
        if self.doc is None or not self.selected_xref:
            return
        xref = self.selected_xref

        def do():
            self.page().delete_annot(self._annot_by_xref(xref))

        self.selected_xref = None
        self.modify("Delete annotation", do)

    def place_signature(self, sx, sy, x, y, moved):
        if self.signature_png is None and not self.choose_signature():
            return
        png = self.signature_png
        img = Image.open(io.BytesIO(png))
        aspect = img.height / img.width

        def do():
            page = self.page()
            if moved:
                p1, p2 = self.to_page(sx, sy), self.to_page(x, y)
            else:
                w = 160 * self.zoom
                p1, p2 = self.to_page(x, y), self.to_page(x + w, y + w * aspect)
            rect = pymupdf.Rect(p1, p2).normalize()
            page.insert_image(rect, stream=png, keep_proportion=True, rotate=-page.rotation % 360)

        self.modify("Sign", do)

    # ---------------------------------------------------------------- forms
    def fill_field(self, pt):
        w = self._widget_at(pt)
        if w is None:
            self.set_status("No form field there. Fields are outlined in blue.")
            return
        name, ftype = w.field_name, w.field_type
        label = w.field_label or name or "Field"
        if ftype == pymupdf.PDF_WIDGET_TYPE_TEXT:
            multi = bool(w.field_flags & pymupdf.PDF_TX_FIELD_IS_MULTILINE)
            r = TextDialog(self.root, "Fill in", "%s:" % label, initial=w.field_value or "", multiline=multi).show()
            if r is None:
                return
            value = r[0]
        elif ftype in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
            values = [v[1] if isinstance(v, (list, tuple)) else v for v in (w.choice_values or [])]
            value = ChoiceDialog(self.root, "Fill in", "%s:" % label, values, w.field_value or "",
                                 editable=ftype == pymupdf.PDF_WIDGET_TYPE_COMBOBOX).show()
            if value is None:
                return
        elif ftype == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
            value = "Off" if w.field_value not in (False, "Off", "", None) else w.on_state()
        elif ftype == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
            value = w.on_state()
        else:
            self.set_status("This kind of field (%s) can't be filled here." % w.field_type_string)
            return
        xref = w.xref

        def do():
            for widget in self.page().widgets():
                if widget.xref == xref:
                    widget.field_value = value
                    widget.update()
                    return
            raise EditError("The field disappeared.")

        self.modify("Fill form", do)

    # ---------------------------------------------------------------- text editing
    def edit_text_at(self, pt):
        run = run_at(self.runs(), pt)
        if run is None:
            self.set_status("No editable text there. (Scanned pages and rotated text can't be edited.)")
            return
        r = TextDialog(self.root, "Edit text", "New text:", initial=run.text, original=run.text,
                       squeeze=True).show()
        if r is None or r[0] == run.text:
            return
        new_text, _size, squeeze = r
        if FONT_INDEX._index is None:
            self.set_status("Looking at installed fonts (first time only)...")
            self.busy()
            FONT_INDEX.find("")

        label = self.modify("Edit text", lambda: edit_run(self.doc, self.page(), run, new_text, self.resolver,
                                                         squeeze))
        if label:
            self.set_status("Text changed using the %s." % label)

    def find_replace(self):
        if self.doc is None or not self._require_edit():
            return
        r = ReplaceDialog(self.root, self).show(None)
        if not r:
            return
        find, repl, case = r
        if FONT_INDEX._index is None:
            self.busy()
            FONT_INDEX.find("")
        res = self.modify("Find & replace", lambda: replace_all(self.doc, find, repl, case, self.resolver))
        if res is None:
            return
        count, labels, failed = res
        msg = "Replaced %d occurrence(s)." % count
        if labels:
            msg += "\nFont used: " + ", ".join(sorted(labels))
        if failed:
            msg += "\nSkipped on page(s) %s, because the text overlaps other text." % ", ".join(map(str, failed))
        if count == 0 and not failed:
            msg = "'%s' was not found." % find
            if self.undo_stack and self.undo_stack[-1][2] == "Find & replace":
                self.undo_stack.pop()
                self.dirty = bool(self.undo_stack)
                self._update_title()
        messagebox.showinfo(APP_NAME, msg)

    # ---------------------------------------------------------------- pages
    def rotate(self, deg):
        if self.doc is None or not self._require_edit():
            return
        self.modify("Rotate page", lambda: self.page().set_rotation((self.page().rotation + deg) % 360))
        if self.fit_mode:
            self.fit(self.fit_mode)

    def move_page(self, delta):
        if self.doc is None or not self._require_edit():
            return
        target = self.pno + delta
        if not 0 <= target < self.doc.page_count:
            return

        def do():
            # move_page(pno, to): page is placed in front of 'to' (-1 = end)
            if delta < 0:
                self.doc.move_page(self.pno, target)
            else:
                self.doc.move_page(self.pno, target + 1 if target + 1 < self.doc.page_count else -1)
            self.pno = target

        self.modify("Move page", do)
        self._refresh_page_list()

    def delete_page(self):
        if self.doc is None or not self._require_edit():
            return
        if self.doc.page_count == 1:
            messagebox.showinfo(APP_NAME, "A PDF needs at least one page.")
            return
        if not messagebox.askyesno(APP_NAME, "Delete page %d? (You can undo this.)" % (self.pno + 1)):
            return
        self.modify("Delete page", lambda: self.doc.delete_page(self.pno))
        self._refresh_page_list()

    def insert_pdfs(self, at_end=True):
        if self.doc is None:
            messagebox.showinfo(APP_NAME, "Open a PDF first.")
            return
        if not self._require_edit():
            return
        if at_end:
            paths = filedialog.askopenfilenames(title="PDFs to add at the end",
                                                filetypes=[("PDF files", "*.pdf")])
        else:
            p = filedialog.askopenfilename(title="PDF to insert after page %d" % (self.pno + 1),
                                           filetypes=[("PDF files", "*.pdf")])
            paths = [p] if p else []
        if not paths:
            return
        sources = []
        for p in paths:
            try:
                src, _pw = self._load_pdf(p)
            except Exception as ex:
                messagebox.showerror(APP_NAME, "Could not open %s:\n%s" % (os.path.basename(p), ex))
                return
            if src is None:
                return
            sources.append(src)

        def do():
            start = -1 if at_end else self.pno + 1
            added = 0
            for src in sources:
                self.doc.insert_pdf(src, start_at=start if start < 0 else start + added)
                added += src.page_count
            return added

        added = self.modify("Merge PDFs", do)
        if added:
            self.resolver.reset(self.doc)
            self._refresh_page_list()
            self.set_status("Added %d page(s)." % added)

    def extract_pages(self):
        if self.doc is None:
            return
        r = TextDialog(self.root, "Extract pages",
                       "Pages to copy into a new PDF (e.g. 1-3, 5, 8-):", initial=str(self.pno + 1)).show()
        if not r:
            return
        try:
            pages = parse_page_ranges(r[0], self.doc.page_count)
        except ValueError as ex:
            messagebox.showerror(APP_NAME, "Page list problem: %s" % ex)
            return
        base = os.path.splitext(os.path.basename(self.path or "document.pdf"))[0]
        path = filedialog.asksaveasfilename(title="Save extracted pages as", defaultextension=".pdf",
                                            initialdir=os.path.dirname(self.path or ""),
                                            initialfile="%s-pages.pdf" % base, filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        if self.path and os.path.abspath(path) == self.path:
            messagebox.showerror(APP_NAME, "Choose a different file name than the open document.")
            return
        new = pymupdf.open()
        for pno in pages:
            new.insert_pdf(self.doc, from_page=pno, to_page=pno)
        try:
            save_document(new, path)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Could not save:\n%s" % ex)
            return
        self.set_status("Saved %d page(s) to %s" % (len(pages), path))

    def split_pages(self):
        if self.doc is None:
            return
        folder = filedialog.askdirectory(title="Folder for the single-page PDFs")
        if not folder:
            return
        base = os.path.splitext(os.path.basename(self.path or "document.pdf"))[0]
        width = max(3, len(str(self.doc.page_count)))
        names = [os.path.join(folder, "%s-p%0*d.pdf" % (base, width, i + 1)) for i in range(self.doc.page_count)]
        existing = [n for n in names if os.path.exists(n)]
        if existing and not messagebox.askyesno(
                APP_NAME, "%d file(s) with these names already exist in that folder, for example:\n%s\n\n"
                          "Replace them?" % (len(existing), os.path.basename(existing[0]))):
            return
        self.busy()
        try:
            for i, name in enumerate(names):
                new = pymupdf.open()
                new.insert_pdf(self.doc, from_page=i, to_page=i)
                save_document(new, name)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Could not save:\n%s" % ex)
            return
        finally:
            self.busy(False)
        self.set_status("Saved %d single-page PDFs to %s" % (len(names), folder))

    # ---------------------------------------------------------------- help
    def show_help(self):
        messagebox.showinfo(APP_NAME, (
            "PDFs open in view mode. Click Edit (Ctrl+E) to make changes, and Done to go back.\n\n"
            "Edit text: choose 'Edit text', hover over a line, click, type the new text.\n"
            "Find & replace: Edit > Find & replace (Ctrl+H).\n"
            "Highlight: drag across words.   Draw: drag freehand.\n"
            "Text box / Note: click where it should go.\n"
            "Sign: draw or load your signature once, then drag a box on the page.\n"
            "Fill forms: click a field outlined in blue.\n"
            "Select: click an annotation, drag to move, Del to delete, double-click to edit.\n"
            "Pages menu: rotate, move, delete.  File menu: merge and split.\n"
            "Ctrl+Z undoes any change. Nothing is written to disk until you Save."))


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
