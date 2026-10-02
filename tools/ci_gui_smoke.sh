#!/usr/bin/env bash
# CI helper: drive the bundled GUI under Xvfb, once as a libadwaita app and
# once on the plain GTK path (the one the Windows build uses), and check that
# the folder handed on the command line really gets cropped. A screenshot of
# each run is left in gui-<name>.png.
#
#   bash tools/ci_gui_smoke.sh [path/to/pdf-cropper]
set -euo pipefail

APP="${1:-./dist/pdf-cropper/pdf-cropper}"
PY="${PYTHON:-.venv/bin/python}"
export GSK_RENDERER=cairo GDK_BACKEND=x11

run_case() {  # name [VAR=value...]
    local name="$1"; shift
    local dir="gui-$name"
    rm -rf "$dir"; mkdir -p "$dir"
    for sample in "Ground floor" "First floor" "Single-line diagram"; do
        "$PY" tests/test_crop.py --make-sample "$dir/$sample.pdf" >/dev/null
    done
    printf 'not a PDF\n' > "$dir/Report.pdf"   # shows up as an error row
    env "$@" xvfb-run -a -s "-screen 0 680x560x24" dbus-run-session -- bash -c '
        "$1" --gui "$2" &
        pid=$!
        for i in $(seq 1 60); do
            [ -f "$2/Single-line diagram_cropped.pdf" ] && break
            sleep 0.5
        done
        sleep 3
        import -window root "$3" || true
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    ' _ "$APP" "$dir" "gui-$name.png"
    for sample in "Ground floor" "First floor" "Single-line diagram"; do
        "$PY" tests/test_crop.py --check-sample "$dir/${sample}_cropped.pdf"
    done
    test ! -e "$dir/Report_cropped.pdf"
    echo "GUI smoke test passed: $name"
}

run_case adwaita
run_case gtk PDF_CROPPER_NO_ADW=1
