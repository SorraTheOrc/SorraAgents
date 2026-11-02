/**
 * Shared Python stubs for the voice-input worker tests (SA-0MVRNLSQT006T5GQ).
 *
 * The worker's transcription path needs `numpy` (see
 * `pi-client/voice-input/whisper_worker.py`), and `numpy` is not necessarily
 * importable in the interpreter the tests spawn (`process.env.PYTHON ||
 * "python3"`). Writing this stub into the test's `PYTHONPATH` stub directory
 * keeps the suite hermetic: it passes on an interpreter that has no numpy,
 * exactly as the `faster_whisper` stub already ensures for that dependency.
 *
 * The stub mirrors only the slice of the numpy API the worker uses:
 * `frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0`.
 */

export const NUMPY_STUB = `
import array
import sys


class _Float32Array:
    """Float32 sample sequence supporting len(), iteration and scaling."""

    def __init__(self, samples):
        self._samples = array.array("f", samples)

    def __len__(self):
        return len(self._samples)

    def __iter__(self):
        return iter(self._samples)

    def __getitem__(self, index):
        return self._samples[index]

    def __truediv__(self, other):
        return _Float32Array([sample / other for sample in self._samples])


class _Int16Array:
    def __init__(self, data):
        self._samples = array.array("h")
        self._samples.frombytes(bytes(data))
        if sys.byteorder == "big":
            self._samples.byteswap()

    def __len__(self):
        return len(self._samples)

    def astype(self, dtype):
        if dtype != "float32":
            raise TypeError("stub numpy only supports astype('float32')")
        return _Float32Array(self._samples)


def frombuffer(data, dtype="<i2", count=-1, offset=0):
    if dtype not in ("<i2", "int16"):
        raise TypeError("stub numpy only supports int16 buffers")
    payload = bytes(data)[offset:]
    if count >= 0:
        payload = payload[: count * 2]
    return _Int16Array(payload)
`;

/**
 * A stub numpy whose import fails, simulating an interpreter that has no
 * numpy installed (the worker must report that at `start`, not mid-stream).
 */
export const MISSING_NUMPY_STUB = `raise ImportError("No module named 'numpy'")\n`;
