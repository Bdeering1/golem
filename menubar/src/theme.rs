use std::borrow::Cow;

use gpui::{AssetSource, Hsla, SharedString, WindowAppearance, rgba};

use crate::ledger::Status;

pub const MONO_FONT: &str = "Menlo";

#[derive(Clone, Copy)]
pub struct Theme {
    pub panel: Hsla,
    pub raised: Hsla,
    /// The list column beside open details, one step back so the details read first.
    pub sidebar: Hsla,
    pub text: Hsla,
    pub text_2: Hsla,
    pub text_3: Hsla,
    pub line: Hsla,
    /// A light shadow on dark reads as a glow and a dark one dims the translucent fill,
    /// so the dark selected tab has none.
    pub segment_shadow: Hsla,
    pub hover: Hsla,
    pub fill: Hsla,
    pub accent: Hsla,
    pub accent_soft: Hsla,
    /// The selected card; a muted blue, so it differs from hover's grey but its filled quick
    /// action stays the only strong blue.
    pub selection: Hsla,
    /// Filter matches, in a find-in-page yellow with dark text so they don't read as a text
    /// selection.
    pub found: Hsla,
    pub on_found: Hsla,
    /// Darker accent and red for filled controls, so white labels on them stay readable.
    pub accent_fill: Hsla,
    pub red_fill: Hsla,
    pub changed: Hsla,
    pub toast: Hsla,
    pub on_accent: Hsla,
    pub scrim: Hsla,
    pub red: Hsla,
    pub orange: Hsla,
    /// Icons only need 3:1, so the light theme's blocked square stays orange, not brown.
    pub orange_icon: Hsla,
    pub cyan: Hsla,
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
            sidebar: rgba(0xebebedff).into(),
            text: rgba(0x000000db).into(),
            text_2: rgba(0x000000a8).into(),
            text_3: rgba(0x00000090).into(),
            line: rgba(0x0000001a).into(),
            segment_shadow: rgba(0x0000001a).into(),
            hover: rgba(0x0000000e).into(),
            fill: rgba(0x00000011).into(),
            accent: rgba(0x0a66e4ff).into(),
            accent_soft: rgba(0x0a66e41f).into(),
            selection: rgba(0x2a5bb524).into(),
            found: rgba(0xffd60aa6).into(),
            on_found: rgba(0x000000db).into(),
            accent_fill: rgba(0x0a66e4ff).into(),
            red_fill: rgba(0xd92d20ff).into(),
            changed: rgba(0x0a66e40f).into(),
            toast: rgba(0xffffffff).into(),
            on_accent: rgba(0xffffffff).into(),
            scrim: rgba(0x0000004d).into(),
            red: rgba(0xc4271bff).into(),
            orange: rgba(0x9e5600ff).into(),
            orange_icon: rgba(0xc46a00ff).into(),
            cyan: rgba(0x0b6c86ff).into(),
            purple: rgba(0x8b46c9ff).into(),
            green: rgba(0x177a36ff).into(),
            gray: rgba(0x6b6b71ff).into(),
            indigo: rgba(0x5a5ea3ff).into(),
        }
    }

    fn dark() -> Theme {
        Theme {
            panel: rgba(0x262629ff).into(),
            raised: rgba(0xffffff12).into(),
            sidebar: rgba(0x1f1f22ff).into(),
            text: rgba(0xffffffeb).into(),
            text_2: rgba(0xffffffb3).into(),
            text_3: rgba(0xffffff8c).into(),
            line: rgba(0xffffff1c).into(),
            segment_shadow: rgba(0x00000000).into(),
            hover: rgba(0xffffff0d).into(),
            fill: rgba(0xffffff17).into(),
            accent: rgba(0x2f8cffff).into(),
            accent_soft: rgba(0x2f8cff33).into(),
            selection: rgba(0x9db4ff26).into(),
            found: rgba(0xffd60ae0).into(),
            on_found: rgba(0x000000e6).into(),
            accent_fill: rgba(0x0b6bdeff).into(),
            red_fill: rgba(0xd92d20ff).into(),
            changed: rgba(0x2f8cff1a).into(),
            toast: rgba(0x3a3a3eff).into(),
            on_accent: rgba(0xffffffff).into(),
            scrim: rgba(0x00000073).into(),
            red: rgba(0xff6257ff).into(),
            orange: rgba(0xffa733ff).into(),
            orange_icon: rgba(0xffa733ff).into(),
            cyan: rgba(0x3fc6dcff).into(),
            purple: rgba(0xcf87ffff).into(),
            green: rgba(0x3ddc68ff).into(),
            gray: rgba(0x9a9aa2ff).into(),
            indigo: rgba(0x9fa3eaff).into(),
        }
    }

    pub fn status_icon_color(&self, status: Status, is_stuck: bool) -> Hsla {
        if is_stuck || status == Status::Blocked {
            return self.orange_icon;
        }
        self.status_color(status, is_stuck)
    }

    pub fn status_color(&self, status: Status, is_stuck: bool) -> Hsla {
        if is_stuck {
            return self.orange;
        }
        match status {
            Status::Scheduled => self.purple,
            Status::Waiting => self.indigo,
            Status::Running => self.cyan,
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
pub const ICON_WARNING: &str = "icons/warning.svg";

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
        "icons/waiting.svg" => {
            r#"<circle cx="8" cy="8" r="6.1"/><path d="M6.6 5.9v4.2M9.4 5.9v4.2"/>"#
        }
        "icons/running.svg" => {
            r#"<circle cx="8" cy="8" r="6.1"/><path d="M6.7 5.6v4.8L10.5 8z" fill="black"/>"#
        }
        "icons/blocked.svg" => {
            r#"<rect x="3.2" y="3.2" width="9.6" height="9.6" rx="2.2" fill="black" stroke="none"/>"#
        }
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
        ICON_WARNING => {
            r#"<path d="M8 2.4L14.2 13.2H1.8z"/><path d="M8 6.6v2.8"/><circle cx="8" cy="11.2" r="0.5" fill="black"/>"#
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
