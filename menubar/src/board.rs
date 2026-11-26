//! Board rules ported from golem's board.py, display.py and chains.py.

use std::collections::{BTreeSet, HashMap};
use std::io::{Read, Seek, SeekFrom};
use std::ops::Range;
use std::sync::LazyLock;

use chrono::{DateTime, Local, NaiveDate};
use regex::Regex;

use crate::ledger::{self, PullRequest, Status, Task, Tasks};

const CHAIN_DONE_AGE_DAYS: i64 = 7;
const MIN_WHOLE_HOURS: i64 = 6;
const DAYS_PER_YEAR: i64 = 365;
const ACTIVITY_TAIL_BYTES: u64 = 16384;
const ACTIVITY_LINE_COUNT: usize = 200;
const TOOL_LINE_PREFIX: &str = "[tool] ";
const SHELL_LINE_PREFIXES: [&str; 2] = ["[tool] command ", "[tool] Bash "];
const FINISHED_AGE_SEPARATOR: &str = "; finished ";
const WAITING_DIRECTORY_TEXT: &str = "waiting for directory:";
const BUSY_DIRECTORY_TEXT: &str = "dir busy:";
const NO_PR_TEXT: &str = "no PR";
const CLOSED_TEXT: &str = "closed";
const PULL_REQUEST_STATE_OPEN: &str = "open";
const DONE_THREAD_STATES: [&str; 3] = ["settled", "archived", "deleted"];
const THREAD_STATE_UNSETTLED: &str = "unsettled";
pub const SHELL_PROMPT: &str = "$ ";

static EXIT_PREFIX_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[^:]{1,40} exited (?:with status )?\d+:\s*").unwrap());
static MARKDOWN_HEADING_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^\s*#+\s*").unwrap());
static MARKDOWN_EMPHASIS_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\*\*|__|`").unwrap());
static PATH_PREFIX_PATTERN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[~/]\S*/").unwrap());
static COMMAND_OPTION_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^--?[A-Za-z][\w-]*(?:=\S*)?$").unwrap());
static COMMAND_RUNNER_PATTERN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\$ (?:uv run|poetry run|pipenv run|npx|bunx|pnpm exec|bundle exec) ").unwrap()
});
static SHORT_REASON_PATTERNS: LazyLock<[(Regex, &'static str); 3]> = LazyLock::new(|| {
    [
        (Regex::new(r"^(?:fatal|error): ").unwrap(), ""),
        (
            Regex::new(r"a branch named '[^']+' already exists").unwrap(),
            "branch exists",
        ),
        (Regex::new(r"\b(\w+) found (\d+)").unwrap(), "$2 $1"),
    ]
});

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Column {
    Queued,
    Running,
    Review,
    Done,
}

pub const BOARD_COLUMNS: [Column; 4] = [
    Column::Queued,
    Column::Running,
    Column::Review,
    Column::Done,
];

impl Column {
    pub fn title(self) -> &'static str {
        match self {
            Column::Queued => "Queued",
            Column::Running => "Running",
            Column::Review => "Review",
            Column::Done => "Done",
        }
    }

    pub fn empty_text(self) -> &'static str {
        match self {
            Column::Queued => "Nothing queued",
            Column::Running => "Nothing running",
            Column::Review => "Nothing to review",
            Column::Done => "Nothing done yet",
        }
    }

    fn index(self) -> usize {
        BOARD_COLUMNS
            .iter()
            .position(|column| *column == self)
            .unwrap_or(0)
    }
}

const BOARD_STATUS_ORDER: [Status; 7] = [
    Status::Failed,
    Status::Blocked,
    Status::Running,
    Status::Scheduled,
    Status::Waiting,
    Status::Finished,
    Status::Cancelled,
];

#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub task_id: String,
    pub root_id: String,
    /// Every task in the chain, root first.
    pub chain_ids: Vec<String>,
    pub label: String,
    pub step: String,
    pub status: Status,
    pub directory: String,
    pub detail: String,
    pub column: Column,
}

#[derive(Clone, Debug, Default)]
pub struct Snapshot {
    pub tasks: Tasks,
    pub rows: Vec<Row>,
    pub activity: HashMap<String, Vec<String>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Action {
    Pickup,
    Watch,
    OpenPullRequest,
    Retry,
    RerunGate,
    Close,
    CloseAll,
    Cancel,
    /// `golem cancel` on a task that isn't running: it only moves the task to Done.
    Dismiss,
    Copy,
}

pub const ACTIONS: [Action; 10] = [
    Action::Pickup,
    Action::Watch,
    Action::OpenPullRequest,
    Action::Retry,
    Action::RerunGate,
    Action::Close,
    Action::CloseAll,
    Action::Cancel,
    Action::Dismiss,
    Action::Copy,
];

impl Action {
    pub fn key(self) -> &'static str {
        match self {
            Action::Pickup => "p",
            Action::Watch => "w",
            Action::OpenPullRequest => "o",
            Action::Retry | Action::RerunGate => "r",
            Action::Close | Action::Dismiss => "c",
            Action::CloseAll => "C",
            Action::Cancel => "x",
            Action::Copy => "y",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Action::Pickup => "Pick up",
            Action::Watch => "Follow",
            Action::OpenPullRequest => "Open PR",
            Action::Retry => "Retry",
            Action::RerunGate => "Recheck",
            Action::Close => "Close",
            Action::CloseAll => "Close finished",
            Action::Cancel => "Cancel",
            Action::Dismiss => "Dismiss",
            Action::Copy => "Copy ID",
        }
    }

    /// The `golem` subcommand that performs this action, where there is one.
    pub fn command(self) -> &'static str {
        match self {
            Action::Pickup => "pickup",
            Action::Retry => "retry",
            Action::RerunGate => "continue",
            Action::Close | Action::CloseAll => "close",
            Action::Cancel | Action::Dismiss => "cancel",
            Action::Watch | Action::OpenPullRequest | Action::Copy => "",
        }
    }

    /// Cancel and Dismiss lose work, so they confirm only with their own key pressed again.
    pub fn is_destructive(self) -> bool {
        matches!(self, Action::Cancel | Action::Dismiss)
    }

    pub fn is_confirmed(self) -> bool {
        matches!(
            self,
            Action::CloseAll | Action::Cancel | Action::Dismiss | Action::Retry
        )
    }

    /// What a card says while this action's `golem` run is in flight.
    pub fn busy_text(self) -> &'static str {
        match self {
            Action::Pickup => "Opening session\u{2026}",
            Action::Retry => "Retrying\u{2026}",
            Action::RerunGate => "Rechecking the gate\u{2026}",
            Action::Close | Action::CloseAll => "Closing\u{2026}",
            Action::Cancel => "Cancelling\u{2026}",
            Action::Dismiss => "Dismissing\u{2026}",
            Action::Watch | Action::OpenPullRequest | Action::Copy => "",
        }
    }

    /// How a card and the details pane name this action's failed `golem` run.
    pub fn failed_text(self) -> &'static str {
        match self {
            Action::Pickup => "Pick up failed",
            Action::Retry => "Retry failed",
            Action::RerunGate => "Gate still failing",
            Action::Close | Action::CloseAll => "Close failed",
            Action::Cancel => "Cancel failed",
            Action::Dismiss => "Dismiss failed",
            Action::Watch | Action::OpenPullRequest | Action::Copy => "",
        }
    }

    /// Whether this action runs `golem`, so only one can be in flight per task.
    pub fn is_golem_run(self) -> bool {
        !self.command().is_empty()
    }

    pub fn unavailable_text(self) -> &'static str {
        match self {
            Action::Pickup => "This task can't be picked up",
            Action::Watch => "This task has no log yet",
            Action::OpenPullRequest => "This task has no pull request",
            Action::Retry | Action::RerunGate => {
                "Only failed, cancelled or blocked tasks can be retried"
            }
            Action::Close => "Only tasks in Review can be closed",
            Action::CloseAll => "Review has no finished tasks to close",
            Action::Cancel => "This task can't be cancelled",
            Action::Dismiss => "This task can't be dismissed",
            Action::Copy => "",
        }
    }
}

// ---- chains ---------------------------------------------------------------------------------

pub fn task_depth(tasks: &Tasks, task_id: &str) -> usize {
    let mut depth = 1;
    let mut parent_id = tasks.get(task_id).and_then(|task| task.after.as_deref());
    while let Some(parent) = parent_id.filter(|parent| tasks.contains_key(*parent)) {
        depth += 1;
        parent_id = tasks[parent].after.as_deref();
    }
    depth
}

pub fn root_task_id<'a>(tasks: &'a Tasks, task_id: &'a str) -> &'a str {
    let mut current = task_id;
    while let Some(parent) = tasks[current]
        .after
        .as_deref()
        .filter(|parent| tasks.contains_key(*parent))
    {
        current = parent;
    }
    current
}

fn dependent_task_ids<'a>(tasks: &'a Tasks, parent_id: &str) -> Vec<&'a str> {
    tasks
        .iter()
        .filter(|(_, task)| task.after.as_deref() == Some(parent_id))
        .map(|(id, _)| id.as_str())
        .collect()
}

/// How many waiting follow-up steps `golem cancel` cancels along with this task.
/// The names of the waiting steps that follow a task, which move to Done with it.
pub fn waiting_dependent_names(tasks: &Tasks, task_id: &str) -> Vec<String> {
    let mut names = Vec::new();
    let mut pending_ids = vec![task_id.to_string()];
    while let Some(parent_id) = pending_ids.pop() {
        for dependent_id in dependent_task_ids(tasks, &parent_id) {
            if tasks[dependent_id].status == Some(Status::Waiting) {
                names.push(chain_step_name(dependent_id, &tasks[dependent_id]));
                pending_ids.push(dependent_id.to_string());
            }
        }
    }
    names
}

pub fn chain_task_ids(tasks: &Tasks, task_id: &str) -> BTreeSet<String> {
    let mut chain_ids = BTreeSet::from([task_id.to_string()]);
    let mut pending_ids = vec![task_id.to_string()];
    while let Some(current_id) = pending_ids.pop() {
        let mut adjacent_ids: Vec<&str> = dependent_task_ids(tasks, &current_id);
        if let Some(parent) = tasks[&current_id]
            .after
            .as_deref()
            .filter(|parent| tasks.contains_key(*parent))
        {
            adjacent_ids.push(parent);
        }
        for adjacent_id in adjacent_ids {
            if chain_ids.insert(adjacent_id.to_string()) {
                pending_ids.push(adjacent_id.to_string());
            }
        }
    }
    chain_ids
}

fn chain_id_sets(tasks: &Tasks) -> Vec<BTreeSet<String>> {
    let mut id_sets: Vec<BTreeSet<String>> = Vec::new();
    let mut listed_ids: BTreeSet<String> = BTreeSet::new();
    for task_id in tasks.keys() {
        if listed_ids.contains(task_id) {
            continue;
        }
        let chain_ids = chain_task_ids(tasks, task_id);
        listed_ids.extend(chain_ids.iter().cloned());
        id_sets.push(chain_ids);
    }
    id_sets
}

fn has_field_in_subtree(tasks: &Tasks, task_id: &str, has_field: fn(&Task) -> bool) -> bool {
    has_field(&tasks[task_id])
        || dependent_task_ids(tasks, task_id)
            .into_iter()
            .any(|dependent_id| has_field_in_subtree(tasks, dependent_id, has_field))
}

fn is_counted_opened(tasks: &Tasks, task_id: &str) -> bool {
    has_field_in_subtree(tasks, task_id, |task| task.opened_at.is_some())
        || has_field_in_subtree(tasks, root_task_id(tasks, task_id), |task| {
            task.closed_at.is_some()
        })
}

fn is_ready_to_open(tasks: &Tasks, task_id: &str) -> bool {
    if is_counted_opened(tasks, task_id) {
        return false;
    }
    match tasks[task_id].status() {
        Status::Blocked => true,
        Status::Finished => dependent_task_ids(tasks, task_id)
            .into_iter()
            .all(|dependent_id| {
                matches!(
                    tasks[dependent_id].status(),
                    Status::Failed | Status::Cancelled
                )
            }),
        _ => false,
    }
}

fn sorted_by_depth(tasks: &Tasks, chain_ids: &BTreeSet<String>) -> Vec<String> {
    let mut ids: Vec<String> = chain_ids.iter().cloned().collect();
    ids.sort_by_key(|id| (task_depth(tasks, id), id.clone()));
    ids
}

// ---- text -----------------------------------------------------------------------------------

pub fn first_line(text: &str) -> &str {
    text.lines().next().unwrap_or("")
}

pub fn display_directory(directory: &str) -> String {
    let home = ledger::home_directory();
    let home_text = home.to_string_lossy();
    match directory.strip_prefix(home_text.as_ref()) {
        Some(rest) if rest.is_empty() || rest.starts_with('/') => format!("~{rest}"),
        _ => directory.to_string(),
    }
}

pub fn task_name(task_id: &str, task: &Task) -> String {
    match &task.label {
        Some(label) => format!("{task_id} {label}"),
        None => task_id.to_string(),
    }
}

fn parse_time(timestamp: &str) -> Option<DateTime<Local>> {
    DateTime::parse_from_rfc3339(timestamp)
        .ok()
        .map(|time| time.with_timezone(&Local))
}

fn local_date(timestamp: &str) -> Option<NaiveDate> {
    parse_time(timestamp).map(|time| time.date_naive())
}

fn clock_time(time: DateTime<Local>) -> String {
    time.format("%-I:%M%p").to_string().to_lowercase()
}

pub fn display_time(timestamp: &str, now: DateTime<Local>) -> String {
    let Some(time) = parse_time(timestamp) else {
        return timestamp.to_string();
    };
    let today = now.date_naive();
    let date = time.date_naive();
    let day_text = if date == today {
        "today".to_string()
    } else if Some(date) == today.succ_opt() {
        "tomorrow".to_string()
    } else if time.format("%Y").to_string() != now.format("%Y").to_string() {
        time.format("%b %-d, %Y").to_string()
    } else {
        time.format("%b %-d").to_string()
    };
    format!("{day_text} {}", clock_time(time))
}

pub fn seconds_since(timestamp: &str, now: DateTime<Local>) -> f64 {
    parse_time(timestamp).map_or(0.0, |time| (now - time).num_milliseconds() as f64 / 1000.0)
}

pub fn duration_text(seconds: f64) -> String {
    let total_minutes = (seconds / 60.0).floor().max(0.0) as i64;
    if total_minutes == 0 {
        return "<1m".to_string();
    }
    let (total_hours, minutes) = (total_minutes / 60, total_minutes % 60);
    if total_hours == 0 {
        return format!("{minutes}m");
    }
    let (days, hours) = (total_hours / 24, total_hours % 24);
    if days == 0 {
        if hours >= MIN_WHOLE_HOURS || minutes == 0 {
            return format!("{hours}h");
        }
        return format!("{hours}h {minutes}m");
    }
    if days >= DAYS_PER_YEAR {
        return format!("{}y", days / DAYS_PER_YEAR);
    }
    if hours > 0 {
        format!("{days}d {hours}h")
    } else {
        format!("{days}d")
    }
}

pub fn moment_text(timestamp: Option<&str>, now: DateTime<Local>) -> String {
    let Some(timestamp) = timestamp else {
        return String::new();
    };
    let seconds = seconds_since(timestamp, now);
    let relative = if seconds < 0.0 {
        format!("in {}", duration_text(-seconds))
    } else {
        format!("{} ago", duration_text(seconds))
    };
    format!("{} ({relative})", display_time(timestamp, now))
}

fn short_age_text(timestamp: Option<&str>, now: DateTime<Local>) -> String {
    timestamp.map_or_else(String::new, |timestamp| {
        duration_text(seconds_since(timestamp, now))
            .split(' ')
            .next()
            .unwrap_or("")
            .to_string()
    })
}

fn list_reason(task: &Task) -> String {
    if let Some(reason) = task
        .gate_stop_reason
        .as_deref()
        .or(task.setup_failure_reason.as_deref())
    {
        return first_line(reason).to_string();
    }
    match (&task.setup_busy_retry_count, &task.setup_directory) {
        (Some(_), Some(directory)) => format!("{WAITING_DIRECTORY_TEXT} {directory}"),
        _ => String::new(),
    }
}

fn pull_request_summary(pull_requests: &[PullRequest]) -> String {
    let Some(first) = pull_requests.first() else {
        return NO_PR_TEXT.to_string();
    };
    let states: BTreeSet<&str> = pull_requests.iter().map(|pr| pr.state.as_str()).collect();
    if states.len() > 1 {
        let counts: Vec<String> = ["open", "merged", "closed"]
            .iter()
            .filter_map(|state| {
                let count = pull_requests.iter().filter(|pr| pr.state == *state).count();
                (count > 0).then(|| format!("{count} {state}"))
            })
            .collect();
        return format!("{} PRs: {}", pull_requests.len(), counts.join(", "));
    }
    if pull_requests.len() == 1 {
        format!("PR {}", first.state)
    } else {
        format!("{} PRs {}", pull_requests.len(), first.state)
    }
}

fn list_detail(task: &Task, now: DateTime<Local>, chain_reason: &str) -> String {
    let timestamp = match task.status() {
        Status::Scheduled => task.due_at.as_deref(),
        Status::Running => task.started_at.as_deref(),
        _ => None,
    };
    let status_detail = match (task.status(), timestamp) {
        (Status::Waiting, _) => task.after.clone().unwrap_or_default(),
        (_, Some(timestamp)) => display_time(timestamp, now),
        (_, None) => chain_reason.to_string(),
    };
    [status_detail, list_reason(task)]
        .into_iter()
        .filter(|text| !text.is_empty())
        .collect::<Vec<_>>()
        .join("; ")
}

pub fn chain_pull_requests(tasks: &Tasks, chain_ids: &[String]) -> Vec<PullRequest> {
    let mut sorted_ids: Vec<&String> = chain_ids.iter().collect();
    sorted_ids.sort();
    let mut pull_requests: Vec<PullRequest> = Vec::new();
    for pull_request in sorted_ids
        .into_iter()
        .flat_map(|id| tasks[id].pull_requests.iter())
    {
        match pull_requests
            .iter_mut()
            .find(|known| known.url == pull_request.url)
        {
            Some(known) => *known = pull_request.clone(),
            None => pull_requests.push(pull_request.clone()),
        }
    }
    pull_requests
}

fn finished_chain_column(
    tasks: &Tasks,
    chain_ids: &[String],
    now: DateTime<Local>,
) -> (Column, String) {
    if chain_ids.iter().any(|id| tasks[id].closed_at.is_some()) {
        return (Column::Done, CLOSED_TEXT.to_string());
    }
    let pull_requests = chain_pull_requests(tasks, chain_ids);
    if !pull_requests.is_empty() {
        let is_finished = pull_requests
            .iter()
            .all(|pr| pr.state != PULL_REQUEST_STATE_OPEN);
        let column = if is_finished {
            Column::Done
        } else {
            Column::Review
        };
        return (column, pull_request_summary(&pull_requests));
    }
    let last_step_id = chain_ids
        .iter()
        .filter(|id| tasks[*id].status() != Status::Cancelled)
        .max_by_key(|id| (task_depth(tasks, id), (*id).clone()));
    if let Some(step) = last_step_id
        .map(|id| &tasks[id])
        .filter(|step| step.t3_code_thread_id.is_some())
    {
        let thread_state = step
            .t3_code_thread_state
            .as_deref()
            .unwrap_or(THREAD_STATE_UNSETTLED);
        let column = if DONE_THREAD_STATES.contains(&thread_state) {
            Column::Done
        } else {
            Column::Review
        };
        return (column, format!("thread {thread_state}"));
    }
    let finished_at = chain_ids
        .iter()
        .filter_map(|id| tasks[id].finished_at.as_deref())
        .max()
        .unwrap_or("");
    let age_days = local_date(finished_at).map_or(0, |date| (now.date_naive() - date).num_days());
    let column = if age_days >= CHAIN_DONE_AGE_DAYS {
        Column::Done
    } else {
        Column::Review
    };
    (
        column,
        format!("{NO_PR_TEXT}{FINISHED_AGE_SEPARATOR}{age_days}d ago"),
    )
}

fn shown_task_id(tasks: &Tasks, chain_ids: &[String]) -> String {
    chain_ids
        .iter()
        .max_by_key(|id| {
            let status_rank = std::cmp::Reverse(tasks[*id].status().list_index());
            (status_rank, task_depth(tasks, id), (*id).clone())
        })
        .cloned()
        .unwrap_or_default()
}

fn timestamp_seconds(timestamp: Option<&str>) -> f64 {
    timestamp
        .and_then(parse_time)
        .map_or(0.0, |time| time.timestamp_millis() as f64 / 1000.0)
}

fn board_sort_key(row: &Row, tasks: &Tasks) -> (usize, usize, f64, String) {
    let task = &tasks[&row.task_id];
    let status_index = BOARD_STATUS_ORDER
        .iter()
        .position(|status| *status == row.status)
        .unwrap_or(0);
    let moment = match row.column {
        Column::Queued => timestamp_seconds(task.due_at.as_deref()),
        Column::Running => timestamp_seconds(task.started_at.as_deref()),
        _ => -timestamp_seconds(task.finished_at.as_deref().or(task.started_at.as_deref())),
    };
    (
        row.column.index(),
        status_index,
        moment,
        row.task_id.clone(),
    )
}

pub fn board_rows(tasks: &Tasks, now: DateTime<Local>) -> Vec<Row> {
    let mut rows: Vec<Row> = chain_id_sets(tasks)
        .iter()
        .map(|chain_set| {
            let chain_ids = sorted_by_depth(tasks, chain_set);
            let shown_id = shown_task_id(tasks, &chain_ids);
            let shown_task = &tasks[&shown_id];
            let (column, chain_reason) = match shown_task.status() {
                Status::Finished => finished_chain_column(tasks, &chain_ids, now),
                Status::Blocked | Status::Failed => (Column::Review, String::new()),
                Status::Running => (Column::Running, String::new()),
                Status::Scheduled | Status::Waiting => (Column::Queued, String::new()),
                Status::Cancelled => (Column::Done, String::new()),
            };
            let step_count = chain_ids
                .iter()
                .map(|id| task_depth(tasks, id))
                .max()
                .unwrap_or(1);
            let root_id = root_task_id(tasks, &shown_id).to_string();
            Row {
                label: tasks[&root_id].label.clone().unwrap_or_default(),
                step: if chain_ids.len() > 1 {
                    format!("{}/{step_count}", task_depth(tasks, &shown_id))
                } else {
                    String::new()
                },
                status: shown_task.status(),
                directory: shown_task.cwd.clone(),
                detail: list_detail(shown_task, now, &chain_reason),
                column,
                chain_ids,
                root_id,
                task_id: shown_id,
            }
        })
        .collect();
    rows.sort_by(|left, right| {
        board_sort_key(left, tasks)
            .partial_cmp(&board_sort_key(right, tasks))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    rows
}

pub fn recent_activity(session_id: &str) -> Vec<String> {
    let Ok(mut file) = std::fs::File::open(ledger::progress_path(session_id)) else {
        return Vec::new();
    };
    let Ok(size) = file.seek(SeekFrom::End(0)) else {
        return Vec::new();
    };
    let start = size.saturating_sub(ACTIVITY_TAIL_BYTES);
    let mut bytes = Vec::new();
    if file.seek(SeekFrom::Start(start)).is_err() || file.read_to_end(&mut bytes).is_err() {
        return Vec::new();
    }
    activity_lines(
        &String::from_utf8_lossy(&bytes),
        bytes.len() as u64 >= ACTIVITY_TAIL_BYTES,
    )
}

fn activity_lines(tail: &str, is_cut: bool) -> Vec<String> {
    let lines: Vec<String> = tail
        .lines()
        .skip(usize::from(is_cut))
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|line| !line.is_empty())
        .collect();
    let skipped = lines.len().saturating_sub(ACTIVITY_LINE_COUNT);
    lines.into_iter().skip(skipped).collect()
}

pub fn board_snapshot(tasks: Tasks, now: DateTime<Local>) -> Snapshot {
    let rows = board_rows(&tasks, now);
    let activity = rows
        .iter()
        .filter(|row| row.status == Status::Running)
        .map(|row| {
            (
                row.task_id.clone(),
                recent_activity(&tasks[&row.task_id].session_id),
            )
        })
        .collect();
    Snapshot {
        tasks,
        rows,
        activity,
    }
}

/// What a card or its details show: the directory, the card's line, and each chain step's
/// ID, label, prompt, result, failure reasons and PR numbers. Kept apart so a query can't
/// match across two of them. Each comes with the name a card shows for a match in it.
fn searched_fields(row: &Row, snapshot: &Snapshot) -> Vec<(&'static str, String)> {
    let mut fields = vec![
        ("Directory", row.directory.clone()),
        ("", card_snippet(row, snapshot)),
    ];
    for id in &row.chain_ids {
        let task = &snapshot.tasks[id];
        let pull_requests: Vec<String> =
            task.pull_requests.iter().map(pull_request_number).collect();
        fields.push(("ID", id.clone()));
        let task_fields = [
            ("Name", task.label.clone()),
            ("Prompt", Some(task.prompt.clone())),
            ("Result", task.result.clone()),
            ("Gate", task.gate_stop_reason.clone()),
            ("Setup", task.setup_failure_reason.clone()),
        ];
        fields.extend(
            task_fields
                .into_iter()
                .filter_map(|(source, text)| text.map(|text| (source, text))),
        );
        fields.push(("PR", pull_requests.join(" ")));
    }
    fields
}

pub fn is_matching(row: &Row, snapshot: &Snapshot, query: &str) -> bool {
    let query = query.to_lowercase();
    let fields = searched_fields(row, snapshot);
    fields
        .iter()
        .any(|(_, field)| field.to_lowercase().contains(&query))
}

/// For a card whose title, repo and line don't show the query, the line of a hidden field
/// that matched, named and from a little before the match, with the match's range in it.
pub fn match_excerpt(
    row: &Row,
    snapshot: &Snapshot,
    query: &str,
) -> Option<(String, Range<usize>)> {
    let query = query.to_lowercase();
    let visible = [
        row_title(row).to_string(),
        repository_name(row).to_string(),
        card_snippet(row, snapshot),
    ];
    if visible
        .iter()
        .any(|text| text.to_lowercase().contains(&query))
    {
        return None;
    }
    let fields = searched_fields(row, snapshot);
    let (source, line) = fields
        .iter()
        .flat_map(|(source, field)| field.lines().map(move |line| (*source, line)))
        .find(|(_, line)| line.to_lowercase().contains(&query))?;
    let line = line.trim();
    let lowered = line.to_lowercase();
    // Lowercasing can change a line's byte length, which would misplace the highlight.
    let Some(start) = lowered.find(&query).filter(|_| lowered.len() == line.len()) else {
        return Some((format!("{source}: {line}"), 0..0));
    };
    let context_start = line[..start]
        .char_indices()
        .rev()
        .nth(EXCERPT_CONTEXT_SIZE - 1)
        .map_or(0, |(index, _)| index);
    let ellipsis = if context_start > 0 { "\u{2026}" } else { "" };
    let prefix = format!("{source}: {ellipsis}");
    let match_start = prefix.len() + start - context_start;
    Some((
        format!("{prefix}{}", &line[context_start..]),
        match_start..match_start + query.len(),
    ))
}

/// Characters of context kept before a filter match shown on a card.
const EXCERPT_CONTEXT_SIZE: usize = 16;

// ---- row facts ------------------------------------------------------------------------------

pub fn is_stuck(row: &Row, task: &Task, now: DateTime<Local>) -> bool {
    match (row.status, &task.due_at) {
        (Status::Scheduled, Some(due_at)) => {
            !list_reason(task).is_empty() && seconds_since(due_at, now) > 0.0
        }
        _ => false,
    }
}

pub fn is_new_for_review(tasks: &Tasks, row: &Row) -> bool {
    if row.column != Column::Review {
        return false;
    }
    if row.status == Status::Failed {
        return !is_counted_opened(tasks, &row.task_id);
    }
    is_ready_to_open(tasks, &row.task_id)
}

pub fn needs_you(tasks: &Tasks, row: &Row, now: DateTime<Local>) -> bool {
    matches!(row.status, Status::Blocked | Status::Failed)
        || is_new_for_review(tasks, row)
        || is_stuck(row, &tasks[&row.task_id], now)
}

fn when_text(task: &Task, now: DateTime<Local>) -> String {
    let stopped_at = task.finished_at.as_deref().or(task.started_at.as_deref());
    match task.status() {
        Status::Scheduled if task.due_at.is_some() => {
            let seconds = -seconds_since(task.due_at.as_deref().unwrap_or(""), now);
            let verb = if task.is_usage_limit_pause() {
                "resumes"
            } else {
                "in"
            };
            if seconds > 0.0 {
                format!("{verb} {}", duration_text(seconds))
            } else {
                "due now".to_string()
            }
        }
        Status::Running if task.started_at.is_some() => format!(
            "for {}",
            duration_text(seconds_since(task.started_at.as_deref().unwrap_or(""), now))
        ),
        Status::Blocked | Status::Failed | Status::Finished if stopped_at.is_some() => {
            format!(
                "{} ago",
                duration_text(seconds_since(stopped_at.unwrap_or(""), now))
            )
        }
        _ => String::new(),
    }
}

/// The short time phrase on a card: a countdown, a run time or an age.
pub fn card_when_text(row: &Row, task: &Task, now: DateTime<Local>) -> String {
    if is_stuck(row, task, now) {
        return format!("stuck {}", short_age_text(task.due_at.as_deref(), now));
    }
    let text = match row.status {
        Status::Waiting => task
            .after
            .as_ref()
            .map(|after| format!("after {after}"))
            .unwrap_or_default(),
        Status::Cancelled => {
            let stopped_at = task.finished_at.as_deref().or(task.started_at.as_deref());
            stopped_at.map_or_else(
                || "never ran".to_string(),
                |stopped_at| format!("{} ago", duration_text(seconds_since(stopped_at, now))),
            )
        }
        _ => when_text(task, now),
    };
    largest_unit_only(&text)
}

/// Cards rank by age, so a duration keeps only its largest unit ("2h 30m ago" reads "2h ago").
fn largest_unit_only(text: &str) -> String {
    let is_unit = |word: &str| {
        word.len() > 1
            && word.ends_with(['m', 'h', 'd', 'y'])
            && word[..word.len() - 1]
                .chars()
                .all(|char| char.is_ascii_digit())
    };
    let mut words: Vec<&str> = Vec::new();
    for word in text.split(' ') {
        if is_unit(word) && words.last().is_some_and(|last| is_unit(last)) {
            continue;
        }
        words.push(word);
    }
    words.join(" ")
}

pub fn start_text(row: &Row, task: &Task, now: DateTime<Local>) -> String {
    let (Status::Scheduled, Some(due_at)) = (row.status, task.due_at.as_deref()) else {
        return String::new();
    };
    let verb = if seconds_since(due_at, now) > 0.0 {
        "was due"
    } else {
        "starts"
    };
    format!("{verb} {}", display_time(due_at, now))
}

pub fn activity_text(line: &str) -> String {
    if let Some(prefix) = SHELL_LINE_PREFIXES
        .iter()
        .find(|prefix| line.starts_with(**prefix))
    {
        return format!("{SHELL_PROMPT}{}", &line[prefix.len()..]);
    }
    match line.strip_prefix(TOOL_LINE_PREFIX) {
        Some(rest) => rest.to_string(),
        None => line.to_string(),
    }
}

/// Keeps the part of a word after its last directory separator, as golem's card text does.
fn without_directories(word: &str) -> &str {
    let cut = word
        .char_indices()
        .rev()
        .find(|(index, char)| *char == '/' && index + 1 < word.len());
    cut.map_or(word, |(index, _)| &word[index + 1..])
}

pub fn card_activity_text(entry: &str) -> String {
    let text = COMMAND_RUNNER_PATTERN
        .replace(&activity_text(entry), SHELL_PROMPT)
        .into_owned();
    let is_command = text.starts_with(SHELL_PROMPT);
    text.split(' ')
        .enumerate()
        .filter(|(index, word)| {
            !(is_command && *index > 0 && COMMAND_OPTION_PATTERN.is_match(word))
        })
        .map(|(_, word)| without_directories(word))
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn pull_request_number(pull_request: &PullRequest) -> String {
    format!(
        "#{}",
        pull_request
            .url
            .trim_end_matches('/')
            .rsplit('/')
            .next()
            .unwrap_or("")
    )
}

fn pull_request_text(pull_requests: &[PullRequest]) -> String {
    let labels: Vec<String> = pull_requests
        .iter()
        .map(|pr| format!("{} {}", pull_request_number(pr), pr.state))
        .collect();
    if labels.len() == 1 {
        format!("PR {}", labels[0])
    } else {
        format!("PRs {}", labels.join(", "))
    }
}

fn short_pull_request_text(pull_requests: &[PullRequest]) -> String {
    if pull_requests.len() == 1 {
        return pull_request_text(pull_requests);
    }
    let mut states: Vec<(&str, usize)> = Vec::new();
    for pull_request in pull_requests {
        match states
            .iter_mut()
            .find(|(state, _)| *state == pull_request.state)
        {
            Some((_, count)) => *count += 1,
            None => states.push((&pull_request.state, 1)),
        }
    }
    let open_count = states
        .iter()
        .find(|(state, _)| *state == PULL_REQUEST_STATE_OPEN)
        .map(|(_, count)| *count);
    if let (true, Some(open_count)) = (states.len() > 1, open_count) {
        return format!("{} PRs, {open_count} open", pull_requests.len());
    }
    let parts: Vec<String> = states
        .iter()
        .map(|(state, count)| format!("{count} {state}"))
        .collect();
    format!("PRs {}", parts.join(", "))
}

pub fn row_pull_requests(snapshot: &Snapshot, row: &Row) -> Vec<PullRequest> {
    chain_pull_requests(&snapshot.tasks, &row.chain_ids)
}

pub fn row_reason(row: &Row, snapshot: &Snapshot) -> String {
    let task = &snapshot.tasks[&row.task_id];
    match row.status {
        Status::Scheduled | Status::Running => {
            let home = ledger::home_directory().to_string_lossy().into_owned();
            list_reason(task)
                .replace(&home, "~")
                .replace(WAITING_DIRECTORY_TEXT, BUSY_DIRECTORY_TEXT)
        }
        Status::Waiting => {
            if row.detail.is_empty() {
                String::new()
            } else {
                format!("after {}", row.detail)
            }
        }
        Status::Cancelled => {
            if row.detail.is_empty() {
                "cancelled".to_string()
            } else {
                row.detail.clone()
            }
        }
        Status::Blocked | Status::Failed => row.detail.clone(),
        Status::Finished => {
            let pull_requests = row_pull_requests(snapshot, row);
            if !pull_requests.is_empty() {
                return pull_request_text(&pull_requests);
            }
            let detail = row
                .detail
                .split(FINISHED_AGE_SEPARATOR)
                .next()
                .unwrap_or("");
            if detail == "thread unsettled" {
                "thread open, no PR".to_string()
            } else {
                detail.to_string()
            }
        }
    }
}

fn short_reason_text(row: &Row, reason: &str, snapshot: &Snapshot) -> String {
    let pull_requests = row_pull_requests(snapshot, row);
    if row.status == Status::Finished && !pull_requests.is_empty() {
        return short_pull_request_text(&pull_requests);
    }
    let reason = if reason == "thread open, no PR" {
        "thread, no PR"
    } else {
        reason
    };
    let mut text = EXIT_PREFIX_PATTERN
        .replace(
            &reason.replace(WAITING_DIRECTORY_TEXT, BUSY_DIRECTORY_TEXT),
            "",
        )
        .into_owned();
    for (pattern, replacement) in SHORT_REASON_PATTERNS.iter() {
        text = pattern.replace_all(&text, *replacement).into_owned();
    }
    text
}

/// The second line of a card: live activity for running tasks, otherwise why the task is here.
pub fn card_snippet(row: &Row, snapshot: &Snapshot) -> String {
    let task = &snapshot.tasks[&row.task_id];
    if row.status == Status::Running {
        return snapshot
            .activity
            .get(&row.task_id)
            .and_then(|lines| lines.last())
            .map_or_else(
                || "no activity yet".to_string(),
                |line| card_activity_text(line),
            );
    }
    let mut reason = row_reason(row, snapshot);
    if row.status == Status::Scheduled {
        reason = PATH_PREFIX_PATTERN.replace_all(&reason, "").into_owned();
    }
    let short_reason = short_reason_text(row, &reason, snapshot);
    // These only restate the status icon, so the result or prompt says more.
    let is_status_text = [NO_PR_TEXT, "cancelled", CLOSED_TEXT].contains(&short_reason.as_str());
    if !short_reason.is_empty() && !is_status_text {
        return short_reason;
    }
    let text = task
        .result
        .as_deref()
        .filter(|result| !result.trim().is_empty())
        .unwrap_or(&task.prompt);
    let line = first_line(text.trim());
    line.strip_prefix("Error:")
        .unwrap_or(line)
        .trim()
        .to_string()
}

/// A task result without the Markdown heading marks and emphasis golem's TUI also drops.
pub fn plain_result(result: &str) -> String {
    let text = MARKDOWN_EMPHASIS_PATTERN.replace_all(result.trim(), "");
    MARKDOWN_HEADING_PATTERN.replace_all(&text, "").into_owned()
}

pub fn waiting_reason_text(row: &Row, snapshot: &Snapshot) -> String {
    if !matches!(row.status, Status::Scheduled | Status::Waiting) {
        return String::new();
    }
    let reason = row_reason(row, snapshot);
    match snapshot.tasks[&row.task_id].setup_busy_retry_count {
        Some(count) if count > 0 && !reason.is_empty() => {
            format!("{reason} (checked {count} times)")
        }
        _ => reason,
    }
}

pub fn step_display_name(tasks: &Tasks, task_id: &str) -> String {
    let has_parent = root_task_id(tasks, task_id) != task_id;
    if has_parent {
        tasks[task_id].label.clone().unwrap_or_default()
    } else {
        String::new()
    }
}

pub fn task_display_name(tasks: &Tasks, task_id: &str) -> String {
    let root_id = root_task_id(tasks, task_id);
    let name = task_name(root_id, &tasks[root_id]);
    let step = step_display_name(tasks, task_id);
    if step.is_empty() {
        name
    } else {
        format!("{name} ({step})")
    }
}

/// The heading a row shows: the chain's label, or its root ID when it has none.
pub fn row_title(row: &Row) -> &str {
    if row.label.is_empty() {
        &row.root_id
    } else {
        &row.label
    }
}

pub fn repository_name(row: &Row) -> &str {
    row.directory
        .trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or("")
}

pub fn branch_name(task: &Task) -> Option<&str> {
    task.repository_branches
        .first()
        .map(|entry| entry.branch.as_str())
}

pub fn task_fields(
    row: &Row,
    snapshot: &Snapshot,
    now: DateTime<Local>,
) -> Vec<(&'static str, String)> {
    let task = &snapshot.tasks[&row.task_id];
    let has_parent = row.task_id != row.root_id;
    let backend = if task.agent_args.is_empty() {
        task.backend.clone()
    } else {
        format!("{}  {}", task.backend, task.agent_args.join(" "))
    };
    let worktree = task
        .worktree
        .as_deref()
        .filter(|worktree| *worktree != task.cwd)
        .unwrap_or("");
    let due_at = task
        .due_at
        .as_deref()
        .filter(|_| row.status == Status::Scheduled);
    let mut fields = vec![
        (
            "Task ID",
            if has_parent {
                task_name(&row.task_id, task)
            } else {
                String::new()
            },
        ),
        ("Directory", display_directory(&row.directory)),
        ("Worktree", display_directory(worktree)),
        ("Branch", branch_name(task).unwrap_or_default().to_string()),
        ("Backend", backend),
        ("Session", task.session_id.clone()),
        ("Due", moment_text(due_at, now)),
        ("Started", moment_text(task.started_at.as_deref(), now)),
        (
            if row.status == Status::Finished {
                "Finished"
            } else {
                "Stopped"
            },
            moment_text(task.finished_at.as_deref(), now),
        ),
        ("Gate", task.gate.clone().unwrap_or_default()),
        ("Setup", task.setup.clone().unwrap_or_default()),
        ("Reason", waiting_reason_text(row, snapshot)),
    ];
    if row.status == Status::Finished && row_pull_requests(snapshot, row).is_empty() {
        fields.push(("Outcome", row_reason(row, snapshot)));
    }
    fields.retain(|(_, value)| !value.is_empty());
    fields
}

pub struct ChainStep {
    pub name: String,
    pub status: Status,
    pub is_shown: bool,
    pub is_inferable: bool,
}

/// A step's label, else the first line of its prompt, since a bare task ID says nothing.
fn chain_step_name(task_id: &str, task: &Task) -> String {
    if let Some(label) = &task.label {
        return label.clone();
    }
    match task
        .prompt
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
    {
        Some(line) => line.to_string(),
        None => task_id.to_string(),
    }
}

pub fn chain_steps(row: &Row, tasks: &Tasks) -> Vec<ChainStep> {
    let statuses: Vec<Status> = row.chain_ids.iter().map(|id| tasks[id].status()).collect();
    let active_index = statuses
        .iter()
        .position(|status| *status != Status::Finished)
        .unwrap_or(statuses.len());
    row.chain_ids
        .iter()
        .zip(&statuses)
        .enumerate()
        .map(|(index, (task_id, status))| ChainStep {
            name: chain_step_name(task_id, &tasks[task_id]),
            status: *status,
            is_shown: *task_id == row.task_id,
            is_inferable: (*status == Status::Finished && index < active_index)
                || (*status == Status::Waiting && index > active_index),
        })
        .collect()
}

pub fn task_activity(row: &Row, snapshot: &Snapshot) -> Vec<String> {
    if let Some(lines) = snapshot.activity.get(&row.task_id) {
        return lines.clone();
    }
    let task = &snapshot.tasks[&row.task_id];
    if task.has_started() {
        recent_activity(&task.session_id)
    } else {
        Vec::new()
    }
}

// ---- activity view --------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ActivityLevel {
    /// Runs of tool calls fold into one line.
    Messages,
    All,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ActivityItem {
    /// A run of tool calls hidden at the Messages level, holding their display text.
    Fold(Vec<String>),
    Tool(String),
    Message(String),
}

fn is_tool_line(line: &str) -> bool {
    line.starts_with(TOOL_LINE_PREFIX)
}

/// Up to three names in prose, then how many more.
pub fn names_text(names: &[&str]) -> String {
    match names {
        [] => String::new(),
        [name] => name.to_string(),
        [first, second] => format!("{first} and {second}"),
        [first, second, third] => format!("{first}, {second} and {third}"),
        [first, second, third, rest @ ..] => {
            format!("{first}, {second}, {third} and {} more", rest.len())
        }
    }
}

pub fn counted_text(count: usize, noun: &str) -> String {
    if count == 1 {
        format!("{count} {noun}")
    } else {
        format!("{count} {noun}s")
    }
}

pub fn activity_counts(lines: &[String]) -> String {
    let tool_count = lines.iter().filter(|line| is_tool_line(line)).count();
    [
        counted_text(lines.len() - tool_count, "message"),
        counted_text(tool_count, "tool call"),
    ]
    .into_iter()
    .filter(|text| !text.starts_with("0 "))
    .collect::<Vec<_>>()
    .join(", ")
}

pub fn activity_view(lines: &[String], level: ActivityLevel) -> Vec<ActivityItem> {
    if lines.is_empty() {
        return Vec::new();
    }
    let mut items = Vec::new();
    for group in lines.chunk_by(|left, right| is_tool_line(left) == is_tool_line(right)) {
        if !is_tool_line(&group[0]) {
            items.extend(group.iter().map(|line| ActivityItem::Message(line.clone())));
        } else if level == ActivityLevel::Messages && group.len() > 1 {
            items.push(ActivityItem::Fold(
                group.iter().map(|line| activity_text(line)).collect(),
            ));
        } else {
            items.extend(
                group
                    .iter()
                    .map(|line| ActivityItem::Tool(activity_text(line))),
            );
        }
    }
    items
}

// ---- actions --------------------------------------------------------------------------------

pub fn closable_task_ids(snapshot: &Snapshot) -> Vec<String> {
    snapshot
        .rows
        .iter()
        .filter(|row| row.column == Column::Review && row.status == Status::Finished)
        .map(|row| row.task_id.clone())
        .collect()
}

pub fn available_actions(snapshot: &Snapshot, row: Option<&Row>) -> Vec<Action> {
    let has_closable = !closable_task_ids(snapshot).is_empty();
    let Some(row) = row else {
        return if has_closable {
            vec![Action::CloseAll]
        } else {
            Vec::new()
        };
    };
    let task = &snapshot.tasks[&row.task_id];
    let mut actions = Vec::new();
    let is_started_cancelled = row.status == Status::Cancelled && task.has_started();
    if matches!(
        row.status,
        Status::Blocked | Status::Failed | Status::Finished
    ) || is_started_cancelled
    {
        actions.push(Action::Pickup);
    }
    if row.status == Status::Running || ledger::progress_path(&task.session_id).exists() {
        actions.push(Action::Watch);
    }
    if !row_pull_requests(snapshot, row).is_empty() {
        actions.push(Action::OpenPullRequest);
    }
    if matches!(row.status, Status::Failed | Status::Cancelled) {
        actions.push(Action::Retry);
    }
    if row.status == Status::Blocked {
        actions.push(Action::RerunGate);
    }
    if row.column == Column::Review && row.status == Status::Finished {
        actions.push(Action::Close);
    }
    if has_closable {
        actions.push(Action::CloseAll);
    }
    if task.is_cancellable() {
        actions.push(match row.status {
            Status::Failed | Status::Blocked => Action::Dismiss,
            _ => Action::Cancel,
        });
    }
    actions.push(Action::Copy);
    actions
}

/// Whether a running task's card snippet is its latest tool call, shown in the code font.
pub fn is_tool_snippet(row: &Row, snapshot: &Snapshot) -> bool {
    row.status == Status::Running
        && snapshot
            .activity
            .get(&row.task_id)
            .and_then(|lines| lines.last())
            .is_some_and(|line| is_tool_line(line))
}

/// The action most likely wanted next, shown as the default button.
pub fn primary_action(snapshot: &Snapshot, row: &Row, actions: &[Action]) -> Option<Action> {
    if row.status == Status::Running {
        return Some(Action::Watch);
    }
    let has_open_pull_request = row_pull_requests(snapshot, row)
        .iter()
        .any(|pr| pr.state == PULL_REQUEST_STATE_OPEN);
    if row.status == Status::Finished && has_open_pull_request {
        return Some(Action::OpenPullRequest);
    }
    [Action::Pickup, Action::Retry]
        .into_iter()
        .find(|action| actions.contains(action))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, TimeZone};

    fn now() -> DateTime<Local> {
        Local.with_ymd_and_hms(2026, 11, 23, 12, 0, 0).unwrap()
    }

    fn iso(time: DateTime<Local>) -> String {
        time.to_rfc3339()
    }

    fn task(status: Status) -> Task {
        Task {
            status: Some(status),
            cwd: "/work/repo".into(),
            session_id: "s".into(),
            ..Task::default()
        }
    }

    fn tasks(entries: Vec<(&str, Task)>) -> Tasks {
        entries
            .into_iter()
            .map(|(id, task)| (id.to_string(), task))
            .collect()
    }

    #[test]
    fn card_durations_keep_their_largest_unit() {
        assert_eq!(largest_unit_only("2h 30m ago"), "2h ago");
        assert_eq!(largest_unit_only("for 2d 18h"), "for 2d");
        assert_eq!(
            largest_unit_only("starts today 3:40pm"),
            "starts today 3:40pm"
        );
    }

    #[test]
    fn duration_text_matches_golem() {
        assert_eq!(duration_text(30.0), "<1m");
        assert_eq!(duration_text(5.0 * 60.0), "5m");
        assert_eq!(duration_text(90.0 * 60.0), "1h 30m");
        assert_eq!(duration_text(7.0 * 3600.0 + 120.0), "7h");
        assert_eq!(duration_text(26.0 * 3600.0), "1d 2h");
        assert_eq!(duration_text(400.0 * 86400.0), "1y");
    }

    #[test]
    fn chain_shows_its_most_urgent_step_in_review() {
        let mut parent = task(Status::Finished);
        parent.label = Some("login".into());
        parent.finished_at = Some(iso(now() - Duration::hours(2)));
        let mut child = task(Status::Failed);
        child.after = Some("a1".into());
        child.label = Some("review".into());
        let rows = board_rows(&tasks(vec![("a1", parent), ("a1-2", child)]), now());
        assert_eq!(rows.len(), 1);
        let row = &rows[0];
        assert_eq!(
            (row.task_id.as_str(), row.column, row.step.as_str()),
            ("a1-2", Column::Review, "2/2")
        );
        assert_eq!(row.label, "login");
        assert_eq!(row.chain_ids, vec!["a1".to_string(), "a1-2".to_string()]);
    }

    #[test]
    fn finished_chain_moves_to_done_when_its_pull_requests_close() {
        let mut open = task(Status::Finished);
        open.pull_requests = vec![PullRequest {
            url: "https://x/pull/4".into(),
            state: "open".into(),
        }];
        let mut merged = open.clone();
        merged.pull_requests[0].state = "merged".into();
        let rows = board_rows(&tasks(vec![("a", open), ("b", merged)]), now());
        let columns: Vec<(&str, Column)> = rows
            .iter()
            .map(|row| (row.task_id.as_str(), row.column))
            .collect();
        assert_eq!(columns, vec![("a", Column::Review), ("b", Column::Done)]);
    }

    #[test]
    fn names_text_lists_three_then_counts() {
        assert_eq!(names_text(&["a", "b"]), "a and b");
        assert_eq!(names_text(&["a", "b", "c", "d", "e"]), "a, b, c and 2 more");
    }

    #[test]
    fn filter_matches_what_cards_show() {
        let mut blocked = task(Status::Blocked);
        blocked.gate_stop_reason = Some("3 tests failed in checkout.spec.ts".into());
        blocked.pull_requests = vec![PullRequest {
            url: "https://x/pull/412".into(),
            state: "open".into(),
        }];
        let mut running = task(Status::Running);
        running.prompt = "fix the flaky suite".into();
        let all = tasks(vec![("a", blocked), ("b", running)]);
        let snapshot = Snapshot {
            rows: board_rows(&all, now()),
            tasks: all,
            activity: HashMap::from([("b".into(), vec!["All 10 runs pass now".into()])]),
        };
        let row = |id: &str| snapshot.rows.iter().find(|row| row.task_id == id).unwrap();
        let [blocked_row, running_row] = [row("a"), row("b")];
        for query in ["#412", "CHECKOUT.spec", "a"] {
            assert!(is_matching(blocked_row, &snapshot, query), "{query}");
        }
        assert!(!is_matching(blocked_row, &snapshot, "#41 2"));
        assert!(is_matching(running_row, &snapshot, "10 runs pass"));
        assert!(!is_matching(running_row, &snapshot, "b fix"));
        let (excerpt, range) = match_excerpt(running_row, &snapshot, "FLAKY").unwrap();
        assert_eq!(&excerpt[range], "flaky");
        assert!(excerpt.starts_with("Prompt"));
        assert_eq!(match_excerpt(running_row, &snapshot, "10 runs"), None);
    }

    #[test]
    fn finished_chain_without_pull_request_ages_into_done() {
        let mut old = task(Status::Finished);
        old.finished_at = Some(iso(now() - Duration::days(8)));
        let mut fresh = task(Status::Finished);
        fresh.finished_at = Some(iso(now() - Duration::days(1)));
        let rows = board_rows(&tasks(vec![("old", old), ("new", fresh)]), now());
        assert_eq!(rows[0].task_id, "new");
        assert_eq!(rows[0].column, Column::Review);
        assert_eq!(rows[1].column, Column::Done);
        assert_eq!(rows[0].detail, "no PR; finished 1d ago");
    }

    #[test]
    fn overdue_scheduled_task_with_a_reason_is_stuck() {
        let mut waiting = task(Status::Scheduled);
        waiting.due_at = Some(iso(now() - Duration::hours(3)));
        waiting.setup_directory = Some("/work/repo".into());
        waiting.setup_busy_retry_count = Some(4);
        let all = tasks(vec![("a", waiting)]);
        let row = &board_rows(&all, now())[0];
        assert!(is_stuck(row, &all["a"], now()));
        assert_eq!(card_when_text(row, &all["a"], now()), "stuck 3h");
    }

    #[test]
    fn finished_task_is_new_until_opened() {
        let mut finished = task(Status::Finished);
        finished.finished_at = Some(iso(now()));
        let mut all = tasks(vec![("a", finished)]);
        let row = board_rows(&all, now()).remove(0);
        assert!(is_new_for_review(&all, &row));
        all.get_mut("a").unwrap().opened_at = Some(iso(now()));
        assert!(!is_new_for_review(&all, &row));
    }

    #[test]
    fn activity_view_folds_tool_runs_at_messages_level() {
        let lines: Vec<String> = [
            "Planning",
            "[tool] Read a",
            "[tool] Read b",
            "Done",
            "[tool] Bash make",
        ]
        .map(String::from)
        .to_vec();
        assert_eq!(activity_counts(&lines), "2 messages, 3 tool calls");
        assert_eq!(
            activity_view(&lines, ActivityLevel::Messages),
            vec![
                ActivityItem::Message("Planning".into()),
                ActivityItem::Fold(vec!["Read a".into(), "Read b".into()]),
                ActivityItem::Message("Done".into()),
                ActivityItem::Tool("$ make".into()),
            ]
        );
    }

    #[test]
    fn actions_follow_status() {
        let mut blocked = task(Status::Blocked);
        blocked.session_id = "no-such-session".into();
        let all = tasks(vec![("a", blocked)]);
        let snapshot = Snapshot {
            rows: board_rows(&all, now()),
            tasks: all,
            activity: HashMap::new(),
        };
        let actions = available_actions(&snapshot, snapshot.rows.first());
        assert_eq!(
            actions,
            vec![
                Action::Pickup,
                Action::RerunGate,
                Action::Dismiss,
                Action::Copy
            ]
        );
    }
}
