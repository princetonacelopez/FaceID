#!/usr/bin/env python3
"""
iPhone-Style "Set Up Face ID" Enrollment & Settings App (GTK4 + Cairo)
Features the signature circular live camera viewport surrounded by 64 radial
tick segments that light up in emerald green (#30D158) as you slowly rotate
your head, plus one-click Dynamic Island live testing and security controls.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cairo
import cv2
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
from face_engine import FaceIDEngine, FaceObservation, load_config, save_config


def send_daemon_cmd(payload: Dict[str, Any], timeout: float = 6.0) -> Dict[str, Any]:
    for sock_path in ("/run/faceid/auth.sock", "/tmp/faceid-runtime/auth.sock"):
        if not os.path.exists(sock_path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(sock_path)
                s.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
                raw = s.recv(65536).decode("utf-8", errors="replace").strip()
                if raw:
                    return json.loads(raw)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "Daemon socket not reachable"}


class FaceIDSetupWindow(Gtk.ApplicationWindow):
    NUM_TICKS = 64
    TARGET_SAMPLES = 24

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="Face ID & Passcode")
        self.set_default_size(520, 740)
        self.set_resizable(False)

        self.username = os.environ.get("SUDO_USER") or os.environ.get("USER") or "user"
        self.config = load_config()
        self.engine = FaceIDEngine(config=self.config)
        self.engine.load_models()

        self.cap: Optional[cv2.VideoCapture] = None
        self.enrolling = False
        self.enrollment_complete = False
        self.current_pixbuf: Optional[GdkPixbuf.Pixbuf] = None
        self.last_obs: Optional[FaceObservation] = None
        self.collected_samples: List[Dict[str, Any]] = []
        self.tick_progress: List[float] = [0.0] * self.NUM_TICKS
        self.phase = 0.0

        self._apply_ios_dark_css()
        self._build_ui()
        self._refresh_status_badge()

        GLib.timeout_add(30, self._on_frame_tick)

    def _apply_ios_dark_css(self) -> None:
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            window {
                background-color: #090A0F;
                color: #F5F7FA;
            }
            .ios-card {
                background-color: #141622;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
                padding: 16px;
            }
            .ios-title {
                font-size: 24px;
                font-weight: 800;
                color: #FFFFFF;
            }
            .ios-subtitle {
                font-size: 13px;
                color: #9BA3B4;
            }
            .ios-instruction {
                font-size: 16px;
                font-weight: 600;
                color: #E2E8F0;
            }
            .ios-btn-primary {
                background: #0A84FF;
                color: #FFFFFF;
                font-weight: 700;
                font-size: 14px;
                border-radius: 12px;
                padding: 11px 20px;
                border: none;
            }
            .ios-btn-primary:hover {
                background: #2492FF;
            }
            .ios-btn-success {
                background: #30D158;
                color: #051609;
                font-weight: 700;
                font-size: 14px;
                border-radius: 12px;
                padding: 11px 20px;
                border: none;
            }
            .ios-btn-secondary {
                background: #1E2235;
                color: #E2E8F0;
                font-weight: 600;
                font-size: 13px;
                border-radius: 12px;
                padding: 10px 16px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            .ios-btn-danger {
                background: rgba(255, 69, 58, 0.16);
                color: #FF453A;
                font-weight: 600;
                font-size: 13px;
                border-radius: 12px;
                padding: 10px 16px;
                border: 1px solid rgba(255, 69, 58, 0.35);
            }
            .status-pill-enrolled {
                background: rgba(48, 209, 88, 0.16);
                color: #30D158;
                border: 1px solid rgba(48, 209, 88, 0.45);
                border-radius: 999px;
                padding: 5px 14px;
                font-weight: 700;
                font-size: 12px;
            }
            .status-pill-empty {
                background: rgba(255, 159, 10, 0.16);
                color: #FF9F0A;
                border: 1px solid rgba(255, 159, 10, 0.45);
                border-radius: 999px;
                padding: 5px 14px;
                font-weight: 700;
                font-size: 12px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.set_margin_top(20)
        root.set_margin_bottom(20)
        root.set_margin_start(24)
        root.set_margin_end(24)
        self.set_child(root)

        # Header + Status Pill
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_col.set_hexpand(True)

        title = Gtk.Label(label="Face ID & Dynamic Island", xalign=0)
        title.add_css_class("ios-title")
        subtitle = Gtk.Label(
            label=f"Biometric authentication for @{self.username} on Fedora 45",
            xalign=0,
        )
        subtitle.add_css_class("ios-subtitle")
        title_col.append(title)
        title_col.append(subtitle)

        self.status_badge = Gtk.Label(label="Checking...")
        self.status_badge.set_valign(Gtk.Align.CENTER)
        header_box.append(title_col)
        header_box.append(self.status_badge)
        root.append(header_box)

        # Circular Face ID Scanner Canvas
        self.scanner_area = Gtk.DrawingArea()
        self.scanner_area.set_content_width(300)
        self.scanner_area.set_content_height(300)
        self.scanner_area.set_draw_func(self._on_draw_scanner)
        root.append(self.scanner_area)

        # Instruction prompt below ring
        self.instruction_label = Gtk.Label(
            label="Click 'Set Up Face ID' to scan your face in a circle"
        )
        self.instruction_label.add_css_class("ios-instruction")
        self.instruction_label.set_wrap(True)
        self.instruction_label.set_justify(Gtk.Justification.CENTER)
        root.append(self.instruction_label)

        # Primary Action Buttons Row
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.CENTER)

        self.enroll_btn = Gtk.Button(label="Set Up Face ID")
        self.enroll_btn.add_css_class("ios-btn-primary")
        self.enroll_btn.connect("clicked", self._on_toggle_enroll)
        btn_row.append(self.enroll_btn)

        self.test_live_btn = Gtk.Button(label="Test Live Face ID")
        self.test_live_btn.add_css_class("ios-btn-success")
        self.test_live_btn.connect("clicked", self._on_test_live)
        btn_row.append(self.test_live_btn)

        self.demo_hud_btn = Gtk.Button(label="Preview Island HUD")
        self.demo_hud_btn.add_css_class("ios-btn-secondary")
        self.demo_hud_btn.connect("clicked", self._on_demo_hud)
        btn_row.append(self.demo_hud_btn)

        root.append(btn_row)

        # Settings Card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("ios-card")

        # Row 1: Security Mode Dropdown
        sec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sec_lbl = Gtk.Label(label="Liveness & Spoof Protection", xalign=0)
        sec_lbl.set_hexpand(True)
        self.sec_combo = Gtk.DropDown.new_from_strings(
            [
                "Smart Balance (<0.5s + Anti-Spoof)",
                "Maximum Speed (Instant Match)",
                "Strict Liveness (Require Head Motion)",
            ]
        )
        mode_map = {"smart": 0, "fast": 1, "strict": 2}
        self.sec_combo.set_selected(mode_map.get(str(self.config.get("security_mode", "smart")), 0))
        self.sec_combo.connect("notify::selected", self._on_settings_changed)
        sec_row.append(sec_lbl)
        sec_row.append(self.sec_combo)
        card.append(sec_row)

        # Row 2: Mini Camera Ring in Dynamic Island
        cam_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cam_lbl = Gtk.Label(label="Show Live Mini Camera Ring in Dynamic Island", xalign=0)
        cam_lbl.set_hexpand(True)
        self.mini_cam_switch = Gtk.Switch()
        self.mini_cam_switch.set_active(bool(self.config.get("show_mini_camera", True)))
        self.mini_cam_switch.set_valign(Gtk.Align.CENTER)
        self.mini_cam_switch.connect("notify::active", self._on_settings_changed)
        cam_row.append(cam_lbl)
        cam_row.append(self.mini_cam_switch)
        card.append(cam_row)

        # Row 3: Reset Face ID Profile
        footer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        info_lbl = Gtk.Label(
            label="Use 'faceidctl status' or 'sudo faceidctl disable' anytime in terminal.",
            xalign=0,
        )
        info_lbl.add_css_class("ios-subtitle")
        info_lbl.set_hexpand(True)

        self.reset_btn = Gtk.Button(label="Reset Face ID")
        self.reset_btn.add_css_class("ios-btn-danger")
        self.reset_btn.connect("clicked", self._on_reset_profile)
        footer_row.append(info_lbl)
        footer_row.append(self.reset_btn)
        card.append(footer_row)

        root.append(card)

    def _refresh_status_badge(self) -> None:
        d_res = send_daemon_cmd({"cmd": "STATUS", "user": self.username}, timeout=2.0)
        count = int(d_res.get("enrolled_samples", 0)) if d_res.get("ok") else 0
        if count == 0:
            embeddings = self.engine.load_user_embeddings(self.username)
            count = len(embeddings)
        for cls in ("status-pill-enrolled", "status-pill-empty"):
            self.status_badge.remove_css_class(cls)
        if count > 0:
            self.status_badge.set_label(f"● Enrolled ({count} angles)")
            self.status_badge.add_css_class("status-pill-enrolled")
        else:
            self.status_badge.set_label("○ Not Enrolled")
            self.status_badge.add_css_class("status-pill-empty")

    def _on_toggle_enroll(self, _btn: Gtk.Button) -> None:
        if self.enrolling:
            self._stop_enrollment(completed=False)
            return

        self.cap = self.engine.open_camera()
        if self.cap is None:
            self.instruction_label.set_label(
                "Could not open webcam (/dev/video0). Make sure no other app is using it."
            )
            return

        self.enrolling = True
        self.enrollment_complete = False
        self.collected_samples = []
        self.tick_progress = [0.0] * self.NUM_TICKS
        self.enroll_btn.set_label("Cancel Scan")
        self.instruction_label.set_label(
            "Position your face within the frame and move your head slowly in a circle."
        )

    def _stop_enrollment(self, completed: bool = False) -> None:
        self.enrolling = False
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.enroll_btn.set_label("Re-Enroll Face ID" if completed else "Set Up Face ID")

        if completed and self.collected_samples:
            res = send_daemon_cmd(
                {
                    "cmd": "SAVE_ENROLLMENT",
                    "user": self.username,
                    "samples": self.collected_samples,
                }
            )
            # Also save a local backup copy in ~/.local/share/faceid/users/
            try:
                local_dir = Path.home() / ".local/share/faceid/users"
                local_dir.mkdir(parents=True, exist_ok=True)
                with open(local_dir / f"{self.username}.json", "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "username": self.username,
                            "sample_count": len(self.collected_samples),
                            "embeddings": self.collected_samples,
                        },
                        f,
                        separators=(",", ":"),
                    )
            except Exception:
                pass
            if not res.get("ok"):
                self.engine.save_user_embeddings(self.username, self.collected_samples)
            self.enrollment_complete = True
            self.instruction_label.set_label(
                "✓ Face ID is Now Set Up! Click 'Test Live Face ID' to see the Dynamic Island."
            )
            self._refresh_status_badge()
            # Trigger a celebratory Dynamic Island unlock animation
            threading.Thread(
                target=lambda: send_daemon_cmd({"cmd": "DEMO_SUCCESS"}), daemon=True
            ).start()

    def _on_frame_tick(self) -> bool:
        self.phase += 0.08
        if self.enrolling and self.cap is not None:
            ok, frame = self.cap.read()
            if ok and frame is not None:
                obs = self.engine.analyze_frame(frame)
                self.last_obs = obs

                # Mirror for natural selfie view & crop square
                mirrored = cv2.flip(frame, 1)
                fh, fw = mirrored.shape[:2]
                side = min(fh, fw)
                y0 = (fh - side) // 2
                x0 = (fw - side) // 2
                square = mirrored[y0 : y0 + side, x0 : x0 + side]
                rgb = cv2.cvtColor(square, cv2.COLOR_BGR2RGB)
                rgb_resized = cv2.resize(rgb, (220, 220), interpolation=cv2.INTER_AREA)

                h, w, c = rgb_resized.shape
                self.current_pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                    rgb_resized.tobytes(),
                    GdkPixbuf.Colorspace.RGB,
                    False,
                    8,
                    w,
                    h,
                    w * c,
                )

                if obs is not None and obs.sharpness >= 10.0:
                    self._record_enrollment_sample(obs)

        self.scanner_area.queue_draw()
        return True

    def _record_enrollment_sample(self, obs: FaceObservation) -> None:
        # Ensure minimum embedding or pose diversity from the previous sample
        if self.collected_samples:
            prev_vec = np.array(self.collected_samples[-1]["vector"], dtype=np.float32).reshape(1, 128)
            delta = float(np.linalg.norm(obs.embedding - prev_vec))
            if delta < 0.012 and len(self.collected_samples) > 3:
                # Gently prompt user to tilt head slightly
                self.instruction_label.set_label(
                    f"Keep tilting your head slowly to complete the circle ({len(self.collected_samples)}/{self.TARGET_SAMPLES})"
                )
                return

        # Map head pose angle to radial tick segment + progressive fill
        angle = math.atan2(obs.pitch, -obs.yaw)  # [-pi, pi]
        center_tick = int(((angle + math.pi) / (2 * math.pi)) * self.NUM_TICKS) % self.NUM_TICKS
        for offset in range(-3, 4):
            self.tick_progress[(center_tick + offset) % self.NUM_TICKS] = 1.0

        self.collected_samples.append(
            {
                "vector": [round(float(x), 5) for x in obs.embedding.flatten()],
                "yaw": round(obs.yaw, 3),
                "pitch": round(obs.pitch, 3),
                "sharpness": round(obs.sharpness, 1),
            }
        )

        # Also smoothly fill remaining ticks proportionally to sample count
        ratio = len(self.collected_samples) / float(self.TARGET_SAMPLES)
        upto = min(self.NUM_TICKS, int(ratio * self.NUM_TICKS))
        for i in range(upto):
            self.tick_progress[i] = 1.0

        pct = min(100, int(ratio * 100))
        self.instruction_label.set_label(
            f"Move your head slowly to complete the circle ({pct}%)"
        )

        if len(self.collected_samples) >= self.TARGET_SAMPLES:
            self.tick_progress = [1.0] * self.NUM_TICKS
            self._stop_enrollment(completed=True)

    def _on_draw_scanner(
        self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int
    ) -> None:
        cx = width * 0.5
        cy = height * 0.5
        inner_r = 104.0
        tick_inner = 114.0
        tick_outer = 132.0

        # Draw 64 radial iOS Face ID ticks around the circle
        for i in range(self.NUM_TICKS):
            ang = (i / float(self.NUM_TICKS)) * 2.0 * math.pi - math.pi * 0.5
            filled = self.tick_progress[i] > 0.5 or self.enrollment_complete

            # Subtle 3D radial pulse on active ticks
            extra_len = 3.0 * math.sin(self.phase + i * 0.25) if self.enrolling else 0.0
            r0 = tick_inner
            r1 = tick_outer + (extra_len if filled else 0.0)

            x0 = cx + math.cos(ang) * r0
            y0 = cy + math.sin(ang) * r0
            x1 = cx + math.cos(ang) * r1
            y1 = cy + math.sin(ang) * r1

            cr.set_line_width(3.2)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            if filled:
                cr.set_source_rgba(0.188, 0.820, 0.345, 0.96)  # iOS Emerald #30D158
            elif self.enrolling:
                cr.set_source_rgba(0.392, 0.824, 1.000, 0.28)
            else:
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.16)

            cr.move_to(x0, y0)
            cr.line_to(x1, y1)
            cr.stroke()

        # Draw circular camera viewport or Face ID hero badge
        cr.save()
        cr.arc(cx, cy, inner_r, 0, 2 * math.pi)
        cr.clip()

        if self.enrolling and self.current_pixbuf is not None:
            Gdk.cairo_set_source_pixbuf(
                cr, self.current_pixbuf, cx - 110.0, cy - 110.0
            )
            cr.paint()

            # Subtle cyan face-tracking reticle overlay when face is detected
            if self.last_obs is not None:
                cr.set_source_rgba(0.188, 0.820, 0.345, 0.55)
                cr.set_line_width(1.6)
                cr.arc(
                    cx - self.last_obs.yaw * 22.0,
                    cy + self.last_obs.pitch * 22.0,
                    58.0,
                    0,
                    2 * math.pi,
                )
                cr.stroke()
        else:
            # Dark glass circle with Apple Face ID vector glyph or green checkmark
            grad = cairo.RadialGradient(cx, cy - 20.0, 10.0, cx, cy, inner_r)
            grad.add_color_stop_rgba(0.0, 0.10, 0.14, 0.24, 1.0)
            grad.add_color_stop_rgba(1.0, 0.05, 0.06, 0.10, 1.0)
            cr.set_source(grad)
            cr.paint()

            if self.enrollment_complete:
                cr.set_source_rgba(0.188, 0.820, 0.345, 1.0)
                cr.set_line_width(6.0)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.move_to(cx - 28.0, cy + 2.0)
                cr.line_to(cx - 6.0, cy + 24.0)
                cr.line_to(cx + 32.0, cy - 20.0)
                cr.stroke()
            else:
                # Draw big centered Face ID glyph
                self._draw_hero_faceid(cr, cx, cy, 76.0)

        cr.restore()

        # Inner circular rim
        cr.arc(cx, cy, inner_r, 0, 2 * math.pi)
        cr.set_line_width(2.0)
        if self.enrollment_complete:
            cr.set_source_rgba(0.188, 0.820, 0.345, 0.85)
        else:
            cr.set_source_rgba(0.392, 0.824, 1.000, 0.45)
        cr.stroke()

    @staticmethod
    def _draw_hero_faceid(cr: cairo.Context, cx: float, cy: float, size: float) -> None:
        half = size * 0.5
        c_len = size * 0.26
        rad = size * 0.16
        cr.save()
        cr.set_source_rgba(0.392, 0.824, 1.000, 0.92)
        cr.set_line_width(3.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # 4 brackets
        cr.move_to(cx - half, cy - half + c_len)
        cr.arc(cx - half + rad, cy - half + rad, rad, math.pi, 1.5 * math.pi)
        cr.line_to(cx - half + c_len, cy - half)
        cr.stroke()

        cr.move_to(cx + half - c_len, cy - half)
        cr.arc(cx + half - rad, cy - half + rad, rad, 1.5 * math.pi, 2.0 * math.pi)
        cr.line_to(cx + half, cy - half + c_len)
        cr.stroke()

        cr.move_to(cx + half, cy + half - c_len)
        cr.arc(cx + half - rad, cy + half - rad, rad, 0.0, 0.5 * math.pi)
        cr.line_to(cx + half - c_len, cy + half)
        cr.stroke()

        cr.move_to(cx - half + c_len, cy + half)
        cr.arc(cx - half + rad, cy + half - rad, rad, 0.5 * math.pi, math.pi)
        cr.line_to(cx - half, cy + half - c_len)
        cr.stroke()

        # Eyes, Nose, Smile
        ex = size * 0.18
        ey = cy - size * 0.14
        cr.move_to(cx - ex, ey - 4.5)
        cr.line_to(cx - ex, ey + 4.0)
        cr.move_to(cx + ex, ey - 4.5)
        cr.line_to(cx + ex, ey + 4.0)
        cr.stroke()

        cr.move_to(cx + 2.0, cy - size * 0.13)
        cr.line_to(cx + 2.0, cy + size * 0.02)
        cr.arc(cx - 2.0, cy + size * 0.02, 4.0, 0, math.pi * 0.75)
        cr.stroke()

        cr.arc(cx, cy + size * 0.06, size * 0.21, 0.18 * math.pi, 0.82 * math.pi)
        cr.stroke()
        cr.restore()

    def _on_test_live(self, _btn: Gtk.Button) -> None:
        if self.enrolling:
            self._stop_enrollment(completed=False)
        self.instruction_label.set_label(
            "Look at the top-center of your screen — scanning with Dynamic Island..."
        )

        def _worker() -> None:
            res = send_daemon_cmd({"cmd": "AUTH", "user": self.username, "reason": "setup-test"}, timeout=8.0)
            ok = bool(res.get("ok"))
            score = res.get("score", 0.0)
            msg = (
                f"✓ Face ID Matched! (Cosine similarity: {score})"
                if ok
                else f"✕ Not Matched ({res.get('message', res.get('status', 'Failed'))})"
            )
            GLib.idle_add(self.instruction_label.set_label, msg)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_demo_hud(self, _btn: Gtk.Button) -> None:
        self.instruction_label.set_label("Playing Dynamic Island animation at the top-center of your screen...")
        threading.Thread(
            target=lambda: send_daemon_cmd({"cmd": "DEMO_SUCCESS"}), daemon=True
        ).start()

    def _on_settings_changed(self, *_args: Any) -> None:
        idx = self.sec_combo.get_selected()
        modes = {0: "smart", 1: "fast", 2: "strict"}
        self.config["security_mode"] = modes.get(idx, "smart")
        self.config["show_mini_camera"] = bool(self.mini_cam_switch.get_active())
        save_config(self.config)
        threading.Thread(
            target=lambda: send_daemon_cmd({"cmd": "SAVE_CONFIG", "config": self.config}),
            daemon=True,
        ).start()

    def _on_reset_profile(self, _btn: Gtk.Button) -> None:
        if self.enrolling:
            self._stop_enrollment(completed=False)
        send_daemon_cmd({"cmd": "RESET_USER", "user": self.username})
        p = self.engine.get_user_profile_path(self.username)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
        self.enrollment_complete = False
        self.tick_progress = [0.0] * self.NUM_TICKS
        self._refresh_status_badge()
        self.instruction_label.set_label("Face ID profile reset. Click 'Set Up Face ID' to enroll again.")


class FaceIDSetupApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.serenevolta.FaceIDSetup")

    def do_activate(self) -> None:
        win = FaceIDSetupWindow(self)
        win.present()


if __name__ == "__main__":
    app = FaceIDSetupApp()
    app.run(None)
