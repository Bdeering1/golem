//! The UI's zoom level: every design size is drawn at it, and it persists across launches.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};

use gpui::Pixels;

use crate::ledger;

pub const DEFAULT_PERCENT: u32 = 100;
pub const STEP_PERCENT: u32 = 10;
const MIN_PERCENT: u32 = 80;
const MAX_PERCENT: u32 = 150;
const FILE_NAME: &str = "app-zoom";

static PERCENT: AtomicU32 = AtomicU32::new(DEFAULT_PERCENT);

fn file_path() -> PathBuf {
    ledger::state_directory().join(FILE_NAME)
}

pub fn percent() -> u32 {
    PERCENT.load(Ordering::Relaxed)
}

pub fn factor() -> f32 {
    percent() as f32 / 100.
}

/// A design length at the current zoom. Never pass it a length GPUI measured, which is
/// already zoomed.
pub fn px(length: f32) -> Pixels {
    gpui::px(length * factor())
}

pub fn load() {
    let saved = std::fs::read_to_string(file_path())
        .ok()
        .and_then(|text| text.trim().parse::<u32>().ok());
    if let Some(percent) = saved {
        PERCENT.store(percent.clamp(MIN_PERCENT, MAX_PERCENT), Ordering::Relaxed);
    }
}

/// Sets and saves the zoom, kept within its limits, and returns what it became.
pub fn set(percent: u32) -> u32 {
    let percent = percent.clamp(MIN_PERCENT, MAX_PERCENT);
    PERCENT.store(percent, Ordering::Relaxed);
    let _ = std::fs::write(file_path(), percent.to_string());
    percent
}
