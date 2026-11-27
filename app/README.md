# golem board app

A native macOS menu bar version of `golem board`. It reads golem's `tasks.json` and progress
logs and runs `golem <action> <id>` for actions, so it needs no changes to golem itself.

Building needs Rust 1.91 or newer and Xcode. Xcode 26 ships without the Metal compiler the
build needs, so download it once:

```sh
xcodebuild -downloadComponent MetalToolchain
```

Then build and install the app:

```sh
./build-app.sh
cp -R "target/release/Golem.app" /Applications
```

Click the menu bar icon or press ctrl-option-G to open the board, and press `?` to list the
shortcuts. Cmd-plus and cmd-minus zoom the board, and cmd-0 resets it. The board remembers
the zoom level.

Pickup for claude and codex tasks opens a terminal. Terminal.app is the default; set another
in `~/.config/golem/board.toml`:

```toml
terminal = "Ghostty"   # any app that takes -e <command>, such as Alacritty
```
