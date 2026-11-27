//! The menu bar item and the AppKit calls GPUI lacks: placing, showing and hiding the panel.

use objc2::MainThreadMarker;
use objc2::rc::Retained;
use objc2_app_kit::{NSApplication, NSView, NSWindow};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

const ICON_SIZE: u32 = 36;
const ICON_SUPERSAMPLING: u32 = 4;
/// The outlines fill the icon; shrunk about its center, the logo matches other menu bar icons.
const LOGO_SCALE: f64 = 0.8;
const PANEL_GAP: f64 = 4.0;
const SCREEN_MARGIN: f64 = 10.0;
/// How far past the item's center the panel's right edge may sit.
const PANEL_RIGHT_OVERHANG: f64 = 220.0;

type Outline = &'static [(f64, f64)];

/// The golem logo's head, drawn as a line `HEAD_LINE_WIDTH` wide.
const HEAD_OUTLINE: Outline = &[
    (12.0, 4.5),
    (18.0, 2.8),
    (24.0, 4.5),
    (28.5, 11.0),
    (28.5, 13.5),
    (32.5, 15.0),
    (32.5, 23.0),
    (28.5, 24.5),
    (26.5, 30.5),
    (21.0, 33.0),
    (15.0, 33.0),
    (9.5, 30.5),
    (7.5, 24.5),
    (3.5, 23.0),
    (3.5, 15.0),
    (7.5, 13.5),
    (7.5, 11.0),
];
const HEAD_LINE_WIDTH: f64 = 2.8;
/// The head's gem and eyes, drawn solid.
const FEATURE_OUTLINES: [Outline; 3] = [
    &[(18.0, 7.0), (20.4, 10.2), (18.0, 13.4), (15.6, 10.2)],
    &[(10.2, 17.0), (16.2, 17.0), (16.2, 20.8), (10.2, 20.8)],
    &[(19.8, 17.0), (25.8, 17.0), (25.8, 20.8), (19.8, 20.8)],
];

fn edges(outline: Outline) -> impl Iterator<Item = ((f64, f64), (f64, f64))> {
    let ends = outline.iter().cycle().skip(1);
    outline.iter().copied().zip(ends.copied())
}

fn is_inside(outline: Outline, x: f64, y: f64) -> bool {
    let crossings = edges(outline).filter(|&((start_x, start_y), (end_x, end_y))| {
        let crosses_row = (start_y > y) != (end_y > y);
        crosses_row && x < start_x + (y - start_y) / (end_y - start_y) * (end_x - start_x)
    });
    crossings.count() % 2 == 1
}

fn distance_to_edge(outline: Outline, x: f64, y: f64) -> f64 {
    let distances = edges(outline).map(|((start_x, start_y), (end_x, end_y))| {
        let (edge_x, edge_y) = (end_x - start_x, end_y - start_y);
        let along =
            ((x - start_x) * edge_x + (y - start_y) * edge_y) / (edge_x * edge_x + edge_y * edge_y);
        let along = along.clamp(0.0, 1.0);
        (x - start_x - along * edge_x).hypot(y - start_y - along * edge_y)
    });
    distances.fold(f64::INFINITY, f64::min)
}

fn is_in_logo(x: f64, y: f64) -> bool {
    let center = f64::from(ICON_SIZE) / 2.0;
    let (x, y) = (
        (x - center) / LOGO_SCALE + center,
        (y - center) / LOGO_SCALE + center,
    );
    distance_to_edge(HEAD_OUTLINE, x, y) <= HEAD_LINE_WIDTH / 2.0
        || FEATURE_OUTLINES
            .iter()
            .any(|outline| is_inside(outline, x, y))
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
                covered += u32::from(is_in_logo(x, y));
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

pub fn native_window(window: &gpui::Window) -> Option<Retained<NSWindow>> {
    let handle = HasWindowHandle::window_handle(window).ok()?;
    let RawWindowHandle::AppKit(appkit_handle) = handle.as_raw() else {
        return None;
    };
    // SAFETY: GPUI's AppKit handle points at the window's live content view.
    let view: &NSView = unsafe { appkit_handle.ns_view.cast().as_ref() };
    view.window()
}

/// The panel frame that puts its top just below the menu bar item, kept on the item's screen.
/// AppKit screen coordinates have their origin at the bottom left.
pub fn panel_frame(tray_icon: &TrayIcon, width: f64, height: f64) -> Option<NSRect> {
    let mtm = MainThreadMarker::new()?;
    let item_window = tray_icon.ns_status_item()?.button(mtm)?.window()?;
    let item_frame = item_window.frame();
    let item_center = item_frame.origin.x + item_frame.size.width / 2.0;
    let top = item_frame.origin.y - PANEL_GAP;
    let mut width = width;
    let mut right = item_center + PANEL_RIGHT_OVERHANG.min(width / 2.0);
    let mut bottom_limit = f64::MIN;
    // The item's screen, not the panel's: a hidden panel reports the screen it last opened on.
    if let Some(screen) = item_window.screen() {
        let visible = screen.visibleFrame();
        let left_limit = visible.origin.x + SCREEN_MARGIN;
        let right_limit = visible.origin.x + visible.size.width - SCREEN_MARGIN;
        width = width.min(right_limit - left_limit);
        right = right.max(left_limit + width).min(right_limit);
        bottom_limit = visible.origin.y + SCREEN_MARGIN;
    }
    let height = height.min(top - bottom_limit);
    Some(NSRect::new(
        NSPoint::new(right - width, top - height),
        NSSize::new(width, height),
    ))
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
