use std::borrow::Cow;

use gpui::{AssetSource, Hsla, SharedString, WindowAppearance, rgba};

use crate::ledger::Status;

pub const MONO_FONT: &str = "Menlo";

#[derive(Clone, Copy)]
pub struct Theme {
    pub panel: Hsla,
    pub raised: Hsla,
    pub text: Hsla,
    pub text_2: Hsla,
    pub text_3: Hsla,
    pub line: Hsla,
    pub hover: Hsla,
    pub fill: Hsla,
    pub accent: Hsla,
    pub accent_soft: Hsla,
    pub on_accent: Hsla,
    pub scrim: Hsla,
    pub red: Hsla,
    pub orange: Hsla,
    pub blue: Hsla,
    pub purple: Hsla,
    pub green: Hsla,
    pub gray: Hsla,
    pub indigo: Hsla,
}

impl Theme {
    pub fn for_appearance(appearance: WindowAppearance) -> Theme {
        match appearance {
            WindowAppearance::Dark | WindowAppearance::VibrantDark => Theme::dark(),
            WindowAppearance::Light | WindowAppearance::VibrantLight => Theme::light(),
        }
    }

    fn light() -> Theme {
        Theme {
            panel: rgba(0xf4f4f5ff).into(),
            raised: rgba(0xffffffd0).into(),
            text: rgba(0x000000db).into(),
            text_2: rgba(0x0000008f).into(),
            text_3: rgba(0x0000005c).into(),
            line: rgba(0x0000001a).into(),
            hover: rgba(0x0000000e).into(),
            fill: rgba(0x00000011).into(),
            accent: rgba(0x0a66e4ff).into(),
            accent_soft: rgba(0x0a66e41f).into(),
            on_accent: rgba(0xffffffff).into(),
            scrim: rgba(0x00000038).into(),
            red: rgba(0xd92d20ff).into(),
            orange: rgba(0xc26a00ff).into(),
            blue: rgba(0x0a66e4ff).into(),
            purple: rgba(0x8b46c9ff).into(),
            green: rgba(0x1b8f3fff).into(),
            gray: rgba(0x7c7c82ff).into(),
            indigo: rgba(0x6b6fb0ff).into(),
        }
    }

    fn dark() -> Theme {
        Theme {
            panel: rgba(0x262629ff).into(),
            raised: rgba(0xffffff12).into(),
            text: rgba(0xffffffeb).into(),
            text_2: rgba(0xffffff99).into(),
            text_3: rgba(0xffffff5c).into(),
            line: rgba(0xffffff1c).into(),
            hover: rgba(0xffffff12).into(),
            fill: rgba(0xffffff17).into(),
            accent: rgba(0x2f8cffff).into(),
            accent_soft: rgba(0x2f8cff33).into(),
            on_accent: rgba(0xffffffff).into(),
            scrim: rgba(0x00000059).into(),
            red: rgba(0xff6257ff).into(),
            orange: rgba(0xffa733ff).into(),
            blue: rgba(0x4a9dffff).into(),
            purple: rgba(0xcf87ffff).into(),
            green: rgba(0x3ddc68ff).into(),
            gray: rgba(0x9a9aa2ff).into(),
            indigo: rgba(0x9fa3eaff).into(),
        }
    }

    pub fn status_color(&self, status: Status, is_stuck: bool) -> Hsla {
        if is_stuck {
            return self.orange;
        }
        match status {
            Status::Scheduled => self.purple,
            Status::Waiting => self.indigo,
            Status::Running => self.blue,
            Status::Blocked => self.orange,
            Status::Failed => self.red,
            Status::Finished => self.green,
            Status::Cancelled => self.gray,
        }
    }
}

pub const ICON_LOGO: &str = "icons/logo.svg";
pub const ICON_REVIEW: &str = "icons/review.svg";
pub const ICON_SEARCH: &str = "icons/search.svg";
pub const ICON_CHEVRON: &str = "icons/chevron.svg";
pub const ICON_FOLD: &str = "icons/fold.svg";

pub fn status_icon(status: Status) -> &'static str {
    match status {
        Status::Scheduled => "icons/scheduled.svg",
        Status::Waiting => "icons/waiting.svg",
        Status::Running => "icons/running.svg",
        Status::Blocked => "icons/blocked.svg",
        Status::Failed => "icons/failed.svg",
        Status::Finished => "icons/finished.svg",
        Status::Cancelled => "icons/cancelled.svg",
    }
}

fn icon_body(path: &str) -> Option<&'static str> {
    Some(match path {
        "icons/scheduled.svg" => r#"<circle cx="8" cy="8" r="6.1"/><path d="M8 4.6V8l2.4 1.6"/>"#,
        "icons/waiting.svg" => r#"<circle cx="8" cy="8" r="6.1" stroke-dasharray="2.4 2.4"/>"#,
        "icons/running.svg" => r#"<circle cx="8" cy="8" r="6.1"/><path d="M6.7 5.6v4.8L10.5 8z" fill="black"/>"#,
        "icons/blocked.svg" => r#"<rect x="3.2" y="3.2" width="9.6" height="9.6" rx="2.2" fill="black" stroke="none"/>"#,
        "icons/failed.svg" => r#"<path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"/>"#,
        "icons/finished.svg" => r#"<path d="M3.2 8.6l3.1 3.1 6.5-7.4"/>"#,
        "icons/cancelled.svg" => r#"<circle cx="8" cy="8" r="6.1"/><path d="M4 12L12 4"/>"#,
        ICON_REVIEW => {
            r#"<path d="M3.8 3.2h8.4L14 9v3.6a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V9z"/><path d="M2 9h3.4l1 1.8h3.2l1-1.8H14"/>"#
        }
        ICON_LOGO => {
            r#"<rect x="2.2" y="3.2" width="11.6" height="9.8" rx="3"/><circle cx="6" cy="8" r="1.1" fill="black" stroke="none"/><circle cx="10" cy="8" r="1.1" fill="black" stroke="none"/><path d="M8 1.2v2"/>"#
        }
        ICON_SEARCH => r#"<circle cx="7" cy="7" r="4.4"/><path d="M10.4 10.4L14 14"/>"#,
        ICON_CHEVRON => r#"<path d="M6 3.2L10.8 8 6 12.8"/>"#,
        ICON_FOLD => {
            r#"<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="2.2"/><path d="M10 2.8v10.4"/><path d="M5 6.2L6.8 8 5 9.8"/>"#
        }
        _ => return None,
    })
}

/// Serves the app's monochrome icons; GPUI tints them with the element's text color.
pub struct Assets;

impl AssetSource for Assets {
    fn load(&self, path: &str) -> gpui::Result<Option<Cow<'static, [u8]>>> {
        Ok(icon_body(path).map(|body| {
            let svg = format!(
                r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" stroke="black" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>"#
            );
            Cow::Owned(svg.into_bytes())
        }))
    }

    fn list(&self, _path: &str) -> gpui::Result<Vec<SharedString>> {
        Ok(Vec::new())
    }
}
