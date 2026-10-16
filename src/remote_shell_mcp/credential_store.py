from __future__ import annotations

import json
import sys
from ctypes import POINTER, Structure, byref, c_ubyte, c_void_p, string_at
from ctypes.wintypes import BOOL, DWORD, LPCWSTR, LPWSTR
from dataclasses import dataclass

import keyring
from keyring.errors import PasswordDeleteError

from .models import CredentialSecret

KEYRING_SERVICE = "codex-remote-shell"
_CRED_TYPE_GENERIC = 1
_CRED_TYPE_DOMAIN_PASSWORD = 2


@dataclass(frozen=True, slots=True)
class WindowsCredential:
    username: str
    password: str


class _FILETIME(Structure):
    _fields_ = [("dwLowDateTime", DWORD), ("dwHighDateTime", DWORD)]


class _CREDENTIALW(Structure):
    _fields_ = [
        ("Flags", DWORD),
        ("Type", DWORD),
        ("TargetName", LPWSTR),
        ("Comment", LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", DWORD),
        ("CredentialBlob", POINTER(c_ubyte)),
        ("Persist", DWORD),
        ("AttributeCount", DWORD),
        ("Attributes", c_void_p),
        ("TargetAlias", LPWSTR),
        ("UserName", LPWSTR),
    ]


def _cred_api() -> tuple[object, object] | None:
    if sys.platform != "win32":
        return None
    from ctypes import windll

    cred_read = windll.advapi32.CredReadW
    cred_read.argtypes = [LPCWSTR, DWORD, DWORD, POINTER(POINTER(_CREDENTIALW))]
    cred_read.restype = BOOL
    cred_free = windll.advapi32.CredFree
    cred_free.argtypes = [c_void_p]
    return cred_read, cred_free


def lookup_windows_credential(host: str) -> WindowsCredential | None:
    api = _cred_api()
    if api is None:
        return None
    cred_read, cred_free = api
    for target, cred_type in (
        (host, _CRED_TYPE_GENERIC),
        (host, _CRED_TYPE_DOMAIN_PASSWORD),
        (f"TERMSRV/{host}", _CRED_TYPE_DOMAIN_PASSWORD),
        (f"TERMSRV/{host}", _CRED_TYPE_GENERIC),
    ):
        found = _cred_read(cred_read, cred_free, target, cred_type)
        if found is not None:
            return found
    return None


def _cred_read(
    cred_read: object, cred_free: object, target: str, cred_type: int
) -> WindowsCredential | None:
    cred_ptr = POINTER(_CREDENTIALW)()
    if not cred_read(target, cred_type, 0, byref(cred_ptr)):  # type: ignore[operator]
        return None
    try:
        cred = cred_ptr.contents
        username = cred.UserName or ""
        if not username or not cred.CredentialBlob or cred.CredentialBlobSize == 0:
            return None
        password = (
            string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            .decode("utf-16-le")
            .rstrip("\x00")
        )
        if not password:
            return None
        return WindowsCredential(username=username, password=password)
    finally:
        cred_free(cred_ptr)  # type: ignore[operator]


class CredentialStore:
    def __init__(self, service_name: str = KEYRING_SERVICE) -> None:
        self.service_name = service_name

    def get(self, host: str) -> CredentialSecret:
        payload = keyring.get_password(self.service_name, host)
        if payload is not None:
            try:
                data = json.loads(payload)
                password = data["password"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"Stored credential for '{host}' is invalid."
                ) from exc
            if not isinstance(password, str) or not password:
                raise RuntimeError(f"Stored credential for '{host}' is invalid.")
            return CredentialSecret(password=password)
        windows = lookup_windows_credential(host)
        if windows is not None:
            return CredentialSecret(password=windows.password)
        return CredentialSecret()

    def set_password(self, host: str, password: str) -> None:
        if not password:
            raise ValueError("password must not be empty.")
        keyring.set_password(
            self.service_name,
            host,
            json.dumps({"password": password}, ensure_ascii=False),
        )

    def delete(self, host: str) -> bool:
        try:
            keyring.delete_password(self.service_name, host)
            return True
        except PasswordDeleteError:
            return False
