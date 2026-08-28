"""Explicit inventory for the pinned Sparkle framework's macOS payload."""

from __future__ import annotations

PREFIX = "Contents/Frameworks/Sparkle.framework"
VERSION = f"{PREFIX}/Versions/B"

HEADERS = """
SPUAppcastSigningValidationStatus SPUDownloadData SPUStandardUpdaterController
SPUStandardUserDriver SPUStandardUserDriverDelegate SPUUpdateCheck SPUUpdatePermissionRequest
SPUUpdater SPUUpdaterDelegate SPUUpdaterSettings SPUUserDriver SPUUserUpdateState
SUAppcast SUAppcastItem SUErrors SUExport SUStandardVersionComparator SUUpdatePermissionResponse
SUUpdater SUUpdaterDelegate SUVersionComparisonProtocol SUVersionDisplayProtocol Sparkle
""".split()
PRIVATE_HEADERS = """
SPUAppcastItemStateResolver SPUGentleUserDriverReminders SPUInstallationType
SPUStandardUserDriver+Private SPUUserAgent+Private SUAppcastItem+Private SUInstallerLauncher+Private
""".split()
LOCALES = """
Base ar ca cs da de el es fa fi fr he hr hu is it ja ko nb nl nn pl pt-BR pt-PT
ro ru sk sl sv th tr uk vi zh_CN zh_HK zh_TW
""".split()

FILES = {
    f"{VERSION}/{path}"
    for path in (
        "Sparkle",
        "Autoupdate",
        "Modules/module.modulemap",
        "Modules/module.private.modulemap",
        "Resources/Info.plist",
        "Resources/ReleaseNotesColorStyle.css",
        "Resources/SUStatus.nib",
        "Resources/SUUpdateAlert.nib",
        "Resources/SUUpdatePermissionPrompt.nib/keyedobjects-101300.nib",
        "Resources/SUUpdatePermissionPrompt.nib/keyedobjects-110000.nib",
        "_CodeSignature/CodeResources",
        "Updater.app/Contents/Info.plist",
        "Updater.app/Contents/PkgInfo",
        "Updater.app/Contents/MacOS/Updater",
        "Updater.app/Contents/Resources/SUStatus.nib",
        "Updater.app/Contents/_CodeSignature/CodeResources",
    )
}
FILES |= {f"{VERSION}/Headers/{name}.h" for name in HEADERS}
FILES |= {f"{VERSION}/PrivateHeaders/{name}.h" for name in PRIVATE_HEADERS}
FILES |= {f"{VERSION}/Resources/{name}.lproj/Sparkle.strings" for name in LOCALES}

LINKS = {
    f"{PREFIX}/{name}": f"Versions/Current/{name}"
    for name in (
        "Sparkle",
        "Autoupdate",
        "Updater.app",
        "Headers",
        "PrivateHeaders",
        "Modules",
        "Resources",
    )
}
LINKS[f"{PREFIX}/Versions/Current"] = "B"
REQUIRED = {
    f"{VERSION}/Sparkle",
    f"{VERSION}/Autoupdate",
    f"{VERSION}/Resources/Info.plist",
    f"{VERSION}/Updater.app/Contents/Info.plist",
    f"{VERSION}/Updater.app/Contents/MacOS/Updater",
}
