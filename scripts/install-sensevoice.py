"""Cross-platform installer for the official sherpa-onnx SenseVoice INT8 model."""

from pathlib import Path
import tarfile
import urllib.request


MODEL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL}.tar.bz2"


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    model_root = root / "runtime" / "models"
    target = model_root / MODEL
    if (target / "model.int8.onnx").is_file() and (target / "tokens.txt").is_file():
        print(f"SenseVoice already installed: {target}")
        return
    model_root.mkdir(parents=True, exist_ok=True)
    archive = model_root / f"{MODEL}.tar.bz2"
    print(f"Downloading {URL}")
    urllib.request.urlretrieve(URL, archive)
    with tarfile.open(archive, "r:bz2") as bundle:
        bundle.extractall(model_root, filter="data")
    archive.unlink()
    if not (target / "model.int8.onnx").is_file():
        raise RuntimeError("SenseVoice install completed without model.int8.onnx")
    print(f"SenseVoice installed: {target}")


if __name__ == "__main__":
    main()
