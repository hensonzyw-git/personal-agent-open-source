#!/usr/bin/env bash
#
# Assert that the isolated acceptance build is actually isolated.
#
# ## Why this exists
#
# The acceptance build's whole value is that it shows the *shipping* cards over
# synthetic facts. It is only worth something if the ordinary product cannot
# reach it and it cannot reach any real fact source — and both of those are
# properties of the build graph, not of the source. A file wrapped in
# `#if ACCEPTANCE` looks isolated whether or not any configuration ever defines
# the flag, and a build configuration that leaked into Release would look
# exactly like one that did not.
#
# So this script checks the artefacts, in the order that matters:
#
#   1. the flag is set in exactly one build configuration, and that
#      configuration is reachable only through one scheme;
#   2. the two products really differ — the acceptance product defines the
#      App-target acceptance types and the production product does not;
#   3. the acceptance product carries no calendar usage description, so an
#      accidental EventKit call aborts instead of asking to read the calendar.
#
# Check 2 is the one that cannot be satisfied by reading the source: it runs on
# the linked binaries. It needs both products built, which is why it shells out
# to `xcodebuild` — a few minutes, and the reason this script is not part of the
# fast suite. The static half of the same argument runs on every commit in
# `tests/unit/test_acceptance_build_isolation.py`.
#
# Usage:  ios/scripts/check_acceptance_isolation.sh
# Exits non-zero on the first failed check, printing which one.

set -euo pipefail

cd "$(dirname "$0")/.."

PROJECT="PersonalAgent.xcodeproj"
ACCEPTANCE_SCHEME="PersonalAgent-Acceptance"
PRODUCTION_SCHEME="PersonalAgent"
APP_TYPES="AcceptanceScene|AcceptanceChecklistView"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=1; }

echo "1. the flag is set by exactly one configuration"
# The pbxproj is read as text. It is a plist, but what matters here is where the
# token sits, and one line mentioning it is the whole of the claim: the flag is
# defined for one configuration and for no other. Anything else -- a second
# line, a line in Debug, a bare `-DACCEPTANCE` somewhere -- fails.
flag_lines=$(grep -c 'ACCEPTANCE' "$PROJECT/project.pbxproj" || true)
if [ "$flag_lines" -eq 1 ]; then
    pass "ACCEPTANCE is mentioned on exactly one line of the project"
else
    bad "ACCEPTANCE is mentioned on $flag_lines lines; expected exactly 1"
fi
if grep -q 'SWIFT_ACTIVE_COMPILATION_CONDITIONS = "DEBUG ACCEPTANCE \$(inherited)";' \
    "$PROJECT/project.pbxproj"; then
    pass "that line is the Acceptance configuration's compilation conditions"
else
    bad "the one mention of ACCEPTANCE is not the expected setting"
fi
if grep -q 'SWIFT_ACTIVE_COMPILATION_CONDITIONS = "DEBUG \$(inherited)";' \
    "$PROJECT/project.pbxproj"; then
    pass "the Debug configuration still defines DEBUG alone"
else
    bad "the Debug configuration's compilation conditions changed"
fi

echo "2. the acceptance scheme is the only way to that configuration"
if grep -q 'buildConfiguration = "Acceptance"' \
    "$PROJECT/xcshareddata/xcschemes/$ACCEPTANCE_SCHEME.xcscheme"; then
    pass "$ACCEPTANCE_SCHEME uses the Acceptance configuration"
else
    bad "$ACCEPTANCE_SCHEME does not use the Acceptance configuration"
fi
if grep -q 'ACCEPTANCE' "$PROJECT/xcshareddata/xcschemes/$PRODUCTION_SCHEME.xcscheme"; then
    bad "$PRODUCTION_SCHEME mentions ACCEPTANCE"
else
    pass "$PRODUCTION_SCHEME does not mention ACCEPTANCE"
fi
if grep -q 'buildConfiguration = "Acceptance"' \
    "$PROJECT/xcshareddata/xcschemes/$PRODUCTION_SCHEME.xcscheme"; then
    bad "$PRODUCTION_SCHEME has an action on the Acceptance configuration"
else
    pass "$PRODUCTION_SCHEME has no action on the Acceptance configuration"
fi

echo "3. the two products differ in the linked binary"
build() {
    xcodebuild -project "$PROJECT" -scheme "$1" \
        -destination 'generic/platform=iOS' \
        -derivedDataPath "$TMPDIR/acceptance-isolation-dd" \
        build CODE_SIGNING_ALLOWED=NO >/dev/null
}
products="$TMPDIR/acceptance-isolation-dd/Build/Products"
rm -rf "$products"

build "$ACCEPTANCE_SCHEME"
build "$PRODUCTION_SCHEME"

acceptance_app=$(find "$products/Acceptance-iphoneos" -name 'PersonalAgent.app' -maxdepth 2 | head -1)
production_app=$(find "$products/Debug-iphoneos" -name 'PersonalAgent.app' -maxdepth 2 | head -1)
# Xcode 26 builds the app's code into a debug dylib beside a thin launcher.
# Symbol-searching the launcher would find nothing in either product, and a
# check that passes for the wrong reason is worse than no check.
binary() {
    local app="$1"
    if [ -f "$app/PersonalAgent.debug.dylib" ]; then
        echo "$app/PersonalAgent.debug.dylib"
    else
        echo "$app/PersonalAgent"
    fi
}

# Debug-map entries (`nm` type `-`) name the *source file* of every compiled
# translation unit, including one whose body `#if`'d away, so they are excluded:
# what must be absent from production is the code, not the file name.
defined_symbols() {
    nm -a "$1" 2>/dev/null | awk '$2 != "-"' | grep -cE "$APP_TYPES" || true
}

if [ ! -f "$(binary "$production_app")" ] || [ ! -f "$(binary "$acceptance_app")" ]; then
    bad "could not find both built products under $products"
else
    acceptance_symbols=$(defined_symbols "$(binary "$acceptance_app")")
    production_symbols=$(defined_symbols "$(binary "$production_app")")
    if [ "$acceptance_symbols" -gt 0 ]; then
        pass "the acceptance product defines the acceptance types ($acceptance_symbols symbols)"
    else
        bad "the acceptance product does not define the acceptance types; the flag is not doing anything"
    fi
    if [ "$production_symbols" -eq 0 ]; then
        pass "the production product defines none of them"
    else
        bad "the production product defines $production_symbols acceptance symbols"
    fi
fi

echo "4. the acceptance build cannot read a calendar"
# Asked of the plist as a plist, not as text: the acceptance file's own comment
# names the key it is explaining the absence of, and a text search would read
# that comment as the key.
has_key() {
    /usr/libexec/PlistBuddy -c "Print :$2" "$1" >/dev/null 2>&1
}
for key in NSCalendarsFullAccessUsageDescription NSCalendarsUsageDescription; do
    if has_key PersonalAgent-Acceptance-Info.plist "$key"; then
        bad "PersonalAgent-Acceptance-Info.plist carries $key"
    else
        pass "no $key in the acceptance Info.plist"
    fi
    if has_key PersonalAgent-Info.plist "$key"; then
        pass "the production Info.plist still has $key"
    else
        bad "the production Info.plist lost $key"
    fi
done

echo
if [ "$fail" -eq 0 ]; then
    echo "acceptance isolation: PASS"
else
    echo "acceptance isolation: FAIL"
fi
exit "$fail"
