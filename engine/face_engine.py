#!/usr/bin/env python3
"""
Face ID Neural Recognition & Anti-Spoof Engine for Linux
Uses OpenCV 4.x YuNet (FaceDetectorYN) + SFace (FaceRecognizerSF) ONNX models
with multi-angle embedding matching and Smart Balance liveness verification.
"""

from __future__ import annotations

import json
import math
import os
import pwd
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

MODEL_DIR = Path("/var/lib/faceid/models")
USER_MODEL_DIR = Path("/var/lib/faceid/users")
CONFIG_PATH = Path("/etc/faceid/config.json")

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_recognition_sface/face_recognition_sface_2021dec.onnx"
)


DEFAULT_CONFIG: Dict[str, Any] = {
    "camera_device": "/dev/video0",
    "camera_index": 0,
    "frame_width": 640,
    "frame_height": 480,
    "cosine_threshold": 0.50,
    "l2_threshold": 1.00,
    "security_mode": "smart",  # "smart" (3D parallax + anti-spoof) | "fast" | "strict"
    "timeout_seconds": 4.5,
    "show_mini_camera": True,
    "hud_style": "dynamic_island",  # "dynamic_island" | "glass_modal"
    "enable_system_auth": True,     # Lock screen, sudo, polkit
    "enable_gdm_auth": True,        # GDM boot login
}


@dataclass
class FaceObservation:
    bbox: Tuple[int, int, int, int]  # x, y, w, h
    landmarks: np.ndarray            # shape (5, 2): right_eye, left_eye, nose, right_mouth, left_mouth
    confidence: float
    embedding: np.ndarray            # shape (1, 128)
    sharpness: float                 # Laplacian variance of face crop
    color_entropy: float             # Channel std diversity to reject flat prints/screens
    yaw: float                       # Estimated horizontal head yaw (-1.0 left .. +1.0 right)
    pitch: float                     # Estimated vertical head pitch (-1.0 up .. +1.0 down)
    affine_nose: Tuple[float, float] = (0.0, 0.0)  # 2D affine-invariant nose coords (alpha, beta)
    skin_ratio: float = 0.5          # Natural YCrCb human skin chrominance ratio
    glare_ratio: float = 0.0         # Specular screen glare ratio
    timestamp: float = field(default_factory=time.monotonic)


def load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    for candidate in (config_path, Path.home() / ".config/faceid/config.json"):
        try:
            if candidate.exists():
                with open(candidate, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    cfg.update(user_cfg)
                break
        except Exception:
            pass
    return cfg


def save_config(cfg: Dict[str, Any], config_path: Path = CONFIG_PATH) -> None:
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except PermissionError:
        fallback = Path.home() / ".config/faceid/config.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        with open(fallback, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)


class FaceIDEngine:
    """Warm-loadable YuNet + SFace neural face recognition and liveness engine."""

    def __init__(
        self,
        model_dir: Path = MODEL_DIR,
        user_dir: Path = USER_MODEL_DIR,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.user_dir = Path(user_dir)
        self.config = config or load_config()
        self.detector: Optional[cv2.FaceDetectorYN] = None
        self.recognizer: Optional[cv2.FaceRecognizerSF] = None
        self._input_size: Tuple[int, int] = (640, 480)

    def resolve_model_paths(self) -> Tuple[Path, Path]:
        candidates = [
            self.model_dir,
            Path(__file__).resolve().parent.parent / "models",
            Path.home() / ".local/share/faceid/models",
        ]
        for d in candidates:
            y = d / YUNET_FILENAME
            s = d / SFACE_FILENAME
            if y.exists() and s.exists():
                return y, s
        return self.model_dir / YUNET_FILENAME, self.model_dir / SFACE_FILENAME

    def load_models(self) -> bool:
        if self.detector is not None and self.recognizer is not None:
            return True
        yunet_path, sface_path = self.resolve_model_paths()
        if not yunet_path.exists() or not sface_path.exists():
            return False

        self.detector = cv2.FaceDetectorYN.create(
            str(yunet_path),
            "",
            self._input_size,
            0.75,  # score threshold
            0.3,   # nms threshold
            5000,  # top_k
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(sface_path), "")
        return True

    def open_camera(self) -> Optional[cv2.VideoCapture]:
        dev_str = str(self.config.get("camera_device", "/dev/video0"))
        cam_idx = int(self.config.get("camera_index", 0))
        cap = cv2.VideoCapture(dev_str, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cam_idx, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cam_idx)
        if not cap.isOpened():
            return None

        w = int(self.config.get("frame_width", 640))
        h = int(self.config.get("frame_height", 480))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def analyze_frame(self, frame: np.ndarray) -> Optional[FaceObservation]:
        """Detect primary face, extract 128-D SFace embedding, head pose, and texture metrics."""
        if not self.load_models() or self.detector is None or self.recognizer is None:
            return None

        h, w = frame.shape[:2]
        if (w, h) != self._input_size:
            self._input_size = (w, h)
            self.detector.setInputSize(self._input_size)

        _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None

        # Filter out tiny distant background faces (< 55x55 px on 640x480)
        foreground_faces = [f for f in faces if float(f[2]) >= 55.0 and float(f[3]) >= 55.0]
        if not foreground_faces:
            return None

        # Pick the largest foreground face closest to center
        best_face = max(
            foreground_faces,
            key=lambda f: float(f[2] * f[3])
            * (1.0 - 0.35 * abs((f[0] + f[2] * 0.5 - w * 0.5) / max(w, 1))),
        )

        x, y, bw, bh = (
            max(0, int(best_face[0])),
            max(0, int(best_face[1])),
            max(1, int(best_face[2])),
            max(1, int(best_face[3])),
        )
        conf = float(best_face[-1])

        # Extract 5 landmarks: right eye, left eye, nose tip, right mouth, left mouth
        landmarks = best_face[4:14].reshape((5, 2))

        # Estimate head yaw & pitch from 5 facial landmarks
        yaw, pitch = self._estimate_head_pose(landmarks)
        affine_nose = self._compute_affine_nose_coords(landmarks)

        # Compute aligned face & 128-D SFace feature vector
        aligned = self.recognizer.alignCrop(frame, best_face)
        feature = self.recognizer.feature(aligned)
        norm = np.linalg.norm(feature)
        if norm > 1e-6:
            feature = feature / norm

        # Compute anti-spoof texture & screen-glare/chrominance metrics on inner face crop
        gray_crop = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray_crop, cv2.CV_64F).var())

        hsv_crop = cv2.cvtColor(aligned, cv2.COLOR_BGR2HSV)
        sat_std = float(np.std(hsv_crop[:, :, 1]))
        val_std = float(np.std(hsv_crop[:, :, 2]))
        color_entropy = float(math.sqrt(sat_std * val_std))

        # Inner face zone (excluding background corners) for skin chrominance & screen glare check
        ah, aw = aligned.shape[:2]
        inner = aligned[int(ah * 0.18) : int(ah * 0.85), int(aw * 0.18) : int(aw * 0.82)]
        ycrcb = cv2.cvtColor(inner, cv2.COLOR_BGR2YCrCb)
        cr = ycrcb[:, :, 1]
        cb = ycrcb[:, :, 2]
        skin_mask = (cr >= 130) & (cr <= 180) & (cb >= 75) & (cb <= 135)
        skin_ratio = float(np.mean(skin_mask))

        # Screen specular highlight / backlight saturation check
        inner_gray = gray_crop[int(ah * 0.18) : int(ah * 0.85), int(aw * 0.18) : int(aw * 0.82)]
        glare_ratio = float(np.mean(inner_gray >= 248))

        return FaceObservation(
            bbox=(x, y, bw, bh),
            landmarks=landmarks,
            confidence=conf,
            embedding=feature.copy(),
            sharpness=sharpness,
            color_entropy=color_entropy,
            yaw=yaw,
            pitch=pitch,
            affine_nose=affine_nose,
            skin_ratio=skin_ratio,
            glare_ratio=glare_ratio,
        )

    @staticmethod
    def _compute_affine_nose_coords(landmarks: np.ndarray) -> Tuple[float, float]:
        """
        Compute the exact 2x2 affine coordinates (alpha, beta) of the nose tip
        in the basis of the eye-line vector u and eye-to-mouth vector v.
        Because all 5 landmarks on a flat 2D photo are coplanar (z=0), (alpha, beta)
        is mathematically invariant under 2D/3D translation, scale, roll, and tilt
        of a phone screen or printed photo, whereas a real 3D protruding nose shifts
        significantly across even small head turns.
        """
        r_eye, l_eye, nose, r_mouth, l_mouth = landmarks
        e_mid = 0.5 * (r_eye + l_eye)
        m_mid = 0.5 * (r_mouth + l_mouth)
        u = l_eye - r_eye
        v = m_mid - e_mid
        mat = np.column_stack([u, v])
        det = float(np.linalg.det(mat))
        if abs(det) < 1e-5:
            return 0.0, 0.0
        sol = np.linalg.solve(mat, nose - e_mid)
        return float(sol[0]), float(sol[1])

    @staticmethod
    def _estimate_head_pose(landmarks: np.ndarray) -> Tuple[float, float]:
        """Return normalized (yaw, pitch) in roughly [-1.0, 1.0] from 5 YuNet landmarks."""
        r_eye, l_eye, nose, r_mouth, l_mouth = landmarks
        eye_center = (r_eye + l_eye) * 0.5
        mouth_center = (r_mouth + l_mouth) * 0.5
        inter_ocular = max(float(np.linalg.norm(l_eye - r_eye)), 1.0)
        eye_to_mouth = max(float(np.linalg.norm(mouth_center - eye_center)), 1.0)

        # Horizontal asymmetry of nose relative to eye midpoint
        yaw = float((nose[0] - eye_center[0]) / (inter_ocular * 0.55))
        # Vertical position of nose between eyes and mouth (neutral ~0.56)
        nose_ratio = float((nose[1] - eye_center[1]) / eye_to_mouth)
        pitch = float((nose_ratio - 0.56) / 0.22)

        return max(-1.5, min(1.5, yaw)), max(-1.5, min(1.5, pitch))

    def get_user_profile_path(self, username: str) -> Path:
        search_dirs = [self.user_dir]
        try:
            pw_dir = Path(pwd.getpwnam(username).pw_dir)
            search_dirs.append(pw_dir / ".local/share/faceid/users")
        except Exception:
            pass
        search_dirs.append(Path(f"/home/{username}/.local/share/faceid/users"))
        search_dirs.append(Path.home() / ".local/share/faceid/users")

        for base in search_dirs:
            try:
                candidate = base / f"{username}.json"
                if candidate.exists() and os.access(candidate, os.R_OK):
                    return candidate
            except Exception:
                continue
        return self.user_dir / f"{username}.json"

    def load_user_embeddings(self, username: str) -> List[np.ndarray]:
        path = self.get_user_profile_path(username)
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_list = data.get("embeddings", [])
            embeddings = []
            for item in raw_list:
                vec = np.array(item["vector"] if isinstance(item, dict) else item, dtype=np.float32).reshape(1, 128)
                n = np.linalg.norm(vec)
                if n > 1e-6:
                    vec = vec / n
                embeddings.append(vec)
            return embeddings
        except Exception:
            return []

    def save_user_embeddings(
        self,
        username: str,
        samples: List[Dict[str, Any]],
    ) -> Path:
        payload = {
            "username": username,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": "opencv_sface_2021dec_128d",
            "sample_count": len(samples),
            "embeddings": samples,
        }
        target = self.user_dir / f"{username}.json"
        try:
            self.user_dir.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.chmod(target, 0o644)
            return target
        except PermissionError:
            fallback_dir = Path.home() / ".local/share/faceid/users"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback = fallback_dir / f"{username}.json"
            with open(fallback, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return fallback

    def match_embedding(
        self,
        live_embedding: np.ndarray,
        enrolled_embeddings: List[np.ndarray],
    ) -> Tuple[bool, float, float]:
        """Return (is_match, best_cosine_score, best_l2_distance)."""
        if not enrolled_embeddings or self.recognizer is None:
            return False, 0.0, 99.0

        best_cosine = -1.0
        best_l2 = 99.0
        for ref in enrolled_embeddings:
            cos_score = float(
                self.recognizer.match(live_embedding, ref, cv2.FaceRecognizerSF_FR_COSINE)
            )
            l2_dist = float(
                self.recognizer.match(live_embedding, ref, cv2.FaceRecognizerSF_FR_NORM_L2)
            )
            if cos_score > best_cosine:
                best_cosine = cos_score
            if l2_dist < best_l2:
                best_l2 = l2_dist

        cos_thresh = float(self.config.get("cosine_threshold", 0.40))
        l2_thresh = float(self.config.get("l2_threshold", 1.05))
        matched = (best_cosine >= cos_thresh) and (best_l2 <= l2_thresh)
        return matched, best_cosine, best_l2

    def check_liveness(self, observations: List[FaceObservation]) -> Tuple[bool, str]:
        """
        Evaluate 3D Presentation Attack Detection (PAD) & liveness:
        - Rejects screen backlight glare, non-skin YCrCb chrominance, and blurry frames.
        - Rejects rigid 2D planar movements (phone/printed photos being waved or tilted)
          by measuring true non-coplanar 3D nose-protrusion affine parallax:
          Delta_3D = max_{i,j} ||(alpha_i, beta_i) - (alpha_j, beta_j)||_2.
        """
        mode = str(self.config.get("security_mode", "smart")).lower()
        if not observations:
            return False, "no_face"

        latest = observations[-1]
        if latest.sharpness < 12.0:
            return False, "low_sharpness"
        if latest.color_entropy < 6.0:
            return False, "flat_color"
        if latest.skin_ratio < 0.22:
            return False, "spoof_chrominance"
        if latest.glare_ratio > 0.22:
            return False, "spoof_screen_glare"

        if mode == "fast":
            return True, "ok_fast"

        if len(observations) < 3:
            return False, "collecting_3d"

        # Compute max non-coplanar 3D affine nose parallax across captured frames
        coords = np.array([o.affine_nose for o in observations], dtype=np.float32)
        diffs = coords - coords[0:1, :]
        max_3d_parallax = float(np.max(np.linalg.norm(diffs, axis=1)))

        # Also check that the 128-D neural embedding exhibits natural 3D viewpoint shift
        # (a tilted 2D photo has near-zero 3D structure change or fails affine parallax)
        emb_deltas = [
            float(np.linalg.norm(o.embedding - observations[0].embedding))
            for o in observations[1:]
        ]
        max_emb_shift = max(emb_deltas) if emb_deltas else 0.0

        req_parallax = 0.052 if mode == "smart" else 0.090
        if max_3d_parallax >= req_parallax and max_emb_shift >= 0.08:
            return True, f"ok_3d_parallax_{max_3d_parallax:.3f}"

        return False, f"need_3d_tilt:{max_3d_parallax / max(req_parallax, 0.01):.2f}"
