"""Utilities for masking sensitive data in logs.

Standardizes on showing only the last 4 characters of secrets to prevent
accidental exposure in logs, stdout, or error messages.
"""


def mask_api_key(api_key: str | None) -> str:
    """Mask an API key, showing only the last 4 characters.
    
    Example: "sk-1234567890abcdef" -> "...cdef"
    """
    if not api_key:
        return "????"
    if len(api_key) <= 4:
        return api_key  # Too short to mask meaningfully
    return f"...{api_key[-4:]}"


def mask_address(address: str | None) -> str:
    """Mask a wallet address, showing only the last 4 characters.
    
    Example: "0x1234567890abcdef" -> "...cdef"
    """
    if not address:
        return "????"
    if len(address) <= 8:
        return address  # Too short to mask meaningfully (e.g., 0x + 6 chars)
    return f"...{address[-4:]}"
