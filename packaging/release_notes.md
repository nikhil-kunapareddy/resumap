## Install

1. Download the disk image for your Mac:
   - Apple Silicon (M1 and later): `Resumap-<version>-macos-arm64.dmg`
   - Intel: `Resumap-<version>-macos-x86_64.dmg`

   Not sure which you have? Apple menu > About This Mac: "Chip: Apple M…" is
   Apple Silicon, "Processor: Intel…" is Intel.
2. Open the `.dmg` and drag **Resumap** onto **Applications**.
3. Open Resumap from Applications. The first time, macOS says it cannot check
   the app, because it is not signed with an Apple Developer ID. Open
   **System Settings > Privacy & Security**, scroll down, click **Open Anyway**
   and confirm. You only do this once.
4. Resumap opens in your browser. Open **Settings** (top right) and choose who
   answers: Claude Code, Codex or pi if one is installed and signed in, or
   paste an Anthropic API key.

The `.zip` files hold the same app, for anyone who prefers them.
`SHA256SUMS.txt` lists every file's checksum (`shasum -a 256 -c SHA256SUMS.txt`).
