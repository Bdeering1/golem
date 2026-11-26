use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, Local};
use futures::StreamExt;
use futures::channel::mpsc::UnboundedReceiver;
use gpui::{
    Animation, AnimationExt, AnyElement, BoxShadow, ClickEvent, ClipboardItem, Context, Div,
    FocusHandle, FontWeight, Hsla, KeyDownEvent, ScrollEvent, ScrollHandle, SharedString, Stateful,
    Subscription, Task, Window, div, linear_color_stop, linear_gradient, point, prelude::*,
    pulsating_between, px, svg,
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
const EXPANDED_PANEL_WIDTH: f64 = 860.0;
const PANEL_HEIGHT: f64 = 640.0;
const MIN_PANEL_HEIGHT: f64 = 320.0;
/// Collapsed-panel layout heights used to fit the panel to its longest group.
const PANEL_CHROME_HEIGHT: f64 = 140.0;
const CARD_GAP: f32 = 2.0;
const CARD_PITCH: f64 = 63.0;
const EMPTY_LIST_HEIGHT: f64 = 90.0;
const SIDE_WIDTH: f32 = 340.0;
const TICK_INTERVAL: Duration = Duration::from_secs(1);
const RELOAD_DEBOUNCE: Duration = Duration::from_millis(80);
const TOAST_DURATION: Duration = Duration::from_secs(3);
const ERROR_TOAST_DURATION: Duration = Duration::from_secs(8);
const CHANGE_HIGHLIGHT_DURATION: Duration = Duration::from_secs(10);
/// A menu bar click that lands this soon after the panel hid itself is the click that hid it.
const REOPEN_GUARD: Duration = Duration::from_millis(300);
const SCROLL_STEP: f32 = 240.0;
const BUSY_PULSE_PERIOD: Duration = Duration::from_millis(1400);
const BUSY_PULSE_FPS: f32 = 30.0;
const LONG_PROMPT_CHARS: usize = 240;
const MESSAGE_ITEM_LIMIT: usize = 12;
/// The palette's top offset, search row and margin, subtracted from the panel height for its list.
const PALETTE_CHROME_HEIGHT: f32 = 150.0;
const PALETTE_ROW_HEIGHT: f32 = 28.0;
const PALETTE_HEADING_HEIGHT: f32 = 25.0;
const PALETTE_FADE_HEIGHT: f32 = 22.0;
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
    Filter,
    Help,
    Quit,
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
    index: usize,
}

struct Toast {
    text: SharedString,
    id: u64,
    is_error: bool,
    /// The failed task a click on the toast opens.
    task_id: Option<String>,
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
    golem: Option<Arc<Golem>>,
    tray_icon: TrayIcon,
    panel: Option<Retained<NSWindow>>,
    focus_handle: FocusHandle,
    list_scroll: ScrollHandle,
    detail_scroll: ScrollHandle,
    palette_scroll: ScrollHandle,
    tab: Column,
    selected_roots: HashMap<Column, String>,
    is_showing_done: bool,
    query: String,
    input: Input,
    is_expanded: bool,
    activity_level: ActivityLevel,
    is_following: bool,
    is_follow_paused: bool,
    is_showing_fields: bool,
    is_showing_prompt: bool,
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
        .min_w(px(17.))
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
            golem: None,
            tray_icon: macos::create_tray_icon(),
            panel: macos::native_window(window),
            focus_handle: cx.focus_handle(),
            list_scroll: ScrollHandle::new(),
            detail_scroll: ScrollHandle::new(),
            palette_scroll: ScrollHandle::new(),
            tab: Column::Review,
            selected_roots: HashMap::new(),
            is_showing_done: false,
            query: String::new(),
            input: Input::None,
            is_expanded: false,
            activity_level: ActivityLevel::Messages,
            is_following: false,
            is_follow_paused: false,
            is_showing_fields: false,
            is_showing_prompt: false,
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

    fn tick(&mut self, cx: &mut Context<Self>) {
        let now = Instant::now();
        self.changed_until.retain(|_, until| *until > now);
        if self.is_visible() {
            cx.notify();
        }
    }

    fn update_tray(&self) {
        let count = |column: Column| {
            self.snapshot
                .rows
                .iter()
                .filter(|row| row.column == column)
                .count()
        };
        let review_count = count(Column::Review);
        let title = (review_count > 0).then(|| format!(" {review_count}"));
        self.tray_icon.set_title(title);
        let tooltip = format!(
            "golem: {} queued, {} running, {review_count} to review",
            count(Column::Queued),
            count(Column::Running)
        );
        let _ = self.tray_icon.set_tooltip(Some(tooltip));
    }

    /// Flashes status changes and keeps the selected task in view when it moves to another group.
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
        let mut changes = Vec::new();
        for row in &self.snapshot.rows {
            let position = (row.column, row.status);
            let earlier = before.get(row.root_id.as_str());
            if earlier != Some(&position) {
                self.changed_until.insert(row.root_id.clone(), until);
            }
            if earlier.is_some_and(|(_, status)| *status != row.status) {
                self.action_errors.remove(&row.task_id);
                let name = board::task_display_name(&self.snapshot.tasks, &row.task_id);
                changes.push(format!("{name} {}", row.status.name()));
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
            return;
        }
        if !changes.is_empty() {
            self.show_toast(changes.join(", "), cx);
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
        if self.is_expanded || self.is_showing_help {
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
        (PANEL_CHROME_HEIGHT + list_height).clamp(MIN_PANEL_HEIGHT, PANEL_HEIGHT)
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
        self.reload(cx);
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
        self.show_message(text, false, cx);
    }

    fn show_message(
        &mut self,
        text: impl Into<SharedString>,
        is_error: bool,
        cx: &mut Context<Self>,
    ) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        self.toast_count += 1;
        let toast_id = self.toast_count;
        self.toast = Some(Toast {
            text,
            id: toast_id,
            is_error,
            task_id: None,
        });
        let duration = if is_error {
            ERROR_TOAST_DURATION
        } else {
            TOAST_DURATION
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

    fn select(&mut self, root_id: String, cx: &mut Context<Self>) {
        self.selected_roots.insert(self.tab, root_id);
        self.is_showing_prompt = false;
        self.input = Input::None;
        self.is_following = false;
        self.detail_scroll.set_offset(point(px(0.), px(0.)));
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
        if self.pending.is_some() || self.is_showing_help {
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
        self.is_following = false;
        self.detail_scroll.set_offset(point(px(0.), px(0.)));
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

    /// Dismisses the toast and opens the task it reports on, if any.
    fn open_toast_task(&mut self, cx: &mut Context<Self>) {
        let toast = self.toast.take();
        if let Some(task_id) = toast.and_then(|toast| toast.task_id) {
            self.open_task(&task_id, cx);
        }
    }

    fn set_palette(&mut self, palette: Option<Palette>, cx: &mut Context<Self>) {
        self.palette = palette;
        self.palette_scroll.scroll_to_item(0);
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
        self.is_showing_done = !self.is_showing_done;
        if self.is_showing_done {
            self.tab = Column::Done;
        } else if self.tab == Column::Done {
            self.tab = Column::Review;
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

    fn scroll_by(&mut self, delta: f32) {
        if delta < 0. {
            self.is_follow_paused = true;
        }
        let handle = if self.is_expanded {
            &self.detail_scroll
        } else {
            &self.list_scroll
        };
        let offset = handle.offset();
        let max_offset = handle.max_offset();
        let y = (offset.y - px(delta)).clamp(-max_offset.y, px(0.));
        handle.set_offset(point(offset.x, y));
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
                    _ => this.show_message(outcome.message, outcome.is_error, cx),
                }
                this.reload(cx);
            });
        })
        .detach();
        cx.notify();
    }

    /// Keeps a failed run on its card, and toasts it only when the details pane isn't showing it.
    fn note_failure(
        &mut self,
        task_id: &str,
        action: Action,
        message: String,
        cx: &mut Context<Self>,
    ) {
        let row = self.snapshot.rows.iter().find(|row| row.task_id == task_id);
        let name = row.map_or(task_id, board::row_title).to_string();
        let is_in_view = self.is_expanded
            && self
                .selected_row()
                .is_some_and(|row| row.task_id == task_id);
        if !is_in_view {
            self.show_message(format!("{} for {name}", action.failed_text()), true, cx);
            if let Some(toast) = self.toast.as_mut() {
                toast.task_id = Some(task_id.to_string());
            }
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
        self.tab = column;
        self.select(root_id, cx);
        self.set_expanded(true, cx);
    }

    fn perform(&mut self, action: Action, row: &Row, cx: &mut Context<Self>) {
        let name = board::row_title(row).to_string();
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
                    Action::Pickup => format!("Picked up {name}"),
                    Action::Retry => format!("Retrying {name}"),
                    Action::RerunGate => format!("Gate passed for {name}; follow-ups started"),
                    Action::Close => format!("Closed {name}"),
                    Action::Cancel => format!("Cancelled {name}"),
                    Action::Dismiss => format!("Dismissed {name}"),
                    _ => String::new(),
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
                        outcome_or(outcome, done_text)
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
                            "Close failed for {}: {failure}",
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
            self.selected_row().map(|_| {
                let label = if self.is_expanded {
                    "Hide details"
                } else {
                    "Show details"
                };
                (
                    Command::ToggleDetails,
                    label.to_string(),
                    if self.is_expanded { "esc" } else { "\u{23ce}" },
                )
            }),
            self.selected_row().map(|_| {
                let label = if self.is_showing_fields && self.is_expanded {
                    "Hide session info"
                } else {
                    "Show session info"
                };
                (Command::ToggleSession, label.to_string(), "I")
            }),
            Some((Command::Filter, "Filter".to_string(), "/")),
            Some((Command::Help, "Keyboard shortcuts".to_string(), "?")),
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
            Command::Filter => self.input = Input::Filter,
            Command::Help => self.set_help(true, cx),
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
                "k" if self.pending.is_none() => {
                    let palette = if self.palette.is_some() {
                        None
                    } else {
                        Some(Palette::default())
                    };
                    self.set_palette(palette, cx);
                    self.set_help(false, cx);
                }
                "w" => self.hide(cx),
                "q" => cx.quit(),
                "v" => self.paste(cx),
                KEY_BACKSPACE => self.clear_input(),
                _ => return,
            }
            cx.notify();
            return;
        }
        if modifiers.control {
            match key {
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
        } else if self.pending.is_some() {
            match (key, typed.as_str()) {
                (KEY_ENTER, _) | (_, "y") => self.answer(true, cx),
                (KEY_ESCAPE, _) | (_, "n") => self.answer(false, cx),
                _ => {}
            }
        } else if self.is_showing_help {
            if matches!(key, KEY_ESCAPE | KEY_ENTER) || matches!(typed.as_str(), "?" | "q") {
                self.set_help(false, cx);
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
            KEY_BACKSPACE => {
                palette.query.pop();
                palette.index = 0;
                self.palette_scroll.scroll_to_item(0);
            }
            _ if is_printable(typed) => {
                palette.query.push_str(typed);
                palette.index = 0;
                self.palette_scroll.scroll_to_item(0);
            }
            _ => {}
        }
    }

    fn handle_filter_key(&mut self, key: &str, typed: &str, cx: &mut Context<Self>) {
        match key {
            KEY_ESCAPE => {
                self.query.clear();
                self.input = Input::None;
            }
            KEY_ENTER | KEY_DOWN | KEY_UP => self.input = Input::None,
            KEY_BACKSPACE => {
                self.query.pop();
            }
            _ if is_printable(typed) => self.query.push_str(typed),
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
            palette.query.push_str(&text);
            palette.index = 0;
            return;
        }
        if self.pending.is_some() || self.is_showing_help {
            return;
        }
        self.input = Input::Filter;
        self.query.push_str(&text);
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
            (_, "t") => {
                self.activity_level = self.activity_level.next();
                self.is_following = false;
                if !self.is_expanded {
                    self.set_expanded(true, cx);
                }
            }
            (_, "e") => {
                self.is_showing_prompt = !self.is_showing_prompt || self.is_following;
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
                    .is_some_and(|toast| toast.task_id.is_some()) =>
            {
                self.open_toast_task(cx);
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

/// The palette list's child position for an item, counting the group headings rendered before it.
fn palette_child_index(items: &[PaletteItem], index: usize) -> usize {
    index + palette_heading_count(&items[..=index])
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

/// Whether a prompt is long enough to clamp to three lines in the details pane.
fn is_long_prompt(prompt: &str) -> bool {
    let prompt = prompt.trim();
    prompt.lines().count() > 3 || prompt.chars().count() > LONG_PROMPT_CHARS
}

fn sentence_case(text: &str) -> String {
    let mut chars = text.chars();
    chars.next().map_or_else(String::new, |first| {
        first.to_uppercase().chain(chars).collect()
    })
}

fn is_printable(typed: &str) -> bool {
    !typed.is_empty() && typed.chars().all(|char| !char.is_control())
}

// ---- rendering ------------------------------------------------------------------------------

impl BoardView {
    fn render_header(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let is_filtering = self.input == Input::Filter;
        let filter_text: AnyElement = if self.query.is_empty() && !is_filtering {
            div()
                .text_color(theme.text_3)
                .child("Filter")
                .into_any_element()
        } else {
            div()
                .flex()
                .items_center()
                .min_w_0()
                .child(div().truncate().child(self.query.clone()))
                .when(is_filtering, |this| {
                    this.child(div().w(px(1.5)).h(px(15.)).bg(theme.accent))
                })
                .into_any_element()
        };
        let needing_count = self
            .tabs()
            .into_iter()
            .flat_map(|column| self.column_rows(column))
            .filter(|row| self.needs_you(row))
            .count();
        let logo = div()
            .size(px(22.))
            .flex_none()
            .rounded(px(6.))
            .bg(theme.accent)
            .flex()
            .items_center()
            .justify_center()
            .child(icon(theme::ICON_LOGO, 15., theme.on_accent));
        div()
            .flex()
            .flex_col()
            .gap(px(6.))
            .px(px(10.))
            .py(px(9.))
            .border_b_1()
            .border_color(theme.line)
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(7.))
                    .child(logo)
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
                                this.bg(theme.accent_soft)
                                    .text_color(theme.accent)
                                    .hover(|style| style.opacity(0.85))
                            })
                            .on_click(
                                cx.listener(|this, _: &ClickEvent, _, cx| this.next_attention(cx)),
                            )
                            .child("Next")
                            .when(needing_count > 0, |this| {
                                this.child(
                                    div()
                                        .font_weight(FontWeight::BOLD)
                                        .child(needing_count.to_string()),
                                )
                            })
                            .child(kbd("N", theme)),
                    ),
            )
            .when_some(self.load_error.clone(), |this, error| {
                this.child(
                    div()
                        .line_clamp(2)
                        .text_size(px(11.5))
                        .text_color(theme.orange)
                        .child(format!(
                            "Can't read the ledger, showing the last good state: {error}"
                        )),
                )
            })
            .into_any_element()
    }

    fn render_tabs(&self, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let tabs = self.tabs().into_iter().map(|column| {
            let rows = self.column_rows(column);
            let is_current = column == self.tab;
            let has_attention = rows.iter().any(|row| self.needs_you(row));
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
                        color: theme.line,
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
                .when(has_attention && !is_current, |this| {
                    this.child(div().size(px(6.)).rounded_full().bg(theme.accent))
                })
        });
        let container = div().flex().flex_col().child(
            div()
                .flex()
                .gap(px(1.))
                .mx(px(10.))
                .mt(px(9.))
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
        let now = Local::now();
        let task = &self.snapshot.tasks[&row.task_id];
        let is_stuck = board::is_stuck(row, task, now);
        let busy_action = self.busy_actions.get(&row.task_id).copied();
        let action_error = self.action_errors.get(&row.task_id);
        let is_changed = self.changed_until.contains_key(&row.root_id);
        let state_color = match busy_action {
            Some(_) => theme.text_3,
            None => theme.status_color(row.status, is_stuck),
        };
        let when = board::card_when_text(row, task, now);
        let (snippet, snippet_color) = match (busy_action, action_error) {
            (Some(action), _) => (action.busy_text().to_string(), theme.accent),
            (None, Some(error)) => (error.action.failed_text().to_string(), theme.red),
            (None, None) => (board::card_snippet(row, &self.snapshot), theme.text_2),
        };
        let is_code = snippet.starts_with(board::SHELL_PROMPT)
            || (busy_action.is_none()
                && action_error.is_none()
                && board::is_tool_snippet(row, &self.snapshot));
        let quick_action =
            (is_selected && !self.is_expanded && busy_action.is_none() && self.tab != Column::Done)
                .then(|| {
                    let actions = board::available_actions(&self.snapshot, Some(row));
                    board::primary_action(&self.snapshot, row, &actions)
                })
                .flatten();
        let root_id = row.root_id.clone();
        div()
            .id(SharedString::from(format!("card-{}", row.root_id)))
            .flex()
            .flex_col()
            .gap(px(2.))
            .pl(px(10.))
            .pr(px(11.))
            .py(px(8.))
            .rounded(px(8.))
            .border_1()
            .map(|this| match (is_selected, is_changed) {
                (true, _) => this.bg(theme.accent_soft).border_color(theme.accent),
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
                            .font_weight(if self.needs_you(row) {
                                FontWeight::SEMIBOLD
                            } else {
                                FontWeight::MEDIUM
                            })
                            .child(board::row_title(row).to_string()),
                    )
                    .when(!row.step.is_empty(), |this| {
                        this.child(
                            div()
                                .flex_none()
                                .px(px(5.))
                                .rounded(px(4.))
                                .bg(theme.fill)
                                .text_size(px(10.5))
                                .font_weight(FontWeight::SEMIBOLD)
                                .text_color(theme.text_2)
                                .child(row.step.clone()),
                        )
                    })
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
                    .flex()
                    .items_center()
                    .gap(px(6.))
                    .pl(px(21.))
                    .min_w_0()
                    .text_size(px(12.))
                    .child(
                        div()
                            .flex_none()
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
                    .children(
                        quick_action.map(|action| Self::quick_action_button(action, theme, cx)),
                    ),
            )
    }

    /// The selected card's primary action in the collapsed panel, so it runs without opening details.
    fn quick_action_button(action: Action, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        div()
            .id("quick-action")
            .flex_none()
            .flex()
            .items_center()
            .gap(px(5.))
            .h(px(20.))
            .my(px(-2.))
            .pl(px(8.))
            .pr(px(3.))
            .rounded(px(5.))
            .bg(theme.accent_fill)
            .text_color(theme.on_accent)
            .text_size(px(11.5))
            .font_weight(FontWeight::SEMIBOLD)
            .hover(|style| style.opacity(0.9))
            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                cx.stop_propagation();
                this.request(action, cx);
            }))
            .child(action.label())
            .child(
                kbd(&key_label(action), theme)
                    .h(px(14.))
                    .bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                    .text_color(theme.on_accent),
            )
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
            let text = if self.query.is_empty() {
                self.tab.empty_text()
            } else {
                "No matches"
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
    ) -> AnyElement {
        let now = Local::now();
        let task = self.snapshot.tasks[&row.task_id].clone();
        let mut body = div().flex().flex_col();
        let stop_reason = task
            .gate_stop_reason
            .clone()
            .or(task.setup_failure_reason.clone())
            .unwrap_or_default();
        if !stop_reason.is_empty() {
            let label = match (&task.gate_stop_reason, &task.gate) {
                (Some(_), Some(gate)) => format!("Gate failed: {gate}"),
                (Some(_), None) => "Gate failed".to_string(),
                _ => "Setup failed".to_string(),
            };
            let color = if row.status == Status::Blocked {
                theme.orange
            } else {
                theme.red
            };
            body = body.child(Self::callout(&label, &stop_reason, color, true, theme));
        }
        if let Some(error) = self.action_errors.get(&row.task_id) {
            body = body.child(Self::callout(
                error.action.failed_text(),
                &sentence_case(&error.message),
                theme.red,
                false,
                theme,
            ));
        }
        if board::is_stuck(row, &task, now) {
            body = body.child(Self::callout(
                "Stuck",
                &board::waiting_reason_text(row, &self.snapshot),
                theme.orange,
                false,
                theme,
            ));
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
                    .child(div().child(board::plain_result(result)))
                    .into_any_element()
            });
        let pull_requests = self.render_pull_requests(row, theme, cx);
        let chain = self.render_chain(row, theme);
        let prompt = self.render_prompt(&task, theme, cx);
        let activity = self.render_activity(row, theme, cx);
        let session = self
            .is_showing_fields
            .then(|| self.render_session(row, now, theme));
        let sections = if self.is_following {
            vec![Some(activity)]
        } else if row.status == Status::Running {
            vec![Some(activity), chain, prompt, session]
        } else {
            vec![
                result,
                pull_requests,
                chain,
                prompt,
                Some(activity),
                session,
            ]
        };
        body.children(sections.into_iter().flatten())
            .into_any_element()
    }

    fn render_pull_requests(
        &self,
        row: &Row,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Option<AnyElement> {
        let pull_requests = board::row_pull_requests(&self.snapshot, row);
        if pull_requests.is_empty() {
            return None;
        }
        let mut list = div().flex().flex_col().gap(px(3.)).text_size(px(12.5));
        for pull_request in pull_requests {
            let url = pull_request.url.clone();
            let is_open = pull_request.state == "open";
            list = list.child(
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
                    .child(
                        div()
                            .font_weight(FontWeight::BOLD)
                            .child(board::pull_request_number(&pull_request)),
                    )
                    .child(
                        div()
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(if is_open { theme.green } else { theme.gray })
                            .child(pull_request.state.clone()),
                    )
                    .child(
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
                    .child(div().w(px(42.)).flex_none().text_color(theme.text_3).child(
                        if index < shown_index {
                            "Before"
                        } else {
                            "Then"
                        },
                    ))
                    .child(icon(
                        theme::status_icon(step.status),
                        11.,
                        theme.status_color(step.status, false),
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
        let is_showing_prompt = self.is_showing_prompt;
        let is_long = is_long_prompt(prompt);
        Some(
            Self::section("Prompt", theme)
                .child(
                    div()
                        .text_color(theme.text_2)
                        .when(is_long && !is_showing_prompt, |this| this.line_clamp(3))
                        .child(prompt.to_string()),
                )
                .when(is_long, |this| {
                    this.child(
                        div()
                            .id("prompt-toggle")
                            .flex()
                            .items_center()
                            .gap(px(6.))
                            .text_size(px(12.))
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(theme.accent)
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                this.is_showing_prompt = !this.is_showing_prompt;
                                cx.notify();
                            }))
                            .child(if is_showing_prompt {
                                "Show less"
                            } else {
                                "Show more"
                            })
                            .child(kbd("E", theme)),
                    )
                })
                .into_any_element(),
        )
    }

    fn render_session(&self, row: &Row, now: DateTime<Local>, theme: &Theme) -> AnyElement {
        let mut grid = div().flex().flex_col().gap(px(4.)).text_size(px(12.5));
        let task = &self.snapshot.tasks[&row.task_id];
        let is_gate_in_callout = task.gate_stop_reason.is_some();
        let fields = board::task_fields(row, &self.snapshot, now)
            .into_iter()
            .filter(|(label, value)| !(*label == "Backend" && *value == task.backend))
            .filter(|(label, _)| !(*label == "Gate" && is_gate_in_callout));
        for (label, value) in fields {
            let is_code = matches!(
                label,
                "Directory" | "Worktree" | "Session" | "Gate" | "Setup"
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
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .when(is_code, |this| {
                                this.font_family(MONO_FONT).text_size(px(12.))
                            })
                            .child(value),
                    ),
            );
        }
        Self::section("Session", theme)
            .child(grid)
            .into_any_element()
    }

    fn render_activity(&mut self, row: &Row, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        let lines = self.selected_activity();
        let segments = ActivityLevel::ALL.into_iter().map(|level| {
            let is_pressed = level == self.activity_level;
            div()
                .id(level.label())
                .px(px(8.))
                .py(px(1.))
                .rounded(px(5.))
                .text_size(px(11.))
                .font_weight(FontWeight::MEDIUM)
                .text_color(if is_pressed { theme.text } else { theme.text_2 })
                .when(is_pressed, |this| this.bg(theme.raised))
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                    this.activity_level = level;
                    this.is_following = false;
                    cx.notify();
                }))
                .child(level.label())
        });
        let live = self.is_following.then(|| {
            let (color, label) = match (self.is_follow_paused, row.status) {
                (true, _) => (theme.text_2, "Paused"),
                (false, Status::Running) => (theme.accent, "Live"),
                (false, _) => (theme.accent, "Full log"),
            };
            div()
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
            .children(live)
            .when(!self.is_following, |this| {
                this.child(
                    div()
                        .flex()
                        .p(px(1.))
                        .rounded(px(6.))
                        .bg(theme.fill)
                        .children(segments),
                )
            });
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
            self.activity_level
        };
        let items = board::activity_view(&lines, level);
        let earlier_count = match level {
            ActivityLevel::Messages => items.len().saturating_sub(MESSAGE_ITEM_LIMIT),
            _ => 0,
        };
        if earlier_count > 0 {
            log = log.child(
                div()
                    .py(px(2.))
                    .text_color(theme.text_3)
                    .child(format!("{earlier_count} earlier, shown in All")),
            );
        }
        for item in items.into_iter().skip(earlier_count) {
            log = log.child(match item {
                ActivityItem::Message(text) => div().py(px(2.)).child(text),
                ActivityItem::Tool(text) => div()
                    .py(px(2.))
                    .pl(px(12.))
                    .font_family(MONO_FONT)
                    .text_size(px(12.))
                    .text_color(theme.text_2)
                    .child(text),
                ActivityItem::Fold(text) => div()
                    .py(px(2.))
                    .pl(px(12.))
                    .text_size(px(12.))
                    .text_color(theme.text_3)
                    .child(text),
            });
        }
        div()
            .flex()
            .flex_col()
            .mb(px(12.))
            .child(header)
            .child(log)
            .into_any_element()
    }

    fn callout(label: &str, text: &str, color: Hsla, is_code: bool, theme: &Theme) -> Div {
        div()
            .mb(px(12.))
            .px(px(12.))
            .py(px(9.))
            .rounded(px(8.))
            .bg(theme.raised)
            .border_l(px(3.))
            .border_color(color)
            .child(
                div()
                    .mb(px(3.))
                    .text_size(px(10.5))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_2)
                    .child(label.to_uppercase()),
            )
            .child(
                div()
                    .when(is_code, |this| {
                        this.font_family(MONO_FONT).text_size(px(12.))
                    })
                    .child(text.to_string()),
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
        let now = Local::now();
        let task = self.snapshot.tasks[&row.task_id].clone();
        let is_stuck = board::is_stuck(&row, &task, now);
        let state_color = theme.status_color(row.status, is_stuck);
        let mut sub_parts =
            vec![board::card_when_text(&row, &task, now).replacen("stuck ", "overdue ", 1)];
        sub_parts.push(board::start_text(&row, &task, now));
        if !row.step.is_empty() {
            sub_parts.push(format!("step {}", row.step.replace('/', " of ")));
        }
        if let Some(branch) = board::branch_name(&task) {
            sub_parts.push(branch.to_string());
        }
        sub_parts.push(task.backend.clone());
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
                    .child(icon(theme::status_icon(row.status), 13., state_color))
                    .child(if is_stuck { "stuck" } else { row.status.name() }),
            );
        for part in sub_parts {
            sub = sub
                .child(div().text_color(theme.text_3).child("\u{b7}"))
                .child(part);
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
                            .child(kbd("esc", theme))
                            .child(icon(theme::ICON_FOLD, 15., theme.text_2)),
                    ),
            )
            .child(
                div().flex().items_center().gap(px(8.)).mb(px(4.)).child(
                    div()
                        .flex_1()
                        .min_w_0()
                        .truncate()
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
            let is_primary = Some(action) == primary && !is_on;
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
                .pl(px(10.))
                .pr(px(6.))
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
                .child(key_chip)
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
                                if this.is_following && !this.is_follow_paused && is_scrolling_up {
                                    this.is_follow_paused = true;
                                    cx.notify();
                                }
                            }))
                            .pl(px(18.))
                            .pr(px(18.))
                            .pt(px(2.))
                            .pb(px(16.))
                            .child(body),
                    )
                    .child(div().absolute().bottom_0().left_0().w_full().h(px(20.)).bg(
                        linear_gradient(
                            180.,
                            linear_color_stop(
                                Hsla {
                                    a: 0.,
                                    ..theme.panel
                                },
                                0.,
                            ),
                            linear_color_stop(theme.panel, 1.),
                        ),
                    )),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .items_center()
                    .gap(px(6.))
                    .pl(px(18.))
                    .pr(px(12.))
                    .pt(px(9.))
                    .pb(px(10.))
                    .min_h(px(47.))
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
            .h(px(36.))
            .px(px(7.))
            .border_t_1()
            .border_color(theme.line)
            .child(
                Self::footer_button("toggle-done", theme)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| this.toggle_done(cx)))
                    .child(done_label)
                    .child(kbd("A", theme)),
            )
            .child(div().flex_1())
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
                    .child("Keys")
                    .child(kbd("?", theme)),
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
        let decline = if pending.action == Action::Cancel {
            "Keep"
        } else {
            "Cancel"
        };
        self.scrim(theme)
            .id("confirm")
            .occlude()
            .child(
                Self::sheet(theme, 340.)
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
                                    "\u{23ce}",
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
                ("\u{2191}\u{2193} J K", "select"),
                ("\u{2190}\u{2192} H L", "switch group"),
                ("tab \u{21e7}tab", "next group"),
                ("\u{2303}N \u{2303}P", "select in search"),
                ("1-4", "jump to group"),
                ("G \u{21e7}G", "first, last"),
                ("N", "next needing you"),
                ("\u{23ce}", "details"),
                ("esc", "back"),
                ("space \u{2303}D", "scroll down"),
                ("\u{2303}U", "scroll up"),
            ],
        );
        let board_keys = section(
            "Board",
            60.,
            &[
                ("/", "filter"),
                ("A", "show Done"),
                ("\u{21e7}C", "close finished"),
                ("\u{2318}K", "all actions"),
                ("Q \u{2318}W", "close panel"),
                ("\u{2303}\u{2325}G", "open panel"),
            ],
        );
        let task_keys = section(
            "Task",
            60.,
            &[
                ("P", "pick up"),
                ("W", "follow log"),
                ("O", "open PR"),
                ("R", "retry, recheck"),
                ("C", "close"),
                ("X", "cancel, dismiss"),
                ("Y", "copy ID"),
                ("E", "full prompt"),
                ("I", "session"),
                ("T", "activity level"),
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
                    theme.status_color(status, false),
                ))
                .child(status.name())
        });
        let chain_chip = div()
            .flex()
            .items_center()
            .gap(px(4.))
            .text_size(px(12.))
            .child(
                div()
                    .px(px(5.))
                    .rounded(px(4.))
                    .bg(theme.fill)
                    .text_size(px(10.5))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(theme.text_2)
                    .child("1/2"),
            )
            .child("chain step");
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
                    .children(legend)
                    .child(chain_chip),
            );
        let [move_column, task_column] = columns;
        let width = (self.panel_width() as f32 - 24.).min(640.);
        self.scrim(theme)
            .id("help")
            .occlude()
            .child(
                Self::sheet(theme, width)
                    .child(
                        div()
                            .text_size(px(14.))
                            .font_weight(FontWeight::BOLD)
                            .child("Keyboard shortcuts"),
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
                    )
                    .child(
                        div().flex().mt(px(14.)).child(
                            Self::sheet_button(
                                "help-ok",
                                "OK".to_string(),
                                "esc",
                                false,
                                false,
                                theme,
                            )
                            .on_click(cx.listener(
                                |this, _: &ClickEvent, _, cx| {
                                    this.set_help(false, cx);
                                    cx.notify();
                                },
                            )),
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
        let current = palette.map_or(0, |palette| {
            palette.index.min(items.len().saturating_sub(1))
        });
        let list_height = (self.panel_height() as f32 - PALETTE_CHROME_HEIGHT).min(420.);
        let heading_count = palette_heading_count(&items);
        let content_height =
            items.len() as f32 * PALETTE_ROW_HEIGHT + heading_count as f32 * PALETTE_HEADING_HEIGHT;
        let is_overflowing = content_height > list_height;
        let mut list = div()
            .id("palette-list")
            .max_h(px(list_height))
            .overflow_y_scroll()
            .track_scroll(&self.palette_scroll)
            .px(px(5.))
            .pt(px(4.))
            .pb(px(6.));
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
                    .child(kbd(&item.key, theme).when(is_current, |this| {
                        this.bg(gpui::rgba(FILLED_KEYCAP_BACKGROUND))
                            .text_color(theme.on_accent)
                    })),
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
            div()
                .flex()
                .items_center()
                .child(query)
                .child(div().w(px(1.5)).h(px(16.)).bg(theme.accent))
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
    fn render_toast(&self, toast: &Toast, theme: &Theme, cx: &mut Context<Self>) -> AnyElement {
        div()
            .absolute()
            .bottom(px(5.))
            .left_0()
            .w_full()
            .px(px(8.))
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
                    .shadow(vec![BoxShadow {
                        color: gpui::rgba(0x00000040).into(),
                        offset: point(px(0.), px(4.)),
                        blur_radius: px(14.),
                        spread_radius: px(0.),
                        inset: false,
                    }])
                    .text_size(px(12.))
                    .font_weight(FontWeight::MEDIUM)
                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                        this.open_toast_task(cx);
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
                    .when(toast.task_id.is_some(), |this| {
                        this.child(
                            div()
                                .flex_none()
                                .text_color(theme.accent)
                                .font_weight(FontWeight::SEMIBOLD)
                                .child("Details"),
                        )
                        .child(kbd("D", theme))
                    }),
            )
            .into_any_element()
    }
}

impl Render for BoardView {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let theme = Theme::for_appearance(window.appearance());
        if !self.tabs().contains(&self.tab) {
            self.tab = Column::Review;
        }
        let side = div()
            .flex()
            .flex_col()
            .min_h_0()
            .min_w_0()
            .when(self.is_expanded, |this| {
                this.w(px(SIDE_WIDTH))
                    .flex_none()
                    .border_r_1()
                    .border_color(theme.line)
            })
            .when(!self.is_expanded, |this| this.flex_1())
            .child(self.render_header(&theme, cx))
            .child(self.render_tabs(&theme, cx))
            .child(self.render_list(&theme, cx));
        let mut body = div().flex_1().min_h_0().flex().child(side);
        if self.is_expanded {
            body = body.child(self.render_detail(&theme, cx));
        }
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
            .child(body)
            .child(self.render_footer(&theme, cx));
        if let Some(toast) = &self.toast {
            root = root.child(self.render_toast(toast, &theme, cx));
        }
        if self.palette.is_some() {
            root = root.child(self.render_palette(&theme, cx));
        }
        if let Some(pending) = &self.pending {
            root = root.child(self.render_confirm(pending, &theme, cx));
        } else if self.is_showing_help {
            root = root.child(self.render_help(&theme, cx));
        }
        root
    }
}
