#!/usr/bin/env bash
# =============================================================================
# Fedora 45 iPhone Face ID & Dynamic Island Full Installer
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${SUDO_USER:-}" ]]; then
    TARGET_USER="$SUDO_USER"
elif [[ -n "${PKEXEC_UID:-}" ]]; then
    TARGET_USER="$(id -nu "$PKEXEC_UID")"
else
    TARGET_USER="$(id -nu 1000 2>/dev/null || echo "$USER")"
fi
TARGET_HOME="$(eval echo "~$TARGET_USER")"

as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

echo "=================================================================="
echo "  Installing iPhone Face ID & Dynamic Island for Fedora 45"
echo "  Target Desktop User: $TARGET_USER ($TARGET_HOME)"
echo "=================================================================="

# 1. Install system dependencies
echo "[1/7] Installing python3-opencv and pam-devel..."
as_root dnf install -y python3-opencv pam-devel checkpolicy policycoreutils-python-utils curl

# 2. Download OpenCV Zoo YuNet & SFace ONNX Models
echo "[2/7] Downloading YuNet & SFace ONNX neural models..."
as_root mkdir -p /var/lib/faceid/models /var/lib/faceid/users /etc/faceid
mkdir -p "$SCRIPT_DIR/models"

YUNET_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
SFACE_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"

if [[ ! -s "$SCRIPT_DIR/models/face_detection_yunet_2023mar.onnx" ]]; then
    curl -L --fail --retry 3 -o "$SCRIPT_DIR/models/face_detection_yunet_2023mar.onnx" "$YUNET_URL"
fi
if [[ ! -s "$SCRIPT_DIR/models/face_recognition_sface_2021dec.onnx" ]]; then
    curl -L --fail --retry 3 -o "$SCRIPT_DIR/models/face_recognition_sface_2021dec.onnx" "$SFACE_URL"
fi

as_root cp -f "$SCRIPT_DIR/models/"*.onnx /var/lib/faceid/models/
as_root chmod 755 /var/lib/faceid /var/lib/faceid/models /var/lib/faceid/users

# 3. Compile Native PAM Module (/lib64/security/pam_faceid.so)
echo "[3/7] Compiling pam_faceid.so..."
gcc -Wall -Wextra -Werror -O2 -fPIC -shared \
    "$SCRIPT_DIR/pam/pam_faceid.c" \
    -o "$SCRIPT_DIR/pam/pam_faceid.so" /lib64/libpam.so.0
as_root install -m 0755 "$SCRIPT_DIR/pam/pam_faceid.so" /lib64/security/pam_faceid.so

# 4. Compile & Load SELinux Policy Module for GDM / Sudo / Polkit / Lock Screen
echo "[4/7] Compiling and loading SELinux policy module (faceid_selinux)..."
pushd "$SCRIPT_DIR/pam" >/dev/null
checkmodule -M -m -o faceid_selinux.mod faceid_selinux.te
semodule_package -o faceid_selinux.pp -m faceid_selinux.mod
as_root semodule -i faceid_selinux.pp || true
popd >/dev/null

# 5. Deploy Engine, UI, CLI, and systemd Root Daemon
echo "[5/7] Deploying /opt/faceid and faceid-daemon.service..."
as_root mkdir -p /opt/faceid/{engine,ui,bin,pam,models}
as_root cp -rf "$SCRIPT_DIR/engine/"* /opt/faceid/engine/
as_root cp -rf "$SCRIPT_DIR/ui/"* /opt/faceid/ui/
as_root cp -rf "$SCRIPT_DIR/bin/"* /opt/faceid/bin/
as_root cp -rf "$SCRIPT_DIR/models/"*.onnx /opt/faceid/models/
as_root chmod +x /opt/faceid/engine/*.py /opt/faceid/ui/*.py /opt/faceid/bin/faceidctl
as_root chmod -R a+r /var/lib/faceid/users 2>/dev/null || true
as_root ln -sf /opt/faceid/bin/faceidctl /usr/local/bin/faceidctl

as_root tee /etc/systemd/system/faceid-daemon.service >/dev/null <<'EOF'
[Unit]
Description=Face ID Neural Biometric Authentication Daemon
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/faceid/engine/daemon.py
Restart=always
RestartSec=1
User=root
Group=root
RuntimeDirectory=faceid
RuntimeDirectoryMode=0755

[Install]
WantedBy=multi-user.target
EOF

as_root systemctl daemon-reload
as_root systemctl enable --now faceid-daemon.service

# 6. Install GNOME Shell 51 Extension + Desktop Dynamic Island HUD Overlay + App Launcher
echo "[6/7] Installing Dynamic Island HUD & GNOME Shell 51 Extension..."
EXT_UUID="faceid-dynamic-island@serene-volta"
EXT_DEST="$TARGET_HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
mkdir -p "$EXT_DEST"
cp -rf "$SCRIPT_DIR/gnome-extension/$EXT_UUID/"* "$EXT_DEST/"

mkdir -p "$TARGET_HOME/.config/autostart" "$TARGET_HOME/.local/share/applications"

cat > "$TARGET_HOME/.config/autostart/faceid-dynamic-island.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Face ID Dynamic Island HUD
Comment=Top-center iPhone Dynamic Island overlay for Face ID
Exec=/usr/bin/python3 /opt/faceid/ui/dynamic_island_hud.py
Icon=camera-web
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

cat > "$TARGET_HOME/.local/share/applications/faceid-setup.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Face ID & Passcode
Comment=Configure iPhone-style Face ID and Dynamic Island HUD
Exec=/usr/bin/python3 /opt/faceid/ui/setup_app.py
Icon=security-high-symbolic
Terminal=false
Categories=Settings;Security;System;
Keywords=FaceID;Biometrics;Camera;Login;DynamicIsland;
EOF

chown -R "$TARGET_USER:$TARGET_USER" \
    "$EXT_DEST" \
    "$TARGET_HOME/.config/autostart/faceid-dynamic-island.desktop" \
    "$TARGET_HOME/.local/share/applications/faceid-setup.desktop"

# Enable GNOME extension if gnome-extensions CLI is available
sudo -u "$TARGET_USER" gnome-extensions enable "$EXT_UUID" 2>/dev/null || true

# 7. Enable PAM authentication hooks
echo "[7/7] Enabling PAM hooks (system-auth & password-auth)..."
as_root /usr/local/bin/faceidctl enable

echo "=================================================================="
echo "  Face ID & Dynamic Island Installed!"
echo "  Run 'faceidctl status' or 'faceidctl enroll' to enroll your face."
echo "=================================================================="
