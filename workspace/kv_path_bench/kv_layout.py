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
            rope = bytes(((code + 91) % 256, 0x3F)) * ROPE_DIM
            output[offset : offset + len(k)] = k
            offset += len(k)
            output[offset : offset + len(rope)] = rope
            offset += len(rope)
    return bytes(output)


def page_keys(prefix: str, count: int) -> list[str]:
    return [f"{prefix}/page-{page}" for page in range(count)]
