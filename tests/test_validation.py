"""
Tests for input validation utilities in app/utils.py.

Covers: validate_password, allowed_file, validate_mime.
"""
import io
import pytest
from app.utils import validate_password, allowed_file, validate_mime


# ── validate_password ──────────────────────────────────────────────────────────

class TestValidatePassword:
    def test_valid_password_returns_empty_list(self):
        assert validate_password("Secure99") == []

    def test_too_short(self):
        errors = validate_password("Ab1")
        assert any("8 characters" in e for e in errors)

    def test_no_uppercase(self):
        errors = validate_password("password1")
        assert any("uppercase" in e for e in errors)

    def test_no_digit(self):
        errors = validate_password("Password")
        assert any("number" in e for e in errors)

    def test_multiple_failures(self):
        # Short + no uppercase + no digit
        errors = validate_password("abc")
        assert len(errors) == 3

    def test_exactly_8_chars_valid(self):
        assert validate_password("Secure99") == []

    def test_7_chars_fails(self):
        errors = validate_password("Secur99")
        assert any("8 characters" in e for e in errors)


# ── allowed_file ───────────────────────────────────────────────────────────────

class TestAllowedFile:
    def test_pdf_allowed(self):
        assert allowed_file("document.pdf") is True

    def test_jpg_allowed(self):
        assert allowed_file("photo.jpg") is True

    def test_png_allowed(self):
        assert allowed_file("image.PNG") is True  # case-insensitive

    def test_docx_allowed(self):
        assert allowed_file("resume.docx") is True

    def test_exe_blocked(self):
        assert allowed_file("virus.exe") is False

    def test_no_extension_blocked(self):
        assert allowed_file("nodothere") is False

    def test_empty_string_blocked(self):
        assert allowed_file("") is False

    def test_custom_allowed_set(self):
        assert allowed_file("image.png", {"png"}) is True
        assert allowed_file("image.jpg", {"png"}) is False


# ── validate_mime ──────────────────────────────────────────────────────────────

class TestValidateMime:
    def _make_file(self, header: bytes, padding: int = 100) -> io.BytesIO:
        return io.BytesIO(header + b"\x00" * padding)

    # PDF magic: %PDF
    def test_pdf_magic_accepted(self):
        f = self._make_file(b"\x25\x50\x44\x46")
        assert validate_mime(f, {"pdf"}) is True

    def test_pdf_magic_rejected_when_not_allowed(self):
        f = self._make_file(b"\x25\x50\x44\x46")
        assert validate_mime(f, {"jpg", "png"}) is False

    # JPEG magic: FF D8 FF
    def test_jpeg_magic_accepted(self):
        f = self._make_file(b"\xff\xd8\xff\xe0")
        assert validate_mime(f, {"jpg", "jpeg"}) is True

    def test_jpeg_magic_rejected_for_pdf_only(self):
        f = self._make_file(b"\xff\xd8\xff\xe0")
        assert validate_mime(f, {"pdf"}) is False

    # PNG magic
    def test_png_magic_accepted(self):
        f = self._make_file(b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a")
        assert validate_mime(f, {"png"}) is True

    # DOCX / ZIP magic
    def test_docx_magic_accepted(self):
        f = self._make_file(b"\x50\x4b\x03\x04")
        assert validate_mime(f, {"docx"}) is True

    def test_docx_magic_also_accepted_for_xlsx(self):
        f = self._make_file(b"\x50\x4b\x03\x04")
        assert validate_mime(f, {"xlsx"}) is True

    # DOC / OLE2 magic
    def test_doc_magic_accepted(self):
        f = self._make_file(b"\xd0\xcf\x11\xe0")
        assert validate_mime(f, {"doc"}) is True

    # Unknown magic — conservative allow
    def test_unknown_magic_passes_through(self):
        f = self._make_file(b"\x00\x00\x00\x00")
        assert validate_mime(f, {"pdf", "jpg"}) is True

    def test_seek_is_reset_to_zero(self):
        """After validate_mime, file pointer must be at 0 so the caller can save it."""
        f = self._make_file(b"\x25\x50\x44\x46")
        validate_mime(f, {"pdf"})
        assert f.tell() == 0

    def test_default_allowed_exts_includes_common_types(self):
        """Default set must accept pdf, jpg, png, docx, doc."""
        for header, ext in [
            (b"\x25\x50\x44\x46", "pdf"),
            (b"\xff\xd8\xff\xe0", "jpg"),
            (b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a", "png"),
            (b"\x50\x4b\x03\x04", "docx"),
            (b"\xd0\xcf\x11\xe0", "doc"),
        ]:
            f = self._make_file(header)
            assert validate_mime(f) is True, f"Default set should accept {ext}"
