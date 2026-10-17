"""Make text from untrusted repositories safe to print to a terminal.

File and directory names, and values read from repo files, can contain terminal escape
sequences, newlines (to forge extra output lines), bidirectional overrides, or bytes that
are not valid UTF-8 (which Python surfaces as lone surrogates and which crash `print`).
Everything repo-derived is passed through `escape_controls` before it reaches the screen.
"""
from __future__ import annotations

import unicodedata

_LINE_SEPARATORS = "  "


def escape_controls(text: str, keep_newlines: bool = False) -> str:
    """Replace control, format, surrogate, private-use and unassigned characters with visible escapes.

    With keep_newlines=True, "\\n" and "\\t" are kept (for a whole block of already-composed output);
    use the default for single values such as a path, where a newline would forge a new line.
    """
    out: list[str] = []
    for ch in text:
        if keep_newlines and ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch)[0] == "C" or ch in _LINE_SEPARATORS:
            code = ord(ch)
            if code <= 0xFF:
                out.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out)
