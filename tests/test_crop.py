#!/usr/bin/env python3
"""Standalone tests for the PDF cropping core (needs pypdfium2, nothing else).

Run:  python tests/test_crop.py
Also used by CI to generate a smoke-test sample:
      python tests/test_crop.py --make-sample out.pdf
      python tests/test_crop.py --check-sample out_cropped.pdf
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_cropper as pc  # noqa: E402

# --- tiny PDF builder -------------------------------------------------------


def make_pdf(pages) -> bytes:
    """Minimal PDF. Each page is a dict: box (l, b, r, t), rotate (degrees),
    rects [(x, y, w, h, grey)] filled in that order (grey 0 black, 1 white),
    and optionally crop (l, b, r, t) for a CropBox and annots [(l, b, r, t)]
    for square annotations without an appearance stream."""
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()]
    extra = []  # annotation objects, numbered after the page objects
    for i, page in enumerate(pages):
        content = "".join(f"{g} g {x} {y} {w} {h} re f\n"
                          for x, y, w, h, g in page.get("rects", ())).encode()
        box = " ".join(str(v) for v in page["box"])
        crop = ("/CropBox [%s] " % " ".join(str(v) for v in page["crop"])
                if "crop" in page else "")
        refs = []
        for rect in page.get("annots", ()):
            extra.append(("<< /Type /Annot /Subtype /Square /Rect [%s] /C [1 0 0] "
                          "/F 4 /P %d 0 R >>" % (" ".join(str(v) for v in rect), 3 + 2 * i)).encode())
            refs.append(f"{2 + 2 * n + len(extra)} 0 R")
        annots = f"/Annots [{' '.join(refs)}] " if refs else ""
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [{box}] {crop}{annots}"
                     f"/Rotate {page.get('rotate', 0)} /Resources << >> "
                     f"/Contents {4 + 2 * i} 0 R >>").encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream")
    objs += extra
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


# An A4 sheet with a black rectangle well inside it.
RECT = (150, 300, 200, 250)          # x, y, w, h
RECT_BOX = (150, 300, 350, 550)      # l, b, r, t
A4 = (0, 0, 595, 842)
SAMPLE = make_pdf([{"box": A4, "rects": [RECT + (0,)]}])


def boxes(path, index=0):
    """(mediabox, cropbox, rotation, page count) of a written file."""
    import pypdfium2
    pdf = pypdfium2.PdfDocument(Path(path).read_bytes())
    try:
        page = pdf[index]
        return page.get_mediabox(), page.get_cropbox(), page.get_rotation(), len(pdf)
    finally:
        pdf.close()


def close_to(box, expected, tolerance=1.0):
    return all(abs(a - b) <= tolerance for a, b in zip(box, expected))


def grown(box, by):
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def expect_raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--make-sample":
        Path(sys.argv[2]).write_bytes(SAMPLE)
        print(f"sample written: {sys.argv[2]}")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--check-sample":
        media, _crop, _rot, _n = boxes(sys.argv[2])
        assert close_to(media, grown(RECT_BOX, pc.DEFAULT_MARGIN_MM * pc.MM)), media
        print(f"sample cropped as expected: {[round(v, 1) for v in media]}")
        return 0

    # --- ink bounding box on raw greyscale buffers ---------------------------
    w, h = 10, 6
    blank = bytearray(b"\xff" * (w * h))
    assert pc.ink_bbox(bytes(blank), w, h, w) is None
    img = bytearray(blank)
    img[2 * w + 3] = 0        # (3, 2)
    img[4 * w + 7] = 100      # (7, 4)
    img[1 * w + 9] = 252      # nearly white: paper, at the default threshold
    assert pc.ink_bbox(bytes(img), w, h, w) == (3, 2, 8, 5)
    assert pc.ink_bbox(bytes(img), w, h, w, threshold=255) == (3, 1, 10, 5)
    assert pc.ink_bbox(bytes(img), w, h, w, threshold=50) == (3, 2, 4, 3)
    # row padding (stride > width) holds garbage that must be ignored
    stride = 12
    padded = bytearray()
    for row in range(h):
        padded += img[row * w:(row + 1) * w] + b"\x00\x00"
    assert pc.ink_bbox(bytes(padded), w, h, stride) == (3, 2, 8, 5)
    # ink in the corners
    img = bytearray(blank)
    img[0] = img[-1] = 0
    assert pc.ink_bbox(bytes(img), w, h, w) == (0, 0, w, h)

    # --- small helpers ---------------------------------------------------------
    assert pc.cropped_name(Path("a/tavola.pdf")) == Path("a/tavola_cropped.pdf")
    assert pc.cropped_name(Path("SCAN.PDF")).name == "SCAN_cropped.PDF"
    assert pc.clamp_margin("2,5") == 2.5 and pc.clamp_margin("x") == pc.DEFAULT_MARGIN_MM
    assert pc.clamp_margin(-3) == 0.0 and pc.clamp_margin(1e9) == pc.MAX_MARGIN_MM
    assert pc.clamp_margin(float("nan")) == pc.DEFAULT_MARGIN_MM

    pc.load_engine()  # a clear failure here beats twenty confusing ones below

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # --- plain page: boxes shrink around the rectangle ---------------------
        f = td / "plain.pdf"
        f.write_bytes(SAMPLE)
        seen = []
        res = pc.crop_file(f, margin_mm=0, progress=seen.append)
        assert res.dest == td / "plain_cropped.pdf" and res.pages == 1 and res.cropped == 1
        media, crop, rot, n = boxes(res.dest)
        assert close_to(media, RECT_BOX) and close_to(crop, RECT_BOX), (media, crop)
        assert rot == 0 and n == 1
        assert abs(res.width - 200 / pc.MM) < 1 and abs(res.height - 250 / pc.MM) < 1
        assert pc.describe(res) == "71 × 88 mm"
        # progress climbs monotonically to 1.0
        assert seen == sorted(seen) and seen[-1] == 1.0 and 0.0 < seen[0] <= 0.1
        # the original is untouched
        assert f.read_bytes() == SAMPLE

        # --- margin: grows the box, never past the page -------------------------
        expect_raises(FileExistsError, pc.crop_file, f)
        res = pc.crop_file(f, margin_mm=10, overwrite=True)
        media = boxes(res.dest)[0]
        assert close_to(media, grown(RECT_BOX, 10 * pc.MM)), media
        edge = td / "edge.pdf"
        edge.write_bytes(make_pdf([{"box": A4, "rects": [(0, 300, 100, 100, 0)]}]))
        media = boxes(pc.crop_file(edge, margin_mm=10).dest)[0]
        assert media[0] == 0 and close_to(media, (0, 300 - 10 * pc.MM, 100 + 10 * pc.MM,
                                                  400 + 10 * pc.MM)), media

        # --- rotated pages: boxes stay in unrotated user space ------------------
        for angle in (90, 180, 270):
            r = td / f"rot{angle}.pdf"
            r.write_bytes(make_pdf([{"box": A4, "rotate": angle, "rects": [RECT + (0,)]}]))
            res = pc.crop_file(r, margin_mm=0)
            media, crop, rot, _n = boxes(res.dest)
            assert close_to(media, RECT_BOX) and close_to(crop, RECT_BOX), (angle, media)
            assert rot == angle
            shown = (250, 200) if angle in (90, 270) else (200, 250)  # as displayed
            assert abs(res.width - shown[0] / pc.MM) < 1, (angle, res)
            assert abs(res.height - shown[1] / pc.MM) < 1, (angle, res)

        # --- page whose origin is not (0, 0), rotated on top ---------------------
        shifted = td / "shifted.pdf"
        shifted.write_bytes(make_pdf([{"box": (100, 200, 700, 1000), "rotate": 90,
                                       "rects": [(300, 400, 120, 60, 0)]}]))
        media = boxes(pc.crop_file(shifted, margin_mm=0).dest)[0]
        assert close_to(media, (300, 400, 420, 460)), media

        # --- an existing CropBox limits what is looked at ------------------------
        precrop = td / "precrop.pdf"
        precrop.write_bytes(make_pdf([{
            "box": A4, "crop": (100, 100, 500, 700),
            "rects": [RECT + (0,), (20, 20, 30, 30, 0)]}]))  # 2nd one is outside the CropBox
        media = boxes(pc.crop_file(precrop, margin_mm=0).dest)[0]
        assert close_to(media, RECT_BOX), media

        # --- white paint is not ink ------------------------------------------------
        white = td / "white.pdf"
        white.write_bytes(make_pdf([{"box": A4, "rects": [(0, 0, 595, 842, 1), RECT + (0,)]}]))
        media = boxes(pc.crop_file(white, margin_mm=0).dest)[0]
        assert close_to(media, RECT_BOX), media

        # --- light colours are (a CAD drawing's yellow lines must count) -----------
        light = td / "light.pdf"
        light.write_bytes(make_pdf([{"box": A4, "rects": [RECT + (0.9,)]}]))
        media = boxes(pc.crop_file(light, margin_mm=0).dest)[0]
        assert close_to(media, RECT_BOX), media
        # ... unless the threshold says otherwise
        expect_raises(pc.NothingToCrop, pc.crop_file, light, 0, True, 128)

        # --- several pages: each one gets its own box, blank ones are left alone ----
        multi = td / "multi.pdf"
        multi.write_bytes(make_pdf([
            {"box": A4, "rects": [RECT + (0,)]},
            {"box": A4},
            {"box": (0, 0, 842, 1191), "rotate": 270, "rects": [(400, 500, 50, 80, 0)]},
        ]))
        res = pc.crop_file(multi, margin_mm=0)
        assert res.pages == 3 and res.cropped == 2
        assert pc.describe(res) == "2 of 3 pages"
        assert close_to(boxes(res.dest, 0)[0], RECT_BOX)
        assert close_to(boxes(res.dest, 1)[0], A4, 0.01)
        assert close_to(boxes(res.dest, 2)[0], (400, 500, 450, 580))
        assert boxes(res.dest, 2)[3] == 3

        # --- the document is carried over as it is ---------------------------------------
        # Rendering makes PDFium generate (and add to the document) an appearance
        # stream for every annotation that lacks one: AutoCAD drawings are full of
        # them, and a file saved after rendering came out up to 80% larger.
        notes = td / "notes.pdf"
        squares = [(160 + 9 * k, 310, 166 + 9 * k, 316) for k in range(20)]
        notes.write_bytes(make_pdf([{"box": A4, "rects": [RECT + (0,)], "annots": squares}]))
        assert b"/AP" not in notes.read_bytes()
        res = pc.crop_file(notes, margin_mm=0)
        assert close_to(boxes(res.dest)[0], RECT_BOX)
        written = res.dest.read_bytes()
        assert b"/AP" not in written, "the saved document carries generated appearances"
        assert written.count(b"/Subtype /Square") + written.count(b"/Subtype/Square") == 20
        assert len(written) < 1.2 * notes.stat().st_size, (notes.stat().st_size, len(written))

        # --- nothing to do, nothing written ------------------------------------------
        full = td / "full.pdf"
        full.write_bytes(make_pdf([{"box": A4, "rects": [(0, 0, 595, 842, 0)]}]))
        expect_raises(pc.NothingToCrop, pc.crop_file, full)
        empty = td / "empty.pdf"
        empty.write_bytes(make_pdf([{"box": A4}]))
        expect_raises(pc.NothingToCrop, pc.crop_file, empty)
        assert not pc.cropped_name(full).exists() and not pc.cropped_name(empty).exists()

        # --- not a PDF ------------------------------------------------------------------
        junk = td / "junk.pdf"
        junk.write_bytes(b"this is not a PDF at all\n" * 20)
        expect_raises(pc.CropError, pc.crop_file, junk)
        assert not list(td.glob("*.part")), "temporary files left behind"

        # --- names with spaces, plus signs and accents (and the output is a valid PDF) --
        odd = td / "RC_ie06_ill.+fm_ piano terrà-RC_ie06.pdf"
        odd.write_bytes(SAMPLE)
        res = pc.crop_file(odd)
        assert res.dest.name == "RC_ie06_ill.+fm_ piano terrà-RC_ie06_cropped.pdf"
        assert res.dest.read_bytes().startswith(b"%PDF-")

        # --- folder scan: recursive, case-insensitive, skips our own output ---------------
        (td / "sub").mkdir()
        upper = td / "sub" / "UPPER.PDF"
        upper.write_bytes(SAMPLE)
        (td / "sub" / "notes.txt").write_text("x")
        found = pc.iter_pdf([td])
        assert upper in found and f in found and multi in found
        assert not [x for x in found if x.stem.endswith("_cropped")]
        assert (td / "sub" / "notes.txt") not in found
        # a file named explicitly is taken as it is
        assert pc.iter_pdf([res.dest]) == [res.dest]

        # --- CLI ------------------------------------------------------------------------
        cli = td / "cli"
        cli.mkdir()
        (cli / "a.pdf").write_bytes(SAMPLE)
        (cli / "b.pdf").write_bytes(SAMPLE)
        assert pc.run_cli([cli], False, 5.0, pc.DEFAULT_THRESHOLD) == 0
        assert close_to(boxes(cli / "a_cropped.pdf")[0], grown(RECT_BOX, 5 * pc.MM))
        assert (cli / "b_cropped.pdf").is_file()
        assert pc.run_cli([cli], False, 5.0, pc.DEFAULT_THRESHOLD) == 0   # all skipped
        (cli / "bad.pdf").write_bytes(b"nope")
        assert pc.run_cli([cli], True, 5.0, pc.DEFAULT_THRESHOLD) == 1    # one error
        assert pc.run_cli([td / "missing"], False, 5.0, pc.DEFAULT_THRESHOLD) == 1

    # --- update check helpers (no network) ---------------------------------
    assert pc.parse_version("v1.10.2") == (1, 10, 2)
    assert pc.parse_version("2") == (2,)
    assert pc.is_newer("1.2.1", "1.2.0") and not pc.is_newer("1.2.0", "1.2.0")
    assert pc.is_newer("v2", "1.9.9") and not pc.is_newer("1.2", "1.2.0")
    assets = [{"name": "pdf-cropper-v1.3.0-windows-x64-portable.zip"},
              {"name": "pdf-cropper-setup-v1.3.0-windows-x64.exe"},
              {"name": "pdf-cropper-v1.3.0-linux-x64-portable.tar.gz"}]
    assert pc.pick_asset(assets, installed=True)["name"].endswith(".exe")
    assert pc.pick_asset(assets, installed=False)["name"].endswith(".zip")
    assert pc.pick_asset([], installed=True) is None

    # --- preferences round-trip ----------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        ini = Path(td) / "cfg" / "settings.ini"
        s = pc.Settings(ini)
        assert s.get_bool("check_updates") and s.get("last_update_check") == ""
        assert pc.clamp_margin(s.get("margin_mm")) == pc.DEFAULT_MARGIN_MM
        s.set("check_updates", False)
        s.set("margin_mm", "7.5")
        again = pc.Settings(ini)
        assert not again.get_bool("check_updates")
        assert pc.clamp_margin(again.get("margin_mm")) == 7.5
        assert again.get_bool("native_decorations")  # untouched default survives

    # --- command line -----------------------------------------------------------
    # as re-parsed by the primary GUI instance
    ns, unknown = pc.build_parser().parse_known_args(["--gui", "a.pdf", "--future-flag"])
    assert ns.gui and ns.paths == ["a.pdf"] and unknown == ["--future-flag"]
    ns = pc.build_parser().parse_args(["--margin", "3,5", "--threshold", "200", "x.pdf"])
    assert ns.margin == 3.5 and ns.threshold == 200
    import contextlib
    import io
    for bad in (["--margin", "-1"], ["--margin", "abc"], ["--threshold", "0"]):
        with contextlib.redirect_stderr(io.StringIO()):
            expect_raises(SystemExit, pc.build_parser().parse_args, bad)

    # --- translations: po -> mo compiler and gettext plumbing ----------------
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import compile_po  # noqa: E402
    root = Path(compile_po.ROOT)
    with tempfile.TemporaryDirectory() as td:
        written = compile_po.compile_all(root / "po", Path(td))
        assert [lang for lang, _count, _out in written] == ["it"]
        import gettext
        it = gettext.translation(pc.TEXTDOMAIN, td, languages=["it"])
        assert it.gettext("Queued") == "In coda"
        assert it.gettext("Cropping… {percent}%") == "Ritaglio in corso… {percent}%"
        assert it.gettext("not a translated string") == "not a translated string"
    # every msgid in the catalogue must exist in the source, and every
    # translatable string of the source must be in the catalogue (adjacent
    # string literals split over lines are joined first)
    import re
    source = re.sub(r'"\s*\n\s*"', "", (root / "pdf_cropper.py").read_text(encoding="utf-8"))
    entries = compile_po.parse_po(root / "po" / "it.po")
    missing = [m for m in entries if m and m not in source]
    assert not missing, f"msgids not found in the source: {missing[:5]}"
    used = set(re.findall(r'\b_\("((?:[^"\\]|\\.)*)"\)', source))
    used = {u.encode().decode("unicode_escape").encode("latin-1").decode("utf-8") for u in used}
    untranslated = sorted(used - set(entries))
    assert not untranslated, f"strings missing from po/it.po: {untranslated[:5]}"
    # placeholders must survive translation
    for msgid, msgstr in entries.items():
        if msgid:
            assert sorted(re.findall(r"\{\w+\}", msgid)) == sorted(re.findall(r"\{\w+\}", msgstr)), msgid
    assert pc.detect_language() in pc.LANGUAGES

    # --- icons: gdk-pixbuf must be able to sniff them ------------------------
    # It only looks for the <svg> element in the first 256 bytes; past that the
    # file is "not a valid icon" and `flatpak build-export` fails. A long
    # leading comment is enough to trigger it, so guard the offset here (pure
    # stdlib: the CI test job has no GdkPixbuf).
    icons = sorted((root / "data" / "icons").rglob("*.svg"))
    assert icons, "no icon SVGs found"
    for icon in icons:
        head = icon.read_bytes()
        offset = head.find(b"<svg")
        assert 0 <= offset <= 256, (
            f"{icon.name}: <svg> starts at byte {offset}; gdk-pixbuf only sniffs "
            "the first 256 bytes, so flatpak-builder would reject this icon")

    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
