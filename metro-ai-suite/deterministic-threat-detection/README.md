# Deterministic Threat Detection with Time-Sensitive Networking (TSN)

This project demonstrates how **Time-Sensitive Networking (TSN)** protects latency-sensitive
AI and sensor workloads in a shared Ethernet network. It provides two end-to-end use cases,
both built on the same TSN switch infrastructure but targeting different camera types,
time-synchronization protocols, and inference pipelines.

---

## Overview

The sample application shows how TSN can be applied to industrial and edge AI deployments to
deliver consistent, deterministic latency even under network congestion. It covers:

- Multi-camera video acquisition over Ethernet with hardware-level time synchronization
- AI inference on synchronized video frames using the DL Streamer Pipeline Server
- Tracking accuracy measurement (HOTA / MOTA / IDF1 metrics) using Intel® SceneScape
- End-to-end latency measurement under congestion scenarios
- The impact of best-effort background traffic on latency and tracking quality
- Traffic protection using **IEEE 802.1Qbv (Time-Aware Shaper)**

---

## Common Architecture

![Deterministic Threat Detection Architecture](./docs/user-guide/_assets/common-deterministic-threat-detection-architecture.svg)

| Component | Role |
|-----------|------|
| **MOXA TSN Switch (TSN-G5000)** | PTP Grandmaster clock, VLAN segmentation, IEEE 802.1Qbv time-aware traffic shaping |
| **Arrow Lake Host (Intel i226 NIC)** | TSN-capable inference host; clock synchronized to the switch via PTP |
| **Camera(s)** | Video source; supports either RTSP cameras (NTP/gPTP) or Basler GigE cameras (IEEE 1588v2 hardware PTP) |
| **Traffic Injector** | Runs `iperf3` to generate background congestion and demonstrate TSN protection |

---

## Use Cases

### Use Case 1 — Multi-Camera AI Inference with Deterministic Delivery

| | |
|-|-|
| **Camera** | AXIS RTSP Camera P3265-LVE |
| **Time Sync** | IEEE 802.1AS (gPTP) |
| **Inference** | DL Streamer Pipeline Server → MQTT |
| **Measured Output** | End-to-end MQTT latency |
| **TSN Benefit** | Deterministic latency maintained under iperf3 congestion |

RTSP camera streams from two AXIS cameras are processed by the DL Streamer Pipeline Server
for AI inference (person detection). Inference results and simulated sensor telemetry are
published over MQTT with PTP timestamps. An MQTT aggregation node measures and visualizes
end-to-end latency in real time.

By injecting iperf3 background traffic and then enabling the IEEE 802.1Qbv Time-Aware
Shaper on the MOXA switch, the demonstration shows how TSN isolates critical camera and
sensor streams from best-effort congestion.

**Key components** (in `usecases/rtsp-deterministic-inference/`):

| Component | Description |
|-----------|-------------|
| `rtsp_camera_pipeline/` | DL Streamer config and GVAPython script for RTSP capture, PTP timestamping, and MQTT publish |
| `sensor_data_producer/` | Python script simulating a sensor publishing MQTT telemetry with PTP timestamps |
| `mqtt_data_aggregator/` | Dash-based real-time latency visualization dashboard |

**[Get Started — Use Case 1](./docs/user-guide/get-started.md)**

---

### Use Case 2 — SceneScape Multi-Camera Tracking with TSN and PTP

| | |
|-|-|
| **Camera** | Basler ace 2 GigE (A2440-20GM) or AXIS RTSP Camera |
| **Time Sync** | IEEE 1588v2 (hardware PTP timestamps from Basler camera) or NTP (RTSP camera) |
| **Inference** | DL Streamer Pipeline Server → Intel® SceneScape |
| **Measured Output** | HOTA / MOTA / IDF1 tracking accuracy |
| **TSN Benefit** | Tracking accuracy preserved under iperf3 congestion |

The Basler GigE camera hardware-stamps each captured frame with an IEEE 1588v2 PTP
timestamp. A patched `gencamsrc` GStreamer plugin propagates these hardware timestamps
through the DL Streamer pipeline into
[Intel® SceneScape](https://github.com/open-edge-platform/scenescape) for 3D multi-camera
object tracking.

The experiment measures how TSN congestion affects HOTA tracking accuracy (dropped frames
degrade tracking identity continuity) and demonstrates that TSN traffic shaping restores
accuracy to the no-congestion baseline.

**Key components** (in `usecases/scenescape-deterministic-inference/`):

| Component | Description |
|-----------|-------------|
| `basler/patches/` | Three `git apply` patches: `gencamsrc` PTP timestamp propagation, Docker macvlan for Basler subnet, `sscape_adapter` PTP timestamp injection into SceneScape MQTT |
| `hota/configs/` | Pre-built DL Streamer pipeline config for HOTA capture |
| `hota/media/` | SEI-injected, B-frame-free MPEG-TS test videos (`Cam_x1_0_1k_sei.ts`, `Cam_x2_0_1k_sei.ts`) |
| `hota/scripts/` | MQTT capture processor, traffic generator, SEI parser GVAPython plugin, and SceneScape scene setup script |

**[Get Started — Use Case 2](./docs/user-guide/get-started-scenescape.md)**

---

## Prerequisites

### Hardware

- 1 × [MOXA TSN-G5000 Series Managed Switch](https://www.moxa.com/en/products/industrial-network-infrastructure/ethernet-switches/en-50155-switches/tsn-g5004-series)
- 2+ × Arrow Lake Linux machines each fitted with an **Intel i226** TSN-capable NIC
- **Use Case 1:** 2 × [AXIS RTSP Camera P3265-LVE](https://www.axis.com/products/axis-p3265-lve)
- **Use Case 2:** 1–2 × [Basler ace 2 GigE (A2440-20GM)](https://www.baslerweb.com/en/cameras/basler-ace/ace-2/) (or AXIS RTSP cameras)

### Software

- Ubuntu 22.04 LTS or later
- Docker Engine 24.0+ and Docker Compose v2
- Python 3.10+
- `linuxptp` (`sudo apt install linuxptp`)
- `iperf3` (`sudo apt install iperf3`)
- **Use Case 2 only:** Basler pylon SDK 7.x and pylon Viewer

---

## How-to Guides

### Common (Both Use Cases)

| Guide | Description |
|-------|-------------|
| [Configure the MOXA TSN Switch](./docs/user-guide/how-to-guides/common/configure-moxa-switch.md) | First-time switch setup — firmware, ports, and management access |
| [Configure VLAN on MOXA Switch](./docs/user-guide/how-to-guides/common/configure-vlan-on-moxa-switch.md) | Create VLANs for traffic segmentation on the switch |
| [Create VLAN on All Machines](./docs/user-guide/how-to-guides/common/create-vlan-on-all-machines.md) | Create matching VLAN interfaces on Arrow Lake hosts |
| [Enable TSN Traffic Shaping](./docs/user-guide/how-to-guides/common/enable-tsn-traffic-shaping.md) | Configure IEEE 802.1Qbv Time-Aware Shaper on the switch |

### Use Case 1 — RTSP Deterministic Inference

| Guide | Description |
|-------|-------------|
| [Configure PTP (IEEE 802.1AS)](./docs/user-guide/how-to-guides/rtsp-deterministic-inference/configure-ptp-gptp.md) | Synchronize all machines using gPTP (`ptp4l` + `phc2sys`) |
| [Run RTSP Camera Capture and AI Inference](./docs/user-guide/how-to-guides/rtsp-deterministic-inference/run-rtsp-camera-and-ai-inference.md) | Set up the DL Streamer pipeline for RTSP capture and inference |
| [Run the Sensor Data Producer](./docs/user-guide/how-to-guides/rtsp-deterministic-inference/run-sensor-data-producer.md) | Simulate sensor telemetry over MQTT |
| [Run the MQTT Aggregator and Visualization](./docs/user-guide/how-to-guides/rtsp-deterministic-inference/run-mqtt-aggregator-and-visualization.md) | Launch the real-time latency dashboard |
| [Run the Traffic Injector](./docs/user-guide/how-to-guides/rtsp-deterministic-inference/run-traffic-injector.md) | Inject iperf3 background congestion |

### Use Case 2 — SceneScape Deterministic Inference

| Guide | Description |
|-------|-------------|
| [Configure PTP (IEEE 1588v2)](./docs/user-guide/how-to-guides/scenescape-deterministic-inference/configure-ptp-1588v2.md) | Configure MOXA switch and host for 1588v2 UDP PTP |
| [Configure Basler Camera PTP Timestamps](./docs/user-guide/how-to-guides/scenescape-deterministic-inference/configure-basler-ptp-timestamps.md) | Assign static IP and enable hardware PTP in pylon Viewer |
| [Set Up SceneScape with Basler GigE](./docs/user-guide/how-to-guides/scenescape-deterministic-inference/integrate-basler-camera-with-scenescape.md) | Build custom image, apply patches, configure GStreamer pipeline |
| [Measure HOTA Tracking Accuracy](./docs/user-guide/how-to-guides/scenescape-deterministic-inference/scenescape-measuring-hota-metrics-with-tsn.md) | Capture and evaluate HOTA / MOTA / IDF1 metrics |
| [HOTA Script Reference](./docs/user-guide/how-to-guides/scenescape-deterministic-inference/hota-script-reference.md) | CLI reference for all HOTA experiment scripts |

**[Full How-to Guides Index](./docs/user-guide/how-to-guides.md)**

---

## References

- [Intel® SceneScape](https://github.com/open-edge-platform/scenescape)
- [Basler Precision Time Protocol Documentation](https://docs.baslerweb.com/precision-time-protocol)
- [MOXA TSN-G5000 Series Manual](https://www.moxa.com/getmedia/a0db0ef9-2741-4bad-91c6-1ec1827aca64/moxa-tsn-g5000-series-web-console-manual-v2.3.pdf)
- [TrackEval — Multi-Object Tracking Evaluation](https://github.com/JonathonLuiten/TrackEval)
- [Release Notes](./docs/user-guide/release-notes.md)
