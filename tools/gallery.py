"""Wonil Home photo gallery: captions, slot images, and the gallery list Unity builds the ring from.

gallery.json is the source of truth. Its "ring" entries are in wall order (oldest first); Unity's
Wonil > Rebuild Photo Ring menu lays them out around the courtyard in that order, so new photos
appended at the end land in the next free spot without moving anything else.

  python tools/gallery.py add <photo> [<photo> ...]   append new photos (newest last) and render them
  python tools/gallery.py render [slot ...]           re-render captions (e.g. after editing people/world in gallery.json)

Then in Unity: Wonil > Rebuild Photo Ring, and push this folder to GitHub.
"""
import datetime, json, os, re, sys
from PIL import Image, ImageDraw, ImageFont, ImageOps
from fontTools.ttLib import TTFont

SITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GALLERY = os.path.join(SITE, "gallery.json")
PROJECT = r"D:\Home\10. Unity & VRChat\Wonil Home"
FALLBACK_DIR = os.path.join(PROJECT, "Assets", "Photos", "Gallery")   # small built-in copies shown until the download lands
SELF_NAMES = {"_wonil_"}

# Frame size in the world. Every photo is PHOTO_H tall with its own width, so portraits stay portrait;
# the caption strip under it is the same height everywhere so rows line up.
PHOTO_H = 0.9        # meters
CAPTION_H = 0.30     # meters (world/date line + two lines of names)
MAX_W = 1.6          # meters; wider photos shrink to fit the column
LONG_SIDE_PX = 1024  # photo resolution along its long side (downloads are uncompressed: ~3 MB each in memory)

FONT_DIR = r"C:\Windows\Fonts"
FONT_FILES = ["NotoSansJP-VF.ttf", "NotoSansSC-VF.ttf", "malgun.ttf", "seguisym.ttf", "segoeui.ttf"]


# ---------- fonts with per-character fallback (names mix Latin, kana, hanzi, hangul, symbols) ----------

class Fonts:
    def __init__(self):
        self.paths = [os.path.join(FONT_DIR, f) for f in FONT_FILES if os.path.exists(os.path.join(FONT_DIR, f))]
        self.cmaps = [set(TTFont(p, fontNumber=0, lazy=True).getBestCmap().keys()) for p in self.paths]
        self.cache = {}

    def get(self, index, size, bold):
        key = (index, size, bold)
        if key not in self.cache:
            f = ImageFont.truetype(self.paths[index], size)
            try:
                f.set_variation_by_axes([700 if bold else 400])   # variable Noto fonts
            except Exception:
                pass
            self.cache[key] = f
        return self.cache[key]

    def runs(self, text, size, bold):
        """Split text into (font, substring) runs, each using the first font that has the glyphs."""
        out = []
        for ch in text:
            idx = next((i for i, cm in enumerate(self.cmaps) if ord(ch) in cm), 0)
            font = self.get(idx, size, bold)
            if out and out[-1][0] is font:
                out[-1][1] += ch
            else:
                out.append([font, ch])
        return out

    def width(self, text, size, bold=False):
        return sum(f.getlength(s) for f, s in self.runs(text, size, bold))

    def draw(self, d, xy, text, size, fill, bold=False):
        x, y = xy
        for f, s in self.runs(text, size, bold):
            d.text((x, y), s, font=f, fill=fill)
            x += f.getlength(s)

    def fit(self, text, size, max_w, bold=False):
        if self.width(text, size, bold) <= max_w:
            return text
        while text and self.width(text + "…", size, bold) > max_w:
            text = text[:-1]
        return text.rstrip() + "…"


FONTS = None


def name_lines(people, size, max_w, max_lines, prefix):
    """Wrap names over up to max_lines lines, separated by ' · '; '+N' closes the last line if names remain."""
    lines, i = [], 0
    for li in range(max_lines):
        line = prefix if li == 0 else ""
        first = True
        while i < len(people):
            last_line = li == max_lines - 1
            more = len(people) - i - 1
            candidate = line + ("" if first else " · ") + people[i]
            tail = ("  +%d" % more) if (last_line and more) else ""
            if FONTS.width(candidate + tail, size) > max_w:
                if first:   # a single name longer than the line
                    candidate = FONTS.fit(candidate, size, max_w - FONTS.width(tail, size))
                    line, i, first = candidate, i + 1, False
                break
            line, i, first = candidate, i + 1, False
        lines.append(line)
        if i >= len(people):
            break
    if i < len(people):
        lines[-1] += "  +%d" % (len(people) - i)
    return lines, i


# ---------- metadata ----------

def vrcx_meta(path):
    """World name and other players from the VRCX description VRCX writes into each screenshot."""
    try:
        desc = Image.open(path).info.get("Description", "")
        meta = json.loads(desc) if desc.startswith("{") else {}
    except Exception:
        meta = {}
    world = (meta.get("world") or {}).get("name", "")
    me = (meta.get("author") or {}).get("displayName", "")
    people = [p.get("displayName", "") for p in meta.get("players", [])]
    people = [p for p in people if p and p != me and p not in SELF_NAMES]
    return world, people


def file_date(name):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", name)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        return "%04d-%02d-%02d" % (y, mo, d), "%02d:%02d:%02d" % (h, mi, s)
    m = re.search(r"(\d{1,2})_(\d{1,2})_(\d{4}) (\d{1,2})_(\d{2})_(\d{2}) (AM|PM)", name)
    if m:
        mo, d, y, h, mi, s, ap = m.groups()
        h = int(h) % 12 + (12 if ap == "PM" else 0)
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d)), "%02d:%s:%s" % (h, mi, s)
    return "", ""


def pretty_date(iso):
    if not iso:
        return ""
    d = datetime.date.fromisoformat(iso)
    return d.strftime("%b ") + str(d.day) + d.strftime(", %Y")


# ---------- rendering ----------

def render(entry):
    """Writes photos/NNN.jpg (photo + caption strip) and the small built-in copy; records world size."""
    img = ImageOps.exif_transpose(Image.open(entry["source"])).convert("RGB")
    aspect = img.width / img.height
    world_w = min(PHOTO_H * aspect, MAX_W)
    world_photo_h = world_w / aspect
    px_per_m = LONG_SIDE_PX / max(world_w, world_photo_h)
    pw, ph = round(world_w * px_per_m), round(world_photo_h * px_per_m)
    strip = round(CAPTION_H * px_per_m)
    photo = img.resize((pw, ph), Image.LANCZOS)

    out = Image.new("RGB", (pw, ph + strip), (21, 22, 26))
    out.paste(photo, (0, 0))
    d = ImageDraw.Draw(out)
    pad = round(strip * 0.10)
    s1, s2 = round(strip * 0.24), round(strip * 0.176)
    inner = pw - 2 * pad

    # Undated art borrows its neighbor's date only for ordering, so don't print it.
    date = "" if entry.get("title") else pretty_date(entry.get("date", ""))
    # Narrow (portrait) frames: smaller text, and the date moves to the second line so the world name has room.
    narrow = pw < LONG_SIDE_PX * 0.7
    if narrow:
        s1, s2 = round(strip * 0.168), round(strip * 0.128)
    date_w = FONTS.width(date, s2) if date and not narrow else 0
    title = entry.get("world") or entry.get("title") or ""
    title = FONTS.fit(title, s1, inner - date_w - (pad if date_w else 0), bold=True)
    y1 = ph + pad
    y2 = y1 + s1 + round(strip * 0.07)
    y3 = y2 + s2 + round(strip * 0.05)
    FONTS.draw(d, (pad, y1), title, s1, (242, 239, 234), bold=True)
    if date and not narrow:
        FONTS.draw(d, (pw - pad - date_w, y1 + (s1 - s2) * 0.6), date, s2, (178, 172, 164))

    people = entry.get("people", [])
    if narrow and date:
        FONTS.draw(d, (pad, y2), date, s2, (178, 172, 164))
        if people:
            rest = FONTS.fit("  ·  %d friend%s" % (len(people), "" if len(people) == 1 else "s"), s2, inner - FONTS.width(date, s2))
            FONTS.draw(d, (pad + FONTS.width(date, s2), y2), rest, s2, (214, 208, 200))
            lines, _ = name_lines(people, s2, inner, 1, "")
            FONTS.draw(d, (pad, y3), lines[0], s2, (214, 208, 200))
        people = []
    if people:
        lines, _ = name_lines(people, s2, inner, 2, "with ")
        for i, line in enumerate(lines):
            FONTS.draw(d, (pad, (y2, y3)[i]), line, s2, (214, 208, 200))
        people = []
    if people:
        line = "with "
        shown = 0
        for i, name in enumerate(people):
            more = len(people) - i - 1
            candidate = line + ("" if shown == 0 else " · ") + name
            tail = ("  +%d" % more) if more else ""
            if FONTS.width(candidate + tail, s2) > inner:
                break
            line, shown = candidate, shown + 1
        if shown == 0:
            line = FONTS.fit("with " + people[0], s2, inner)
            shown = 1
        if shown < len(people):
            line += "  +%d" % (len(people) - shown)
        FONTS.draw(d, (pad, y2), line, s2, (214, 208, 200))

    # A fresh image object carries no EXIF/XMP/text chunks, so screenshot metadata is never published.
    path = os.path.join(SITE, entry["file"].replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.save(path, "JPEG", quality=86, optimize=True, progressive=True)

    os.makedirs(FALLBACK_DIR, exist_ok=True)
    small = out.copy()
    small.thumbnail((640, 640), Image.LANCZOS)
    small.save(os.path.join(FALLBACK_DIR, os.path.basename(entry["file"])), "JPEG", quality=82)

    entry["world_w"] = round(world_w, 4)
    entry["world_h"] = round(world_photo_h + CAPTION_H, 4)


# ---------- commands ----------

def load():
    return json.load(open(GALLERY, encoding="utf-8"))


def save(g):
    json.dump(g, open(GALLERY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def next_slot(g):
    return max(e["slot"] for e in g["ring"] + g.get("other", [])) + 1


def cmd_add(paths):
    g = load()
    added = []
    for p in sorted(paths, key=lambda p: file_date(os.path.basename(p))):
        n = next_slot(g)
        date, time = file_date(os.path.basename(p))
        world, people = vrcx_meta(p)
        e = {"slot": n, "file": "photos/%03d.jpg" % n, "source": os.path.abspath(p), "date": date, "time": time,
             "world": world, "people": people}
        render(e)
        g["ring"].append(e)
        added.append(n)
    save(g)
    print("added slots", added)


def cmd_render(slots):
    g = load()
    want = set(int(s) for s in slots)
    for e in g["ring"]:
        if not want or e["slot"] in want:
            render(e)
    save(g)
    print("rendered", len(want) or len(g["ring"]), "photos")


if __name__ == "__main__":
    FONTS = Fonts()
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "add":
        cmd_add(args)
    elif cmd == "render":
        cmd_render(args)
    else:
        print(__doc__)
