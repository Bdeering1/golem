# golem menu bar board

A native macOS menu bar version of `golem board`. It reads golem's ledger and progress logs
directly and runs `golem <action> <id>` for actions, so it needs no changes to golem itself.

```sh
./build-app.sh                                   # builds target/release/Golem Board.app
cp -R "target/release/Golem Board.app" /Applications
```

Click the menu bar icon or press ctrl-option-G to open the board, and press `?` for keys.

Pickup for claude and codex tasks opens a terminal. Terminal.app is the default; set another
in `~/.config/golem/menubar.toml`:

```toml
terminal = "Ghostty"   # any app that takes `-e <command>`, such as Ghostty or Alacritty
```
