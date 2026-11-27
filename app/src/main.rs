mod app;
mod board;
mod golem_cli;
mod ledger;
mod macos;
mod theme;
mod zoom;

use global_hotkey::hotkey::{Code, HotKey, Modifiers};
use global_hotkey::{GlobalHotKeyEvent, GlobalHotKeyManager, HotKeyState};
use gpui::{
    ActivationPolicy, App, AppContext, Bounds, WindowBackgroundAppearance, WindowBounds,
    WindowKind, WindowOptions, point, px, size,
};
use tray_icon::{MouseButton, MouseButtonState, TrayIconEvent};

fn main() {
    gpui_platform::application()
        .with_assets(theme::Assets)
        .run(|cx: &mut App| {
            cx.set_activation_policy(ActivationPolicy::Accessory);
            let (toggle_sender, toggle_receiver) = futures::channel::mpsc::unbounded::<()>();
            let tray_sender = toggle_sender.clone();
            TrayIconEvent::set_event_handler(Some(move |event: TrayIconEvent| {
                if let TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Down,
                    ..
                } = event
                {
                    let _ = tray_sender.unbounded_send(());
                }
            }));
            GlobalHotKeyEvent::set_event_handler(Some(move |event: GlobalHotKeyEvent| {
                if event.state == HotKeyState::Pressed {
                    let _ = toggle_sender.unbounded_send(());
                }
            }));
            let hotkey_manager = GlobalHotKeyManager::new().ok().filter(|manager| {
                manager
                    .register(HotKey::new(
                        Some(Modifiers::CONTROL | Modifiers::ALT),
                        Code::KeyG,
                    ))
                    .is_ok()
            });
            let options = WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(Bounds::new(
                    point(px(0.), px(0.)),
                    size(px(440.), px(640.)),
                ))),
                titlebar: None,
                focus: false,
                show: false,
                kind: WindowKind::PopUp,
                is_movable: false,
                is_resizable: false,
                is_minimizable: false,
                window_background: WindowBackgroundAppearance::Transparent,
                ..Default::default()
            };
            cx.open_window(options, |window, cx| {
                cx.new(|cx| app::BoardView::new(window, cx, toggle_receiver, hotkey_manager))
            })
            .expect("board window can be created");
        });
}
