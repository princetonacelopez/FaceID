#!/usr/bin/env python3
"""
iPhone Dynamic Island Face ID HUD for Linux (GNOME Wayland/XWayland + Cairo)
Renders a 60fps spring-animated Dynamic Island pill at the top-center of the
primary monitor right beneath the physical webcam whenever PAM or Face ID is active.
Uses an override-redirect RGBA click-through overlay so it works cleanly on
GNOME Mutter without stealing focus or mouse clicks.
"""

from __future__ import annotations

import json
import math
import os
import pwd
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Force X11/XWayland backend so GNOME Mutter allows exact top-center
# override-redirect positioning and click-through input regions.
os.environ["GDK_BACKEND"] = "x11"

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

STATE_PATHS = [
    Path("/run/faceid/state.json"),
    Path("/tmp/faceid-runtime/state.json"),
]


class DynamicIslandOverlay(Gtk.Window):
    CANVAS_W = 420
    CANVAS_H = 96

    def __init__(self) -> None:
        # POPUP window is override-redirect on XWayland: floats above top bar,
        # never appears in Alt-Tab or overview, and never steals keyboard focus.
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_title("Face ID Dynamic Island")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_default_size(self.CANVAS_W, self.CANVAS_H)
        self.set_size_request(self.CANVAS_W, self.CANVAS_H)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_size_request(self.CANVAS_W, self.CANVAS_H)
        self.drawing_area.connect("draw", self.on_draw)
        self.add(self.drawing_area)

        # Animation state
        self.current_state = "IDLE"
        self.message = "Face ID"
        self.progress = 0.0
        self.face_detected = False
        self.show_mini_camera = True
        self.preview_pixbuf: Optional[GdkPixbuf.Pixbuf] = None
        self.last_preview_mtime = 0.0
        self.last_seq = -1

        # Spring physics variables
        self.pill_scale = 0.0       # 0.0 (collapsed) -> 1.0 (expanded)
        self.pill_velocity = 0.0
        self.target_scale = 0.0
        self.phase = 0.0
        self.state_enter_ts = time.monotonic()
        self.last_tick_ts = time.monotonic()
        self._was_drawn_idle = True

        self.connect("realize", self._on_realize)
        self.position_on_primary_monitor()
        self.show_all()

        GLib.timeout_add(16, self.on_tick)       # ~60fps animation loop
        GLib.timeout_add(45, self.poll_state)    # ~22Hz state & thumbnail polling

    def _on_realize(self, *_args: Any) -> None:
        """Make the entire overlay window 100% click-through."""
        gdk_win = self.get_window()
        if gdk_win is not None:
            empty_region = cairo.Region(cairo.RectangleInt(0, 0, 0, 0))
            gdk_win.input_shape_combine_region(empty_region, 0, 0)
            gdk_win.set_override_redirect(True)

    def position_on_primary_monitor(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        mon = display.get_primary_monitor()
        if mon is None and display.get_n_monitors() > 0:
            mon = display.get_monitor(0)
        if mon is not None:
            geom = mon.get_geometry()
            target_x = geom.x + (geom.width - self.CANVAS_W) // 2
            target_y = geom.y + 4
            self.move(target_x, target_y)

    def poll_state(self) -> bool:
        for p in STATE_PATHS:
            if not p.exists():
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data: Dict[str, Any] = json.load(f)
                seq = int(data.get("seq", 0))
                ts = float(data.get("timestamp", 0.0))
                new_state = str(data.get("state", "IDLE"))

                # Auto-expire stale non-IDLE states older than 6 seconds
                if new_state != "IDLE" and (time.time() - ts) > 6.0:
                    new_state = "IDLE"

                if seq != self.last_seq or new_state != self.current_state:
                    self.last_seq = seq
                    if new_state != self.current_state:
                        self.current_state = new_state
                        self.state_enter_ts = time.monotonic()
                        if new_state != "IDLE":
                            self.position_on_primary_monitor()
                    self.message = str(data.get("message", "Face ID")) or "Face ID"
                    self.progress = float(data.get("progress", 0.0))
                    self.face_detected = bool(data.get("face_detected", False))
                    self.show_mini_camera = bool(data.get("show_mini_camera", True))
                    self.target_scale = 0.0 if self.current_state == "IDLE" else 1.0

                preview_path = str(data.get("preview_path", ""))
                if preview_path and self.show_mini_camera and os.path.exists(preview_path):
                    mtime = os.path.getmtime(preview_path)
                    if mtime > self.last_preview_mtime:
                        self.last_preview_mtime = mtime
                        self.preview_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                            preview_path, 48, 48, False
                        )
                break
            except Exception:
                pass
        return True

    def on_tick(self) -> bool:
        # Skip 60fps redraws when completely collapsed & idle
        if self.target_scale == 0.0 and self.pill_scale == 0.0 and self.pill_velocity == 0.0:
            if not self._was_drawn_idle:
                self._was_drawn_idle = True
                self.drawing_area.queue_draw()
            return True

        self._was_drawn_idle = False
        now = time.monotonic()
        dt = min(0.05, max(0.005, now - self.last_tick_ts))
        self.last_tick_ts = now
        self.phase += dt * 4.5

        stiffness = 260.0 if self.target_scale > 0.5 else 210.0
        damping = 22.0
        force = stiffness * (self.target_scale - self.pill_scale) - damping * self.pill_velocity
        self.pill_velocity += force * dt
        self.pill_scale += self.pill_velocity * dt

        if self.target_scale == 0.0 and self.pill_scale < 0.01 and abs(self.pill_velocity) < 0.05:
            self.pill_scale = 0.0
            self.pill_velocity = 0.0

        self.drawing_area.queue_draw()
        return True

    @staticmethod
    def _rounded_rect(
        cr: cairo.Context, x: float, y: float, w: float, h: float, r: float
    ) -> None:
        r = min(r, w * 0.5, h * 0.5)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw_faceid_glyph(
        self, cr: cairo.Context, cx: float, cy: float, size: float, color: tuple
    ) -> None:
        """Draw the authentic Apple Face ID icon (4 brackets + eyes + J-nose + smile + scan wave)."""
        r, g, b, a = color
        half = size * 0.5
        corner_len = size * 0.27
        rad = size * 0.16

        pulse = 1.0 + 0.05 * math.sin(self.phase * 1.4)
        half_p = half * pulse

        cr.save()
        cr.set_source_rgba(r, g, b, a)
        cr.set_line_width(2.2)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        # Top-Left bracket
        cr.move_to(cx - half_p, cy - half_p + corner_len)
        cr.arc(cx - half_p + rad, cy - half_p + rad, rad, math.pi, 1.5 * math.pi)
        cr.line_to(cx - half_p + corner_len, cy - half_p)
        cr.stroke()

        # Top-Right bracket
        cr.move_to(cx + half_p - corner_len, cy - half_p)
        cr.arc(cx + half_p - rad, cy - half_p + rad, rad, 1.5 * math.pi, 2.0 * math.pi)
        cr.line_to(cx + half_p, cy - half_p + corner_len)
        cr.stroke()

        # Bottom-Right bracket
        cr.move_to(cx + half_p, cy + half_p - corner_len)
        cr.arc(cx + half_p - rad, cy + half_p - rad, rad, 0.0, 0.5 * math.pi)
        cr.line_to(cx + half_p - corner_len, cy + half_p)
        cr.stroke()

        # Bottom-Left bracket
        cr.move_to(cx - half_p + corner_len, cy + half_p)
        cr.arc(cx - half_p + rad, cy + half_p - rad, rad, 0.5 * math.pi, math.pi)
        cr.line_to(cx - half_p, cy + half_p - corner_len)
        cr.stroke()

        # Eyes
        eye_x = size * 0.18
        eye_y = cy - size * 0.14
        cr.move_to(cx - eye_x, eye_y - 2.2)
        cr.line_to(cx - eye_x, eye_y + 1.8)
        cr.stroke()

        cr.move_to(cx + eye_x, eye_y - 2.2)
        cr.line_to(cx + eye_x, eye_y + 1.8)
        cr.stroke()

        # Nose J-curve
        cr.move_to(cx + 0.8, cy - size * 0.13)
        cr.line_to(cx + 0.8, cy + size * 0.01)
        cr.arc(cx - 0.8, cy + size * 0.01, 1.6, 0, math.pi * 0.75)
        cr.stroke()

        # Smile arc
        cr.arc(cx, cy + size * 0.06, size * 0.21, 0.18 * math.pi, 0.82 * math.pi)
        cr.stroke()

        # Animated horizontal scan wave inside brackets
        scan_y = cy + math.sin(self.phase * 1.8) * (half * 0.72)
        grad = cairo.LinearGradient(cx - half * 0.8, scan_y, cx + half * 0.8, scan_y)
        grad.add_color_stop_rgba(0.0, r, g, b, 0.0)
        grad.add_color_stop_rgba(0.5, r, g, b, 0.65)
        grad.add_color_stop_rgba(1.0, r, g, b, 0.0)
        cr.set_source(grad)
        cr.set_line_width(1.6)
        cr.move_to(cx - half * 0.75, scan_y)
        cr.line_to(cx + half * 0.75, scan_y)
        cr.stroke()
        cr.restore()

    def _draw_padlock_glyph(
        self, cr: cairo.Context, cx: float, cy: float, size: float, unlocked: bool, color: tuple
    ) -> None:
        r, g, b, a = color
        cr.save()
        cr.set_source_rgba(r, g, b, a)
        cr.set_line_width(2.2)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

        body_w = size * 0.68
        body_h = size * 0.52
        bx = cx - body_w * 0.5
        by = cy - body_h * 0.15

        shackle_r = body_w * 0.30
        if unlocked:
            cr.move_to(cx - shackle_r, by)
            cr.line_to(cx - shackle_r, by - shackle_r * 0.75)
            cr.arc(cx, by - shackle_r * 0.75, shackle_r, math.pi, 1.88 * math.pi)
            cr.stroke()
        else:
            cr.move_to(cx - shackle_r, by)
            cr.line_to(cx - shackle_r, by - shackle_r * 0.5)
            cr.arc(cx, by - shackle_r * 0.5, shackle_r, math.pi, 2.0 * math.pi)
            cr.line_to(cx + shackle_r, by)
            cr.stroke()

        self._rounded_rect(cr, bx, by, body_w, body_h, 4.5)
        cr.stroke()

        if unlocked:
            cr.set_line_width(2.1)
            cr.move_to(cx - 4.0, by + body_h * 0.52)
            cr.line_to(cx - 1.0, by + body_h * 0.72)
            cr.line_to(cx + 4.8, by + body_h * 0.32)
            cr.stroke()
        else:
            cr.arc(cx, by + body_h * 0.5, 1.8, 0, 2 * math.pi)
            cr.fill()
        cr.restore()

    def on_draw(self, widget: Gtk.Widget, cr: cairo.Context) -> bool:
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()

        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.restore()

        if self.pill_scale <= 0.01:
            return False

        s = max(0.0, min(1.12, self.pill_scale))
        alpha = min(1.0, s * 1.25)

        shake_dx = 0.0
        if self.current_state == "FAILED":
            t_fail = time.monotonic() - self.state_enter_ts
            if t_fail < 0.65:
                shake_dx = 13.0 * math.exp(-5.0 * t_fail) * math.sin(34.0 * t_fail)

        pill_w = 96.0 + (332.0 - 96.0) * s
        pill_h = 24.0 + (62.0 - 24.0) * s
        px = (width - pill_w) * 0.5 + shake_dx
        py = 6.0
        radius = pill_h * 0.5

        if self.current_state == "MATCHED":
            accent = (0.188, 0.820, 0.345, alpha)  # iOS Emerald Green #30D158
        elif self.current_state == "FAILED":
            accent = (1.000, 0.271, 0.227, alpha)  # iOS Coral Red #FF453A
        elif self.current_state == "FACE_FOUND":
            accent = (0.039, 0.518, 1.000, alpha)  # iOS Electric Blue #0A84FF
        else:
            accent = (0.392, 0.824, 1.000, alpha)  # iOS Cyan #64D2FF

        # Soft outer glow
        self._rounded_rect(cr, px - 2.0, py - 2.0, pill_w + 4.0, pill_h + 4.0, radius + 2.0)
        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.24 * alpha)
        cr.fill()

        # Deep obsidian pill background (#060609)
        self._rounded_rect(cr, px, py, pill_w, pill_h, radius)
        cr.set_source_rgba(0.024, 0.024, 0.038, 0.97 * alpha)
        cr.fill_preserve()

        # Subtle glass inner border
        cr.set_line_width(1.3)
        cr.set_source_rgba(accent[0], accent[1], accent[2], 0.52 * alpha)
        cr.stroke()

        if s < 0.45:
            return False

        content_alpha = max(0.0, min(1.0, (s - 0.45) / 0.55))
        icon_cx = px + 36.0
        icon_cy = py + pill_h * 0.5

        if self.current_state == "MATCHED":
            self._draw_padlock_glyph(
                cr, icon_cx, icon_cy, 28.0, True, (0.188, 0.820, 0.345, content_alpha)
            )
        elif self.current_state == "FAILED":
            self._draw_padlock_glyph(
                cr, icon_cx, icon_cy, 28.0, False, (1.000, 0.271, 0.227, content_alpha)
            )
        else:
            self._draw_faceid_glyph(
                cr, icon_cx, icon_cy, 27.0, (accent[0], accent[1], accent[2], content_alpha)
            )

        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(14.0)
        cr.set_source_rgba(0.97, 0.98, 1.0, content_alpha)
        cr.move_to(px + 66.0, py + 26.0)
        cr.show_text("Face ID")

        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12.0)
        if self.current_state == "MATCHED":
            cr.set_source_rgba(0.188, 0.820, 0.345, content_alpha)
        elif self.current_state == "FAILED":
            cr.set_source_rgba(1.000, 0.410, 0.380, content_alpha)
        else:
            cr.set_source_rgba(0.68, 0.72, 0.79, content_alpha)
        cr.move_to(px + 66.0, py + 44.0)
        cr.show_text(self.message)
        cr.restore()

        ring_cx = px + pill_w - 34.0
        ring_cy = py + pill_h * 0.5
        ring_r = 20.0

        cr.save()
        if self.show_mini_camera and self.preview_pixbuf is not None:
            cr.arc(ring_cx, ring_cy, ring_r - 1.5, 0, 2 * math.pi)
            cr.clip()
            Gdk.cairo_set_source_pixbuf(
                cr,
                self.preview_pixbuf,
                ring_cx - 24.0,
                ring_cy - 24.0,
            )
            cr.paint_with_alpha(content_alpha)
        else:
            cr.arc(ring_cx, ring_cy, ring_r - 2.0, 0, 2 * math.pi)
            cr.set_source_rgba(0.08, 0.10, 0.14, content_alpha)
            cr.fill()
            cr.arc(ring_cx, ring_cy, 4.5, 0, 2 * math.pi)
            cr.set_source_rgba(accent[0], accent[1], accent[2], 0.8 * content_alpha)
            cr.fill()
        cr.restore()

        cr.save()
        cr.set_line_width(2.4)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.arc(ring_cx, ring_cy, ring_r, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.14 * content_alpha)
        cr.stroke()

        cr.set_source_rgba(accent[0], accent[1], accent[2], content_alpha)
        if self.current_state == "MATCHED":
            cr.arc(ring_cx, ring_cy, ring_r, 0, 2 * math.pi)
            cr.stroke()
        else:
            start_ang = self.phase * 1.6
            span = max(0.35, min(0.92, self.progress)) * 2 * math.pi
            cr.arc(ring_cx, ring_cy, ring_r, start_ang, start_ang + span)
            cr.stroke()
        cr.restore()
        return False


def start_fallback_socket_relay() -> None:
    """Relay /tmp/faceid-runtime/auth.sock -> /run/faceid/auth.sock for PAM clients."""
    rt_dir = Path("/tmp/faceid-runtime")
    rt_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(rt_dir, 0o777)
    except Exception:
        pass
    relay_sock = rt_dir / "auth.sock"
    if relay_sock.exists():
        try:
            relay_sock.unlink()
        except Exception:
            return

    def _handle(client: socket.socket) -> None:
        try:
            data = client.recv(65536)
            if not data:
                return
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(7.0)
                upstream.connect("/run/faceid/auth.sock")
                upstream.sendall(data)
                resp = upstream.recv(65536)
                if resp:
                    client.sendall(resp)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _serve() -> None:
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(relay_sock))
            os.chmod(relay_sock, 0o666)
            srv.listen(8)
            while True:
                c, _ = srv.accept()
                threading.Thread(target=_handle, args=(c,), daemon=True).start()
        except Exception:
            pass

    threading.Thread(target=_serve, daemon=True).start()


def start_lockscreen_watcher() -> None:
    """
    Listen for org.gnome.ScreenSaver WakeUpScreen D-Bus signals so Face ID
    triggers on demand when the user wakes the lock screen (never polling
    the webcam continuously while the user is away from their desk).
    """
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    auth_in_flight = threading.Lock()
    last_trigger = [0.0]

    def _trigger_auth() -> None:
        now = time.monotonic()
        if (now - last_trigger[0]) < 3.0 or not auth_in_flight.acquire(blocking=False):
            return
        last_trigger[0] = now
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(6.0)
                s.connect("/run/faceid/auth.sock")
                req = json.dumps(
                    {"cmd": "AUTH", "user": user, "reason": "lockscreen"}
                ) + "\n"
                s.sendall(req.encode("utf-8"))
                s.recv(4096)
        except Exception:
            pass
        finally:
            auth_in_flight.release()

    def _on_signal(
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        signal_name: str,
        _params: GLib.Variant,
    ) -> None:
        if signal_name == "WakeUpScreen":
            threading.Thread(target=_trigger_auth, daemon=True).start()

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.signal_subscribe(
            "org.gnome.ScreenSaver",
            "org.gnome.ScreenSaver",
            "WakeUpScreen",
            "/org/gnome/ScreenSaver",
            None,
            Gio.DBusSignalFlags.NONE,
            _on_signal,
        )
    except Exception:
        pass


def _wait_for_display(timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        ok, _ = Gtk.init_check(sys.argv)
        if ok:
            return
        time.sleep(0.5)


def main() -> None:
    _wait_for_display()

    try:
        Path("/tmp/faceid-gtk-hud.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    start_fallback_socket_relay()
    start_lockscreen_watcher()

    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())

    _overlay = DynamicIslandOverlay()
    Gtk.main()


if __name__ == "__main__":
    main()
