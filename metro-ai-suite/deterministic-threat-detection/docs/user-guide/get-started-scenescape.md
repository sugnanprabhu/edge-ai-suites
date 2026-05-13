# Get Started: Scenescape with Basler GigE Camera and TSN PTP

This guide explains how to integrate a Basler GigE camera with
[Intel® SceneScape](https://github.com/open-edge-platform/scenescape) using TSN
Precision Time Protocol (PTP) timestamping. The camera's hardware PTP timestamp is
propagated through the GStreamer pipeline and published alongside inference results,
enabling accurate end-to-end latency measurement in a TSN network.

## Hardware Requirements

| Component | Details |
|-----------|---------|
| **Basler ace 2 Camera (A2440-20GM)** | GigE Vision camera with IEEE 1588v2 PTP hardware timestamping support |
| **AXIS RTSP Camera P3265-LVE** | General RTSP camera with optional support of NTP |
| **MOXA TSN Switch** | Managed switch supporting IEEE 802.1AS (gPTP), IEEE 802.1Qbv (Time-Aware Shaper), and IEEE 1588v2 |
| **Arrow Lake Host Machine** | Linux-based system with an Intel i226 TSN-capable network card |

> **Note:** you can choose either basler camera or the RTSP camera for this demo. The Basler camera provides more accurate hardware PTP timestamps, while the RTSP camera relies on software timestamps and NTP synchronization.

## Network Topology

```
Basler GigE Camera/RTSP Camera ──┐
                                 ├──  MOXA TSN Switch  ──  Arrow Lake Host (SceneScape)
Basler GigE Camera/RTSP Camera ──┘
```

The MOXA switch acts as the PTP Grandmaster clock. The host machine and the Basler camera
both synchronize to it. The camera hardware-stamps each frame with the PTP time, which is
then carried through the GStreamer pipeline to SceneScape.

### Logical Roles

| Machine | Role |
|---------|------|
| Arrow Lake Host (Machine 1) | Runs SceneScape and the DL Streamer inference pipeline |
| Traffic Injector (Machine 2) | Injects background traffic with `iperf3` to simulate congestion |

All machines are connected to the MOXA switch and synchronized using PTP.

## NTP vs PTP

### NTP Synchronization (RTSP Camera)
Scenescape supports the NTP synchronized RTSP camera by default. Make sure to set the following NTP setting to `true` in the `scenescape/dlstreamer-pipeline-server/queuing-config.json` for both qcam1 and qcam2 pipelines:

```json
"frame_ntp_config": {
    "useFrameNtpTimestamp": true
},
```

### PTP Synchronization (Basler GigE Camera)
The Basler camera provides more accurate hardware PTP timestamps, but requires additional configuration steps to set up the camera, switch, and host for IEEE 1588v2 PTP. Follow the instructions in the next section to enable PTP support for the Basler camera.
Follow [Configure Basler Camera for Scenescape](./how-to-guides/integrate-basler-camera-with-scenescape.md) before continuing with the rest of the steps in this guide to ensure the camera is properly configured for PTP and SceneScape can read the hardware timestamps.


## End-to-End Testing

### Step 1: Set Up VLANs on the Host

Create VLAN interfaces to isolate critical camera traffic from best-effort traffic on the
TSN switch.

> **Note:** First configure VLAN IDs on the MOXA switch as described in the
> [MOXA VLAN Configuration Guide](./how-to-guides/configure-vlan-on-moxa-switch.md).

```bash
# Replace enp1s0 with your i226 interface name
sudo ip link add link enp1s0 name enp1s0.1 type vlan id 1
sudo ip link set enp1s0.1 type vlan egress-qos-map 0:1
sudo ifconfig enp1s0.1 192.168.127.31 up

sudo ip link add link enp1s0 name enp1s0.5 type vlan id 5
sudo ip link set enp1s0.5 type vlan egress-qos-map 0:5
sudo ifconfig enp1s0.5 192.168.5.31 up
```

> **Note**: if you are using 1588v2 PTP for the time synchronization, make sure to assign any IP address to the default host interface (e.g., `enp1s0`) that is within the same subnet as the camera and switch to ensure the PTP daemon can discover the Grandmaster over UDP.

For detailed instructions, refer to the
[HOST VLAN Configuration Guide](./how-to-guides/create-vlan-on-all-machines.md).

### Step 2: Run SceneScape

```bash
git clone https://github.com/open-edge-platform/scenescape
cd scenescape
make demo
```

### Step 3: Inject Background Traffic

Use iPerf3 to simulate network congestion over the vlan 5. Observe the SceneScape controller logs for
signs of packet loss and video stream degradation as best-effort traffic competes with
the camera stream.

### Step 4: Enable TSN Traffic Shaping

Configure the Time-Aware Shaper (IEEE 802.1Qbv) on the MOXA switch to schedule and
prioritize the camera traffic, protecting it from background congestion.

![MOXA Time Aware Shaper](./_assets/moxa-time-aware-shaper-port-setting.png)

> **Note:** Apply the port setting on the switch port that connects to the host running
> SceneScape.

For detailed instructions, refer to the
[TSN Traffic Shaping Guide](./how-to-guides/enable-tsn-traffic-shaping.md).

## Resources

- [Basler Precision Time Protocol Documentation](https://docs.baslerweb.com/precision-time-protocol)
- [SceneScape Repository](https://github.com/open-edge-platform/scenescape)
