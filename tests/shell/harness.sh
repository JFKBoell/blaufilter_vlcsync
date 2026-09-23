#!/usr/bin/env bash
# Shared scaffolding for the shell tests.
#
# The deploy scripts talk to a Raspberry Pi: NetworkManager, systemd, the DRM
# driver, raspi-config, whiptail. All of that is stubbed here so the logic
# around it can be tested on any machine — which is where the bugs actually
# were (a grep that found nothing killing the menu, a role change wiping
# settings, cmdline.txt rewrites).
set -u

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export REPO_ROOT

PASSED=0
FAILED=0
CURRENT_TEST=""

# ------------------------------------------------------------- assertions

ok()   { PASSED=$((PASSED + 1)); printf '    ok   %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '    FAIL %s\n' "$1"; [[ $# -gt 1 ]] && printf '         %s\n' "$2"; }

assert_eq() {  # actual expected description
    if [[ "$1" == "$2" ]]; then ok "$3"; else fail "$3" "erwartet '$2', war '$1'"; fi
}

assert_contains() {  # haystack needle description
    if [[ "$1" == *"$2"* ]]; then ok "$3"; else fail "$3" "'$2' fehlt in: $1"; fi
}

assert_not_contains() {  # haystack needle description
    if [[ "$1" != *"$2"* ]]; then ok "$3"; else fail "$3" "'$2' sollte fehlen in: $1"; fi
}

assert_rc() {  # actual-rc expected-rc description
    if [[ "$1" == "$2" ]]; then ok "$3"; else fail "$3" "Rückgabe $1, erwartet $2"; fi
}

assert_file_contains() {  # file needle description
    local content; content=$(cat "$1" 2>/dev/null)
    assert_contains "$content" "$2" "$3"
}

# --------------------------------------------------------------- sandbox

# Creates a throwaway system: config, boot files, fake DRM tree, stub commands.
# Call before sourcing the script under test; it exports the paths the scripts
# read, so nothing touches the real machine.
make_sandbox() {
    SANDBOX=$(mktemp -d)
    export SANDBOX
    mkdir -p "$SANDBOX"/{etc,boot,bin,drm/card1-HDMI-A-1,theme,repo/deploy/steps,log}

    export CONFIG="$SANDBOX/etc/config"
    export BOOT_DIR="$SANDBOX/boot"
    export CMDLINE="$SANDBOX/boot/cmdline.txt"
    export DRM_ROOT="$SANDBOX/drm"
    export MOUNTS="$SANDBOX/etc/mounts"
    export SPLASH_TARGET="$SANDBOX/theme/splash.png"
    export VIDEO="$SANDBOX/video.mp4"
    export STUB_LOG="$SANDBOX/log/commands.log"
    : > "$STUB_LOG"

    printf '[blaufilter]\ndevice_id = 2\nrole = client\nssid = Blaufilter\nopen_wifi = 0\ndebug_pin = 1234\nrepo_dir = %s/repo\n' \
        "$SANDBOX" > "$CONFIG"
    printf 'console=tty1 root=PARTUUID=ab12cd34-02 rootfstype=ext4 rootwait quiet splash\n' > "$CMDLINE"
    printf 'dtoverlay=vc4-kms-v3d\n' > "$BOOT_DIR/config.txt"
    printf '/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n' > "$MOUNTS"
    printf 'connected\n' > "$DRM_ROOT/card1-HDMI-A-1/status"
    printf '3840x2160\n3840x2160\n1920x1080\n1280x720\n' > "$DRM_ROOT/card1-HDMI-A-1/modes"

    # Steps are replaced by recorders: the tests check that the right step ran
    # with the right environment, not what the real step does to a Pi.
    local step
    for step in 20-network-host.sh 20-network-client.sh 40-controller.sh 26-txpower.sh 50-splash.sh; do
        # Written to the log as well as stdout: some callers discard stdout,
        # and the test still has to see that the step ran.
        cat > "$SANDBOX/repo/deploy/steps/$step" <<EOF
#!/usr/bin/env bash
line="STEP $step BF_ID=\${BF_ID:-} BF_ROLE=\${BF_ROLE:-} BF_SSID=\${BF_SSID:-} BF_PSK=\${BF_PSK:-} BF_OPEN=\${BF_OPEN:-} BF_SPLASH=\${BF_SPLASH:-}"
echo "\$line"
echo "\$line" >> "$STUB_LOG"
EOF
        chmod +x "$SANDBOX/repo/deploy/steps/$step"
    done
    : > "$SANDBOX/repo/deploy/install.sh"
    chmod +x "$SANDBOX/repo/deploy/install.sh"

    install_stubs
    PATH="$SANDBOX/bin:$PATH"
    export PATH
}

cleanup_sandbox() { [[ -n ${SANDBOX:-} && -d $SANDBOX ]] && rm -rf "$SANDBOX"; }

# Every stub records its call so tests can assert what would have happened.
install_stubs() {
    local cmd
    for cmd in hostnamectl systemctl journalctl iw raspi-config update-initramfs avahi-publish; do
        cat > "$SANDBOX/bin/$cmd" <<EOF
#!/usr/bin/env bash
echo "$cmd \$*" >> "$STUB_LOG"
exit \${STUB_RC_${cmd//-/_}:-0}
EOF
        chmod +x "$SANDBOX/bin/$cmd"
    done

    cat > "$SANDBOX/bin/ip" <<'EOF'
#!/usr/bin/env bash
echo "ip $*" >> "$STUB_LOG"
echo "5: wlan0    inet 192.168.4.12/24 brd 192.168.4.255 scope global wlan0"
EOF
    chmod +x "$SANDBOX/bin/ip"

    # nmcli: profile list, secrets, and up/modify/add/delete, all recorded.
    # NMCLI_UP_OK limits which profiles may be activated, so a failed join can
    # be exercised.
    cat > "$SANDBOX/bin/nmcli" <<'EOF'
#!/usr/bin/env bash
echo "nmcli $*" >> "$STUB_LOG"
case "$*" in
    *"-f NAME,TYPE,AUTOCONNECT connection show"*)
        printf 'blaufilter-ap:802-11-wireless:yes\nblaufilter:802-11-wireless:yes\n'
        printf 'Werkstatt:802-11-wireless:no\nBuero:802-11-wireless:yes\n'
        printf 'Wired connection 1:802-3-ethernet:yes\n'
        exit 0 ;;
    *"--show-secrets"*psk*)
        # No colon: NMCLI_PSK="" has to mean "no key stored", which is the
        # case the missing-password guard exists for.
        echo "${NMCLI_PSK-geheim123}"; exit 0 ;;
esac
if [[ ${1:-} == connection && ${2:-} == up ]]; then
    allow=${NMCLI_UP_OK:-__all__}
    if [[ $allow == __all__ || " $allow " == *" $3 "* ]]; then
        echo "Verbindung erfolgreich aktiviert: $3"; exit 0
    fi
    echo "Error: Aktivierung von '$3' fehlgeschlagen." >&2; exit 4
fi
exit 0
EOF
    chmod +x "$SANDBOX/bin/nmcli"
}

# Dialog answers come from a queue; each dialog pops the next one. "CANCEL"
# makes that dialog behave as if the user pressed Escape, and running past the
# end of the queue cancels too, so a test that forgets an answer fails loudly
# instead of silently taking an empty one.
#
# The cursor lives in a file on purpose: the scripts read a selection with
# value=$(whiptail ...), which runs the stub in a subshell — a shell variable
# would be incremented there and lost.
queue_answers() {
    printf '%s\n' "$@" > "$SANDBOX/answers"
    : > "$SANDBOX/answers.used"
}

next_answer() {
    local total used
    total=$(wc -l < "$SANDBOX/answers" 2>/dev/null || echo 0)
    used=$(wc -l < "$SANDBOX/answers.used" 2>/dev/null || echo 0)
    if (( used >= total )); then echo "CANCEL"; return; fi
    echo x >> "$SANDBOX/answers.used"
    sed -n "$((used + 1))p" "$SANDBOX/answers"
}

stub_whiptail() {
    : > "$SANDBOX/answers"
    : > "$SANDBOX/answers.used"
    # The selection goes to stderr because the scripts swap fds (3>&1 1>&2 2>&3)
    whiptail() {
        local answer; answer=$(next_answer)
        echo "whiptail $*" >> "$STUB_LOG"
        [[ $answer == CANCEL ]] && return 1
        echo "$answer" >&2
        return 0
    }
    # yes_no and msg wrap whiptail; answering them through the same queue keeps
    # the tests readable.
    yes_no() {
        local answer; answer=$(next_answer)
        echo "yes_no $(echo -e "$1" | head -1)" >> "$STUB_LOG"
        [[ $answer == YES ]]
    }
    msg() { echo "msg $(echo -e "$1" | head -1)" >> "$STUB_LOG"; }
}

# run_detached forks and waits for a terminal; in tests it runs inline.
stub_run_detached() {
    run_detached() {
        local headline=$1; shift
        echo "run_detached $headline" >> "$STUB_LOG"
        "$@" >> "$STUB_LOG" 2>&1
    }
}

load_setup() {
    # shellcheck disable=SC1090
    source "$REPO_ROOT/deploy/blaufilter-setup.sh"
    stub_whiptail
    stub_run_detached
}

stub_log() { cat "$STUB_LOG"; }

describe() { CURRENT_TEST=$1; printf '\n  %s\n' "$1"; }

finish() {
    printf '\n  %d ok, %d fehlgeschlagen\n' "$PASSED" "$FAILED"
    [[ $FAILED -eq 0 ]]
}
