"""Stdlib-only Solana key helpers: base58, ed25519 on-curve check, PDA/ATA derivation.

Read-only math; no signing and no key material. Mirrors
solana_program::pubkey::Pubkey::find_program_address (sha256 over
seeds || [bump] || program_id || "ProgramDerivedAddress", first bump from 255
down whose hash is NOT a valid ed25519 point).
"""
import hashlib

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
ATA_PROGRAM = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def b58decode(s):
    if not isinstance(s, str) or not s or any(c not in _INDEX for c in s):
        raise ValueError('invalid base58')
    n = 0
    for c in s:
        n = n * 58 + _INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b''
    return b'\0' * (len(s) - len(s.lstrip('1'))) + body


def b58encode(b):
    n = int.from_bytes(b, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    return '1' * (len(b) - len(b.lstrip(b'\0'))) + out


def pubkey_bytes(s):
    b = b58decode(s)
    if len(b) != 32 or b58encode(b) != s:
        raise ValueError('invalid pubkey')
    return b


def is_on_curve(b):
    """True if 32 bytes decompress to an ed25519 point (as curve25519-dalek decompress)."""
    y = int.from_bytes(b, 'little') & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True  # x = 0 is a valid point
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def create_program_address(seeds, program_id):
    h = hashlib.sha256()
    for s in seeds:
        if len(s) > 32:
            raise ValueError('seed too long')
        h.update(s)
    h.update(pubkey_bytes(program_id))
    h.update(b'ProgramDerivedAddress')
    d = h.digest()
    if is_on_curve(d):
        raise ValueError('on curve')
    return d


def find_program_address(seeds, program_id):
    if len(seeds) > 15:
        raise ValueError('too many seeds')
    for bump in range(255, -1, -1):
        try:
            return b58encode(create_program_address(list(seeds) + [bytes([bump])], program_id)), bump
        except ValueError as e:
            if str(e) != 'on curve':
                raise
    raise ValueError('no viable bump')


def associated_token_address(owner, mint, token_program):
    return find_program_address([pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)],
                                ATA_PROGRAM)[0]
