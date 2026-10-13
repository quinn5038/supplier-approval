"""Windows-user-bound storage for platform credentials.

The encrypted payload lives outside the repository and can only be decrypted
by the Windows user that saved it. No credential value is logged or returned
to the browser.
"""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


class CredentialStoreUnavailable(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_MAGIC = b"ICCEC-DPAPI-1\0"
_ENTROPY = b"ICCEC Supplier Approval/platform-credentials/v1"
_ALLOWED_KEYS = ("SCPMA_COOKIE", "SCPMA_AUTH_TOKEN", "APP_TOKEN", "AGENT_ID")


def _windows_apis():
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_ptr = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [
        blob_ptr, wintypes.LPCWSTR, blob_ptr, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, blob_ptr,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_ptr, ctypes.POINTER(wintypes.LPWSTR), blob_ptr, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, blob_ptr,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def credential_path() -> Path:
    override = os.getenv("SUPPLIER_CREDENTIAL_FILE", "").strip()
    if override:
        return Path(override)
    root = os.getenv("LOCALAPPDATA", "").strip()
    if not root:
        raise CredentialStoreUnavailable("Windows LOCALAPPDATA 不可用")
    return Path(root) / "ICCEC Supplier Approval" / "platform_credentials.bin"


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return value, buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreUnavailable("安全记住凭证仅支持 Windows")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    crypt32, kernel32 = _windows_apis()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source), "ICCEC Supplier Approval", ctypes.byref(entropy),
        None, None, 0x1, ctypes.byref(output))
    _ = source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStoreUnavailable("安全记住凭证仅支持 Windows")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    crypt32, kernel32 = _windows_apis()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1,
        ctypes.byref(output))
    _ = source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def save_credentials(values: dict[str, str]) -> Path:
    clean = {key: str(values.get(key) or "").strip() for key in _ALLOWED_KEYS}
    if not clean["SCPMA_COOKIE"] or not clean["SCPMA_AUTH_TOKEN"]:
        raise ValueError("Authorization 和 Cookie 不能为空")
    raw = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    target = credential_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(_MAGIC + _protect(raw))
    os.replace(temporary, target)
    return target


def load_credentials() -> dict[str, str]:
    target = credential_path()
    if not target.is_file():
        return {}
    payload = target.read_bytes()
    if not payload.startswith(_MAGIC):
        return {}
    try:
        values = json.loads(_unprotect(payload[len(_MAGIC):]).decode("utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {key: str(values.get(key) or "") for key in _ALLOWED_KEYS}


def clear_credentials() -> bool:
    target = credential_path()
    if not target.exists():
        return False
    target.unlink()
    return True


def has_saved_credentials() -> bool:
    values = load_credentials()
    return bool(values.get("SCPMA_COOKIE") and values.get("SCPMA_AUTH_TOKEN"))
