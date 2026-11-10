"""Minisign signatures verified with the standard library only: Ed25519 from
RFC 8032's reference verification, over minisign's prehashed form (the
BLAKE2b-512 of the file), and the global signature over the trusted
comment, which names the file. The key is in updates.py.
"""

from __future__ import annotations

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493        # the group's order
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(a, b):
    """Two points in extended coordinates (X, Y, Z, T)."""
    p = _ED_P
    e, h = (a[1] - a[0]) * (b[1] - b[0]) % p, (a[1] + a[0]) * (b[1] + b[0]) % p
    c, d = 2 * a[3] * b[3] * _ED_D % p, 2 * a[2] * b[2] % p
    e, f, g, h = h - e, d - c, d + c, h + e
    return e * f, g * h, f * g, e * h


def _ed_multiply(s, point):
    result = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            result = _ed_add(result, point)
        point = _ed_add(point, point)
        s >>= 1
    return result


def _ed_x(y, sign):
    p = _ED_P
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, p - 2, p) % p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p:
        x = x * _ED_SQRT_M1 % p
    if (x * x - x2) % p:
        return None
    return p - x if (x & 1) != sign else x


def _ed_point(data: bytes):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _ed_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _ED_P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    import hashlib
    if len(signature) != 64:
        return False
    a, r = _ed_point(public), _ed_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _ED_L:
        return False
    p, gy = _ED_P, 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
    gx = _ed_x(gy, 0)
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _ED_L
    left, right = _ed_multiply(s, (gx, gy, 1, gx * gy % p)), _ed_add(r, _ed_multiply(k, a))
    return (left[0] * right[2] - right[0] * left[2]) % p == 0 and (left[1] * right[2] - right[1] * left[2]) % p == 0


def minisign_verify(digest: bytes, signature: str, public_key: str) -> str:
    """The trusted comment of a minisign signature (a .minisig file's text)
    of a file whose BLAKE2b-512 is digest, made with the key whose public
    half is public_key (the base64 line of minisign.pub); ValueError when
    it is not."""
    import base64

    def decode(text: str, length: int) -> bytes:
        try:
            data = base64.b64decode(text.strip(), validate=True)
        except ValueError:
            data = b""
        if len(data) != length:
            raise ValueError("not a minisign signature or key")
        return data

    lines = [line.rstrip("\r") for line in signature.split("\n")]
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") \
            or not lines[2].startswith("trusted comment: "):
        raise ValueError("not a minisign signature")
    key, sig, global_sig = decode(public_key, 42), decode(lines[1], 74), decode(lines[3], 64)
    # "ED": prehashed; "Ed", the legacy form over the whole file, is never made here
    if key[:2] != b"Ed" or sig[:2] != b"ED":
        raise ValueError("not a prehashed Ed25519 signature")
    if sig[2:10] != key[2:10]:
        raise ValueError("signed with another key")
    trusted = lines[2][len("trusted comment: "):]
    if not ed25519_verify(key[10:], digest, sig[10:]):
        raise ValueError("the signature does not match the file")
    if not ed25519_verify(key[10:], sig[10:] + trusted.encode("utf-8"), global_sig):
        raise ValueError("the signature does not match its trusted comment")
    return trusted
