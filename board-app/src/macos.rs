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

/// The golem logo's outlines: a head and shoulders, then the gem, eyes and chest seam.
/// A point inside an odd number of them is drawn, so the later ones cut holes in the first two.
const LOGO_OUTLINES: [&[(f64, f64)]; 6] = [
    &[
        (14.0, 1.8),
        (18.0, 0.8),
        (22.0, 1.8),
        (25.6, 6.4),
        (25.6, 8.2),
        (28.4, 9.2),
        (28.4, 15.2),
        (25.6, 16.2),
        (24.4, 20.2),
        (20.2, 22.0),
        (15.8, 22.0),
        (11.6, 20.2),
        (10.4, 16.2),
        (7.6, 15.2),
        (7.6, 9.2),
        (10.4, 8.2),
        (10.4, 6.4),
    ],
    &[
        (0.8, 35.0),
        (2.2, 28.6),
        (10.4, 24.4),
        (18.0, 27.2),
        (25.6, 24.4),
        (33.8, 28.6),
        (35.2, 35.0),
    ],
    &[(18.0, 3.0), (19.8, 5.5), (18.0, 8.0), (16.2, 5.5)],
    &[(12.4, 10.2), (16.8, 10.2), (16.8, 13.0), (12.4, 13.0)],
    &[(19.2, 10.2), (23.6, 10.2), (23.6, 13.0), (19.2, 13.0)],
    &[(17.2, 29.4), (18.8, 29.4), (18.8, 35.0), (17.2, 35.0)],
];

fn is_in_logo(x: f64, y: f64) -> bool {
    let center = f64::from(ICON_SIZE) / 2.0;
    let (x, y) = (
        (x - center) / LOGO_SCALE + center,
        (y - center) / LOGO_SCALE + center,
    );
    let mut is_inside = false;
    for outline in LOGO_OUTLINES {
        for (index, &(start_x, start_y)) in outline.iter().enumerate() {
            let (end_x, end_y) = outline[(index + 1) % outline.len()];
            let crosses_row = (start_y > y) != (end_y > y);
            if crosses_row && x < start_x + (y - start_y) / (end_y - start_y) * (end_x - start_x) {
                is_inside = !is_inside;
            }
        }
    }
    is_inside
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
