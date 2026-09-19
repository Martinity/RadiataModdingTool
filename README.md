# Overview

A desktop application for modding _Radiata Stories_. This application automatically manages the disk's file system in-memory, allowing you to modify, extract, and analyze game data without cluttering your local machine with thousands of extracted files.

## Use Cases

- __Reverse engineering & Analysis__: Includes a built-in hex editor and hex diffing tool directly on the staging page for quick, precise binary analysis.
- __Datamining__: Browse the file tree, search for specific files, and extract unmodified game files directly to your PC.
- __Asset Modding__: Utilize built-in editors to modify assets on the fly, or use format handlers to translate between proprietary game formats and standard formats.

## List of current Plugins

| Plugin            | Format Support          | Features                     |
|-------------------|-------------------------|------------------------------|
| **Texture Editor**| `.fis`                  | Edit & export as PNG         |
| **Audio Player**  | `.020`                  | Playback + WAV export        |
| **Event Editor**  | `.evd`                  | Parses script bytes into a readable and editable table |
| **Message Editor**  | `.rmf`                  | *Experimental*: change ingame messages        |
| **Hex Editor**    | Any file                | Raw binary editing           |

## List of current Patches

| Patch | Description |
|-------|-------------|
| Slimmed | Cut the non-essential sections of the disk out saving ~1GB |

## Technical Specifications
- __Languages__: Python, C (heavy data processing)
- __GUI__: PyQt6
- __Build__: Automated Windows and Linux CI releases with smoke testing via PyInstaller. MacOS support is questionable.

# Installation

## Windows

This build is unsigned. Windows SmartScreen will show a warning alert.
Click More info → Run anyway to launch the tool.

## Linux

Install with preferred AppImage manager _or_ make the AppImage executable before launching:

``` bash
chmod +x RadiataModdingTool-*.AppImage
./RadiataModdingTool-*.AppImage
```

## macOS

This build is unsigned. macOS Gatekeeper will block execution by default.
If blocked, Control-click (Right-click) on the app icon and select Open from the context menu _or_ clear the quarantine flag with:

``` zsh
xattr -cr /path/to/RadiataModdingTool.app
```

## Build Integrity Verification (Optional)

Verify your downloads match the compile-time binary fingerprints:

- Windows (PowerShell): `Get-FileHash .\RadiataModdingTool-*.zip` compare to the SHA256SUM-windows-x64.txt stored sting
- Linux: `sha256sum -c SHA256SUM-linux-x86_64.txt`
- macOS: `shasum -a 256 -c SHA256SUM-macos-arm64.txt`

## Acknowledgements

Special thanks to project contributors and CUE.

Unofficial and not associated with Square Enix or tri-Ace.
