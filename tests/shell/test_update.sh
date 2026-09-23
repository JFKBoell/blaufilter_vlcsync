#!/usr/bin/env bash
# Updating an installed device from the menu.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox
load_setup

# A real little repository, so git_state and git pull have something to work on
git -C "$SANDBOX/repo" init -q -b main 2>/dev/null
git -C "$SANDBOX/repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "erster" 2>/dev/null

# The installer is a recorder here: the test checks which arguments it gets.
cat > "$SANDBOX/repo/deploy/install.sh" <<EOF
#!/usr/bin/env bash
echo "INSTALL \$*" >> "$STUB_LOG"
EOF
chmod +x "$SANDBOX/repo/deploy/install.sh"
# git is stubbed for pull only, so no network is needed
cat > "$SANDBOX/bin/git" <<EOF
#!/usr/bin/env bash
if [[ " \$* " == *" pull "* ]]; then
    echo "git \$*" >> "$STUB_LOG"
    exit \${STUB_RC_git_pull:-0}
fi
exec /usr/bin/git "\$@"
EOF
chmod +x "$SANDBOX/bin/git"
# bash caches command paths, and git was already used above — without this the
# real git would keep running instead of the stub.
hash -r

describe "git_state"
assert_contains "$(git_state "$SANDBOX/repo")" "main @" "nennt Zweig und Commit"
assert_contains "$(git_state "$SANDBOX/leer")" "kein Git-Repository" "meldet fehlendes Repository"

describe "Holen und einspielen"
: > "$STUB_LOG"
queue_answers "beides" YES
menu_update
log=$(stub_log)
assert_contains "$log" "git -C $SANDBOX/repo pull --ff-only" "holt den neuen Stand"
assert_contains "$log" "INSTALL " "spielt ihn ein"
assert_contains "$log" "--id 2" "mit der gespeicherten Geräte-ID"
assert_contains "$log" "--role client" "und der gespeicherten Rolle"
assert_contains "$log" "--psk geheim123" "und dem gespeicherten WLAN-Passwort"
assert_contains "$log" "--pin 1234" "und der gespeicherten PIN"

describe "Nur holen"
: > "$STUB_LOG"
queue_answers "holen"
menu_update
log=$(stub_log)
assert_contains "$log" "pull --ff-only" "holt"
assert_not_contains "$log" "INSTALL " "spielt nicht ein"

describe "Nur einspielen"
: > "$STUB_LOG"
queue_answers "spielen" YES
menu_update
log=$(stub_log)
assert_not_contains "$log" "pull --ff-only" "holt nicht"
assert_contains "$log" "INSTALL " "spielt ein"

describe "Offenes WLAN wird als --open weitergereicht"
: > "$STUB_LOG"
cfg_set open_wifi 1
queue_answers "spielen" YES
menu_update
log=$(stub_log)
assert_contains "$log" "--open" "nutzt --open"
assert_not_contains "$log" "--psk" "ohne Passwort"
cfg_set open_wifi 0

describe "Fehlgeschlagenes Holen spielt nichts ein"
: > "$STUB_LOG"
queue_answers "beides"
STUB_RC_git_pull=1 menu_update
log=$(stub_log)
assert_not_contains "$log" "INSTALL " "bricht vor dem Einspielen ab"
assert_contains "$log" "msg Das Holen ist fehlgeschlagen" "erklärt warum"

describe "Warnung bei aktivem Schreibschutz"
: > "$STUB_LOG"
printf 'overlay / overlay rw 0 0\n' > "$MOUNTS"
queue_answers CANCEL
menu_update
assert_contains "$(stub_log)" "Schreibschutz ist aktiv" "weist auf den Schreibschutz hin"
printf '/dev/mmcblk0p2 / ext4 rw 0 0\n' > "$MOUNTS"

describe "Abbruch spielt nichts ein"
: > "$STUB_LOG"
queue_answers "spielen" NO
menu_update
assert_not_contains "$(stub_log)" "INSTALL " "nichts passiert"

finish
