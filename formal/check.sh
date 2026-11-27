#!/bin/sh
# Model-checks formal/*.tla: current.cfg must pass, and each *-before-fix.cfg must fail on the
# invariant its past bug broke, which proves the model can still catch that bug.
set -eu
cd "$(dirname "$0")"

TLA2TOOLS_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
TLA2TOOLS_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
jar="${XDG_CACHE_HOME:-$HOME/.cache}/golem/tla2tools-1.7.4.jar"
if [ ! -f "$jar" ]; then
    mkdir -p "$(dirname "$jar")"
    curl -sSfL -o "$jar.part" "$TLA2TOOLS_URL"
    mv "$jar.part" "$jar"
fi
echo "$TLA2TOOLS_SHA256  $jar" | shasum -a 256 -c - >/dev/null

metadir=$(mktemp -d)
trap 'rm -r "$metadir"' EXIT

tlc() {
    java -XX:+UseParallelGC -cp "$jar" tlc2.TLC -deadlock -workers auto \
        -metadir "$metadir/$1" -config "$1" T3ThreadWait.tla
}

status=0
if output=$(tlc current.cfg 2>&1); then
    echo "pass: current.cfg"
else
    echo "FAIL: current.cfg breaks a property; the counterexample is a bug report:"
    echo "$output"
    status=1
fi
for config in wait-before-fix.cfg:ResultComesFromARunThatRan \
    settle-before-fix.cfg:SkipsSettleOnlyForALaterRun; do
    file=${config%%:*}
    invariant=${config#*:}
    if tlc "$file" 2>&1 | grep -q "Invariant $invariant is violated"; then
        echo "pass: $file still fails on $invariant"
    else
        echo "FAIL: $file no longer fails on $invariant, so the model can't catch that bug"
        status=1
    fi
done
exit "$status"
