"""Joplin E2EE decryption.

Implements the subset of Joplin's EncryptionService needed to *read* encrypted
sync data: the modern AES-256-GCM methods (KeyV1=8, FileV1=9, StringV1=10).
Reference: joplin/packages/lib/services/e2ee/EncryptionService.ts and crypto.ts.

The legacy SJCL methods (1-7) are not implemented; encountering one raises
UnsupportedEncryptionMethod so callers can surface a clear error instead of
producing garbage.

Flow:
  1. A master key item's `content` is a JSON EncryptionResult {salt,iv,ct},
     encrypted with method 8 (KeyV1) using the user's *master password*. Decrypt
     it to recover the master key plaintext (a hex string).
  2. A note's `encryption_cipher_text` is a JED01 blob: header (method +
     master key id) followed by length-prefixed chunks, each an EncryptionResult
     encrypted with method 10 (StringV1) using the *master key plaintext* as the
     PBKDF2 password. Decrypt and concatenate the chunks.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Encryption method IDs (EncryptionService.ts). Only the modern AES-GCM methods
# are supported here.
METHOD_KEY_V1 = 8      # master key content; password = user master password
METHOD_FILE_V1 = 9     # file/resource content; plaintext encoded as base64
METHOD_STRING_V1 = 10  # string content (notes etc.); plaintext as UTF-16LE

# PBKDF2 iteration counts differ: the master password is low-entropy so it needs
# many iterations; the master key is already 256 bits of entropy so content
# encryption uses very few.
_PBKDF2_ITERATIONS = {
    METHOD_KEY_V1: 220000,
    METHOD_FILE_V1: 3,
    METHOD_STRING_V1: 3,
}
_PBKDF2_KEYLEN = 32          # AES-256
_PBKDF2_DIGEST = "sha512"
_GCM_TAG_BYTES = 16          # auth tag is appended to the ciphertext in `ct`

_HEADER_IDENTIFIER = "JED01"


class JoplinCryptoError(Exception):
    """Base class for decryption failures."""


class UnsupportedEncryptionMethod(JoplinCryptoError):
    def __init__(self, method: int):
        super().__init__(
            f"Encryption method {method} is not supported (only the modern "
            f"AES-256-GCM methods 8/9/10 are implemented; legacy SJCL methods "
            f"1-7 are not)."
        )
        self.method = method


@dataclass
class EncryptionHeader:
    method: int
    master_key_id: str


def _pbkdf2(password: bytes, salt: bytes, method: int) -> bytes:
    iterations = _PBKDF2_ITERATIONS[method]
    return hashlib.pbkdf2_hmac(
        _PBKDF2_DIGEST, password, salt, iterations, dklen=_PBKDF2_KEYLEN
    )


def _decrypt_result(result_json: str, password: bytes, method: int) -> bytes:
    """Decrypt a single EncryptionResult JSON string -> plaintext bytes.

    result_json is {"salt": b64, "iv": b64, "ct": b64}. `ct` is the GCM
    ciphertext with the 16-byte auth tag appended (Web Crypto / cryptography
    both use this layout).
    """
    if method not in _PBKDF2_ITERATIONS:
        raise UnsupportedEncryptionMethod(method)
    try:
        result = json.loads(result_json)
        salt = base64.b64decode(result["salt"])
        iv = base64.b64decode(result["iv"])
        ct = base64.b64decode(result["ct"])
    except (ValueError, KeyError) as e:
        raise JoplinCryptoError(f"Malformed encryption result: {e}") from e

    key = _pbkdf2(password, salt, method)
    try:
        # AESGCM expects ciphertext||tag, which is exactly how Joplin stores ct.
        return AESGCM(key).decrypt(iv, ct, None)
    except Exception as e:  # InvalidTag etc.
        raise JoplinCryptoError(
            "AES-GCM decryption failed - wrong password/master key or corrupt "
            "data."
        ) from e


def _plaintext_to_str(plaintext: bytes, method: int) -> str:
    """Decode decrypted plaintext bytes to a string per the method's encoding.

    Verified against real Joplin Server data (Joplin >= v3, methods 8/10):
      - StringV1 content plaintext is UTF-16LE.
      - A KeyV1 master key decrypts to 256 raw bytes; the *string form* Joplin
        uses as the password for content is the lowercase hex of those bytes.
    """
    if method == METHOD_STRING_V1:
        return plaintext.decode("utf-16-le")
    if method == METHOD_KEY_V1:
        return plaintext.hex()
    if method == METHOD_FILE_V1:
        # File content plaintext is base64-encoded text.
        return plaintext.decode("utf-8")
    raise UnsupportedEncryptionMethod(method)


def decrypt_master_key(content: str, master_password: str) -> str:
    """Decrypt a master key item's `content` -> master key plaintext (hex str).

    `content` is a bare EncryptionResult JSON (no JED header), encrypted with
    method 8 (KeyV1) using the user's master password.
    """
    plaintext = _decrypt_result(
        content, master_password.encode("utf-8"), METHOD_KEY_V1
    )
    return _plaintext_to_str(plaintext, METHOD_KEY_V1)


def parse_header(cipher_text: str) -> Tuple[EncryptionHeader, str]:
    """Parse a JED01 blob's header. Returns (header, remaining_body)."""
    if not cipher_text.startswith(_HEADER_IDENTIFIER[:3]):
        raise JoplinCryptoError("Not a Joplin encrypted blob (missing JED header).")
    # Identifier is 'JED' + 2-digit version.
    identifier = cipher_text[:5]
    if identifier[:3] != "JED" or not identifier[3:].isdigit():
        raise JoplinCryptoError(f"Unexpected encryption header: {identifier!r}")
    pos = 5
    meta_len = int(cipher_text[pos:pos + 6], 16)
    pos += 6
    metadata = cipher_text[pos:pos + meta_len]
    pos += meta_len
    method = int(metadata[0:2], 16)
    master_key_id = metadata[2:34]
    return EncryptionHeader(method=method, master_key_id=master_key_id), cipher_text[pos:]


def _iter_chunks(body: str):
    """Yield each length-prefixed chunk payload from a JED body."""
    pos = 0
    n = len(body)
    while pos < n:
        chunk_len = int(body[pos:pos + 6], 16)
        pos += 6
        yield body[pos:pos + chunk_len]
        pos += chunk_len


def decrypt_cipher_text(cipher_text: str, master_keys_plaintext: Dict[str, str]) -> str:
    """Decrypt a full JED01 blob (e.g. a note's encryption_cipher_text).

    master_keys_plaintext maps master key id -> decrypted master key (hex str).
    The master key string is the PBKDF2 password for the content chunks.
    """
    header, body = parse_header(cipher_text)
    if header.method not in _PBKDF2_ITERATIONS:
        raise UnsupportedEncryptionMethod(header.method)
    key = master_keys_plaintext.get(header.master_key_id)
    if key is None:
        raise JoplinCryptoError(
            f"No master key available for id {header.master_key_id}."
        )
    password = key.encode("utf-8")
    parts: List[str] = []
    for chunk in _iter_chunks(body):
        plaintext = _decrypt_result(chunk, password, header.method)
        parts.append(_plaintext_to_str(plaintext, header.method))
    return "".join(parts)


def build_master_key_store(
    master_keys: List[dict], master_password: str
) -> Dict[str, str]:
    """Decrypt every master key in an info.json `masterKeys` list.

    Returns {master_key_id: plaintext}. Keys that use an unsupported method or
    fail to decrypt are skipped (so one bad/legacy key doesn't block the rest).
    """
    store: Dict[str, str] = {}
    for mk in master_keys:
        mk_id = mk.get("id")
        content = mk.get("content")
        if not mk_id or not content:
            continue
        method = int(mk.get("encryption_method", METHOD_KEY_V1))
        if method != METHOD_KEY_V1:
            # Master keys use KeyV1 in modern Joplin; skip anything else.
            continue
        try:
            store[mk_id] = decrypt_master_key(content, master_password)
        except JoplinCryptoError:
            continue
    return store


# =============================================================================
# Encryption (writing)
# =============================================================================

# Fields kept in cleartext in an encrypted item's sync envelope. Mirrors
# Joplin's BaseItem.serializeForSync keepKeys: foreign keys and the timestamp
# needed to link and synchronise items. Everything else lives in the cipher.
_ENVELOPE_KEEP_KEYS = frozenset({
    "id", "note_id", "tag_id", "parent_id", "share_id", "updated_time",
    "deleted_time", "type_", "is_locked", "extracted_resource_ids",
})


def _str_to_plaintext(text: str, method: int) -> bytes:
    """Encode a string to the plaintext bytes a given method encrypts."""
    if method == METHOD_STRING_V1:
        return text.encode("utf-16-le")
    if method == METHOD_FILE_V1:
        return text.encode("utf-8")
    raise UnsupportedEncryptionMethod(method)


def _encrypt_result(plaintext: bytes, password: bytes, method: int) -> dict:
    """Encrypt bytes into an EncryptionResult dict {salt, iv, ct} (all base64).

    A fresh random salt and 96-bit IV are generated per call. `ct` is the
    AES-256-GCM ciphertext with the 16-byte auth tag appended, matching how
    Joplin (and our decryptor) read it back.
    """
    if method not in _PBKDF2_ITERATIONS:
        raise UnsupportedEncryptionMethod(method)
    salt = os.urandom(32)
    iv = os.urandom(12)
    key = _pbkdf2(password, salt, method)
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def encrypt_cipher_text(
    plaintext_item: str,
    master_key_id: str,
    master_key: str,
    method: int = METHOD_STRING_V1,
) -> str:
    """Encrypt a serialized item into a JED01 blob (single chunk).

    master_key is the decrypted master key (hex string) used as the PBKDF2
    password, exactly as on the decryption side.
    """
    password = master_key.encode("utf-8")
    result = _encrypt_result(
        _str_to_plaintext(plaintext_item, method), password, method
    )
    chunk = json.dumps(result, separators=(",", ":"))
    metadata = f"{method:02x}{master_key_id}"
    header = f"{_HEADER_IDENTIFIER}{len(metadata):06x}{metadata}"
    return f"{header}{len(chunk):06x}{chunk}"


def build_encrypted_item(
    plaintext_item: str, master_key_id: str, master_key: str
) -> str:
    """Turn a plaintext serialized item into its encrypted sync envelope.

    The full plaintext item goes into `encryption_cipher_text`; the envelope
    keeps only the cleartext keepKeys (real values) and blanks everything else,
    mirroring how Joplin stores encrypted items so real Joplin clients can read
    them back.
    """
    cipher = encrypt_cipher_text(plaintext_item, master_key_id, master_key)
    parts = plaintext_item.rsplit("\n\n", 1)
    metadata = parts[1] if len(parts) > 1 else parts[0]

    lines: List[str] = []
    seen_applied = seen_cipher = False
    for line in metadata.split("\n"):
        key = line.split(": ", 1)[0]
        if key == "encryption_applied":
            lines.append("encryption_applied: 1")
            seen_applied = True
        elif key == "encryption_cipher_text":
            lines.append(f"encryption_cipher_text: {cipher}")
            seen_cipher = True
        elif key in _ENVELOPE_KEEP_KEYS:
            lines.append(line)
        else:
            lines.append(f"{key}: ")
    # serialize() omits None fields, so these may be absent - always emit them.
    if not seen_cipher:
        lines.append(f"encryption_cipher_text: {cipher}")
    if not seen_applied:
        lines.append("encryption_applied: 1")
    return "\n".join(lines)
