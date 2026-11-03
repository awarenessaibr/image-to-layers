# Image to Layers

This project turns a reference image plus one mask per layer into a Layer Studio composition or a ZIP that can be imported by the studio importer. It can also recover missing layers from an aligned studio export that still contains the final reference image.

## Quick start

```bash
python3 tools/image-to-layers.py \
  --reference path/to/reference.png \
  --masks path/to/masks \
  --name "My Scene" \
  --out data/projects
```

This writes a project composition to:

```text
data/projects/My-Scene/compositions/My-Scene.composition.json
```

To generate a ZIP for Studio's import flow instead:

```bash
python3 tools/image-to-layers.py \
  --reference path/to/reference.png \
  --masks path/to/masks \
  --name "My Scene" \
  --mode zip
```

## Knockout background mode

Use knockout mode when the full final image is already available as `--reference` and a cut-out of the thing to remove is available as `--knockout`.

```bash
python3 tools/image-to-layers.py \
  --reference path/to/final.png \
  --knockout path/to/cutout.png \
  --name "My Scene" \
  --out data/projects \
  --knockout-underlap 1
```

This keeps the pixels from the final image and removes the silhouette of the cut-out. If the cut-out is flattened on white, the script treats border-connected white as empty and preserves enclosed white areas such as paper inside a frame. `--knockout-underlap` shrinks the hole by a few pixels so the background tucks under the cut-out edge. `--knockout-keep-cutout` additionally emits the cut-out as a separate layer for pairing and matching.

Some objects inside the cut-out silhouette may need to stay in the background (for example a chair the subject is holding). Pass seed points with `--knockout-exclude` and every object touching a seed is kept instead of removed:

```bash
python3 tools/image-to-layers.py \
  --reference path/to/final.png \
  --knockout path/to/cutout.png \
  --knockout-exclude "0.42,0.61;0.55,0.28" \
  --name "My Scene" \
  --out data/projects
```

Seeds are normalized `x,y` pairs; the whole connected object under a seed is preserved. In the web UI these seeds come from clicking objects on the pink knockout preview, which also redraws as you move Underlap, Feather, and Dilate. Each tile in the Result grid has its own download button in addition to the ZIP/JSON download.

## Recover missing layers from an aligned export

When layers are removed from a composition, the base image can be left with holes. If you have the complete final image and the studio's aligned export, the recovery mode can turn those holes back into layers:

```bash
python3 tools/image-to-layers.py \
  --reference path/to/final.png \
  --export path/to/aligned-export.png \
  --name "My Scene" \
  --out data/projects
```

The tool:

- Detects holes from the export's transparency. If the export is opaque, it falls back to near-white pixels that differ from the final image.
- Drops edge-touching frames and regions smaller than `--recover-min-area`.
- Cuts each remaining region from the final image as `missing-01.png`, `missing-02.png`, …
- Keeps the aligned export as a visible `Aligned export` base layer, so opening the composition shows the whole scene again.
- Writes the final image as the hidden `Final image` reference layer.
- Saves `missing-preview.png`, a final-image preview with the detected regions highlighted.

Recovered projects use a `<name>-recovered` folder and composition name so they do not overwrite the original. With `--mode zip`, the ZIP contains only the missing cutouts and a `<name>-recovered-missing-preview.png` is written next to it.

Useful options:

- `--recover-holes auto|alpha|white` — hole source (default `auto`).
- `--recover-min-area 0.1` — minimum region area as a percentage of the canvas.
- `--recover-keep-border` — keep regions that touch the image border.
- `--recover-alpha 32`, `--recover-white 244`, `--recover-diff 16` — detection thresholds.
- `--keep-base-alpha` — keep a uniform partial alpha on the export instead of normalizing it to opaque.

The same workflow is available in the UI under **Recover missing**: add the final image as the reference, add the aligned export, then run. A live magenta overlay previews the holes on the canvas before the run.

## Mask contract

- One mask PNG per layer
- Filename stem becomes the layer name
- Mask can be RGBA/LA with alpha values or an 8-bit grayscale image
- Pixels above 127 are treated as inside the layer

## Notes

- The generated composition keeps the reference image as a hidden reference layer so missed regions remain visible as white.
- Layer order is based on role heuristics and filename classification, matching the studio's conventions.
- Character-part names are recognized as a convenience for rigging and auto-detection.
- Point prompts currently create approximate circular masks; they do not invoke SAM. Use Automatic SAM for object-aware segmentation.

## Point-prompt groups

Point-prompt mode is organized around named groups:

1. Press **Add layer group**, name it (for example `man`, `books`, `window`), and confirm.
2. Click the image to add positive points. The translucent on-canvas area shows exactly what the group will become; `Alt`+click adds negative points that cut into it.
3. Press **Review & run**. A confirmation dialog lists every group with its name, positive/negative point count, and approximate image coverage.
4. Choose **Confirm & segment** to write the cutouts, or **Back to edit** to keep adjusting.

Each point-prompt layer is written with an explicit `segmentGroup` equal to its name, so Studio shows the named group instead of collapsing it into a generic layer.

## Automatic SAM names and groups

Automatic SAM does not return semantic labels, so the tool derives readable, stable names from each mask's shape, position, and colour. Masks are grouped into `Background`, `Books`, `Foreground`, `Subject`, `Props`, `Effects`, and `Details`, and named `<group>-NN` (for example `background-01`, `book-02`, `subject-01`).

Project compositions also write a `segmentGroup` on every automatic layer, so Studio's segment list shows real groups instead of one giant `mask` group. ZIP mode uses the same group-prefixed filenames; Studio's stem-based grouping then keeps each group together after **Import ZIP + image**.

## Optional SAM 2 setup

Mask upload and point-prompt fallback only need Pillow. Automatic SAM requires Meta SAM 2, PyTorch, TorchVision, and NumPy. Use a supported Python version (3.10 or newer; Python 3.12 is a practical choice), then install SAM 2 in the same environment used to start the server:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -r requirements-sam.txt
PORT=8009 HOST=127.0.0.1 .venv/bin/python image-to-layers-server.py
```

On first use, SAM 2 downloads the selected checkpoint from Hugging Face. The default is SAM 2.1 Large; choose Tiny or Small in the UI for a smaller download and faster inference. Automatic device selection prefers CUDA, then Apple MPS, then CPU. CPU inference works but can be slow.

If SAM 2 is not installed, Automatic SAM reports the missing setup instead of returning placeholder masks.
