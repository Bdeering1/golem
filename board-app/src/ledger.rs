use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::Deserialize;

pub const LEDGER_FILE_NAME: &str = "tasks.json";
pub const PROGRESS_DIRECTORY_NAME: &str = "progress";

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Scheduled,
    Running,
    Waiting,
    Blocked,
    Failed,
    Finished,
    Cancelled,
}

pub const LIST_STATUS_ORDER: [Status; 7] = [
    Status::Blocked,
    Status::Failed,
    Status::Running,
    Status::Scheduled,
    Status::Waiting,
    Status::Finished,
    Status::Cancelled,
];

impl Status {
    pub fn name(self) -> &'static str {
        match self {
            Status::Scheduled => "scheduled",
            Status::Running => "running",
            Status::Waiting => "waiting",
            Status::Blocked => "blocked",
            Status::Failed => "failed",
            Status::Finished => "finished",
            Status::Cancelled => "cancelled",
        }
    }

    pub fn list_index(self) -> usize {
        LIST_STATUS_ORDER
            .iter()
            .position(|status| *status == self)
            .unwrap_or(0)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct PullRequest {
    pub url: String,
    pub state: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct RepositoryBranch {
    pub repository: String,
    pub branch: String,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(default)]
pub struct Task {
    pub cwd: String,
    pub prompt: String,
    pub agent_args: Vec<String>,
    pub status: Option<Status>,
    pub session_id: String,
    pub backend: String,
    pub label: Option<String>,
    pub after: Option<String>,
    pub due_at: Option<String>,
    pub worktree: Option<String>,
    pub t3_code_thread_id: Option<String>,
    pub t3_code_thread_state: Option<String>,
    pub result: Option<String>,
    pub usage_limit_resume_count: Option<u64>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub opened_at: Option<String>,
    pub closed_at: Option<String>,
    pub gate: Option<String>,
    pub gate_stop_reason: Option<String>,
    pub setup: Option<String>,
    pub setup_failure_reason: Option<String>,
    pub setup_directory: Option<String>,
    pub setup_busy_retry_count: Option<u64>,
    pub repository_branches: Vec<RepositoryBranch>,
    pub pull_requests: Vec<PullRequest>,
    pub command_process_group: Option<i64>,
}

impl Task {
    pub fn status(&self) -> Status {
        self.status.unwrap_or(Status::Cancelled)
    }

    pub fn has_started(&self) -> bool {
        match self.status() {
            Status::Cancelled => self.started_at.is_some(),
            Status::Scheduled | Status::Waiting => false,
            _ => true,
        }
    }

    pub fn is_cancellable(&self) -> bool {
        match self.status() {
            Status::Finished => self.command_process_group.is_some(),
            Status::Scheduled
            | Status::Waiting
            | Status::Running
            | Status::Failed
            | Status::Blocked => true,
            Status::Cancelled => false,
        }
    }

    pub fn is_usage_limit_pause(&self) -> bool {
        self.status() == Status::Scheduled && self.usage_limit_resume_count.unwrap_or(0) > 0
    }
}

pub type Tasks = BTreeMap<String, Task>;

pub fn home_directory() -> PathBuf {
    std::env::home_dir().unwrap_or_else(|| PathBuf::from("/"))
}

pub fn state_directory() -> PathBuf {
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home_directory().join(".local").join("state"));
    base.join("golem")
}

pub fn progress_path(session_id: &str) -> PathBuf {
    state_directory()
        .join(PROGRESS_DIRECTORY_NAME)
        .join(format!("{session_id}.log"))
}

pub fn load_tasks() -> Result<Tasks, String> {
    let path = state_directory().join(LEDGER_FILE_NAME);
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Tasks::new()),
        Err(error) => return Err(error.to_string()),
    };
    serde_json::from_str(&text).map_err(|error| error.to_string())
}
