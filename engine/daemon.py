#!/usr/bin/env python3
"""
Face ID Warm Authentication & Telemetry Daemon (faceid-daemon)
- Pre-loads OpenCV YuNet + SFace ONNX models in RAM for instant (<350ms) response.
- Serves PAM authentication requests on /run/faceid/auth.sock.
- Broadcasts 30fps Dynamic Island HUD state to /run/faceid/state.json,
  /run/faceid/preview.jpg, and /run/faceid/events.sock.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from face_engine import FaceIDEngine, FaceObservation, load_config, save_config

RUN_DIR = Path("/run/faceid")
AUTH_SOCKET_PATH = RUN_DIR / "auth.sock"
EVENTS_SOCKET_PATH = RUN_DIR / "events.sock"
STATE_JSON_PATH = RUN_DIR / "state.json"
PREVIEW_JPG_PATH = RUN_DIR / "preview.jpg"


class FaceIDDaemon:
    def __init__(self) -> None:
        self.config = load_config()
        self.engine = FaceIDEngine(config=self.config)
        self.auth_lock = threading.Lock()
        self.event_clients: List[socket.socket] = []
        self.event_clients_lock = threading.Lock()
        self.running = True
        self.seq = 0

    def ensure_run_dir(self) -> None:
        global RUN_DIR, AUTH_SOCKET_PATH, EVENTS_SOCKET_PATH, STATE_JSON_PATH, PREVIEW_JPG_PATH
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(RUN_DIR, 0o755)
        except PermissionError:
            RUN_DIR = Path("/tmp/faceid-runtime")
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(RUN_DIR, 0o777)
            AUTH_SOCKET_PATH = RUN_DIR / "auth.sock"
            EVENTS_SOCKET_PATH = RUN_DIR / "events.sock"
            STATE_JSON_PATH = RUN_DIR / "state.json"
            PREVIEW_JPG_PATH = RUN_DIR / "preview.jpg"

    def broadcast_state(
        self,
        state: str,
        message: str = "",
        username: str = "",
        score: float = 0.0,
        progress: float = 0.0,
        face_detected: bool = False,
        frame_crop: Optional[np.ndarray] = None,
    ) -> None:
        """Publish Dynamic Island state to /run/faceid/state.json and connected HUD clients."""
        self.seq += 1
        has_preview = False
        if frame_crop is not None and self.config.get("show_mini_camera", True):
            try:
                thumb = cv2.resize(frame_crop, (96, 96), interpolation=cv2.INTER_AREA)
                tmp_jpg = PREVIEW_JPG_PATH.with_suffix(".tmp.jpg")
                cv2.imwrite(str(tmp_jpg), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                os.chmod(tmp_jpg, 0o644)
                tmp_jpg.replace(PREVIEW_JPG_PATH)
                has_preview = True
            except Exception:
                pass

        payload: Dict[str, Any] = {
            "seq": self.seq,
            "timestamp": time.time(),
            "state": state,  # IDLE | SCANNING | FACE_FOUND | MATCHED | FAILED
            "message": message,
            "username": username,
            "score": round(score, 4),
            "progress": round(progress, 3),
            "face_detected": face_detected,
            "show_mini_camera": bool(self.config.get("show_mini_camera", True)),
            "preview_path": str(PREVIEW_JPG_PATH) if has_preview else "",
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")

        try:
            tmp_state = STATE_JSON_PATH.with_suffix(".tmp")
            with open(tmp_state, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.chmod(tmp_state, 0o644)
            tmp_state.replace(STATE_JSON_PATH)
        except Exception:
            pass

        with self.event_clients_lock:
            stale: List[socket.socket] = []
            for client in self.event_clients:
                try:
                    client.sendall(line)
                except Exception:
                    stale.append(client)
            for s in stale:
                try:
                    s.close()
                except Exception:
                    pass
                self.event_clients.remove(s)

    def resolve_effective_username(self, requested_user: str) -> str:
        """If PAM passes 'root' (e.g. during sudo/su/polkit), resolve to the enrolled desktop user."""
        if requested_user and requested_user != "root":
            enrolled = self.engine.load_user_embeddings(requested_user)
            if enrolled:
                return requested_user

        # Check all local desktop users (UID >= 1000) and /var/lib/faceid/users
        for pw in pwd.getpwall():
            if 1000 <= pw.pw_uid < 60000:
                if self.engine.load_user_embeddings(pw.pw_name):
                    return pw.pw_name

        for base in (self.engine.user_dir, Path.home() / ".local/share/faceid/users"):
            if base.exists():
                for p in sorted(base.glob("*.json")):
                    candidate = p.stem
                    if candidate != "root" and self.engine.load_user_embeddings(candidate):
                        return candidate
        return requested_user

    @staticmethod
    def _recv_line(conn: socket.socket, max_bytes: int = 1048576) -> str:
        chunks: List[bytes] = []
        total = 0
        while total < max_bytes:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    def authenticate_user(self, requested_user: str, reason: str = "pam") -> Dict[str, Any]:
        """Run a full Face ID recognition & Dynamic Island HUD cycle."""
        with self.auth_lock:
            self.config = load_config()
            self.engine.config = self.config

            if not self.engine.load_models():
                return {
                    "ok": False,
                    "status": "NO_MODELS",
                    "message": "ONNX face models not found",
                }

            username = self.resolve_effective_username(requested_user)
            enrolled = self.engine.load_user_embeddings(username)
            if not enrolled:
                return {
                    "ok": False,
                    "status": "NOT_ENROLLED",
                    "message": f"No Face ID profile enrolled for {username}",
                }

            cap = self.engine.open_camera()
            if cap is None:
                self.broadcast_state("FAILED", "Camera Busy or Disconnected", username=username)
                time.sleep(0.5)
                self.broadcast_state("IDLE", "")
                return {
                    "ok": False,
                    "status": "NO_CAMERA",
                    "message": "Could not open webcam",
                }

            timeout = float(self.config.get("timeout_seconds", 3.5))
            start_ts = time.monotonic()
            observations: List[FaceObservation] = []
            matched = False
            best_score = 0.0
            liveness_reason = "scanning"

            try:
                self.broadcast_state(
                    "SCANNING",
                    "Face ID",
                    username=username,
                    progress=0.1,
                )

                # Flush 1 initial stale buffer frame if any
                cap.grab()

                while (time.monotonic() - start_ts) < timeout:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        time.sleep(0.02)
                        continue

                    elapsed = time.monotonic() - start_ts
                    progress = min(0.95, elapsed / max(timeout, 0.5))

                    # Prepare centered square crop for mini-camera preview
                    fh, fw = frame.shape[:2]
                    side = min(fh, fw)
                    cy, cx = fh // 2, fw // 2
                    preview_crop = cv2.flip(
                        frame[cy - side // 2 : cy + side // 2, cx - side // 2 : cx + side // 2],
                        1,
                    )

                    obs = self.engine.analyze_frame(frame)
                    if obs is None:
                        self.broadcast_state(
                            "SCANNING",
                            "Looking for you...",
                            username=username,
                            progress=progress,
                            face_detected=False,
                            frame_crop=preview_crop,
                        )
                        continue

                    # Crop around detected face for the circular HUD ring
                    bx, by, bw, bh = obs.bbox
                    pad = int(max(bw, bh) * 0.35)
                    x0 = max(0, bx - pad)
                    y0 = max(0, by - pad)
                    x1 = min(fw, bx + bw + pad)
                    y1 = min(fh, by + bh + pad)
                    if x1 > x0 and y1 > y0:
                        preview_crop = cv2.flip(frame[y0:y1, x0:x1], 1)

                    is_match, cos_score, _ = self.engine.match_embedding(obs.embedding, enrolled)
                    if cos_score > best_score:
                        best_score = cos_score

                    if is_match:
                        observations.append(obs)
                        live_ok, liveness_reason = self.engine.check_liveness(observations)
                        if live_ok:
                            matched = True
                            self.broadcast_state(
                                "MATCHED",
                                "Unlocked",
                                username=username,
                                score=cos_score,
                                progress=1.0,
                                face_detected=True,
                                frame_crop=preview_crop,
                            )
                            break
                        else:
                            if liveness_reason.startswith("spoof_"):
                                msg = "2D Photo / Screen Blocked"
                                prog_val = progress
                            elif liveness_reason.startswith("need_3d_tilt:"):
                                try:
                                    ratio = float(liveness_reason.split(":")[1])
                                except Exception:
                                    ratio = 0.3
                                msg = "Tilt head slightly (3D)..."
                                prog_val = min(0.95, 0.25 + 0.70 * ratio)
                            else:
                                msg = "Verifying 3D Depth..."
                                prog_val = min(0.95, progress + 0.20)
                            self.broadcast_state(
                                "FACE_FOUND",
                                msg,
                                username=username,
                                score=cos_score,
                                progress=prog_val,
                                face_detected=True,
                                frame_crop=preview_crop,
                            )
                    else:
                        self.broadcast_state(
                            "FACE_FOUND",
                            "Scanning Face...",
                            username=username,
                            score=cos_score,
                            progress=progress,
                            face_detected=True,
                            frame_crop=preview_crop,
                        )
            finally:
                cap.release()

            if matched:
                # If triggered from the lock screen or GDM, also tell systemd-logind
                # to unlock active graphical sessions immediately.
                if reason in ("gdm-password", "gdm-switchable-auth", "lockscreen", "screensaver"):
                    try:
                        subprocess.Popen(
                            ["loginctl", "unlock-sessions"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass
                # Hold the unlocked padlock state briefly so the user sees the green checkmark
                time.sleep(0.55)
                self.broadcast_state("IDLE", "", username=username)
                return {
                    "ok": True,
                    "status": "MATCHED",
                    "username": username,
                    "score": round(best_score, 4),
                    "liveness": liveness_reason,
                }

            self.broadcast_state(
                "FAILED",
                "Face Not Recognized",
                username=username,
                score=best_score,
                progress=1.0,
                face_detected=False,
            )
            time.sleep(0.75)
            self.broadcast_state("IDLE", "", username=username)
            return {
                "ok": False,
                "status": "FAILED",
                "username": username,
                "score": round(best_score, 4),
                "message": "Face not recognized within timeout",
            }

    def simulate_ui_demo(self, demo_type: str = "success") -> Dict[str, Any]:
        """Animate the Dynamic Island HUD for visual testing."""
        self.broadcast_state("SCANNING", "Face ID", username="demo", progress=0.25)
        time.sleep(0.45)
        self.broadcast_state("FACE_FOUND", "Verifying...", username="demo", progress=0.70, face_detected=True)
        time.sleep(0.45)
        if demo_type == "fail":
            self.broadcast_state("FAILED", "Face Not Recognized", username="demo", progress=1.0)
            time.sleep(0.95)
        else:
            self.broadcast_state("MATCHED", "Unlocked", username="demo", score=0.78, progress=1.0, face_detected=True)
            time.sleep(0.85)
        self.broadcast_state("IDLE", "")
        return {"ok": True, "status": "DEMO_COMPLETE", "demo_type": demo_type}

    def handle_auth_client(self, conn: socket.socket) -> None:
        try:
            raw = self._recv_line(conn)
            if not raw:
                return
            req = json.loads(raw) if raw.startswith("{") else {"cmd": raw}
            cmd = str(req.get("cmd", "STATUS")).upper()

            if cmd == "AUTH":
                user = str(req.get("user", os.environ.get("USER", "root")))
                reason = str(req.get("reason", "pam"))
                res = self.authenticate_user(user, reason=reason)
            elif cmd == "DEMO_SUCCESS":
                res = self.simulate_ui_demo("success")
            elif cmd == "DEMO_FAIL":
                res = self.simulate_ui_demo("fail")
            elif cmd == "RELOAD":
                self.config = load_config()
                self.engine.config = self.config
                res = {"ok": True, "status": "RELOADED", "config": self.config}
            elif cmd == "SAVE_ENROLLMENT":
                user = str(req.get("user", ""))
                samples = req.get("samples", [])
                if not user or not samples:
                    res = {"ok": False, "status": "INVALID_ARGS"}
                else:
                    saved_path = self.engine.save_user_embeddings(user, samples)
                    res = {"ok": True, "status": "SAVED", "path": str(saved_path), "count": len(samples)}
            elif cmd == "SAVE_CONFIG":
                new_cfg = req.get("config", {})
                if isinstance(new_cfg, dict):
                    self.config.update(new_cfg)
                    save_config(self.config)
                    self.engine.config = self.config
                res = {"ok": True, "status": "CONFIG_SAVED", "config": self.config}
            elif cmd == "RESET_USER":
                user = str(req.get("user", ""))
                p = self.engine.get_user_profile_path(user)
                if p.exists():
                    p.unlink()
                res = {"ok": True, "status": "RESET", "user": user}
            else:
                # STATUS
                user = str(req.get("user", ""))
                eff_user = self.resolve_effective_username(user) if user else ""
                emb = self.engine.load_user_embeddings(eff_user) if eff_user else []
                res = {
                    "ok": True,
                    "status": "ONLINE",
                    "models_loaded": self.engine.load_models(),
                    "user": eff_user,
                    "enrolled_samples": len(emb),
                    "config": self.config,
                }

            conn.sendall((json.dumps(res) + "\n").encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve_events(self) -> None:
        if EVENTS_SOCKET_PATH.exists():
            EVENTS_SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(EVENTS_SOCKET_PATH))
        os.chmod(EVENTS_SOCKET_PATH, 0o666)
        srv.listen(16)
        while self.running:
            try:
                client, _ = srv.accept()
                with self.event_clients_lock:
                    self.event_clients.append(client)
            except Exception:
                if not self.running:
                    break

    def run(self) -> None:
        self.ensure_run_dir()
        self.engine.load_models()
        self.broadcast_state("IDLE", "")

        if AUTH_SOCKET_PATH.exists():
            AUTH_SOCKET_PATH.unlink()

        ev_thread = threading.Thread(target=self.serve_events, daemon=True)
        ev_thread.start()

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(AUTH_SOCKET_PATH))
        os.chmod(AUTH_SOCKET_PATH, 0o666)
        srv.listen(16)

        def _stop(*_args: Any) -> None:
            self.running = False
            try:
                srv.close()
            except Exception:
                pass
            sys.exit(0)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        print(f"[faceid-daemon] Listening on {AUTH_SOCKET_PATH} (models_loaded={self.engine.load_models()})", flush=True)
        while self.running:
            try:
                conn, _ = srv.accept()
                t = threading.Thread(target=self.handle_auth_client, args=(conn,), daemon=True)
                t.start()
            except Exception:
                if not self.running:
                    break


if __name__ == "__main__":
    FaceIDDaemon().run()
