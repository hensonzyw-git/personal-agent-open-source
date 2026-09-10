"""The static half of "the acceptance build is isolated".

The isolated acceptance build exists because the 2026-09-10 review would not
release the ordinary App for installation: the cards it changed could only be
checked by driving a real conversation against the real backend, with the real
calendar at the far end of one of them. The acceptance build draws those same
cards over synthetic facts instead -- which is only worth something if it is
genuinely a *different product*. A file wrapped in `#if ACCEPTANCE` looks
isolated whether or not any configuration ever defines the flag, and a build
configuration that leaked into Release would look exactly like one that did not.

What this file checks is source and project structure. What it deliberately does
not check is the claim that actually matters -- that the two *built products*
differ -- because that needs both products linked, and that runs in
`ios/scripts/check_acceptance_isolation.sh`.

The split is not arbitrary. Everything here is a property of text, so it can run
on every commit; the symbol comparison costs two `xcodebuild` runs, so it runs
at the install gate. Both exist because neither alone is sufficient: the script
cannot tell you *why* a product differs, and this file cannot tell you that the
configuration was honoured.

## The one thing here that looks like a mistake and is not

There is no `#if ACCEPTANCE` in the Kit (the SwiftPM package) -- and this file
fails if someone adds one. That is measured, not assumed: the flag is set on the
App target's `Acceptance` build configuration, and a SwiftPM package target does
not inherit it. A `#if ACCEPTANCE` / `#error` pair placed in the Kit compiled
cleanly under the acceptance scheme, which is what a block that is false in
every build looks like. So gating the Kit would not narrow the acceptance build
-- it would delete the harness from the product that needs it, and the deletion
would look exactly like successful isolation.

The consequence is recorded rather than hidden: the Kit's acceptance types (and
the seeded script with them) are present in production binaries. They are inert
there -- `AcceptanceScene`, the only type that composes them into a running app,
is in the App target and absent from the production product by symbol.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
IOS = ROOT / "ios"
PROJECT = IOS / "PersonalAgent.xcodeproj"
PBXPROJ = PROJECT / "project.pbxproj"
SCHEMES = PROJECT / "xcshareddata/xcschemes"
APP = IOS / "PersonalAgent"
KIT_SOURCES = IOS / "PersonalAgentKit/Sources/PersonalAgentKit"

ACCEPTANCE_SCHEME = "PersonalAgent-Acceptance"
PRODUCTION_SCHEME = "PersonalAgent"

#: The Kit files the acceptance build seeds its facts from. They are compiled in
#: every configuration on purpose -- see the module docstring -- so what is
#: asserted about them is not their absence but their harmlessness.
KIT_ACCEPTANCE_FILES = (
    "AcceptanceScenario.swift",
    "AcceptanceChecklist.swift",
    "AcceptanceBackend.swift",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"{path} is gone; this test cannot see the build it pins"
    return path.read_text(encoding="utf-8")


def _plist_keys(path: Path) -> set[str]:
    with path.open("rb") as handle:
        return set(plistlib.load(handle))


def _code_lines(source: str) -> list[str]:
    """Every line that is not wholly a comment.

    The prose in these files names the things they must not do -- that is what
    documentation is for -- so a search over the raw text finds `EventKit` in a
    sentence explaining that there is no EventKit here and reports it as a
    capability. Only whole-line comments are dropped; a trailing one would be
    missed, which is the cheap direction to be wrong in (it can only make this
    test quieter, and the failure it guards looks nothing like a comment)."""
    return [
        line
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]


# ------------------------------------------------- the App target's one branch


class TestTheAcceptanceAppIsBehindTheFlag:
    def test_the_scene_is_wholly_inside_the_conditional(self) -> None:
        """A file that opens `#if ACCEPTANCE` and forgets to close it still
        compiles -- the rest of the file is simply inside the block, which is
        what is wanted, or the compiler reports a missing `#endif`, which is
        not silent. What is *not* silent-free is the reverse: a type declared
        above the `#if` and used inside it, which the production build then
        carries."""
        lines = _read(APP / "AcceptanceScene.swift").splitlines()
        code = [line for line in lines if line.strip() and not line.strip().startswith("//")]
        assert code[0] == "#if ACCEPTANCE", (
            "AcceptanceScene.swift must open with #if ACCEPTANCE, not merely contain it"
        )
        assert code[-1] == "#endif"
        assert code.count("#if ACCEPTANCE") == 1
        assert code.count("#endif") == 1

    def test_the_production_composition_cannot_reach_it(self) -> None:
        """`PersonalAgentApp` is the only other App-target file that names the
        scene, and it names it only inside its acceptance branch. The `#else`
        is what makes that a fact rather than a fence: without it the two
        compositions would both be built and the last one would win."""
        source = _read(APP / "PersonalAgentApp.swift")
        match = re.search(r"#if ACCEPTANCE(.*?)#else(.*?)#endif", source, re.DOTALL)
        assert match, "PersonalAgentApp no longer splits the acceptance branch with an #else"
        acceptance_branch, production_branch = match.group(1), match.group(2)
        assert "AcceptanceScene()" in acceptance_branch
        assert "RootView(" not in acceptance_branch
        assert "AcceptanceScene" not in production_branch
        # The production composition has to still be there. A branch that only
        # removed the acceptance half would isolate the build by emptying it.
        assert "RootView(" in production_branch

    def test_the_acceptance_build_does_not_even_declare_the_app_model(self) -> None:
        """The strongest form of the isolation and the easiest to lose: the
        production properties (`AppModel`, the `UIApplicationDelegateAdaptor`)
        are declared under `#if !ACCEPTANCE`, so on the acceptance path there
        is no device session, no enrollment and no EventKit mirror engine to
        construct -- not merely one that is not constructed."""
        source = _read(APP / "PersonalAgentApp.swift")
        match = re.search(r"#if !ACCEPTANCE(.*?)#endif", source, re.DOTALL)
        assert match, "the AppModel properties are no longer gated by #if !ACCEPTANCE"
        gated = match.group(1)
        assert "AppModel()" in gated
        assert "UIApplicationDelegateAdaptor" in gated
        # And nothing outside that block declares them, which would make the
        # gate decorative.
        outside = source.replace(gated, "")
        assert "private var model = AppModel()" not in outside
        assert "@UIApplicationDelegateAdaptor" not in outside

    def test_no_other_app_file_names_the_acceptance_types(self) -> None:
        """The inventory check: one new App-target file referencing the scene
        from outside the branch would be an unguarded path into the harness,
        and it would not look like one."""
        naming = {
            path.name
            for path in sorted(APP.glob("*.swift"))
            if "Acceptance" in _read(path)
        }
        assert naming == {"AcceptanceScene.swift", "PersonalAgentApp.swift"}


# ----------------------------------------------------------- the flag's reach


class TestTheFlagIsSetInExactlyOnePlace:
    def test_the_pbxproj_names_it_once(self) -> None:
        lines = [
            line
            for line in _read(PBXPROJ).splitlines()
            if "ACCEPTANCE" in line
        ]
        assert lines == [
            '\t\t\t\tSWIFT_ACTIVE_COMPILATION_CONDITIONS = "DEBUG ACCEPTANCE $(inherited)";'
        ], (
            "the flag must be defined by exactly one build setting and nothing "
            f"else; found {lines}"
        )

    def test_the_release_configuration_does_not_carry_it(self) -> None:
        """The one mention above is the acceptance configuration's. This is the
        other direction of the same claim: Release defines no compilation
        conditions at all, so it cannot have inherited the flag from a shared
        xcconfig either."""
        source = _read(PBXPROJ)
        assert 'SWIFT_ACTIVE_COMPILATION_CONDITIONS = "DEBUG $(inherited)";' in source
        release = source[source.index("A0000000000000000000000A /* Release */") :][:3000]
        assert "ACCEPTANCE" not in release

    def test_the_acceptance_configuration_is_on_both_lists(self) -> None:
        """A project-level configuration with no target-level counterpart (or
        the reverse) compiles the App target with default settings, which is a
        build that is neither the acceptance product nor the production one."""
        source = _read(PBXPROJ)
        for object_id in (
            "A00000000000000000000013",  # project-level Acceptance
            "A00000000000000000000014",  # target-level Acceptance
        ):
            assert f"{object_id} /* Acceptance */" in source
            assert f"{object_id} /* Acceptance */,\n\t\t\t)" in source, (
                f"{object_id} is not in a build configuration list"
            )


class TestTheSchemeIsTheOnlyDoorToIt:
    def test_every_action_uses_the_acceptance_configuration(self) -> None:
        schema = _read(SCHEMES / f"{ACCEPTANCE_SCHEME}.xcscheme")
        configurations = re.findall(r'buildConfiguration = "([^"]+)"', schema)
        assert configurations, "the acceptance scheme declares no build configuration"
        assert set(configurations) == {"Acceptance"}, (
            "every action in the acceptance scheme must build the acceptance "
            f"product, not a Release one; found {sorted(set(configurations))}"
        )

    def test_the_production_scheme_has_no_way_in(self) -> None:
        schema = _read(SCHEMES / f"{PRODUCTION_SCHEME}.xcscheme")
        assert "ACCEPTANCE" not in schema
        assert 'buildConfiguration = "Acceptance"' not in schema


class TestTheInfoPlistsDifferTheOneWayThatMatters:
    CALENDAR_KEYS = ("NSCalendarsFullAccessUsageDescription", "NSCalendarsUsageDescription")

    def test_the_acceptance_build_cannot_ask_for_a_calendar(self) -> None:
        keys = _plist_keys(IOS / "PersonalAgent-Acceptance-Info.plist")
        assert not keys & set(self.CALENDAR_KEYS), (
            "with a calendar usage description present, a mistaken EventKit call "
            "in the acceptance build would prompt to read the real calendar "
            "instead of aborting"
        )

    def test_the_production_build_still_can(self) -> None:
        keys = _plist_keys(IOS / "PersonalAgent-Info.plist")
        assert keys >= set(self.CALENDAR_KEYS), (
            "the production build's calendar access is the feature under "
            "acceptance; its declarations must not have been moved"
        )

    def test_the_acceptance_configuration_points_at_the_acceptance_plist(self) -> None:
        """Checked from the configuration that is actually built. Pinning the
        file's contents proves nothing about which file the scheme hands the
        App target, and it is the wiring that decides."""
        source = _read(PBXPROJ)
        for configuration, plist, bundle in (
            ("A00000000000000000000014 /* Acceptance */", "PersonalAgent-Acceptance-Info.plist", "org.example.PersonalAgent.Acceptance"),
            ("A0000000000000000000000B /* Debug */", "PersonalAgent-Info.plist", "org.example.PersonalAgent"),
        ):
            block = source[source.index(configuration) :][:3000]
            assert f'INFOPLIST_FILE = "{plist}";' in block, f"{configuration} does not use {plist}"
            assert f"PRODUCT_BUNDLE_IDENTIFIER = {bundle};" in block, (
                f"{configuration} has bundle id other than {bundle}; the acceptance "
                "build must be a separately installable app, and it must not "
                "silently take over the production bundle id"
            )


# ------------------------------------------------ the Kit is not gated, on purpose


class TestTheKitCarriesNoFlag:
    def test_no_acceptance_conditional_anywhere_in_the_kit(self) -> None:
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(KIT_SOURCES.rglob("*.swift"))
            if any("#if ACCEPTANCE" in line for line in _code_lines(_read(path)))
        ]
        assert offenders == [], (
            "a SwiftPM package target does not inherit the App target's "
            "SWIFT_ACTIVE_COMPILATION_CONDITIONS (measured: an #if ACCEPTANCE / "
            "#error probe in this directory compiled cleanly under the acceptance "
            "scheme). Such a block is false in every build, so it does not narrow "
            f"the acceptance product -- it deletes the harness from it. Offenders: {offenders}"
        )

    def test_the_kit_acceptance_sources_touch_nothing_real(self) -> None:
        """The property that makes their presence in production binaries inert.

        Not "they are unreachable" -- they are reachable, they are simply
        holding no capability that leaves the app's own sandbox: no calendar,
        no network, no keychain. `AcceptanceBackend` does open a file, and it is
        named in the assertion below rather than waved through: it writes the
        seeded archive under Application Support so that killing and relaunching
        the acceptance app recovers the seeded history, which is one of the
        things the checklist walks through. That is a write to a directory the
        app owns, in a product that is not the production one.
        """
        for name in KIT_ACCEPTANCE_FILES:
            code = "\n".join(_code_lines(_read(KIT_SOURCES / name)))
            imports = set(re.findall(r"^import (\w+)", code, re.MULTILINE))
            assert imports == {"Foundation"}, (
                f"{name} imports {sorted(imports)}; the acceptance harness must "
                "not acquire a capability beyond Foundation"
            )
            for forbidden in ("EventKit", "URLSession", "SecItem", "EKEvent", "URLRequest"):
                assert forbidden not in code, f"{name} reaches for {forbidden}"
            if "FileManager" in code:
                assert name == "AcceptanceBackend.swift", (
                    f"{name} is not the file that owns the seeded archive; a second "
                    "one opening files is not a capability this build should hold"
                )
                assert "applicationSupportDirectory" in code, (
                    "AcceptanceBackend reaches the filesystem for something other "
                    "than the app's own container, which is no longer inert"
                )
                for absolute in ('URL(fileURLWithPath: "/', '"~/', "NSHomeDirectory"):
                    assert absolute not in code, (
                        f"the seeded archive must live in the app's container, not at {absolute}"
                    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
