<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# DL Streamer Pipeline Server patches

This directory holds file-level overrides applied on top of the upstream
`DLSTREAMER_PIPELINE_SERVER_IMAGE` base image at build time (see the
`dlstreamer-pipeline-server` service's `dockerfile_inline` in
`docker-compose-pymavlink.yml`). Files here are copied into
`/home/pipeline-server/...` inside the image, overwriting the upstream copy.

## `gstreamer_rtsp_destination.py`

Overrides `src/server/rtsp/gstreamer_rtsp_destination.py`.

**Bug fixed:** `GStreamerRtspDestination` is a single object shared by every
RTSP client connected to a given path (the factory is `set_shared(True)`).
Its `_push_buffer()` treated **any** non-`OK` `Gst.FlowReturn` from
`appsrc.emit('push-buffer', ...)` as fatal and called `_end_stream()`, which
permanently clears the shared `self._app_src`/`self._need_data` state. Because
the shared media isn't necessarily torn down/recreated just because one
`push-buffer` call returned non-OK, this could permanently stop RTP delivery
to all current and future clients on that path — new clients would still
complete the RTSP DESCRIBE/SETUP/PLAY handshake (the media object stays
"alive") but never receive any frame data, requiring a full
`docker restart dlstreamer-pipeline-server` to recover.

`Gst.FlowReturn.FLUSHING` is a transient condition expected during normal
backpressure/pipeline state changes - it does not mean the destination is
broken. The patch skips that single frame and returns instead of tearing the
shared destination down, leaving the existing fatal-error handling
(`Gst.FlowReturn.ERROR`, `NOT_NEGOTIATED`, etc. -> `_end_stream()`) unchanged.

Diff against the upstream file (same patch is applied verbatim in this
override):

```diff
         retval = self._app_src.emit('push-buffer', buffer)
+        if retval == Gst.FlowReturn.FLUSHING:
+            # Transient condition (expected during backpressure/pipeline
+            # state changes). Skip this frame only - do NOT tear down the
+            # shared destination, since a single transient FLUSHING return
+            # should not permanently stop delivery for all clients.
+            self._logger.debug(
+                "Push buffer flushing for stream {}, skipping frame".format(self._rtsp_path))
+            return
         if retval != Gst.FlowReturn.OK:
             self._logger.debug(
                 "Push buffer failed for stream {} with {}".format(self._rtsp_path, retval))
             self._end_stream()
```
