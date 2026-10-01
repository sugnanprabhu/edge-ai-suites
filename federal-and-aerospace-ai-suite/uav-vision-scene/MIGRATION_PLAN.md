# Migration Plan: Remove Committed Detection Model, Download at Startup

## 1. Background

`uav-vision-scene` is a reference copy of `smart-intersection` and currently ships the
**identical** proprietary model under:

```
src/dlstreamer-pipeline-server/models/intersection/
├── config.json              # OTX/Geti hyperparameters (unused at runtime)
├── confidence_threshold     # unused binary float32 (unused at runtime)
├── label_schema.json        # Geti label schema (unused at runtime)
├── openvino.bin             # 5,100,672 bytes — byte-identical to smart-intersection's original
└── openvino.xml             # 654,412 bytes   — byte-identical to smart-intersection's original
```

This is the same Intel-Geti-trained, INT8-quantized YOLOX (vehicle/pedestrian) model already
migrated out of `smart-intersection` (see that app's `src/dlstreamer-pipeline-server/models/intersection/README.md`
for the equivalent change). The same problems apply here:

- Governed by the restrictive, non-sublicensable **Limited Edge Software Distribution License
  Agreement** (`LICENSE.txt`), not Apache-2.0 — yet the model folder itself carries **no license
  file** of its own.
- Custom-trained in a private Intel Geti project; **no public URL** exists to re-download this
  exact artifact.
- `config.json`, `label_schema.json`, and `confidence_threshold` are confirmed unused by any
  runtime code path (verified via repo-wide grep) — pure leftover Geti export artifacts.
- The Helm init-models script (`chart/templates/dlstreamer-pipeline-server/scripts/init-models.sh`)
  still points at `smart-intersection`'s path, not this app's — a pre-existing copy-paste bug this
  migration also fixes.

## 2. Target State

Replace with the same public, Apache-2.0-licensed Open Model Zoo (OMZ) model already adopted in
`smart-intersection`:

- **Model**: [`person-vehicle-bike-detection-crossroad-1016`](https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/person-vehicle-bike-detection-crossroad-1016/README.md)
  (MobileNetV2 + SSD, precision **FP16-INT8**)
- **License**: Apache License 2.0 (`https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/master/LICENSE`)
- **Labels**: `0`=non-vehicle, `1`=vehicle, `2`=person — matches `sscape_adapter.py`'s existing
  `otype == "person"` / `otype == "vehicle"` checks with no code changes required.
- Model binaries are **no longer committed to git**; they are downloaded at startup time for both
  Docker Compose and Helm deployment paths.

> [!NOTE]
> Open question to confirm before cutover: this model was trained/benchmarked on street-level
> crossroad camera footage. `uav-vision-scene` implies aerial/drone top-down video sources —
> validate detection accuracy on representative UAV footage before relying on this model in
> production. If accuracy is insufficient, the same download-at-startup mechanism below still
> applies; only the model identity/URL changes.

## 3. Step-by-Step Migration

### Step 3.1 — Remove committed model artifacts from git

```bash
cd uav-vision-scene
git rm src/dlstreamer-pipeline-server/models/intersection/openvino.xml
git rm src/dlstreamer-pipeline-server/models/intersection/openvino.bin
git rm src/dlstreamer-pipeline-server/models/intersection/config.json
git rm src/dlstreamer-pipeline-server/models/intersection/label_schema.json
git rm src/dlstreamer-pipeline-server/models/intersection/confidence_threshold
```

Add to `.gitignore` so re-downloaded binaries are never re-committed:

```gitignore
# Downloaded at startup — see models/intersection/README.md
src/dlstreamer-pipeline-server/models/intersection/openvino.xml
src/dlstreamer-pipeline-server/models/intersection/openvino.bin
src/dlstreamer-pipeline-server/models/intersection/.done
```

### Step 3.2 — Add model-proc and provenance docs (tracked in git)

Create `src/dlstreamer-pipeline-server/models/intersection/openvino.json` (DL Streamer
model-proc file):

```json
{
    "json_schema_version": "2.0.0",
    "input_preproc": [],
    "output_postproc": [
        {
            "labels": ["non-vehicle", "vehicle", "person"],
            "converter": "tensor_to_bbox_ssd"
        }
    ]
}
```

Create `src/dlstreamer-pipeline-server/models/intersection/README.md` documenting the model
source, license, and download commands (copy from `smart-intersection`'s equivalent file, update
paths/links as needed).

### Step 3.3 — Add a download script (used by both Compose and Helm paths)

New file: `src/dlstreamer-pipeline-server/models/download_model.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="$(dirname "$0")/intersection"
BASE_URL="https://storage.openvinotoolkit.org/repositories/open_model_zoo/2022.3/models_bin/1/person-vehicle-bike-detection-crossroad-1016/FP16-INT8"

if [ -f "$MODEL_DIR/.done" ]; then
  echo "Model already present, skipping download."
  exit 0
fi

mkdir -p "$MODEL_DIR"
curl -fsSL -o "$MODEL_DIR/openvino.xml" "$BASE_URL/person-vehicle-bike-detection-crossroad-1016.xml"
curl -fsSL -o "$MODEL_DIR/openvino.bin" "$BASE_URL/person-vehicle-bike-detection-crossroad-1016.bin"

# Optional: verify checksums against open_model_zoo model.yml before marking done
touch "$MODEL_DIR/.done"
echo "Model downloaded successfully."
```

Make it executable: `chmod +x src/dlstreamer-pipeline-server/models/download_model.sh`.

### Step 3.4 — Wire the download into the Docker Compose startup flow

Add the call to `install.sh`, before any `docker compose up` instructions are expected to work
(mirrors how `install.sh` already runs `init.sh` and generates secrets/certs):

```bash
# in install.sh, after secrets/cert generation
echo "Downloading detection model..."
./src/dlstreamer-pipeline-server/models/download_model.sh
```

This keeps the Compose flow a single `./install.sh` + `docker compose up -d` experience with no
manual steps, while guaranteeing the model is present on the host before the
`dlstreamer-pipeline-server` bind mount (`./src/dlstreamer-pipeline-server/models/intersection:/home/pipeline-server/models/object_detection/intersection`)
is populated.

> Alternative considered: a dedicated one-shot `model-downloader` service in `docker-compose.yml`
> using `depends_on: condition: service_completed_successfully`. Rejected for this migration to
> keep the change minimal — `install.sh` already owns one-time setup steps (certs, secrets) and is
> the natural home for this one too. Revisit if the model needs to be refreshed without a full
> `install.sh` re-run.

### Step 3.5 — Fix and simplify the Helm init-models script

Replace `chart/templates/dlstreamer-pipeline-server/scripts/init-models.sh` (currently downloads
the whole `smart-intersection` folder from a GitHub tarball — wrong app, and unnecessarily
fetches the entire repo) with a direct OMZ download, removing the dependency on this repo's own
model folder and the earlier path bug:

```sh
if [ -f /data/models/intersection/.done ]; then
  echo ".done file exists, skipping model download"
else
  echo "Downloading detection model from Open Model Zoo..."
  apk add --no-cache curl
  mkdir -p /data/models/intersection
  BASE_URL="https://storage.openvinotoolkit.org/repositories/open_model_zoo/2022.3/models_bin/1/person-vehicle-bike-detection-crossroad-1016/FP16-INT8"
  curl -fsSL -o /data/models/intersection/openvino.xml "$BASE_URL/person-vehicle-bike-detection-crossroad-1016.xml"
  curl -fsSL -o /data/models/intersection/openvino.bin "$BASE_URL/person-vehicle-bike-detection-crossroad-1016.bin"
  cp {{ .Values... path to chart's models dir }}/intersection/openvino.json /data/models/intersection/openvino.json
  touch /data/models/intersection/.done
fi
chown -R 1000:1000 /data
```

The `openvino.json` (model-proc) and `README.md` remain tracked in git and are copied in (small,
license-clean, non-binary files) — only the large binary IR files are fetched at runtime.

### Step 3.6 — Add `model-proc` to all `gvadetect` pipeline definitions

`src/dlstreamer-pipeline-server/config.json` has **12** `gvadetect` occurrences (4 videos × {no
device / GPU / NPU}), each referencing:

```
model=/home/pipeline-server/models/object_detection/intersection/openvino.xml
```

Update all 12 to also pass:

```
model-proc=/home/pipeline-server/models/object_detection/intersection/openvino.json
```

```bash
sed -i 's#model=/home/pipeline-server/models/object_detection/intersection/openvino.xml#model=/home/pipeline-server/models/object_detection/intersection/openvino.xml model-proc=/home/pipeline-server/models/object_detection/intersection/openvino.json#g' \
  src/dlstreamer-pipeline-server/config.json chart/files/dlstreamer-pipeline-server/config.json
```

(Verify both copies stay identical with `diff` afterward, as they are today.)

### Step 3.7 — Documentation updates

- `docs/user-guide/get-started.md`: note that `./install.sh` now also downloads the detection
  model; no separate manual step for the user.
- `docs/user-guide/export-and-optimize-geti-model.md`: either remove (no longer applicable since
  the shipped default is now a public OMZ model) or keep as an optional "bring your own
  Geti-trained model" guide, clearly marked optional/advanced.
- `docs/user-guide/troubleshooting.md` §"NPU Inference Failures with Geti-Trained Models": keep,
  but mark as applicable only if a user substitutes a custom Geti model.

## 4. Validation Plan

1. `rm -rf src/dlstreamer-pipeline-server/models/intersection/{openvino.xml,openvino.bin,.done}`
   to simulate a clean clone.
2. Run `./install.sh` (Compose) and confirm `openvino.xml`/`openvino.bin` are (re)downloaded and
   `sha384sum` matches the OMZ `model.yml` checksums for `FP16-INT8`.
3. `docker compose up -d` and confirm `dlstreamer-pipeline-server` starts without model-load
   errors (`docker logs <container>`).
4. Confirm detections in published MQTT/metadata show `category` values `person` / `vehicle` /
   `non-vehicle` (via `sscape_adapter.py`'s `detectionPolicy`).
5. For Helm: `helm install` on a clean PVC, confirm the init container downloads the model and
   `.done` marker prevents re-download on pod restarts.
6. `python3 -m json.tool` both `config.json` copies to confirm valid JSON after the `sed` edit.

## 5. Rollback Plan

- Revert the git commit(s) from this migration; the original committed binaries return via git
  history.
- No destructive changes are made to volumes/PVCs that would prevent rollback.

## 6. Out of Scope / Follow-ups

- Re-validating detection accuracy on actual drone/aerial footage (flagged above) — may require
  selecting a different pretrained model or a fresh Geti-trained aerial model, in which case only
  Step 3.3/3.5's URL needs to change, not the overall download-at-startup mechanism.
- Auditing other `metro-vision-ai-app-recipe` apps for the same pattern (this repo and
  `smart-intersection` are now handled; check `smart-parking`, `loitering-detection` similarly).
