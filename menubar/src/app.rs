use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::Local;
use futures::StreamExt;
use futures::channel::mpsc::UnboundedReceiver;
use gpui::{
    AnyElement, BoxShadow, ClickEvent, ClipboardItem, Context, Div, FocusHandle, FontWeight, Hsla,
    KeyDownEvent, ScrollHandle, SharedString, Stateful, Subscription, Task, Window, div, point,
    prelude::*, px, svg,
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
const CARD_PITCH: f64 = 66.0;
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
const LONG_PROMPT_CHARS: usize = 240;
const NOTHING_NEEDS_YOU_TEXT: &str = "Nothing needs you right now";
const GOLEM_MISSING_TEXT: &str = "Still looking for golem";
const TERMINAL_BACKENDS: [&str; 2] = ["claude", "codex"];
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
}

/// The result of a background `golem` run: the toast text and whether it reports a failure.
struct Outcome {
    message: String,
    is_error: bool,
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
    is_showing_fields: bool,
    is_showing_prompt: bool,
    is_showing_activity: bool,
    pending: Option<Pending>,
    is_showing_help: bool,
    palette: Option<Palette>,
    toast: Option<Toast>,
    toast_count: u64,
    busy_actions: HashMap<String, Action>,
    action_errors: HashMap<String, String>,
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
            is_showing_fields: false,
            is_showing_prompt: false,
            is_showing_activity: true,
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
        if self.is_following {
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
        if self.is_expanded || self.is_showing_help || self.palette.is_some() {
            return PANEL_HEIGHT;
        }
        let longest = self
            .tabs()
            .into_iter()
            .map(|column| self.column_rows(column).len())
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

    /// The palette needs the full panel height, so the panel grows while it shows.
    fn set_palette(&mut self, palette: Option<Palette>, cx: &mut Context<Self>) {
        let is_resizing = palette.is_some() != self.palette.is_some();
        self.palette = palette;
        if is_resizing {
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

    fn scroll_by(&self, delta: f32) {
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

    fn toggle_follow(&mut self, cx: &mut Context<Self>) {
        self.is_following = !self.is_following;
        if self.is_following {
            self.activity_level = ActivityLevel::All;
            self.is_showing_activity = true;
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
                    if outcome.is_error {
                        this.action_errors
                            .insert(task_id.clone(), outcome.message.clone());
                    }
                }
                this.show_message(outcome.message, outcome.is_error, cx);
                this.reload(cx);
            });
        })
        .detach();
        cx.notify();
    }

    fn perform(&mut self, action: Action, row: &Row, cx: &mut Context<Self>) {
        let name = board::row_title(row).to_string();
        let task_id = row.task_id.clone();
        if let Some(busy_action) = self.busy_actions.get(&task_id) {
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
                        let mut result = outcome_or(outcome, done_text);
                        if action != Action::RerunGate && result.is_error {
                            result.message = format!(
                                "Couldn't {} {name}: {}",
                                action.label().to_lowercase(),
                                result.message
                            );
                        }
                        if action == Action::RerunGate && result.is_error {
                            result.message =
                                format!("Gate still failing for {name}: {}", result.message);
                        }
                        result
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
                        message: failure.clone(),
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
                    Action::Pickup => "Pick up session".to_string(),
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
                _ => return,
            }
            cx.notify();
            return;
        }
        if modifiers.control {
            match key {
                "d" => self.scroll_by(SCROLL_STEP),
                "u" => self.scroll_by(-SCROLL_STEP),
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
            }
            KEY_ENTER => {
                if let Some(item) = items.get(palette.index.min(items.len().saturating_sub(1))) {
                    self.run_command(item.command, window, cx);
                }
            }
            KEY_BACKSPACE => {
                palette.query.pop();
                palette.index = 0;
            }
            _ if is_printable(typed) => {
                palette.query.push_str(typed);
                palette.index = 0;
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
            KEY_ENTER | KEY_DOWN => self.input = Input::None,
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
                self.is_showing_activity = true;
                self.is_following = false;
                if !self.is_expanded {
                    self.set_expanded(true, cx);
                }
            }
            (_, "e") => {
                self.is_showing_prompt = !self.is_showing_prompt;
                if !self.is_expanded {
                    self.set_expanded(true, cx);
                }
            }
            (_, "i") => {
                self.is_showing_fields = !self.is_showing_fields;
                if !self.is_expanded {
                    self.set_expanded(true, cx);
                }
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

/// Whether a prompt is long enough to clamp to three lines in the details pane.
fn is_long_prompt(prompt: &str) -> bool {
    let prompt = prompt.trim();
    prompt.lines().count() > 3 || prompt.chars().count() > LONG_PROMPT_CHARS
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
                            .bg(theme.fill)
                            .font_weight(FontWeight::MEDIUM)
                            .hover(|style| style.bg(theme.hover))
                            .on_click(
                                cx.listener(|this, _: &ClickEvent, _, cx| this.next_attention(cx)),
                            )
                            .child("Next")
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
                .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| this.set_tab(column, cx)))
                .child(column.title())
                .child(
                    div()
                        .text_size(px(11.5))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(theme.text_3)
                        .child(rows.len().to_string()),
                )
                .when(has_attention, |this| {
                    this.child(div().size(px(6.)).rounded_full().bg(theme.orange))
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
            (None, Some(error)) => (
                error.replacen(&format!(" {}:", board::row_title(row)), ":", 1),
                theme.red,
            ),
            (None, None) => (board::card_snippet(row, &self.snapshot), theme.text_2),
        };
        let is_code =
            snippet.starts_with(board::SHELL_PROMPT) || snippet.starts_with(board::TOOL_MARK);
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
                (false, true) => this.bg(theme.changed).border_color(theme.line),
                (false, false) => this
                    .bg(theme.raised)
                    .border_color(theme.line)
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
                    .child(icon(theme::status_icon(row.status), 14., state_color))
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
                    ),
            )
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
            .gap(px(5.));
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

    fn disclosure(
        id: &'static str,
        title: &str,
        is_open: bool,
        meta: String,
        trailing: Vec<AnyElement>,
        theme: &Theme,
        cx: &mut Context<Self>,
    ) -> Div {
        div()
            .border_t_1()
            .border_color(theme.line)
            .pt(px(7.))
            .pb(px(2.))
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap(px(8.))
                    .min_h(px(26.))
                    .child(
                        div()
                            .id(id)
                            .flex()
                            .flex_none()
                            .items_center()
                            .gap(px(6.))
                            .h(px(26.))
                            .pl(px(2.))
                            .pr(px(6.))
                            .rounded(px(6.))
                            .font_weight(FontWeight::SEMIBOLD)
                            .hover(|style| style.bg(theme.hover))
                            .on_click(cx.listener(move |this, _: &ClickEvent, _, cx| {
                                if id == "fields" {
                                    this.is_showing_fields = !this.is_showing_fields;
                                } else {
                                    this.is_showing_activity = !this.is_showing_activity;
                                }
                                cx.notify();
                            }))
                            .child(icon(theme::ICON_CHEVRON, 10., theme.text_2).when(
                                is_open,
                                |this| {
                                    this.with_transformation(gpui::Transformation::rotate(
                                        gpui::percentage(0.25),
                                    ))
                                },
                            ))
                            .child(title.to_string()),
                    )
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_size(px(12.))
                            .text_color(theme.text_3)
                            .child(meta),
                    )
                    .children(trailing),
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
            body = body.child(self.callout(
                &label,
                &stop_reason,
                row.status == Status::Blocked,
                theme,
            ));
        }
        if let Some(error) = self.action_errors.get(&row.task_id) {
            body = body.child(self.callout("Last action failed", error, false, theme));
        }
        if board::is_stuck(row, &task, now) {
            body = body.child(self.callout(
                "Stuck",
                &board::waiting_reason_text(row, &self.snapshot),
                true,
                theme,
            ));
        }
        if let Some(result) = task
            .result
            .as_deref()
            .filter(|result| !result.trim().is_empty())
        {
            let title = if row.status == Status::Failed {
                "Stop reason"
            } else {
                "Result"
            };
            body = body
                .child(Self::section(title, theme).child(div().child(board::plain_result(result))));
        }
        let pull_requests = board::row_pull_requests(&self.snapshot, row);
        if !pull_requests.is_empty() {
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
                                .flex_1()
                                .min_w_0()
                                .truncate()
                                .font_family(MONO_FONT)
                                .text_size(px(11.5))
                                .text_color(theme.text_3)
                                .child(pull_request.url.trim_start_matches("https://").to_string()),
                        ),
                );
            }
            body = body.child(Self::section("Pull requests", theme).child(list));
        }
        if row.chain_ids.len() > 1 {
            let steps = board::chain_steps(row, &self.snapshot.tasks);
            let mut chain = div()
                .flex()
                .flex_wrap()
                .items_center()
                .gap(px(5.))
                .text_size(px(12.))
                .text_color(theme.text_2);
            for (index, step) in steps.iter().enumerate() {
                if index > 0 {
                    chain = chain.child("\u{2192}");
                }
                let text = if step.is_inferable {
                    step.name.clone()
                } else {
                    format!("{} \u{b7} {}", step.name, step.status.name())
                };
                chain = chain.child(
                    div()
                        .flex()
                        .items_center()
                        .gap(px(4.))
                        .pl(px(6.))
                        .pr(px(8.))
                        .py(px(2.))
                        .rounded(px(10.))
                        .bg(theme.fill)
                        .when(step.is_shown, |this| {
                            this.text_color(theme.text)
                                .font_weight(FontWeight::SEMIBOLD)
                                .border_1()
                                .border_color(theme.line)
                        })
                        .child(icon(
                            theme::status_icon(step.status),
                            11.,
                            theme.status_color(step.status, false),
                        ))
                        .child(text),
                );
            }
            body = body.child(Self::section("Chain", theme).child(chain));
        }
        if !task.prompt.trim().is_empty() {
            let is_showing_prompt = self.is_showing_prompt;
            let is_long = is_long_prompt(&task.prompt);
            body = body.child(
                Self::section("Prompt", theme)
                    .child(
                        div()
                            .text_color(theme.text_2)
                            .when(is_long && !is_showing_prompt, |this| this.line_clamp(3))
                            .child(task.prompt.trim().to_string()),
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
                    }),
            );
        }
        let fields_meta = if self.is_showing_fields {
            String::new()
        } else {
            task.backend.clone()
        };
        let mut fields = Self::disclosure(
            "fields",
            "Session",
            self.is_showing_fields,
            fields_meta,
            Vec::new(),
            theme,
            cx,
        );
        if self.is_showing_fields {
            let mut grid = div()
                .flex()
                .flex_col()
                .gap(px(4.))
                .ml(px(18.))
                .mt(px(4.))
                .mb(px(8.))
                .text_size(px(12.5));
            for (label, value) in board::task_fields(row, &self.snapshot, now) {
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
            fields = fields.child(grid);
        }
        body = body.child(fields);

        let lines = self.selected_activity();
        let mut trailing: Vec<AnyElement> = Vec::new();
        if self.is_following {
            let live = if row.status == Status::Running {
                "Live"
            } else {
                "Full log"
            };
            trailing.push(
                div()
                    .flex()
                    .items_center()
                    .gap(px(5.))
                    .text_color(theme.accent)
                    .text_size(px(12.))
                    .font_weight(FontWeight::SEMIBOLD)
                    .child(div().size(px(6.)).rounded_full().bg(theme.accent))
                    .child(live)
                    .into_any_element(),
            );
        }
        if self.is_showing_activity {
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
            trailing.push(
                div()
                    .flex()
                    .p(px(1.))
                    .rounded(px(6.))
                    .bg(theme.fill)
                    .children(segments)
                    .into_any_element(),
            );
        }
        let activity_meta = if self.is_showing_activity {
            String::new()
        } else {
            board::activity_counts(&lines)
        };
        let mut activity = Self::disclosure(
            "activity",
            "Activity",
            self.is_showing_activity,
            activity_meta,
            trailing,
            theme,
            cx,
        );
        if self.is_showing_activity {
            let mut log = div()
                .flex()
                .flex_col()
                .ml(px(18.))
                .mt(px(3.))
                .mb(px(6.))
                .text_size(px(12.5));
            if lines.is_empty() {
                let text = if row.status == Status::Running {
                    "No activity yet"
                } else {
                    "No activity"
                };
                log = log.child(div().text_color(theme.text_3).child(text));
            }
            for (index, item) in board::activity_view(&lines, self.activity_level)
                .into_iter()
                .enumerate()
            {
                log = log.child(match item {
                    ActivityItem::Message(text) => div().py(px(2.)).child(text).into_any_element(),
                    ActivityItem::Tool(text) => div()
                        .py(px(2.))
                        .pl(px(12.))
                        .font_family(MONO_FONT)
                        .text_size(px(12.))
                        .text_color(theme.text_2)
                        .child(text)
                        .into_any_element(),
                    ActivityItem::Fold(text) => div()
                        .id(("fold", index))
                        .py(px(2.))
                        .text_color(theme.text_2)
                        .hover(|style| style.text_color(theme.accent))
                        .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                            this.activity_level = this.activity_level.next();
                            cx.notify();
                        }))
                        .child(format!("+ {text}"))
                        .into_any_element(),
                });
            }
            activity = activity.child(log);
        }
        body.child(activity).into_any_element()
    }

    fn callout(&self, label: &str, text: &str, is_warning: bool, theme: &Theme) -> Div {
        div()
            .mb(px(12.))
            .px(px(12.))
            .py(px(9.))
            .rounded(px(8.))
            .bg(theme.raised)
            .border_l(px(3.))
            .border_color(if is_warning { theme.orange } else { theme.red })
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
                    .font_family(MONO_FONT)
                    .text_size(px(12.))
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
        if let Some(branch) = board::branch_name(&task) {
            sub_parts.push(branch.to_string());
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
                            .size(px(24.))
                            .flex()
                            .items_center()
                            .justify_center()
                            .rounded(px(6.))
                            .hover(|style| style.bg(theme.hover))
                            .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                this.set_expanded(false, cx)
                            }))
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
            .position(|action| matches!(action, Action::Close | Action::Cancel));
        let buttons = actions.into_iter().enumerate().map(|(index, action)| {
            let is_on = action == Action::Watch && self.is_following;
            let label = match action {
                _ if busy_action == Some(action) => action.busy_text(),
                Action::Watch if is_on => "Following",
                _ => action.label(),
            };
            let is_primary = Some(action) == primary && !is_on;
            let is_danger = action == Action::Cancel;
            let (background, foreground) = if is_primary {
                (theme.accent, theme.on_accent)
            } else if is_on {
                (theme.accent_soft, theme.accent)
            } else if is_danger {
                (theme.fill, theme.red)
            } else {
                (theme.fill, theme.text)
            };
            let key_chip = kbd(&key_label(action), theme).when(is_primary, |this| {
                this.bg(gpui::rgba(0xffffff38)).text_color(theme.on_accent)
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
                .when(busy_action.is_some(), |this| this.opacity(0.55))
                .when(busy_action.is_none(), |this| {
                    this.hover(|style| style.opacity(0.9))
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
                    .id("detail")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .track_scroll(&self.detail_scroll)
                    .pl(px(18.))
                    .pr(px(18.))
                    .pt(px(2.))
                    .pb(px(16.))
                    .child(body),
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
        theme: &Theme,
    ) -> Stateful<Div> {
        div()
            .id(id)
            .flex_1()
            .flex()
            .items_center()
            .justify_center()
            .gap(px(7.))
            .h(px(28.))
            .rounded(px(7.))
            .bg(if is_primary { theme.accent } else { theme.fill })
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
                this.bg(gpui::rgba(0xffffff38)).text_color(theme.on_accent)
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
                let body = match action {
                    Action::Close => "It moves to Done.",
                    Action::Cancel if row.status == Status::Running => {
                        "The agent stops and the task moves to Done."
                    }
                    Action::Cancel => "The task moves to Done.",
                    _ => "It restarts from its prompt.",
                };
                let verb = if action == Action::Cancel {
                    "Cancel task"
                } else {
                    action.label()
                };
                (
                    format!("{} {name}?", action.label()),
                    body.to_string(),
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
                                    theme,
                                )
                                .on_click(
                                    cx.listener(|this, _: &ClickEvent, _, cx| {
                                        this.answer(false, cx)
                                    }),
                                ),
                            )
                            .child(
                                Self::sheet_button("accept", verb, "\u{23ce}", true, theme)
                                    .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                        this.answer(true, cx)
                                    })),
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
                ("1-4", "jump to group"),
                ("G \u{21e7}G", "first, last"),
                ("N", "next needing you"),
                ("\u{23ce}", "details"),
                ("esc", "back"),
                ("\u{2303}D \u{2303}U", "scroll"),
            ],
        );
        let board_keys = section(
            "Board",
            72.,
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
            30.,
            &[
                ("P", "pick up"),
                ("W", "follow log"),
                ("O", "open PR"),
                ("R", "retry, recheck"),
                ("C", "close"),
                ("X", "cancel"),
                ("Y", "copy ID"),
                ("E", "full prompt"),
                ("I", "session"),
                ("T", "activity level"),
            ],
        );
        let columns = [
            div()
                .flex_1()
                .min_w_0()
                .flex()
                .flex_col()
                .child(move_keys)
                .child(board_keys),
            div().flex_1().min_w_0().flex().flex_col().child(task_keys),
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
                            .child(div().flex().gap(px(18.)).children(columns))
                            .child(
                                div()
                                    .flex()
                                    .flex_wrap()
                                    .gap(px(10.))
                                    .mt(px(12.))
                                    .children(legend),
                            ),
                    )
                    .child(
                        div().flex().mt(px(14.)).child(
                            Self::sheet_button("help-ok", "OK".to_string(), "esc", false, theme)
                                .on_click(cx.listener(|this, _: &ClickEvent, _, cx| {
                                    this.set_help(false, cx);
                                    cx.notify();
                                })),
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
        let mut list = div()
            .id("palette-list")
            .max_h(px(420.))
            .overflow_y_scroll()
            .track_scroll(&self.palette_scroll)
            .px(px(5.))
            .pt(px(4.))
            .pb(px(6.));
        let mut group = String::new();
        let mut child_count = 0;
        for (index, item) in items.iter().enumerate() {
            if index == current {
                let heading_count = usize::from(item.group != group);
                self.palette_scroll
                    .scroll_to_item(child_count + heading_count);
            }
            child_count += 1 + usize::from(item.group != group);
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
                    .h(px(28.))
                    .pl(px(8.))
                    .pr(px(6.))
                    .rounded(px(6.))
                    .text_color(foreground)
                    .when(is_current, |this| this.bg(theme.accent))
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
                        this.bg(gpui::rgba(0xffffff38)).text_color(theme.on_accent)
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
                    .child(list),
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
                    .items_start()
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
                        this.toast = None;
                        cx.notify();
                    }))
                    .when(toast.is_error, |this| {
                        this.border_color(theme.red)
                            .child(div().mt(px(2.)).child(icon(
                                theme::status_icon(Status::Failed),
                                12.,
                                theme.red,
                            )))
                    })
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .map(|this| {
                                if toast.is_error {
                                    this.line_clamp(3)
                                } else {
                                    this.truncate()
                                }
                            })
                            .child(toast.text.clone()),
                    ),
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
            .child(self.render_header(&theme, cx))
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
