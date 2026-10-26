"""Mute the default Windows audio output endpoint while audio sharing is sending,
and restore its previous mute state when sending stops.

Safe on non-Windows platforms and when audio/COM libraries are unavailable.
"""

from contextlib import contextmanager
import atexit
import logging
import sys
import threading

LOGGER = logging.getLogger("beamer.audio_mute")

_COM_AVAILABLE = False
comtypes = None
AudioUtilities = None
IAudioEndpointVolume = None

if sys.platform == "win32":
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        _COM_AVAILABLE = True
    except (ImportError, Exception) as _err:
        LOGGER.debug("Audio mute backend unavailable: %s", _err)
        _COM_AVAILABLE = False


def is_available() -> bool:
    """Return True if endpoint volume control is supported and libraries are loaded."""
    return _COM_AVAILABLE


@contextmanager
def _com_session():
    """Ensure COM is initialized on the current thread for the duration of the block."""
    if not _COM_AVAILABLE or comtypes is None:
        yield
        return
    uninit = False
    try:
        comtypes.CoInitialize()
        uninit = True
    except OSError as error:
        # RPC_E_CHANGED_MODE (-2147417850 / 0x80010106): thread is already initialized
        # in a different mode (e.g. MTA), which is safe to use.
        if getattr(error, "winerror", None) != -2147417850:
            LOGGER.warning("comtypes.CoInitialize failed: %s", error)
            raise
    try:
        yield
    finally:
        if uninit:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _get_endpoint_volume():
    """Retrieve the IAudioEndpointVolume interface for the default render device."""
    if not _COM_AVAILABLE or AudioUtilities is None:
        return None
    try:
        speakers = AudioUtilities.GetSpeakers()
        if speakers is None:
            return None
        if hasattr(speakers, "EndpointVolume"):
            return speakers.EndpointVolume
        if hasattr(speakers, "Activate") and IAudioEndpointVolume is not None:
            iface = speakers.Activate(
                IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None
            )
            return iface.QueryInterface(IAudioEndpointVolume)
    except Exception as error:
        LOGGER.warning("Could not access default audio endpoint: %s", error)
        return None
    return None


def get_mute() -> bool | None:
    """Return True if default speakers are muted, False if unmuted, or None if unavailable."""
    if not is_available():
        return None
    try:
        with _com_session():
            epv = _get_endpoint_volume()
            if epv is None:
                return None
            return bool(epv.GetMute())
    except Exception as error:
        LOGGER.warning("Failed to get audio mute state: %s", error)
        return None


def set_mute(mute: bool) -> bool:
    """Set default speakers mute state. Returns True on success, False on failure."""
    if not is_available():
        return False
    try:
        with _com_session():
            epv = _get_endpoint_volume()
            if epv is None:
                return False
            epv.SetMute(1 if mute else 0, None)
            return True
    except Exception as error:
        LOGGER.warning("Failed to set audio mute state: %s", error)
        return False


class SpeakerMute:
    """Manages muting the default render endpoint while active, restoring previous state.

    Thread-safe and idempotent. Mutes only if the device was not already muted,
    and restores only what it changed. Supports context-manager usage.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._managed = False
        self._changed = False

    @property
    def is_active(self) -> bool:
        """True if mute() has been called and restore() has not yet been called."""
        with self._lock:
            return self._managed

    @property
    def changed(self) -> bool:
        """True if this instance actively changed the mute state."""
        with self._lock:
            return self._changed

    def mute(self) -> bool:
        """Mute speakers if not already muted.

        Returns True if speakers are muted (or were already muted),
        or False if muting could not be performed.
        """
        with self._lock:
            if self._managed:
                return True
            if not is_available():
                return False
            current = get_mute()
            if current is None:
                return False
            self._managed = True
            if current:
                # Already muted: leave hardware alone and do not unmute later
                self._changed = False
                return True
            if set_mute(True):
                self._changed = True
                try:
                    atexit.register(self.restore)
                except Exception:
                    pass
                return True
            self._managed = False
            return False

    def restore(self) -> bool:
        """Restore speakers to unmuted only if this instance changed their mute state."""
        with self._lock:
            if not self._managed:
                return False
            try:
                atexit.unregister(self.restore)
            except Exception:
                pass
            success = True
            if self._changed:
                success = set_mute(False)
            self._changed = False
            self._managed = False
            return success

    def stop(self) -> bool:
        """Alias for restore()."""
        return self.restore()

    def close(self) -> bool:
        """Alias for restore()."""
        return self.restore()

    def __enter__(self):
        self.mute()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.restore()
        return False
