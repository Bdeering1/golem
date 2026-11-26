use std::collections::{HashMap, HashSet};
use std::f32::consts::FRAC_PI_2;
use std::rc::Rc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, Local};
use futures::StreamExt;
use futures::channel::mpsc::UnboundedReceiver;
use gpui::{
    Animation, AnimationExt, AnyElement, BoxShadow, ClickEvent, ClipboardItem, Context, Div,
    ElementId, FocusHandle, Font, FontWeight, HighlightStyle, Hsla, KeyDownEvent, MouseButton,
    MouseDownEvent, Pixels, ScrollEvent, ScrollHandle, SharedString, Stateful, StyledText,
    Subscription, Task, TextRun, Transformation, Window, WindowTextSystem, div, linear_color_stop,
    linear_gradient, point, prelude::*, pulsating_between, px, radians, svg,
};
use notify::Watcher;
use objc2::rc::Retained;
use objc2_app_kit::NSWindow;
use tray_icon::TrayIcon;

use crate::board::{
    self, ACTIONS, Action, ActivityItem, ActivityLevel, BOARD_COLUMNS, Column, Row, Snapshot,
};
use crate::golem_cli::{self, Golem};
use crate::ledger::{self, Status};
use crate::macos;
use crate::theme::{self, MONO_FONT, Theme};

const PANEL_WIDTH: f64 = 440.0;
const EXPANDED_PANEL_WIDTH: f64 = 900.0;
const PANEL_HEIGHT: f64 = 640.0;
const MIN_PANEL_HEIGHT: f64 = 320.0;
/// Collapsed-panel layout heights used to fit the panel to its longest group.
const PANEL_CHROME_HEIGHT: f64 = 134.0;
const LOAD_ERROR_HEIGHT: f64 = 25.0;
const TOAST_ROW_HEIGHT: f64 = 44.0;
const CARD_GAP: f32 = 2.0;
/// The footer and the details action bar share a height so their top edges line up.
const BOTTOM_BAR_HEIGHT: f32 = 37.0;
/// The shortcuts sheet's height plus the footer below it; the palette uses it too.
const HELP_PANEL_HEIGHT: f64 = 500.0;
const SMALL_SHEET_MAX_WIDTH: f32 = 440.0;
const LOAD_ERROR_TITLE: &str = "Can't read the ledger";
const ERROR_TEXT_MAX_HEIGHT: f32 = 220.0;
const ERROR_LINE_STEP: f32 = 21.0;
/// A page of the error text less two lines, so paging never skips a line.
const ERROR_PAGE_STEP: f32 = ERROR_TEXT_MAX_HEIGHT - 2. * ERROR_LINE_STEP;
const REPOSITORY_MAX_WIDTH: f32 = 120.0;
const CARD_PITCH: f64 = 63.0;
const EMPTY_LIST_HEIGHT: f64 = 90.0;
const TICK_INTERVAL: Duration = Duration::from_secs(1);
const RELOAD_DEBOUNCE: Duration = Duration::from_millis(80);
const TOAST_DURATION: Duration = Duration::from_secs(3);
const ERROR_TOAST_DURATION: Duration = Duration::from_secs(8);
const CHANGE_HIGHLIGHT_DURATION: Duration = Duration::from_secs(10);
/// A menu bar click that lands this soon after the panel hid itself is the click that hid it.
const REOPEN_GUARD: Duration = Duration::from_millis(300);
const FILTER_RESET_AFTER: Duration = Duration::from_secs(60);
const SCROLL_STEP: f32 = 240.0;
const BUSY_PULSE_PERIOD: Duration = Duration::from_millis(1400);
const BUSY_PULSE_FPS: f32 = 30.0;
/// The details body's side padding, and a callout's extra inset (padding and left border).
const DETAIL_SIDE_PADDING: f32 = 18.;
const CALLOUT_INSET: f32 = 27.;
const PROMPT_LINE_LIMIT: usize = 3;
const PARAGRAPH_GAP: f32 = 7.;
/// A one-letter keycap's width; a button that hides its keycap while typing pads both sides
/// by half of it and its gap, so the button keeps its width and its label stays centred.
const KEYCAP_WIDTH: f32 = 17.;
const COPIED_DURATION: Duration = Duration::from_millis(1500);
/// Session fields the palette offers to copy, for handing off to a terminal.
const COPYABLE_FIELDS: [(&str, &str); 6] = [
    ("Directory", "Copy directory"),
    ("Worktree", "Copy worktree path"),
    ("Branch", "Copy branch"),
    ("Session", "Copy session ID"),
    ("Gate", "Copy gate command"),
    ("Setup", "Copy setup command"),
];
const RESULT_LINE_LIMIT: usize = 12;
const CALLOUT_LINE_LIMIT: usize = 4;
const SECTION_RESULT: &str = "result";
const SECTION_PROMPT: &str = "prompt";
const SECTION_STOP_REASON: &str = "stop-reason";
const SECTION_ACTION_ERROR: &str = "action-error";
const EXPANDABLE_SECTIONS: [&str; 4] = [
    SECTION_RESULT,
    SECTION_PROMPT,
    SECTION_STOP_REASON,
    SECTION_ACTION_ERROR,
];
const MESSAGE_ITEM_LIMIT: usize = 12;
const STOPPED_MESSAGE_ITEM_LIMIT: usize = 3;
/// The palette's top offset, search row and margin, subtracted from the panel height for its list.
const PALETTE_CHROME_HEIGHT: f32 = 112.0;
const PALETTE_ROW_HEIGHT: f32 = 28.0;
const PALETTE_HEADING_HEIGHT: f32 = 28.5;
const PALETTE_LIST_PADDING: f32 = 5.0;
const PALETTE_MIN_PEEK: f32 = 8.0;
const PALETTE_FADE_HEIGHT: f32 = 22.0;
/// Tall enough to fade a whole line, so a cut-off section reads as more below.
const DETAIL_FADE_HEIGHT: f32 = 32.0;
const NOTHING_NEEDS_YOU_TEXT: &str = "Nothing needs you right now";
const GOLEM_MISSING_TEXT: &str = "Still looking for golem";
const TERMINAL_BACKENDS: [&str; 2] = ["claude", "codex"];
const FILLED_KEYCAP_BACKGROUND: u32 = 0x00000030;
const KEY_ESCAPE: &str = "escape";
const KEY_ENTER: &str = "enter";
const KEY_DOWN: &str = "down";
const KEY_UP: &str = "up";
const KEY_BACKSPACE: &str = "backspace";
const TAB_COLUMNS: [Column; 3] = [Column::Queued, Column::Running, Column::Review];
const ATTENTION_ORDER: [Column; 4] = [
    Column::Review,
    Column::Running,
    Column::Queued,
    Column::Done,
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum Input {
    None,
    Filter,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Command {
    Task(Action),
    NextAttention,
    ToggleDone,
    ToggleDetails,
    ToggleSession,
    /// Copies the selected task's Session field with this label.
    CopyField(&'static str),
    Quit,
}

struct TextMeasure {
    system: Rc<WindowTextSystem>,
    font: Font,
}

struct ErrorDetail {
    title: &'static str,
    text: SharedString,
}

struct PaletteItem {
    group: String,
    command: Command,
    label: String,
    key: String,
    is_danger: bool,
}

#[derive(Default)]
struct Palette {
    query: String,
    caret: usize,
    index: usize,
}

struct Toast {
    text: SharedString,
    id: u64,
    is_error: bool,
    /// The failed task a click on the toast opens.
    task_id: Option<String>,
    /// The action whose failure the toast reports, which titles its error sheet.
    action: Option<Action>,
}

/// The result of a background `golem` run: the toast text and whether it reports a failure.
struct Outcome {
    message: String,
    is_error: bool,
}

/// A task's last failed `golem` run, shown on its card and in details until the next action.
struct ActionError {
    action: Action,
    message: String,
}

struct Pending {
    action: Action,
    task_id: Option<String>,
}

pub struct BoardView {
    snapshot: Snapshot,
    load_error: Option<String>,
    /// The full text of an error toast that has no task to open, shown in its own sheet.
    error_detail: Option<ErrorDetail>,
    /// Whether any ledger read has succeeded, so an empty list can mean "nothing" rather than "unknown".
    loaded_at: Option<DateTime<Local>>,
    golem: Option<Arc<Golem>>,
    tray_icon: TrayIcon,
    panel: Option<Retained<NSWindow>>,
    focus_handle: FocusHandle,
    list_scroll: ScrollHandle,
    error_scroll: ScrollHandle,
    detail_scroll: ScrollHandle,
    palette_scroll: ScrollHandle,
    input_scroll: ScrollHandle,
    tab: Column,
    selected_roots: HashMap<Column, String>,
    is_showing_done: bool,
    query: String,
    query_caret: usize,
    input: Input,
    is_expanded: bool,
    /// A stopped task with a Result shows its activity as one counts line until opened.
    is_activity_open: bool,
    /// Whether the activity shows its earlier messages, not just the latest few.
    is_showing_earlier: bool,
    is_following: bool,
    is_follow_paused: bool,
    /// The activity shown when Follow paused; it stays put while new lines arrive.
    paused_lines: Vec<String>,
    is_showing_fields: bool,
    /// Clamped sections the user expanded, by toggle id; E expands or collapses all of them.
    expanded_sections: HashSet<&'static str>,
    /// Activity items, by index, whose folded tool calls were opened in place.
    expanded_folds: HashSet<usize>,
    /// The task the details last showed; any other selection resets the per-task view.
    shown_root_id: Option<String>,
    shown_task_id: Option<String>,
    is_error_copied: bool,
    copied_id: Option<SharedString>,
    /// The window's text system and font, kept from the last frame to measure wrapping.
    text_measure: Option<TextMeasure>,
    /// The panel size render last asked AppKit for, so content changes resize it once.
    placed_size: Option<(f64, f64)>,
    pending: Option<Pending>,
    is_showing_help: bool,
    palette: Option<Palette>,
    toast: Option<Toast>,
    toast_count: u64,
    busy_actions: HashMap<String, Action>,
    action_errors: HashMap<String, ActionError>,
    pull_request_presses: HashMap<String, usize>,
    changed_until: HashMap<String, Instant>,
    detail_activity: Option<(String, Vec<String>)>,
    hidden_at: Option<Instant>,
    _watcher: Option<notify::RecommendedWatcher>,
    _hotkey_manager: Option<global_hotkey::GlobalHotKeyManager>,
    _subscriptions: Vec<Subscription>,
    _tasks: Vec<Task<()>>,
}

fn ledger_watcher(
    sender: futures::channel::mpsc::UnboundedSender<bool>,
) -> Option<notify::RecommendedWatcher> {
    let state_directory = ledger::state_directory();
    let progress_directory = state_directory.join(ledger::PROGRESS_DIRECTORY_NAME);
    std::fs::create_dir_all(&progress_directory).ok()?;
    let mut watcher = notify::recommended_watcher(move |result: notify::Result<notify::Event>| {
        let Ok(event) = result else {
            return;
        };
        let is_ledger = |path: &std::path::PathBuf| {
            path.file_name()
                .is_some_and(|name| name == ledger::LEDGER_FILE_NAME)
        };
        let is_progress = |path: &std::path::PathBuf| {
            path.parent()
                .and_then(|parent| parent.file_name())
                .is_some_and(|name| name == ledger::PROGRESS_DIRECTORY_NAME)
        };
        if event.paths.iter().any(is_ledger) {
            let _ = sender.unbounded_send(true);
        } else if event.paths.iter().any(is_progress) {
            let _ = sender.unbounded_send(false);
        }
    })
    .ok()?;
    watcher
        .watch(&state_directory, notify::RecursiveMode::NonRecursive)
        .ok()?;
    watcher
        .watch(&progress_directory, notify::RecursiveMode::NonRecursive)
        .ok()?;
    Some(watcher)
}

fn kbd(text: &str, theme: &Theme) -> Div {
    div()
        .flex_none()
        .px(px(4.))
        .min_w(px(KEYCAP_WIDTH))
        .h(px(16.))
        .flex()
        .items_center()
        .justify_center()
        .rounded(px(4.))
        .bg(theme.fill)
        .text_color(theme.text_2)
        .text_size(px(11.))
        .font_weight(FontWeight::MEDIUM)
        .child(text.to_string())
}

fn icon(path: &'static str, size: f32, color: Hsla) -> gpui::Svg {
    svg()
        .path(path)
        .size(px(size))
        .flex_none()
        .text_color(color)
}

/// The slow fade on a busy task's icon and button, so an in-flight run doesn't look frozen.
fn busy_pulse() -> Animation {
    let mut animation = Animation::new(BUSY_PULSE_PERIOD)
        .repeat()
        .with_easing(pulsating_between(0.3, 1.0));
    animation.max_fps = Some(BUSY_PULSE_FPS);
    animation
}

fn key_label(action: Action) -> String {
    let key = action.key();
    if key.chars().all(char::is_uppercase) {
        format!("\u{21e7}{key}")
    } else {
        key.to_uppercase()
    }
}

impl BoardView {
    pub fn new(
        window: &mut Window,
        cx: &mut Context<Self>,
        toggle_receiver: UnboundedReceiver<()>,
        hotkey_manager: Option<global_hotkey::GlobalHotKeyManager>,
    ) -> Self {
        let (reload_sender, reload_receiver) = futures::channel::mpsc::unbounded::<bool>();
        let watcher = ledger_watcher(reload_sender);
        let subscriptions = vec![
            cx.observe_window_activation(window, |this, window, cx| {
                if !window.is_window_active() {
                    this.hide(cx);
                }
            }),
            cx.observe_window_appearance(window, |_, _, cx| cx.notify()),
        ];
        let tasks = vec![
            cx.spawn_in(window, async move |this, cx| {
                let mut toggle_receiver = toggle_receiver;
                while toggle_receiver.next().await.is_some() {
                    let _ = this.update_in(cx, |this, window, cx| this.toggle(window, cx));
                }
            }),
            cx.spawn(async move |this, cx| {
                let mut reload_receiver = reload_receiver;
                while let Some(is_ledger) = reload_receiver.next().await {
                    cx.background_executor().timer(RELOAD_DEBOUNCE).await;
                    let mut is_ledger_changed = is_ledger;
                    while let Ok(is_ledger) = reload_receiver.try_recv() {
                        is_ledger_changed |= is_ledger;
                    }
                    // Progress logs change constantly while a task runs; only the open panel shows them.
                    let reloaded = this.update(cx, |this, cx| {
                        if is_ledger_changed || this.is_visible() {
                            this.reload(cx);
                        }
                    });
                    if reloaded.is_err() {
                        break;
                    }
                }
            }),
            cx.spawn(async move |this, cx| {
                loop {
                    cx.background_executor().timer(TICK_INTERVAL).await;
                    let is_alive = this.update(cx, |this, cx| this.tick(cx)).is_ok();
                    if !is_alive {
                        break;
                    }
                }
            }),
            cx.spawn(async move |this, cx| {
                let golem = cx
                    .background_executor()
                    .spawn(async { Golem::discover() })
                    .await;
                let _ = this.update(cx, |this, _| this.golem = Some(Arc::new(golem)));
            }),
        ];
        let mut view = BoardView {
            snapshot: Snapshot::default(),
            load_error: None,
            error_detail: None,
            loaded_at: None,
            golem: None,
            tray_icon: macos::create_tray_icon(),
            panel: macos::native_window(window),
            focus_handle: cx.focus_handle(),
            list_scroll: ScrollHandle::new(),
            error_scroll: ScrollHandle::new(),
            detail_scroll: ScrollHandle::new(),
            palette_scroll: ScrollHandle::new(),
            input_scroll: ScrollHandle::new(),
            tab: Column::Review,
            selected_roots: HashMap::new(),
            is_showing_done: false,
            query: String::new(),
            query_caret: 0,
            input: Input::None,
            is_expanded: false,
            is_activity_open: false,
            is_showing_earlier: false,
            is_following: false,
            is_follow_paused: false,
            paused_lines: Vec::new(),
            is_showing_fields: false,
            expanded_sections: HashSet::new(),
            expanded_folds: HashSet::new(),
            shown_root_id: None,
            shown_task_id: None,
            is_error_copied: false,
            copied_id: None,
            text_measure: None,
            placed_size: None,
            pending: None,
            is_showing_help: false,
            palette: None,
            toast: None,
            toast_count: 0,
            busy_actions: HashMap::new(),
            action_errors: HashMap::new(),
            pull_request_presses: HashMap::new(),
            changed_until: HashMap::new(),
            detail_activity: None,
            hidden_at: None,
            _watcher: watcher,
            _hotkey_manager: hotkey_manager,
            _subscriptions: subscriptions,
            _tasks: tasks,
        };
        view.reload(cx);
        view
    }

    // ---- data -------------------------------------------------------------------------------

    fn reload(&mut self, cx: &mut Context<Self>) {
        match ledger::load_tasks() {
            Ok(tasks) => {
                let selected_index = self.selected_index();
                let snapshot = board::board_snapshot(tasks, Local::now());
                let previous = std::mem::replace(&mut self.snapshot, snapshot);
                self.note_changes(&previous, cx);
                self.keep_selection_position(selected_index);
                self.load_error = None;
                self.loaded_at = Some(Local::now());
            }
            Err(error) => self.load_error = Some(error),
        }
        self.detail_activity = None;
        self.update_tray();
        if self.is_following && !self.is_follow_paused {
            self.detail_scroll.scroll_to_bottom();
        }
        cx.notify();
    }

    /// Highlights count down only on screen: while the panel shows and in the current tab, so
    /// another tab's changes keep its dot until it is opened.
    fn tick(&mut self, cx: &mut Context<Self>) {
        if !self.is_visible() {
            return;
        }
        let now = Instant::now();
        let unseen_until = now + CHANGE_HIGHLIGHT_DURATION;
        let shown: HashSet<String> = (self.column_rows(self.tab).into_iter())
            .map(|row| row.root_id.clone())
            .collect();
        let rows = &self.snapshot.rows;
        self.changed_until.retain(|root_id, until| {
            if !shown.contains(root_id) {
                *until = unseen_until;
            }
            *until > now && rows.iter().any(|row| &row.root_id == root_id)
        });
        cx.notify();
    }

    fn update_tray(&self) {
        let count = |column: Column| {
            self.snapshot
                .rows
                .iter()
                .filter(|row| row.column == column)
                .count()
        };
        // The menu bar shows what Next counts, so it drops to zero once nothing needs you.
        let needing_count = self
            .snapshot
            .rows
            .iter()
            .filter(|row| row.column != Column::Done && self.needs_you(row))
            .count();
        let title = (needing_count > 0).then(|| format!(" {needing_count}"));
        self.tray_icon.set_title(title);
        let tooltip = format!(
            "golem: {needing_count} need you; {} queued, {} running, {} to review",
            count(Column::Queued),
            count(Column::Running),
            count(Column::Review)
        );
        let _ = self.tray_icon.set_tooltip(Some(tooltip));
    }

    /// Tints changed cards and keeps the selected task in view when it moves to another group;
    /// other tasks' changes show only as the tint, the tab dot and the Next count.
    fn note_changes(&mut self, previous: &Snapshot, cx: &mut Context<Self>) {
        if previous.tasks.is_empty() {
            return;
        }
        let before: HashMap<&str, (Column, Status)> = previous
            .rows
            .iter()
            .map(|row| (row.root_id.as_str(), (row.column, row.status)))
            .collect();
        let until = Instant::now() + CHANGE_HIGHLIGHT_DURATION;
        for row in &self.snapshot.rows {
            let position = (row.column, row.status);
            let earlier = before.get(row.root_id.as_str());
            if earlier != Some(&position) {
                self.changed_until.insert(row.root_id.clone(), until);
            }
            if earlier.is_some_and(|(_, status)| *status != row.status) {
                self.action_errors.remove(&row.task_id);
            }
        }
        let selected = self.selected_roots.get(&self.tab).cloned();
        let moved = selected.and_then(|root| {
            let row = self.snapshot.rows.iter().find(|row| row.root_id == root)?;
            let is_visible = row.column != Column::Done || self.is_showing_done;
            (row.column != self.tab && before.contains_key(root.as_str()) && is_visible)
                .then(|| row.clone())
        });
        if let Some(row) = moved {
            self.tab = row.column;
            self.selected_roots.insert(row.column, row.root_id.clone());
            let name = board::task_display_name(&self.snapshot.tasks, &row.task_id);
            self.show_toast(
                format!(
                    "{name} {}, now in {}",
                    row.status.name(),
                    row.column.title()
                ),
                cx,
            );
        }
    }

    fn tabs(&self) -> Vec<Column> {
        let mut tabs = TAB_COLUMNS.to_vec();
        if self.is_showing_done {
            tabs.push(Column::Done);
        }
        tabs
    }

    fn filtered_rows(&self) -> Vec<&Row> {
        self.snapshot
            .rows
            .iter()
            .filter(|row| {
                self.query.is_empty() || board::is_matching(row, &self.snapshot.tasks, &self.query)
            })
            .collect()
    }

    fn column_rows(&self, column: Column) -> Vec<&Row> {
        self.filtered_rows()
            .into_iter()
            .filter(|row| row.column == column)
            .collect()
    }

    fn selected_row(&self) -> Option<&Row> {
        let rows = self.column_rows(self.tab);
        let selected = self.selected_roots.get(&self.tab);
        rows.iter()
            .find(|row| Some(&row.root_id) == selected)
            .or(rows.first())
            .copied()
    }

    fn selected_index(&self) -> Option<usize> {
        let selected = self.selected_row()?.root_id.clone();
        self.column_rows(self.tab)
            .iter()
            .position(|row| row.root_id == selected)
    }

    /// When the selected task leaves the group, selects the task that took its place.
    fn keep_selection_position(&mut self, previous_index: Option<usize>) {
        let rows = self.column_rows(self.tab);
        let selected = self.selected_roots.get(&self.tab);
        let is_still_shown = rows.iter().any(|row| Some(&row.root_id) == selected);
        let (Some(index), false, Some(last)) =
            (previous_index, is_still_shown, rows.len().checked_sub(1))
        else {
            return;
        };
        let root_id = rows[index.min(last)].root_id.clone();
        self.selected_roots.insert(self.tab, root_id);
    }

    fn needs_you(&self, row: &Row) -> bool {
        board::needs_you(&self.snapshot.tasks, row, Local::now())
    }

    fn selected_activity(&mut self) -> Vec<String> {
        let Some(row) = self.selected_row().cloned() else {
            return Vec::new();
        };
        if let Some((task_id, lines)) = &self.detail_activity
            && *task_id == row.task_id
        {
            return lines.clone();
        }
        let lines = board::task_activity(&row, &self.snapshot);
        self.detail_activity = Some((row.task_id.clone(), lines.clone()));
        lines
    }

    // ---- panel ------------------------------------------------------------------------------

    fn is_visible(&self) -> bool {
        self.panel.as_deref().is_some_and(macos::is_panel_visible)
    }

    fn panel_width(&self) -> f64 {
        if self.is_expanded {
            EXPANDED_PANEL_WIDTH
        } else {
            PANEL_WIDTH
        }
    }

    /// The collapsed panel fits its longest group so it doesn't change height when switching tabs.
    fn panel_height(&self) -> f64 {
        if self.is_expanded {
            return PANEL_HEIGHT;
        }
        let longest = self
            .tabs()
            .into_iter()
            .map(|column| {
                let rows = self.snapshot.rows.iter();
                rows.filter(|row| row.column == column).count()
            })
            .max()
            .unwrap_or(0);
        let list_height = (longest as f64 * CARD_PITCH).max(EMPTY_LIST_HEIGHT);
        let minimum_height = if self.is_showing_help || self.palette.is_some() {
            HELP_PANEL_HEIGHT
        } else {
            MIN_PANEL_HEIGHT
        };
        let load_error_height = if self.load_error.is_some() {
            LOAD_ERROR_HEIGHT
        } else {
            0.
        };
        let toast_height = if self.toast.as_ref().is_some_and(|toast| toast.is_error) {
            TOAST_ROW_HEIGHT
        } else {
            0.
        };
        (PANEL_CHROME_HEIGHT + load_error_height + toast_height + list_height)
            .clamp(minimum_height, PANEL_HEIGHT)
    }

    /// Moves the panel under the menu bar item, then shows it if asked. AppKit applies the
    /// frame on the next turn of the run loop, because GPUI can't take a resize mid-update.
    fn place(&self, is_showing: bool, cx: &mut Context<Self>) {
        let Some(panel) = self.panel.clone() else {
            return;
        };
        let frame = macos::panel_frame(
            &panel,
            &self.tray_icon,
            self.panel_width(),
            self.panel_height(),
        );
        cx.spawn(async move |_, _| {
            if let Some(frame) = frame {
                panel.setFrame_display(frame, true);
            }
            if is_showing {
                macos::show_panel(&panel);
            }
        })
        .detach();
    }

    fn toggle(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        if self.is_visible() {
            self.hide(cx);
            return;
        }
        if self
            .hidden_at
            .is_some_and(|hidden_at| hidden_at.elapsed() < REOPEN_GUARD)
        {
            return;
        }
        // A glance after a break should show the whole board, not a filter typed long ago.
        if self
            .hidden_at
            .is_some_and(|hidden_at| hidden_at.elapsed() > FILTER_RESET_AFTER)
        {
            self.query.clear();
        }
        self.reload(cx);
        let until = Instant::now() + CHANGE_HIGHLIGHT_DURATION;
        self.changed_until
            .values_mut()
            .for_each(|changed_until| *changed_until = until);
        self.place(true, cx);
        self.focus_handle.focus(window, cx);
        self.reveal_selected();
    }

    fn hide(&mut self, cx: &mut Context<Self>) {
        if !self.is_visible() {
            return;
        }
        self.pending = None;
        self.is_showing_help = false;
        self.palette = None;
        self.input = Input::None;
        self.is_following = false;
        if let Some(panel) = &self.panel {
            macos::hide_panel(panel);
        }
        self.hidden_at = Some(Instant::now());
        cx.notify();
    }

    fn show_toast(&mut self, text: impl Into<SharedString>, cx: &mut Context<Self>) {
        self.show_message(text, false, None, None, cx);
    }

    /// An error with no task to open stays until dismissed, since it's the only place to read it.
    fn show_message(
        &mut self,
        text: impl Into<SharedString>,
        is_error: bool,
        task_id: Option<String>,
        action: Option<Action>,
        cx: &mut Context<Self>,
    ) {
        let text = text.into();
        let is_holding_error = self
            .toast
            .as_ref()
            .is_some_and(|toast| toast.is_error && toast.task_id.is_none());
        if text.is_empty() || (is_holding_error && !is_error) {
            return;
        }
        self.toast_count += 1;
        let toast_id = self.toast_count;
        self.toast = Some(Toast {
            text,
            id: toast_id,
            is_error,
            task_id: task_id.clone(),
            action,
        });
        let duration = match (is_error, &task_id) {
            (true, None) => return cx.notify(),
            (true, Some(_)) => ERROR_TOAST_DURATION,
            (false, _) => TOAST_DURATION,
        };
        cx.spawn(async move |this, cx| {
            cx.background_executor().timer(duration).await;
            let _ = this.update(cx, |this, cx| {
                if this
                    .toast
                    .as_ref()
                    .is_some_and(|toast| toast.id == toast_id)
                {
                    this.toast = None;
                    cx.notify();
                }
            });
        })
        .detach();
        cx.notify();
    }

    // ---- navigation -------------------------------------------------------------------------

    fn reveal_selected(&self) {
        let rows = self.column_rows(self.tab);
        let selected = self.selected_row().map(|row| row.root_id.clone());
        if let Some(index) = rows
            .iter()
            .position(|row| Some(&row.root_id) == selected.as_ref())
        {
            self.list_scroll.scroll_to_item(index);
        }
    }

    /// Clears the view's per-task state only when the shown task changes, so an extra J at
    /// the end of the list or a repeated click keeps folds, Follow and scroll. A chain moving
    /// to its next step keeps Follow but drops the old step's paused lines and folds.
    fn reset_view_if_task_changed(&mut self) {
        let selected = self.selected_row();
        let selected_root_id = selected.map(|row| row.root_id.clone());
        let selected_task_id = selected.map(|row| row.task_id.clone());
        if selected_task_id == self.shown_task_id {
            return;
        }
        if selected_root_id != self.shown_root_id {
            self.is_following = false;
        }
        self.shown_root_id = selected_root_id;
        self.shown_task_id = selected_task_id;
        self.is_follow_paused = false;
        self.paused_lines.clear();
        self.expanded_sections.clear();
        self.expanded_folds.clear();
        self.is_activity_open = false;
        self.is_showing_earlier = false;
        if self.is_following {
            self.detail_scroll.scroll_to_bottom();
        } else {
            self.detail_scroll.set_offset(point(px(0.), px(0.)));
        }
    }

    fn open_activity(&mut self) {
        self.is_activity_open = true;
        self.is_showing_earlier = true;
    }

    /// The activity items that fold tool calls at the Messages level, for E to open them all.
    fn fold_indexes(&mut self) -> HashSet<usize> {
        let lines = self.selected_activity();
        board::activity_view(&lines, ActivityLevel::Messages)
            .iter()
            .enumerate()
            .filter(|(_, item)| matches!(item, ActivityItem::Fold(_)))
            .map(|(index, _)| index)
            .collect()
    }

    fn select(&mut self, root_id: String, cx: &mut Context<Self>) {
        self.selected_roots.insert(self.tab, root_id);
        self.input = Input::None;
        self.reset_view_if_task_changed();
        self.reveal_selected();
        cx.notify();
    }

    /// ⌃N/⌃P move the palette or list selection without leaving the text being typed.
    fn move_while_typing(&mut self, delta: isize, window: &mut Window, cx: &mut Context<Self>) {
        if self.palette.is_some() {
            let key = if delta > 0 { KEY_DOWN } else { KEY_UP };
            self.handle_palette_key(key, "", window, cx);
            return;
        }
        if self.is_sheet_open() {
            return;
        }
        let input = self.input;
        self.move_selection(delta, cx);
        self.input = input;
    }

    fn move_selection(&mut self, delta: isize, cx: &mut Context<Self>) {
        let rows = self.column_rows(self.tab);
        if rows.is_empty() {
            return;
        }
        let current = self
            .selected_row()
            .and_then(|selected| rows.iter().position(|row| row.root_id == selected.root_id));
        let last = rows.len() as isize - 1;
        let index = (current.unwrap_or(0) as isize + delta).clamp(0, last) as usize;
        let root_id = rows[index].root_id.clone();
        self.select(root_id, cx);
    }

    fn set_tab(&mut self, column: Column, cx: &mut Context<Self>) {
        if column == Column::Done {
            self.is_showing_done = true;
        }
        self.tab = column;
        self.reset_view_if_task_changed();
        self.reveal_selected();
        cx.notify();
    }

    fn move_tab(&mut self, delta: isize, cx: &mut Context<Self>) {
        let tabs = self.tabs();
        let index = tabs.iter().position(|tab| *tab == self.tab).unwrap_or(0) as isize;
        let next = (index + delta).rem_euclid(tabs.len() as isize) as usize;
        self.set_tab(tabs[next], cx);
    }

    fn set_expanded(&mut self, is_expanded: bool, cx: &mut Context<Self>) {
        if is_expanded && self.selected_row().is_none() {
            return;
        }
        self.is_expanded = is_expanded;
        if !is_expanded {
            self.is_following = false;
        }
        self.place(false, cx);
        self.reveal_selected();
        cx.notify();
    }

    /// Dismisses the toast and opens its task, or its full text when there's no task.
    fn open_toast_details(&mut self, cx: &mut Context<Self>) {
        let Some(toast) = self.toast.take() else {
            return;
        };
        match toast.task_id {
            Some(task_id) => self.open_task(&task_id, cx),
            None if toast.is_error => self.show_error_detail(ErrorDetail {
                title: toast
                    .action
                    .map(Action::failed_text)
                    .filter(|title| !title.is_empty())
                    .unwrap_or("Action failed"),
                text: toast.text,
            }),
            None => {}
        }
    }

    fn show_load_error(&mut self) {
        let Some(error) = self.load_error.clone() else {
            return;
        };
        self.show_error_detail(ErrorDetail {
            title: LOAD_ERROR_TITLE,
            text: error.into(),
        });
    }

    fn copy_error_detail(&mut self, cx: &mut Context<Self>) {
        let Some(detail) = &self.error_detail else {
            return;
        };
        cx.write_to_clipboard(ClipboardItem::new_string(detail.text.to_string()));
        self.is_error_copied = true;
        cx.notify();
    }

    fn show_error_detail(&mut self, detail: ErrorDetail) {
        self.is_error_copied = false;
        self.error_scroll.set_offset(point(px(0.), px(0.)));
        self.error_detail = Some(detail);
    }

    fn set_palette(&mut self, palette: Option<Palette>, cx: &mut Context<Self>) {
        let was_open = self.palette.is_some();
        self.palette = palette;
        self.palette_scroll.scroll_to_item(0);
        if was_open != self.palette.is_some() {
            self.place(false, cx);
        }
        cx.notify();
    }

    /// The shortcuts sheet needs the full panel height, so the panel grows while it shows.
    fn set_help(&mut self, is_showing: bool, cx: &mut Context<Self>) {
        if self.is_showing_help == is_showing {
            return;
        }
        self.is_showing_help = is_showing;
        self.place(false, cx);
        cx.notify();
    }

    /// Follow shows only the log, so revealing session info also leaves Follow.
    fn toggle_session(&mut self, cx: &mut Context<Self>) {
        self.is_showing_fields = !self.is_showing_fields || !self.is_expanded || self.is_following;
        self.is_following = false;
        if !self.is_expanded {
            self.set_expanded(true, cx);
        }
        if self.is_showing_fields {
            self.detail_scroll.scroll_to_bottom();
        }
    }

    fn toggle_done(&mut self, cx: &mut Context<Self>) {
        if !self.is_showing_done {
            self.set_tab(Column::Done, cx);
        } else {
            self.is_showing_done = false;
            if self.tab == Column::Done {
                self.set_tab(Column::Review, cx);
            }
        }
        self.place(false, cx);
        cx.notify();
    }

    fn next_attention(&mut self, cx: &mut Context<Self>) {
        let shown_columns: Vec<Column> = ATTENTION_ORDER
            .into_iter()
            .filter(|column| *column != Column::Done || self.is_showing_done)
            .collect();
        let ordered: Vec<Row> = shown_columns
            .iter()
            .flat_map(|column| {
                self.column_rows(*column)
                    .into_iter()
                    .cloned()
                    .collect::<Vec<_>>()
            })
            .collect();
        let needing: Vec<usize> = (0..ordered.len())
            .filter(|index| self.needs_you(&ordered[*index]))
            .collect();
        let Some(first) = needing.first() else {
            self.show_toast(NOTHING_NEEDS_YOU_TEXT, cx);
            return;
        };
        let selected_root = self.selected_row().map(|row| row.root_id.clone());
        let current = ordered
            .iter()
            .position(|row| Some(&row.root_id) == selected_root.as_ref());
        let after = needing
            .iter()
            .find(|index| current.is_none_or(|current| **index > current));
        let target = ordered[*after.unwrap_or(first)].clone();
        if needing.len() == 1 && current == Some(*first) {
            self.show_toast("Only task needing you", cx);
        }
        self.tab = target.column;
        self.select(target.root_id, cx);
    }

    /// A stopped task's messages mostly restate its Result, so its activity starts summarised.
    fn is_summarised(&self, row: &Row) -> bool {
        let has_result = self.snapshot.tasks[&row.task_id]
            .result
            .as_deref()
            .is_some_and(|result| !result.trim().is_empty());
        row.status != Status::Running && has_result
    }

    fn pause_follow(&mut self) {
        let can_scroll = self.detail_scroll.max_offset().y > px(0.);
        if !self.is_following || self.is_follow_paused || !can_scroll {
            return;
        }
        self.is_follow_paused = true;
        self.paused_lines = self.selected_activity();
    }

    fn scroll_by(&mut self, delta: f32) {
        if delta < 0. {
            self.pause_follow();
        }
        let handle = if self.is_expanded {
            &self.detail_scroll
        } else {
            &self.list_scroll
        };
        scroll_handle_by(handle, delta);
    }

    /// The time the board's durations count to, held at the last good read while the ledger
    /// can't be read, so stale cards don't look live.
    fn board_now(&self) -> DateTime<Local> {
        match (&self.load_error, self.loaded_at) {
            (Some(_), Some(loaded_at)) => loaded_at,
            _ => Local::now(),
        }
    }

    fn load_error_text(&self) -> String {
        let Some(loaded_at) = self.loaded_at else {
            return LOAD_ERROR_TITLE.to_string();
        };
        let seconds = (Local::now() - loaded_at).num_seconds();
        if seconds < 60 {
            return "Can't read the ledger \u{b7} last read just now".to_string();
        }
        let age = board::duration_text(seconds as f64);
        format!("Can't read the ledger \u{b7} last read {age} ago")
    }

    /// While the filter is edited, letter keys type, so bare-letter keycaps hide.
    fn is_typing(&self) -> bool {
        self.input == Input::Filter
    }

    /// Whether a task's card is inside the list's visible area, as of the last layout.
    fn is_card_on_screen(&self, task_id: &str) -> bool {
        let rows = self.column_rows(self.tab);
        let Some(index) = rows.iter().position(|row| row.task_id == task_id) else {
            return false;
        };
        let Some(card) = self.list_scroll.bounds_for_item(index) else {
            return false;
        };
        let viewport = self.list_scroll.bounds();
        let offset = self.list_scroll.offset().y;
        card.top() + offset < viewport.bottom() && card.bottom() + offset > viewport.top()
    }

    /// Whether a sheet covers the board, so board keys and navigation must not reach it.
    fn is_sheet_open(&self) -> bool {
        self.pending.is_some() || self.is_showing_help || self.error_detail.is_some()
    }

    // ---- actions ----------------------------------------------------------------------------

    fn request(&mut self, action: Action, cx: &mut Context<Self>) {
        self.input = Input::None;
        let row = self.selected_row().cloned();
        if action == Action::Watch {
            self.toggle_follow(cx);
            return;
        }
        if action.is_confirmed() {
            let task_id = if action == Action::CloseAll {
                None
            } else {
                row.map(|row| row.task_id)
            };
            self.pending = Some(Pending { action, task_id });
            self.set_help(false, cx);
            cx.notify();
            return;
        }
        if let Some(row) = row {
            self.perform(action, &row, cx);
        }
    }

    fn request_key(&mut self, key: &str, cx: &mut Context<Self>) -> bool {
        let candidates: Vec<Action> = ACTIONS
            .into_iter()
            .filter(|action| action.key() == key)
            .collect();
        let Some(first) = candidates.first().copied() else {
            return false;
        };
        let row = self.selected_row().cloned();
        let available = board::available_actions(&self.snapshot, row.as_ref());
        match candidates
            .into_iter()
            .find(|action| available.contains(action))
        {
            Some(action) => self.request(action, cx),
            None if row.is_none() => self.show_toast("No task selected", cx),
            None => self.show_toast(first.unavailable_text(), cx),
        }
        true
    }

    /// W starts following, resumes a paused follow, or stops following.
    fn toggle_follow(&mut self, cx: &mut Context<Self>) {
        let is_resuming = self.is_following && self.is_follow_paused;
        self.is_following = is_resuming || !self.is_following;
        self.is_follow_paused = false;
        if self.is_following {
            self.detail_scroll.scroll_to_bottom();
        }
        if !self.is_expanded {
            self.set_expanded(true, cx);
        }
        cx.notify();
    }

    fn answer(&mut self, is_yes: bool, cx: &mut Context<Self>) {
        let Some(pending) = self.pending.take() else {
            return;
        };
        cx.notify();
        if !is_yes {
            return;
        }
        if pending.action == Action::CloseAll {
            self.close_finished(cx);
            return;
        }
        let row = pending.task_id.and_then(|task_id| {
            self.snapshot
                .rows
                .iter()
                .find(|row| row.task_id == task_id)
                .cloned()
        });
        if let Some(row) = row {
            self.perform(pending.action, &row, cx);
        }
    }

    /// Runs `work` off the main thread, marking each `busy` task with its action until it returns.
    fn run_in_background(
        &mut self,
        busy: Vec<(String, Action)>,
        work: impl FnOnce() -> Outcome + Send + 'static,
        cx: &mut Context<Self>,
    ) {
        for (task_id, _) in &busy {
            self.action_errors.remove(task_id);
        }
        self.busy_actions.extend(busy.iter().cloned());
        let job = cx.background_executor().spawn(async move { work() });
        cx.spawn(async move |this, cx| {
            let outcome = job.await;
            let _ = this.update(cx, |this, cx| {
                for (task_id, _) in &busy {
                    this.busy_actions.remove(task_id);
                }
                match (outcome.is_error, busy.as_slice()) {
                    (true, [(task_id, action)]) => {
                        this.note_failure(task_id, *action, outcome.message, cx)
                    }
                    // The short outcome names its task once the selection has moved on.
                    (false, [(task_id, _)])
                        if this
                            .selected_row()
                            .is_none_or(|row| row.task_id != *task_id) =>
                    {
                        let row = this
                            .snapshot
                            .rows
                            .iter()
                            .find(|row| row.task_id == *task_id);
                        let name = row.map_or(task_id.as_str(), board::row_title).to_string();
                        this.show_toast(format!("{name}: {}", outcome.message), cx)
                    }
                    _ => {
                        let action = busy.first().map(|(_, action)| *action);
                        this.show_message(outcome.message, outcome.is_error, None, action, cx)
                    }
                }
                this.reload(cx);
            });
        })
        .detach();
        cx.notify();
    }

    /// Keeps a failed run on its card, and toasts it only when that card is off screen.
    fn note_failure(
        &mut self,
        task_id: &str,
        action: Action,
        message: String,
        cx: &mut Context<Self>,
    ) {
        let row = self.snapshot.rows.iter().find(|row| row.task_id == task_id);
        let name = row.map_or(task_id, board::row_title).to_string();
        if !(self.is_visible() && self.is_card_on_screen(task_id)) {
            self.show_message(
                format!("{} for {name}", action.failed_text()),
                true,
                Some(task_id.to_string()),
                Some(action),
                cx,
            );
        }
        self.action_errors
            .insert(task_id.to_string(), ActionError { action, message });
    }

    /// Shows a task's details, switching to its group.
    fn open_task(&mut self, task_id: &str, cx: &mut Context<Self>) {
        let Some(row) = self.snapshot.rows.iter().find(|row| row.task_id == task_id) else {
            return;
        };
        let (column, root_id) = (row.column, row.root_id.clone());
        if column == Column::Done {
            self.is_showing_done = true;
        }
        if !self
            .column_rows(column)
            .iter()
            .any(|row| row.root_id == root_id)
        {
            self.query.clear();
        }
        self.tab = column;
        self.select(root_id, cx);
        self.set_expanded(true, cx);
    }

    fn perform(&mut self, action: Action, row: &Row, cx: &mut Context<Self>) {
        let task_id = row.task_id.clone();
        if let Some(busy_action) = self.busy_actions.get(&task_id)
            && action.is_golem_run()
        {
            self.show_toast(busy_action.busy_text(), cx);
            return;
        }
        match action {
            Action::Watch => self.toggle_follow(cx),
            Action::Copy => {
                cx.write_to_clipboard(ClipboardItem::new_string(task_id.clone()));
                self.show_toast(format!("Copied {task_id}"), cx);
            }
            Action::OpenPullRequest => {
                let mut pull_requests = board::row_pull_requests(&self.snapshot, row);
                pull_requests.sort_by_key(|pr| pr.state != "open");
                let presses = self.pull_request_presses.entry(task_id).or_insert(0);
                let press = *presses % pull_requests.len().max(1);
                *presses += 1;
                let Some(pull_request) = pull_requests.get(press).cloned() else {
                    return;
                };
                let mut opened = format!("Opened PR {}", board::pull_request_number(&pull_request));
                if pull_requests.len() > 1 {
                    opened += &format!(" ({} of {})", press + 1, pull_requests.len());
                }
                self.run_in_background(
                    Vec::new(),
                    move || outcome_or(golem_cli::open_url(&pull_request.url), opened),
                    cx,
                );
            }
            _ => {
                let Some(golem) = self.golem.clone() else {
                    self.show_toast(GOLEM_MISSING_TEXT, cx);
                    return;
                };
                let backend = self.snapshot.tasks[&row.task_id].backend.clone();
                let is_terminal_pickup =
                    action == Action::Pickup && TERMINAL_BACKENDS.contains(&backend.as_str());
                let done_text = match action {
                    Action::Pickup => "Picked up",
                    Action::Retry => "Retrying",
                    Action::RerunGate => "Gate passed",
                    Action::Close => "Closed",
                    Action::Cancel => "Cancelled",
                    Action::Dismiss => "Dismissed",
                    _ => "",
                };
                let busy = vec![(task_id.clone(), action)];
                self.run_in_background(
                    busy,
                    move || {
                        let outcome = if is_terminal_pickup {
                            golem.pickup_in_terminal(&task_id)
                        } else {
                            golem.run(action.command(), &task_id)
                        };
                        outcome_or(outcome, done_text.to_string())
                    },
                    cx,
                );
            }
        }
    }

    fn close_finished(&mut self, cx: &mut Context<Self>) {
        let Some(golem) = self.golem.clone() else {
            self.show_toast(GOLEM_MISSING_TEXT, cx);
            return;
        };
        let task_ids = board::closable_task_ids(&self.snapshot);
        let busy = task_ids
            .iter()
            .map(|task_id| (task_id.clone(), Action::Close))
            .collect();
        self.run_in_background(
            busy,
            move || {
                let failures: Vec<String> = task_ids
                    .iter()
                    .map(|task_id| golem.run(Action::Close.command(), task_id))
                    .filter(|outcome| !outcome.is_success)
                    .map(|outcome| outcome.message)
                    .collect();
                match failures.first() {
                    Some(failure) => Outcome {
                        message: format!(
                            "Couldn't close {}: {failure}",
                            board::counted_text(failures.len(), "task")
                        ),
                        is_error: true,
                    },
                    None => Outcome {
                        message: format!("Closed {}", board::counted_text(task_ids.len(), "task")),
                        is_error: false,
                    },
                }
            },
            cx,
        );
    }

    /// The selected task's Session field with this label, when it has one.
    fn selected_field(&self, label: &str) -> Option<String> {
        let row = self.selected_row()?;
        board::task_fields(row, &self.snapshot, self.board_now())
            .into_iter()
            .find(|(field_label, value)| *field_label == label && !value.is_empty())
            .map(|(_, value)| value)
    }

    fn palette_items(&self) -> Vec<PaletteItem> {
        let mut items = Vec::new();
        if let Some(row) = self.selected_row() {
            for action in board::available_actions(&self.snapshot, Some(row)) {
                if action == Action::CloseAll {
                    continue;
                }
                let label = match action {
                    Action::Watch if row.status != Status::Running => "Show log".to_string(),
                    Action::Pickup => "Pick up".to_string(),
                    Action::Watch => "Follow live log".to_string(),
                    _ => action.label().to_string(),
                };
                items.push(PaletteItem {
                    group: board::row_title(row).to_string(),
                    command: Command::Task(action),
                    label,
                    key: key_label(action),
                    is_danger: action == Action::Cancel,
                });
            }
            let details_label = if self.is_expanded {
                "Hide details"
            } else {
                "Show details"
            };
            let session_label = if self.is_showing_fields && self.is_expanded {
                "Hide session info"
            } else {
                "Show session info"
            };
            let details_key = if self.is_expanded { "esc" } else { "\u{23ce}" };
            let copy_items = COPYABLE_FIELDS
                .into_iter()
                .filter(|(field, _)| self.selected_field(field).is_some())
                .map(|(field, text)| (Command::CopyField(field), text.to_string(), ""));
            let view_items = [
                (
                    Command::ToggleDetails,
                    details_label.to_string(),
                    details_key,
                ),
                (Command::ToggleSession, session_label.to_string(), "I"),
            ];
            for (command, label, key) in view_items.into_iter().chain(copy_items) {
                items.push(PaletteItem {
                    group: board::row_title(row).to_string(),
                    command,
                    label,
                    key: key.to_string(),
                    is_danger: false,
                });
            }
        }
        let has_closable = !board::closable_task_ids(&self.snapshot).is_empty();
        let board_items = [
            Some((Command::NextAttention, "Next needing you".to_string(), "N")),
            has_closable.then(|| {
                (
                    Command::Task(Action::CloseAll),
                    "Close finished".to_string(),
                    "\u{21e7}C",
                )
            }),
            Some((
                Command::ToggleDone,
                if self.is_showing_done {
                    "Hide Done"
                } else {
                    "Show Done"
                }
                .to_string(),
                "A",
            )),
            Some((Command::Quit, "Quit golem board".to_string(), "\u{2318}Q")),
        ];
        for (command, label, key) in board_items.into_iter().flatten() {
            items.push(PaletteItem {
                group: "Board".to_string(),
                command,
                label,
                key: key.to_string(),
                is_danger: false,
            });
        }
        let query = self
            .palette
            .as_ref()
            .map(|palette| palette.query.to_lowercase())
            .unwrap_or_default();
        items.retain(|item| query.is_empty() || item.label.to_lowercase().contains(&query));
        items
    }

    fn run_command(&mut self, command: Command, window: &mut Window, cx: &mut Context<Self>) {
        self.set_palette(None, cx);
        match command {
            Command::Task(action) => self.request(action, cx),
            Command::NextAttention => self.next_attention(cx),
            Command::ToggleDone => self.toggle_done(cx),
            Command::ToggleDetails => self.set_expanded(!self.is_expanded, cx),
            Command::ToggleSession => self.toggle_session(cx),
            Command::CopyField(label) => {
                if let Some(value) = self.selected_field(label) {
                    cx.write_to_clipboard(ClipboardItem::new_string(value));
                    self.show_toast("Copied", cx);
                }
            }
            Command::Quit => cx.quit(),
        }
        self.focus_handle.focus(window, cx);
        cx.notify();
    }

    // ---- keyboard ---------------------------------------------------------------------------

    fn handle_key(&mut self, event: &KeyDownEvent, window: &mut Window, cx: &mut Context<Self>) {
        let keystroke = &event.keystroke;
        let modifiers = keystroke.modifiers;
        let key = keystroke.key.as_str();
        if modifiers.platform {
            match key {
                "k" if self.pending.is_none() && self.error_detail.is_none() => {
                    let palette = if self.palette.is_some() {
                        None
                    } else {
                        Some(Palette::default())
                    };
                    self.set_palette(palette, cx);
                    self.set_help(false, cx);
                }
                "c" if self.error_detail.is_some() => self.copy_error_detail(cx),
                "w" => self.hide(cx),
                "q" => cx.quit(),
                "v" => self.paste(cx),
                KEY_BACKSPACE => self.clear_input(),
                "left" | "right" => {
                    let edge = if key == "left" { "home" } else { "end" };
                    match self.palette.as_mut() {
                        Some(palette) => {
                            edit_input(&mut palette.query, &mut palette.caret, edge, "");
                        }
                        None if self.input == Input::Filter => {
                            edit_input(&mut self.query, &mut self.query_caret, edge, "");
                        }
                        None => return,
                    }
                }
                _ => return,
            }
            cx.notify();
            return;
        }
        if modifiers.control {
            match key {
                "d" | "u" if self.error_detail.is_some() => {
                    let delta = if key == "d" {
                        ERROR_PAGE_STEP
                    } else {
                        -ERROR_PAGE_STEP
                    };
                    scroll_handle_by(&self.error_scroll, delta);
                }
                "d" | "u" if self.palette.is_some() || self.is_sheet_open() => return,
                "d" => self.scroll_by(SCROLL_STEP),
                "u" => self.scroll_by(-SCROLL_STEP),
                "n" | "j" => self.move_while_typing(1, window, cx),
                "p" | "k" => self.move_while_typing(-1, window, cx),
                _ => return,
            }
            cx.notify();
            return;
        }
        if modifiers.alt {
            return;
        }
        let typed = keystroke.key_char.clone().unwrap_or_else(|| {
            let is_single_char = key.chars().count() == 1;
            match (is_single_char, modifiers.shift) {
                (true, true) => key.to_uppercase(),
                (true, false) => key.to_string(),
                (false, _) if key == "space" => " ".to_string(),
                (false, _) => String::new(),
            }
        });
        if self.palette.is_some() {
            self.handle_palette_key(key, &typed, window, cx);
        } else if let Some(action) = self.pending.as_ref().map(|pending| pending.action) {
            // Return opens and closes details all day, so it never confirms losing work.
            let is_destructive = action.is_destructive();
            match (key, typed.as_str()) {
                (KEY_ESCAPE, _) | (_, "n") => self.answer(false, cx),
                (_, typed) if is_destructive && !event.is_held && typed == action.key() => {
                    self.answer(true, cx)
                }
                (KEY_ENTER, _) | (_, "y") if !is_destructive => self.answer(true, cx),
                _ => {}
            }
        } else if self.is_showing_help {
            if matches!(key, KEY_ESCAPE | KEY_ENTER) || matches!(typed.as_str(), "?" | "q") {
                self.set_help(false, cx);
            }
        } else if self.error_detail.is_some() {
            match (key, typed.as_str()) {
                (KEY_ESCAPE | KEY_ENTER, _) => self.error_detail = None,
                (KEY_DOWN, _) | (_, "j") => scroll_handle_by(&self.error_scroll, ERROR_LINE_STEP),
                (KEY_UP, _) | (_, "k") => scroll_handle_by(&self.error_scroll, -ERROR_LINE_STEP),
                ("pagedown" | "space", _) => {
                    scroll_handle_by(&self.error_scroll, ERROR_PAGE_STEP);
                }
                ("pageup", _) => scroll_handle_by(&self.error_scroll, -ERROR_PAGE_STEP),
                _ => {}
            }
        } else if self.input == Input::Filter {
            self.handle_filter_key(key, &typed, cx);
        } else if key == "tab" && modifiers.shift {
            self.move_tab(-1, cx);
        } else {
            self.handle_board_key(key, &typed, cx);
        }
        cx.notify();
    }

    fn handle_palette_key(
        &mut self,
        key: &str,
        typed: &str,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let items = self.palette_items();
        let Some(palette) = self.palette.as_mut() else {
            return;
        };
        match key {
            KEY_ESCAPE => self.set_palette(None, cx),
            KEY_DOWN | KEY_UP if !items.is_empty() => {
                let step = if key == KEY_DOWN { 1 } else { items.len() - 1 };
                palette.index = (palette.index + step) % items.len();
                self.palette_scroll
                    .scroll_to_item(palette_child_index(&items, palette.index));
            }
            KEY_ENTER => {
                if let Some(item) = items.get(palette.index.min(items.len().saturating_sub(1))) {
                    self.run_command(item.command, window, cx);
                }
            }
            _ => {
                let query_size = palette.query.len();
                edit_input(&mut palette.query, &mut palette.caret, key, typed);
                if palette.query.len() != query_size {
                    palette.index = 0;
                    self.palette_scroll.scroll_to_item(0);
                }
            }
        }
    }

    fn handle_filter_key(&mut self, key: &str, typed: &str, cx: &mut Context<Self>) {
        match key {
            KEY_ESCAPE => {
                self.query.clear();
                self.input = Input::None;
            }
            // Leaving the filter also does what the key does, so one press moves or opens.
            KEY_ENTER | KEY_DOWN | KEY_UP => {
                self.input = Input::None;
                self.show_matching_tab();
                self.handle_board_key(key, typed, cx);
                return;
            }
            _ if edit_input(&mut self.query, &mut self.query_caret, key, typed) => {}
            _ => return,
        }
        self.show_matching_tab();
        cx.notify();
    }

    /// While filtering, switches to the first group with matches when the current one has none.
    fn show_matching_tab(&mut self) {
        if self.query.is_empty() || !self.column_rows(self.tab).is_empty() {
            return;
        }
        let tabs = self.tabs();
        if let Some(column) = tabs
            .into_iter()
            .find(|column| !self.column_rows(*column).is_empty())
        {
            self.tab = column;
        }
    }

    fn clear_input(&mut self) {
        if let Some(palette) = self.palette.as_mut() {
            palette.query.clear();
            palette.index = 0;
        } else if self.input == Input::Filter {
            self.query.clear();
        }
    }

    /// Pastes the clipboard's text, on one line, into the palette search or the filter.
    fn paste(&mut self, cx: &mut Context<Self>) {
        let Some(text) = cx.read_from_clipboard().and_then(|item| item.text()) else {
            return;
        };
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        if let Some(palette) = self.palette.as_mut() {
            edit_input(&mut palette.query, &mut palette.caret, "", &text);
            palette.index = 0;
            return;
        }
        if self.is_sheet_open() {
            return;
        }
        self.input = Input::Filter;
        edit_input(&mut self.query, &mut self.query_caret, "", &text);
        self.show_matching_tab();
    }

    fn handle_board_key(&mut self, key: &str, typed: &str, cx: &mut Context<Self>) {
        match (key, typed) {
            (KEY_ESCAPE, _) => {
                if self.toast.as_ref().is_some_and(|toast| toast.is_error) {
                    self.toast = None;
                } else if self.is_following {
                    self.is_following = false;
                } else if self.is_expanded {
                    self.set_expanded(false, cx);
                } else if !self.query.is_empty() {
                    self.query.clear();
                } else {
                    self.hide(cx);
                }
            }
            (KEY_DOWN, _) | (_, "j") => self.move_selection(1, cx),
            (KEY_UP, _) | (_, "k") => self.move_selection(-1, cx),
            ("right" | "tab", _) | (_, "l") => self.move_tab(1, cx),
            ("left", _) | (_, "h") => self.move_tab(-1, cx),
            ("home", _) | (_, "g") => self.move_selection(isize::MIN / 2, cx),
            ("end", _) | (_, "G") => self.move_selection(isize::MAX / 2, cx),
            ("space" | "pagedown", _) => self.scroll_by(SCROLL_STEP),
            ("pageup", _) => self.scroll_by(-SCROLL_STEP),
            (KEY_ENTER, _) => self.set_expanded(!self.is_expanded, cx),
            (_, "1" | "2" | "3" | "4") => {
                let index = typed.parse::<usize>().unwrap_or(1) - 1;
                self.set_tab(BOARD_COLUMNS[index], cx);
            }
            (_, "/") => self.input = Input::Filter,
            (_, "n") => self.next_attention(cx),
            (_, "a") => self.toggle_done(cx),
            (_, "?") => self.set_help(true, cx),
            (_, "e") => {
                let fold_indexes = self.fold_indexes();
                let is_expanding = self.is_following
                    || self.expanded_sections.len() < EXPANDABLE_SECTIONS.len()
                    || self.expanded_folds != fold_indexes;
                (self.expanded_sections, self.expanded_folds) = if is_expanding {
                    (EXPANDABLE_SECTIONS.into_iter().collect(), fold_indexes)
                } else {
                    (HashSet::new(), HashSet::new())
                };
                self.is_activity_open = is_expanding;
                self.is_showing_earlier = is_expanding;
                self.is_following = false;
                if !self.is_expanded {
                    self.set_expanded(true, cx);
                }
            }
            (_, "i") => self.toggle_session(cx),
            (_, "d")
                if self
                    .toast
                    .as_ref()
                    .is_some_and(|toast| toast.task_id.is_some() || toast.is_error) =>
            {
                self.open_toast_details(cx);
            }
            (_, "d") if self.load_error.is_some() => self.show_load_error(),
            (_, "d")
                if self
                    .selected_row()
                    .is_some_and(|row| self.action_errors.contains_key(&row.task_id)) =>
            {
                self.set_expanded(true, cx)
            }
            (_, "q") => self.hide(cx),
            _ => {
                self.request_key(typed, cx);
            }
        }
    }
}

/// A failed run reports golem's own message; a successful one says `done_text` when there is one.
fn outcome_or(outcome: golem_cli::CommandOutcome, done_text: String) -> Outcome {
    match (outcome.is_success, done_text.is_empty()) {
        (false, _) => Outcome {
            message: outcome.message,
            is_error: true,
        },
        (true, true) => Outcome {
            message: outcome.message,
            is_error: false,
        },
        (true, false) => Outcome {
            message: done_text,
            is_error: false,
        },
    }
}

/// The lines that arrived since Follow paused, or None once the paused tail has left the
/// bounded history and the new lines can't be told apart.
fn lines_since_pause<'a>(
    paused_lines: &[String],
    current_lines: &'a [String],
) -> Option<&'a [String]> {
    let Some(paused_tail) = paused_lines.last() else {
        return Some(current_lines);
    };
    let index = current_lines.iter().rposition(|line| line == paused_tail)?;
    Some(&current_lines[index + 1..])
}

/// golem and agents mark commands and paths with backticks; show those in the code font instead.
fn code_marked_text(text: &str) -> StyledText {
    if text.matches('`').count() % 2 == 1 {
        return StyledText::new(text.to_string());
    }
    let mut plain = String::new();
    let mut overrides = Vec::new();
    for (index, part) in text.split('`').enumerate() {
        let start = plain.len();
        plain.push_str(part);
        if index % 2 == 1 && !part.is_empty() {
            overrides.push((start..plain.len(), SharedString::from(MONO_FONT)));
        }
    }
    // Font overrides only apply to whole runs, so empty highlights split the code spans out.
    let highlights = overrides
        .iter()
        .map(|(range, _)| (range.clone(), HighlightStyle::default()));
    StyledText::new(plain)
        .with_highlights(highlights)
        .with_font_family_overrides(overrides)
}

fn tool_line(text: String, theme: &Theme) -> Div {
    div()
        .py(px(2.))
        .pl(px(12.))
        .font_family(MONO_FONT)
        .text_size(px(12.))
        .text_color(theme.text_2)
        .child(text)
}

/// A muted, clickable stand-in for activity that is not shown.
fn fold_row(id: impl Into<ElementId>, theme: &Theme) -> Stateful<Div> {
    div()
        .id(id)
        .flex()
        .gap(px(5.))
        .py(px(2.))
        .px(px(4.))
        .ml(px(8.))
        .rounded(px(5.))
        .text_size(px(12.))
        .text_color(theme.text_3)
        .hover(|style| style.bg(theme.hover))
}

fn scroll_handle_by(handle: &ScrollHandle, delta: f32) {
    let offset = handle.offset();
    let max_offset = handle.max_offset();
    let y = (offset.y - px(delta)).clamp(-max_offset.y, px(0.));
    handle.set_offset(point(offset.x, y));
}

/// The palette list's child position for an item, counting the group headings rendered before it.
fn palette_child_index(items: &[PaletteItem], index: usize) -> usize {
    index + palette_heading_count(&items[..=index])
}

/// The palette list's height at most `available`, cut after a whole row so none shows half.
fn palette_list_height(items: &[PaletteItem], available: f32) -> f32 {
    let mut height = PALETTE_LIST_PADDING * 2.;
    for (index, item) in items.iter().enumerate() {
        let is_new_group = index == 0 || items[index - 1].group != item.group;
        let next = height
            + PALETTE_ROW_HEIGHT
            + if is_new_group {
                PALETTE_HEADING_HEIGHT
            } else {
                0.
            };
        if next > available {
            break;
        }
        height = next;
    }
    height
}

/// How many group headings the palette renders above and among these items.
fn palette_heading_count(items: &[PaletteItem]) -> usize {
    items
        .iter()
        .enumerate()
        .filter(|(index, item)| *index == 0 || items[index - 1].group != item.group)
        .count()
}

/// The "owner/repo" part of a GitHub pull request URL.
fn pull_request_repository(url: &str) -> String {
    let parts: Vec<&str> = url.trim_start_matches("https://").split('/').collect();
    match parts.as_slice() {
        [_, owner, repository, ..] => format!("{owner}/{repository}"),
        _ => url.to_string(),
    }
}

/// Whether text is long enough to clamp to `line_limit` lines in the details pane.
fn sentence_case(text: &str) -> String {
    let mut chars = text.chars();
    chars.next().map_or_else(String::new, |first| {
        first.to_uppercase().chain(chars).collect()
    })
}

fn is_printable(typed: &str) -> bool {
    !typed.is_empty() && typed.chars().all(|char| !char.is_control())
}

/// Edits a hand-rolled input at its caret, which counts bytes from the end so clearing the
/// text elsewhere leaves it valid. Returns whether the key edited or moved anything.
fn edit_input(text: &mut String, caret: &mut usize, key: &str, typed: &str) -> bool {
    *caret = (*caret).min(text.len());
    let index = text.len() - *caret;
    let previous = text[..index]
        .char_indices()
        .next_back()
        .map(|(start, _)| start);
    let next_size = text[index..].chars().next().map(char::len_utf8);
    match key {
        "left" => *caret = text.len() - previous.unwrap_or(index),
        "right" => *caret -= next_size.unwrap_or(0),
        "home" => *caret = text.len(),
        "end" => *caret = 0,
        KEY_BACKSPACE => {
            if let Some(start) = previous {
                text.remove(start);
            }
        }
        "delete" => {
            if next_size.is_some() {
                text.remove(index);
                *caret -= next_size.unwrap_or(0);
            }
        }
        _ if is_printable(typed) => text.insert_str(index, typed),
        _ => return false,
    }
    true
}

// ---- rendering ------------------------------------------------------------------------------

impl BoardView {
    /// Text being typed with its caret, scrolled just enough to keep the caret in view; a
    /// click moves the caret to the nearest character.
    fn editing_text(
        &self,
        id: &'static str,
        text: &str,
        caret: usize,
        font_size: f32,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let caret_height = font_size + 2.;
        let (before, after) = text.split_at(text.len() - caret.min(text.len()));
        self.input_scroll.scroll_to_item(1);
        let run = |text: &str| {
            div()
                .flex_none()
                .whitespace_nowrap()
                .child(text.to_string())
        };
        div()
            .id(id)
            .min_w_0()
            .flex()
            .items_center()
            .overflow_x_scroll()
            .track_scroll(&self.input_scroll)
            .on_mouse_down(
                MouseButton::Left,
                cx.listener(move |this, event: &MouseDownEvent, window, cx| {
                    this.place_caret(event.position.x, px(font_size), window);
                    cx.notify();
                }),
            )
            .child(run(before))
            .child(
                div()
                    .flex_none()
                    .w(px(1.5))
                    .h(px(caret_height))
                    .bg(theme.accent),
            )
            .child(run(after))
    }

    /// Moves the active input's caret to the character nearest `click_x`.
    fn place_caret(&mut self, click_x: Pixels, font_size: Pixels, window: &mut Window) {
        let text = match &self.palette {
            Some(palette) => palette.query.clone(),
            None => self.query.clone(),
        };
        let x = click_x - self.input_scroll.bounds().left() - self.input_scroll.offset().x;
        let run = window.text_style().to_run(text.len());
        let line = window
            .text_system()
            .shape_line(text.clone().into(), font_size, &[run], None);
        let caret = text.len() - line.closest_index_for_x(x).min(text.len());
        match self.palette.as_mut() {
            Some(palette) => palette.caret = caret,
            None => self.query_caret = caret,
        }
    }

    fn render_header(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let is_filtering = self.input == Input::Filter;
        let filter_text: AnyElement = if self.query.is_empty() && !is_filtering {
            div()
                .text_color(theme.text_3)
                .child("Filter")
                .into_any_element()
        } else if is_filtering {
            self.editing_text("query", &self.query, self.query_caret, 13., theme, cx)
                .into_any_element()
        } else {
            div()
                .min_w_0()
                .truncate()
                .child(self.query.clone())
                .into_any_element()
        };
        let needing_count = self
            .tabs()
            .into_iter()
            .flat_map(|column| self.column_rows(column))
            .filter(|row| self.needs_you(row))
            .count();
        div()
            .flex()
            .flex_col()
            .gap(px(6.))
            .px(px(10.))
            .pt(px(9.))
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(7.))
                    .child(
                        div()
                            .id("filter")
                            .flex_1()
                            .min_w_0()
                            .flex()
                            .items_center()
                            .gap(px(6.))
                            .h(px(28.))
                            .pl(px(9.))
                            .pr(px(7.))
                            .rounded(px(8.))
                            .bg(theme.fill)
                            .when(is_filtering, |this| {
                                this.border_1().border_color(theme.accent)
                            })
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                if this.input != Input::Filter {
                                    this.query_caret = 0;
                                }
                                this.input = Input::Filter;
                                cx.notify();
                            }))
                            .child(icon(theme::ICON_SEARCH, 13., theme.text_2))
                            .child(div().flex_1().min_w_0().child(filter_text))
                            .map(|this| {
                                if self.query.is_empty() {
                                    return this;
                                }
                                this.child(
                                    div()
                                        .id("clear-filter")
                                        .size(px(16.))
                                        .flex()
                                        .items_center()
                                        .justify_center()
                                        .rounded_full()
                                        .bg(theme.text_3)
                                        .text_color(theme.panel)
                                        .text_size(px(11.))
                                        .font_weight(FontWeight::BOLD)
                                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                            cx.stop_propagation();
                                            this.query.clear();
                                            this.input = Input::None;
                                            cx.notify();
                                        }))
                                        .child("\u{d7}"),
                                )
                            }),
                    )
                    .child(
                        div()
                            .id("next")
                            .flex()
                            .items_center()
                            .gap(px(6.))
                            .h(px(28.))
                            .pl(px(10.))
                            .pr(px(7.))
                            .rounded(px(8.))
                            .font_weight(FontWeight::MEDIUM)
                            .map(|this| {
                                if needing_count == 0 {
                                    return this.bg(theme.fill).text_color(theme.text_3);
                                }
                                this.bg(theme.fill)
                                    .text_color(theme.text)
                                    .hover(|style| style.bg(theme.hover))
                            })
                            .on_click(
                                cx.listener(|this, _: &ClickEvent, _, cx| this.next_attention(cx)),
                            )
                            .child("Next")
                            .when(needing_count > 0, |this| {
                                this.child(
                                    div()
                                        .font_weight(FontWeight::BOLD)
                                        .text_color(theme.accent)
                                        .child(needing_count.to_string()),
                                )
                            }),
                    ),
            )
            .when(self.load_error.is_some(), |this| {
                this.child(
                    div()
                        .flex()
                        .items_center()
                        .gap(px(6.))
                        .text_size(px(11.5))
                        .child(
                            div()
                                .flex_1()
                                .min_w_0()
                                .truncate()
                                .text_color(theme.orange)
                                .child(self.load_error_text()),
                        )
                        .child(
                            div()
                                .id("load-error-details")
                                .flex()
                                .items_center()
                                .gap(px(5.))
                                .text_color(theme.accent)
                                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                    this.show_load_error();
                                    cx.notify();
                                }))
                                .child("Details")
                                .child(kbd("D", theme).when(self.is_typing(), Div::invisible)),
                        ),
                )
            })
            .into_any_element()
    }

    fn render_tabs(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let tabs = self.tabs().into_iter().map(|column| {
            let rows = self.column_rows(column);
            let is_current = column == self.tab;
            let has_news = rows
                .iter()
                .any(|row| self.changed_until.contains_key(&row.root_id));
            div()
                .id(column.title())
                .flex_1()
                .min_w_0()
                .flex()
                .justify_center()
                .items_center()
                .gap(px(5.))
                .h(px(24.))
                .rounded(px(6.))
                .text_color(if is_current { theme.text } else { theme.text_2 })
                .font_weight(if is_current {
                    FontWeight::SEMIBOLD
                } else {
                    FontWeight::MEDIUM
                })
                .when(is_current, |this| {
                    this.bg(theme.raised).shadow(vec![BoxShadow {
                        color: theme.segment_shadow,
                        offset: point(px(0.), px(1.)),
                        blur_radius: px(3.),
                        spread_radius: px(0.),
                        inset: false,
                    }])
                })
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                    this.input = Input::None;
                    this.set_tab(column, cx)
                }))
                .child(column.title())
                .child(
                    div()
                        .text_size(px(11.5))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.text_3)
                        .child(rows.len().to_string()),
                )
                .when(has_news && !is_current, |this| {
                    this.child(div().size(px(6.)).rounded_full().bg(theme.accent))
                })
        });
        let container = div().flex().flex_col().child(
            div()
                .flex()
                .gap(px(1.))
                .mx(px(10.))
                .mt(px(13.))
                .mb(px(7.))
                .p(px(2.))
                .rounded(px(8.))
                .bg(theme.fill)
                .children(tabs),
        );
        container.into_any_element()
    }

    fn render_card(
        &self,
        row: &Row,
        is_selected: bool,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let now = self.board_now();
        let task = &self.snapshot.tasks[&row.task_id];
        let is_stuck = board::is_stuck(row, task, now);
        let busy_action = self.busy_actions.get(&row.task_id).copied();
        let action_error = self.action_errors.get(&row.task_id);
        let is_changed = self.changed_until.contains_key(&row.root_id);
        // A finished task already picked up no longer needs you, so it steps back.
        let is_handled =
            row.column == Column::Review && row.status == Status::Finished && !self.needs_you(row);
        let state_color = match busy_action {
            Some(_) => theme.text_3,
            None if is_handled => theme.text_3,
            None => theme.status_icon_color(row.status, is_stuck),
        };
        let when = board::card_when_text(row, task, now);
        let (snippet, snippet_color) = match (busy_action, action_error) {
            (Some(action), _) => (action.busy_text().to_string(), theme.text_2),
            (None, Some(error)) => (error.action.failed_text().to_string(), theme.red),
            (None, None) if is_handled => (board::card_snippet(row, &self.snapshot), theme.text_3),
            (None, None) => (board::card_snippet(row, &self.snapshot), theme.text_2),
        };
        let is_code = snippet.starts_with(board::SHELL_PROMPT)
            || (busy_action.is_none()
                && action_error.is_none()
                && board::is_tool_snippet(row, &self.snapshot));
        let is_covered = self.palette.is_some() || self.is_sheet_open();
        let quick_action =
            (!self.is_expanded && !is_covered && busy_action.is_none() && self.tab != Column::Done)
                .then(|| {
                    let actions = board::available_actions(&self.snapshot, Some(row));
                    board::primary_action(&self.snapshot, row, &actions)
                })
                .flatten()
                .filter(|action| *action != Action::Watch);
        let root_id = row.root_id.clone();
        let card_id = SharedString::from(format!("card-{}", row.root_id));
        // Unselected rows reveal their action on hover, over the snippet, so no row loses width.
        let hover_action = quick_action.filter(|_| !is_selected).map(|action| {
            let backing = theme.panel.blend(if is_changed {
                theme.changed
            } else {
                theme.hover
            });
            div()
                .absolute()
                .top_0()
                .bottom_0()
                .right_0()
                .flex()
                .items_center()
                .invisible()
                .group_hover(card_id.clone(), |style| style.visible())
                .child(div().w(px(24.)).h_full().bg(linear_gradient(
                    90.,
                    linear_color_stop(Hsla { a: 0., ..backing }, 0.),
                    linear_color_stop(backing, 1.),
                )))
                .child(div().h_full().flex().items_center().bg(backing).child(
                    Self::quick_action_button(action, &row.root_id, false, false, theme, cx),
                ))
        });
        let quick_action = quick_action.filter(|_| is_selected).map(|action| {
            Self::quick_action_button(action, &row.root_id, true, self.is_typing(), theme, cx)
        });
        div()
            .id(card_id.clone())
            .group(card_id)
            .flex()
            .flex_col()
            .gap(px(2.))
            .pl(px(10.))
            .pr(px(11.))
            .py(px(8.))
            .rounded(px(8.))
            .border_1()
            .map(|this| match (is_selected, is_changed) {
                (true, _) => this
                    .bg(theme.selection)
                    .border_color(gpui::transparent_black()),
                (false, true) => this
                    .bg(theme.changed)
                    .border_color(gpui::transparent_black()),
                (false, false) => this
                    .border_color(gpui::transparent_black())
                    .hover(|style| style.bg(theme.hover)),
            })
            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                this.select(root_id.clone(), cx);
                this.set_expanded(true, cx);
            }))
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(7.))
                    .min_w_0()
                    .child(match busy_action {
                        Some(_) => icon(theme::status_icon(row.status), 14., state_color)
                            .with_animation(
                                SharedString::from(format!("busy-{}", row.task_id)),
                                busy_pulse(),
                                |icon, opacity| icon.opacity(opacity),
                            )
                            .into_any_element(),
                        None => icon(theme::status_icon(row.status), 14., state_color)
                            .into_any_element(),
                    })
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_size(px(13.5))
                            .font_weight(FontWeight::MEDIUM)
                            .when(is_handled, |this| this.text_color(theme.text_2))
                            .child(board::row_title(row).to_string()),
                    )
                    .child(
                        div()
                            .flex_none()
                            .text_size(px(11.5))
                            .font_weight(if is_stuck {
                                FontWeight::SEMIBOLD
                            } else {
                                FontWeight::NORMAL
                            })
                            .text_color(if is_stuck { theme.orange } else { theme.text_3 })
                            .child(when),
                    ),
            )
            .child(
                div()
                    .relative()
                    .flex()
                    .items_center()
                    .gap(px(6.))
                    .pl(px(21.))
                    .min_w_0()
                    .text_size(px(12.))
                    .child(
                        div()
                            .flex_none()
                            .max_w(px(REPOSITORY_MAX_WIDTH))
                            .truncate()
                            .text_color(theme.text_3)
                            .child(board::repository_name(row).to_string()),
                    )
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_color(snippet_color)
                            .when(is_code, |this| {
                                this.font_family(MONO_FONT).text_size(px(11.5))
                            })
                            .child(snippet),
                    )
                    .children(quick_action)
                    .children(hover_action),
            )
    }

    /// A card's primary action in the collapsed panel, so it runs without opening details.
    /// Only the selected card's button shows its key, since keys act on the selection.
    fn quick_action_button(
        action: Action,
        root_id: &str,
        has_key: bool,
        is_key_hidden: bool,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let root_id = root_id.to_string();
        div()
            .id(SharedString::from(format!("quick-action-{root_id}")))
            .flex_none()
            .flex()
            .items_center()
            .gap(px(5.))
            .h(px(20.))
            .my(px(-2.))
            .map(|this| match (has_key, is_key_hidden) {
                (true, false) => this.pl(px(8.)).pr(px(3.)),
                (true, true) => this.px(px((8. + 5. + KEYCAP_WIDTH + 3.) / 2.)),
                (false, _) => this.px(px(8.)),
            })
            .rounded(px(5.))
            .bg(theme.accent_fill)
            .text_color(theme.on_accent)
            .text_size(px(11.5))
            .font_weight(FontWeight::SEMIBOLD)
            .hover(|style| style.opacity(0.9))
            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                cx.stop_propagation();
                this.select(root_id.clone(), cx);
                this.request(action, cx);
            }))
            .child(action.label())
            .when(has_key && !is_key_hidden, |this| {
                this.child(
                    kbd(&key_label(action), theme)
                        .h(px(14.))
                        .bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                        .text_color(theme.on_accent),
                )
            })
            .into_any_element()
    }

    fn render_list(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let rows: Vec<Row> = self.column_rows(self.tab).into_iter().cloned().collect();
        let selected_root = self.selected_row().map(|row| row.root_id.clone());
        let list = div()
            .id("list")
            .flex_1()
            .min_h_0()
            .overflow_y_scroll()
            .track_scroll(&self.list_scroll)
            .px(px(10.))
            .pb(px(10.))
            .flex()
            .flex_col()
            .gap(px(CARD_GAP));
        if rows.is_empty() {
            let text = if self.loaded_at.is_none() && self.load_error.is_some() {
                "Tasks unavailable"
            } else if self.query.is_empty() {
                self.tab.empty_text()
            } else {
                "No matches \u{b7} esc clears"
            };
            return list
                .child(
                    div()
                        .py(px(34.))
                        .flex()
                        .justify_center()
                        .text_color(theme.text_3)
                        .child(text),
                )
                .into_any_element();
        }
        let cards: Vec<Stateful<Div>> = rows
            .iter()
            .map(|row| {
                self.render_card(row, Some(&row.root_id) == selected_root.as_ref(), theme, cx)
            })
            .collect();
        list.children(cards).into_any_element()
    }

    fn section(title: &str, theme: &Theme) -> Div {
        div().flex().flex_col().gap(px(3.)).mb(px(12.)).child(
            div()
                .text_size(px(11.))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(theme.text_2)
                .child(title.to_uppercase()),
        )
    }

    fn render_detail_body(
        &mut self,
        row: &Row,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Vec<AnyElement> {
        let now = self.board_now();
        let task = self.snapshot.tasks[&row.task_id].clone();
        let mut body: Vec<AnyElement> = Vec::new();
        let stop_reason = task
            .gate_stop_reason
            .clone()
            .or(task.setup_failure_reason.clone())
            .unwrap_or_default();
        if !stop_reason.is_empty() {
            let label = match task.gate_stop_reason {
                Some(_) => "Gate failed",
                None => "Setup failed",
            };
            let command = task
                .gate_stop_reason
                .as_ref()
                .and(task.gate.clone())
                .map(|gate| {
                    let text = div()
                        .font_family(MONO_FONT)
                        .text_size(px(12.))
                        .child(format!("$ {gate}"));
                    self.copyable("gate-command".into(), gate, text, theme, cx)
                });
            let color = if row.status == Status::Blocked {
                theme.orange
            } else {
                theme.red
            };
            let mono = |text: Div| text.font_family(MONO_FONT).text_size(px(12.));
            body.push(
                Self::callout(label, color, theme)
                    .children(command)
                    .child(self.clamped_text(
                        SECTION_STOP_REASON,
                        &stop_reason,
                        CALLOUT_LINE_LIMIT,
                        mono,
                        theme,
                        cx,
                    ))
                    .into_any_element(),
            );
        }
        if let Some(error) = self.action_errors.get(&row.task_id) {
            body.push(
                Self::callout(error.action.failed_text(), theme.red, theme)
                    .child(self.clamped_text(
                        SECTION_ACTION_ERROR,
                        &sentence_case(&error.message),
                        CALLOUT_LINE_LIMIT,
                        |text| text,
                        theme,
                        cx,
                    ))
                    .into_any_element(),
            );
        }
        if board::is_stuck(row, &task, now) {
            body.push(
                Self::callout("Stuck", theme.orange, theme)
                    .child(board::waiting_reason_text(row, &self.snapshot))
                    .into_any_element(),
            );
        }
        let result = task
            .result
            .as_deref()
            .filter(|result| !result.trim().is_empty())
            .map(|result| {
                let title = if row.status == Status::Failed {
                    "Stop reason"
                } else {
                    "Result"
                };
                Self::section(title, theme)
                    .child(self.clamped_text(
                        SECTION_RESULT,
                        &board::plain_result(result),
                        RESULT_LINE_LIMIT,
                        |text| text,
                        theme,
                        cx,
                    ))
                    .into_any_element()
            });
        let pull_requests = self.render_pull_requests(row, theme, cx);
        let chain = self.render_chain(row, theme);
        let prompt = self.render_prompt(&task, theme, cx);
        let activity = self.render_activity(row, theme, cx);
        let session = self
            .is_showing_fields
            .then(|| self.render_session(row, now, theme, cx));
        let sections = if self.is_following {
            vec![Some(activity)]
        } else if row.status == Status::Running {
            vec![Some(activity), prompt, chain, session]
        } else {
            vec![
                result,
                pull_requests,
                prompt,
                chain,
                Some(activity),
                session,
            ]
        };
        body.extend(sections.into_iter().flatten());
        body
    }

    /// "#412 open" that opens the pull request; a lone PR shows it in the details status line.
    fn pull_request_link(
        pull_request: &ledger::PullRequest,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let url = pull_request.url.clone();
        let is_open = pull_request.state == "open";
        div()
            .id(SharedString::from(format!("pr-{}", pull_request.url)))
            .flex()
            .gap(px(6.))
            .items_baseline()
            .rounded(px(5.))
            .hover(|style| style.bg(theme.hover))
            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                let url = url.clone();
                this.run_in_background(
                    Vec::new(),
                    move || outcome_or(golem_cli::open_url(&url), String::new()),
                    cx,
                );
            }))
            .text_color(if is_open { theme.accent } else { theme.text_3 })
            .child(
                div()
                    .font_weight(FontWeight::BOLD)
                    .child(board::pull_request_number(pull_request)),
            )
            .child(pull_request.state.clone())
    }

    fn render_pull_requests(
        &self,
        row: &Row,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Option<AnyElement> {
        let pull_requests = board::row_pull_requests(&self.snapshot, row);
        if pull_requests.len() < 2 {
            return None;
        }
        let mut list = div().flex().flex_col().gap(px(3.)).text_size(px(12.5));
        for pull_request in pull_requests {
            list = list.child(
                Self::pull_request_link(&pull_request, theme, cx).child(
                    div()
                        .min_w_0()
                        .truncate()
                        .text_color(theme.text_3)
                        .child(pull_request_repository(&pull_request.url)),
                ),
            );
        }
        Some(
            Self::section("Pull requests", theme)
                .child(list)
                .into_any_element(),
        )
    }

    /// The chain's other steps; the shown step's position is already in the status line.
    fn render_chain(&self, row: &Row, theme: &Theme) -> Option<AnyElement> {
        if row.chain_ids.len() < 2 {
            return None;
        }
        let steps = board::chain_steps(row, &self.snapshot.tasks);
        let shown_index = steps.iter().position(|step| step.is_shown).unwrap_or(0);
        let lines = steps
            .iter()
            .enumerate()
            .filter(|(_, step)| !step.is_shown)
            .map(|(index, step)| {
                div()
                    .flex()
                    .items_center()
                    .gap(px(6.))
                    .text_size(px(12.5))
                    .child(div().flex_none().text_color(theme.text_3).child(
                        if index < shown_index {
                            "Before"
                        } else {
                            "Then"
                        },
                    ))
                    .child(icon(
                        theme::status_icon(step.status),
                        11.,
                        theme.status_icon_color(step.status, false),
                    ))
                    .child(div().min_w_0().truncate().child(step.name.clone()))
                    .when(!step.is_inferable, |this| {
                        this.child(div().text_color(theme.text_3).child(step.status.name()))
                    })
            });
        Some(
            Self::section("Chain", theme)
                .children(lines)
                .into_any_element(),
        )
    }

    fn render_prompt(
        &self,
        task: &ledger::Task,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Option<AnyElement> {
        let prompt = task.prompt.trim();
        if prompt.is_empty() {
            return None;
        }
        Some(
            Self::section("Prompt", theme)
                .child(self.clamped_text(
                    SECTION_PROMPT,
                    prompt,
                    PROMPT_LINE_LIMIT,
                    |text| text.text_color(theme.text_2),
                    theme,
                    cx,
                ))
                .into_any_element(),
        )
    }

    /// Long text clamped to `line_limit` lines with its own Show more control; E expands all.
    /// How many lines `text` takes in the details pane, wrapped as rendered; blank lines
    /// render as paragraph gaps, so they don't count. Before the first frame, source lines.
    fn wrapped_line_count(&self, section: &str, text: &str) -> usize {
        let Some(measure) = &self.text_measure else {
            return text.lines().count();
        };
        let is_callout = matches!(section, SECTION_STOP_REASON | SECTION_ACTION_ERROR);
        let width = self.panel_width() as f32
            - PANEL_WIDTH as f32
            - 1.
            - 2. * DETAIL_SIDE_PADDING
            - if is_callout { CALLOUT_INSET } else { 0. };
        let (font, font_size) = match section {
            SECTION_STOP_REASON => (gpui::font(MONO_FONT), 12.),
            _ => (measure.font.clone(), 13.),
        };
        let run = TextRun {
            len: text.len(),
            font,
            color: Hsla::default(),
            background_color: None,
            underline: None,
            strikethrough: None,
        };
        let shaped = measure.system.shape_text(
            SharedString::from(text.to_string()),
            px(font_size),
            &[run],
            Some(px(width)),
            None,
        );
        shaped.map_or_else(
            |_| text.lines().count(),
            |lines| {
                (lines.iter())
                    .filter(|line| !line.text.trim().is_empty())
                    .map(|line| line.wrap_boundaries().len() + 1)
                    .sum()
            },
        )
    }

    fn clamped_text(
        &self,
        section: &'static str,
        text: &str,
        line_limit: usize,
        style: impl FnOnce(Div) -> Div,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Div {
        let is_expanded = self.expanded_sections.contains(section);
        let is_long = self.wrapped_line_count(section, text.trim()) > line_limit;
        let is_clamped = is_long && !is_expanded;
        // Diagnostics keep their lines, each cut to one; prose joins its paragraphs, because
        // line_clamp limits each paragraph separately.
        let is_diagnostic = matches!(section, SECTION_STOP_REASON | SECTION_ACTION_ERROR);
        let shown_text = match (is_clamped, is_diagnostic) {
            (false, _) => text.to_string(),
            (true, true) => text.lines().take(line_limit).collect::<Vec<_>>().join("\n"),
            (true, false) => text.split_whitespace().collect::<Vec<_>>().join(" "),
        };
        let body = match (is_clamped, is_diagnostic) {
            (false, _) => style(div())
                .flex()
                .flex_col()
                .gap(px(PARAGRAPH_GAP))
                .children(
                    shown_text
                        .split("\n\n")
                        .map(|paragraph| paragraph.trim_matches('\n'))
                        .filter(|paragraph| !paragraph.is_empty())
                        .map(|paragraph| div().child(code_marked_text(paragraph))),
                ),
            (true, true) => style(div()).truncate().child(code_marked_text(&shown_text)),
            (true, false) => style(div())
                .line_clamp(line_limit)
                .text_ellipsis()
                .child(code_marked_text(&shown_text)),
        };
        div()
            .flex()
            .flex_col()
            .gap(px(3.))
            .child(body)
            .when(is_long, |this| {
                this.child(
                    div()
                        .id(section)
                        .flex()
                        .items_center()
                        .gap(px(6.))
                        .text_size(px(12.))
                        .font_weight(FontWeight::MEDIUM)
                        .text_color(theme.accent)
                        .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                            if !this.expanded_sections.remove(section) {
                                this.expanded_sections.insert(section);
                            }
                            cx.notify();
                        }))
                        .child(if is_expanded {
                            "Show less"
                        } else {
                            "Show more"
                        }),
                )
            })
    }

    /// A value with a Copy link that shows on hover, for paths, branches and commands.
    fn copyable(
        &self,
        id: SharedString,
        value: String,
        content: Div,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Stateful<Div> {
        let is_copied = self.copied_id.as_ref() == Some(&id);
        div()
            .id(id.clone())
            .group(id.clone())
            .flex()
            .items_baseline()
            .gap(px(8.))
            .child(content.min_w_0())
            .child(
                div()
                    .id(SharedString::from(format!("{id}-copy")))
                    .flex_none()
                    .when(!is_copied, |this| {
                        this.invisible()
                            .group_hover(id.clone(), |style| style.visible())
                    })
                    .text_size(px(11.5))
                    .font_weight(FontWeight::MEDIUM)
                    .text_color(theme.accent)
                    .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                        cx.write_to_clipboard(ClipboardItem::new_string(value.clone()));
                        this.mark_copied(id.clone(), cx);
                    }))
                    .child(if is_copied { "Copied" } else { "Copy" }),
            )
    }

    /// Shows "Copied" on the link that was clicked, briefly, where the eye already is.
    fn mark_copied(&mut self, id: SharedString, cx: &mut Context<Self>) {
        self.copied_id = Some(id.clone());
        cx.notify();
        cx.spawn(async move |this, cx| {
            cx.background_executor().timer(COPIED_DURATION).await;
            let _ = this.update(cx, |this, cx| {
                if this.copied_id.as_ref() == Some(&id) {
                    this.copied_id = None;
                    cx.notify();
                }
            });
        })
        .detach();
    }

    fn render_session(
        &self,
        row: &Row,
        now: DateTime<Local>,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let mut grid = div().flex().flex_col().gap(px(4.)).text_size(px(12.5));
        let task = &self.snapshot.tasks[&row.task_id];
        let is_gate_in_callout = task.gate_stop_reason.is_some();
        let fields = board::task_fields(row, &self.snapshot, now)
            .into_iter()
            .filter(|(label, _)| !(*label == "Gate" && is_gate_in_callout));
        for (label, value) in fields {
            let is_code = matches!(
                label,
                "Directory" | "Worktree" | "Branch" | "Session" | "Gate" | "Setup"
            );
            grid = grid.child(
                div()
                    .flex()
                    .gap(px(12.))
                    .child(
                        div()
                            .w(px(78.))
                            .flex_none()
                            .text_color(theme.text_2)
                            .child(label),
                    )
                    .map(|this| {
                        if !is_code {
                            return this.child(div().flex_1().min_w_0().child(value));
                        }
                        let text = div()
                            .font_family(MONO_FONT)
                            .text_size(px(12.))
                            .child(value.clone());
                        let id = SharedString::from(format!("session-{label}"));
                        this.child(self.copyable(id, value, text, theme, cx).flex_1())
                    }),
            );
        }
        Self::section("Session", theme)
            .child(grid)
            .into_any_element()
    }

    fn render_activity(&mut self, row: &Row, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let current_lines = self.selected_activity();
        let is_paused = self.is_following && self.is_follow_paused;
        let new_lines = lines_since_pause(&self.paused_lines, &current_lines);
        // Paused, the log keeps what was read and adds new lines under a divider.
        let (lines, new_start) = match (is_paused, new_lines) {
            (false, _) => (current_lines.clone(), None),
            (true, Some(new_lines)) => {
                let start = self.paused_lines.len();
                let shown = [self.paused_lines.as_slice(), new_lines].concat();
                (shown, (!new_lines.is_empty()).then_some(start))
            }
            (true, None) => (self.paused_lines.clone(), None),
        };
        let new_activity = match new_lines {
            Some([]) => None,
            Some(new_lines) => Some(format!("{} new", new_lines.len())),
            None => Some("new activity".to_string()),
        };
        let is_summary = !self.is_following
            && !self.is_activity_open
            && self.is_summarised(row)
            && !lines.is_empty();
        if is_summary {
            let summary = div()
                .id("activity-summary")
                .self_start()
                .flex()
                .items_center()
                .gap(px(5.))
                .mx(px(-4.))
                .px(px(4.))
                .py(px(2.))
                .rounded(px(5.))
                .text_size(px(12.5))
                .text_color(theme.text_2)
                .hover(|style| style.bg(theme.hover))
                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                    this.open_activity();
                    cx.notify();
                }))
                .child(board::activity_counts(&lines))
                .child(icon(theme::ICON_CHEVRON, 8., theme.text_3));
            return Self::section("Activity", theme)
                .child(summary)
                .into_any_element();
        }
        let live = self.is_following.then(|| {
            let (color, label) = match (self.is_follow_paused, row.status) {
                (true, _) if new_activity.is_some() => (
                    theme.text_2,
                    format!("Paused \u{b7} {}", new_activity.clone().unwrap_or_default()),
                ),
                (true, _) => (theme.text_2, "Paused".to_string()),
                (false, Status::Running) => (theme.accent, "Live".to_string()),
                (false, _) => (theme.accent, "Full log".to_string()),
            };
            div()
                .id("follow-state")
                .when(self.is_follow_paused, |this| {
                    this.on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.toggle_follow(cx)))
                })
                .flex()
                .items_center()
                .gap(px(5.))
                .text_color(color)
                .text_size(px(11.5))
                .font_weight(FontWeight::SEMIBOLD)
                .child(div().size(px(6.)).rounded_full().bg(color))
                .child(label)
        });
        let header = div()
            .flex()
            .items_center()
            .gap(px(8.))
            .mb(px(2.))
            .child(
                div()
                    .flex_1()
                    .text_size(px(11.))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_2)
                    .child("ACTIVITY"),
            )
            .children(live);
        let mut log = div().flex().flex_col().text_size(px(12.5));
        if lines.is_empty() {
            let text = if row.status == Status::Running {
                "No activity yet"
            } else {
                "No activity"
            };
            log = log.child(div().text_color(theme.text_3).child(text));
        }
        let level = if self.is_following {
            ActivityLevel::All
        } else {
            ActivityLevel::Messages
        };
        let items = board::activity_view(&lines, level);
        let earlier_count = match (self.is_following || self.is_showing_earlier, row.status) {
            (true, _) => 0,
            (false, Status::Running) => items.len().saturating_sub(MESSAGE_ITEM_LIMIT),
            (false, _) => items.len().saturating_sub(STOPPED_MESSAGE_ITEM_LIMIT),
        };
        if earlier_count > 0 {
            log = log.child(
                div()
                    .id("show-earlier")
                    .py(px(2.))
                    .text_color(theme.accent)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.is_showing_earlier = true;
                        cx.notify();
                    }))
                    .child(format!("Show {earlier_count} earlier")),
            );
        }
        let is_history = row.status != Status::Running && !self.is_following;
        for (index, item) in items.into_iter().enumerate().skip(earlier_count) {
            if Some(index) == new_start {
                log = log.child(
                    div()
                        .flex()
                        .items_center()
                        .gap(px(8.))
                        .my(px(4.))
                        .text_size(px(11.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.accent)
                        .child("New")
                        .child(div().flex_1().h(px(1.)).bg(theme.accent_soft)),
                );
            }
            match item {
                ActivityItem::Message(text) => {
                    log = log.child(
                        div()
                            .py(px(2.))
                            .when(is_history, |this| this.text_color(theme.text_2))
                            .child(text),
                    );
                }
                ActivityItem::Tool(text) => log = log.child(tool_line(text, theme)),
                ActivityItem::Fold(tools) => {
                    let is_open = self.expanded_folds.contains(&index);
                    let chevron = icon(theme::ICON_CHEVRON, 8., theme.text_3)
                        .when(is_open, |this| {
                            this.with_transformation(Transformation::rotate(radians(FRAC_PI_2)))
                        });
                    log = log.child(
                        fold_row(("activity-fold", index), theme)
                            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                                if !this.expanded_folds.remove(&index) {
                                    this.expanded_folds.insert(index);
                                }
                                cx.notify();
                            }))
                            .items_center()
                            .child(board::counted_text(tools.len(), "tool call"))
                            .child(chevron),
                    );
                    if is_open {
                        log = log.children(tools.into_iter().map(|text| tool_line(text, theme)));
                    }
                }
            }
        }
        div()
            .flex()
            .flex_col()
            .mb(px(12.))
            .child(header)
            .child(log)
            .into_any_element()
    }

    /// A boxed notice with a coloured edge and an uppercase label; callers add the body.
    fn callout(label: &str, color: Hsla, theme: &Theme) -> Div {
        div()
            .flex()
            .flex_col()
            .gap(px(3.))
            .mb(px(12.))
            .px(px(12.))
            .py(px(9.))
            .rounded(px(8.))
            .bg(theme.raised)
            .border_l(px(3.))
            .border_color(color)
            .child(
                div()
                    .text_size(px(10.5))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_2)
                    .child(label.to_uppercase()),
            )
    }

    fn render_detail(&mut self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let Some(row) = self.selected_row().cloned() else {
            return div()
                .flex_1()
                .flex()
                .justify_center()
                .pt(px(34.))
                .text_color(theme.text_3)
                .child(format!("No task selected in {}", self.tab.title()))
                .into_any_element();
        };
        let now = self.board_now();
        let task = self.snapshot.tasks[&row.task_id].clone();
        let is_stuck = board::is_stuck(&row, &task, now);
        let state_color = theme.status_color(row.status, is_stuck);
        let mut sub_parts =
            vec![board::card_when_text(&row, &task, now).replacen("stuck ", "overdue ", 1)];
        sub_parts.push(board::start_text(&row, &task, now));
        if !row.step.is_empty() {
            sub_parts.push(format!("step {}", row.step.replace('/', " of ")));
        }
        sub_parts.retain(|part| !part.is_empty());
        let mut sub = div()
            .flex()
            .flex_wrap()
            .items_center()
            .gap(px(7.))
            .text_size(px(12.5))
            .text_color(theme.text_2)
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(5.))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(state_color)
                    .child(icon(
                        theme::status_icon(row.status),
                        13.,
                        theme.status_icon_color(row.status, is_stuck),
                    ))
                    .child(if is_stuck { "stuck" } else { row.status.name() }),
            );
        let pull_requests = board::row_pull_requests(&self.snapshot, &row);
        let mut lone_pull_request = match pull_requests.as_slice() {
            [pull_request] => Some(Self::pull_request_link(pull_request, theme, cx)),
            _ => None,
        };
        for (index, part) in sub_parts.into_iter().enumerate() {
            sub = sub
                .child(div().text_color(theme.text_3).child("\u{b7}"))
                .child(part);
            if index == 0
                && let Some(link) = lone_pull_request.take()
            {
                sub = sub
                    .child(div().text_color(theme.text_3).child("\u{b7}"))
                    .child(link);
            }
        }
        let head = div()
            .pl(px(18.))
            .pr(px(12.))
            .pt(px(12.))
            .pb(px(10.))
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(8.))
                    .h(px(20.))
                    .text_size(px(11.5))
                    .text_color(theme.text_3)
                    .child(div().flex_1().min_w_0().truncate().child(format!(
                        "{} \u{b7} {}",
                        board::repository_name(&row),
                        row.root_id
                    )))
                    .when(self.is_following, |this| {
                        this.child(
                            div()
                                .id("follow-back")
                                .h(px(24.))
                                .pl(px(6.))
                                .pr(px(4.))
                                .flex()
                                .items_center()
                                .gap(px(6.))
                                .rounded(px(6.))
                                .text_color(theme.text_2)
                                .hover(|style| style.bg(theme.hover))
                                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                    this.is_following = false;
                                    cx.notify();
                                }))
                                .child("Back")
                                .child(kbd("esc", theme)),
                        )
                    })
                    .child(
                        div()
                            .id("collapse")
                            .h(px(24.))
                            .px(px(4.))
                            .flex()
                            .items_center()
                            .gap(px(6.))
                            .rounded(px(6.))
                            .hover(|style| style.bg(theme.hover))
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                this.set_expanded(false, cx)
                            }))
                            .when(!self.is_following, |this| this.child(kbd("esc", theme)))
                            .child(icon(theme::ICON_FOLD, 15., theme.text_2)),
                    ),
            )
            .child(
                div().flex().items_center().gap(px(8.)).mb(px(4.)).child(
                    div()
                        .flex_1()
                        .min_w_0()
                        .text_size(px(19.))
                        .font_weight(FontWeight::BOLD)
                        .child(board::row_title(&row).to_string()),
                ),
            )
            .child(sub);
        let body = self.render_detail_body(&row, theme, cx);
        let actions = board::available_actions(&self.snapshot, Some(&row));
        let primary = board::primary_action(&self.snapshot, &row, &actions);
        let is_running = row.status == Status::Running;
        let busy_action = self.busy_actions.get(&row.task_id).copied();
        let mut actions: Vec<Action> = actions
            .into_iter()
            .filter(|action| match action {
                Action::CloseAll | Action::Copy => false,
                Action::Watch => is_running,
                _ => true,
            })
            .collect();
        actions.sort_by_key(|action| Some(*action) != primary);
        let first_ending = actions
            .iter()
            .position(|action| matches!(action, Action::Close | Action::Cancel | Action::Dismiss));
        let buttons = actions.into_iter().enumerate().map(|(index, action)| {
            let is_on = action == Action::Watch && self.is_following;
            let label = match action {
                _ if busy_action == Some(action) => action.busy_text(),
                Action::Watch if is_on && self.is_follow_paused => "Resume",
                Action::Watch if is_on => "Following",
                _ => action.label(),
            };
            let is_resume = is_on && self.is_follow_paused;
            let is_primary = (Some(action) == primary && !is_on) || is_resume;
            let is_danger = action == Action::Cancel;
            let (background, foreground) = if is_primary {
                (theme.accent_fill, theme.on_accent)
            } else if is_on {
                (theme.accent_soft, theme.accent)
            } else if is_danger {
                (theme.fill, theme.red)
            } else {
                (theme.fill, theme.text)
            };
            let has_key = is_primary;
            let key_chip = kbd(&key_label(action), theme).when(is_primary, |this| {
                this.bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                    .text_color(theme.on_accent)
            });
            div()
                .id(action.label())
                .flex()
                .items_center()
                .gap(px(7.))
                .h(px(26.))
                .map(|this| match (has_key, self.is_typing()) {
                    (true, false) => this.pl(px(10.)).pr(px(6.)),
                    (true, true) => this.px(px((10. + 7. + KEYCAP_WIDTH + 6.) / 2.)),
                    (false, _) => this.px(px(10.)),
                })
                .rounded(px(7.))
                .bg(background)
                .text_color(foreground)
                .font_weight(if is_primary {
                    FontWeight::SEMIBOLD
                } else {
                    FontWeight::MEDIUM
                })
                .when(Some(index) == first_ending, |this| this.ml_auto())
                .map(|this| {
                    if busy_action.is_some() && action.is_golem_run() {
                        this.opacity(0.55)
                    } else {
                        this.hover(|style| style.opacity(0.9))
                    }
                })
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.request(action, cx)))
                .child(label)
                .when(has_key && !self.is_typing(), |this| this.child(key_chip))
        });
        div()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .child(head)
            .child(
                div()
                    .relative()
                    .flex_1()
                    .min_h_0()
                    .flex()
                    .flex_col()
                    .child(
                        div()
                            .id("detail")
                            .flex_1()
                            .min_h_0()
                            .overflow_y_scroll()
                            .track_scroll(&self.detail_scroll)
                            .on_scroll(cx.listener(|this, event: &ScrollEvent, _, cx| {
                                let is_scrolling_up = event.delta.pixel_delta(px(16.)).y > px(0.);
                                if is_scrolling_up {
                                    this.pause_follow();
                                    cx.notify();
                                }
                            }))
                            .pl(px(DETAIL_SIDE_PADDING))
                            .pr(px(DETAIL_SIDE_PADDING))
                            .pt(px(2.))
                            .pb(px(DETAIL_FADE_HEIGHT + 4.))
                            .flex()
                            .flex_col()
                            .children(body),
                    )
                    .child(
                        div()
                            .absolute()
                            .bottom_0()
                            .left_0()
                            .w_full()
                            .h(px(DETAIL_FADE_HEIGHT))
                            .bg(linear_gradient(
                                180.,
                                linear_color_stop(
                                    Hsla {
                                        a: 0.,
                                        ..theme.panel
                                    },
                                    0.,
                                ),
                                linear_color_stop(theme.panel, 1.),
                            )),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .items_center()
                    .gap(px(6.))
                    .pl(px(18.))
                    .pr(px(12.))
                    .py(px(5.))
                    .min_h(px(BOTTOM_BAR_HEIGHT))
                    .border_t_1()
                    .border_color(theme.line)
                    .children(buttons),
            )
            .into_any_element()
    }

    fn footer_button(id: &'static str, theme: &Theme) -> Stateful<Div> {
        div()
            .id(id)
            .flex()
            .items_center()
            .gap(px(6.))
            .h(px(24.))
            .pl(px(7.))
            .pr(px(5.))
            .rounded(px(6.))
            .text_size(px(12.))
            .font_weight(FontWeight::MEDIUM)
            .text_color(theme.text_2)
            .hover(|style| style.bg(theme.hover).text_color(theme.text))
    }

    fn render_footer(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        // Overlays cover the footer's controls, so it empties rather than show them inert.
        if self.palette.is_some() || self.is_sheet_open() {
            return div().h(px(BOTTOM_BAR_HEIGHT)).into_any_element();
        }
        let done_count = self.column_rows(Column::Done).len();
        let done_label: AnyElement = if self.is_showing_done {
            div().child("Hide Done").into_any_element()
        } else {
            div()
                .flex()
                .gap(px(4.))
                .child("Show Done")
                .child(div().text_color(theme.text_3).child(done_count.to_string()))
                .into_any_element()
        };
        div()
            .flex()
            .items_center()
            .gap(px(2.))
            .h(px(BOTTOM_BAR_HEIGHT))
            .px(px(7.))
            .border_t_1()
            .border_color(theme.line)
            .child(
                Self::footer_button("toggle-done", theme)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.toggle_done(cx)))
                    .child(done_label),
            )
            // A passing message sits in the footer's free space, so the panel keeps its size.
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .flex()
                    .items_center()
                    .gap(px(6.))
                    .px(px(6.))
                    .text_size(px(12.))
                    .text_color(theme.text_2)
                    .font_weight(FontWeight::MEDIUM)
                    .children(
                        self.toast
                            .as_ref()
                            .filter(|toast| !toast.is_error)
                            .map(|toast| {
                                [
                                    div()
                                        .flex_none()
                                        .size(px(5.))
                                        .rounded_full()
                                        .bg(theme.accent)
                                        .into_any_element(),
                                    div()
                                        .min_w_0()
                                        .truncate()
                                        .child(toast.text.clone())
                                        .into_any_element(),
                                ]
                            })
                            .into_iter()
                            .flatten(),
                    ),
            )
            .child(
                Self::footer_button("palette", theme)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.set_palette(Some(Palette::default()), cx);
                        cx.notify();
                    }))
                    .child("Actions")
                    .child(kbd("\u{2318}K", theme)),
            )
            .child(
                Self::footer_button("help", theme)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.set_help(true, cx);
                        cx.notify();
                    }))
                    .when(self.is_typing(), |this| {
                        this.px(px((7. + 6. + KEYCAP_WIDTH + 5.) / 2.))
                    })
                    .child("Keys")
                    .when(!self.is_typing(), |this| this.child(kbd("?", theme))),
            )
            .into_any_element()
    }

    fn scrim(&self, theme: &Theme) -> Div {
        div()
            .absolute()
            .top_0()
            .left_0()
            .size_full()
            .flex()
            .justify_center()
            .items_start()
            .bg(theme.scrim)
    }

    fn sheet_button(
        id: &'static str,
        label: String,
        key: &str,
        is_primary: bool,
        is_danger: bool,
        theme: &Theme,
    ) -> Stateful<Div> {
        let primary_color = if is_danger {
            theme.red_fill
        } else {
            theme.accent_fill
        };
        div()
            .id(id)
            .flex_1()
            .flex()
            .items_center()
            .justify_center()
            .gap(px(7.))
            .h(px(28.))
            .rounded(px(7.))
            .bg(if is_primary {
                primary_color
            } else {
                theme.fill
            })
            .text_color(if is_primary {
                theme.on_accent
            } else {
                theme.text
            })
            .font_weight(if is_primary {
                FontWeight::SEMIBOLD
            } else {
                FontWeight::MEDIUM
            })
            .child(label)
            .child(kbd(key, theme).when(is_primary, |this| {
                this.bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                    .text_color(theme.on_accent)
            }))
    }

    /// Sheets span the panel less a 12 pt inset, so no header text shows clipped at their sides.
    fn sheet_width(&self, max_width: f32) -> f32 {
        (self.panel_width() as f32 - 24.).min(max_width)
    }

    fn sheet(theme: &Theme, width: f32) -> Div {
        div()
            .w(px(width))
            .p(px(18.))
            .pb(px(14.))
            .rounded_b(px(12.))
            .bg(theme.panel)
            .shadow(vec![BoxShadow {
                color: gpui::rgba(0x00000059).into(),
                offset: point(px(0.), px(14.)),
                blur_radius: px(40.),
                spread_radius: px(0.),
                inset: false,
            }])
    }

    /// Confirmations and errors sit at the bottom, so the task stays visible above; inset on
    /// every side, since a flush sheet in the 900 pt expanded panel would span it.
    fn bottom_sheet(&self, theme: &Theme) -> Div {
        Self::sheet(theme, self.sheet_width(SMALL_SHEET_MAX_WIDTH))
            .mb(px(12.))
            .rounded(px(12.))
    }

    fn render_confirm(
        &self,
        pending: &Pending,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        let row = pending.task_id.as_ref().and_then(|task_id| {
            self.snapshot
                .rows
                .iter()
                .find(|row| &row.task_id == task_id)
        });
        let (title, body, verb) = match (pending.action, row) {
            (Action::CloseAll, _) => {
                let count = board::closable_task_ids(&self.snapshot).len();
                (
                    format!("Close {}?", board::counted_text(count, "finished task")),
                    "They move to Done.".to_string(),
                    "Close all".to_string(),
                )
            }
            (action, Some(row)) => {
                let name = board::row_title(row);
                let follow_up_count =
                    board::waiting_dependent_count(&self.snapshot.tasks, &row.task_id);
                let moved = match follow_up_count {
                    0 => "The task moves to Done.".to_string(),
                    1 => "It and its waiting follow-up move to Done.".to_string(),
                    count => format!("It and its {count} waiting follow-ups move to Done."),
                };
                let body = match action {
                    Action::Cancel if row.status == Status::Running => {
                        format!("The agent stops. {moved}")
                    }
                    Action::Cancel | Action::Dismiss => moved,
                    _ => "It restarts from its prompt.".to_string(),
                };
                let verb = if action == Action::Cancel {
                    "Cancel task"
                } else {
                    action.label()
                };
                (
                    format!("{} {name}?", action.label()),
                    body,
                    verb.to_string(),
                )
            }
            (_, None) => (
                "Task no longer exists".to_string(),
                String::new(),
                "OK".to_string(),
            ),
        };
        let accept_key = if pending.action.is_destructive() {
            key_label(pending.action)
        } else {
            "\u{23ce}".to_string()
        };
        let decline = if pending.action == Action::Cancel {
            "Keep"
        } else {
            "Cancel"
        };
        self.scrim(theme)
            .id("confirm")
            .occlude()
            .items_end()
            .child(
                self.bottom_sheet(theme)
                    .child(
                        div()
                            .text_size(px(14.))
                            .font_weight(FontWeight::BOLD)
                            .mb(px(3.))
                            .child(title),
                    )
                    .child(div().text_color(theme.text_2).child(body))
                    .child(
                        div()
                            .flex()
                            .gap(px(8.))
                            .mt(px(14.))
                            .child(
                                Self::sheet_button(
                                    "decline",
                                    decline.to_string(),
                                    "esc",
                                    false,
                                    false,
                                    theme,
                                )
                                .on_click(
                                    cx.listener(|this, _: &ClickEvent, _, cx| {
                                        this.answer(false, cx)
                                    }),
                                ),
                            )
                            .child(
                                Self::sheet_button(
                                    "accept",
                                    verb,
                                    &accept_key,
                                    true,
                                    pending.action == Action::Cancel,
                                    theme,
                                )
                                .on_click(
                                    cx.listener(|this, _: &ClickEvent, _, cx| {
                                        this.answer(true, cx)
                                    }),
                                ),
                            ),
                    ),
            )
            .into_any_element()
    }

    /// The full text of an error that has no task, which the one-line toast cuts short.
    fn render_error_detail(
        &self,
        detail: &ErrorDetail,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> AnyElement {
        self.scrim(theme)
            .id("error-detail")
            .occlude()
            .items_end()
            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                this.error_detail = None;
                cx.notify();
            }))
            .child(
                self.bottom_sheet(theme)
                    .id("error-sheet")
                    .on_click(|_, _, cx| cx.stop_propagation())
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .gap(px(7.))
                            .text_size(px(14.))
                            .font_weight(FontWeight::BOLD)
                            .child(icon(theme::status_icon(Status::Failed), 13., theme.red))
                            .child(div().flex_1().child(detail.title))
                            .child(kbd("esc", theme)),
                    )
                    .child(
                        div()
                            .id("error-text")
                            .mt(px(8.))
                            .max_h(px(ERROR_TEXT_MAX_HEIGHT))
                            .overflow_y_scroll()
                            .track_scroll(&self.error_scroll)
                            .text_color(theme.text_2)
                            .child(code_marked_text(&detail.text)),
                    )
                    .child(
                        div().flex().justify_end().mt(px(12.)).child(
                            div()
                                .id("error-copy")
                                .flex()
                                .items_center()
                                .gap(px(7.))
                                .h(px(26.))
                                .pl(px(10.))
                                .pr(px(5.))
                                .rounded(px(7.))
                                .bg(theme.fill)
                                .font_weight(FontWeight::MEDIUM)
                                .hover(|style| style.bg(theme.hover))
                                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                    this.copy_error_detail(cx);
                                }))
                                .child(if self.is_error_copied {
                                    "Copied"
                                } else {
                                    "Copy"
                                })
                                .child(kbd("\u{2318}C", theme)),
                        ),
                    ),
            )
            .into_any_element()
    }

    fn render_help(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let section = |title: &str, key_width: f32, entries: &[(&str, &str)]| {
            let rows = entries.iter().map(|(keys, text)| {
                div()
                    .flex()
                    .items_center()
                    .gap(px(6.))
                    .py(px(2.))
                    .text_size(px(12.))
                    .child(
                        div()
                            .w(px(key_width))
                            .flex_none()
                            .flex()
                            .gap(px(2.))
                            .children(keys.split(' ').map(|key| kbd(key, theme))),
                    )
                    .child(div().truncate().child(text.to_string()))
            });
            div()
                .flex()
                .flex_col()
                .child(
                    div()
                        .mt(px(10.))
                        .mb(px(4.))
                        .text_size(px(11.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.text_2)
                        .child(title.to_uppercase()),
                )
                .children(rows)
        };
        let move_keys = section(
            "Move",
            72.,
            &[
                ("\u{2191}\u{2193} J K", "Select"),
                ("\u{2190}\u{2192} H L", "Switch group"),
                ("1-4", "Jump to group"),
                ("G \u{21e7}G", "First, last"),
                ("N", "Next needing you"),
                ("\u{23ce}", "Details"),
                ("esc", "Back"),
                ("\u{2423} \u{2303}D \u{2303}U", "Scroll"),
            ],
        );
        let board_keys = section(
            "Board",
            48.,
            &[
                ("/", "Filter"),
                ("A", "Show Done"),
                ("\u{21e7}C", "Close finished"),
                ("\u{2318}K", "All actions"),
                ("\u{2303}\u{2325}G", "Open panel"),
            ],
        );
        let task_keys = section(
            "Task",
            48.,
            &[
                ("P", "Pick up"),
                ("W", "Follow log"),
                ("O", "Open PR"),
                ("R", "Retry, recheck"),
                ("C", "Close, dismiss"),
                ("X", "Cancel"),
                ("Y", "Copy ID"),
                ("E", "Expand all"),
                ("I", "Session info"),
            ],
        );
        let columns = [
            div().flex_1().min_w_0().flex().flex_col().child(move_keys),
            div()
                .flex_1()
                .min_w_0()
                .flex()
                .flex_col()
                .child(task_keys)
                .child(board_keys),
        ];
        let statuses = [
            Status::Scheduled,
            Status::Waiting,
            Status::Running,
            Status::Blocked,
            Status::Failed,
            Status::Finished,
            Status::Cancelled,
        ];
        let legend = statuses.into_iter().map(|status| {
            div()
                .flex()
                .items_center()
                .gap(px(3.))
                .text_size(px(12.))
                .child(icon(
                    theme::status_icon(status),
                    11.,
                    theme.status_icon_color(status, false),
                ))
                .child(status.name())
        });
        let status_keys = div()
            .flex()
            .flex_col()
            .child(
                div()
                    .mt(px(10.))
                    .mb(px(6.))
                    .text_size(px(11.))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_2)
                    .child("STATUS"),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .gap_x(px(10.))
                    .gap_y(px(6.))
                    .children(legend),
            );
        let [move_column, task_column] = columns;
        let width = self.sheet_width(640.);
        self.scrim(theme)
            .id("help")
            .occlude()
            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                this.set_help(false, cx);
                cx.notify();
            }))
            .child(
                Self::sheet(theme, width)
                    .id("help-sheet")
                    .on_click(|_, _, cx| cx.stop_propagation())
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .justify_between()
                            .child(
                                div()
                                    .text_size(px(14.))
                                    .font_weight(FontWeight::BOLD)
                                    .child("Keyboard shortcuts"),
                            )
                            .child(kbd("esc", theme)),
                    )
                    .child(
                        div()
                            .id("help-body")
                            .max_h(px(PANEL_HEIGHT as f32 - 150.))
                            .overflow_y_scroll()
                            .child(
                                div()
                                    .flex()
                                    .gap(px(18.))
                                    .children([move_column.child(status_keys), task_column]),
                            ),
                    ),
            )
            .into_any_element()
    }

    fn render_palette(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let items = self.palette_items();
        let palette = self.palette.as_ref();
        let query = palette
            .map(|palette| palette.query.clone())
            .unwrap_or_default();
        let caret = palette.map_or(0, |palette| palette.caret);
        let current = palette.map_or(0, |palette| {
            palette.index.min(items.len().saturating_sub(1))
        });
        let available_height = (self.panel_height() as f32 - PALETTE_CHROME_HEIGHT).min(420.);
        let full_height = palette_list_height(&items, f32::MAX);
        let is_overflowing = full_height > available_height;
        // An overflowing list shows part of the next row, so it reads as scrollable.
        let half_row = PALETTE_ROW_HEIGHT / 2.;
        let whole_rows_height = palette_list_height(&items, available_height);
        let spare_height = available_height - whole_rows_height;
        let list_height = match is_overflowing {
            false => full_height,
            true if spare_height >= PALETTE_MIN_PEEK => {
                whole_rows_height + spare_height.min(half_row)
            }
            true => palette_list_height(&items, available_height - half_row) + half_row,
        };
        let mut list = div()
            .id("palette-list")
            .max_h(px(list_height))
            .overflow_y_scroll()
            .track_scroll(&self.palette_scroll)
            .px(px(5.))
            .pt(px(PALETTE_LIST_PADDING))
            .pb(px(PALETTE_LIST_PADDING));
        let mut group = String::new();
        for (index, item) in items.iter().enumerate() {
            if item.group != group {
                group = item.group.clone();
                list = list.child(
                    div()
                        .px(px(8.))
                        .pt(px(7.))
                        .pb(px(3.))
                        .text_size(px(11.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.text_3)
                        .child(group.clone()),
                );
            }
            let is_current = index == current;
            let command = item.command;
            let foreground = if is_current {
                theme.on_accent
            } else if item.is_danger {
                theme.red
            } else {
                theme.text
            };
            list = list.child(
                div()
                    .id(("palette-item", index))
                    .flex()
                    .items_center()
                    .gap(px(8.))
                    .h(px(PALETTE_ROW_HEIGHT))
                    .pl(px(8.))
                    .pr(px(6.))
                    .rounded(px(6.))
                    .text_color(foreground)
                    .when(is_current, |this| this.bg(theme.accent_fill))
                    .on_click(cx.listener(move |this, _: &ClickEvent, window, cx| {
                        this.run_command(command, window, cx)
                    }))
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .child(item.label.clone()),
                    )
                    .when(!item.key.is_empty(), |this| {
                        this.child(kbd(&item.key, theme).when(is_current, |this| {
                            this.bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                                .text_color(theme.on_accent)
                        }))
                    }),
            );
        }
        if items.is_empty() {
            list = list.child(
                div()
                    .py(px(18.))
                    .flex()
                    .justify_center()
                    .text_color(theme.text_3)
                    .child("No matches"),
            );
        }
        let search_text: AnyElement = if query.is_empty() {
            div()
                .text_color(theme.text_3)
                .child("Search actions")
                .into_any_element()
        } else {
            self.editing_text("palette-query", &query, caret, 14., theme, cx)
                .into_any_element()
        };
        self.scrim(theme)
            .id("palette")
            .occlude()
            .pt(px(62.))
            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                this.set_palette(None, cx);
                cx.notify();
            }))
            .child(
                div()
                    .id("palette-box")
                    .relative()
                    .w(px(370.))
                    .rounded(px(12.))
                    .overflow_hidden()
                    .bg(theme.panel)
                    .border_1()
                    .border_color(theme.line)
                    .on_click(|_, _, cx| cx.stop_propagation())
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .gap(px(8.))
                            .h(px(42.))
                            .px(px(12.))
                            .border_b_1()
                            .border_color(theme.line)
                            .text_size(px(14.))
                            .child(icon(theme::ICON_SEARCH, 14., theme.text_2))
                            .child(div().flex_1().min_w_0().child(search_text))
                            .child(kbd("esc", theme)),
                    )
                    .child(list)
                    .when(is_overflowing, |this| {
                        this.child(
                            div()
                                .absolute()
                                .bottom_0()
                                .left_0()
                                .w_full()
                                .h(px(PALETTE_FADE_HEIGHT))
                                .bg(linear_gradient(
                                    180.,
                                    linear_color_stop(
                                        Hsla {
                                            a: 0.,
                                            ..theme.panel
                                        },
                                        0.,
                                    ),
                                    linear_color_stop(theme.panel, 1.),
                                )),
                        )
                    }),
            )
            .into_any_element()
    }

    /// Toasts sit over the footer so they never cover a card.
    /// An error's row above the footer, which the panel grows for so it never hides a card.
    fn render_toast(&self, toast: &Toast, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        div()
            .flex_none()
            .px(px(8.))
            .pt(px(4.))
            .pb(px(6.))
            .child(
                div()
                    .id("toast")
                    .flex()
                    .items_center()
                    .gap(px(8.))
                    .px(px(11.))
                    .py(px(6.))
                    .rounded(px(8.))
                    .bg(theme.toast)
                    .border_1()
                    .border_color(theme.line)
                    .text_size(px(12.))
                    .font_weight(FontWeight::MEDIUM)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.open_toast_details(cx);
                        cx.notify();
                    }))
                    .when(toast.is_error, |this| {
                        this.border_color(theme.red).child(icon(
                            theme::status_icon(Status::Failed),
                            12.,
                            theme.red,
                        ))
                    })
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .child(toast.text.clone()),
                    )
                    .when(toast.task_id.is_some() || toast.is_error, |this| {
                        this.child(
                            div()
                                .flex_none()
                                .text_color(theme.accent)
                                .font_weight(FontWeight::SEMIBOLD)
                                .child("Details"),
                        )
                        .child(kbd("D", theme).when(self.is_typing(), Div::invisible))
                    }),
            )
            .into_any_element()
    }
}

impl Render for BoardView {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let theme = Theme::for_appearance(window.appearance());
        self.text_measure = Some(TextMeasure {
            system: window.text_system().clone(),
            font: window.text_style().font(),
        });
        if !self.tabs().contains(&self.tab) {
            self.tab = Column::Review;
        }
        self.reset_view_if_task_changed();
        let size = (self.panel_width(), self.panel_height());
        if self.is_visible() && self.placed_size != Some(size) {
            self.placed_size = Some(size);
            self.place(false, cx);
        }
        let side = div()
            .flex()
            .flex_col()
            .min_h_0()
            .min_w_0()
            .when(self.is_expanded, |this| {
                this.w(px(PANEL_WIDTH as f32))
                    .flex_none()
                    .border_l_1()
                    .border_color(theme.line)
                    .bg(theme.sidebar)
            })
            .when(!self.is_expanded, |this| this.flex_1())
            .relative()
            .child(self.render_header(&theme, cx))
            .child(self.render_tabs(&theme, cx))
            .child(self.render_list(&theme, cx))
            .when_some(
                self.toast.as_ref().filter(|toast| toast.is_error),
                |this, toast| this.child(self.render_toast(toast, &theme, cx)),
            )
            .child(self.render_footer(&theme, cx));
        // Details open to the left so the list stays where it was under the pointer.
        let mut body = div().flex_1().min_h_0().flex();
        if self.is_expanded {
            body = body.child(self.render_detail(&theme, cx));
        }
        body = body.child(side);
        let mut root = div()
            .id("board")
            .track_focus(&self.focus_handle)
            .on_key_down(cx.listener(Self::handle_key))
            .relative()
            .size_full()
            .flex()
            .flex_col()
            .overflow_hidden()
            .rounded(px(12.))
            .border_1()
            .border_color(theme.line)
            .bg(theme.panel)
            .text_color(theme.text)
            .text_size(px(13.))
            .child(body);
        if self.palette.is_some() {
            root = root.child(self.render_palette(&theme, cx));
        }
        if let Some(pending) = &self.pending {
            root = root.child(self.render_confirm(pending, &theme, cx));
        } else if self.is_showing_help {
            root = root.child(self.render_help(&theme, cx));
        } else if let Some(detail) = &self.error_detail {
            root = root.child(self.render_error_detail(detail, &theme, cx));
        }
        root
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edited(keys: &[(&str, &str)]) -> (String, usize) {
        let mut text = String::new();
        let mut caret = 0;
        for (key, typed) in keys {
            edit_input(&mut text, &mut caret, key, typed);
        }
        (text, caret)
    }

    #[test]
    fn edit_input_edits_at_the_caret() {
        let typed = |text| ("", text);
        assert_eq!(
            edited(&[typed("api"), ("left", ""), typed("X")]),
            ("apXi".to_string(), 1)
        );
        assert_eq!(
            edited(&[
                typed("é1"),
                ("left", ""),
                ("left", ""),
                ("delete", ""),
                ("end", ""),
                (KEY_BACKSPACE, "")
            ]),
            (String::new(), 0)
        );
        assert_eq!(
            edited(&[
                typed("abc"),
                ("home", ""),
                ("right", ""),
                (KEY_BACKSPACE, ""),
                typed("Z")
            ]),
            ("Zbc".to_string(), 2)
        );
    }

    #[test]
    fn edit_input_keeps_a_stale_caret_valid_after_clearing() {
        let mut text = "abc".to_string();
        let mut caret = 2;
        text.clear();
        edit_input(&mut text, &mut caret, "", "x");
        assert_eq!((text.as_str(), caret), ("x", 0));
    }
}
