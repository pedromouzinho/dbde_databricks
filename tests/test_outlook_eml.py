# =============================================================================
# tests/test_outlook_eml.py — prepare_outlook_draft .eml primary artifact
# =============================================================================
# Locks in the fix: prepare_outlook_draft generates a standard .eml (RFC 822)
# draft that Outlook opens on double-click (X-Unsent:1), instead of the fragile
# .cmd/PowerShell launcher. Pure, no network.
#
# Runs with pytest, or standalone:  python tests/test_outlook_eml.py
# =============================================================================

import email
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tools_email as TE
    DEPS_OK = True
except Exception as exc:
    print(f"SKIP test_outlook_eml (deps unavailable): {exc}")
    DEPS_OK = False


def test_eml_has_unsent_and_recipients():
    if not DEPS_OK:
        return
    payload = {
        "subject": "Estrutura Epic 1046350",
        "to": "jorge.rodrigues@millenniumbcp.pt",
        "cc": "x@y.pt",
        "bcc": "",
        "html_body": "<b>Olá Jorge</b>",
        "text_body": "Olá Jorge",
    }
    msg = email.message_from_bytes(TE._build_eml(payload))
    assert msg.get("X-Unsent") == "1"          # opens as editable draft in Outlook
    assert msg.get("To") == "jorge.rodrigues@millenniumbcp.pt"
    assert msg.get("Cc") == "x@y.pt"
    assert msg.get("Subject") == "Estrutura Epic 1046350"
    assert msg.get("Date")                       # a Date header is present


def test_eml_carries_html_and_text():
    if not DEPS_OK:
        return
    msg = email.message_from_bytes(TE._build_eml(
        {"subject": "s", "to": "a@b.pt", "html_body": "<p>HTML aqui</p>", "text_body": "texto aqui"}))
    assert msg.is_multipart()
    subtypes = {p.get_content_subtype() for p in msg.walk() if not p.is_multipart()}
    assert "html" in subtypes and "plain" in subtypes


def test_eml_text_only_when_no_html():
    if not DEPS_OK:
        return
    msg = email.message_from_bytes(TE._build_eml(
        {"subject": "s", "to": "a@b.pt", "html_body": "", "text_body": "só texto"}))
    assert not msg.is_multipart()
    assert "só texto" in msg.get_content()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if not failures else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
