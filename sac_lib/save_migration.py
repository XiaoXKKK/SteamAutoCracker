"""Per-game identity and one-time Steam Remote Storage import for Goldberg."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

# SteamID64 prefix for a public-universe individual account with the default instance.
# The low 32 bits hold the AccountID; see Valve's CSteamID in steamclientpublic.h.
STEAM_ID_BASE = (
    (1 << 56)    # k_EUniversePublic
    | (1 << 52)  # k_EAccountTypeIndividual
    | (1 << 32)  # k_unSteamUserDefaultInstance
)


def game_is_running(folder):
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    processes = (wintypes.DWORD * 16384)()
    used = wintypes.DWORD()
    if not psapi.EnumProcesses(processes, ctypes.sizeof(processes), ctypes.byref(used)):
        raise OSError("Unable to check running processes.")
    if used.value == ctypes.sizeof(processes):
        raise OSError("Process list was truncated; close other applications and retry.")
    root = Path(folder).resolve()
    for pid in processes[:used.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                if root in Path(buffer.value).resolve().parents:
                    return True
        finally:
            kernel.CloseHandle(handle)
    return False


def account_steam_id(account_id):
    text = str(account_id)
    if not text.isdecimal() or not 0 < int(text) < 2**32:
        raise ValueError("Select a valid Steam account ID.")
    return str(STEAM_ID_BASE + int(text))


def discover_steam_path():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                path = Path(winreg.QueryValueEx(key, "SteamPath")[0])
                if (path / "userdata").is_dir():
                    return str(path)
        except OSError:
            pass
    return ""


def steam_accounts(steam_path):
    userdata = Path(steam_path) / "userdata"
    if not userdata.is_dir():
        raise ValueError("Select the Steam installation folder containing userdata.")
    result = []
    for child in userdata.iterdir():
        if child.is_dir() and child.name.isdecimal():
            try:
                account_steam_id(child.name)
            except ValueError:
                continue
            result.append(child.name)
    return sorted(result, key=int)


def _reject_links(path):
    # Reject junctions as well as symbolic links, including ancestor directories.
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Save migration does not follow links or junctions.")


def _files(path):
    _reject_links(path)
    if not path.exists():
        return []
    if not path.is_dir():
        raise ValueError("Expected a save directory.")
    result = []
    for item in path.rglob("*"):
        _reject_links(item)
        if item.is_file():
            result.append(item)
    return result


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def migrate_account(steam_path, account_id, app_id, dll_directories, appdata=None, game_folder=None):
    """Seed remote storage BEFORE exposing the original identity to the game.

    A successful import is remembered per AppID/account so subsequent application
    does not replace newer emulator saves with an older Steam snapshot.
    Failures restore the previous remote tree, identity files, and import marker.
    """
    steam_id = account_steam_id(account_id)
    if game_folder and game_is_running(game_folder):
        raise ValueError("Close the game before importing saves.")
    if not str(app_id).isdecimal() or int(app_id) <= 0:
        raise ValueError("Invalid AppID.")
    if not dll_directories:
        raise ValueError("No Steam API DLL directory was found.")
    roaming = appdata or os.environ.get("APPDATA")
    if not roaming:
        raise ValueError("APPDATA is unavailable; cannot locate Goldberg saves.")
    source = Path(steam_path).absolute() / "userdata" / str(int(account_id)) / str(int(app_id)) / "remote"
    if not source.parent.parent.is_dir():
        raise ValueError("The saved Steam account is unavailable. Select it again.")
    target = Path(roaming).absolute() / "Goldberg SteamEmu Saves" / str(int(app_id))
    remote = target / "remote"
    marker = target / "sac_import.json"
    identities = []
    for directory in dict.fromkeys(map(str, dll_directories)):
        directory = Path(directory).absolute()
        if (directory / "local_save.txt").exists():
            raise ValueError("Custom local_save.txt detected. Import is stopped to avoid using the wrong save directory.")
        identity = directory / "steam_settings" / "force_steamid.txt"
        _reject_links(identity)
        if identity.exists() and not identity.is_file():
            raise ValueError("Expected an identity file.")
        identities.append(identity)
    _reject_links(target)
    _reject_links(marker)
    source_files = _files(source)
    _files(remote)
    if source == remote or source in remote.parents or remote in source.parents:
        raise ValueError("Source and destination must be separate directories.")
    previous_marker = marker.read_bytes() if marker.exists() else None
    try:
        imported = json.loads(previous_marker) if previous_marker else {}
    except (ValueError, TypeError):
        raise ValueError("Import record is unreadable; restore it or review the saves before retrying.")
    if not isinstance(imported, dict):
        raise ValueError("Invalid import record.")
    already_imported = (imported.get("account_id") == str(int(account_id))
                        and imported.get("app_id") == str(int(app_id)) and remote.is_dir())
    if not source_files and not already_imported:
        raise ValueError("No Steam remote saves were found for this account/AppID. Identity was not changed, to protect game-managed local saves.")

    target.mkdir(parents=True, exist_ok=True)
    backup = target / "sac_backups" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    _reject_links(backup)
    backup.mkdir(parents=True)
    old_identities = [(path, path.read_bytes() if path.exists() else None) for path in identities]
    for index, (path, content) in enumerate(old_identities):
        if content is not None:
            (backup / ("identity-%d.txt" % index)).write_bytes(content)
    (backup / "manifest.json").write_text(json.dumps({
        "source": str(source), "remote": str(remote),
        "identity_files": [str(path) for path in identities],
        "identity_existed": [data is not None for _, data in old_identities],
        "remote_existed": remote.exists(), "already_imported": already_imported,
    }, indent=2), encoding="utf-8")
    if previous_marker is not None:
        (backup / "sac_import.json").write_bytes(previous_marker)

    moved_old = False
    installed_remote = False
    try:
        if not already_imported:
            staged = backup / "staged-remote"
            shutil.copytree(source, staged, copy_function=shutil.copy2)
            staged_files = _files(staged)
            if len(staged_files) != len(source_files):
                raise OSError("Source save set changed during import.")
            for original in source_files:
                if _digest(original) != _digest(staged / original.relative_to(source)):
                    raise OSError("Save copy verification failed.")
            if game_folder and game_is_running(game_folder):
                raise ValueError("The game was started during import. Close it and retry.")
            if remote.exists():
                remote.rename(backup / "remote-before")
                moved_old = True
            staged.rename(remote)
            installed_remote = True
        for identity in identities:
            _atomic_text(identity, steam_id + "\n")
        _atomic_text(marker, json.dumps({"account_id": str(int(account_id)), "app_id": str(int(app_id))}, indent=2))
    except Exception:
        # Keep failed data for inspection instead of recursively deleting it.
        if installed_remote:
            remote.rename(backup / "failed-remote")
        if moved_old:
            (backup / "remote-before").rename(remote)
        for identity, content in old_identities:
            if content is None:
                if identity.exists():
                    identity.unlink()
            elif not identity.exists() or identity.read_bytes() != content:
                identity.write_bytes(content)
        if previous_marker is None:
            if marker.exists():
                marker.unlink()
        else:
            marker.write_bytes(previous_marker)
        raise
    return {"backup": str(backup), "files": 0 if already_imported else len(source_files),
            "already_imported": already_imported, "steam_id": steam_id}
