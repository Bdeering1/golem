//! The menu bar item and the AppKit calls GPUI lacks: placing, showing and hiding the panel.

use objc2::MainThreadMarker;
use objc2::rc::Retained;
use objc2_app_kit::{NSApplication, NSScreen, NSView, NSWindow};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

const ICON_SIZE: u32 = 36;
const ICON_SUPERSAMPLING: u32 = 4;
const PANEL_GAP: f64 = 4.0;
const SCREEN_MARGIN: f64 = 10.0;
/// How far past the item's center the panel's right edge may sit, as in the prototype.
const PANEL_RIGHT_OVERHANG: f64 = 220.0;

/// Signed distance from a point to the golem logo: a rounded head, two eyes and an antenna.
fn logo_distance(x: f64, y: f64) -> f64 {
    let (center_x, center_y, half_width, half_height, radius) = (18.0, 20.5, 14.0, 11.5, 5.5);
    let qx = (x - center_x).abs() - (half_width - radius);
    let qy = (y - center_y).abs() - (half_height - radius);
    let outside = qx.max(0.0).hypot(qy.max(0.0)) + qx.max(qy).min(0.0) - radius;
    let head = outside.abs() - 1.6;
    let left_eye = (x - 12.5).hypot(y - 20.5) - 2.6;
    let right_eye = (x - 23.5).hypot(y - 20.5) - 2.6;
    let antenna_y = y.clamp(2.5, 9.0);
    let antenna = (x - 18.0).hypot(y - antenna_y) - 1.6;
    head.min(left_eye).min(right_eye).min(antenna)
}

fn logo_icon() -> Icon {
    let mut rgba = Vec::with_capacity((ICON_SIZE * ICON_SIZE * 4) as usize);
    let step = 1.0 / f64::from(ICON_SUPERSAMPLING);
    for row in 0..ICON_SIZE {
        for column in 0..ICON_SIZE {
            let mut covered = 0u32;
            for sample in 0..ICON_SUPERSAMPLING * ICON_SUPERSAMPLING {
                let x = f64::from(column) + (f64::from(sample % ICON_SUPERSAMPLING) + 0.5) * step;
                let y = f64::from(row) + (f64::from(sample / ICON_SUPERSAMPLING) + 0.5) * step;
                covered += u32::from(logo_distance(x, y) <= 0.0);
            }
            let alpha = covered * 255 / (ICON_SUPERSAMPLING * ICON_SUPERSAMPLING);
            rgba.extend_from_slice(&[0, 0, 0, alpha as u8]);
        }
    }
    Icon::from_rgba(rgba, ICON_SIZE, ICON_SIZE).expect("logo icon has valid dimensions")
}

pub fn create_tray_icon() -> TrayIcon {
    TrayIconBuilder::new()
        .with_icon_templated(logo_icon())
        .with_tooltip("golem")
        .build()
        .expect("menu bar item can be created")
}

/// The menu bar item's frame in AppKit screen coordinates (origin at the bottom left).
fn status_item_frame(tray_icon: &TrayIcon) -> Option<NSRect> {
    let mtm = MainThreadMarker::new()?;
    let button = tray_icon.ns_status_item()?.button(mtm)?;
    Some(button.window()?.frame())
}

pub fn native_window(window: &gpui::Window) -> Option<Retained<NSWindow>> {
    let handle = HasWindowHandle::window_handle(window).ok()?;
    let RawWindowHandle::AppKit(appkit_handle) = handle.as_raw() else {
        return None;
    };
    // SAFETY: GPUI's AppKit handle points at the window's live content view.
    let view: &NSView = unsafe { appkit_handle.ns_view.cast().as_ref() };
    view.window()
}

/// The panel frame that puts its top just below the menu bar item, kept on screen.
pub fn panel_frame(
    panel: &NSWindow,
    tray_icon: &TrayIcon,
    width: f64,
    height: f64,
) -> Option<NSRect> {
    let item_frame = status_item_frame(tray_icon)?;
    let item_center = item_frame.origin.x + item_frame.size.width / 2.0;
    let top = item_frame.origin.y - PANEL_GAP;
    let mut right = item_center + PANEL_RIGHT_OVERHANG.min(width / 2.0);
    let mut bottom_limit = f64::MIN;
    if let Some(screen) = panel.screen().or_else(|| item_screen(tray_icon)) {
        let visible = screen.visibleFrame();
        right = right.min(visible.origin.x + visible.size.width - SCREEN_MARGIN);
        bottom_limit = visible.origin.y + SCREEN_MARGIN;
    }
    let height = height.min(top - bottom_limit);
    Some(NSRect::new(
        NSPoint::new(right - width, top - height),
        NSSize::new(width, height),
    ))
}

fn item_screen(tray_icon: &TrayIcon) -> Option<Retained<NSScreen>> {
    let mtm = MainThreadMarker::new()?;
    tray_icon.ns_status_item()?.button(mtm)?.window()?.screen()
}

pub fn show_panel(panel: &NSWindow) {
    if let Some(mtm) = MainThreadMarker::new() {
        #[allow(deprecated)]
        NSApplication::sharedApplication(mtm).activateIgnoringOtherApps(true);
    }
    panel.makeKeyAndOrderFront(None);
}

pub fn hide_panel(panel: &NSWindow) {
    panel.orderOut(None);
}

pub fn is_panel_visible(panel: &NSWindow) -> bool {
    panel.isVisible()
}
