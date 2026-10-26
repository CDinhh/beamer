"""Capture what this PC plays, leaving out Beamer's own sound.

Beamer plays the sound it receives from other PCs. If it then sent everything this PC plays, that
sound would travel back to where it came from, and round again. Windows 10 (2004) and 11 can capture
"all processes except this one" (process loopback), which is what this does. Without it (older
Windows, or a failure) `available()` is False and the caller falls back to plain loopback.
"""

import ctypes
import logging
import os
import sys
import threading
from ctypes import POINTER, Structure, Union, byref, c_ubyte, c_uint32, c_uint64, c_ulong, c_void_p, c_wchar_p, windll

LOGGER = logging.getLogger("beamer.loopback")

RATE = 48000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2

_VT_BLOB = 65
_MODE_EXCLUDE_TREE = 1
_ACTIVATION_PROCESS_LOOPBACK = 1
_FLAGS = 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000  # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
_SILENT = 0x2
_HNS_20MS = 200000

try:
    if sys.platform != "win32":
        raise ImportError("Windows only")
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject
    from pycaw.api.audioclient import IAudioClient, WAVEFORMATEX

    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


if _AVAILABLE:
    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetBuffer",
                      (["out"], POINTER(POINTER(c_ubyte)), "data"), (["out"], POINTER(c_uint32), "frames"),
                      (["out"], POINTER(c_ulong), "flags"), (["out"], POINTER(c_uint64), "devicePosition"),
                      (["out"], POINTER(c_uint64), "qpcPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
        )

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = (
            COMMETHOD([], HRESULT, "GetActivateResult",
                      (["out"], POINTER(HRESULT), "result"), (["out"], POINTER(POINTER(IUnknown)), "interface")),
        )

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = (
            COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "operation")),
        )

    class IAgileObject(IUnknown):
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_ = ()

    class _Done(COMObject):
        """Tells the waiting thread that Windows has finished activating the audio client."""
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self):
            super().__init__()
            self.finished = threading.Event()

        def ActivateCompleted(self, operation):
            self.finished.set()
            return 0


class _ProcessParams(Structure):
    _fields_ = [("pid", c_ulong), ("mode", c_ulong)]


class _ActivationParams(Structure):
    _fields_ = [("kind", c_ulong), ("process", _ProcessParams)]


class _Blob(Structure):
    _fields_ = [("size", c_ulong), ("data", c_void_p)]


class _PropVariantValue(Union):
    _fields_ = [("blob", _Blob), ("pad", c_ubyte * 16)]


class _PropVariant(Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", _PropVariantValue)]


def _activate_client():
    """An IAudioClient that captures every process except this one."""
    params = _ActivationParams(_ACTIVATION_PROCESS_LOOPBACK, _ProcessParams(os.getpid(), _MODE_EXCLUDE_TREE))
    variant = _PropVariant()
    variant.vt = _VT_BLOB
    variant.value.blob = _Blob(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p))
    done = _Done()
    operation = POINTER(IActivateAudioInterfaceAsyncOperation)()
    windll.Mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
        c_wchar_p, POINTER(GUID), POINTER(_PropVariant), c_void_p, POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    windll.Mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.c_long
    handler = done.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    result = windll.Mmdevapi.ActivateAudioInterfaceAsync(
        "VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(variant),
        ctypes.cast(handler, c_void_p), byref(operation))
    if result != 0:
        raise OSError("Process loopback is not available (0x%08X)" % (result & 0xFFFFFFFF))
    if not done.finished.wait(5):
        raise OSError("Windows did not finish setting up process loopback")
    status, interface = operation.GetActivateResult()
    if status != 0:
        raise OSError("Process loopback was refused (0x%08X)" % (status & 0xFFFFFFFF))
    return interface.QueryInterface(IAudioClient)


class LoopbackCapture:
    """Calls `on_chunk(pcm)` with 48 kHz 16-bit stereo, for what other programs play. Silence is skipped."""

    def __init__(self, on_chunk):
        self._on_chunk = on_chunk
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="beamer-loopback", daemon=True)

    @property
    def thread(self):
        """The capturing thread, so a caller can tell if it stopped by itself."""
        return self._thread

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(10)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(3)

    def _run(self) -> None:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        client = capture = None
        try:
            client = _activate_client()
            fmt = WAVEFORMATEX()
            fmt.wFormatTag, fmt.nChannels, fmt.nSamplesPerSec = 1, CHANNELS, RATE
            fmt.nAvgBytesPerSec, fmt.nBlockAlign, fmt.wBitsPerSample, fmt.cbSize = RATE * FRAME_BYTES, FRAME_BYTES, 16, 0
            client.Initialize(0, _FLAGS, _HNS_20MS, 0, byref(fmt), None)
            event = windll.kernel32.CreateEventW(None, False, False, None)
            client.SetEventHandle(event)
            capture = client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
            client.Start()
        except Exception as error:
            self._error = error
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                windll.kernel32.WaitForSingleObject(event, 100)
                while capture.GetNextPacketSize() > 0:
                    data, frames, flags, _position, _qpc = capture.GetBuffer()
                    size = frames * FRAME_BYTES
                    pcm = b"" if flags & _SILENT else ctypes.string_at(data, size)
                    capture.ReleaseBuffer(frames)
                    if pcm:
                        self._on_chunk(pcm)
        except Exception:
            LOGGER.exception("Capture stopped")
        finally:
            try:
                client.Stop()
            except Exception:
                pass
            windll.kernel32.CloseHandle(event)
            comtypes.CoUninitialize()
