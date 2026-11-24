//! Runs `golem` the way the TUI does, from a GUI app that starts without the shell's PATH.

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Deserialize;

use crate::ledger;

const GOLEM_PREFIX: &str = "golem: ";
const ENVIRONMENT_MARKER: &str = "__golem_menubar__";
const DEFAULT_TERMINAL: &str = "Terminal";
const CONFIG_FILE_NAME: &str = "menubar.toml";
const INTERRUPTED_EXIT_STATUS: i32 = 130;
const FALLBACK_DIRECTORIES: [&str; 3] = [".local/bin", "/opt/homebrew/bin", "/usr/local/bin"];

static RUN_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Default, Deserialize)]
#[serde(default)]
struct Config {
    terminal: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Golem {
    /// Absolute path to the golem executable, if one was found.
    pub executable: Option<PathBuf>,
    pub path_variable: String,
    pub terminal_app: String,
}

pub struct CommandOutcome {
    pub is_success: bool,
    /// The first line golem printed, without its `golem: ` prefix.
    pub message: String,
}

fn login_shell() -> String {
    std::env::var("SHELL").ok().filter(|shell| !shell.is_empty()).unwrap_or_else(|| "/bin/zsh".to_string())
}

fn config_path() -> PathBuf {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| ledger::home_directory().join(".config"));
    base.join("golem").join(CONFIG_FILE_NAME)
}

fn configured_terminal() -> String {
    let config: Config = std::fs::read_to_string(config_path())
        .ok()
        .and_then(|text| toml::from_str(&text).ok())
        .unwrap_or_default();
    config.terminal.unwrap_or_else(|| DEFAULT_TERMINAL.to_string())
}

/// Asks the login shell for PATH and golem's location, since launchd gives apps neither.
fn shell_environment() -> Option<(String, String)> {
    let script = format!("printf '\\n{ENVIRONMENT_MARKER}%s\\n%s\\n' \"$PATH\" \"$(command -v golem)\"");
    let output = Command::new(login_shell()).args(["-lic", &script]).stdin(Stdio::null()).output().ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let (_, marked) = text.rsplit_once(ENVIRONMENT_MARKER)?;
    let mut lines = marked.lines();
    let path_variable = lines.next()?.to_string();
    let executable = lines.next().unwrap_or("").to_string();
    Some((path_variable, executable))
}

fn fallback_executable(path_variable: &str) -> Option<PathBuf> {
    let home = ledger::home_directory();
    path_variable
        .split(':')
        .map(PathBuf::from)
        .chain(FALLBACK_DIRECTORIES.iter().map(|directory| home.join(directory)))
        .map(|directory| directory.join("golem"))
        .find(|candidate| candidate.is_file())
}

fn shell_quoted(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

fn applescript_quoted(text: &str) -> String {
    format!("\"{}\"", text.replace('\\', "\\\\").replace('"', "\\\""))
}

pub fn first_message(output: &str) -> String {
    let line = output.trim().lines().next().unwrap_or("");
    let message = line.strip_prefix(GOLEM_PREFIX).unwrap_or(line);
    let mut chars = message.chars();
    chars.next().map_or_else(String::new, |first| first.to_uppercase().chain(chars).collect())
}

impl Golem {
    pub fn discover() -> Golem {
        let inherited_path = std::env::var("PATH").unwrap_or_default();
        let (path_variable, reported) = shell_environment().unwrap_or((inherited_path, String::new()));
        let executable = Some(PathBuf::from(&reported))
            .filter(|path| path.is_absolute() && path.is_file())
            .or_else(|| fallback_executable(&path_variable));
        Golem { executable, path_variable, terminal_app: configured_terminal() }
    }

    fn missing_outcome() -> CommandOutcome {
        CommandOutcome { is_success: false, message: "Can't find golem on PATH".to_string() }
    }

    /// Runs `golem <subcommand> <task_id>` to completion and reports its first line of output.
    pub fn run(&self, subcommand: &str, task_id: &str) -> CommandOutcome {
        let Some(executable) = &self.executable else {
            return Golem::missing_outcome();
        };
        // Files, not pipes: an executor that golem starts inherits them and outlives it.
        let run_id = RUN_COUNTER.fetch_add(1, Ordering::Relaxed);
        let output_path = std::env::temp_dir().join(format!("golem-menubar-{}-{run_id}.log", std::process::id()));
        let result = (|| {
            let output_file = std::fs::File::create(&output_path)?;
            let status = Command::new(executable)
                .args([subcommand, task_id])
                .env("PATH", &self.path_variable)
                .stdin(Stdio::null())
                .stdout(output_file.try_clone()?)
                .stderr(output_file)
                .status()?;
            Ok::<_, std::io::Error>((status, std::fs::read_to_string(&output_path).unwrap_or_default()))
        })();
        let _ = std::fs::remove_file(&output_path);
        match result {
            Ok((status, text)) => CommandOutcome { is_success: status.success(), message: first_message(&text) },
            Err(error) => CommandOutcome { is_success: false, message: error.to_string() },
        }
    }

    /// Opens a terminal window that runs `golem pickup <task_id>` and waits for Enter if it fails.
    pub fn pickup_in_terminal(&self, task_id: &str) -> CommandOutcome {
        let Some(executable) = &self.executable else {
            return Golem::missing_outcome();
        };
        let script = format!(
            "PATH={path}; export PATH; {golem} pickup {id}; status=$?; \
             if [ $status -ne 0 ] && [ $status -ne {INTERRUPTED_EXIT_STATUS} ]; then \
             printf 'golem: press Enter to close\\n'; read _; fi",
            path = shell_quoted(&self.path_variable),
            golem = shell_quoted(&executable.to_string_lossy()),
            id = shell_quoted(task_id),
        );
        let result = if self.terminal_app == DEFAULT_TERMINAL {
            let command = format!("/bin/sh -c {}", shell_quoted(&script));
            let apple_script = format!(
                "tell application \"Terminal\"\n activate\n do script {}\nend tell",
                applescript_quoted(&command)
            );
            Command::new("osascript").args(["-e", &apple_script]).output()
        } else {
            Command::new("open")
                .args(["-na", &self.terminal_app, "--args", "-e", "/bin/sh", "-c", &script])
                .output()
        };
        match result {
            Ok(output) if output.status.success() => CommandOutcome { is_success: true, message: String::new() },
            Ok(output) => CommandOutcome {
                is_success: false,
                message: first_message(&String::from_utf8_lossy(&output.stderr)),
            },
            Err(error) => CommandOutcome { is_success: false, message: error.to_string() },
        }
    }
}

pub fn open_url(url: &str) -> CommandOutcome {
    match Command::new("open").arg(url).output() {
        Ok(output) => CommandOutcome {
            is_success: output.status.success(),
            message: first_message(&String::from_utf8_lossy(&output.stderr)),
        },
        Err(error) => CommandOutcome { is_success: false, message: error.to_string() },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_message_drops_prefix_and_capitalizes() {
        assert_eq!(first_message("golem: retried a1\nmore"), "Retried a1");
        assert_eq!(first_message("\n"), "");
    }

    #[test]
    fn shell_quoting_escapes_single_quotes() {
        assert_eq!(shell_quoted("it's"), "'it'\\''s'");
    }
}
