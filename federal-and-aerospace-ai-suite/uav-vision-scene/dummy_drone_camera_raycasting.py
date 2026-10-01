#!/usr/bin/env python3

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Dummy multi-camera drone -> Scenescape external_source MQTT adapter that
ray-casts pixel bounding boxes to world coordinates itself, using
Scenescape's own built-in geometry library.

This is deliberately the opposite design from
tools/px4_sih_demo/dummy_drone_camera_simulator.py, which sends raw pixel
bounding boxes plus a per-message pose override ('extrinsics') to the
camera-detector topic and lets the Scene Controller reconstruct each
detection's ground position (controller/src/controller/scene.py
_cameraWithPoseOverride() / moving_object.py's cameraPointToWorldPoint()
call on the bounding box's bottom-center point). That adapter therefore
uses a lightweight, dependency-free re-implementation of the pinhole/
quaternion math and never imports scene_common.transform.

This script instead performs that exact same reconstruction itself, in the
adapter process, using the real scene_common.transform.CameraIntrinsics /
CameraPose classes (the same classes controller/src/controller/scene.py and
controller/src/controller/moving_object.py use), then publishes the
already-ray-cast world positions as plain external_source objects (no
camera registration, no detector topic, no 'extrinsics' override involved
at all). Per camera per tick:

  1. A dummy ground target is picked near where that camera's rigidly
     mounted optical axis currently crosses the ground plane (a small
     deterministic wobble is added so it visibly moves), then forward-
     projected to a synthetic pixel bounding box (_project_to_pixels(),
     the same lightweight pinhole model as the sibling script) - this
     stands in for a real onboard detector's output.
  2. That pixel bounding box is converted back to a world point with
     Scenescape's own library: CameraIntrinsics.mapPixelToNormalizedImagePlane()
     to undistort/normalize the box, then CameraPose.cameraPointToWorldPoint()
     on its bottom-center point to intersect the ground plane - exactly the
     two calls moving_object.py's mapObjectDetectionToWorld()/camLoc make
     for a real 'bounding_box_px' detection.
  3. The recovered world point (should closely match the original ground
     target, modulo any simulated pixel rounding) is published as an
     external_source object, alongside the drone's own vehicle marker.

Because scene_common.transform pulls in cv2/open3d/fast_geometry, this
script needs an environment where those are actually importable - the bare
host/venv the sibling lightweight adapters target is NOT enough by itself
(scene_common/src/fast_geometry/ is only source + a compiled .so; it is not
a valid importable package - missing __init__.py - until installed/built).
Two supported options:

  1. Any Python environment where the scene_common package has actually
     been installed/built with its native deps (cv2, open3d, fast_geometry
     with a proper __init__.py) - e.g. tests/.venv created by this repo's
     test tooling:
       source tests/.venv/bin/activate
       PYTHONPATH=scene_common/src python3 \\
         tools/external_source_adapters/dummy_drone_camera_raycasting.py

  2. Inside a Scenescape controller/scene container image, where
     scene_common is pip-installed as part of the image build:
       docker run --rm -it --network scenescape \\
         -v "$(pwd)/tools:/tools:ro" \\
         -e SCENESCAPE_SOURCE_ID=drone-raycast-1 \\
         -e SCENESCAPE_MQTT_AUTH=/run/secrets/controller.auth \\
         -e SCENESCAPE_ROOT_CERT=/run/secrets/certs/scenescape-ca.pem \\
         intel/scenescape-controller:<version> \\
         python3 /tools/external_source_adapters/dummy_drone_camera_raycasting.py

Camera rig model: same 3 fixed cameras (front/back/bottom), gimbal angles
and mount offset as tools/px4_sih_demo/dummy_drone_camera_simulator.py's
DEFAULT_GIMBAL_DEG / DEFAULT_MOUNT_OFFSET_M, reproducing a settings.json
shaped like:
  CameraDefaults.Gimbal:                           Pitch=-20, Roll=0 (front)
  Vehicles.<vehicle>.Cameras.back_center.Gimbal:   Pitch=-20, Yaw=180
  Vehicles.<vehicle>.Cameras.bottom_center.Gimbal: Pitch=-90
All three cameras share CameraDefaults.CaptureSettings (1920x1080, 90 deg
FOV) - the front camera uses those defaults verbatim; only the gimbal is
overridden per role.

Contract fields for 'external_source': docs/user-guide/microservices/
controller/data_formats.md. Requires SCENESCAPE_SOURCE_ID to be listed in
the controller's CONTROLLER_TRUSTED_POSITIONING_SOURCES.

Environment variables (mirrors the other tools/external_source_adapters/*.py
and tools/px4_sih_demo/dummy_drone_camera_simulator.py scripts):
  SCENESCAPE_MQTT_AUTH        Required. user:password or path to auth file.
  SCENESCAPE_ROOT_CERT        Required. Path to Scenescape CA certificate.
  SCENESCAPE_SOURCE_ID        Required. Trusted external_source id for both
                              the drone marker and its cameras' detections.
  SCENESCAPE_BROKER           Optional. Broker host (default "localhost").
  SCENESCAPE_BROKER_PORT      Optional. Broker port (default 1883).
  SCENESCAPE_MQTT_INSECURE    Optional. "1"/"true" to skip TLS verification.
  SCENESCAPE_THING_TYPE       Optional. external_source category for the
                              drone marker itself (default "vehicle").
  SIM_CAMERA_ROLES            Optional. Comma-separated subset of
                              front,back,bottom to simulate (default: all
                              three).
  SIM_DETECTION_CATEGORY_<ROLE>  Optional per-role override (FRONT/BACK/
                              BOTTOM) for the category published for that
                              camera's ray-cast detection (default
                              "person").
  SIM_RADIUS_M, SIM_PERIOD_S, SIM_FLIGHT_ALT_M, SIM_CLIMB_TIME_S
                              Optional. Same meaning/defaults as in
                              dummy_drone_camera_simulator.py (circular
                              flight around a scene-local home point).
  SIM_HOME_X_M, SIM_HOME_Y_M   Optional. Scene-local (x, y) of the flight
                              circle's center, metres. Default: the scene's
                              actual map center (map_extent_m/2 from
                              tools/px4_sih_demo/px4_sih_config.json, the
                              "home" convention used by
                              setup_geospatial_drone_scene.py), not (0, 0) -
                              the scene origin (0, 0) is the map's
                              bottom-left corner, not its center, so flying
                              around it looks off-center/clipped against
                              one edge of the map.
  SIM_CAMERA_FOV_DEGREES, SIM_CAMERA_WIDTH, SIM_CAMERA_HEIGHT
                              Optional. Shared camera intrinsics (default
                              90.0 deg, 1920x1080).
  SIM_MOUNT_FORWARD_M, SIM_MOUNT_RIGHT_M, SIM_MOUNT_UP_M
                              Optional. Shared camera mount offset, metres
                              (default 0.50, 0.0, 0.15 - settings.json
                              X=0.50, Y=0.0, Z=-0.15).
  SIM_GIMBAL_PITCH_DEG_<ROLE>, SIM_GIMBAL_YAW_DEG_<ROLE>,
  SIM_GIMBAL_ROLL_DEG_<ROLE>   Optional per-role gimbal overrides (FRONT/
                              BACK/BOTTOM), same defaults as
                              dummy_drone_camera_simulator.py's
                              DEFAULT_GIMBAL_DEG.
  SIM_BOX_SIZE_PX             Optional. Synthetic dummy bounding box side
                              length, pixels (default 60.0).
  SIM_WOBBLE_RADIUS_M         Optional. Radius of the ground target's slow
                              circular wobble around each camera's optical-
                              axis ground crossing, metres (default 2.0).
  SIM_WOBBLE_PERIOD_S         Optional. Period of that wobble, seconds
                              (default 6.0).
  PUBLISH_HZ                  Optional. Publish rate (default 5).
  LOG_LEVEL                   Optional. Logging level (default INFO).

Run:
  python3 tools/external_source_adapters/dummy_drone_camera_raycasting.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

from scene_common.geometry import Point, Rectangle
from scene_common.mqtt import PubSub
from scene_common.timestamp import get_iso_time
from scene_common.transform import CameraIntrinsics, CameraPose

logger = logging.getLogger(__name__)

CAMERA_ROLES = ("front", "back", "bottom")

# Reproduces CameraDefaults.Gimbal (front) and Vehicles.<vehicle>.Cameras.
# {back_center,bottom_center}.Gimbal from a settings.json of that shape -
# identical defaults to dummy_drone_camera_simulator.py's DEFAULT_GIMBAL_DEG.
DEFAULT_GIMBAL_DEG = {
  "front": {"pitch": -20.0, "yaw": 0.0, "roll": 0.0},
  "back": {"pitch": -20.0, "yaw": 180.0, "roll": 0.0},
  "bottom": {"pitch": -90.0, "yaw": 0.0, "roll": 0.0},
}

# Reproduces CameraDefaults X=0.50, Y=0.0, Z=-0.15 (AirSim NED, so -0.15 in
# Z means 0.15m *up*) - shared by all three cameras.
DEFAULT_MOUNT_OFFSET_M = {"forward": 0.50, "right": 0.0, "up": 0.15}

DEFAULT_DETECTION_CATEGORY = {"front": "vehicle", "back": "person", "bottom": "vehicle"}

IDENTITY_QUAT = (0.0, 0.0, 0.0, 1.0)


def _env(name, default=None, required=False):
  value = os.environ.get(name, default)
  if required and not value:
    raise SystemExit(f"Missing required environment variable: {name}")
  return value


def _matrix_to_quat(matrix):
  """3x3 rotation matrix to a (x, y, z, w) quaternion (standard trace method).

  Only used to build the *forward* camera rig geometry (rotation of the
  synthetic rig model itself, not the ray-cast) - the actual pixel->world
  reconstruction below always goes through scene_common.transform's
  CameraPose, never through this quaternion directly.
  """
  m = matrix
  trace = m[0, 0] + m[1, 1] + m[2, 2]
  if trace > 0:
    s = 0.5 / math.sqrt(trace + 1.0)
    w = 0.25 / s
    x = (m[2, 1] - m[1, 2]) * s
    y = (m[0, 2] - m[2, 0]) * s
    z = (m[1, 0] - m[0, 1]) * s
  elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
    s = 2.0 * math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])
    w = (m[2, 1] - m[1, 2]) / s
    x = 0.25 * s
    y = (m[0, 1] + m[1, 0]) / s
    z = (m[0, 2] + m[2, 0]) / s
  elif m[1, 1] > m[2, 2]:
    s = 2.0 * math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])
    w = (m[0, 2] - m[2, 0]) / s
    x = (m[0, 1] + m[1, 0]) / s
    y = 0.25 * s
    z = (m[1, 2] + m[2, 1]) / s
  else:
    s = 2.0 * math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])
    w = (m[1, 0] - m[0, 1]) / s
    x = (m[0, 2] + m[2, 0]) / s
    y = (m[1, 2] + m[2, 1]) / s
    z = 0.25 * s
  return (x, y, z, w)


def _gimbal_quaternion(heading_rad, pitch_deg, yaw_offset_deg, roll_deg):
  """Quaternion (x, y, z, w) and forward unit vector (world axes) for a
  camera rigidly mounted on a vehicle whose horizontal heading is
  heading_rad, offset by a fixed gimbal (pitch, yaw, roll) in AirSim
  convention (pitch negative = tilt down; yaw offset is added to the
  vehicle's heading). Camera-local axes follow the OpenCV convention
  (x=right, y=down, z=forward) - identical to
  dummy_drone_camera_simulator.py's _gimbal_quaternion().
  """
  total_yaw = heading_rad + math.radians(yaw_offset_deg)
  pitch_rad = math.radians(pitch_deg)
  roll_rad = math.radians(roll_deg)

  forward_horizontal = np.array([math.sin(total_yaw), -math.cos(total_yaw), 0.0])
  tilt = -pitch_rad
  forward = math.cos(tilt) * forward_horizontal + math.sin(tilt) * np.array([0.0, 0.0, -1.0])
  forward = forward / np.linalg.norm(forward)

  world_up = np.array([0.0, 0.0, 1.0])
  if abs(np.dot(forward, world_up)) > 0.999:
    world_up = np.array([0.0, 1.0, 0.0])
  camera_up = world_up - forward * np.dot(world_up, forward)
  camera_up = camera_up / np.linalg.norm(camera_up)
  down = -camera_up
  right = np.cross(down, forward)
  right = right / np.linalg.norm(right)

  if abs(roll_rad) > 1e-9:
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
    right, down = (
      right * cos_r + down * sin_r,
      down * cos_r - right * sin_r,
    )

  rotation_matrix = np.column_stack([right, down, forward])
  return _matrix_to_quat(rotation_matrix), forward


def _ground_intersection(camera_position, forward):
  """World point (x, y, 0) where the camera's optical axis crosses the
  ground plane z=0, or None if it doesn't point downward at all."""
  if forward[2] >= -1e-6:
    return None
  t = -camera_position[2] / forward[2]
  if t <= 0:
    return None
  return (camera_position[0] + t * forward[0],
         camera_position[1] + t * forward[1], 0.0)


def _project_to_pixels(world_point, camera_position, camera_rotation, fx, fy, cx, cy):
  """Pinhole-projects world_point into this camera's pixel coordinates, no
  lens distortion. Used only to synthesize a realistic-looking dummy pixel
  bounding box (standing in for a real onboard detector's output) - the
  reverse (pixel->world) reconstruction is always done by the real
  scene_common.transform classes, never by inverting this function."""
  rotation_matrix = np.array([
    [1 - 2 * (camera_rotation[1]**2 + camera_rotation[2]**2),
     2 * (camera_rotation[0] * camera_rotation[1] - camera_rotation[2] * camera_rotation[3]),
     2 * (camera_rotation[0] * camera_rotation[2] + camera_rotation[1] * camera_rotation[3])],
    [2 * (camera_rotation[0] * camera_rotation[1] + camera_rotation[2] * camera_rotation[3]),
     1 - 2 * (camera_rotation[0]**2 + camera_rotation[2]**2),
     2 * (camera_rotation[1] * camera_rotation[2] - camera_rotation[0] * camera_rotation[3])],
    [2 * (camera_rotation[0] * camera_rotation[2] - camera_rotation[1] * camera_rotation[3]),
     2 * (camera_rotation[1] * camera_rotation[2] + camera_rotation[0] * camera_rotation[3]),
     1 - 2 * (camera_rotation[0]**2 + camera_rotation[1]**2)],
  ])
  relative = np.array(world_point, dtype=float) - np.array(camera_position, dtype=float)
  camera_point = rotation_matrix.T @ relative
  if camera_point[2] <= 1e-6:
    return None
  u = fx * (camera_point[0] / camera_point[2]) + cx
  v = fy * (camera_point[1] / camera_point[2]) + cy
  return u, v


def drone_position(elapsed_s, radius_m, period_s, flight_alt_m, climb_time_s,
                   home_x_m=0.0, home_y_m=0.0):
  """Drone position (scene-local metres, Z-up) and horizontal heading
  (radians) at elapsed_s, circling scene-local point (home_x_m, home_y_m).
  Identical to dummy_drone_camera_simulator.py's drone_position()."""
  angle = 2.0 * math.pi * (elapsed_s % period_s) / period_s
  x = home_x_m + radius_m * math.sin(angle)
  y = home_y_m + radius_m * (1.0 - math.cos(angle))
  climb = min(elapsed_s / climb_time_s, 1.0) if climb_time_s > 0 else 1.0
  z = flight_alt_m * climb
  heading = angle + math.pi / 2.0
  return (x, y, z), heading


def _yaw_quaternion(yaw):
  """Quaternion (x, y, z, w) for a yaw-only rotation (radians)."""
  return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


def camera_pose_for_role(role, drone_pos, heading, gimbal_deg, mount_offset_m):
  """Builds (camera_position, camera_rotation, forward) for one fixed
  camera role this frame, from its rigid gimbal offset and mount offset
  relative to the vehicle body. Identical geometry to
  dummy_drone_camera_simulator.py's camera_pose_for_role()."""
  x, y, z = drone_pos
  camera_rotation, forward = _gimbal_quaternion(
    heading, gimbal_deg["pitch"], gimbal_deg["yaw"], gimbal_deg["roll"])

  forward_horizontal = (math.sin(heading), -math.cos(heading), 0.0)
  right_horizontal = (math.cos(heading), math.sin(heading), 0.0)
  mount_forward_m = mount_offset_m["forward"]
  mount_right_m = mount_offset_m["right"]
  mount_up_m = mount_offset_m["up"]
  camera_position = (
    x + forward_horizontal[0] * mount_forward_m + right_horizontal[0] * mount_right_m,
    y + forward_horizontal[1] * mount_forward_m + right_horizontal[1] * mount_right_m,
    z + mount_up_m,
  )
  return camera_position, camera_rotation, forward


def raycast_detection(role, camera_position, camera_rotation, forward, intrinsics,
                      resolution, box_size_px, wobble_radius_m, wobble_period_s,
                      elapsed_s):
  """Synthesizes a dummy pixel bounding box for this camera (a small ground
  target wobbling near the camera's optical-axis ground crossing, forward-
  projected to pixels), then reconstructs its world position using
  Scenescape's own built-in ray-casting: CameraIntrinsics.
  mapPixelToNormalizedImagePlane() + CameraPose.cameraPointToWorldPoint(),
  exactly as controller/src/controller/moving_object.py does for a real
  'bounding_box_px' detection (see camLoc / mapObjectDetectionToWorld()).

  Returns (world_xyz, pixel_bbox) or (None, None) if this camera isn't
  currently pointed at the ground at all, or the synthetic target falls
  behind the camera.
  """
  ground_target = _ground_intersection(camera_position, forward)
  if ground_target is None:
    return None, None

  # Slow circular wobble so the dummy detection visibly moves across frames.
  wobble_angle = 2.0 * math.pi * (elapsed_s % wobble_period_s) / wobble_period_s
  target = (
    ground_target[0] + wobble_radius_m * math.cos(wobble_angle),
    ground_target[1] + wobble_radius_m * math.sin(wobble_angle),
    0.0,
  )

  fx, fy, cx, cy = intrinsics.intrinsics[0, 0], intrinsics.intrinsics[1, 1], \
    intrinsics.intrinsics[0, 2], intrinsics.intrinsics[1, 2]
  pixel = _project_to_pixels(target, camera_position, camera_rotation, fx, fy, cx, cy)
  if pixel is None:
    return None, None
  u, v = pixel
  width, height = resolution
  half = box_size_px / 2.0
  bbox = {
    "x": max(0.0, min(u - half, width - box_size_px)),
    "y": max(0.0, min(v - half, height - box_size_px)),
    "width": box_size_px,
    "height": box_size_px,
  }

  pose = CameraPose(
    {"translation": list(camera_position), "rotation": list(camera_rotation), "scale": [1.0, 1.0, 1.0]},
    intrinsics)
  agnostic = intrinsics.mapPixelToNormalizedImagePlane(Rectangle(bbox))
  bottom_center = Point(agnostic.x + agnostic.width / 2.0, agnostic.y2)
  world_point = pose.cameraPointToWorldPoint(bottom_center)
  return world_point.asNumpyCartesian.tolist(), bbox


def build_vehicle_object(source_id, category, x_m, y_m, alt_m, heading):
  """The drone's own trackable object, in the same absolute scene-local
  coordinates as the camera ray-cast detections below.

  Deliberately omits ``size``: the controller only overrides a class's
  Object Library-configured x/y/z_size with an explicit per-object
  ``size`` (see moving_object.py's mapObjectDetectionToWorld()). Leaving
  it out lets whatever shape/size you configure for this category in the
  Object Library actually take effect instead of always being hidden
  behind a hardcoded value."""
  return {
    "id": source_id,
    "category": category,
    "translation": [x_m, y_m, alt_m],
    "rotation": list(_yaw_quaternion(heading)),
  }


def build_detection_object(source_id, role, category, world_xyz, confidence=0.9):
  """See build_vehicle_object() for why ``size`` is intentionally omitted."""
  return {
    "id": f"{source_id}-{role}-raycast",
    "category": category,
    "translation": list(world_xyz),
    "confidence": confidence,
  }


def group_by_category(objects):
  """Group a flat objects list by category, preserving first-seen order.

  Required because the controller derives the single detection type used
  to track/publish an entire external_source message from the MQTT topic's
  thing_type segment, not from each object's own category - see
  dummy_drone_simulator.py's group_by_category() (same rationale)."""
  grouped = {}
  for obj in objects:
    grouped.setdefault(obj["category"], []).append(obj)
  return grouped


def build_payload(source_id, objects):
  """Builds an external_source dict (docs/user-guide/microservices/
  controller/data_formats.md). reference_frame 'scene' with an identity
  pose at the scene origin: every object's own translation below is
  already an absolute scene-local world position (computed either from the
  drone's own known flight path, or ray-cast to the ground plane by
  raycast_detection()), so no additional pose translation/rotation is
  needed or applied."""
  return {
    "timestamp": get_iso_time(),
    "source_id": source_id,
    "objects": objects,
    "pose": {
      "reference_frame": "scene",
      "translation": [0.0, 0.0, 0.0],
      "rotation": list(IDENTITY_QUAT),
      "provider": "dummy-drone-camera-raycasting",
    },
  }


def _load_px4_demo_config():
  """Best-effort read of tools/px4_sih_demo/px4_sih_config.json, so
  SIM_HOME_X_M/SIM_HOME_Y_M default to the scene's actual map center
  (scene-local map_extent_m/2, the "home" convention used by
  setup_geospatial_drone_scene.py) instead of the scene origin (0, 0),
  which is the map's bottom-left corner, not its center. Identical to
  dummy_drone_simulator.py's _load_px4_demo_config()."""
  config_path = Path(__file__).resolve().parents[1] / "px4_sih_demo" / "px4_sih_config.json"
  try:
    with open(config_path) as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError):
    return {}


def _role_gimbal_deg(role, defaults):
  """DEFAULT_GIMBAL_DEG[role], overridable by SIM_GIMBAL_{PITCH,YAW,ROLL}_DEG_<ROLE>."""
  upper = role.upper()
  return {
    "pitch": float(_env(f"SIM_GIMBAL_PITCH_DEG_{upper}", str(defaults["pitch"]))),
    "yaw": float(_env(f"SIM_GIMBAL_YAW_DEG_{upper}", str(defaults["yaw"]))),
    "roll": float(_env(f"SIM_GIMBAL_ROLL_DEG_{upper}", str(defaults["roll"]))),
  }


def parse_args(argv=None):
  demo_config = _load_px4_demo_config()
  map_extent_m = demo_config.get("map_extent_m", [0.0, 0.0])
  default_home_x_m = map_extent_m[0] / 2.0
  default_home_y_m = map_extent_m[1] / 2.0

  parser = argparse.ArgumentParser(
    description="Publish dummy multi-camera drone detections as Scenescape "
                "external_source MQTT messages, ray-cast to world "
                "coordinates in this adapter using scene_common.transform.")
  parser.add_argument("--publish-hz", type=float,
                      default=float(_env("PUBLISH_HZ", "5")))
  parser.add_argument("--camera-roles",
                      default=_env("SIM_CAMERA_ROLES", ",".join(CAMERA_ROLES)))
  parser.add_argument("--sim-radius-m", type=float,
                      default=float(_env("SIM_RADIUS_M", "20.0")))
  parser.add_argument("--sim-period-s", type=float,
                      default=float(_env("SIM_PERIOD_S", "30.0")))
  parser.add_argument("--sim-flight-alt-m", type=float,
                      default=float(_env("SIM_FLIGHT_ALT_M", "20.0")))
  parser.add_argument("--sim-climb-time-s", type=float,
                      default=float(_env("SIM_CLIMB_TIME_S", "10.0")))
  parser.add_argument("--sim-home-x-m", type=float,
                      default=float(_env("SIM_HOME_X_M", str(default_home_x_m))))
  parser.add_argument("--sim-home-y-m", type=float,
                      default=float(_env("SIM_HOME_Y_M", str(default_home_y_m))))
  parser.add_argument("--camera-fov-degrees", type=float,
                      default=float(_env("SIM_CAMERA_FOV_DEGREES", "90.0")))
  parser.add_argument("--camera-width", type=int,
                      default=int(_env("SIM_CAMERA_WIDTH", "1920")))
  parser.add_argument("--camera-height", type=int,
                      default=int(_env("SIM_CAMERA_HEIGHT", "1080")))
  parser.add_argument("--mount-forward-m", type=float,
                      default=float(_env("SIM_MOUNT_FORWARD_M", str(DEFAULT_MOUNT_OFFSET_M["forward"]))))
  parser.add_argument("--mount-right-m", type=float,
                      default=float(_env("SIM_MOUNT_RIGHT_M", str(DEFAULT_MOUNT_OFFSET_M["right"]))))
  parser.add_argument("--mount-up-m", type=float,
                      default=float(_env("SIM_MOUNT_UP_M", str(DEFAULT_MOUNT_OFFSET_M["up"]))))
  parser.add_argument("--box-size-px", type=float,
                      default=float(_env("SIM_BOX_SIZE_PX", "60.0")))
  parser.add_argument("--wobble-radius-m", type=float,
                      default=float(_env("SIM_WOBBLE_RADIUS_M", "2.0")))
  parser.add_argument("--wobble-period-s", type=float,
                      default=float(_env("SIM_WOBBLE_PERIOD_S", "6.0")))
  return parser.parse_args(argv)


def main(argv=None):
  logging.basicConfig(
    level=getattr(logging, _env("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
  args = parse_args(argv)

  source_id = _env("SCENESCAPE_SOURCE_ID", required=True)
  thing_type = _env("SCENESCAPE_THING_TYPE", "vehicle")
  broker = _env("SCENESCAPE_BROKER", "localhost")
  broker_port = int(_env("SCENESCAPE_BROKER_PORT", "1883"))
  mqtt_auth = _env("SCENESCAPE_MQTT_AUTH", required=True)
  root_cert = _env("SCENESCAPE_ROOT_CERT", required=True)
  mqtt_insecure = _env("SCENESCAPE_MQTT_INSECURE", "").lower() in ("1", "true", "yes")

  roles = [r.strip() for r in args.camera_roles.split(",") if r.strip()]
  unknown = set(roles) - set(CAMERA_ROLES)
  if unknown:
    raise SystemExit(f"Unknown camera role(s) {sorted(unknown)}; must be a subset of {CAMERA_ROLES}")

  mount_offset_m = {"forward": args.mount_forward_m, "right": args.mount_right_m, "up": args.mount_up_m}
  gimbal_deg = {role: _role_gimbal_deg(role, DEFAULT_GIMBAL_DEG[role]) for role in roles}
  detection_category = {
    role: _env(f"SIM_DETECTION_CATEGORY_{role.upper()}", DEFAULT_DETECTION_CATEGORY[role])
    for role in roles
  }
  resolution = (args.camera_width, args.camera_height)
  intrinsics = CameraIntrinsics(args.camera_fov_degrees, resolution=resolution)

  logger.info(
    "Simulating %s camera(s) ray-cast to world coordinates on a circular "
    "flight (radius=%sm period=%ss climb=%ss) around home=(%s, %s), "
    "publishing as source_id=%s", roles, args.sim_radius_m, args.sim_period_s,
    args.sim_climb_time_s, args.sim_home_x_m, args.sim_home_y_m, source_id)

  pubsub = PubSub(mqtt_auth, None, root_cert, broker, port=broker_port,
                  insecure=mqtt_insecure)
  pubsub.connect()
  pubsub.loopStart()

  interval = 1.0 / args.publish_hz
  start = time.monotonic()
  next_publish = start
  publish_count = 0

  try:
    while True:
      now = time.monotonic()
      if now < next_publish:
        time.sleep(next_publish - now)
        continue
      next_publish = now + interval
      elapsed_s = now - start

      (drone_x, drone_y, drone_z), heading = drone_position(
        elapsed_s, args.sim_radius_m, args.sim_period_s, args.sim_flight_alt_m,
        args.sim_climb_time_s, args.sim_home_x_m, args.sim_home_y_m)

      objects = [build_vehicle_object(source_id, thing_type, drone_x, drone_y, drone_z, heading)]
      for role in roles:
        camera_position, camera_rotation, forward = camera_pose_for_role(
          role, (drone_x, drone_y, drone_z), heading, gimbal_deg[role], mount_offset_m)
        world_xyz, bbox = raycast_detection(
          role, camera_position, camera_rotation, forward, intrinsics, resolution,
          args.box_size_px, args.wobble_radius_m, args.wobble_period_s, elapsed_s)
        if world_xyz is None:
          continue
        objects.append(build_detection_object(source_id, role, detection_category[role], world_xyz))

      payload = build_payload(source_id, objects)

      # Publish one message per category so each object is tracked/output
      # under its own real category (see group_by_category()).
      for obj_category, category_objects in group_by_category(payload["objects"]).items():
        topic = PubSub.formatTopic(
          PubSub.DATA_EXTERNAL, scene_id=source_id, thing_type=obj_category)
        payload_copy = dict(payload, objects=category_objects)
        pubsub.publish(topic, json.dumps(payload_copy))

      publish_count += 1
      if publish_count == 1 or publish_count % max(1, int(args.publish_hz * 5)) == 0:
        logger.info("Published #%s drone=(%.2f, %.2f, %.2f) detections=%s",
                   publish_count, drone_x, drone_y, drone_z,
                   [o["id"] for o in objects if o["id"] != source_id])
  except KeyboardInterrupt:
    logger.info("Interrupted; exiting")
  finally:
    pubsub.loopStop()
  return 0


if __name__ == "__main__":
  sys.exit(main())
