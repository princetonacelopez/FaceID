import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

export default class FaceIDDynamicIslandExtension extends Extension {
    enable() {
        this._pill = new St.BoxLayout({
            style_class: 'faceid-dynamic-island',
            orientation: Clutter.Orientation.HORIZONTAL,
            reactive: false,
            visible: false,
            opacity: 0,
        });

        this._iconLabel = new St.Label({
            style_class: 'faceid-island-glyph',
            text: '⛶',
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._textBox = new St.BoxLayout({
            style_class: 'faceid-island-textbox',
            orientation: Clutter.Orientation.VERTICAL,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._titleLabel = new St.Label({
            style_class: 'faceid-island-title',
            text: 'Face ID',
        });

        this._statusLabel = new St.Label({
            style_class: 'faceid-island-status',
            text: 'Scanning...',
        });

        this._textBox.add_child(this._titleLabel);
        this._textBox.add_child(this._statusLabel);

        this._badgeLabel = new St.Label({
            style_class: 'faceid-island-badge',
            text: '●',
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._pill.add_child(this._iconLabel);
        this._pill.add_child(this._textBox);
        this._pill.add_child(this._badgeLabel);

        Main.layoutManager.addTopChrome(this._pill, {
            affectsInputRegion: false,
            trackFullscreen: false,
        });

        this._lastSeq = -1;
        this._currentState = 'IDLE';
        this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => {
            this._pollState();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _isGtkLayerHudActive() {
        if (Main.sessionMode.currentMode === 'unlock-dialog') {
            return false; // Always use GNOME Shell HUD on the lock screen
        }
        try {
            const pidFile = Gio.File.new_for_path('/tmp/faceid-gtk-hud.pid');
            if (!pidFile.query_exists(null)) return false;
            const [ok, contents] = pidFile.load_contents(null);
            if (!ok) return false;
            const pid = new TextDecoder().decode(contents).trim();
            const procDir = Gio.File.new_for_path(`/proc/${pid}`);
            return procDir.query_exists(null);
        } catch (_e) {
            return false;
        }
    }

    _pollState() {
        if (this._isGtkLayerHudActive()) {
            if (this._pill.visible) {
                this._pill.visible = false;
            }
            return;
        }

        const paths = ['/run/faceid/state.json', '/tmp/faceid-runtime/state.json'];
        for (const p of paths) {
            try {
                const file = Gio.File.new_for_path(p);
                if (!file.query_exists(null)) continue;
                const [ok, bytes] = file.load_contents(null);
                if (!ok) continue;
                const data = JSON.parse(new TextDecoder().decode(bytes));
                const nowSec = Date.now() / 1000.0;
                let state = data.state || 'IDLE';
                if (state !== 'IDLE' && (nowSec - (data.timestamp || 0)) > 6.0) {
                    state = 'IDLE';
                }
                if (state !== this._currentState || data.seq !== this._lastSeq) {
                    this._lastSeq = data.seq;
                    this._currentState = state;
                    this._updateUI(state, data.message || 'Face ID');
                }
                break;
            } catch (_e) {
                // Ignore transient JSON write races
            }
        }
    }

    _updateUI(state, message) {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor) return;

        if (state === 'IDLE') {
            this._pill.ease({
                opacity: 0,
                duration: 220,
                mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                onComplete: () => {
                    this._pill.visible = false;
                },
            });
            return;
        }

        this._pill.remove_style_class_name('state-matched');
        this._pill.remove_style_class_name('state-failed');
        this._pill.remove_style_class_name('state-scanning');

        if (state === 'MATCHED') {
            this._pill.add_style_class_name('state-matched');
            this._iconLabel.text = '🔓';
            this._statusLabel.text = 'Unlocked';
            this._badgeLabel.text = '✓';
        } else if (state === 'FAILED') {
            this._pill.add_style_class_name('state-failed');
            this._iconLabel.text = '🔒';
            this._statusLabel.text = message || 'Try Again';
            this._badgeLabel.text = '✕';
        } else {
            this._pill.add_style_class_name('state-scanning');
            this._iconLabel.text = '⛶';
            this._statusLabel.text = message || 'Scanning...';
            this._badgeLabel.text = '◉';
        }

        const pillWidth = 310;
        this._pill.set_width(pillWidth);
        this._pill.set_position(
            monitor.x + Math.floor((monitor.width - pillWidth) / 2),
            monitor.y + 8
        );

        if (!this._pill.visible) {
            this._pill.opacity = 0;
            this._pill.visible = true;
        }

        this._pill.ease({
            opacity: 255,
            duration: 200,
            mode: Clutter.AnimationMode.EASE_OUT_BACK,
        });
    }

    disable() {
        if (this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = null;
        }
        if (this._pill) {
            Main.layoutManager.removeChrome(this._pill);
            this._pill.destroy();
            this._pill = null;
        }
    }
}
