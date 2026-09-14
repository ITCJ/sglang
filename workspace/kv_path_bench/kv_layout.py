"""Shared DeepSeek-V3.1 BF16 MLA page format for the two benchmark nodes."""

LAYERS = 61
PAGE_SIZE = 128
K_DIM = 512
ROPE_DIM = 64
ELEMENT_BYTES = 2
PAGE_BYTES = LAYERS * PAGE_SIZE * (K_DIM + ROPE_DIM) * ELEMENT_BYTES


def page_payload(page: int) -> bytes:
    """One object: [layer, token, compressed KV (512), RoPE (64)]."""
    output = bytearray(PAGE_BYTES)
    offset = 0
    for layer in range(LAYERS):
        for token in range(PAGE_SIZE):
            code = (page * 11 + layer * 7 + token * 3) % 256
            k = bytes((code, 0x3F)) * K_DIM
            rope_code = (code + 91 + (page // 256) * 53) % 256
            rope = bytes((rope_code, 0x3F)) * ROPE_DIM
            output[offset : offset + len(k)] = k
            offset += len(k)
            output[offset : offset + len(rope)] = rope
            offset += len(rope)
    return bytes(output)


def split_page_payload(page: int) -> bytes:
    """One key still holds one page: all compressed KV, then all RoPE."""
    packed = page_payload(page)
    token_bytes = (K_DIM + ROPE_DIM) * ELEMENT_BYTES
    k_bytes = K_DIM * ELEMENT_BYTES
    return b"".join(packed[i : i + k_bytes] for i in range(0, len(packed), token_bytes)) + b"".join(
        packed[i + k_bytes : i + token_bytes] for i in range(0, len(packed), token_bytes)
    )


def page_keys(prefix: str, count: int) -> list[str]:
    return [f"{prefix}/page-{page}" for page in range(count)]
