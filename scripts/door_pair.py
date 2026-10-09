"""The certificate and key the door serves TLS with, made where lemonfiber is not.

lemonfiber writes `config/door/certificate.pem` and `config/door/key.pem`
before `up`, and the door's Caddyfile names both. A check that validates or
starts the door with no lemonfiber binary involved writes a throwaway pair in
their place, the way an operator running the stack without lemonfiber does:
a P-256 key and a certificate for it, made by `openssl`, valid for a day and
never kept.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import ssl
import subprocess

CERTIFICATE = "certificate.pem"
KEY = "key.pem"
# The names a check asks the door at, so it verifies the name as well as the pin.
NAMES = "subjectAltName=DNS:door,DNS:localhost,IP:127.0.0.1"
# What lemonfiber writes into, relative to the stack root.
DIRECTORY = pathlib.PurePosixPath("config/door")


def write(directory: pathlib.Path) -> list[pathlib.Path]:
    """A throwaway pair written into `directory`, and the paths written.

    Two `openssl` calls rather than one, because `req -newkey ec` is spelled
    differently in LibreSSL and OpenSSL, and `ecparam` then `req -key` is
    spelled the same in both.
    """
    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError("openssl is not on PATH, and the door needs a certificate pair to start")
    key, certificate = directory / KEY, directory / CERTIFICATE
    subprocess.run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                   check=True, capture_output=True)
    key.chmod(0o600)
    subprocess.run([openssl, "req", "-new", "-x509", "-key", str(key), "-out", str(certificate),
                    "-days", "1", "-subj", "/CN=door", "-addext", NAMES],
                   check=True, capture_output=True)
    return [certificate, key]


def fingerprint(certificate: pathlib.Path) -> str:
    """The SHA-256 of a PEM certificate's DER encoding, as lowercase hex."""
    der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()
